from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from research_agent.config import Settings, effective_limits, validate_defaults
from research_agent.contracts import Criterion, CriterionAssessment, ResearchRequest, ResearchTask, digest
from research_agent.errors import InvariantViolation, Stopped, ToolFailure
from research_agent.graph.builder import ResearchGraph
from research_agent.graph.source_researcher import SourceResearcher
from research_agent.peer_contracts import PeerQuestion, PeerQuestions, PeerReply
from research_agent.services.operations import Operations
from research_agent.services.peer_review import (
    PeerConsultation,
    apply_peer_gaps,
    pending_messages,
    writing_feedback,
)
from research_agent.services.source_notes import SourceNotes


class MemoryDB:
    def __init__(self):
        self.values, self.kinds = {}, {}
        self.phase = AsyncMock()
        self.fail_final_once = False

    async def saved_value(self, run, key):
        return deepcopy(self.values.get(key))

    async def commit_value(self, run, fence, key, kind, value, task_id=None):
        if self.fail_final_once and key == "peer-consultation:prewrite:v1":
            self.fail_final_once = False
            raise RuntimeError("crash after committed exchanges")
        if key in self.values and self.values[key] != value:
            raise InvariantViolation("immutable operation changed")
        self.values[key], self.kinds[key] = deepcopy(value), kind
        return deepcopy(value)


class PeerModel:
    def __init__(self):
        self.calls = []
        self.resolve_second_round = False
        self.no_questions = False
        self.reply_error = None
        self.max_passages = None

    def build(self, role, payload, schema, correction=None):
        if self.max_passages is not None and len(payload["passages"]) > self.max_passages:
            raise Stopped("input_budget_exceeded")
        return {}, 500

    async def generate(self, key, role, payload, schema, **kwargs):
        self.calls.append((key, role, deepcopy(payload), kwargs))
        if role == "peer_questioner":
            if self.no_questions:
                value = {"questions": []}
            else:
                actor = payload["speaker_task_id"]
                target = next(t["task_id"] for t in payload["researchers"] if t["task_id"] != actor)
                prior = next(
                    (m["question_id"] for m in payload["messages"] if m["sender_task_id"] == actor), None
                )
                value = {
                    "questions": [
                        {
                            "target_task_id": target,
                            "criterion_id": "c0",
                            "kind": "clarification",
                            "question": f"Which condition supports the claim? Round {payload['round']} from {actor}.",
                            "passage_ids": [payload["passages"][0]["passage_id"]],
                            "followup_to": prior,
                        }
                    ]
                }
        else:
            if self.reply_error:
                raise self.reply_error
            clarified = self.resolve_second_round and payload.get("round") == 2
            value = {
                "question_id": payload["question_id"],
                "status": "clarified" if clarified else "needs_research",
                "answer": "The first source qualifies the condition; check the second source's version.",
                "passage_ids": [payload["passages"][0]["passage_id"]] if payload["passages"] else [],
            }
        value = schema.model_validate(value)
        kwargs["validator"](value)
        return value


@pytest.fixture
def setup_peer(monkeypatch):
    config = Settings().snapshot()
    db = MemoryDB()
    tasks, sources, notes = [], {}, []
    for i in range(2):
        task = {
            "task_id": f"t{i}",
            "question": f"Research facet {i}",
            "scope": "Original scope",
            "facet_ids": [f"f{i}"],
            "priority": 1,
            "exclusions": [],
            "seed_queries": [f"public topic aspect {i}"],
            "source_targets": ["official"],
            "source_urls": [],
            "parent_gap_ids": [],
            "max_rounds": 2,
            "success_criteria": [
                {
                    "criterion_id": f"c{i}",
                    "description": f"Original question {i}",
                    "kind": "authoritative_fact",
                    "facet_ids": [f"f{i}"],
                    "required": True,
                }
            ],
        }
        tasks.append(task)
        text = f"Original frozen source {i} states its version and scope."
        sources[f"s{i}"] = {
            "id": f"s{i}",
            "title": f"Source {i}",
            "content_text": text,
            "content_sha256": digest(text),
        }
        notes.append(
            {
                "source_id": f"s{i}",
                "source_hash": digest(text),
                "criterion_ids": [f"c{i}"],
                "material_role": "original",
                "passages": [
                    {
                        "source_id": f"s{i}",
                        "passage_id": f"p{i}",
                        "start_char": 0,
                        "end_char": len(text),
                        "text": text,
                    }
                ],
            }
        )
        db.values[f"reader-context:0:t{i}"] = {"task_id": f"t{i}", "source_ids": [f"s{i}"]}
    monkeypatch.setattr(SourceNotes, "index", AsyncMock(return_value=notes))

    async def get_source(sid):
        return deepcopy(sources[sid])

    services = SimpleNamespace(
        config=config,
        db=db,
        run_id="run",
        fence=1,
        llm=PeerModel(),
        warn=AsyncMock(),
        request=SimpleNamespace(query="Explain the documented behavior"),
        ops=SimpleNamespace(guard=AsyncMock(return_value=300)),
        evidence=SimpleNamespace(
            get_source=get_source, source_index=AsyncMock(return_value=[{"id": sid} for sid in sources])
        ),
    )
    state = {
        "run_id": "run",
        "batch_no": 0,
        "plan_history": [{"tasks": tasks}],
        "memory_snapshot": {},
        "understanding": {
            "facets": [{"facet_id": f"f{i}", "required": True} for i in range(2)],
            "intent": "explanation",
        },
        "query_rewrite": {"scope": "hybrid", "search_terms": ["approved public query"]},
    }
    return services, state, notes


