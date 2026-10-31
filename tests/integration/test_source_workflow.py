"""Source-first production graph against real PostgreSQL, no external calls."""

import json
from uuid import uuid4
from urllib.parse import urlsplit
import pytest
from research_agent.contracts import ResearchRequest
from research_agent.services.runtime import Services
from research_agent.storage.checkpoint import saver
from research_agent.storage.db import all_rows
from research_agent.workers.research_worker import execute
from tests.fakes import ScriptedLLM, FixtureFetch, FixtureSearch, FixtureMemory
from tests.integration.test_graph import SCENARIOS


class SourceModel(ScriptedLLM):
    identity = True
    invalid_quote = False
    needs_edit = False

    async def generate(self, key, role, payload, schema, **kwargs):
        if role == "writer":
            async with self.ops.db.pool.connection() as conn:
                assert not await all_rows(
                    conn, "SELECT id FROM research.claims WHERE run_id=%s", (self.ops.run_id,)
                )
        return await super().generate(key, role, payload, schema, **kwargs)

    def response(self, role, p):
        if role == "understand":
            value = super().response(role, p)
            value["understanding"]["needs_discovery"] = True
            return value
        if role == "planner":
            original = super().response(role, p)
            return {
                "tasks": [
                    {
                        "question": t["question"],
                        "facet_ids": t["facet_ids"],
                        "queries": t["seed_queries"],
                        "source_targets": t["source_targets"],
                        "source_urls": t.get("source_urls", []),
                        "criteria": []
                        if p["batch_no"]
                        else [
                            {
                                "question": c["description"],
                                "required": c.get("required", True),
                                "kind": c["kind"],
                                "facet_ids": c.get("facet_ids") or t["facet_ids"],
                            }
                            for c in t["success_criteria"]
                        ],
                        "existing_criterion_ids": [c["criterion_id"] for c in t["success_criteria"]]
                        if p["batch_no"]
                        else [],
                        "gap_ids": t["parent_gap_ids"],
                    }
                    for t in original["tasks"]
                ]
            }
        if role == "source_reader":
            publisher = urlsplit(p["source"]["url"]).hostname
            quote = f"Published by {publisher}. This is our original specification."
            contexts = p["source_identity_context"]["contexts"] + [
                {"context_id": x["passage_id"], "text": x["text"]} for x in p["passages"]
            ]
            context = next(x for x in contexts if quote in x["text"])
            return {
                "passage_ids": [x["passage_id"] for x in p["passages"]],
                "addressed_criterion_ids": [x["criterion_id"] for x in p["criteria"]],
                "firsthand_criterion_ids": [x["criterion_id"] for x in p["criteria"]]
                if self.identity
                else [],
                "notes": "Input text is converted to a boolean. This is source material, not a verdict.",
                "source_identity": {
                    "basis": "original_publication",
                    "publisher_name": publisher,
                    "context_id": context["context_id"],
                    "quote": quote,
                    "link_id": None,
                    "explanation": "Explicit original publisher.",
                }
                if self.identity
                else None,
            }
        if role in {"writer", "editor"}:
            value = {
                "title": "Cedar input and output",
                "sections": [
                    {
                        "heading": "How it works",
                        "paragraphs": [
                            {
                                "text": "Cedar accepts raw text and returns a boolean.",
                                "block_type": "paragraph",
                                "language": None,
                                "illustrative": False,
                                "passage_ids": [x["passage_id"] for x in p["passages"]],
                            }
                        ],
                    }
                ],
            }
            if role == "writer" and self.needs_edit:
                value["sections"][0]["paragraphs"][0]["text"] += " It is always right."
            if role == "editor":
                for sec in value["sections"]:
                    sec["paragraphs"] = [
                        {"reuse_paragraph_id": None, "replacement": p} for p in sec["paragraphs"]
                    ]
                value.update(
                    addressed_feedback_ids=[f["feedback_id"] for f in p["feedback"]], remaining_issues=[]
                )
            return value
        if role == "report_verifier":
            paragraphs = []
            for para in p["paragraphs"]:
                body = "".join(s["text"] for s in para["segments"])
                atoms = []
                for anchor, quote in [
                    ("Cedar accepts raw text", "Cedar accepts raw text."),
                    ("returns a boolean", "Cedar returns a boolean."),
                ]:
                    passage = next(x for x in p["passages"] if quote in x["text"])
                    atoms.append(
                        {
                            "anchor_ids": [s["anchor_id"] for s in para["segments"] if anchor in s["text"]],
                            "anchor": anchor,
                            "kind": "fact",
                            "verdict": "entailment",
                            "criterion_ids": [q["criterion_id"] for q in p["criteria"]],
                            "answered_criterion_ids": [q["criterion_id"] for q in p["criteria"]],
                            "supports": [
                                {
                                    "passage_id": passage["passage_id"],
                                    "quote": "Invented quote." if self.invalid_quote else quote,
                                    "primary_for_claim": self.identity,
                                    "direct": True,
                                }
                            ],
                        }
                    )
                bad = "always right" in body
                paragraphs.append(
                    {
                        "paragraph_id": para["paragraph_id"],
                        "atoms": atoms,
                        "discourse_ids": [],
                        "complete": not bad,
                        "headings_supported": True,
                        "unsupported_text": ["It is always right."] if bad else [],
                        "explanation": "Unsupported guarantee." if bad else "",
                    }
                )
            return {"paragraphs": paragraphs}
        if role == "quality":
            return {"issues": [], "strengths": ["Connected I/O explanation."]}
        return super().response(role, p)


