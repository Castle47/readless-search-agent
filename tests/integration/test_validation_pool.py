import asyncio
from uuid import uuid4
import pytest
from research_agent.contracts import ResearchRequest
from research_agent.storage.db import Database
from research_agent.services.operations import Operations
from research_agent.errors import Stopped


async def test_shared_validation_reservation_is_atomic_across_runs(settings):
    pool = {"id": "test-pool-" + str(uuid4()), "limit": 1000, "prior_tokens": 100}
    databases = []
    runs = []
    operations = []
    for _ in range(2):
        db = Database(settings.database_url.get_secret_value(), owner="test-" + str(uuid4()))
        await db.open()
        databases.append(db)
        config = settings.snapshot()
        config["validation_pool"] = pool
        await db.create_run(
            ResearchRequest(query="Validation cohort fixture", memory_mode="off"), str(uuid4()), config
        )
        run = await db.claim("pool-worker")
        runs.append(run)
        operations.append(Operations(db, str(run["id"]), run["fence"], config))
    hold = asyncio.Event()
    started = asyncio.Event()
    calls = []

    async def call(timeout):
        calls.append(1)
        started.set()
        await hold.wait()
        return {"usage": {"total_tokens": 550}}

    tasks = [asyncio.create_task(ops.run("shared", "llm", {}, call, tokens=600)) for ops in operations]
    await started.wait()
    done, _ = await asyncio.wait(tasks, timeout=2, return_when=asyncio.FIRST_COMPLETED)
    assert done and len(calls) == 1
    hold.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert sum(isinstance(r, Stopped) for r in results) == 1

    async def small(timeout):
        return {"usage": {"total_tokens": 350}}

    await operations[0].run("remaining", "llm", {}, small, tokens=350)
    with pytest.raises(Stopped, match="budget_exhausted"):
        await operations[1].run("over", "llm", {}, small, tokens=1)
    for db, run in zip(databases, runs):
        await db.terminal(str(run["id"]), run["fence"], "partial", "validation_only")
        await db.close()
