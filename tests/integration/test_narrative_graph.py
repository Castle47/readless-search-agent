import json
from copy import deepcopy
from uuid import uuid4
from research_agent.contracts import ResearchRequest
from research_agent.services.runtime import Services
from research_agent.storage.checkpoint import saver
from research_agent.storage.db import all_rows
from research_agent.workers.research_worker import execute
from tests.fakes import ScriptedLLM, FixtureSearch, FixtureFetch, FixtureMemory
from tests.integration.test_graph import SCENARIOS


async def run_fixture(db, settings, model):
    scenario = json.loads((SCENARIOS / "authoritative_fact/scenario.json").read_text())
    page = scenario["pages"][0]
    page.update(
        text="Cedar accepts raw text. Cedar returns a boolean. This is the documented input and output contract.",
        quote="Cedar accepts raw text.",
        claim="Cedar accepts raw text.",
    )
    await db.create_run(
        ResearchRequest(query="Explain Cedar input and output.", memory_mode="off"),
        str(uuid4()),
        settings.snapshot(),
    )
    run = await db.claim("narrative-test")
    s = Services(settings, db, run)
    await s.close()
    s.llm = model(s.ops, scenario)
    s.search = FixtureSearch(s.ops, scenario)
    s.fetch = FixtureFetch(s.ops, scenario)
    s.memory = FixtureMemory()
    async with saver(settings) as checkpointer:
        await execute(settings, db, run, checkpointer, s)
    return s, await db.get_run(str(run["id"])), await s.evidence.report()


class ConnectedModel(ScriptedLLM):
    def response(self, role, p):
        result = super().response(role, p)
        if role == "extract":
            first = result["candidates"][0]
            second = deepcopy(first)
            text = "Cedar returns a boolean."
            chunk = p["chunks"][0]["text"]
            start = chunk.index(text)
            second.update(quote=text, claim_text=text, start_char=start, end_char=start + len(text))
            result["candidates"].append(second)
        if role == "writer":
            result = {
                "title": "Cedar input and output",
                "sections": [
                    {
                        "heading": "",
                        "paragraphs": [
                            {
                                "text": "Cedar accepts raw text and returns a boolean.",
                                "claim_ids": [c["claim_id"] for c in p["claims"]],
                                "evidence_ids": list(
                                    dict.fromkeys(
                                        e["evidence_id"] for c in p["claims"] for e in c["evidence_links"]
                                    )
                                ),
                            }
                        ],
                    }
                ],
            }
        if role == "report_verifier":
            for audit in result["paragraphs"]:
                audit["atoms"] = []
                for c in p["claims"]:
                    anchor = "Cedar accepts raw text" if "accepts" in c["text"] else "returns a boolean"
                    eid = next(e["evidence_id"] for e in p["evidence"] if c["text"] in e["quote"])
                    audit["atoms"].append(
                        {
                            "text": c["text"],
                            "anchor": anchor,
                            "kind": "fact",
                            "claim_ids": [c["claim_id"]],
                            "evidence_ids": [eid],
                            "verdict": "entailment",
                            "answered_criterion_ids": [p["criteria"][0]["criterion_id"]],
                        }
                    )

        return result


async def test_connected_prose_keeps_two_atomic_citations_and_roundtrips(db, settings):
    s, run, report = await run_fixture(db, settings, ConnectedModel)
    assert run["status"] == "completed", run.get("error")
    assert report.structured.schema_version == 3
    p = report.structured.sections[0].paragraphs[0]
    assert p.text == "Cedar accepts raw text and returns a boolean."
    assert len(p.assertions) == 2 and len(report.citations) == 2
    assert {c.paragraph_id for c in report.citations} == {"p1"}
    byid = {a.statement_id: a for a in p.assertions}
    for c in report.citations:
        assert p.text[c.start_char : c.end_char] == byid[c.statement_id].anchor
    assert report.structured.quality_status == "passed"
    assert all(q.status == "answered" for q in report.structured.coverage if q.required)


async def test_auditor_decomposes_bundled_writer_hint_without_destroying_prose(db, settings):
    class Bundled(ConnectedModel):
        def response(self, role, p):
            result = super().response(role, p)
            if role == "report_verifier":
                assert all(len(para["citation_hints"]) == 1 for para in p["paragraphs"])
                assert all(para["citation_hints"][0]["anchor"] == para["text"] for para in p["paragraphs"])
            return result

    _, run, report = await run_fixture(db, settings, Bundled)
    assert run["status"] == "completed"
    para = report.structured.sections[0].paragraphs[0]
    assert len(para.assertions) == 2 and para.text == "Cedar accepts raw text and returns a boolean."