async def source_fixture(db, settings, model=SourceModel, tasks=1, transform=None):
    scenario = json.loads((SCENARIOS / "authoritative_fact/scenario.json").read_text())
    scenario["tasks"] = tasks
    scenario["pages"][0].update(
        text="Cedar accepts raw text. Cedar returns a boolean. The caller uses that boolean to branch.",
        quote="Cedar accepts raw text.",
        claim="Cedar accepts raw text.",
    )
    if transform:
        transform(scenario)
    config = settings.snapshot()
    config["source_first_workflow_version"] = 1
    await db.create_run(ResearchRequest(query="Explain Cedar.", memory_mode="off"), str(uuid4()), config)
    run = await db.claim("source-test")
    s = Services(settings, db, run)
    await s.close()
    s.llm = model(s.ops, scenario)
    s.fetch = FixtureFetch(s.ops, scenario)
    s.search = FixtureSearch(s.ops, scenario)
    s.memory = FixtureMemory()
    async with saver(settings) as cp:
        await execute(settings, db, run, cp, s)
    return s, await db.get_run(str(run["id"]))


async def test_source_first_graph_creates_facts_only_after_answer_audit_and_preserves_spans(db, settings):
    s, run = await source_fixture(db, settings)
    assert run["status"] == "completed", run.get("error")
    report = await s.evidence.report()
    bundle = await s.evidence.bundle()
    assert report.structured.schema_version == 3 and len(report.citations) == 2
    assert report.structured.sections[0].paragraphs[0].text == "Cedar accepts raw text and returns a boolean."
    assert not ({"extract", "evidence_verifier", "merge"} & {role for role, _ in s.llm.calls})
    for citation in report.citations:
        e = next(e for e in bundle["evidence"] if e["id"] == citation.evidence_id)
        source = await s.evidence.get_source(e["source_id"])
        assert source["content_text"][e["start_char"] : e["end_char"]] == e["quote"]
        p = report.structured.sections[0].paragraphs[0]
        assert p.text[citation.start_char : citation.end_char] in p.text


async def test_parallel_tasks_share_source_read_but_final_coverage_uses_audited_answer(db, settings):
    s, run = await source_fixture(db, settings, tasks=2)
    assert run["status"] == "completed"
    assert sum(role == "source_reader" for role, _ in s.llm.calls) == 1
    assert run["coverage"] == 1


async def test_source_notes_do_not_promote_unidentified_source_to_primary(db, settings):
    class NoIdentity(SourceModel):
        identity = False

    s, run = await source_fixture(db, settings, NoIdentity)
    assert run["status"] == "partial"
    assert any(role == "reason" and payload.get("research_notes") for role, payload in s.llm.calls)
    report = await s.evidence.report()
    assert report.structured.sections == [] and not report.citations
    assert "原文摘录" not in report.markdown
    assert all(not r["verification"]["primary_for_claim"] for r in (await s.evidence.bundle())["relations"])


