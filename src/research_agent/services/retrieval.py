"""Bounded selection over observed candidates; decisions survive checkpoint replay."""

import asyncio
from typing import Literal

from ..adapters.fetch import canonical_url, domain_allowed
from ..contracts import Contract, digest, stable_id, validate_ids
from ..errors import Stopped, ToolFailure
from ..storage.db import all_rows
from .documents import redundant_view, same_work, unreadable_url, work_identity


class CandidateDecision(Contract):
    candidate_id: str
    read: bool
    reason: str
    material_role: Literal[
        "original", "explanation", "implementation", "background", "irrelevant", "unknown"
    ] = "unknown"


class SourceSelection(Contract):
    decisions: list[CandidateDecision]


async def eligible_candidates(services, candidates):
    sources = await services.evidence.source_index()
    known = {
        u for s in sources for u in [s["canonical_url"], s.get("metadata", {}).get("requested_url")] if u
    }
    async with services.db.pool.connection() as conn:
        failed = await all_rows(
            conn,
            "SELECT operation_key,error FROM research.operations WHERE run_id=%s AND kind='fetch' AND status='failed'",
            (services.run_id,),
        )
        notes = await all_rows(
            conn,
            "SELECT result FROM research.operations WHERE run_id=%s AND kind='source_note' AND status='succeeded'",
            (services.run_id,),
        )
    read_ids = {
        r["result"]["source_id"]
        for r in notes
        if r["result"].get("passages") and r["result"].get("material_role") != "irrelevant"
    }
    failed = {r["operation_key"]: r["error"] or {} for r in failed}
    kept, rejected, seen = [], [], set()
    for h in candidates:
        u = canonical_url(h["url"])
        original_document = work_identity(u)
        if (
            getattr(services, "config", {}).get("adaptive_research_workflow_version")
            and original_document["representation"] == "pdf"
            and original_document["work_id"]
            and original_document["work_id"].startswith("arxiv:")
        ):
            # Resolve an observed arXiv identifier to its publication landing
            # record. This is retrieval, not a claim of authorship or support.
            h = {**h, "observed_pdf_url": u}
            u = canonical_url(
                "https://arxiv.org/abs/"
                + original_document["work_id"].split(":", 1)[1]
                + (original_document.get("edition") or "")
            )
        reason = None
        identity = work_identity(u)
        error = failed.get("fetch:" + stable_id("url", u), {})
        if not domain_allowed(u, services.request.allowed_domains, services.request.blocked_domains):
            reason = "domain_filter"
        elif u in seen or u in known:
            reason = "already_fetched_url"
        elif unreadable_url(u):
            reason = "unsupported_pdf_use_observed_html"
        elif error and not error.get("retryable"):
            reason = "cached_nonretryable_failure:" + error.get("code", "failed")
        elif any(
            s["id"] in read_ids
            and redundant_view(
                identity, s.get("metadata", {}).get("document") or work_identity(s["canonical_url"])
            )
            for s in sources
        ):
            reason = "already_read_full_text_of_work"
        seen.add(u)
        if reason:
            rejected.append({"url": u, "reason": reason})
        else:
            kept.append({**h, "url": u, "document": identity})
    output = []
    for h in kept:
        better = next(
            (
                q
                for q in kept
                if q is not h
                and q["document"]["representation"] == "full_text"
                and same_work(h["document"], q["document"])
                and not (
                    h["document"].get("edition")
                    and q["document"].get("edition")
                    and h["document"]["edition"] != q["document"]["edition"]
                )
            ),
            None,
        )
        if better and h["document"]["representation"] == "abstract":
            rejected.append(
                {"url": h["url"], "reason": "observed_full_text_preferred", "alternative": better["url"]}
            )
        elif h["document"]["representation"] == "full_text" and any(
            same_work(h["document"], q["document"])
            and h["document"]["edition"] == q["document"]["edition"]
            and q["document"]["representation"] == "full_text"
            for q in output
        ):
            rejected.append({"url": h["url"], "reason": "duplicate_full_text_representation"})
        else:
            output.append(h)
    return output, rejected


