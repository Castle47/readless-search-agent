"""Public-web fetcher with DNS pinning, bounded decompression and per-run robots policy."""

import asyncio
import hashlib
import ipaddress
import socket
import zlib
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, urljoin
from urllib.robotparser import RobotFileParser
import httpcore
import httpx
import tldextract
import trafilatura
from ..contracts import FetchResult, stable_id, utcnow
from ..errors import ToolFailure

PSL = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)
AGENT = "ReadLessResearch/1.0"


def canonical_url(url):
    try:
        parts = urlsplit(url)
        if (
            parts.scheme.lower() not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
        ):
            raise ValueError()
        if any(ord(c) < 32 for c in url) or "\\" in url:
            raise ValueError()
        host = parts.hostname.lower().rstrip(".").encode("idna").decode("ascii")
        port = parts.port
        if port not in {None, 80, 443}:
            raise ValueError()
        authority = ("[" + host + "]") if ":" in host else host
        if port and (parts.scheme, port) not in {("http", 80), ("https", 443)}:
            authority += ":" + str(port)
        params = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in {"gclid", "fbclid"}
        ]
        return urlunsplit((parts.scheme.lower(), authority, parts.path or "/", urlencode(params), ""))
    except (ValueError, UnicodeError):
        raise ToolFailure("unsafe_url") from None


def domain_matches(host, domain):
    return host == domain or host.endswith("." + domain)


def domain_allowed(url, allowed=(), blocked=()):
    host = urlsplit(canonical_url(url)).hostname
    return not any(domain_matches(host, d) for d in blocked) and (
        not allowed or any(domain_matches(host, d) for d in allowed)
    )


def public_ip(value):
    try:
        ip = ipaddress.ip_address(value)
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        return ip.is_global and not ip.is_multicast and not ip.is_reserved
    except ValueError:
        return False


class PublicNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, resolver=None, backend=None):
        self.resolver = resolver
        self.backend = backend or httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        loop = asyncio.get_running_loop()
        try:
            addresses = await asyncio.wait_for(
                (
                    self.resolver(host, port)
                    if self.resolver
                    else loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
                ),
                timeout=timeout,
            )
            ips = sorted({a[4][0] for a in addresses})
        except (OSError, TimeoutError):
            raise ToolFailure("network_error", retryable=True) from None
        if not ips or any(not public_ip(ip) for ip in ips):
            raise ToolFailure("unsafe_address")
        # Connect directly to the verified IP; httpcore keeps original hostname for TLS SNI/cert validation.
        return await self.backend.connect_tcp(ips[0], port, timeout, local_address, socket_options)

    async def connect_unix_socket(self, *args, **kwargs):
        raise ToolFailure("unsafe_address")

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


class PublicDNS:
    """Trusted DoH for networks which synthesize private addresses for public sites.

    Resolution requests are in the fetch-attempt ledger. The resulting public IP is
    still pinned by PublicNetworkBackend; this does not permit private egress.
    """

    def __init__(self, ops, endpoint):
        self.ops = ops
        self.endpoint = endpoint
        self.client = httpx.AsyncClient(trust_env=False, follow_redirects=False)

    async def close(self):
        await self.client.aclose()

    async def __call__(self, host, port):
        try:
            address = ipaddress.ip_address(host)
            return [
                (
                    socket.AF_INET6 if address.version == 6 else socket.AF_INET,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    (host, port),
                )
            ]
        except ValueError:
            pass

        async def call(timeout):
            try:
                response = await self.client.get(
                    self.endpoint,
                    params={"name": host, "type": "A"},
                    headers={"Accept": "application/dns-json"},
                    timeout=timeout,
                )
                response.raise_for_status()
                value = response.json()
                addresses = [a["data"] for a in value.get("Answer", []) if a.get("type") == 1]
                if value.get("Status") != 0 or not addresses:
                    raise ToolFailure("dns_unavailable")
                if any(not public_ip(ip) for ip in addresses):
                    raise ToolFailure("unsafe_address")
                return {"addresses": sorted(set(addresses))}
            except httpx.TimeoutException:
                raise ToolFailure("timeout", retryable=True) from None
            except httpx.HTTPError:
                raise ToolFailure("dns_unavailable", retryable=True) from None

        value = await self.ops.run(
            "dns:" + stable_id("dns", host),
            "fetch_aux",
            {"host": host, "endpoint": self.endpoint},
            call,
            timeout=10,
        )
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in value["addresses"]]


class PublicTransport(httpx.AsyncHTTPTransport):
    def __init__(self, backend=None):
        super().__init__(trust_env=False, retries=0)
        self._pool = httpcore.AsyncConnectionPool(
            network_backend=backend or PublicNetworkBackend(),
            max_connections=6,
            max_keepalive_connections=0,
            retries=0,
        )


