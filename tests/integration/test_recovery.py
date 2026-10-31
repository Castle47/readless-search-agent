import json
from uuid import uuid4
import pytest
from research_agent.contracts import ResearchRequest
from research_agent.services.runtime import Services
from research_agent.storage.db import one
from research_agent.storage.checkpoint import saver
from research_agent.workers.research_worker import execute
from tests.fakes import ScriptedLLM, FixtureSearch, FixtureFetch, FixtureMemory
from tests.integration.test_graph import SCENARIOS


class Crash(BaseException):
    pass


async def fixture_services(db, settings, run, scenario):
    s = Services(settings, db, run)
    await s.close()
    s.llm = ScriptedLLM(s.ops, scenario)
    s.search = FixtureSearch(s.ops, scenario)
    s.fetch = FixtureFetch(s.ops, scenario)
    s.memory = FixtureMemory()
    return s


@pytest.mark.parametrize("boundary", ["source", "evidence", "round", "task", "finalize"])
async def test_crash_recovery_at_committed_boundaries(boundary, db, settings):
    scenario = json.loads((SCENARIOS / "authoritative_fact/scenario.json").read_text())
    await db.create_run(
        ResearchRequest(query="Recovery fixture", memory_mode="off"), str(uuid4()), settings.snapshot()
    )
    run = await db.claim("first")
    rid = str(run["id"])
    services = await fixture_services(db, settings, run, scenario)
    target = services.evidence if boundary in {"source", "evidence", "finalize"} else services.db
    method = {
        "source": "source",
        "evidence": "persist",
        "round": "commit_value",
        "task": "commit_value",
        "finalize": "finalize",
    }[boundary]
    original = getattr(target, method)
    fired = False

    async def crash(*args, **kwargs):
        nonlocal fired
        value = await original(*args, **kwargs)
        should = boundary in {"source", "evidence", "finalize"} or args[3] == boundary
        if should and not fired:
            fired = True
            raise Crash()
        return value

    setattr(target, method, crash)
    async with saver(settings) as checkpointer:
        with pytest.raises(Crash):
            await execute(settings, db, run, checkpointer, services)
    setattr(target, method, original)
    assert fired
    before = await db.get_run(rid)
    if boundary == "finalize":
        assert before["status"] == "completed"
        assert await db.claim("second") is None
    else:
        async with db.pool.connection() as conn:
            await conn.execute(
                "UPDATE research.runs SET lease_until=now()-interval '1 second' WHERE id=%s", (rid,)
            )
        resumed = await db.claim("second")
        next_services = await fixture_services(db, settings, resumed, scenario)
        async with saver(settings) as checkpointer:
            await execute(settings, db, resumed, checkpointer, next_services)
        assert (await db.get_run(rid))["status"] == "completed"
        assert next_services.fetch.calls == 0, "saved fetch must be replayed without network"
    async with db.pool.connection() as conn:
        for table, expected in [("sources", 1), ("claims", 1), ("evidence", 1), ("reports", 1)]:
            result = await one(
                conn, "SELECT count(*) AS n FROM research." + table + " WHERE run_id=%s", (rid,)
            )
            assert result["n"] == expected
