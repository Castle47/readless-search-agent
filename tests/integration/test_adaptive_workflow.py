"""Reader-centred workflow, real persistence and checkpoints, no provider calls."""

import json
import pytest
from uuid import uuid4
from copy import deepcopy

from research_agent.config import Settings
from research_agent.contracts import ResearchRequest
from research_agent.services.runtime import Services
from research_agent.storage.checkpoint import saver
from research_agent.workers.research_worker import execute
from tests.fakes import FixtureFetch, FixtureSearch, FixtureMemory
from tests.integration.test_graph import SCENARIOS
from tests.integration.test_source_workflow import SourceModel


class AdaptiveModel(SourceModel):
    needs_edit = False
    cannot_repair = False
    needs_extra = False
    always_missing = False

    def response(self, role, p):
        if role == "source_selector":
            if self.needs_extra:
                return {
                    "decisions": [
                        {
                            "candidate_id": c["candidate_id"],
                            "read": i == 0,
                            "reason": "Complementary fixture source",
                        }
                        for i, c in enumerate(p["candidates"])
                    ]
                }
        if role == "readiness":
            return {
                "criteria": [
                    {
                        "criterion_id": c["criterion_id"],
                        "ready": not self.always_missing,
                        "explained_parts": ["Input and output behavior"],
                        "missing_parts": ["Mechanism still missing"] if self.always_missing else [],
                        "source_ids": [s["source_id"] for s in p["sources"]],
                    }
                    for c in p["criteria"]
                ],
                "synthesis_notes": "Explain the documented interface and how it is used.",
                "source_contributions": [
                    {"source_id": s["source_id"], "adds_value": True, "contribution": "Documented behavior."}
                    for s in p["sources"]
                ],
                "candidate_decisions": [
                    {
                        "candidate_id": c["candidate_id"],
                        "read": False,
                        "reason": "Fixture scope is already explained.",
                    }
                    for c in p["unread_candidates"]
                ],
                "search_queries": [],
            }
        if role == "editor":
            value = super().response("writer", p)
            if not self.cannot_repair:
                for sec in value["sections"]:
                    for b in sec["paragraphs"]:
                        b["text"] = b["text"].replace(" It is always right.", "")
            return value
        if role == "report_verifier":
            result = {
                "blocks": [],
                "important_facts": [],
                "coverage": [],
                "issues": [],
                "research_queries": [],
            }
            paragraphs = [b for s in p["draft"]["sections"] for b in s["paragraphs"]]
            source_by_id = {s["source_id"]: s for s in p["sources"]}
            for b in paragraphs:
                bad = "always right" in b["text"] or not self.identity
                passage = next(x for x in p["passages"] if "Cedar accepts raw text." in x["text"])
                primary = source_by_id[passage["source_id"]]["identity_established"]
                result["blocks"].append(
                    {
                        "paragraph_id": b["paragraph_id"],
                        "supported": not bad,
                        "kind": "fact",
                        "passage_ids": [passage["passage_id"]],
                    }
                )
                result["important_facts"].append(
                    {
                        "paragraph_id": b["paragraph_id"],
                        "statement": "Cedar accepts raw text",
                        "kind": "product_contract",
                        "supported": self.identity,
                        "passage_ids": [passage["passage_id"]],
                        "primary_passage_ids": [passage["passage_id"]] if primary else [],
                        "explanation": "Original interface description",
                    }
                )
                if bad:
                    result["issues"].append(
                        {
                            "paragraph_ids": [b["paragraph_id"]],
                            "kind": "fact",
                            "problem": "Unsupported guarantee or missing original identity",
                            "suggestion": "Remove the unsupported guarantee while preserving the explanation.",
                        }
                    )
                    if self.needs_extra:
                        result["research_queries"] = ["Cedar documented guarantees"]
            good = not result["issues"]
            result["coverage"] = [
                {
                    "criterion_id": c["criterion_id"],
                    "answered": good,
                    "paragraph_ids": [b["paragraph_id"] for b in paragraphs],
                    "missing": "" if good else "Unresolved interface statement",
                }
                for c in p["answer_criteria"]
            ]
            return result
        return super().response(role, p)