@pytest.mark.asyncio
async def test_two_round_addressed_conversation_is_bounded_and_replayed(setup_peer):
    s, state, _ = setup_peer
    result = await PeerConsultation(s).before_write(state)
    discussion = result["peer_consultation"]
    assert discussion["rounds"] == 2 and len(discussion["messages"]) == 4
    assert len(s.llm.calls) == 8
    for _, role, payload, args in s.llm.calls:
        assert args["task_id"] == payload["speaker_task_id"]
        assert args["partition"] == "research"
        assert payload["discussion_is_not_verified_evidence"]
        if role == "peer_responder":
            assert payload["speaker_task_id"] == payload["question_to_answer"]["target_task_id"]
    assert await PeerConsultation(s).before_write(state) == result
    assert len(s.llm.calls) == 8
    assert set(s.db.kinds.values()) >= {"peer_snapshot", "peer_input", "peer_message", "peer_consultation"}


@pytest.mark.asyncio
async def test_resume_after_lost_checkpoint_reuses_every_committed_reply(setup_peer):
    s, state, _ = setup_peer
    s.db.fail_final_once = True
    with pytest.raises(RuntimeError, match="crash"):
        await PeerConsultation(s).before_write(state)
    assert len(s.llm.calls) == 8
    result = await PeerConsultation(s).before_write(state)
    assert len(s.llm.calls) == 8 and len(result["peer_consultation"]["messages"]) == 4


@pytest.mark.asyncio
async def test_failed_reply_remains_unresolved_and_is_not_retried_on_replay(setup_peer):
    s, state, _ = setup_peer
    s.llm.reply_error = ToolFailure("timeout")
    s.db.fail_final_once = True
    with pytest.raises(RuntimeError):
        await PeerConsultation(s).before_write(state)
    calls = len(s.llm.calls)
    result = await PeerConsultation(s).before_write(state)
    assert len(s.llm.calls) == calls
    assert all(m["reply"]["status"] == "unresolved" for m in result["peer_consultation"]["messages"])


@pytest.mark.asyncio
async def test_pending_concerns_create_gaps_and_safe_supplemental_tasks(setup_peer):
    s, state, _ = setup_peer
    state["plan_history"][0]["tasks"][1]["exclusions"] = ["Do not expand into deployment"]
    state.update(await PeerConsultation(s).before_write(state))
    graph = ResearchGraph(s)
    criteria = graph.all_criteria(state)
    baseline = {"gaps": [], "required_coverage": 1.0, "can_answer": True, "explanation": "Ready"}
    state["gap_assessment"] = apply_peer_gaps(state, baseline, criteria)
    assert len(state["gap_assessment"]["gaps"]) == 1
    assert not state["gap_assessment"]["can_answer"]
    assert state["gap_assessment"]["required_coverage"] == 0.5
    task = graph.fallback_tasks(state, batch=1)[0]
    assert task.seed_queries == ["approved public query"]
    assert task.exclusions == ["Do not expand into deployment"]
    assert task.success_criteria[0].description == "Original question 0"
    assert "待核对" in task.question
    new_state = {
        **state,
        "batch_no": 1,
        "plan_history": state["plan_history"] + [{"tasks": [task.model_dump()]}],
    }
    dispatch = graph.dispatch(new_state)
    assert dispatch[0].arg["peer_questions"]
    assert dispatch[0].arg["peer_questions"][0]["source_ids"] == ["s0"]


