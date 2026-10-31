import json
from copy import deepcopy
from uuid import uuid4
from research_agent.contracts import ResearchRequest
from research_agent.services.runtime import Services
from research_agent.storage.checkpoint import saver
from research_agent.workers.research_worker import execute
from tests.fakes import ScriptedLLM, FixtureSearch, FixtureFetch, FixtureMemory
from tests.integration.test_graph import SCENARIOS


async def test_observed_publisher_link_is_read_before_reference_summary(db, settings):
    scenario = json.loads((SCENARIOS / "authoritative_fact/scenario.json").read_text())
    official = scenario["pages"][0]
    reference = deepcopy(official)
    reference.update(
        url="https://reference.example/topic",
        text="A secondary reference page. Please use the linked original publisher for its specification.",
        quote="A secondary reference page.",
        claim="A secondary reference page.",
        metadata={
            "reference_links": [
                {
                    "url": official["url"],
                    "label": "Official website",
                    "official_link": True,
                    "documentation": False,
                }
            ]
        },
    )
    scenario["pages"] = [reference, official]

    class Model(ScriptedLLM):
        def response(self, role, payload):
            if role == "extract":
                assert payload["url"] == official["url"], (
                    "The reference summary must not replace the original publisher"
                )
            result = super().response(role, payload)
            if role == "understand":
                result["understanding"]["needs_discovery"] = True
            return result

    await db.create_run(
        ResearchRequest(query="Explain the linked specification.", memory_mode="off"),
        str(uuid4()),
        settings.snapshot(),
    )
    run = await db.claim("discovery-test")
    s = Services(settings, db, run)
    await s.close()
    s.llm = Model(s.ops, scenario)
    s.search = FixtureSearch(s.ops, scenario)
    s.fetch = FixtureFetch(s.ops, scenario)
    s.memory = FixtureMemory()
    async with saver(settings) as cp:
        await execute(settings, db, run, cp, s)
    assert (await db.get_run(str(run["id"])))["status"] == "completed"
    assert s.search.calls == 1 and s.fetch.calls == 2
    report = await s.evidence.report()
    bundle = await s.evidence.bundle()
    cited = {c.evidence_id for c in report.citations}
    sids = {e["source_id"] for e in bundle["evidence"] if e["id"] in cited}
    assert {s["canonical_url"] for s in bundle["sources"] if s["id"] in sids} == {official["url"]}


async def test_planner_can_select_an_observed_publisher_url_without_an_official_label(db, settings):
    scenario = json.loads((SCENARIOS / "authoritative_fact/scenario.json").read_text())
    official = scenario["pages"][0]
    reference = deepcopy(official)
    reference.update(
        url="https://guide.example/cedar",
        text="Independent notes about Cedar.",
        quote="Independent notes about Cedar.",
        claim="Independent notes about Cedar.",
        metadata={
            "reference_links": [
                {
                    "url": official["url"],
                    "label": "Acme Science",
                    "official_link": False,
                    "documentation": False,
                }
            ]
        },
    )
    scenario["pages"] = [reference, official]

    class Model(ScriptedLLM):
        def response(self, role, payload):
            if role == "extract":
                assert payload["url"] == official["url"]
            result = super().response(role, payload)
            if role == "understand":
                result["understanding"]["needs_discovery"] = True
            if role == "planner":
                result["tasks"][0]["source_urls"] = [official["url"]]
            return result

    await db.create_run(
        ResearchRequest(query="Explain Cedar.", memory_mode="off"), str(uuid4()), settings.snapshot()
    )
    run = await db.claim("publisher-url-test")
    s = Services(settings, db, run)
    await s.close()
    s.llm = Model(s.ops, scenario)
    s.search = FixtureSearch(s.ops, scenario)
    s.fetch = FixtureFetch(s.ops, scenario)
    s.memory = FixtureMemory()
    async with saver(settings) as cp:
        await execute(settings, db, run, cp, s)
    assert (await db.get_run(str(run["id"])))["status"] == "completed"
    assert s.search.calls == 1 and s.fetch.calls == 2


async def test_new_task_reuses_another_tasks_verified_source_without_copying_coverage(db, settings):
    from tests.integration.test_narrative_graph import ConnectedModel, run_fixture
    from research_agent.graph.researcher import Researcher

    s, run, _ = await run_fixture(db, settings, ConnectedModel)
    original = await s.evidence.bundle()
    task = deepcopy(original["tasks"][0]["specification"])
    task["task_id"] = str(uuid4())
    for criterion in task["success_criteria"]:
        criterion["criterion_id"] = str(uuid4())
    selected = await Researcher(s).select_read({"task": task, "visited_source_ids": [], "discovery": {}})
    assert selected["pending_source_ids"] == [original["sources"][0]["id"]]
    assert selected["direct_read"] is False
    assert await s.evidence.bundle() == original
    assert not any(m["task_id"] == task["task_id"] for m in original["mappings"])