async def test_fabricated_final_quote_never_becomes_evidence(db, settings):
    class BadQuote(SourceModel):
        invalid_quote = True

    s, run = await source_fixture(db, settings, BadQuote)
    assert run["status"] == "partial", run.get("error")
    assert not (await s.evidence.bundle())["evidence"]
    assert not (await s.evidence.report()).citations


async def test_whole_editor_rechecks_the_revised_answer_once(db, settings):
    class Edit(SourceModel):
        needs_edit = True

    s, run = await source_fixture(db, settings, Edit)
    assert run["status"] == "completed", run.get("error")
    roles = [role for role, _ in s.llm.calls]
    assert roles.count("editor") == 1 and roles.count("report_verifier") == 2
    assert "always right" not in (await s.evidence.report()).markdown


@pytest.mark.parametrize("boundary", ["source_note", "final_audit"])
async def test_source_note_and_final_audit_resume_without_duplicate_paid_attempts(db, settings, boundary):
    from tests.integration.test_recovery import Crash

    scenario = json.loads((SCENARIOS / "authoritative_fact/scenario.json").read_text())
    scenario["pages"][0].update(
        text="Cedar accepts raw text. Cedar returns a boolean.",
        quote="Cedar accepts raw text.",
        claim="Cedar accepts raw text.",
    )
    config = settings.snapshot()
    config["source_first_workflow_version"] = 1
    await db.create_run(ResearchRequest(query="Explain Cedar.", memory_mode="off"), str(uuid4()), config)
    run = await db.claim("first-source-worker")
    rid = str(run["id"])

    async def services(row):
        s = Services(settings, db, row)
        await s.close()
        s.llm = SourceModel(s.ops, scenario)
        s.fetch = FixtureFetch(s.ops, scenario)
        s.search = FixtureSearch(s.ops, scenario)
        s.memory = FixtureMemory()
        return s

    s = await services(run)
    target = s.db if boundary == "source_note" else s.evidence
    method = "commit_value" if boundary == "source_note" else "persist"
    original = getattr(target, method)
    fired = False

    async def crash(*args, **kwargs):
        nonlocal fired
        value = await original(*args, **kwargs)
        if not fired and (boundary == "final_audit" or args[3] == "source_note"):
            fired = True
            raise Crash()
        return value

    setattr(target, method, crash)
    async with saver(settings) as cp:
        with pytest.raises(Crash):
            await execute(settings, db, run, cp, s)
    setattr(target, method, original)
    assert fired
    async with db.pool.connection() as conn:
        await conn.execute(
            "UPDATE research.runs SET lease_until=now()-interval '1 second' WHERE id=%s", (rid,)
        )
    resumed = await db.claim("resumed-source-worker")
    s2 = await services(resumed)
    async with saver(settings) as cp:
        await execute(settings, db, resumed, cp, s2)
    assert (await db.get_run(rid))["status"] == "completed"
    async with db.pool.connection() as conn:
        rows = await all_rows(
            conn,
            "SELECT o.operation_key,count(a.id) AS attempts FROM research.operations o JOIN research.operation_attempts a ON a.operation_id=o.id WHERE o.run_id=%s AND o.kind='llm' GROUP BY o.operation_key",
            (rid,),
        )
        assert all(r["attempts"] == 1 for r in rows)
    assert len((await s2.evidence.bundle())["claims"]) == 2