@pytest.mark.asyncio
async def test_peer_clarification_does_not_promote_material_coverage(setup_peer):
    s, state, _ = setup_peer
    s.llm.resolve_second_round = True
    state.update(await PeerConsultation(s).before_write(state))
    assert pending_messages(state["peer_consultation"]) == []
    baseline = {
        "gaps": [],
        "required_coverage": 0.0,
        "can_answer": False,
        "explanation": "No independent review",
    }
    assert apply_peer_gaps(state, baseline, ResearchGraph(s).all_criteria(state)) == baseline
    data = writing_feedback(state)
    assert data["peer_consultation"]["status"] == "advisory_only_not_verified_evidence"
    assert "passage_ids" not in str(data)


@pytest.mark.asyncio
async def test_optional_concerns_never_become_required(setup_peer):
    s, state, _ = setup_peer
    state["plan_history"][0]["tasks"][0]["success_criteria"][0]["required"] = False
    state.update(await PeerConsultation(s).before_write(state))
    baseline = {"gaps": [], "required_coverage": 1.0, "can_answer": True, "explanation": "Ready"}
    result = apply_peer_gaps(state, baseline, ResearchGraph(s).all_criteria(state))
    assert result["gaps"][0]["priority"] == 3 and result["can_answer"]


@pytest.mark.asyncio
async def test_no_useful_question_stops_after_one_round(setup_peer):
    s, state, _ = setup_peer
    s.llm.no_questions = True
    result = await PeerConsultation(s).before_write(state)
    assert not result["peer_consultation"]["messages"]
    assert len(s.llm.calls) == 2


@pytest.mark.asyncio
async def test_one_researcher_and_disabled_snapshots_skip_prewrite(setup_peer):
    s, state, _ = setup_peer
    state["plan_history"][0]["tasks"] = state["plan_history"][0]["tasks"][:1]
    assert (await PeerConsultation(s).before_write(state))["peer_consultation"]["status"] == "skipped"
    assert not s.llm.calls
    s.config.pop("peer_review_workflow_version")
    assert await PeerConsultation(s).before_write(state) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["budget_exhausted", "deadline"])
async def test_optional_consultation_stops_without_claiming_success_or_blocking_writing(setup_peer, reason):
    s, state, _ = setup_peer
    s.ops.guard.side_effect = Stopped(reason)
    result = await PeerConsultation(s).before_write(state)
    assert result["peer_consultation"]["status"] == "partial"
    assert result["peer_consultation"]["reason"] == reason
    assert "stop_reason" not in result and not s.llm.calls


@pytest.mark.asyncio
async def test_cancel_propagates_and_does_not_finalize_consultation(setup_peer):
    s, state, _ = setup_peer
    s.ops.guard.side_effect = Stopped("cancelled")
    with pytest.raises(Stopped, match="cancelled"):
        await PeerConsultation(s).before_write(state)
    assert "peer-consultation:prewrite:v1" not in s.db.values


@pytest.mark.asyncio
async def test_forged_source_note_cannot_enter_discussion(setup_peer):
    s, state, notes = setup_peer
    notes[0]["passages"][0]["text"] = "fabricated quotation"
    with pytest.raises(InvariantViolation, match="frozen source"):
        await PeerConsultation(s).before_write(state)
    assert not s.llm.calls


@pytest.mark.asyncio
async def test_input_packing_drops_whole_passages_and_freezes_the_offered_set(setup_peer):
    s, state, _ = setup_peer
    s.llm.max_passages = 1
    await PeerConsultation(s).before_write(state)
    for _, _, payload, _ in s.llm.calls:
        assert len(payload["passages"]) == 1 and payload["omitted_passages"] == 1
        assert payload["passages"][0]["text"].endswith("version and scope.")


def question_payload():
    return {
        "speaker_task_id": "t0",
        "researchers": [
            {"task_id": "t0", "criteria": [{"criterion_id": "c0"}]},
            {"task_id": "t1", "criteria": [{"criterion_id": "c1"}]},
        ],
        "passages": [{"passage_id": "E0"}, {"passage_id": "E1"}],
        "messages": [],
    }


@pytest.mark.parametrize(
    "change",
    [
        {"target_task_id": "t0"},
        {"target_task_id": "invented"},
        {"criterion_id": "invented"},
        {"passage_ids": ["fake"]},
        {"passage_ids": []},
        {"kind": "conflict"},
        {"followup_to": "fake"},
    ],
)
def test_invalid_recipient_criterion_and_evidence_are_rejected(change):
    question = {
        "target_task_id": "t1",
        "criterion_id": "c0",
        "kind": "clarification",
        "question": "Check scope",
        "passage_ids": ["E0"],
        **change,
    }
    with pytest.raises(ValueError):
        PeerConsultation.validate_questions(
            PeerQuestions(questions=[PeerQuestion(**question)]), question_payload()
        )


