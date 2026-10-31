from research_agent.storage.db import one
import asyncio
from uuid import uuid4
import pytest
from research_agent.contracts import ResearchRequest
from research_agent.services.operations import Operations
from research_agent.errors import Stopped, ToolFailure


async def test_cancel_interrupts_inflight_without_new_requests(db, settings):
    await db.create_run(
        ResearchRequest(query="Cancellation fixture", memory_mode="off"), str(uuid4()), settings.snapshot()
    )
    run = await db.claim("cancel-worker")
    rid = str(run["id"])
    ops = Operations(db, rid, run["fence"], run["config"])
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow(timeout):
        started.set()
        try:
            await asyncio.sleep(20)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(ops.run("slow", "search", {}, slow))
    await started.wait()
    await db.cancel(rid)
    with pytest.raises(Stopped, match="cancelled"):
        await asyncio.wait_for(task, timeout=3)
    assert cancelled.is_set()
    with pytest.raises(Stopped, match="cancelled"):
        await ops.run("never", "search", {}, slow)
    assert (await db.get_run(rid))["budget"]["used"]["search_calls"] == 1
    await db.terminal(rid, run["fence"], "cancelled", "cancelled")


async def test_deadline_reserves_finalization_window(db, settings):
    await db.create_run(
        ResearchRequest(query="Deadline fixture", memory_mode="off"), str(uuid4()), settings.snapshot()
    )
    run = await db.claim("deadline-worker")
    rid = str(run["id"])
    ops = Operations(db, rid, run["fence"], run["config"])
    async with db.pool.connection() as conn:
        await conn.execute(
            "UPDATE research.runs SET deadline_at=now()+interval '80 seconds' WHERE id=%s", (rid,)
        )
    with pytest.raises(Stopped, match="deadline"):
        await ops.guard("research")
    assert 0 < await ops.guard("finalization") <= 80
    await db.terminal(rid, run["fence"], "partial", "deadline")


async def test_provider_unknown_usage_is_charged_and_auth_not_retried(db, settings):
    await db.create_run(
        ResearchRequest(query="Provider fixture", memory_mode="off"), str(uuid4()), settings.snapshot()
    )
    run = await db.claim("provider-worker")
    rid = str(run["id"])
    ops = Operations(db, rid, run["fence"], run["config"])
    calls = []

    async def auth_fail(timeout):
        calls.append(1)
        raise ToolFailure("http_401", fatal=True)

    with pytest.raises(ToolFailure):
        await ops.run("auth", "llm", {}, auth_fail, tokens=500)
    assert len(calls) == 1
    assert (await db.get_run(rid))["budget"]["used"]["total_tokens"] == 0

    async def unknown(timeout):
        raise ToolFailure("timeout")

    with pytest.raises(ToolFailure):
        await ops.run("unknown", "llm", {}, unknown, tokens=500)
    assert (await db.get_run(rid))["budget"]["used"]["total_tokens"] == 500
    await db.terminal(rid, run["fence"], "failed", "http_401")


async def test_three_parallel_fetches_can_make_nested_robots_requests(db, settings):
    await db.create_run(
        ResearchRequest(query="Nested robots fixture", memory_mode="off"), str(uuid4()), settings.snapshot()
    )
    run = await db.claim("nested-worker")
    rid = str(run["id"])
    ops = Operations(db, rid, run["fence"], run["config"])

    async def fetch(index):
        async def page(timeout):
            async def robots(t):
                await asyncio.sleep(0.01)
                return {"robots": "allowed"}

            return await ops.run("robots-" + str(index), "fetch_aux", {}, robots)

        return await ops.run("fetch-" + str(index), "fetch", {}, page)

    results = await asyncio.wait_for(asyncio.gather(*(fetch(i) for i in range(3))), timeout=3)
    assert all(r["robots"] == "allowed" for r in results)
    await db.terminal(rid, run["fence"], "partial", "test_done")