async def bounded_body(response, limit):
    encoding = response.headers.get("content-encoding", "identity").lower()
    if encoding not in {"identity", "gzip", "deflate", ""}:
        raise ToolFailure("unsupported_encoding")
    decoder = (
        zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
        if encoding in {"gzip", "deflate"}
        else None
    )
    output = bytearray()
    try:
        async for raw in response.aiter_raw(chunk_size=16384):
            data = decoder.decompress(raw, limit - len(output) + 1) if decoder else raw
            output.extend(data)
            if len(output) > limit or decoder and decoder.unconsumed_tail:
                raise ToolFailure("response_too_large")
        if decoder:
            output.extend(decoder.flush(limit - len(output) + 1))
            if not decoder.eof or decoder.unused_data:
                raise ToolFailure("invalid_compression")
        if len(output) > limit:
            raise ToolFailure("response_too_large")
    except zlib.error:
        raise ToolFailure("invalid_compression") from None
    return bytes(output)


def publisher(url):
    parsed = PSL(urlsplit(url).hostname)
    value = parsed.top_domain_under_public_suffix or urlsplit(url).hostname
    aliases = {"openai.com": "openai.com", "chatgpt.com": "openai.com"}
    return aliases.get(value, value)


def source_type(url):
    host = urlsplit(url).hostname
    if host in {"arxiv.org", "export.arxiv.org", "aclanthology.org", "openreview.net"}:
        return "paper"
    if host in {"github.com", "gitlab.com", "raw.githubusercontent.com"}:
        return "repository"
    if host.endswith(".gov") or host.startswith(("docs.", "developers.")):
        return "official"
    if host in {"reddit.com", "www.reddit.com", "news.ycombinator.com", "x.com"}:
        return "discussion"
    return "unknown"


class Fetcher:
    def __init__(self, ops, request, transport=None):
        self.ops = ops
        self.request = request
        self.config = ops.config["fetch"]
        self.robots = {}
        endpoint = ops.config.get("web_dns_endpoint", "system")
        self.dns = PublicDNS(ops, endpoint) if endpoint != "system" else None
        self.client = httpx.AsyncClient(
            transport=transport or PublicTransport(PublicNetworkBackend(resolver=self.dns)),
            follow_redirects=False,
            trust_env=False,
            headers={
                "User-Agent": AGENT,
                "Accept-Encoding": "gzip, deflate",
                "Accept": "text/html,text/plain;q=0.9",
            },
        )

    async def close(self):
        await self.client.aclose()
        if self.dns:
            await self.dns.close()

    def allowed(self, url):
        if not domain_allowed(url, self.request.allowed_domains, self.request.blocked_domains):
            raise ToolFailure("domain_blocked")

    async def raw(self, url, timeout):
        try:
            async with self.client.stream("GET", url, timeout=timeout) as response:
                code = response.status_code
                if code in {301, 302, 303, 307, 308}:
                    return {"redirect": response.headers.get("location"), "status": code}
                if code >= 400:
                    raise ToolFailure("http_" + str(code), retryable=code in {408, 429} or code >= 500)
                media = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if media not in {"text/html", "text/plain", "application/xhtml+xml"}:
                    raise ToolFailure("unsupported_content")
                data = await bounded_body(response, self.config["max_response_bytes"])
                encoding = response.encoding or "utf-8"
                return {"body": data.decode(encoding, errors="replace"), "media_type": media, "status": code}
        except httpx.TimeoutException:
            raise ToolFailure("timeout", retryable=True) from None
        except httpx.RequestError:
            raise ToolFailure("network_error", retryable=True) from None

    async def robots_allowed(self, url, task_id=None):
        origin = urlunsplit((*urlsplit(url)[:2], "", "", ""))
        if origin not in self.robots:
            robots_url = origin + "/robots.txt"
            try:
                result = await self.ops.run(
                    "robots:" + stable_id("url", origin),
                    "fetch_aux",
                    {"url": robots_url},
                    lambda timeout: self.raw(robots_url, timeout),
                    task_id=task_id,
                    timeout=self.config["timeout_seconds"],
                )
                if result.get("redirect"):
                    raise ToolFailure("robots_redirect_unconfirmed")
                parser = RobotFileParser()
                parser.parse(result["body"].splitlines())
                self.robots[origin] = parser
            except ToolFailure as error:
                if error.code in {"http_404", "http_410"}:
                    parser = RobotFileParser()
                    parser.parse([])
                    self.robots[origin] = parser
                else:
                    self.robots[origin] = None
        parser = self.robots[origin]
        if parser is None:
            raise ToolFailure("robots_unavailable")
        if not parser.can_fetch(AGENT, url):
            raise ToolFailure("robots_disallowed")

    async def fetch(self, url, task_id=None):
        if getattr(self, "private_scope", False):
            raise ToolFailure("private_scope_no_web_fetch")
        if urlsplit(url).hostname == "readless.invalid":
            raise ToolFailure("private_source_use_snapshot")
        requested = canonical_url(url)
        self.allowed(requested)

        async def operation(timeout):
            current = requested
            for redirect in range(self.config["max_redirects"] + 1):
                self.allowed(current)
                await self.robots_allowed(current, task_id)
                if redirect:
                    raw = await self.ops.run(
                        "redirect:" + stable_id("redirect", requested, current, redirect),
                        "fetch_aux",
                        {"url": current},
                        lambda t: self.raw(current, t),
                        task_id=task_id,
                        timeout=timeout,
                    )
                else:
                    raw = await self.raw(current, timeout)
                if raw.get("redirect"):
                    current = canonical_url(urljoin(current, raw["redirect"]))
                    continue
                break
            else:
                raise ToolFailure("too_many_redirects")
            text = raw["body"]
            title = current
            published = None
            metadata = {"extractor": "trafilatura", "requested_url": requested}
            from ..services.documents import work_identity

            metadata["document"] = work_identity(current, raw["body"] if raw["media_type"] != "text/plain" else None)
            identity_notes = []
            if raw["media_type"] != "text/plain":
                extracted = await asyncio.to_thread(
                    trafilatura.bare_extraction,
                    text,
                    url=current,
                    include_comments=False,
                    include_tables=True,
                    with_metadata=True,
                    as_dict=True,
                )
                if not extracted or not extracted.get("text"):
                    raise ToolFailure("empty_content")
                text = extracted["text"]
                title = extracted.get("title") or current
                published, date_metadata = explicit_dates(raw["body"])
                metadata.update(date_metadata)
                from ..services.sections import section_locations, reference_links, retained_identity_text

                metadata["sections"] = section_locations(raw["body"], text)
                metadata["reference_links"] = reference_links(raw["body"], current, self.request.query)
                identity_notes = retained_identity_text(raw["body"], text, self.config["chunk_chars"])
                from ..services.sections import retained_bibliography
                bibliography, fields = retained_bibliography(raw["body"], text, self.config["chunk_chars"])
                metadata["bibliographic_fields"] = fields
                identity_notes = list(dict.fromkeys(bibliography + identity_notes))
            if len(text.strip()) < 80:
                raise ToolFailure("empty_content")
            if identity_notes:
                metadata["extractor"] = "trafilatura+identity-footer-v1"
                suffix = "\n\n".join(identity_notes)
                available = self.config["max_text_chars"] - len(suffix) - 2
                if len(text) > available:
                    metadata["truncated"] = True
                text = text[:available].rstrip()
                main_end = len(text)
                metadata["sections"] = [
                    {**s, "end_char": min(s["end_char"], main_end)}
                    for s in metadata.get("sections", [])
                    if s["start_char"] < main_end
                ]
                text += "\n\n" + suffix
                metadata["identity_region"] = {"start_char": main_end + 2, "end_char": len(text)}
            if len(text) > self.config["max_text_chars"]:
                metadata["truncated"] = True
                text = text[: self.config["max_text_chars"]]
            result = FetchResult(
                requested_url=requested,
                canonical_url=current,
                title=title,
                media_type=raw["media_type"],
                text=text,
                content_sha256=hashlib.sha256(text.encode()).hexdigest(),
                retrieved_at=utcnow(),
                published_at=published,
                publisher_key=publisher(current),
                source_type=source_type(current),
                metadata=metadata,
            )
            return result.model_dump(mode="json")

        raw = await self.ops.run(
            "fetch:" + stable_id("url", requested),
            "fetch",
            {"url": requested},
            operation,
            task_id=task_id,
            timeout=self.config["timeout_seconds"],
        )
        return FetchResult.model_validate_json(__import__("json").dumps(raw))