async def fixture(db, settings, model=AdaptiveModel, scenario_update=None):
    scenario = json.loads((SCENARIOS / "authoritative_fact/scenario.json").read_text())
    scenario["pages"][0].update(
        text="Cedar accepts raw text. Cedar returns a boolean. The caller uses that boolean to branch.",
        quote="Cedar accepts raw text.",
        claim="Cedar accepts raw text.",
    )
    if model.needs_extra:
        extra = deepcopy(scenario["pages"][0])
        extra["url"] = "https://additional.example/cedar"
        extra["text"] += " Cedar does not guarantee every decision is correct."
        scenario["pages"].append(extra)
    if scenario_update:
        scenario_update(scenario)
    config = Settings(model=settings.model).snapshot()
    await db.create_run(ResearchRequest(query="Explain Cedar.", memory_mode="off"), str(uuid4()), config)
    run = await db.claim("adaptive-fixture", lease_seconds=config["worker"]["lease_seconds"])
    s = Services(settings, db, run)
    await s.close()
    s.llm = model(s.ops, scenario)
    s.search, s.fetch, s.memory = (
        FixtureSearch(s.ops, scenario),
        FixtureFetch(s.ops, scenario),
        FixtureMemory(),
    )
    async with saver(settings) as cp:
        await execute(settings, db, run, cp, s)
        checkpoint = await cp.aget_tuple({"configurable": {"thread_id": s.run_id}})
    return s, await db.get_run(s.run_id), checkpoint.checkpoint["channel_values"]


@pytest.mark.parametrize("model", ["openai/gpt-5.6-luna", "openai/gpt-6-astra"])
async def test_survey_precedes_plan_and_complete_answer_has_exact_source_citations(db, settings, model):
    settings.model = model
    s, run, state = await fixture(db, settings)
    assert run["status"] == "completed", run.get("error")
    roles = [r for r, _ in s.llm.calls]
    assert roles.index("source_selector") < roles.index("planner")
    assert "readiness" in roles and roles.count("report_verifier") == 1
    assert "editor" not in roles and "quality" not in roles
    report = await s.evidence.report()
    assert report.execution.model_id == model
    from research_agent.api.app import public_run

    assert public_run(run)["model"] == model
    assert report.structured.sections[0].paragraphs[0].text == "Cedar accepts raw text and returns a boolean."
    assert report.citations and not report.structured.limitations
    bundle = await s.evidence.bundle()
    for evidence in bundle["evidence"]:
        source = await s.evidence.get_source(evidence["source_id"])
        assert source["content_text"][evidence["start_char"] : evidence["end_char"]] == evidence["quote"]
    assert await db.saved_value(s.run_id, "answer:accepted:v1")
    assert state["whole_answer_review"]["issues"] == []


async def test_editor_followed_by_full_answer_review_not_just_changed_blocks(db, settings):
    class Edit(AdaptiveModel):
        needs_edit = True

    s, run, state = await fixture(db, settings, Edit)
    assert run["status"] == "completed", run.get("error")
    roles = [r for r, _ in s.llm.calls]
    assert roles.count("writer") == 1 and roles.count("editor") == 1 and roles.count("report_verifier") == 2
    assert roles.index("editor") < max(i for i, r in enumerate(roles) if r == "report_verifier")
    assert state["adaptive_revision"]["whole_answer_rechecked"]
    assert "always right" not in (await s.evidence.report()).markdown


async def test_unresolved_answer_preserves_full_draft_and_never_publishes_spliced_fragments(db, settings):
    class Bad(AdaptiveModel):
        needs_edit = cannot_repair = True

    s, run, state = await fixture(db, settings, Bad)
    assert run["status"] == "partial", run.get("error")
    assert not (await s.evidence.report()).structured.sections
    raw = await db.saved_value(s.run_id, "draft:edited:v1")
    assert "Cedar accepts raw text" in raw["sections"][0]["paragraphs"][0]["text"]
    assert "always right" in raw["sections"][0]["paragraphs"][0]["text"]
    assert not await db.saved_value(s.run_id, "answer:accepted:v1")


async def test_review_can_trigger_targeted_search_and_read_before_editing(db, settings):
    class Supplement(AdaptiveModel):
        needs_edit = needs_extra = True

    s, run, state = await fixture(db, settings, Supplement)
    assert run["status"] == "completed", run.get("error")
    assert state["adaptive_revision"]["supplemental_source_ids"]
    roles = [r for r, _ in s.llm.calls]
    assert "source_reader" in roles[roles.index("report_verifier") + 1 : roles.index("editor")]


async def test_semantic_missing_part_is_not_overridden_by_primary_source_tag(db, settings):
    class Missing(AdaptiveModel):
        always_missing = True

    s, run, state = await fixture(db, settings, Missing)
    assert state["gap_assessment"]["gaps"]
    assert state["gap_assessment"]["required_coverage"] == 0
    assert len(state["plan_history"]) == 2


async def test_original_interface_without_established_identity_stays_unverified(db, settings):
    class NoOriginal(AdaptiveModel):
        identity = False

    s, run, state = await fixture(db, settings, NoOriginal)
    assert run["status"] == "partial", run.get("error")
    assert not await db.saved_value(s.run_id, "answer:accepted:v1")
    assert await db.saved_value(s.run_id, "draft:initial:v1")
    assert not (await s.evidence.report()).structured.sections