async def test_provider_server_error_does_not_release_uncertain_tokens(db, settings):
    await db.create_run(
        ResearchRequest(query="Uncertain server failure", memory_mode="off"),
        str(uuid4()),
        settings.snapshot(),
    )
    run = await db.claim("server-error")
    rid = str(run["id"])
    ops = Operations(db, rid, run["fence"], run["config"])

    async def failed(timeout):
        raise ToolFailure("http_500")

    with pytest.raises(ToolFailure):
        await ops.run("server-error", "llm", {}, failed, tokens=500)
    assert (await db.get_run(rid))["budget"]["used"]["total_tokens"] == 500
    async with db.pool.connection() as conn:
        row = await one(conn, "SELECT status FROM research.operation_attempts WHERE run_id=%s", (rid,))
        assert row["status"] == "unknown"
    await db.terminal(rid, run["fence"], "failed", "test_done")


async def test_verifier_repairs_and_retries_share_database_subcaps(db, settings):
    await db.create_run(
        ResearchRequest(query="Verifier subcap fixture", memory_mode="off"), str(uuid4()), settings.snapshot()
    )
    run = await db.claim("verifier-subcap")
    rid = str(run["id"])
    ops = Operations(db, rid, run["fence"], run["config"])

    async def call(timeout):
        return {"usage": {"total_tokens": 8000}}

    await ops.run(
        "verify-one", "llm", {}, call, tokens=8000, partition="finalization", model_role="report_verifier"
    )
    await ops.run(
        "verify-correction",
        "llm",
        {},
        call,
        tokens=8000,
        partition="finalization",
        model_role="report_verifier",
    )
    with pytest.raises(Stopped, match="budget_exhausted"):
        await ops.run(
            "verify-extra", "llm", {}, call, tokens=1, partition="finalization", model_role="report_verifier"
        )

    async def repair(timeout):
        return {"usage": {"total_tokens": 2000}}

    # Rechecks use the approved repair-and-recheck allocation, even after the
    # independent first-audit allocation is exhausted. Total accounting is once.
    await ops.run(
        "recheck",
        "llm",
        {},
        call,
        tokens=8000,
        partition="finalization",
        model_role="report_verifier",
        is_recheck=True,
    )
    await ops.run("repair", "llm", {}, repair, tokens=2000, partition="finalization", model_role="repair")
    with pytest.raises(Stopped, match="budget_exhausted"):
        await ops.run(
            "repair-extra", "llm", {}, repair, tokens=1, partition="finalization", model_role="repair"
        )
    budget = (await db.get_run(rid))["budget"]
    assert budget["used"]["tokens_report_verifier"] == 16000
    assert budget["used"]["tokens_repair_and_recheck"] == 10000
    assert budget["used"]["tokens_finalization"] == 26000
    await db.terminal(rid, run["fence"], "partial", "test_done")


async def test_proposal_and_batch_guards_are_independent_of_production_partitions(db, settings):
    from research_agent.services.operations import Operations
    from research_agent.contracts import ResearchRequest
    from uuid import uuid4

    c = settings.snapshot()
    c["validation_pool"] = {"id": "test-global-" + str(uuid4()), "limit": 100000, "prior_tokens": 0}
    c["validation_proposal"] = {"id": "test-proposal-" + str(uuid4()), "limit": 10000, "prior_tokens": 0}
    c["validation_batch"] = {"id": "test-batch-" + str(uuid4()), "limit": 150, "prior_tokens": 0}
    await db.create_run(ResearchRequest(query="Guard test", memory_mode="off"), str(uuid4()), c)
    r = await db.claim("test")
    ops = Operations(db, str(r["id"]), r["fence"], c)

    async def use(timeout):
        return {"usage": {"total_tokens": 100}}

    await ops.run("one", "llm", {}, use, tokens=100)
    with pytest.raises(Stopped, match="budget_exhausted"):
        await ops.run("two", "llm", {}, use, tokens=100)
    b = (await db.get_run(str(r["id"])))["budget"]
    assert b["limits"]["total_tokens"] == 300000 and b["used"]["total_tokens"] == 100
    await db.terminal(str(r["id"]), r["fence"], "completed", "test_done")
