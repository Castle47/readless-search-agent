import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

from research_agent.contracts import FetchResult, SearchHit, utcnow
from research_agent.errors import ToolFailure
from research_agent.services.adaptive_research import survey


async def test_blocked_teaching_source_is_replaced_before_planning(monkeypatch):
    original = "https://paper.example/original"
    blocked = "https://teaching.example/blocked"
    replacement = "https://teaching.example/readable"
    cache = {}

    async def saved(run, key):
        return cache.get(key)

    async def commit(run, fence, key, kind, value):
        cache[key] = value

    async def search(query, language):
        urls = [replacement] if "worked example tutorial" in query else [original, blocked]
        return [
            SearchHit(url=url, title=url, snippet="fixture", rank=i + 1, published_at=None)
            for i, url in enumerate(urls)
        ]

    async def select(services, hits, *args, **kwargs):
        return [{**h, "selection_role": "original" if h["url"] == original else "explanation"} for h in hits]

    monkeypatch.setattr("research_agent.services.adaptive_research.select_sources", select)

    async def fetch(url, **kwargs):
        if url == blocked:
            raise ToolFailure("robots_disallowed")
        text = "Original passage for " + url
        return FetchResult(
            requested_url=url,
            canonical_url=url,
            title=url,
            media_type="text/html",
            text=text,
            content_sha256=hashlib.sha256(text.encode()).hexdigest(),
            retrieved_at=utcnow(),
            published_at=None,
            publisher_key="example",
            source_type="unknown",
            metadata={},
        )

    s = SimpleNamespace(
        run_id="run",
        fence=1,
        config={"research": {"survey_queries": 2, "survey_pages": 4}},
        db=SimpleNamespace(
            saved_value=AsyncMock(side_effect=saved),
            commit_value=AsyncMock(side_effect=commit),
            phase=AsyncMock(),
        ),
        search=SimpleNamespace(search=AsyncMock(side_effect=search)),
        fetch=SimpleNamespace(fetch=AsyncMock(side_effect=fetch)),
        evidence=SimpleNamespace(source=AsyncMock(side_effect=lambda p: p.canonical_url)),
        warn=AsyncMock(),
    )
    state = {
        "understanding": {
            "intent": "explanation",
            "standalone_query": "Explain a mechanism",
            "search_terms": ["Example model original paper"],
        }
    }
    result = await survey(s, state)
    assert [x["url"] for x in result["discovery"]["sources"]] == [original, replacement]
    assert result["discovery"]["unavailable_sources"][0]["reason"] == "robots_disallowed"
    assert s.search.search.await_count == 2
    assert (await survey(s, state)) == result
    assert s.search.search.await_count == 2


def test_long_readiness_explanations_fit_the_assessment_contract():
    from types import SimpleNamespace

    from research_agent.contracts import CriterionAssessment
    from research_agent.services.adaptive_research import readiness_summary

    decision = SimpleNamespace(ready=True, explained_parts=["具体机制的完整说明" * 100], missing_parts=[])
    assessment = CriterionAssessment(
        criterion_id="Q1",
        status="covered",
        supporting_claim_ids=[],
        contradicting_claim_ids=[],
        explanation=readiness_summary(decision, False),
    )
    assert len(assessment.explanation) == 500
    assert decision.explained_parts == ["具体机制的完整说明" * 100]
