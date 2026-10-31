"""Observed reading frontier and bounded breadth policy, independent of citations."""

import re

from ..adapters.fetch import canonical_url
from ..contracts import normalize
from ..storage.db import all_rows
from .documents import redundant_view, work_identity
from .retrieval import eligible_candidates


def query_language(query):
    return "zh-CN" if re.search(r"[\u4e00-\u9fff]", query) else "en"


def separate_queries(terms):
    """Split bilingual alternatives, preserving URLs, C/C++, and normal slashes."""
    groups = []
    for term in terms:
        parts = re.split(r"\s+[/／|]\s+", term.strip())
        if len({query_language(p) for p in parts}) < 2:
            parts = [term]
        groups.append([normalize(p) for p in parts if p.strip()])
    # First retain the distinct search intentions, then their translations.
    result, seen = [], set()
    for i in range(max(map(len, groups), default=0)):
        for group in groups:
            if i < len(group) and group[i].casefold() not in seen:
                seen.add(group[i].casefold())
                result.append(group[i])
    return result


def independent_works(sources):
    """Group identified works and exact-content mirrors, not page URLs."""
    groups = []
    for source in sources:
        identity = source.get("metadata", {}).get("document") or work_identity(source["canonical_url"])
        keys = {"url:" + canonical_url(source["canonical_url"])}
        if identity.get("work_id"):
            keys.add("work:" + identity["work_id"])
        keys.update("work:" + identifier for identifier in identity.get("identifiers", []))
        if source.get("content_sha256"):
            keys.add("hash:" + source["content_sha256"])
        matches = [g for g in groups if g["keys"] & keys]
        group = {"keys": keys, "source_ids": {source["id"]}}
        for match in matches:
            group["keys"].update(match["keys"])
            group["source_ids"].update(match["source_ids"])
            groups.remove(match)
        groups.append(group)
    return [sorted(g["source_ids"]) for g in groups]


async def reading_frontier(services, notes):
    sources = await services.evidence.source_index()
    async with services.db.pool.connection() as conn:
        plans = await all_rows(
            conn, "SELECT payload FROM research.plans WHERE run_id=%s ORDER BY batch_no", (services.run_id,)
        )
        operations = await all_rows(
            conn,
            "SELECT kind,result FROM research.operations WHERE run_id=%s AND kind IN ('discovery','search','source_read_failure') AND status IN ('succeeded','failed') ORDER BY operation_key",
            (services.run_id,),
        )
    discovery = next((r["result"] for r in operations if r["kind"] == "discovery"), {}) or {}
    failed_reads = {r["result"]["source_id"] for r in operations if r["kind"] == "source_read_failure"}
    read_ids = {n["source_id"] for n in notes}
    useful_ids = {
        n["source_id"]
        for n in notes
        if n.get("passages") and n.get("material_role") != "irrelevant" and n["source_id"] not in failed_reads
    }
    # Unread bibliographic records may connect two already-read representations.
    # They establish identity links, never add another read work to the count.
    works = [
        sorted(set(group) & useful_ids) for group in independent_works(sources) if set(group) & useful_ids
    ]
    read_sources = [s for s in sources if s["id"] in read_ids]
    planned = list(dict.fromkeys(u for p in plans for t in p["payload"]["tasks"] for u in t["source_urls"]))
    labels = {s["url"]: s for s in discovery.get("sources", [])}
    for source in discovery.get("sources", []):
        for link in source.get("reference_links", []):
            labels.setdefault(link["url"], {"title": link.get("label", ""), "selection_role": "unknown"})
    candidates, considered = [], list(read_sources)
    ordered_sources = sorted(
        sources, key=lambda s: work_identity(s["canonical_url"])["representation"] != "full_text"
    )
    for source in ordered_sources:
        if source["id"] in failed_reads:
            continue
        identity = source.get("metadata", {}).get("document") or work_identity(source["canonical_url"])
        if any(
            source["id"] == previous["id"]
            or (source.get("content_sha256") and source["content_sha256"] == previous.get("content_sha256"))
            or redundant_view(
                identity,
                previous.get("metadata", {}).get("document") or work_identity(previous["canonical_url"]),
            )
            for previous in considered
        ):
            continue
        # A stored abstract is an identity record when the full work is available.
        if identity["representation"] == "abstract" and any(
            (other.get("metadata", {}).get("document") or work_identity(other["canonical_url"])).get(
                "work_id"
            )
            == identity.get("work_id")
            and (other.get("metadata", {}).get("document") or work_identity(other["canonical_url"]))[
                "representation"
            ]
            == "full_text"
            for other in sources
        ):
            continue
        candidates.append(
            {
                "source_id": source["id"],
                "url": source["canonical_url"],
                "title": source["title"],
                "fetched": True,
                "planned": source["canonical_url"] in planned,
                "expected_role": labels.get(source["canonical_url"], {}).get("selection_role", "unknown"),
                "expected_contribution": labels.get(source["canonical_url"], {}).get("selection_reason", ""),
                "preview": labels.get(source["canonical_url"], {}).get("identity_context", "")[:1500],
            }
        )
        considered.append(source)
    remaining, rejected = await eligible_candidates(
        services,
        [{"url": u, "title": labels.get(u, {}).get("title", "Planned observed source")} for u in planned],
    )
    for candidate in remaining:
        candidates.append(
            {
                "source_id": None,
                "url": candidate["url"],
                "title": candidate["title"],
                "fetched": False,
                "planned": True,
                "expected_role": labels.get(candidate["url"], {}).get("selection_role", "unknown"),
            }
        )
    # The original and a teaching source lead the first batch. Thereafter every
    # frontier item is read or gets an explicit marginal-value decision.
    candidates.sort(
        key=lambda c: (
            {"original": 0, "explanation": 1, "implementation": 2}.get(c["expected_role"], 3),
            not c["fetched"],
        )
    )
    initial_searches = discovery.get("search_count", len(discovery.get("survey_queries", [])))
    extra_searches = max(0, sum(r["kind"] == "search" for r in operations) - initial_searches)
    minimum = (
        services.config["research"].get("overview_min_sources", 3)
        if services.understanding.get("intent") == "explanation"
        else 1
    )
    return {
        "minimum_useful_works": minimum,
        "read_works": works,
        "read_work_count": len(works),
        "extra_searches": extra_searches,
        "extra_search_limit": 0 if getattr(services.search, "private_scope", False) else services.config["research"].get("breadth_extra_searches", 2),
        "candidates": candidates,
        "unavailable_or_redundant": rejected,
        "reading_failures": [r["result"] for r in operations if r["kind"] == "source_read_failure"],
    }


def breadth_needs_search(frontier, useful_works=None):
    count = frontier["read_work_count"] if useful_works is None else useful_works
    return (
        count < frontier["minimum_useful_works"]
        and frontier["extra_searches"] < frontier["extra_search_limit"]
    )