async def test_provenance_gap_is_repair_feedback_not_a_format_failure(db, settings):
    class MissingLink(AdaptiveModel):
        def response(self, role, payload):
            value = super().response(role, payload)
            if role == "report_verifier" and sum(r == role for r, _ in self.calls) == 1:
                for fact in value["important_facts"]:
                    fact["primary_passage_ids"] = []
            return value

    s, run, state = await fixture(db, settings, MissingLink)
    assert run["status"] == "completed", run.get("error")
    roles = [r for r, _ in s.llm.calls]
    assert roles.count("report_verifier") == 2 and roles.count("editor") == 1
    assert not any(w["code"] == "model.output_invalid" for w in run["warnings"])


async def test_optional_readiness_notes_do_not_trigger_retries_or_new_tasks(db, settings):
    class OptionalNote(AdaptiveModel):
        def response(self, role, payload):
            value = super().response(role, payload)
            if role == "readiness":
                for item in value["criteria"]:
                    item["missing_parts"] = ["An optional historical example could be added."]
            return value

    s, run, state = await fixture(db, settings, OptionalNote)
    assert run["status"] == "completed", run.get("error")
    assert len(state["plan_history"]) == 1
    assert not any(w["code"] == "model.output_invalid" for w in run["warnings"])


async def test_reader_packet_packing_runs_off_the_heartbeat_loop(db, settings):
    import threading

    main_thread = threading.get_ident()
    packing_threads = set()

    class Packing(AdaptiveModel):
        def build(self, role, *args, **kwargs):
            if role == "source_reader":
                packing_threads.add(threading.get_ident())
            return super().build(role, *args, **kwargs)

    s, run, _ = await fixture(db, settings, Packing)
    assert run["status"] == "completed", run.get("error")
    assert packing_threads - {main_thread}


async def test_observed_arxiv_pdf_is_resolved_without_inventing_a_new_work(db, settings):
    from research_agent.services.retrieval import eligible_candidates

    await db.create_run(
        ResearchRequest(query="A paper", memory_mode="off"),
        str(uuid4()),
        Settings(model=settings.model).snapshot(),
    )
    run = await db.claim("paper-fixture")
    s = Services(settings, db, run)
    try:
        candidates, rejected = await eligible_candidates(
            s, [{"url": "https://arxiv.org/pdf/1706.03762v7", "title": "An observed paper"}]
        )
        assert not rejected and len(candidates) == 1
        assert candidates[0]["url"] == "https://arxiv.org/abs/1706.03762v7"
        assert candidates[0]["observed_pdf_url"] == "https://arxiv.org/pdf/1706.03762v7"
        assert candidates[0]["document"]["work_id"] == "arxiv:1706.03762"
    finally:
        await db.terminal(s.run_id, s.fence, "cancelled", "fixture_done")
        await s.close()


class OverviewModel(AdaptiveModel):
    def response(self, role, p):
        value = super().response(role, p)
        if role == "understand":
            value["understanding"].update(
                intent="explanation",
                search_terms=["Cedar overview / Cedar 概览", "Cedar original specification"],
            )
        elif role == "source_selector":
            for i, decision in enumerate(value["decisions"]):
                decision.update(
                    read=i < min(3, p["max_pages"]), material_role="original" if i == 0 else "explanation"
                )
        elif role == "planner" and not p["batch_no"]:
            value["tasks"][0]["source_urls"] = [
                link["url"] for source in p["discovery"]["sources"] for link in source["reference_links"]
            ]
        elif role == "readiness":
            for decision in value["candidate_decisions"]:
                decision.update(
                    read=True, reason="The planned implementation supplies an end-to-end example."
                )
        return value


async def test_overview_reads_past_first_pair_and_processes_planned_unfetched_source(db, settings):
    def scenario_update(scenario):
        original = scenario["pages"][0]
        for i in range(3):
            source = deepcopy(original)
            source.update(
                url=f"https://teaching{i}.example/cedar", text=original["text"] + f"\nWorked example {i}."
            )
            scenario["pages"].append(source)
        original["metadata"] = {
            "reference_links": [
                {
                    "url": scenario["pages"][-1]["url"],
                    "label": "Annotated implementation",
                    "documentation": True,
                }
            ]
        }

    s, run, state = await fixture(db, settings, OverviewModel, scenario_update)
    assert run["status"] == "completed", run.get("error")
    assert len([r for r, _ in s.llm.calls if r == "source_reader"]) == 4
    assert len(next(iter(state["task_results"].values()))["criterion_results"]) == 1
    assert next(iter(state["task_results"].values()))["rounds"] == 3
    reviews = [p for r, p in s.llm.calls if r == "readiness"]
    assert min(len(p["sources"]) for p in reviews) == 3
    assert any(any(c["fetched"] is False for c in p["unread_candidates"]) for p in reviews)
    assert all(" / " not in q for q in s.search.queries)