async def test_writer_prose_needs_no_span_annotations(db, settings):
    class Transitional(ConnectedModel):
        def response(self, role, p):
            value = super().response(role, p)
            if role == "writer":
                value["sections"][0]["paragraphs"][0]["text"] = (
                    "Here is the interface: Cedar accepts raw text and returns a boolean."
                )
            return value

    _, run, report = await run_fixture(db, settings, Transitional)
    assert run["status"] == "completed"
    assert len(report.structured.sections[0].paragraphs[0].assertions) == 2
    assert report.structured.sections[0].paragraphs[0].text.startswith("Here is the interface:")


async def test_incomplete_writer_hints_can_resolve_to_real_links_without_losing_prose(db, settings):
    class IncompleteHint(ConnectedModel):
        def response(self, role, p):
            value = super().response(role, p)
            if role == "writer":
                para = value["sections"][0]["paragraphs"][0]
                para["claim_ids"] = para["claim_ids"][:1]
            return value

    _, run, report = await run_fixture(db, settings, IncompleteHint)
    assert run["status"] == "completed"
    assert len(report.citations) == 2
    assert report.structured.sections[0].paragraphs[0].text == "Cedar accepts raw text and returns a boolean."


async def test_discourse_atom_uses_bounded_schema_correction_without_deleting_paragraph(db, settings):
    from research_agent.adapters.llm import LLM

    attempts = []

    class DiscourseModel(ConnectedModel):
        def response(self, role, p):
            value = super().response(role, p)
            if role == "writer":
                value["sections"][0]["paragraphs"][0]["text"] += " This is an illustration."
            return value

        async def generate(self, key, role, payload, schema, **kwargs):
            if role != "report_verifier":
                return await super().generate(key, role, payload, schema, **kwargs)
            llm = LLM(settings, self.ops)

            async def respond(body, timeout):
                result = self.response(role, payload)
                if not attempts:
                    result["paragraphs"][0]["atoms"].append(
                        {
                            "text": "This is an illustration.",
                            "anchor": "This is an illustration.",
                            "kind": "inference",
                            "verdict": "entailment",
                            "claim_ids": [],
                            "evidence_ids": [],
                            "answered_criterion_ids": [],
                        }
                    )
                attempts.append(body)
                if schema.__name__ == "CompactBatchParagraphAudit":
                    for paragraph in result["paragraphs"]:
                        for atom in paragraph["atoms"]:
                            atom.pop("text", None)
                return {
                    "content": json.dumps(result),
                    "finish_reason": "stop",
                    "usage": {"total_tokens": 100},
                }

            llm.request = respond
            try:
                return await llm.generate(key, role, payload, schema, **kwargs)
            finally:
                await llm.close()

    _, run, report = await run_fixture(db, settings, DiscourseModel)
    assert run["status"] == "completed"
    assert len(attempts) == 2
    assert "discourse" in attempts[1]["messages"][-1]["content"]
    assert report.structured.sections[0].paragraphs[0].text.endswith("This is an illustration.")
    assert len(report.citations) == 2


async def test_known_hint_ids_never_approve_wrong_final_claim_evidence_links(db, settings):
    class WrongLinks(ConnectedModel):
        def response(self, role, p):
            value = super().response(role, p)
            if role == "report_verifier":
                for para in value["paragraphs"]:
                    for atom in para["atoms"]:
                        atom["evidence_ids"] = [
                            next(
                                e["evidence_id"]
                                for e in p["evidence"]
                                if e["evidence_id"] not in atom["evidence_ids"]
                            )
                        ]
            return value

    _, run, report = await run_fixture(db, settings, WrongLinks)
    assert run["status"] == "partial"
    assert "Cedar accepts raw text and returns a boolean." not in report.markdown
    assert not any(c.paragraph_id for c in report.citations)
    assert any(w["code"] == "editorial.blocks_rejected" for w in run["warnings"])


async def test_fluid_unannotated_claim_cannot_hide_behind_valid_citations(db, settings):
    class HiddenClaim(ConnectedModel):
        def response(self, role, p):
            result = super().response(role, p)
            if role == "writer":
                result["sections"][0]["paragraphs"][0]["text"] += " It is always correct."
            if role == "report_verifier":
                for paragraph in result["paragraphs"]:
                    paragraph.update(complete=False, unsupported_text=["It is always correct."])
            return result

    s, run, report = await run_fixture(db, settings, HiddenClaim)
    assert run["status"] == "partial"
    assert "It is always correct." not in report.markdown
    assert any(w["code"] == "editorial.blocks_rejected" for w in run["warnings"])


