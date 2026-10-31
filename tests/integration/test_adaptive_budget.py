from uuid import uuid4

import pytest
from research_agent.config import Settings
from research_agent.contracts import ResearchRequest
from research_agent.errors import Stopped
from research_agent.services.operations import Operations, protected_tokens


async def test_shared_finalization_can_retry_large_unknown_request_but_cannot_exceed_total(db):
    config = Settings().snapshot()
    config["research"].update(
        total_tokens=160000, token_partitions={"research": 0, "finalization": 160000, "memory": 0}
    )
    await db.create_run(ResearchRequest(query="Budget fixture", memory_mode="off"), str(uuid4()), config)
    run = await db.claim("shared-budget", lease_seconds=90)
    rid = str(run["id"])
    ops = Operations(db, rid, run["fence"], config)
    before = (await db.get_run(rid))["budget"]
    assert before["limits"]["tokens_report_verifier"] == 160000
    assert (
        protected_tokens(config, before["limits"], {}, {}, "finalization", {"tokens_report_verifier": 38000})
        == 0
    )
    requested = {
        "total_tokens": 38000,
        "tokens_finalization": 38000,
        "tokens_report_verifier": 38000,
        "llm_http_attempts": 1,
    }
    first = await ops.reserve("large-review", "llm", "same-input", requested.copy(), None, "finalization")
    await ops.settle(first, error={"code": "network_error"}, unknown=True)
    retry = await ops.reserve("large-review", "llm", "same-input", requested.copy(), None, "finalization")
    await ops.settle(
        retry,
        result={"ok": True},
        actual={
            "total_tokens": 20000,
            "tokens_finalization": 20000,
            "tokens_report_verifier": 20000,
            "llm_http_attempts": 1,
        },
    )
    await ops.close_finalization_stage("writer")
    await ops.close_finalization_stage("report_verifier")
    current = (await db.get_run(rid))["budget"]
    assert current["used"]["total_tokens"] == 58000
    assert current["limits"]["tokens_finalization"] == 160000
    assert current["limits"]["tokens_repair_and_recheck"] == 160000
    edit = {"total_tokens": 90000, "tokens_finalization": 90000, "tokens_repair_and_recheck": 90000}
    attempt = await ops.reserve("edit", "llm", "edit-input", edit.copy(), None, "finalization")
    await ops.settle(attempt, result={"ok": True}, actual=edit)
    with pytest.raises(Stopped, match="budget_exhausted"):
        await ops.reserve(
            "over-total",
            "llm",
            "too-much",
            {"total_tokens": 13000, "tokens_finalization": 13000},
            None,
            "finalization",
        )
    assert (await db.get_run(rid))["budget"]["used"]["total_tokens"] == 148000
    await db.terminal(rid, run["fence"], "cancelled", "fixture_done")
