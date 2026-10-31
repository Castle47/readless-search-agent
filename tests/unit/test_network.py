import zlib
import httpx
import pytest
from research_agent.adapters.fetch import PublicNetworkBackend, bounded_body
from research_agent.errors import ToolFailure


class Backend:
    def __init__(self):
        self.connected = []

    async def connect_tcp(self, host, *args):
        self.connected.append(host)
        return object()


async def test_dns_pin_does_not_resolve_twice():
    backend = Backend()
    lookups = []

    async def resolve(host, port):
        lookups.append(host)
        return (
            [(2, 1, 6, "", ("8.8.8.8", port))] if len(lookups) == 1 else [(2, 1, 6, "", ("127.0.0.1", port))]
        )

    network = PublicNetworkBackend(resolve, backend)
    await network.connect_tcp("example.com", 443)
    assert lookups == ["example.com"]
    assert backend.connected == ["8.8.8.8"]
    with pytest.raises(ToolFailure):
        await network.connect_tcp("example.com", 443)
    assert backend.connected == ["8.8.8.8"]


class Stream(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        yield self.data


async def test_compression_bomb_is_bounded():
    compressor = zlib.compressobj(wbits=16 + zlib.MAX_WBITS)
    data = compressor.compress(b"x" * 3000000) + compressor.flush()
    response = httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=Stream(data))
    with pytest.raises(ToolFailure, match="response_too_large"):
        await bounded_body(response, 1024)


async def test_unicode_body_is_preserved():
    data = "中文🐿️e\u0301".encode()
    response = httpx.Response(200, stream=Stream(data))
    assert await bounded_body(response, 1024) == data


async def test_search_sends_the_specified_general_category_without_changing_query(settings):
    from types import SimpleNamespace
    from research_agent.adapters.searxng import Search
    from research_agent.contracts import ResearchRequest

    observed = []

    def response(request):
        observed.append(dict(request.url.params))
        return httpx.Response(200, json={"results": [], "unresponsive_engines": []})

    async def run(key, kind, payload, call, **kwargs):
        assert kind == "search"
        return await call(kwargs["timeout"])

    search = Search(
        settings,
        SimpleNamespace(config=settings.snapshot(), run=run),
        ResearchRequest(query="Transformer"),
        transport=httpx.MockTransport(response),
    )
    try:
        await search.search("Transformer architecture", "en")
    finally:
        await search.close()
    assert observed[0]["categories"] == "general"
    assert observed[0]["q"].strip() == "Transformer architecture"
    assert observed[0]["language"] == "en"


def test_explicit_publication_and_modification_are_separate():
    from research_agent.adapters.fetch import explicit_dates

    published, metadata = explicit_dates(
        '<html><head><meta property="article:published_time" content="2020-01-01T12:00:00+00:00"><script type="application/ld+json">{"dateModified":"2025-01-01T12:00:00Z"}</script></head><body>Last year 2019</body></html>'
    )
    assert published.year == 2020
    assert metadata["modified_at"].startswith("2025-01-01")
    unknown, metadata = explicit_dates("<html><body>This is not a publication date: 2024-01-01</body></html>")
    assert unknown is None
    assert metadata["date_provenance"] == "unknown"


@pytest.mark.parametrize("text_limit", [100000, 500])
async def test_fetch_snapshot_preserves_affiliation_text_with_exact_hash_and_bounds(settings, text_limit):
    import hashlib
    from types import SimpleNamespace
    from research_agent.adapters.fetch import Fetcher
    from research_agent.contracts import ResearchRequest

    main = (
        "Cedar accepts application context and returns a typed decision for the surrounding software. " * 20
    )
    disclosure = "Independent guide. Not affiliated with Acme Labs."
    html = f"<html><head><title>Cedar guide</title></head><body><main><h1>Cedar</h1><p>{main}</p></main><footer><p>{disclosure}</p><small>© 2026 Independent Notes</small></footer></body></html>"
    requests = []

    def response(request):
        requests.append(request.url.path)
        robots = request.url.path == "/robots.txt"
        body = "User-agent: *\nAllow: /" if robots else html
        return httpx.Response(
            200,
            headers={"Content-Type": "text/plain" if robots else "text/html"},
            stream=Stream(body.encode()),
        )

    async def run(key, kind, payload, call, **kwargs):
        assert kind in {"fetch", "fetch_aux"}
        return await call(kwargs.get("timeout", 20))

    config = settings.snapshot()
    config["web_dns_endpoint"] = "system"
    config["fetch"]["max_text_chars"] = text_limit
    fetcher = Fetcher(
        SimpleNamespace(config=config, run=run),
        ResearchRequest(query="Explain Cedar"),
        transport=httpx.MockTransport(response),
    )
    try:
        page = await fetcher.fetch("https://example.org/cedar")
    finally:
        await fetcher.close()
    assert requests == ["/robots.txt", "/cedar"]
    assert disclosure in page.text and "© 2026 Independent Notes" in page.text
    assert len(page.text) <= text_limit
    assert hashlib.sha256(page.text.encode()).hexdigest() == page.content_sha256
    region = page.metadata["identity_region"]
    assert 0 <= region["start_char"] < region["end_char"] == len(page.text)
    assert disclosure in page.text[region["start_char"] : region["end_char"]]
    assert all(s["end_char"] <= region["start_char"] for s in page.metadata["sections"])
    assert page.metadata.get("truncated", False) == (text_limit == 500)
    assert "primary_for_claim" not in page.metadata  # Preservation never grants authority.