def explicit_dates(html):
    """Only labelled metadata, never a heuristic date borrowed from unrelated page text."""
    import json
    from lxml import html as lxml_html

    published = []
    modified = []

    def parse(value):
        if not isinstance(value, str):
            return None
        value = value.strip()
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if len(value) == 10:
                result = result.replace(hour=23, minute=59, second=59)
            return (
                result.replace(tzinfo=timezone.utc)
                if result.tzinfo is None
                else result.astimezone(timezone.utc)
            )
        except ValueError:
            return None

    def collect(key, value):
        date = parse(value)
        if not date:
            return
        if key in {"datepublished", "article:published_time", "date", "pubdate", "publishdate"}:
            published.append(date)
        elif key in {"datemodified", "article:modified_time", "last-modified", "lastmod", "modified"}:
            modified.append(date)

    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key.lower() in {"datepublished", "datemodified"}:
                    collect(key.lower(), child)
                if isinstance(child, (dict, list)):
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    try:
        tree = lxml_html.fromstring(html)
        for element in tree.xpath("//meta[@content]"):
            collect(
                (element.get("property") or element.get("name") or element.get("itemprop") or "").lower(),
                element.get("content"),
            )
        for element in tree.xpath("//time[@datetime][@itemprop]"):
            collect(element.get("itemprop").lower(), element.get("datetime"))
        for script in tree.xpath('//script[@type="application/ld+json"]'):
            try:
                walk(json.loads(script.text or ""))
            except (ValueError, TypeError):
                pass
    except (ValueError, TypeError):
        pass
    return (
        max(published) if published else None,
        {
            "modified_at": max(modified).isoformat() if modified else None,
            "date_provenance": "explicit_page_metadata" if published or modified else "unknown",
        },
    )
