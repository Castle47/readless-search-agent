import asyncio
import json
from uuid import uuid4
from research_agent.contracts import ResearchRequest
from research_agent.services.runtime import Services
from research_agent.storage.checkpoint import saver
from research_agent.storage.db import one
from research_agent.workers.research_worker import execute
from tests.fakes import ScriptedLLM, FixtureSearch, FixtureFetch, FixtureMemory
from tests.integration.test_graph import SCENARIOS


async def test_worker_cancellation_commits_before_watchdog_cleanup(db, settings):
    scenario = json.loads((SCENARIOS / "authoritative_fact/scenario.json").read_text())
    await db.create_run(
        ResearchRequest(query="Worker cancel fixture", memory_mode="off"), str(uuid4()), settings.snapshot()
    )
    run = await db.claim("worker-cancel")
    rid = str(run["id"])
    services = Services(settings, db, run)
    await services.close()
    services.llm = ScriptedLLM(services.ops, scenario)
    services.search = FixtureSearch(services.ops, scenario, delay=20)
    services.fetch = FixtureFetch(services.ops, scenario)
    services.memory = FixtureMemory()
    async with saver(settings) as checkpointer:
        work = asyncio.create_task(execute(settings, db, run, checkpointer, services))
        for _ in range(100):
            if services.search.active:
                break
            await asyncio.sleep(0.02)
        assert services.search.active
        await db.cancel(rid)
        await asyncio.wait_for(work, timeout=4)
    row = await db.get_run(rid)
    assert row["status"] == "cancelled" and row["report_id"] is None
    async with db.pool.connection() as conn:
        assert (
            await one(
                conn, "SELECT count(*) AS n FROM research.tasks WHERE run_id=%s AND status='active'", (rid,)
            )
        )["n"] == 0
        assert (await one(conn, "SELECT count(*) AS n FROM research.memory_outbox WHERE run_id=%s", (rid,)))[
            "n"
        ] == 0
