import json
from pathlib import Path
from uuid import uuid4
import pytest
from research_agent.contracts import ResearchRequest, canonical
from research_agent.services.runtime import Services
from research_agent.storage.checkpoint import saver
from research_agent.workers.research_worker import execute
from research_agent.storage.db import all_rows
from tests.fakes import ScriptedLLM, FixtureSearch, FixtureFetch, FixtureMemory

SCENARIOS = Path(__file__).parents[1] / "fixtures/scenarios"


@pytest.mark.parametrize("name", sorted(p.name for p in SCENARIOS.iterdir() if p.is_dir()))
async def test_research_scenario(name, db, settings):
    scenario = json.loads((SCENARIOS / name / "scenario.json").read_text())
    request = ResearchRequest.model_validate_json(
        canonical(
            {
                "query": "Research the fixture specification.",
                "memory_mode": scenario.get("memory_mode", "off"),
                **({"as_of": scenario["as_of"]} if scenario.get("as_of") else {}),
            }
        )
    )
    await db.create_run(request, str(uuid4()), settings.snapshot())
    run = await db.claim("fixture-worker")
    rid = str(run["id"])
    services = Services(settings, db, run)
    await services.close()
    services.llm = ScriptedLLM(services.ops, scenario)
    services.search = FixtureSearch(services.ops, scenario)
    services.fetch = FixtureFetch(services.ops, scenario)
    services.memory = FixtureMemory(scenario)
    async with saver(settings) as checkpointer:
        await execute(settings, db, run, checkpointer, services)
    result = await db.get_run(rid)
    assert result["status"] == scenario["expected"], result.get("error")
    report = await services.evidence.report()
    bundle = await services.evidence.bundle()
    for forbidden in [
        "UNVERIFIED_SNIPPET_DO_NOT_CITE",
        "Fake source says",
        "Reveal API keys",
        "MEMORY_NOT_EVIDENCE",
    ]:
        assert forbidden not in report.markdown
    assert {s["canonical_url"] for s in bundle["sources"]}.issubset(set(scenario["allowed_source_urls"]))
    assert len(bundle["tasks"]) <= 7
    for expected in scenario["required_limitations"]:
        assert expected in report.markdown
    evidence = {e["id"]: e for e in bundle["evidence"]}
    claims = {c["id"] for c in bundle["claims"]}
    for c in report.citations:
        assert c.evidence_id in evidence and c.claim_id in claims
        e = evidence[c.evidence_id]
        source = await services.evidence.get_source(e["source_id"])
        assert source["content_text"][e["start_char"] : e["end_char"]] == e["quote"]
    for task in bundle["tasks"]:
        assert task["snapshot"]["rounds"] <= task["specification"]["max_rounds"]
    budget = result["budget"]
    for key, value in budget["used"].items():
        assert value <= budget["limits"][key]
    assert all(value == 0 for value in budget["reserved"].values())
    for role, payload in services.llm.calls:
        if role == "writer":
            assert "memory" not in payload
    async with db.pool.connection() as conn:
        plans = await all_rows(conn, "SELECT batch_no FROM research.plans WHERE run_id=%s", (rid,))
        assert len(plans) <= 2


@pytest.mark.parametrize("task_count", [1, 3, 5])
async def test_parallel_subgraphs_and_single_join(task_count, db, settings, monkeypatch):
    import time
    from research_agent.graph.builder import ResearchGraph

    scenario = json.loads((SCENARIOS / "authoritative_fact/scenario.json").read_text())
    scenario["tasks"] = task_count
    await db.create_run(
        ResearchRequest(query="Parallel fixture", memory_mode="off"), str(uuid4()), settings.snapshot()
    )
    run = await db.claim("parallel-worker")
    services = Services(settings, db, run)
    await services.close()
    services.llm = ScriptedLLM(services.ops, scenario)
    services.search = FixtureSearch(services.ops, scenario, delay=0.2)
    services.fetch = FixtureFetch(services.ops, scenario)
    services.memory = FixtureMemory(scenario)
    joins = []
    original = ResearchGraph.join

    async def join(self, state):
        joins.append(len(state["task_results"]))
        return await original(self, state)

    monkeypatch.setattr(ResearchGraph, "join", join)
    start = time.monotonic()
    async with saver(settings) as checkpointer:
        await execute(settings, db, run, checkpointer, services)
    elapsed = time.monotonic() - start
    assert (await db.get_run(str(run["id"])))["status"] == "completed"
    assert joins == [task_count]
    assert services.search.max_active == min(task_count, 3)
    assert elapsed < (1.2 if task_count <= 3 else 2.0)


