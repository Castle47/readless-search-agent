"""Broad source survey and semantic material readiness, cached per snapshot."""

import asyncio
import re

from ..adaptive_contracts import ReadinessReview
from ..contracts import CriterionAssessment, digest, validate_ids
from ..errors import Stopped, ToolFailure
from .documents import same_work, unreadable_url, work_identity
from .research_breadth import breadth_needs_search, query_language, reading_frontier, separate_queries
from .retrieval import select_sources


async def readable_full_text(services, page, task_id=None):
    """Follow an observed full-text representation of this exact work only."""
    document = work_identity(page.canonical_url)
    if document.get("representation") != "abstract":
        return page
    for link in page.metadata.get("reference_links", []):
        if (
            link.get("full_text")
            and not unreadable_url(link["url"])
            and same_work(document, work_identity(link["url"]))
        ):
            try:
                full = await services.fetch.fetch(link["url"], task_id=task_id)
                await services.evidence.source(full)
                return full
            except (Stopped, ToolFailure) as error:
                if error.code == "cancelled" or getattr(error, "fatal", False):
                    raise
                await services.warn("survey.full_text_unavailable", reason=error.code)
    return page


def readiness_summary(assessment, breadth_pending):
    """Bound the diagnostic summary; the complete model judgment stays stored."""
    prefix = "尚有有价值的未读候选或互补资料缺口。" if breadth_pending else ""
    parts = assessment.explained_parts if assessment.ready else assessment.missing_parts
    return (prefix + "；".join(parts) or "解释材料仍不足。")[:500]


async def survey(services, state):
    services.understanding = state["understanding"]
    key = "discovery:reader-centred:v2"
    prior = await services.db.saved_value(services.run_id, key)
    if prior is not None:
        return {"discovery": prior}
    await services.db.phase(services.run_id, services.fence, "discover")
    u = state["understanding"]
    queries = separate_queries(u.get("search_terms") or [u["standalone_query"]])[
        : (1 if u.get("intent") == "fact" else services.config["research"]["survey_queries"])
    ]
    hits = []
    errors = []
    for query in queries:
        language = query_language(query)
        try:
            hits += [h.model_dump(mode="json") for h in await services.search.search(query, language)]
        except (Stopped, ToolFailure) as error:
            if error.code == "cancelled" or getattr(error, "fatal", False):
                raise
            errors.append({"query": query, "reason": error.code})
            if error.code in {"budget_exhausted", "deadline"}:
                break
    result = {
        "sources": [],
        "searched": True,
        "survey_queries": queries,
        "search_count": len(queries),
        "candidate_urls": list(dict.fromkeys(h["url"] for h in hits)),
        "search_errors": errors,
        "unavailable_sources": [],
    }

    attempted = set()

    async def read_selected(selected):
        for hit in selected:
            if hit["url"] in attempted:
                continue
            attempted.add(hit["url"])
            try:
                page = await services.fetch.fetch(hit["url"])
                await services.evidence.source(page)
                page = await readable_full_text(services, page)
                sid = await services.evidence.source(page)
                context = (
                    page.text if len(page.text) <= 3000 else page.text[:2400] + "\n…\n" + page.text[-600:]
                )
                result["sources"].append(
                    {
                        "source_id": sid,
                        "url": page.canonical_url,
                        "title": page.title,
                        "identity_context": context,
                        "reference_links": page.metadata.get("reference_links", []),
                        "selection_role": hit.get("selection_role", "unknown"),
                        "selection_reason": hit.get("selection_reason", ""),
                    }
                )
            except (Stopped, ToolFailure) as error:
                if error.code == "cancelled" or getattr(error, "fatal", False):
                    raise
                result["unavailable_sources"].append(
                    {"url": hit["url"], "role": hit.get("selection_role", "unknown"), "reason": error.code}
                )
                await services.warn("survey.page_unavailable", reason=error.code, url=hit["url"])

    try:
        selected = await select_sources(
            services,
            hits,
            u["standalone_query"],
            query=" | ".join(queries),
            max_pages=services.config["research"]["survey_pages"],
        )
        await read_selected(selected)
        # Replace inaccessible choices from the already observed candidate pool
        # before buying another search. Selection still judges contribution.
        target = services.config["research"]["survey_pages"] if u.get("intent") != "fact" else 1
        for attempt in range(2):
            if len(result["sources"]) >= target or not result["unavailable_sources"]:
                break
            pool = [h for h in hits if h["url"] not in attempted]
            if not pool:
                break
            substitutes = await select_sources(
                services,
                pool,
                u["standalone_query"]
                + " Select accessible complementary alternatives to the failed choices.",
                query="survey replacement " + str(attempt),
                max_pages=target - len(result["sources"]),
            )
            if not substitutes:
                break
            await read_selected(substitutes)
        roles = {s["selection_role"] for s in result["sources"]}
        minimum = services.config["research"].get("overview_min_sources", 3)
        needs_breadth = len(result["sources"]) < minimum or not roles.intersection(
            {"explanation", "implementation"}
        )
        if u.get("intent") == "explanation" and needs_breadth:
            subject = next((q for q in queries if query_language(q) == "en"), queries[0])
            subject = re.sub(r"\b(original|paper|official)\b", "", subject, flags=re.IGNORECASE)
            query = " ".join(subject.split()) + (
                " worked example tutorial" if query_language(subject) == "en" else " 讲解 实例"
            )
            result["search_count"] += 1
            new_hits = await services.search.search(query, query_language(query))
            replacements = await select_sources(
                services,
                [h.model_dump(mode="json") for h in new_hits],
                u["standalone_query"]
                + " Find complementary, accessible teaching or implementation material; compare alternatives, not another mirror.",
                query=query,
                max_pages=max(1, target - len(result["sources"])),
            )
            result["replacement_query"] = query
            result["candidate_urls"] = list(
                dict.fromkeys(result["candidate_urls"] + [h.url for h in new_hits])
            )
            await read_selected(replacements)
    except (Stopped, ToolFailure) as error:
        if error.code == "cancelled" or getattr(error, "fatal", False):
            raise
        await services.warn("survey.unavailable", reason=error.code)
    await services.db.commit_value(services.run_id, services.fence, key, "discovery", result)
    return {"discovery": result}