def test_reply_cannot_invent_evidence_or_resolve_by_consensus():
    payload = {"question_id": "q0", "passages": [{"passage_id": "E0"}]}
    for qid, citations in [("fake", ["E0"]), ("q0", ["fake"]), ("q0", [])]:
        with pytest.raises(ValueError):
            PeerConsultation.validate_reply(
                PeerReply(question_id=qid, status="clarified", answer="We agree", passage_ids=citations),
                payload,
            )


@pytest.mark.asyncio
async def test_postwrite_question_reaches_source_owner_and_keeps_verifier_findings(setup_peer):
    s, state, _ = setup_peer
    state.update(
        source_draft={
            "sections": [
                {
                    "paragraphs": [
                        {"paragraph_id": "B0", "text": "An unsupported claim.", "passage_ids": ["P0"]}
                    ]
                }
            ]
        },
        source_materials={"passages": {"P0": {"source_id": "s1"}}},
        whole_answer_review={
            "issues": [{"kind": "fact", "paragraph_ids": ["B0"], "problem": "Check the condition"}],
            "important_facts": [],
            "coverage": [{"criterion_id": "Q1", "paragraph_ids": ["B0"]}],
        },
    )
    before = deepcopy(state)
    result = await PeerConsultation(s).after_review(state)
    message = result["postwrite_consultation"]["messages"][0]
    assert message["sender_task_id"] == "answer_verifier"
    assert message["question"]["target_task_id"] == "t1"
    assert message["question"]["criterion_id"] == "c1"
    assert s.llm.calls[0][3]["partition"] == "finalization"
    assert s.llm.calls[0][2]["answer_blocks"][0]["text"] == "An unsupported claim."
    assert state == before
    assert await PeerConsultation(s).after_review(state) == result
    assert len(s.llm.calls) == 1


@pytest.mark.asyncio
async def test_peer_supplement_rereads_even_if_old_readiness_was_covered(setup_peer):
    s, state, _ = setup_peer
    s.understanding = state["understanding"]
    researcher = SourceResearcher(s)
    researcher.criterion_assessments = AsyncMock(
        return_value=[
            CriterionAssessment(
                criterion_id="c0",
                status="covered",
                supporting_claim_ids=[],
                contradicting_claim_ids=[],
                explanation="Old readiness",
            )
        ]
    )
    task_state = {
        "task": state["plan_history"][0]["tasks"][0],
        "batch_no": 1,
        "peer_questions": [
            {"question_id": "q0", "criterion_id": "c0", "question": "Recheck scope", "source_ids": ["s0"]}
        ],
    }
    patch = await researcher.guard(task_state)
    assert patch["stop_reason"] is None
    assert (await researcher.select_read({**task_state, **patch}))["pending_source_ids"] == ["s0"]
    assert s.peer_task_questions["t0"] == task_state["peer_questions"]


@pytest.mark.asyncio
async def test_focused_read_has_a_distinct_source_note_cache_key(setup_peer):
    s, state, _ = setup_peer
    s.understanding = {
        "standalone_query": "Question",
        "reader_goal": "Explain",
        "search_terms": ["public query"],
    }
    notes = SourceNotes(s)
    notes.planned_queries = ["public query"]
    notes.criteria = AsyncMock(
        return_value={
            "c0": Criterion.model_validate(state["plan_history"][0]["tasks"][0]["success_criteria"][0])
        }
    )
    s.db.saved_value = AsyncMock(return_value={"chunk_ids": ["chunk"]})
    task = ResearchTask.model_validate(state["plan_history"][0]["tasks"][0])
    page = SimpleNamespace(content_sha256="hash")
    await notes.read("s0", page, task)
    original_key = s.db.saved_value.call_args.args[1]
    s.peer_task_questions = {"t0": [{"question_id": "q0", "question": "Check version"}]}
    await notes.read("s0", page, task)
    assert s.db.saved_value.call_args.args[1] != original_key