async def test_unresolved_quality_issue_is_not_force_accepted(db, settings):
    class PoorWriting(ConnectedModel):
        def response(self, role, p):
            result = super().response(role, p)
            if role == "quality":
                return {
                    "issues": [
                        {
                            "paragraph_ids": ["p1"],
                            "problem": "Central term is unexplained.",
                            "suggestion": "Explain the documented output clearly.",
                        }
                    ],
                    "strengths": [],
                }
            return result

    s, run, report = await run_fixture(db, settings, PoorWriting)
    assert run["status"] == "partial"
    assert report.structured.quality_status == "issues_remaining"
    assert len([r for r, _ in s.llm.calls if r == "quality"]) == 1
    assert len([r for r, _ in s.llm.calls if r == "editor"]) == 1


async def test_rejected_heading_does_not_discard_independently_entailed_body(db, settings):
    class BadHeading(ConnectedModel):
        def response(self, role, p):
            value = super().response(role, p)
            if role == "writer":
                value["sections"][0]["heading"] = "Always accurate"
            if role == "report_verifier":
                for audit in value["paragraphs"]:
                    if any(para["heading"] == "Always accurate" for para in p["paragraphs"]):
                        audit.update(headings_supported=False, unsupported_text=["Always accurate"])
            return value

    _, run, report = await run_fixture(db, settings, BadHeading)
    assert run["status"] == "completed"
    assert "Always accurate" not in report.markdown
    assert report.structured.sections[0].paragraphs[0].text == "Cedar accepts raw text and returns a boolean."


async def test_unsupported_body_matching_heading_is_still_rejected(db, settings):
    class BadBody(ConnectedModel):
        def response(self, role, p):
            value = super().response(role, p)
            if role == "writer":
                value["sections"][0]["heading"] = "Always accurate"
                value["sections"][0]["paragraphs"][0]["text"] += " Always accurate."
            if role == "report_verifier":
                for audit in value["paragraphs"]:
                    audit.update(headings_supported=False, unsupported_text=["Always accurate"])
            return value

    _, run, report = await run_fixture(db, settings, BadBody)
    assert run["status"] == "partial"
    assert "Always accurate" not in report.markdown


async def test_verifier_can_correct_extraction_criterion_mapping(db, settings):
    class CorrectMapping(ConnectedModel):
        def response(self, role, p):
            result = super().response(role, p)
            if role == "understand":
                result["understanding"]["intent"] = "explanation"
            if role == "planner" and not p["batch_no"]:
                extra = deepcopy(result["tasks"][0]["success_criteria"][0])
                extra.update(criterion_id="c1", description="Cedar output type")
                result["tasks"][0]["success_criteria"].append(extra)
            if role == "extract":
                for c in result["candidates"]:
                    c["criterion_ids"] = ["C0"]
            if role == "evidence_verifier":
                for v in result["verdicts"]:
                    v["criterion_ids"] = ["C1"] if len(p["criteria"]) > 1 else []
            return result

    s, run, report = await run_fixture(db, settings, CorrectMapping)
    assert run["status"] == "partial"  # Original C0 remains unanswered, not silently removed.
    async with db.pool.connection() as conn:
        rows = await all_rows(conn, "SELECT specification FROM research.tasks WHERE run_id=%s", (run["id"],))
        c1 = next(
            c["criterion_id"]
            for row in rows
            for c in row["specification"]["success_criteria"]
            if c["description"] == "Cedar output type"
        )
        mappings = await all_rows(
            conn, "SELECT criterion_id FROM research.task_claims WHERE run_id=%s", (run["id"],)
        )
    assert mappings and {str(m["criterion_id"]) for m in mappings} == {c1}


async def test_reference_page_does_not_become_primary_for_external_product(db, settings):
    scenario = json.loads((SCENARIOS / "authoritative_fact/scenario.json").read_text())
    scenario["pages"][0]["url"] = "https://en.wikipedia.org/wiki/Example_product"
    await db.create_run(
        ResearchRequest(query="Explain the external product.", memory_mode="off"),
        str(uuid4()),
        settings.snapshot(),
    )
    run = await db.claim("reference-test")
    s = Services(settings, db, run)
    await s.close()
    s.llm = ScriptedLLM(s.ops, scenario)
    s.search = FixtureSearch(s.ops, scenario)
    s.fetch = FixtureFetch(s.ops, scenario)
    s.memory = FixtureMemory()
    async with saver(settings) as cp:
        await execute(settings, db, run, cp, s)
    result = await db.get_run(str(run["id"]))
    assert result["status"] == "partial"
    bundle = await s.evidence.bundle()
    assert bundle["relations"] and all(
        not r["verification"]["primary_for_claim"] for r in bundle["relations"]
    )