async def test_audit_includes_competing_source_even_if_writer_only_cites_one():
    from types import SimpleNamespace
    from research_agent.contracts import Criterion
    from research_agent.services.source_workflow import SourceWorkflow, draft_of
    from research_agent.source_contracts import SourceComposition

    material = {
        "sources": {
            s: {"title": s, "canonical_url": "https://" + s + ".example/", "identity": {}}
            for s in ["S0", "S1"]
        },
        "passages": {
            "P0": {
                "source_alias": "S0",
                "text": "The value is A.",
                "start_char": 0,
                "table_header": "",
                "criterion_ids": ["c"],
            },
            "P1": {
                "source_alias": "S1",
                "text": "The value is B.",
                "start_char": 0,
                "table_header": "",
                "criterion_ids": ["c"],
            },
        },
    }
    draft = draft_of(
        SourceComposition.model_validate(
            {
                "title": "Value",
                "sections": [
                    {
                        "heading": "",
                        "paragraphs": [{"text": "The value is A.", "passage_ids": ["P0"]}],
                    }
                ],
            }
        )
    )
    graph = SimpleNamespace(
        all_criteria=lambda state: {
            "c": Criterion(criterion_id="c", description="What value?", kind="corroborated")
        }
    )
    service = SimpleNamespace(llm=SimpleNamespace(build=lambda *args: ({}, 0)))
    payload, _, _ = SourceWorkflow(service, graph).audit_payload(
        draft.sections[0].paragraphs, {"source_materials": material, "source_draft": draft.model_dump()}
    )
    assert {p["passage_id"] for p in payload["passages"]} == {"P0", "P1"}


async def test_null_quote_reuses_exact_original_span_without_model_copying(db, settings):
    class SpanReference(SourceModel):
        def response(self, role, payload):
            value = super().response(role, payload)
            if role == "report_verifier":
                for p in value["paragraphs"]:
                    for a in p["atoms"]:
                        for support in a["supports"]:
                            support["quote"] = None
            return value

    s, run = await source_fixture(db, settings, SpanReference)
    assert run["status"] == "completed"
    bundle = await s.evidence.bundle()
    for e in bundle["evidence"]:
        source = await s.evidence.get_source(e["source_id"])
        assert source["content_text"][e["start_char"] : e["end_char"]] == e["quote"]
        assert len(e["quote"]) <= 1200


def test_long_original_passages_use_bounded_exact_spans_and_lists_keep_all_items():
    from research_agent.services.source_workflow import audit_spans, draft_of
    from research_agent.source_contracts import SourceComposition

    text = ("Original sentence.\n" * 150) + "Final sentence."
    spans, aliases = audit_spans(
        {"P0": {"text": text, "start_char": 70, "source_alias": "S0", "table_header": ""}}
    )
    assert "".join(p["text"] for p in spans.values()) == text
    assert all(len(p["text"]) <= 1200 for p in spans.values())
    assert all(text[p["start_char"] - 70 : p["end_char"] - 70] == p["text"] for p in spans.values())
    c = SourceComposition.model_validate(
        {
            "title": "Example",
            "sections": [
                {
                    "heading": "Uses",
                    "paragraphs": [
                        {"text": "First item", "block_type": "list", "passage_ids": ["P0"]},
                        {"text": "Second item", "block_type": "list", "passage_ids": ["P1"]},
                    ],
                }
            ],
        }
    )
    d = draft_of(c)
    assert len(d.sections[0].paragraphs) == 1
    assert d.sections[0].paragraphs[0].text == "First item\nSecond item"
    assert d.sections[0].paragraphs[0].passage_ids == ["P0", "P1"]


async def test_editor_reuses_a_verified_block_without_another_audit(db, settings):
    class Reuse(SourceModel):
        def response(self, role, payload):
            if role == "editor":
                pid = payload["draft"]["sections"][0]["paragraphs"][0]["paragraph_id"]
                return {
                    "title": payload["draft"]["title"],
                    "sections": [
                        {
                            "heading": "How it works",
                            "paragraphs": [{"reuse_paragraph_id": pid, "replacement": None}],
                        }
                    ],
                    "addressed_feedback_ids": [f["feedback_id"] for f in payload["feedback"]],
                    "remaining_issues": [],
                }
            value = super().response(role, payload)
            if role == "writer":
                first = value["sections"][0]["paragraphs"][0]
                value["sections"][0]["paragraphs"].append(
                    {**first, "text": first["text"] + " It is always right."}
                )
            return value

    s, run = await source_fixture(db, settings, Reuse)
    assert run["status"] == "completed"
    roles = [role for role, _ in s.llm.calls]
    assert roles.count("editor") == 1 and roles.count("report_verifier") == 1
    report = await s.evidence.report()
    assert len(report.structured.sections[0].paragraphs) == 1
    assert report.citations


