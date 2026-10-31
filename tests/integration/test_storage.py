import asyncio
from uuid import uuid4
import pytest
from research_agent.contracts import ResearchRequest
from research_agent.services.operations import Operations
from research_agent.errors import ResearchError, Stopped, Fenced
from research_agent.storage.db import one


async def new_run(db, settings, limits=None):
    request = ResearchRequest(query="offline storage contract test", memory_mode="off", limits=limits or {})
    await db.create_run(request, str(uuid4()), settings.snapshot())
    return await db.claim(str(uuid4()))


async def test_idempotency_owner_and_cancel(db, settings):
    key = str(uuid4())
    request = ResearchRequest(query="test", memory_mode="off")
    a = await db.create_run(request, key, settings.snapshot())
    b = await db.create_run(request, key, settings.snapshot())
    assert a["id"] == b["id"]
    with pytest.raises(ResearchError, match="idempotency_conflict"):
        await db.create_run(ResearchRequest(query="different"), key, settings.snapshot())
    old = db.owner
    db.owner = "unrelated-owner"
    with pytest.raises(ResearchError, match="not_found"):
        await db.get_run(str(a["id"]))
    db.owner = old
    cancelled = await db.cancel(str(a["id"]))
    assert cancelled["status"] == "cancelled"
    assert (await db.cancel(str(a["id"])))["status"] == "cancelled"


async def test_atomic_budget_reservation_and_replay(db, settings):
    run = await new_run(db, settings, {"search_calls": 1})
    rid = str(run["id"])
    ops = Operations(db, rid, run["fence"], run["config"])
    calls = []

    async def call(timeout):
        calls.append(1)
        await asyncio.sleep(0.02)
        return {"hits": []}

    results = await asyncio.gather(
        *(ops.run("search-" + str(i), "search", {"q": str(i)}, call) for i in range(3)),
        return_exceptions=True,
    )
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, Stopped) for r in results) == 2
    winner = next(i for i, r in enumerate(results) if isinstance(r, dict))
    await ops.run("search-" + str(winner), "search", {"q": str(winner)}, call)
    assert len(calls) == 1
    budget = (await db.get_run(rid))["budget"]
    assert budget["used"]["search_calls"] == 1
    assert budget["used"]["search_http_attempts"] == 1
    assert not any(budget["reserved"].values())
    await db.terminal(rid, run["fence"], "partial", "budget_exhausted")


async def test_expired_lease_fences_old_worker_and_charges_unknown(db, settings):
    run = await new_run(db, settings)
    rid = str(run["id"])
    ops = Operations(db, rid, run["fence"], run["config"])
    await ops.reserve(
        "uncertain",
        "llm",
        "input",
        {"llm_http_attempts": 1, "total_tokens": 500, "tokens_research": 500},
        None,
        "research",
    )
    async with db.pool.connection() as conn:
        await conn.execute(
            "UPDATE research.runs SET lease_until=now()-interval '1 second' WHERE id=%s", (rid,)
        )
    newer = await db.claim("new-worker")
    assert newer["fence"] == run["fence"] + 1
    with pytest.raises(Fenced):
        await ops.reserve("late", "llm", "input2", {"llm_http_attempts": 1}, None, "research")
    budget = (await db.get_run(rid))["budget"]
    assert budget["used"]["total_tokens"] == 500
    assert budget["reserved"]["total_tokens"] == 0
    async with db.pool.connection() as conn:
        attempt = await one(conn, "SELECT status FROM research.operation_attempts WHERE run_id=%s", (rid,))
        assert attempt["status"] == "unknown"
    await db.terminal(rid, newer["fence"], "failed", "test_done")


async def test_implicit_as_of_exactly_matches_creation(db, settings):
    from datetime import datetime

    row = await db.create_run(ResearchRequest(query="Timestamp contract"), str(uuid4()), settings.snapshot())
    assert datetime.fromisoformat(row["request"]["as_of"]) == row["created_at"]
    await db.cancel(str(row["id"]))
