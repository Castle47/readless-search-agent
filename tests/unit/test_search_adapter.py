import httpx
import pytest
from research_agent.adapters.searxng import Search
from research_agent.contracts import ResearchRequest
from research_agent.errors import ToolFailure


class Ops:
    def __init__(self, settings):
        self.config = settings.snapshot()

    async def run(self, key, kind, payload, call, **kwargs):
        return await call(2)


async def test_search_does_not_pin_a_guessed_old_page_path(settings):
    queries = []

    def response(request):
        queries.append(request.url.params["q"])
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://docs.python.org/3/builtins/stdtypes.html",
                        "title": "Types",
                        "content": "a search lead",
                    }
                ],
                "unresponsive_engines": [],
            },
        )

    search = Search(
        settings,
        Ops(settings),
        ResearchRequest(query="tuple", allowed_domains=["docs.python.org"]),
        transport=httpx.MockTransport(response),
    )
    hits = await search.search("site:docs.python.org/3/library/stdtypes.html tuple", "en")
    assert queries[0].count("site:docs.python.org") == 1
    assert "library/stdtypes" not in queries[0]
    assert hits[0].url == "https://docs.python.org/3/builtins/stdtypes.html"
    await search.close()


async def test_provider_blocks_are_not_reported_as_genuine_empty_search(settings):
    def response(request):
        return httpx.Response(200, json={"results": [], "unresponsive_engines": [["engine", "CAPTCHA"]]})

    search = Search(
        settings, Ops(settings), ResearchRequest(query="tuple"), transport=httpx.MockTransport(response)
    )
    with pytest.raises(ToolFailure, match="search_unavailable"):
        await search.search("tuple", "en")
    await search.close()


async def test_model_site_filter_is_enforced_even_if_engine_ignores_it(settings):
    def response(request):
        return httpx.Response(
            200,
            json={
                "results": [
                    {"url": "https://example.org/unrelated", "title": "Lists", "content": "not Python"},
                    {"url": "https://docs.python.org/3/", "title": "Python", "content": "lead"},
                ],
                "unresponsive_engines": [],
            },
        )

    search = Search(
        settings, Ops(settings), ResearchRequest(query="list"), transport=httpx.MockTransport(response)
    )
    hits = await search.search("site:docs.python.org Python list", "en")
    assert [h.url for h in hits] == ["https://docs.python.org/3/"]
    await search.close()