def test_graph_has_conditional_peer_node_and_limits_do_not_expand_total_budget(setup_peer):
    s, _, _ = setup_peer
    edges = {(e.source, e.target) for e in ResearchGraph(s).compile(None).get_graph().edges}
    assert ("merge", "peer_consultation") in edges and ("peer_consultation", "gaps") in edges
    limits = effective_limits(ResearchRequest(query="topic"), s.config)
    assert limits["tokens_peer_review"] == 48000 and limits["peer_review_http_attempts"] == 16
    assert limits["total_tokens"] == sum(
        limits[k] for k in ("tokens_research", "tokens_finalization", "tokens_memory")
    )
    s.config["peer_review_workflow_version"] = 0
    edges = {(e.source, e.target) for e in ResearchGraph(s).compile(None).get_graph().edges}
    assert ("merge", "gaps") in edges
    assert "tokens_peer_review" not in effective_limits(ResearchRequest(query="topic"), s.config)


@pytest.mark.asyncio
async def test_peer_calls_share_atomic_budget_accounting_and_actual_usage(setup_peer):
    s, _, _ = setup_peer
    ops = Operations.__new__(Operations)
    ops.locks, ops.config = {}, s.config
    ops.cached = AsyncMock(return_value=None)
    ops.guard = AsyncMock(return_value=300)

    async def reserve(key, kind, hashed, requested, task_id, partition):
        return {"reservation": requested, "attempt": 1}

    ops.reserve = AsyncMock(side_effect=reserve)
    ops.monitored = AsyncMock(return_value={"usage": {"total_tokens": 123}})
    ops.settle = AsyncMock()
    await ops.run(
        "peer", "llm", {}, AsyncMock(), tokens=500, partition="research", model_role="peer_questioner"
    )
    requested = ops.reserve.call_args.args[3]
    assert requested["tokens_peer_review"] == requested["tokens_research"] == requested["total_tokens"] == 500
    assert requested["peer_review_http_attempts"] == 1
    actual = ops.settle.call_args.kwargs["actual"]
    assert actual["tokens_peer_review"] == actual["tokens_research"] == actual["total_tokens"] == 123
    assert actual["peer_review_http_attempts"] == 1


def test_configuration_rejects_unbounded_peer_loops():
    config = Settings().defaults()
    config["peer_review"]["max_rounds"] = 3
    with pytest.raises(ValueError, match="peer"):
        validate_defaults(config)


@pytest.mark.asyncio
async def test_private_peer_reread_does_not_require_unused_web_budget(setup_peer):
    s, state, _ = setup_peer
    state.update(await PeerConsultation(s).before_write(state))
    graph = ResearchGraph(s)
    state["gap_assessment"] = apply_peer_gaps(
        state,
        {"gaps": [], "required_coverage": 1.0, "can_answer": True, "explanation": "Ready"},
        graph.all_criteria(state),
    )
    s.ops.research_headroom = AsyncMock(return_value=40000)
    s.db.get_run = AsyncMock(
        return_value={
            "budget": {
                "limits": {"search_calls": 0, "fetched_pages": 0, "tokens_research": 40000},
                "used": {},
                "reserved": {},
            }
        }
    )
    assert await graph.after_gaps(state) == "plan"
    assert await graph.after_gaps({**state, "peer_consultation": {}}) == "write"


@pytest.mark.asyncio
async def test_recorded_model_result_can_be_replayed_after_optional_stage_deadline(setup_peer):
    s, state, _ = setup_peer
    peer = PeerConsultation(s)
    snapshot = await peer.snapshot(state, "snapshot")
    payload = {**peer.payload(state, snapshot, []), "speaker_task_id": "t0", "round": 1}
    s.db.values["recover:model"] = {"content": '{"questions":[]}'}
    s.llm.no_questions = True
    s.ops.guard.side_effect = Stopped("deadline")
    result = await peer.call(
        "recover",
        "peer_questioner",
        payload,
        PeerQuestions,
        peer.validate_questions,
        actor="t0",
        partition="research",
    )
    assert result.questions == []
    s.ops.guard.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("node", ["read_existing", "fetch_extract"])
async def test_read_focus_survives_checkpoint_resume_directly_into_reader(setup_peer, monkeypatch, node):
    from research_agent.graph.researcher import Researcher

    s, state, _ = setup_peer
    task_state = {
        "task": state["plan_history"][0]["tasks"][0],
        "peer_questions": [{"question_id": "q0", "question": "Recheck exact scope"}],
    }

    async def read(self, current):
        assert self.s.peer_task_questions["t0"] == current["peer_questions"]
        return {"restored": True}

    monkeypatch.setattr(Researcher, node, read)
    assert await getattr(SourceResearcher(s), node)(task_state) == {"restored": True}