async def test_writer_budget_stop_is_preserved_in_public_run_and_execution(db, settings):
    from research_agent.errors import Stopped

    class NoWritingBudget(SourceModel):
        async def generate(self, key, role, payload, schema, **kwargs):
            if role == "writer":
                raise Stopped("budget_exhausted")
            return await super().generate(key, role, payload, schema, **kwargs)

    s, run = await source_fixture(db, settings, NoWritingBudget)
    assert run["status"] == "partial" and run["stop_reason"] == "budget_exhausted"
    report = await s.evidence.report()
    assert report.execution.stop_reason == "budget_exhausted"
    assert report.structured.sections == [] and not report.citations


async def test_second_publisher_relay_does_not_satisfy_independent_corroboration(db, settings):
    from copy import deepcopy

    def two_sources(scenario):
        scenario["criterion_kind"] = "corroborated"
        source = scenario["pages"][0]
        source["url"] = "https://publisher.example/spec"
        repeat = deepcopy(source)
        repeat["url"] = "https://mirror.example/article"
        scenario["pages"].append(repeat)

    class Relay(SourceModel):
        def response(self, role, payload):
            value = super().response(role, payload)
            if role == "report_verifier":
                sources = {s["source_id"]: s for s in payload["sources"]}
                for paragraph in value["paragraphs"]:
                    for atom in paragraph["atoms"]:
                        quote = atom["supports"][0]["quote"]
                        atom["supports"] = [
                            {
                                "passage_id": p["passage_id"],
                                "quote": quote,
                                "primary_for_claim": sources[p["source_id"]]["url"].startswith(
                                    "https://publisher.example"
                                ),
                                "direct": sources[p["source_id"]]["url"].startswith(
                                    "https://publisher.example"
                                ),
                            }
                            for p in payload["passages"]
                            if quote in p["text"]
                        ]
            return value

    s, run = await source_fixture(db, settings, Relay, transform=two_sources)
    assert run["status"] == "partial"
    bundle = await s.evidence.bundle()
    assert len(bundle["sources"]) == 2
    # Inconsistent complete=true output is stopped before creating claims.
    # Its saved raw audit still identifies the second publisher as a relay.
    assert not bundle["relations"] and not bundle["claims"]
    async with db.pool.connection() as conn:
        raw = await all_rows(
            conn, "SELECT result FROM research.operations WHERE run_id=%s AND kind='llm'", (s.run_id,)
        )
    assert any(
        not support["direct"]
        for row in raw
        for p in (row["result"] or {}).get("payload", {}).get("paragraphs", [])
        for atom in p.get("atoms", [])
        for support in atom["supports"]
    )
    assert (await s.evidence.report()).structured.sections == []


async def test_checker_can_reference_original_body_without_copying_or_rewording_it(db, settings):
    class SegmentReferences(SourceModel):
        def response(self, role, payload):
            value = super().response(role, payload)
            if role == "report_verifier":
                for paragraph in value["paragraphs"]:
                    supports = [s for atom in paragraph["atoms"] for s in atom["supports"]]
                    atom = paragraph["atoms"][0]
                    paragraph["atoms"] = [{**atom, "anchor": None, "supports": supports}]
            return value

    s, run = await source_fixture(db, settings, SegmentReferences)
    assert run["status"] == "completed"
    report = await s.evidence.report()
    p = report.structured.sections[0].paragraphs[0]
    assert p.assertions[0].anchor == p.text
    assert all(p.text[c.start_char : c.end_char] == p.text for c in report.citations)