async def test_one_available_work_finishes_after_bounded_breadth_search(db, settings):
    s, run, state = await fixture(db, settings, OverviewModel)
    assert run["status"] == "completed", run.get("error")
    assert len([r for r, _ in s.llm.calls if r == "source_reader"]) == 1
    result = next(iter(state["task_results"].values()))
    assert result["stop_reason"] == "sufficient" and result["rounds"] == 3
    reviews = [p for r, p in s.llm.calls if r == "readiness"]
    assert reviews[-1]["breadth_policy"]["extra_searches"] == 2


async def test_unproven_reader_identity_withholds_authority_without_discarding_reading(db, settings):
    from research_agent.services.source_notes import SourceNotes

    class Unproven(AdaptiveModel):
        def response(self, role, p):
            value = super().response(role, p)
            if role == "source_reader":
                value["source_identity"]["quote"] = "Not present in the source."
            return value

    s, run, state = await fixture(db, settings, Unproven)
    assert run["status"] == "partial", run.get("error")
    notes = await SourceNotes(s).index()
    assert notes and all(
        n["passages"] and not n["identity"] and not n["firsthand_criterion_ids"] for n in notes
    )
    assert len([r for r, _ in s.llm.calls if r == "source_reader"]) == 1
    assert any(w["code"] == "reading.identity_unverified" for w in run["warnings"])
    assert not await s.db.saved_value(s.run_id, "answer:accepted:v1")


async def test_one_unreadable_selected_source_does_not_abort_other_sources(db, settings):
    from research_agent.errors import ToolFailure
    from research_agent.services.source_notes import SourceNotes

    class BrokenSource(OverviewModel):
        async def generate(self, key, role, payload, schema, **kwargs):
            if role == "source_reader" and "official.example.org" in payload["source"]["url"]:
                raise ToolFailure("network_error")
            return await super().generate(key, role, payload, schema, **kwargs)

        def response(self, role, payload):
            value = super().response(role, payload)
            if role == "source_selector":
                for decision in value["decisions"]:
                    decision["read"] = True
            return value

    def extra_sources(scenario):
        for i in range(3):
            source = deepcopy(scenario["pages"][0])
            source.update(url=f"https://supplement{i}.example/read", text=source["text"] + f" Example {i}.")
            scenario["pages"].append(source)

    s, run, state = await fixture(db, settings, BrokenSource, extra_sources)
    assert run["status"] == "completed", run.get("error")
    assert len({n["source_id"] for n in await SourceNotes(s).index()}) == 3
    assert any(w["code"] == "reading.source_unavailable" for w in run["warnings"])


@pytest.mark.parametrize("reuse_draft", [True, False])
async def test_frozen_answer_reuses_exact_material_and_prose_but_runs_fresh_review(db, settings, reuse_draft):
    from research_agent.graph.builder import ResearchGraph
    from research_agent.services.frozen_answer import copy_frozen_answer

    old, old_run, original = await fixture(db, settings)
    config = Settings(model=settings.model).snapshot()
    config["research"].update(post_write_pages=0, post_write_searches=0)
    request = ResearchRequest.model_validate_json(json.dumps(old_run["request"], default=str))
    await db.create_run(request, str(uuid4()), config)
    run = await db.claim("frozen-answer-test", lease_seconds=90)
    s = Services(settings, db, run)
    await s.close()
    s.llm = AdaptiveModel(s.ops, old.llm.scenario)
    s.search, s.fetch, s.memory = (
        FixtureSearch(s.ops, old.llm.scenario),
        FixtureFetch(s.ops, old.llm.scenario),
        FixtureMemory(),
    )
    state = await copy_frozen_answer(old.evidence, s, original, reuse_draft=reuse_draft)
    if reuse_draft:
        assert state["source_draft"] == original["source_draft"]
    else:
        assert "source_draft" not in state and "source_materials" not in state
    assert not (await s.evidence.bundle())["claims"]
    s.understanding = state["understanding"]
    async with saver(settings) as cp:
        graph = ResearchGraph(s).compile(cp)
        await graph.aupdate_state(
            {"configurable": {"thread_id": s.run_id}}, state, as_node="write" if reuse_draft else "gaps"
        )
        await execute(settings, db, run, cp, s)
    assert (await db.get_run(s.run_id))["status"] == "completed"
    assert (await db.get_run(old.run_id))["status"] == old_run["status"]
    assert [r for r, _ in s.llm.calls] == (
        ["report_verifier"] if reuse_draft else ["writer", "report_verifier"]
    )
    assert s.search.calls == 0 and s.fetch.calls == 0
    assert (await s.evidence.report()).citations
