from uuid import uuid4
import pytest
from research_agent.contracts import ResearchRequest
from research_agent.services.operations import Operations
from research_agent.config import Settings
from research_agent.errors import Stopped
from research_agent.storage.db import all_rows


async def test_batch_preserves_finalization_and_memory_before_another_research_call(db, settings):
    config = Settings().snapshot()
    protected = sum(config["research"]["token_partitions"][k] for k in ["finalization", "memory"])
    config["validation_batch"] = {
        "id": "test-015e-" + str(uuid4()),
        "limit": protected + 83040,
        "prior_tokens": 0,
    }
    await db.create_run(ResearchRequest(query="Bounded fixture", memory_mode="off"), str(uuid4()), config)
    run = await db.claim("budget-fixture")
    ops = Operations(db, str(run["id"]), run["fence"], config)

    async def spent(timeout):
        return {"usage": {"total_tokens": 80000}}

    await ops.run("research", "llm", {}, spent, tokens=80000)
    assert await ops.research_headroom() == 3040
    with pytest.raises(Stopped):
        await ops.run("research-too-large", "llm", {}, spent, tokens=4000)

    async def write(timeout):
        return {"usage": {"total_tokens": 15000}}

    await ops.run("write", "llm", {}, write, tokens=16000, partition="finalization", model_role="writer")
    async with db.pool.connection() as c:
        events = await all_rows(
            c,
            "SELECT payload FROM research.events WHERE run_id=%s AND event_type='budget.denied'",
            (ops.run_id,),
        )
    assert events[-1]["payload"]["denied_pools"] == [
        {
            "scope": "validation_batch",
            "available": protected + 3040,
            "requested": 4000,
            "protected": protected,
        }
    ]
    assert (await db.get_run(ops.run_id))["budget"]["limits"]["total_tokens"] == 400000
    await db.terminal(ops.run_id, ops.fence, "cancelled", "fixture_done")


async def test_frozen_stage_is_cumulative_and_keeps_quality_allocation(db, settings):
    config = Settings().snapshot()
    # The historical 015E workflow retains its separate quality allocation.
    # Shared adaptive pools have their own retry/total-cap regression.
    config["shared_finalization_budget"] = False
    config["validation_stage"] = {"id": "test-stage-" + str(uuid4()), "limit": 96000, "prior_tokens": 0}
    await db.create_run(ResearchRequest(query="Frozen fixture", memory_mode="off"), str(uuid4()), config)
    run = await db.claim("stage-fixture")
    ops = Operations(db, str(run["id"]), run["fence"], config)
    await ops.close_finalization_stage("writer")

    async def audit(timeout):
        return {"usage": {"total_tokens": 50000}}

    await ops.run(
        "audit", "llm", {}, audit, tokens=50000, partition="finalization", model_role="report_verifier"
    )
    await ops.close_finalization_stage("report_verifier")
    quality_cap = config["model"]["finalization_split"]["quality"]
    repair_tokens = 96000 - 50000 - quality_cap

    async def repair(timeout):
        return {"usage": {"total_tokens": repair_tokens}}

    await ops.run(
        "repair", "llm", {}, repair, tokens=repair_tokens, partition="finalization", model_role="editor"
    )
    with pytest.raises(Stopped):
        await ops.run(
            "would-eat-quality", "llm", {}, repair, tokens=1, partition="finalization", model_role="editor"
        )

    async def quality(timeout):
        return {"usage": {"total_tokens": 5000}}

    await ops.run(
        "quality", "llm", {}, quality, tokens=quality_cap, partition="finalization", model_role="quality"
    )
    await db.terminal(ops.run_id, ops.fence, "cancelled", "fixture_done")