async def semantic_readiness(services, criteria):
    value = await material_assessment(services, criteria)
    return [CriterionAssessment.model_validate(r) for r in value["assessments"]]


async def material_assessment(services, criteria):
    # Same snapshot shared across overlapping tasks and replay; only one model
    # decision may be bought at a time for a run's reading frontier.
    if not hasattr(services, "material_readiness_lock"):
        services.material_readiness_lock = asyncio.Lock()
    async with services.material_readiness_lock:
        return await _material_assessment(services, criteria)


async def _material_assessment(services, criteria):
    from .source_notes import SourceNotes

    all_notes = await SourceNotes(services).index()
    notes = [n for n in all_notes if n.get("passages") and n.get("material_role") != "irrelevant"]
    frontier = await reading_frontier(services, all_notes)

    def missing(reason):
        return [
            CriterionAssessment(
                criterion_id=c.criterion_id,
                status="missing",
                supporting_claim_ids=[],
                contradicting_claim_ids=[],
                explanation=reason,
            )
            for c in criteria
        ]

    key = "readiness:semantic:v2:" + digest([notes, [c.model_dump() for c in criteria], frontier])
    saved = await services.db.saved_value(services.run_id, key)
    if saved is not None:
        return saved
    if not notes or (
        frontier["read_work_count"] < frontier["minimum_useful_works"] and frontier["candidates"]
    ):
        reason = "先阅读已选和计划中的互补资料，主题已提及不等于研究完成。"
        value = {
            "assessments": [r.model_dump() for r in missing(reason)],
            "review": {"deferred": reason},
            "input": {"breadth": frontier},
            "next_sources": frontier["candidates"],
            "needs_search": not frontier["candidates"],
            "search_queries": [],
        }
        await services.db.commit_value(services.run_id, services.fence, key, "material_readiness", value)
        return value
    by_source = {}
    for note in notes:
        item = by_source.setdefault(note["source_id"], {"notes": [], "open_questions": [], "passages": {}})
        if note.get("identity"):
            item["identity"] = {
                k: note["identity"][k]
                for k in ["publisher_name", "basis", "identity_scope"]
                if k in note["identity"]
            }
        item.setdefault("material_roles", []).append(note.get("material_role", "original"))
        item["notes"].append(note["notes"])
        item["open_questions"] += note.get("open_questions", [])
        for p in note["passages"]:
            item["passages"][p["passage_id"]] = p
    aliases = {}
    payload = {
        "question": services.understanding.get("standalone_query", services.request.query),
        "reader_goal": services.understanding.get("reader_goal"),
        "criteria": [
            {"criterion_id": f"Q{i}", "question": c.description, "required": c.required, "kind": c.kind}
            for i, c in enumerate(criteria)
        ],
        "sources": [],
        "passages": [],
        "breadth_policy": {k: v for k, v in frontier.items() if k != "candidates"},
        "unread_candidates": [{**c, "candidate_id": f"U{i}"} for i, c in enumerate(frontier["candidates"])],
    }
    for sid, item in by_source.items():
        source = await services.evidence.get_source(sid)
        alias = f"S{len(aliases)}"
        aliases[alias] = sid
        payload["sources"].append(
            {
                "source_id": alias,
                "title": source["title"],
                "url": source["canonical_url"],
                "source_identity": item.get("identity"),
                "material_roles": sorted(set(item["material_roles"])),
                "notes": list(dict.fromkeys(item["notes"])),
                "open_questions": list(dict.fromkeys(item["open_questions"])),
            }
        )
    # Notes describe the whole reading; original passages validate that there
    # is explanatory substance. Keep complete spans, never fabricate excerpts.
    queues = [list(by_source[sid]["passages"].values()) for sid in aliases.values()]
    while any(queues):
        for i, queue in enumerate(queues):
            if not queue:
                continue
            p = queue.pop(0)
            payload["passages"].append({"source_id": f"S{i}", "text": p["text"]})
            try:
                services.llm.build("readiness", payload, ReadinessReview)
            except Stopped as error:
                if error.code != "input_budget_exceeded":
                    raise
                payload["passages"].pop()
            await asyncio.sleep(0)

    def validate(value):
        expected = {f"Q{i}" for i in range(len(criteria))}
        if len(value.criteria) != len(expected) or {r.criterion_id for r in value.criteria} != expected:
            raise ValueError("Return one semantic readiness judgment for each Q question")
        if {c.source_id for c in value.source_contributions} != set(aliases) or len(
            value.source_contributions
        ) != len(aliases):
            raise ValueError("Compare every offered S source exactly once")
        expected_candidates = {c["candidate_id"] for c in payload["unread_candidates"]}
        if {c.candidate_id for c in value.candidate_decisions} != expected_candidates or len(
            value.candidate_decisions
        ) != len(expected_candidates):
            raise ValueError("Decide read or skip for every offered U candidate exactly once")
        for r in value.criteria:
            validate_ids(r.source_ids, aliases)
            if r.ready and (not r.explained_parts or not r.source_ids):
                raise ValueError("Ready requires explained parts and source support")

    try:
        value = await services.llm.generate(
            key + ":model", "readiness", payload, ReadinessReview, validator=validate
        )
        indexed = {r.criterion_id: r for r in value.criteria}
        next_sources = [
            frontier["candidates"][int(c.candidate_id[1:])] for c in value.candidate_decisions if c.read
        ]
        useful = {aliases[c.source_id] for c in value.source_contributions if c.adds_value}
        useful_count = sum(bool(set(group) & useful) for group in frontier["read_works"])
        needs_search = bool(value.search_queries) or (
            not next_sources and breadth_needs_search(frontier, useful_count)
        )
        breadth_pending = bool(next_sources) or needs_search
        search_queries = separate_queries(value.search_queries)
        results = [
            CriterionAssessment(
                criterion_id=c.criterion_id,
                status="covered" if indexed[f"Q{i}"].ready and not breadth_pending else "missing",
                supporting_claim_ids=[],
                contradicting_claim_ids=[],
                explanation=readiness_summary(indexed[f"Q{i}"], breadth_pending),
            )
            for i, c in enumerate(criteria)
        ]
        detail = value.model_dump(mode="json")
    except (Stopped, ToolFailure) as error:
        if error.code == "cancelled" or getattr(error, "fatal", False):
            raise
        await services.warn("readiness.unavailable", reason=error.code)
        results = missing("材料就绪评估未完成，不自动把来源身份当作解释充分。")
        detail = {"error": error.code}
        next_sources, needs_search, search_queries = frontier["candidates"], True, []
    result = {
        "assessments": [r.model_dump(mode="json") for r in results],
        "review": detail,
        "input": payload,
        "next_sources": next_sources,
        "needs_search": needs_search,
        "search_queries": search_queries,
    }
    await services.db.commit_value(
        services.run_id,
        services.fence,
        key,
        "material_readiness",
        result,
    )
    return result