async def test_optional_example_cannot_block_essential_answer(db, settings):
    class OptionalExample(SourceModel):
        def response(self, role, p):
            value = super().response(role, p)
            if role == "understand":
                value["understanding"]["intent"] = "explanation"
            if role == "planner" and p["batch_no"] == 0:
                task = value["tasks"][0]
                task["criteria"].append(
                    {
                        "question": "Optional third-party example",
                        "kind": "authoritative_fact",
                        "facet_ids": task["facet_ids"],
                        "required": False,
                    }
                )
            if role == "source_reader":
                value["addressed_criterion_ids"] = value["addressed_criterion_ids"][:1]
                value["firsthand_criterion_ids"] = value["firsthand_criterion_ids"][:1]
            if role == "report_verifier":
                for paragraph in value["paragraphs"]:
                    for atom in paragraph["atoms"]:
                        atom["criterion_ids"] = atom["criterion_ids"][:1]
                        atom["answered_criterion_ids"] = atom["answered_criterion_ids"][:1]
            return value

    s, run = await source_fixture(db, settings, OptionalExample)
    assert run["status"] == "completed", run.get("error")
    report = await s.evidence.report()
    assert report.structured.coverage[0].status == "answered"
    # Inspect immutable plan storage, not just the final aggregate status.
    async with s.db.pool.connection() as conn:
        rows = await all_rows(
            conn, "SELECT payload FROM research.plans WHERE run_id=%s AND batch_no=0", (s.run_id,)
        )
    assert [c["required"] for c in rows[0]["payload"]["tasks"][0]["success_criteria"]] == [True, False]


async def test_later_reader_receives_attributed_unverified_source_context(db, settings):
    def second_page(scenario):
        scenario["pages"].append(
            {
                **scenario["pages"][0],
                "url": "https://official.example.org/general-docs",
                "text": scenario["pages"][0]["text"] + " General interface documentation.",
            }
        )

    s, run = await source_fixture(db, settings, transform=second_page)
    assert run["status"] == "completed", run.get("error")
    payloads = [p for role, p in s.llm.calls if role == "source_reader"]
    with_context = [p for p in payloads if p["related_source_leads"]]
    assert with_context
    for p in with_context:
        for lead in p["related_source_leads"]:
            assert lead["verified_facts"] is False and lead["url"] != p["source"]["url"]
            assert lead["source_hash"] and lead["passages"]
            source = await s.evidence.get_source(lead["source_id"])
            for span in lead["passages"]:
                assert "passage_id" not in span and "criterion_ids" not in span
                assert source["content_text"][span["start_char"] : span["end_char"]] == span["text"]


async def test_verifier_format_failure_is_not_given_to_editor_as_a_fact_objection(db, settings):
    from research_agent.errors import ToolFailure

    class UnavailableAudit(SourceModel):
        async def generate(self, key, role, payload, schema, **kwargs):
            if role == "report_verifier" and not kwargs.get("is_recheck"):
                raise ToolFailure("structured_output_invalid")
            if role == "quality":
                assert "fact_feedback" not in payload
                assert "audit_execution_feedback" not in payload
            if role == "editor":
                execution = [f for f in payload["feedback"] if f.get("feedback_kind") == "audit_execution"]
                assert execution and "does not show a defect" in execution[0]["suggestion"]
            return await super().generate(key, role, payload, schema, **kwargs)

    s, run = await source_fixture(db, settings, UnavailableAudit)
    assert run["status"] == "completed", run.get("error")
    assert (await s.evidence.report()).structured.sections[0].paragraphs[
        0
    ].text == "Cedar accepts raw text and returns a boolean."


async def test_audit_budget_stop_keeps_initial_draft_and_reports_reason(db, settings):
    from research_agent.errors import Stopped

    class NoAuditBudget(SourceModel):
        async def generate(self, key, role, payload, schema, **kwargs):
            if role == "report_verifier":
                raise Stopped("budget_exhausted")
            if role in {"quality", "editor"}:
                raise AssertionError("Exhausted audit must stop cleanly")
            return await super().generate(key, role, payload, schema, **kwargs)

    s, run = await source_fixture(db, settings, NoAuditBudget)
    assert run["status"] == "partial" and run["stop_reason"] == "budget_exhausted"
    raw = await db.saved_value(s.run_id, "draft:initial:v1")
    assert raw["sections"][0]["paragraphs"][0]["text"] == "Cedar accepts raw text and returns a boolean."
    assert (await s.evidence.report()).execution.stop_reason == "budget_exhausted"