async def select_sources(services, candidates, question, criteria=(), query="", max_pages=None):
    key = "source-selection:" + digest([query, candidates] + ([max_pages] if max_pages is not None else []))
    if not hasattr(services, "source_selection_locks"):
        services.source_selection_locks = {}
    async with services.source_selection_locks.setdefault(key, asyncio.Lock()):
        return await _select_sources(services, candidates, question, criteria, query, key, max_pages)


async def _select_sources(services, candidates, question, criteria, query, key, max_pages=None):
    # Freeze a decision against the observed search result BEFORE checking mutable
    # already-read state, so restart after a fetch cannot buy another selection.
    saved = await services.db.saved_value(services.run_id, key)
    if saved is not None:
        return saved["selected"]
    frozen = await services.db.saved_value(services.run_id, key + ":input")
    if frozen is not None:
        candidates, rejected = frozen["candidates"], frozen["rejected"]
    else:
        candidates, rejected = await eligible_candidates(services, candidates)
    cap = max_pages if max_pages is not None else services.config["research"]["pages_per_round"]
    if frozen is None:
        payload = {
            "question": question,
            "search_query": query,
            "reader_goal": services.understanding.get("reader_goal"),
            "local_topic_context": services.understanding.get("local_topic_context", []),
            "criteria": list(criteria),
            "max_pages": cap,
            "candidates": [
                {
                    "candidate_id": f"C{i}",
                    "url": h["url"],
                    "title": h.get("title", ""),
                    "snippet": h.get("snippet", ""),
                    "engines": h.get("engines", []),
                    "representation": h["document"]["representation"],
                }
                for i, h in enumerate(candidates)
            ],
        }
        omitted = []
        while payload["candidates"]:
            try:
                services.llm.build(
                    "source_selector",
                    payload,
                    SourceSelection,
                    "Choose only offered IDs, at most max_pages; one decision per candidate.",
                )
                break
            except Stopped as error:
                if error.code != "input_budget_exceeded":
                    raise
                long = max(payload["candidates"], key=lambda h: len(h.get("snippet", "")))
                if long.get("snippet"):
                    omitted.append(long["candidate_id"])
                    long["snippet"] = ""
                    long["snippet_omitted_for_capacity"] = True
                elif payload["criteria"]:
                    payload["criteria"] = []
                else:
                    rejected.extend(
                        {"url": h["url"], "reason": "selector_input_capacity"} for h in candidates
                    )
                    payload["candidates"] = []
                    candidates = []
    if frozen is not None:
        payload, omitted = frozen["payload"], frozen["omitted"]
    else:
        await services.db.commit_value(
            services.run_id,
            services.fence,
            key + ":input",
            "source_selection_input",
            {"candidates": candidates, "rejected": rejected, "payload": payload, "omitted": omitted},
        )
    selected = []
    decisions = []
    if candidates:

        def check(value):
            ids = [x.candidate_id for x in value.decisions]
            validate_ids(ids, [c["candidate_id"] for c in payload["candidates"]])
            if len(ids) != len(set(ids)) or set(ids) != {c["candidate_id"] for c in payload["candidates"]}:
                raise ValueError("Return exactly one decision for every offered candidate")
            if sum(x.read for x in value.decisions) > cap:
                raise ValueError("Select at most max_pages")

        try:
            value = await services.llm.generate(
                key + ":model", "source_selector", payload, SourceSelection, validator=check
            )
            decisions = [x.model_dump() for x in value.decisions]
            selected = [
                {
                    **candidates[int(x.candidate_id[1:])],
                    "selection_role": x.material_role,
                    "selection_reason": x.reason,
                }
                for x in value.decisions
                if x.read
            ]
        except (Stopped, ToolFailure) as error:
            if error.code in {"cancelled", "deadline", "budget_exhausted"} or getattr(error, "fatal", False):
                raise
            await services.warn("source_selection.degraded", reason=error.code)
    await services.db.commit_value(
        services.run_id,
        services.fence,
        key,
        "source_selection",
        {
            "input": payload,
            "selected": selected,
            "decisions": decisions,
            "rejected": rejected,
            "omitted_snippets": omitted,
        },
    )
    return selected
