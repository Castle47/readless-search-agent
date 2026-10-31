from types import SimpleNamespace
from unittest.mock import AsyncMock

from research_agent.graph.researcher import Researcher
from research_agent.services.sections import reference_links


async def test_follow_selected_publisher_before_old_guide_links_and_skip_redirect_aliases():
    guide = {
        "id": "guide",
        "canonical_url": "https://guide.example/api/",
        "metadata": {
            "requested_url": "https://guide.example/api",
            "reference_links": [{"url": "https://guide.example/more", "official_link": False}],
        },
    }
    publisher = {
        "id": "publisher",
        "canonical_url": "https://acme.example/",
        "metadata": {"reference_links": [{"url": "https://acme.example/blog/cedar", "official_link": False}]},
    }
    bundle = {"sources": [guide, publisher], "evidence": [], "relations": []}
    task = SimpleNamespace(
        source_urls=["https://acme.example/", "https://guide.example/api"], success_criteria=[]
    )
    researcher = Researcher(SimpleNamespace(evidence=SimpleNamespace(bundle=AsyncMock(return_value=bundle))))
    researcher.task = lambda state: task
    result = await researcher.select_read({"visited_source_ids": ["guide", "publisher"], "discovery": {}})
    urls = [h["url"] for h in result["pending_hits"]]
    assert urls == ["https://acme.example/blog/cedar", "https://guide.example/more"]
    assert result["primary_domains"] == []  # Navigation does not certify authority.


def test_publisher_research_links_precede_login_and_social_navigation():
    links = reference_links(
        """<a href="https://console.acme.example/">Sign in</a>
        <a href="https://docs.acme.example/">Docs</a>
        <a href="/blog/introducing-cedar">(proof)</a>
        <a href="https://x.com/acme">X</a>""",
        "https://acme.example/",
        "介绍 Cedar 模型",
    )
    assert [x["url"] for x in links[:2]] == [
        "https://docs.acme.example/",
        "https://acme.example/blog/introducing-cedar",
    ]
    assert all(not x["official_link"] for x in links)


async def test_source_first_fallback_follows_observed_external_docs_before_reading_a_guide(
    settings, monkeypatch
):
    from research_agent.graph.source_researcher import SourceResearcher
    from research_agent.services.source_notes import SourceNotes

    monkeypatch.setattr(SourceNotes, "index", AsyncMock(return_value=[]))
    async def eligible(services, candidates):
        from research_agent.services.documents import work_identity
        return [{**h, "document": work_identity(h["url"])} for h in candidates], []
    monkeypatch.setattr("research_agent.graph.source_researcher.eligible_candidates", eligible)
    services = SimpleNamespace(
        config=settings.snapshot(), evidence=SimpleNamespace(source_index=AsyncMock(return_value=[]))
    )
    reader = SourceResearcher(services)
    reader.task = lambda state: SimpleNamespace(source_urls=[])
    result = await reader.select_read(
        {
            "visited_source_ids": [],
            "discovery": {
                "sources": [
                    {
                        "source_id": "guide",
                        "url": "https://guide.example/topic",
                        "reference_links": [
                            {
                                "url": "https://guide.example/api",
                                "documentation": True,
                                "official_link": False,
                            },
                            {
                                "url": "https://docs.publisher.example/start",
                                "documentation": True,
                                "official_link": False,
                            },
                        ],
                    }
                ]
            },
        }
    )
    assert [h["url"] for h in result["pending_hits"]] == ["https://docs.publisher.example/start"]
    assert result["direct_read"] and result["pending_source_ids"] == []
    assert result["primary_domains"] == []  # A navigation lead is not a source-identity verdict.