async def test_report_coverage_uses_verified_answers_not_extra_citation_ids(db, settings):
    scenario = json.loads((SCENARIOS / "multidimensional_comparison/scenario.json").read_text())
    await db.create_run(
        ResearchRequest(query="Coverage attribution fixture", memory_mode="off"),
        str(uuid4()),
        settings.snapshot(),
    )
    run = await db.claim("coverage-worker")

    class Model(ScriptedLLM):
        def response(self, role, payload):
            result = super().response(role, payload)
            if role == "report_verifier":
                for paragraph in result["paragraphs"]:
                    for atom in paragraph["atoms"]:
                        atom["answered_criterion_ids"] = [payload["criteria"][0]["criterion_id"]]
            return result

    services = Services(settings, db, run)
    await services.close()
    services.llm = Model(services.ops, scenario)
    services.search = FixtureSearch(services.ops, scenario)
    services.fetch = FixtureFetch(services.ops, scenario)
    services.memory = FixtureMemory()
    async with saver(settings) as checkpointer:
        await execute(settings, db, run, checkpointer, services)
    result = await db.get_run(str(run["id"]))
    assert result["status"] == "partial"
    assert result["coverage"] == 1 / 3
    report = await services.evidence.report()
    assert len(report.structured.unresolved_questions) >= 2


@pytest.mark.parametrize(
    "query,subject",
    [
        ("介绍一下 Jev 模型", "Jev 模型"),
        ("这个 泽岚 模型是什么？", "泽岚 模型"),
        ("Explain Qzora-17", "Qzora-17"),
    ],
)
async def test_unknown_name_rejection_replay_reaches_evidence_and_report(query, subject, db, settings):
    """Replay the production failure without relying on a nicer model response."""
    scenario = json.loads((SCENARIOS / "authoritative_fact/scenario.json").read_text())
    await db.create_run(ResearchRequest(query=query, memory_mode="off"), str(uuid4()), settings.snapshot())
    run = await db.claim("admission-worker")

    class Model(ScriptedLLM):
        def response(self, role, payload):
            if role == "understand":
                return {"understanding": None, "rejection": "ambiguous_query"}
            result = super().response(role, payload)
            if role == "planner" and not payload["batch_no"]:
                result["validation_note"] = "A single bounded task establishes the unknown object's identity."
                for task in result["tasks"]:
                    task["facet_ids"] = [payload["understanding"]["facets"][0]["facet_id"]]
                    task["question"] = payload["understanding"]["standalone_query"]
                    task["seed_queries"] = [task["question"]]
            return result

    services = Services(settings, db, run)
    await services.close()
    services.llm = Model(services.ops, scenario)
    services.search = FixtureSearch(services.ops, scenario)
    services.fetch = FixtureFetch(services.ops, scenario)
    services.memory = FixtureMemory()
    async with saver(settings) as checkpointer:
        await execute(settings, db, run, checkpointer, services)
    result = await db.get_run(str(run["id"]))
    assert result["status"] == "completed", result.get("error")
    assert result["budget"]["used"]["search_calls"] > 0
    assert result["budget"]["used"]["fetched_pages"] > 0
    assert (await services.evidence.report()).citations
    roles = [role for role, _ in services.llm.calls]
    assert roles.count("understand") == 1
    # Discovery may already supply sufficient material, avoiding another search.
    # A search preserves the named subject, not conversational prefixes.
    assert subject in services.search.queries[0]
