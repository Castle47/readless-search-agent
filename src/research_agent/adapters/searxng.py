import httpx
import re
from ..contracts import SearchHit, stable_id, normalize
from ..errors import ToolFailure
from .fetch import canonical_url, domain_allowed


class Search:
    def __init__(self, settings, ops, request, transport=None):
        self.settings = settings
        self.ops = ops
        self.request = request
        self.client = httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False)

    async def close(self):
        await self.client.aclose()

    async def search(self, query, language, task_id=None):
        if getattr(self, "private_scope", False):
            return []
        allowed = getattr(self, "public_queries", None)
        if allowed is not None and query not in allowed:
            return []  # Never externalize queries synthesized from private evidence.
        query = normalize(query)
        query = re.sub(r"(?i)site:([^\s/]+)/\S*", r"site:\1", query)
        query_domains = [d.lower() for d in re.findall(r"(?<!-)\bsite:([a-zA-Z0-9.-]+)", query) if "." in d]
        # Search-engine filters are hints; redirect-aware code also enforces domain filters.
        suffix = " ".join("-site:" + d for d in self.request.blocked_domains)
        if len(self.request.allowed_domains) == 1:
            domain = self.request.allowed_domains[0]
            if not re.search(r"(?i)site:" + re.escape(domain) + r"(?:\s|$)", query):
                suffix += " site:" + domain
        elif self.request.allowed_domains:
            suffix += " (" + " OR ".join("site:" + d for d in self.request.allowed_domains) + ")"
        payload = {"q": query + " " + suffix, "language": language, "categories": "general", "format": "json", "safesearch": 0}

        async def call(timeout):
            try:
                response = await self.client.get(
                    self.settings.searxng_url.rstrip("/") + "/search", params=payload, timeout=timeout
                )
            except httpx.TimeoutException:
                raise ToolFailure("timeout", retryable=True) from None
            except httpx.RequestError:
                raise ToolFailure("network_error", retryable=True) from None
            code = response.status_code
            if code >= 400:
                raise ToolFailure("http_" + str(code), retryable=code in {408, 429} or code >= 500)
            try:
                data = response.json()
                raw = data["results"]
            except (ValueError, KeyError, TypeError):
                raise ToolFailure("invalid_search_response") from None
            degraded = [
                str(item[0])
                for item in data.get("unresponsive_engines", [])
                if isinstance(item, list) and item
            ]
            if not raw and degraded:
                raise ToolFailure("search_unavailable")
            result = []
            seen = set()
            for item in raw:
                try:
                    url = canonical_url(item["url"])
                except (KeyError, ToolFailure):
                    continue
                if url in seen or not domain_allowed(
                    url, self.request.allowed_domains, self.request.blocked_domains
                ):
                    continue
                if query_domains and not domain_allowed(url, query_domains):
                    continue
                seen.add(url)
                result.append(
                    SearchHit(
                        url=url,
                        title=str(item.get("title", "")),
                        snippet=str(item.get("content", "")),
                        rank=len(result) + 1,
                        published_at=None,
                        engines=list(dict.fromkeys(str(e) for e in item.get("engines", []) if e)),
                    ).model_dump(mode="json")
                )
            # URL shape is not authority: a random repository or docs subdomain
            # must not displace a relevant original publisher. Keep search rank.
            return {
                "hits": result[: self.ops.config["research"]["search_results"]],
                "degraded_engines": degraded,
            }

        result = await self.ops.run(
            "search:" + stable_id("query", payload),
            "search",
            payload,
            call,
            task_id=task_id,
            timeout=self.ops.config["search"]["timeout_seconds"],
        )
        if result.get("degraded_engines"):
            await self.ops.db.warning(
                self.ops.run_id, self.ops.fence, "search.degraded", {"engines": result["degraded_engines"]}
            )
        return [SearchHit.model_validate(x) for x in result["hits"]]
