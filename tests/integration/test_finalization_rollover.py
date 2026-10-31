import asyncio
from uuid import uuid4

import pytest

from research_agent.contracts import ResearchRequest
from research_agent.errors import InvariantViolation, Stopped
from research_agent.services.operations import Operations, closed_stage_key
from research_agent.storage.db import one


async def fixture_ops(db, settings):
    await db.create_run(
        ResearchRequest(query="Budget rollover fixture", memory_mode="off"), str(uuid4()), settings.snapshot()
    )
    run = await db.claim("rollover-test")
    return Operations(db, str(run["id"]), run["fence"], run["config"])


async def spend(ops, key, role, actual, reserved=None, recheck=False):
    async def call(_timeout):
        return {"usage": {"total_tokens": actual}}

    return await ops.run(
        key,
        "llm",
        {"key": key},
        call,
        tokens=reserved or actual,
        partition="finalization",
        model_role=role,
        is_recheck=recheck,
    )


async def test_rollover_conserves_funds_protects_quality_and_stops_at_total(db, settings):
    ops = await fixture_ops(db, settings)
    await spend(ops, "write", "writer", 9000, 16000)
    result = await ops.close_finalization_stage("writer")
    assert result["tokens"] == 7000 and result["recipient_limit"] == 23000
    await spend(ops, "audit", "report_verifier", 20000)
    result = await ops.close_finalization_stage("report_verifier")
    assert result["tokens"] == 3000 and result["recipient_limit"] == 13000
    await spend(ops, "repair", "repair", 12000)
    # 6000 is still exclusively available for the whole-answer review.
    budget = (await db.get_run(ops.run_id))["budget"]
    assert budget["limits"]["tokens_quality"] == 6000
    await spend(ops, "quality", "quality", 2000, 6000)
    result = await ops.close_finalization_stage("quality")
    assert result["tokens"] == 4000 and result["recipient_limit"] == 17000
    await spend(ops, "recheck", "report_verifier", 5000, recheck=True)
    with pytest.raises(Stopped, match="budget_exhausted"):
        await spend(ops, "one-more", "repair", 1)
    budget = (await db.get_run(ops.run_id))["budget"]
    assert budget["used"]["tokens_finalization"] == budget["used"]["total_tokens"] == 48000
    assert (
        sum(
            budget["limits"][k]
            for k in [
                "tokens_writer",
                "tokens_report_verifier",
                "tokens_repair_and_recheck",
                "tokens_quality",
            ]
        )
        == 48000
    )
    assert budget["limits"]["total_tokens"] == 300000


async def test_quality_reservation_cannot_be_spent_by_other_or_unclassified_final_calls(db, settings):
    ops = await fixture_ops(db, settings)
    await ops.close_finalization_stage("writer")
    await ops.close_finalization_stage("report_verifier")
    await spend(ops, "pre-quality", "repair", 42000)
    with pytest.raises(Stopped, match="budget_exhausted"):
        await spend(ops, "unclassified", None, 1)
    await spend(ops, "review", "quality", 6000)
    assert (await db.get_run(ops.run_id))["budget"]["used"]["tokens_finalization"] == 48000


async def test_closure_is_atomic_idempotent_under_concurrency_and_recovery(db, settings):
    ops = await fixture_ops(db, settings)
    await spend(ops, "writer", "writer", 4000)
    recovered = Operations(db, ops.run_id, ops.fence, ops.config)
    results = await asyncio.gather(
        ops.close_finalization_stage("writer"), recovered.close_finalization_stage("writer")
    )
    assert results[0] == results[1] and results[0]["tokens"] == 12000
    assert await recovered.close_finalization_stage("writer") == results[0]
    async with db.pool.connection() as conn:
        row = await one(
            conn,
            "SELECT count(*) AS n FROM research.events WHERE run_id=%s AND event_type='budget.finalization_transferred'",
            (ops.run_id,),
        )
    assert row["n"] == 1
    # Previously saved output remains replayable, but a new attempt is forbidden
    # after the stage has given away its unused allocation.
    assert (await spend(recovered, "writer", "writer", 4000))["usage"]["total_tokens"] == 4000
    with pytest.raises(Stopped, match="budget_exhausted"):
        await spend(recovered, "new-writer", "writer", 1)
    assert (await db.get_run(ops.run_id))["budget"]["used"]["total_tokens"] == 4000


async def test_inflight_and_unknown_spend_cannot_be_donated(db, settings):
    ops = await fixture_ops(db, settings)
    request = {"total_tokens": 4000, "tokens_finalization": 4000, "tokens_writer": 4000}
    attempt = await ops.reserve("unknown-write", "llm", "hash", request, None, "finalization")
    before = (await db.get_run(ops.run_id))["budget"]
    with pytest.raises(InvariantViolation, match="unsettled"):
        await ops.close_finalization_stage("writer")
    assert (await db.get_run(ops.run_id))["budget"] == before
    assert await db.saved_value(ops.run_id, closed_stage_key("writer")) is None
    await ops.settle(attempt, error={"code": "timeout"}, unknown=True)
    assert (await ops.close_finalization_stage("writer"))["tokens"] == 12000
    assert (await db.get_run(ops.run_id))["budget"]["used"]["total_tokens"] == 4000


async def test_failed_transfer_rolls_back_marker_and_limits(db, settings, monkeypatch):
    ops = await fixture_ops(db, settings)
    before = (await db.get_run(ops.run_id))["budget"]

    async def crash(*_args, **_kwargs):
        raise RuntimeError("crash during transfer transaction")

    with monkeypatch.context() as m:
        m.setattr(db, "event", crash)
        with pytest.raises(RuntimeError, match="crash"):
            await ops.close_finalization_stage("writer")
    assert (await db.get_run(ops.run_id))["budget"] == before
    assert await db.saved_value(ops.run_id, closed_stage_key("writer")) is None
    assert (await ops.close_finalization_stage("writer"))["tokens"] == 16000


async def test_stage_order_and_old_configs_do_not_silently_gain_rollover(db, settings):
    ops = await fixture_ops(db, settings)
    with pytest.raises(InvariantViolation, match="order"):
        await ops.close_finalization_stage("report_verifier")
    with pytest.raises(InvariantViolation, match="order"):
        await ops.close_finalization_stage("quality")
    legacy = Operations(
        db,
        ops.run_id,
        ops.fence,
        {k: v for k, v in ops.config.items() if k != "finalization_rollover_version"},
    )
    assert await legacy.close_finalization_stage("writer") is None
    assert await db.saved_value(ops.run_id, closed_stage_key("writer")) is None
