from uuid import uuid4
import pytest
from research_agent.contracts import ResearchRequest, stable_id
from research_agent.errors import ToolFailure
from research_agent.services.runtime import Services
from research_agent.services.retrieval import eligible_candidates, select_sources
from tests.fakes import ScriptedLLM


async def start(db, settings):
    await db.create_run(
        ResearchRequest(query="介绍一下原生的 transformer 模型", memory_mode="off"),
        str(uuid4()),
        settings.snapshot(),
    )
    run = await db.claim("retrieval-fixture")
    s = Services(settings, db, run)
    await s.close()
    s.understanding = {"reader_goal": "Explain the original neural architecture"}
    return s


async def test_selection_skips_failed_url_and_pdf_before_spending_candidate_slots(db, settings):
    s = await start(db, settings)
    bad = "https://bad.example/article"

    async def fail(timeout):
        raise ToolFailure("robots_denied", retryable=False)

    with pytest.raises(ToolFailure):
        await s.ops.run("fetch:" + stable_id("url", bad), "fetch", {"url": bad}, fail)
    hits = [
        {"url": u, "title": "Attention Is All You Need", "snippet": "", "rank": i + 1}
        for i, u in enumerate(
            [
                bad,
                "https://arxiv.org/pdf/1706.03762",
                "https://arxiv.org/abs/1706.03762",
                "https://arxiv.org/html/1706.03762v7",
            ]
        )
    ]
    kept, rejected = await eligible_candidates(s, hits)
    assert [h["url"] for h in kept] == ["https://arxiv.org/html/1706.03762v7"]
    assert {r["reason"].split(":")[0] for r in rejected} == {
        "cached_nonretryable_failure",
        "unsupported_pdf_use_observed_html",
        "observed_full_text_preferred",
    }
    await db.terminal(s.run_id, s.fence, "cancelled", "fixture_done")


async def test_selector_replay_does_not_issue_another_paid_decision_after_source_state_changes(
    db, settings, monkeypatch
):
    s = await start(db, settings)
    s.llm = ScriptedLLM(s.ops, {})
    hits = [
        {
            "url": "https://arxiv.org/html/1706.03762v7",
            "title": "Original paper",
            "snippet": "Self attention architecture",
            "rank": 1,
        }
    ]
    first = await select_sources(s, hits, s.request.query, query="Transformer architecture")

    async def never(*args):
        raise AssertionError("Replay must use the saved decision before mutable filtering")

    monkeypatch.setattr("research_agent.services.retrieval.eligible_candidates", never)
    assert await select_sources(s, hits, s.request.query, query="Transformer architecture") == first
    assert len(s.llm.calls) == 1
    await db.terminal(s.run_id, s.fence, "cancelled", "fixture_done")


async def test_empty_selection_never_falls_back_to_search_rank(db, settings):
    s = await start(db, settings)

    class NoUsefulSource(ScriptedLLM):
        def response(self, role, p):
            return {
                "decisions": [
                    {"candidate_id": c["candidate_id"], "read": False, "reason": "Unrelated music video"}
                    for c in p["candidates"]
                ]
            }

    s.llm = NoUsefulSource(s.ops, {})
    hits = [
        {
            "url": "https://youtube.com/watch?v=unrelated",
            "title": "Attention official music video",
            "snippet": "Music",
            "rank": 1,
        }
    ]
    assert await select_sources(s, hits, s.request.query) == []
    assert (await db.get_run(s.run_id))["budget"]["used"].get("fetched_pages", 0) == 0
    await db.terminal(s.run_id, s.fence, "cancelled", "fixture_done")


async def test_initial_search_executes_the_existing_plan_without_rewriting_it(db, settings):
    from types import SimpleNamespace
    from research_agent.graph.source_researcher import SourceResearcher

    s = await start(db, settings)
    s.understanding["search_languages"] = ["zh-CN", "en"]
    reader = SourceResearcher(s)
    task = SimpleNamespace(
        task_id=str(uuid4()), seed_queries=["Transformer model original paper definition purpose"]
    )
    # No task foreign key needed for this isolated decision fixture.
    from unittest.mock import AsyncMock

    s.db = SimpleNamespace(commit_value=AsyncMock())
    reader.task = lambda state: task
    result = await reader.reason({"round": 0, "searched_queries": []})
    assert result["pending_action"] == {"query": task.seed_queries[0], "language": "en"}


async def test_failed_search_feedback_reaches_the_next_bounded_round(db, settings, monkeypatch):
    from research_agent.graph.source_researcher import SourceResearcher
    from research_agent.graph.researcher import Researcher
    from unittest.mock import AsyncMock

    s = await start(db, settings)
    reader = SourceResearcher(s)
    monkeypatch.setattr(
        Researcher,
        "search",
        AsyncMock(
            return_value={
                "pending_hits": [],
                "stop_reason": "provider_unavailable",
                "search_errors": ["search_unavailable"],
            }
        ),
    )
    result = await reader.search({"pending_action": {"query": "bad long query", "language": "zh-CN"}})
    assert result["stop_reason"] is None
    assert result["retrieval_feedback"]["outcome"] == "search_unavailable"
    assert result["retrieval_feedback"]["query"] == "bad long query"
    # Cancellation/budget still stop; only query failure can consume another existing round.
    monkeypatch.setattr(
        Researcher, "search", AsyncMock(return_value={"pending_hits": [], "stop_reason": "budget_exhausted"})
    )
    assert (await reader.search({"pending_action": {"query": "q", "language": "en"}}))[
        "stop_reason"
    ] == "budget_exhausted"
    await db.terminal(s.run_id, s.fence, "cancelled", "fixture_done")
