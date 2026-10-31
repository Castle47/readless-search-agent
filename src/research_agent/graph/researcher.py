import re

from langgraph.graph import END, START, StateGraph

from ..contracts import (
    EvidenceVerification,
    Extraction,
    FetchResult,
    NextAction,
    ResearchResult,
    ResearchTask,
    canonical,
    digest,
    normalize,
    stable_id,
    validate_ids,
)
from ..errors import Stopped, ToolFailure
from .state import TaskInput, TaskOutput, TaskState


class Researcher:
    def __init__(self, services):
        self.s = services

    def task(self, state):
        return ResearchTask.model_validate(state["task"])

    async def criterion_assessments(self, task, batch_no):
        return await self.s.evidence.assessments(
            task.success_criteria, self.s.understanding["source_policy"], batch_no
        )

    async def guard(self, state):
        task = self.task(state)
        defaults = {
            k: state.get(k, v)
            for k, v in {
                "round": 0,
                "no_progress_count": 0,
                "query_keys": [],
                "searched_queries": [],
                "visited_source_ids": [],
                "read_chunks": {},
                "claim_ids": [],
                "evidence_ids": [],
                "criterion_results": [],
                "search_errors": [],
                "stop_reason": None,
            }.items()
        }
        if defaults["stop_reason"]:
            return defaults
        try:
            await self.s.ops.guard()
        except Stopped as error:
            return {**defaults, "stop_reason": error.code}
        if not defaults["round"]:
            await self.s.db.commit_value(
                self.s.run_id,
                self.s.fence,
                "task-start:" + task.task_id,
                "task_start",
                {"task_id": task.task_id},
                task.task_id,
            )
        results = await self.criterion_assessments(task, state["batch_no"])
        defaults["criterion_results"] = [r.model_dump() for r in results]
        required_facets = {f["facet_id"] for f in self.s.understanding["facets"] if f["required"]}
        core = {
            c.criterion_id
            for c in task.success_criteria
            if c.required and required_facets.intersection(c.facet_ids or task.facet_ids)
        }
        needed = [r for r in results if not core or r.criterion_id in core]
        if all(r.status == "covered" for r in needed) and (
            self.s.understanding.get("intent") == "fact"
            or not self.s.config.get("editorial_workflow_version")
            or self.s.config.get("adaptive_research_workflow_version")
        ):
            defaults["stop_reason"] = "sufficient"
        elif defaults["round"] >= task.max_rounds:
            defaults["stop_reason"] = "max_rounds"
        elif defaults["no_progress_count"] >= self.s.config["research"]["no_progress_rounds"]:
            defaults["stop_reason"] = "no_progress"
        return defaults

    async def reason(self, state):
        task = self.task(state)
        bundle = await self.s.evidence.bundle(task.task_id)
        payload = {
            "task": state["task"],
            "reader_goal": self.s.understanding.get("reader_goal"),
            "criterion_results": state["criterion_results"],
            "searched_queries": state["searched_queries"],
            "claims": [
                {"id": c["id"], "text": c["canonical_text"], "status": c["status"]} for c in bundle["claims"]
            ][-8:],
            "search_languages": self.s.understanding["search_languages"],
        }
        if self.s.config.get("retrieval_workflow_version") and state.get("retrieval_feedback"):
            payload["retrieval_feedback"] = state["retrieval_feedback"]
        if self.s.config.get("source_first_workflow_version"):
            from ..services.source_notes import SourceNotes

            notes = await SourceNotes(self.s).index()
            sources = {s["id"]: s for s in await self.s.evidence.source_index()}
            payload["research_notes"] = []
            for note in sorted(notes, key=lambda n: not bool(n.get("firsthand_criterion_ids"))):
                payload["research_notes"].append(
                    {
                        "url": sources[note["source_id"]]["canonical_url"],
                        "notes": note["notes"],
                        "material_role": note.get("material_role", "original"),
                        "open_questions": note.get("open_questions", []),
                        "identity_established": bool(note["identity"]),
                        "firsthand_criterion_ids": note.get("firsthand_criterion_ids", []),
                    }
                )
                try:
                    self.s.llm.build("reason", payload, NextAction)
                except Stopped as error:
                    if error.code != "input_budget_exceeded":
                        raise
                    payload["research_notes"].pop()
        if self.s.config.get("adaptive_research_workflow_version"):
            readiness = await SourceNotes(self.s).readiness()
            payload["reading_completion"] = {
                "needs_search": readiness["needs_search"],
                "review": readiness["review"],
                "unread_candidates": readiness["next_sources"],
            }
        action = None
        try:
            action = await self.s.llm.generate(
                f"reason:task={task.task_id}:round={state['round'] + 1}:v1",
                "reason",
                payload,
                NextAction,
                task_id=task.task_id,
                validator=lambda a: validate_ids(
                    a.target_criterion_ids, [c.criterion_id for c in task.success_criteria]
                ),
            )
        except Stopped as error:
            if error.code not in {"input_budget_exceeded"}:
                return {"stop_reason": error.code}
        except ToolFailure as error:
            if error.fatal:
                raise
            await self.s.warn("reason_degraded", task_id=task.task_id)
        required = {f["facet_id"] for f in self.s.understanding["facets"] if f["required"]}
        core = {
            c.criterion_id
            for c in task.success_criteria
            if c.required and required.intersection(c.facet_ids or task.facet_ids)
        }
        if (
            action
            and action.action == "finish"
            and all(
                r["status"] == "covered"
                for r in state["criterion_results"]
                if not core or r["criterion_id"] in core
            )
        ):
            return {"stop_reason": "sufficient"}
        queries = (
            [action.query] if action and action.action == "search" and action.query else []
        ) + task.seed_queries
        selected = next((q for q in queries if normalize(q).casefold() not in state["query_keys"]), None)
        if selected is None:
            return {"stop_reason": "no_new_query"}
        # Declining finish cannot override the database criterion threshold.
        language = (
            action.language
            if action and action.language in self.s.understanding["search_languages"]
            else self.s.understanding["search_languages"][0]
        )
        return {"pending_action": {"query": selected, "language": language}, "stop_reason": None}

    async def search(self, state):
        task = self.task(state)
        action = state["pending_action"]
        try:
            hits = await self.s.search.search(action["query"], action["language"], task.task_id)
            result = {"pending_hits": [h.model_dump(mode="json") for h in hits]}
        except Stopped as error:
            return {"stop_reason": error.code, "pending_hits": []}
        except ToolFailure as error:
            if error.fatal:
                raise
            result = {
                "pending_hits": [],
                "search_errors": sorted(set(state["search_errors"] + [error.code])),
                "stop_reason": "provider_unavailable",
            }
        return {
            **result,
            "query_keys": state["query_keys"] + [normalize(action["query"]).casefold()],
            "searched_queries": state["searched_queries"] + [action["query"]],
        }

    def chunks(self, source_id, text, task, excluded=(), focus=""):
        import math
        from collections import Counter

        def words(value):
            output = []
            for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_]+|[\u4e00-\u9fff]+", value.lower()):
                if re.search(r"[\u4e00-\u9fff]", token):
                    output.extend(token[i : i + 2] for i in range(len(token) - 1))
                else:
                    output.append(token[:-1] if token.endswith("s") and len(token) > 3 else token)
            return output

        query = " ".join(
            [
                task.question,
                focus,
                *task.seed_queries,
                *getattr(self.s, "understanding", {}).get("search_terms", []),
                *[c.description for c in task.success_criteria],
            ]
        )
        terms = set(words(query)) - {
            "the",
            "and",
            "of",
            "to",
            "in",
            "is",
            "are",
            "a",
            "an",
            "site",
            "http",
            "https",
            "org",
            "www",
            "com",
            "html",
            "doc",
            "documentation",
            "official",
        }
        size = self.s.config["fetch"]["chunk_chars"]
        overlap = self.s.config["fetch"]["chunk_overlap_chars"]
        chunks = []
        start = 0
        while start < len(text):
            end = min(start + size, len(text))
            if end < len(text):
                boundary = text.rfind("\n\n", start + overlap, end)
                if boundary >= 0:
                    end = boundary + 2
            chunks.append(
                {
                    "chunk_id": stable_id("chunk", source_id, start, end),
                    "start_char": start,
                    "text": text[start:end],
                }
            )
            if end == len(text):
                break
            start = end - overlap
        chunks = [c for c in chunks if c["chunk_id"] not in excluded]
        counts = [Counter(words(c["text"])) for c in chunks]
        frequency = {term: sum(term in count for count in counts) for term in terms}
        for chunk, count in zip(chunks, counts):
            chunk["score"] = sum(
                math.log1p(count[term]) * (1 + math.log((len(chunks) + 1) / (frequency[term] + 1)))
                for term in terms
                if count[term]
            )
        selected = sorted(chunks, key=lambda c: (-c["score"], c["start_char"]))[
            : self.s.config["fetch"]["chunks_per_page"]
        ]
        return [
            {k: v for k, v in c.items() if k != "score"}
            for c in sorted(selected, key=lambda c: c["start_char"])
        ]

    def temporal_ok(self, page):
        r = self.s.request
        historical = self.s.understanding["freshness"] == "historical" or r.time_range is not None
        modified = page.metadata.get("modified_at")
        modified_at = __import__("datetime").datetime.fromisoformat(modified) if modified else None
        if modified_at and modified_at > r.as_of:
            return False
        if historical and page.published_at is None and modified_at is None:
            return False
        if page.published_at and page.published_at > r.as_of:
            return False
        if r.time_range and page.published_at:
            if r.time_range.start and page.published_at < r.time_range.start:
                return False
            if r.time_range.end and page.published_at > r.time_range.end:
                return False
        return True

    async def extract(self, task, source_id, page, excluded=(), primary_domains=(), focus=""):
        from ..services.packets import packets
        from ..services.spans import locate_quote
        from ..services.table_context import table_header

        chunks = self.chunks(source_id, page.text, task, excluded, focus)
        prior = await self.s.evidence.bundle(task.task_id) if focus else None
        aliases = {f"C{i}": c.criterion_id for i, c in enumerate(task.success_criteria)}
        reverse = {v: k for k, v in aliases.items()}
        criteria = [{**c.model_dump(), "criterion_id": f"C{i}"} for i, c in enumerate(task.success_criteria)]
        candidates, processed, deferred = [], [], set()
        from ..services.provenance import identity_context, resolve_identity

        identity = await identity_context(
            self.s.evidence, source_id, page, self.s.config["fetch"]["chunk_chars"]
        )

        def extraction_payload(group):
            payload = {
                "source_id": "S0",
                "url": page.canonical_url,
                "title": page.title[:200],
                "published_at": page.published_at.isoformat() if page.published_at else None,
                "criteria": criteria,
                "chunks": [
                    {
                        "chunk_id": f"K{i}",
                        "start_char": 0,
                        "text": c["text"],
                        "table_header": table_header(
                            page.text, c["start_char"], c["start_char"] + len(c["text"])
                        ),
                    }
                    for i, c in enumerate(group)
                ],
            }
            if focus:
                payload.update(
                    reading_focus=focus, already_extracted=[c["canonical_text"] for c in prior["claims"]]
                )
            return payload

        for group in packets(chunks, extraction_payload, self.s.llm, "extract", Extraction):
            token = digest([c["chunk_id"] for c in group])[:16]

            def validate(value, group=group):
                for c in value.candidates:
                    validate_ids([c.source_id], ["S0"])
                    validate_ids([c.chunk_id], [f"K{i}" for i in range(len(group))])
                    validate_ids(c.criterion_ids, aliases)

            try:
                result = await self.s.llm.generate(
                    f"extract:task={task.task_id}:source={source_id}:packet={token}:v3",
                    "extract",
                    extraction_payload(group),
                    Extraction,
                    task_id=task.task_id,
                    validator=validate,
                )
                processed.extend(c["chunk_id"] for c in group)
            except (ToolFailure, Stopped) as error:
                if getattr(error, "fatal", False) or error.code == "cancelled":
                    raise
                await self.s.warn("evidence.packet_deferred", source_id=source_id, reason=error.code)
                if error.code in {"deadline", "budget_exhausted"}:
                    raise
                continue
            for c in result.candidates:
                chunk = group[int(c.chunk_id[1:])]
                span = locate_quote(chunk["text"], c.quote, c.start_char, c.end_char)
                if not span:
                    await self.s.warn("evidence.rejected", reason="quote_span", task_id=task.task_id)
                    continue
                start, end, quote, repaired = span
                if repaired:
                    await self.s.warn("evidence.span_repaired", task_id=task.task_id, source_id=source_id)
                candidates.append(
                    c.model_copy(
                        update={
                            "source_id": source_id,
                            "chunk_id": chunk["chunk_id"],
                            "criterion_ids": [aliases[cid] for cid in c.criterion_ids],
                            "quote": quote,
                            "start_char": chunk["start_char"] + start,
                            "end_char": chunk["start_char"] + end,
                        }
                    )
                )

        def verifier_payload(group):
            return {
                "source": {
                    "source_id": "S0",
                    "url": page.canonical_url,
                    "title": page.title[:200],
                    "published_at": page.published_at.isoformat() if page.published_at else None,
                },
                "criteria": criteria,
                "observed_primary_domains": list(primary_domains),
                "source_identity_context": identity,
                "candidates": [
                    {
                        "candidate_index": i,
                        "identity_context_id": f"V{i}",
                        "quote": c.quote,
                        "claim_text": c.claim_text,
                        "relation": c.relation,
                        "directness": c.directness,
                        "criterion_ids": [reverse[cid] for cid in c.criterion_ids],
                        "context_before": page.text[max(0, c.start_char - 160) : c.start_char],
                        "context_after": page.text[c.end_char : c.end_char + 160],
                        "table_header": table_header(page.text, c.start_char, c.end_char),
                    }
                    for i, c in enumerate(group)
                ],
            }

        # Five is the existing candidate/verdict schema cap, not an information drop.
        for offset in range(0, len(candidates), 5):
            for group in packets(
                candidates[offset : offset + 5],
                verifier_payload,
                self.s.llm,
                "evidence_verifier",
                EvidenceVerification,
            ):

                def validate_verdict(value, group=group):
                    if {v.candidate_index for v in value.verdicts} != set(range(len(group))) or len(
                        value.verdicts
                    ) != len(group):
                        raise ValueError("one verdict per candidate required")
                    for v in value.verdicts:
                        validate_ids(v.criterion_ids, aliases)
                    # Identity is a quoted structured field, just like an
                    # evidence span. Give the existing correction attempt the
                    # invalid field instead of silently losing primary status.
                    contexts = [
                        {
                            "context_id": f"V{i}",
                            "start_char": max(0, c.start_char - 160),
                            "text": page.text[max(0, c.start_char - 160) : c.end_char + 160],
                        }
                        for i, c in enumerate(group)
                    ]
                    if value.source_identity and not resolve_identity(
                        value.source_identity,
                        {**identity, "contexts": identity["contexts"] + contexts},
                        source_id,
                        page,
                    ):
                        raise ValueError(
                            "source_identity must use an exact contiguous quote from its context_id "
                            "or an offered official-link ID. Do not join distant fragments with ellipses. "
                            "Return null if no supplied passage establishes the publisher relationship."
                        )

                token = digest([c.model_dump(mode="json") for c in group])[:16]
                try:
                    verified = await self.s.llm.generate(
                        f"verify:task={task.task_id}:source={source_id}:packet={token}:v4",
                        "evidence_verifier",
                        verifier_payload(group),
                        EvidenceVerification,
                        task_id=task.task_id,
                        validator=validate_verdict,
                    )
                    from urllib.parse import urlsplit

                    from ..adapters.fetch import publisher

                    secondary = (urlsplit(page.canonical_url).hostname or "").endswith(
                        "wikipedia.org"
                    ) and not re.search(r"维基|wikipedia", self.s.request.query, re.IGNORECASE)
                    outside = (
                        primary_domains
                        and publisher(page.canonical_url) not in primary_domains
                        and all(c.kind == "authoritative_fact" for c in task.success_criteria)
                    )
                    if secondary or outside:
                        verified = verified.model_copy(
                            update={
                                "verdicts": [
                                    v.model_copy(update={"primary_for_claim": False})
                                    for v in verified.verdicts
                                ]
                            }
                        )
                    candidate_contexts = [
                        {
                            "context_id": f"V{i}",
                            "start_char": max(0, c.start_char - 160),
                            "text": page.text[max(0, c.start_char - 160) : c.end_char + 160],
                        }
                        for i, c in enumerate(group)
                    ]
                    proof = resolve_identity(
                        verified.source_identity,
                        {**identity, "contexts": identity["contexts"] + candidate_contexts},
                        source_id,
                        page,
                    )
                    if not proof and any(v.primary_for_claim for v in verified.verdicts):
                        await self.s.warn("source.identity_unverified", source_id=source_id)
                    await self.s.evidence.persist(
                        task,
                        [
                            (
                                group[v.candidate_index],
                                v.model_copy(
                                    update={"criterion_ids": [aliases[cid] for cid in v.criterion_ids]}
                                ),
                            )
                            for v in verified.verdicts
                        ],
                        primary_provenance=proof,
                        identity_claim=verified.source_identity.model_dump()
                        if verified.source_identity
                        else None,
                    )
                except (ToolFailure, Stopped) as error:
                    if getattr(error, "fatal", False) or error.code == "cancelled":
                        raise
                    deferred.update(c.chunk_id for c in group)
                    await self.s.warn("evidence.packet_deferred", source_id=source_id, reason=error.code)
                    if error.code in {"deadline", "budget_exhausted"}:
                        raise
        return [cid for cid in processed if cid not in deferred]

    @staticmethod
    def stored_page(source):
        return FetchResult.model_validate_json(
            canonical(
                {
                    "requested_url": source["canonical_url"],
                    "canonical_url": source["canonical_url"],
                    "title": source["title"],
                    "media_type": source["media_type"],
                    "text": source["content_text"],
                    "content_sha256": source["content_sha256"],
                    "published_at": source["published_at"],
                    "retrieved_at": source["retrieved_at"],
                    "publisher_key": source["publisher_key"],
                    "source_type": source["source_type"],
                    "metadata": source["metadata"],
                }
            )
        )

    async def select_read(self, state):
        task = self.task(state)
        bundle = await self.s.evidence.bundle()
        evidence = {e["id"]: e for e in bundle["evidence"]}
        core_ids = {c.criterion_id for c in task.success_criteria}
        shared_primary_sources = {
            evidence[r["evidence_id"]]["source_id"]
            for r in bundle["relations"]
            if r["verification"].get("primary_for_claim")
        }
        primary_sources = {
            evidence[r["evidence_id"]]["source_id"]
            for r in bundle["relations"]
            if r["verification"].get("primary_for_claim")
            and core_ids.intersection(r["verification"].get("criterion_ids", []))
        }
        # Another task may already have found the original source. Reuse its
        # frozen page and independently extract/map evidence for THIS task;
        # sharing a source never copies another task's coverage verdict.
        pending_shared = []
        for source in bundle["sources"]:
            if source["id"] not in shared_primary_sources or source["id"] in state["visited_source_ids"]:
                continue
            page = self.stored_page(await self.s.evidence.get_source(source["id"]))
            if self.temporal_ok(page) and self.chunks(source["id"], page.text, task):
                pending_shared.append(source["id"])
        if pending_shared:
            from ..adapters.fetch import publisher

            return {
                "pending_source_ids": pending_shared[: self.s.config["research"]["pages_per_round"]],
                "primary_domains": sorted(
                    {
                        publisher(s["canonical_url"])
                        for s in bundle["sources"]
                        if s["id"] in shared_primary_sources
                    }
                ),
                "direct_read": False,
            }
        ids = list(
            dict.fromkeys(
                [s["source_id"] for s in state.get("discovery", {}).get("sources", [])]
                + state["visited_source_ids"]
            )
        )
        from ..adapters.fetch import publisher

        references = [
            link
            for source in state.get("discovery", {}).get("sources", [])
            for link in source.get("reference_links", [])
        ]
        for source in bundle["sources"]:
            if source["id"] in state["visited_source_ids"]:
                references.extend(source.get("metadata", {}).get("reference_links", []))
        domains = sorted(
            {publisher(link["url"]) for link in references if link.get("official_link")}
            | {publisher(s["canonical_url"]) for s in bundle["sources"] if s["id"] in primary_sources}
        )
        # Following links on a planner-selected site is navigation, not proof
        # that the site is primary. Keep candidate domains out of qualification.
        follow_domains = set(domains) | {publisher(url) for url in task.source_urls}
        visited_urls = {
            url
            for s in bundle["sources"]
            if s["id"] in state["visited_source_ids"]
            for url in [s["canonical_url"], s.get("metadata", {}).get("requested_url")]
            if url
        }
        # source_urls are allowlisted against observed links/user URLs by the
        # planner validator; source_targets are categories, never URLs.
        # Follow the newly opened, explicitly selected source before walking
        # every old navigation link on a discovery summary. This is navigation
        # only; an observed publisher lead is still not evidence of authority.
        targets = [
            link["url"]
            for source in sorted(
                bundle["sources"],
                key=lambda s: (
                    s["id"] not in primary_sources,
                    next(
                        (
                            i
                            for i, url in enumerate(task.source_urls)
                            if url in {s["canonical_url"], s.get("metadata", {}).get("requested_url")}
                        ),
                        len(task.source_urls),
                    ),
                ),
            )
            if source["id"] in state["visited_source_ids"]
            and (
                source["id"] in primary_sources
                or source["canonical_url"] in task.source_urls
                or source.get("metadata", {}).get("requested_url") in task.source_urls
            )
            for link in source.get("metadata", {}).get("reference_links", [])
            if publisher(link["url"]) in follow_domains
        ]
        targets += list(task.source_urls)
        targets += [
            link["url"]
            for link in references
            if link.get("official_link") or publisher(link["url"]) in follow_domains
        ]
        targets = list(dict.fromkeys(url for url in targets if url not in visited_urls))
        if targets:
            return {
                "pending_source_ids": [],
                "direct_read": True,
                "primary_domains": domains,
                "pending_hits": [
                    {"url": url, "title": "Observed publisher link", "snippet": "", "rank": i + 1}
                    for i, url in enumerate(targets)
                ],
            }
        pending = []
        for sid in ids:
            if (
                state["round"]
                and self.s.understanding["source_policy"] == "primary_required"
                and sid not in primary_sources
            ):
                continue
            page = self.stored_page(await self.s.evidence.get_source(sid))
            if self.temporal_ok(page) and self.chunks(
                sid, page.text, task, state["read_chunks"].get(sid, [])
            ):
                pending.append(sid)
                if len(pending) >= self.s.config["research"]["pages_per_round"]:
                    break
        return {"pending_source_ids": pending, "primary_domains": domains, "direct_read": False}

    async def read_existing(self, state):
        task = self.task(state)
        read = {k: list(v) for k, v in state["read_chunks"].items()}
        visited = set(state["visited_source_ids"])
        for sid in state["pending_source_ids"]:
            try:
                page = self.stored_page(await self.s.evidence.get_source(sid))
                done = await self.extract(
                    task, sid, page, read.get(sid, []), state.get("primary_domains", [])
                )
                read[sid] = sorted(set(read.get(sid, []) + done))
                visited.add(sid)
            except Stopped as error:
                return {"stop_reason": error.code, "read_chunks": read, "visited_source_ids": sorted(visited)}
            except ToolFailure as error:
                if error.fatal or not self.s.config.get("adaptive_research_workflow_version"):
                    raise
                visited.add(sid)
                await self.s.db.commit_value(
                    self.s.run_id,
                    self.s.fence,
                    "source-read-failure:" + sid,
                    "source_read_failure",
                    {"source_id": sid, "reason": error.code},
                    task.task_id,
                )
                await self.s.warn("reading.source_unavailable", source_id=sid, reason=error.code)
        return {"read_chunks": read, "visited_source_ids": sorted(visited)}

    async def fetch_extract(self, state):
        if state.get("stop_reason"):
            return {}
        task = self.task(state)
        visited = set(state["visited_source_ids"])
        errors = list(state["search_errors"])
        read = {k: list(v) for k, v in state["read_chunks"].items()}
        bundle = await self.s.evidence.bundle()
        visited_urls = {
            url
            for source in bundle["sources"]
            if source["id"] in visited
            for url in [source["canonical_url"], source.get("metadata", {}).get("requested_url")]
            if url
        }
        candidates = [h for h in state["pending_hits"] if h["url"] not in visited_urls]
        for hit in candidates[: self.s.config["research"]["pages_per_round"]]:
            sid = None
            try:
                await self.s.ops.guard()
                page = await self.s.fetch.fetch(hit["url"], task.task_id)
                if self.s.config.get("adaptive_research_workflow_version"):
                    from ..services.adaptive_research import readable_full_text

                    await self.s.evidence.source(page)
                    page = await readable_full_text(self.s, page, task.task_id)
                sid = await self.s.evidence.source(page)
                if sid in visited:
                    continue
                visited.add(sid)
                if not self.temporal_ok(page):
                    await self.s.warn("source_temporal_unknown", source_id=sid)
                    continue
                if page.metadata.get("truncated"):
                    await self.s.warn("source_truncated", source_id=sid)
                read[sid] = await self.extract(
                    task, sid, page, primary_domains=state.get("primary_domains", [])
                )
            except Stopped as error:
                if error.code == "input_budget_exceeded":
                    await self.s.warn("extraction_input_too_large", task_id=task.task_id)
                    continue
                return {
                    "stop_reason": error.code,
                    "visited_source_ids": sorted(visited),
                    "search_errors": errors,
                    "read_chunks": read,
                }
            except ToolFailure as error:
                if error.fatal:
                    raise
                if sid and self.s.config.get("adaptive_research_workflow_version"):
                    await self.s.db.commit_value(
                        self.s.run_id,
                        self.s.fence,
                        "source-read-failure:" + sid,
                        "source_read_failure",
                        {"source_id": sid, "reason": error.code},
                        task.task_id,
                    )
                errors = sorted(set(errors + [error.code]))
                await self.s.warn("fetch.failed", reason=error.code, task_id=task.task_id)
        return {"visited_source_ids": sorted(visited), "search_errors": errors, "read_chunks": read}

    async def assess(self, state):
        task = self.task(state)
        # A committed round can be replayed even when its checkpoint was not persisted.
        key = f"round:batch={state['batch_no']}:task={task.task_id}:round={state['round'] + 1}"
        cached = await self.s.db.saved_value(self.s.run_id, key)
        if cached is not None:
            return cached
        bundle = await self.s.evidence.bundle(task.task_id)
        results = await self.s.evidence.assessments(
            task.success_criteria, self.s.understanding["source_policy"], state["batch_no"]
        )
        ids = sorted(e["id"] for e in bundle["evidence"])
        old = {r["criterion_id"]: r["status"] for r in state["criterion_results"]}
        progress = bool(set(ids) - set(state["evidence_ids"])) or any(
            r.status == "covered" and old.get(r.criterion_id) != "covered" for r in results
        )
        patch = {
            "round": state["round"] + 1,
            "no_progress_count": 0 if progress else state["no_progress_count"] + 1,
            "evidence_ids": ids,
            "claim_ids": sorted(c["id"] for c in bundle["claims"]),
            "criterion_results": [r.model_dump() for r in results],
        }
        await self.s.db.commit_value(self.s.run_id, self.s.fence, key, "round", patch, task.task_id)
        return patch

    async def finish(self, state):
        task = self.task(state)
        key = f"task:batch={state['batch_no']}:task={task.task_id}"
        previous = await self.s.db.saved_value(self.s.run_id, key)
        if previous is None:
            reason = state["stop_reason"] or "max_rounds"
            bundle = await self.s.evidence.bundle(task.task_id)
            ids = sorted(e["id"] for e in bundle["evidence"])
            status = (
                "cancelled"
                if reason == "cancelled"
                else "sufficient"
                if reason == "sufficient"
                else "partial"
                if ids
                else "failed"
                if reason == "provider_unavailable"
                else "empty"
            )
            result = ResearchResult(
                task_id=task.task_id,
                batch_no=state["batch_no"],
                rounds=state["round"],
                status=status,
                stop_reason=reason,
                claim_ids=sorted(c["id"] for c in bundle["claims"]),
                evidence_ids=ids,
                criterion_results=state["criterion_results"],
                unresolved=[r["explanation"] for r in state["criterion_results"] if r["status"] != "covered"],
                error_ids=state["search_errors"],
            )
            previous = await self.s.db.commit_value(
                self.s.run_id, self.s.fence, key, "task", result.model_dump(mode="json"), task.task_id
            )
        return {"task_results": {f"task:{state['batch_no']}:{task.task_id}": previous}, "warnings": {}}

    def compile(self):
        g = StateGraph(TaskState, input_schema=TaskInput, output_schema=TaskOutput)
        for name in [
            "guard",
            "select_read",
            "read_existing",
            "reason",
            "search",
            "fetch_extract",
            "assess",
            "finish",
        ]:
            g.add_node(name, getattr(self, name))
        g.add_edge(START, "guard")
        for name in ["guard", "reason"]:
            g.add_conditional_edges(
                name,
                lambda s: "finish" if s.get("stop_reason") else "next",
                {"finish": "finish", "next": "select_read" if name == "guard" else "search"},
            )
        g.add_conditional_edges(
            "select_read",
            lambda s: (
                "fetch_extract"
                if s.get("direct_read")
                else "read_existing"
                if s.get("pending_source_ids")
                else "reason"
            ),
            ["fetch_extract", "read_existing", "reason"],
        )
        g.add_edge("read_existing", "assess")
        g.add_edge("search", "fetch_extract")
        g.add_edge("fetch_extract", "assess")
        g.add_edge("assess", "guard")
        g.add_edge("finish", END)
        return g.compile(checkpointer=None)
