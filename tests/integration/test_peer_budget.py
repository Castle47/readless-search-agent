"""Real PostgreSQL transactions; run only with the existing test database available."""

from uuid import uuid4

import pytest

from research_agent.config import Settings
from research_agent.contracts import ResearchRequest
from research_agent.errors import Stopped
from research_agent.services.operations import Operations


@pytest.mark.asyncio
@pytest.mark.parametrize("tokens,attempts", [(700, 16), (48000, 1)])
async def test_peer_cap_is_atomic_replayable_and_does_not_block_other_roles(db, tokens, attempts):
    config = Settings().snapshot()
    config["peer_review"].update(token_cap=tokens, max_http_attempts=attempts)
    await db.create_run(ResearchRequest(query="Peer budget fixture", memory_mode="off"), str(uuid4()), config)
    run = await db.claim("peer-budget-test")
    ops = Operations(db, str(run["id"]), run["fence"], config)
    calls = []

    async def respond(timeout):
        calls.append(True)
        return {"usage": {"total_tokens": 300}}

    first = await ops.run("peer-1", "llm", {}, respond, tokens=500, model_role="peer_questioner")
    assert await ops.run("peer-1", "llm", {}, respond, tokens=500, model_role="peer_questioner") == first
    assert len(calls) == 1
    with pytest.raises(Stopped, match="budget_exhausted"):
        await ops.run("peer-2", "llm", {}, respond, tokens=500, model_role="peer_responder")
    assert len(calls) == 1
    await ops.run("ordinary-research", "llm", {}, respond, tokens=500, model_role="reason")
    stored = await db.get_run(ops.run_id)
    assert stored["budget"]["used"]["tokens_peer_review"] == 300
    assert stored["budget"]["used"]["peer_review_http_attempts"] == 1
    assert stored["budget"]["used"]["tokens_research"] == 600
    await db.terminal(ops.run_id, ops.fence, "cancelled", "fixture_done")


@pytest.mark.asyncio
async def test_peer_message_event_is_emitted_once_on_replay(db):
    config = Settings().snapshot()
    await db.create_run(ResearchRequest(query="Peer event fixture", memory_mode="off"), str(uuid4()), config)
    run = await db.claim("peer-event-test")
    value = {
        "question_id": "q",
        "sender_task_id": "a",
        "question": {"target_task_id": "b"},
        "reply": {"status": "unresolved"},
        "evidence_locations": {},
    }
    for _ in range(2):
        await db.commit_value(str(run["id"]), run["fence"], "peer-message", "peer_message", value)
    events = await db.events(str(run["id"]), 0, 100)
    assert sum(event["event_type"] == "researcher.exchange" for event in events) == 1
    await db.terminal(str(run["id"]), run["fence"], "cancelled", "fixture_done")
