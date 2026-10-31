import asyncio
import json
from uuid import uuid4
from research_agent.contracts import ResearchRequest, SearchHit, stable_id
from research_agent.services.runtime import Services
from research_agent.storage.db import all_rows
from research_agent.storage.checkpoint import saver
from research_agent.workers.research_worker import execute
from tests.fakes import ScriptedLLM, FixtureFetch, FixtureMemory
from tests.integration.test_graph import SCENARIOS


async def test_fast_task_commits_while_slow_subgraph_continues(db, settings):
    scenario = json.loads((SCENARIOS / "two_origins/scenario.json").read_text())
    scenario["tasks"] = 3
    await db.create_run(
        ResearchRequest(query="Fast and slow fixture", memory_mode="off"), str(uuid4()), settings.snapshot()
    )
    run = await db.claim("fast-slow")
    rid = str(run["id"])

    class Model(ScriptedLLM):
        def response(self, role, payload):
            result = super().response(role, payload)
            if role == "planner" and not payload["batch_no"]:
                for i, t in enumerate(result["tasks"]):
                    t["success_criteria"][0]["kind"] = "corroborated" if i == 1 else "authoritative_fact"
            return result

    class Search:
        def __init__(self, ops):
            self.ops = ops
            self.rounds = {}
            self.active = 0
            self.peak = 0

        async def search(self, query, language, task_id=None):
            async def call(timeout):
                self.rounds[task_id] = self.rounds.get(task_id, 0) + 1
                self.active += 1
                self.peak = max(self.peak, self.active)
                try:
                    await asyncio.sleep(0.2)
                finally:
                    self.active -= 1
                index = 1 if self.rounds[task_id] >= 3 else 0
                page = scenario["pages"][index]
                return {
                    "hits": [
                        {
                            "url": page["url"],
                            "title": "Fixture",
                            "snippet": "not evidence",
                            "rank": 1,
                            "published_at": None,
                        }
                    ]
                }

            value = await self.ops.run(
                "search:" + stable_id("query", query, language),
                "search",
                {"query": query},
                call,
                task_id=task_id,
            )
            return [SearchHit.model_validate(h) for h in value["hits"]]

    services = Services(settings, db, run)
    await services.close()
    services.llm = Model(services.ops, scenario)
    services.search = Search(services.ops)
    services.fetch = FixtureFetch(services.ops, scenario)
    services.memory = FixtureMemory()
    observed = False
    async with saver(settings) as checkpointer:
        work = asyncio.create_task(execute(settings, db, run, checkpointer, services))
        while not work.done():
            async with db.pool.connection() as conn:
                tasks = await all_rows(conn, "SELECT status FROM research.tasks WHERE run_id=%s", (rid,))
            statuses = [t["status"] for t in tasks]
            observed = observed or ("sufficient" in statuses and "active" in statuses)
            await asyncio.sleep(0.02)
        await work
    assert observed
    assert max(services.search.rounds.values()) == 3
    assert services.search.peak <= 3
    assert (await db.get_run(rid))["status"] == "completed"
