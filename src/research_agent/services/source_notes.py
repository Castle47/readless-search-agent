"""Immutable reader notes cached by source snapshot and complete research brief."""

import asyncio

from ..contracts import Criterion, CriterionAssessment, digest, validate_ids
from ..source_contracts import SourceReading
from ..storage.db import all_rows
from ..storage.evidence import independent_origins
from ..errors import Stopped
from .packets import packets
from .provenance import identity_context, resolve_identity

# Reserve space for the existing reader validation feedback, not another call.
IDENTITY_FEEDBACK = "Identity must cite one exact contiguous supplied identity passage or an offered official link; otherwise return null"


class SourceNotes:
    def __init__(self, services):
        self.s = services
        if not hasattr(services, "source_read_locks"):
            services.source_read_locks = {}

    async def criteria(self):
        async with self.s.db.pool.connection() as conn:
            rows = await all_rows(
                conn, "SELECT payload FROM research.plans WHERE run_id=%s AND batch_no=0", (self.s.run_id,)
            )
        self.planned_queries = sorted({q for t in rows[0]["payload"]["tasks"] for q in t["seed_queries"]})
        return {
            c["criterion_id"]: Criterion.model_validate(c)
            for t in rows[0]["payload"]["tasks"]
            for c in t["success_criteria"]
        }

    async def index(self):
        await self.s.db.get_run(self.s.run_id)
        async with self.s.db.pool.connection() as conn:
            rows = await all_rows(
                conn,
                "SELECT result FROM research.operations WHERE run_id=%s AND kind='source_note' AND status='succeeded' ORDER BY operation_key",
                (self.s.run_id,),
            )
        return [r["result"] for r in rows]

    async def read(self, source_id, page, task):
        from ..graph.researcher import Researcher
        from .table_context import table_header
        from .reading_passages import reading_passages, coalesce_passages
        from .publication_identity import established_identity, identity_summary

        criteria = await self.criteria()
        brief = {
            "question": self.s.understanding.get("standalone_query", self.s.request.query),
            "reader_goal": self.s.understanding.get("reader_goal"),
            "criteria": [c.model_dump() for c in criteria.values()],
            "search_terms": sorted(set(self.planned_queries + self.s.understanding.get("search_terms", []))),
        }
        cache = digest([source_id, page.content_sha256, brief])
        lock = self.s.source_read_locks.setdefault(cache, asyncio.Lock())
        async with lock:
            saved = await self.s.db.saved_value(self.s.run_id, "source-read:" + cache)
            if saved is not None:
                return saved["chunk_ids"]
            qa = {f"Q{i}": cid for i, cid in enumerate(criteria)}
            proof = await established_identity(self.s, source_id)
            full_identity = await identity_context(
                self.s.evidence, source_id, page, self.s.config["fetch"]["chunk_chars"]
            )

            def identity_for(known):
                if not known:
                    return {"contexts": [], "official_links": []}
                contexts = []
                if known["support_source_id"] == source_id and known.get("quote"):
                    contexts = [
                        {"context_id": "I0", "start_char": known["start_char"], "text": known["quote"]}
                    ]
                return {"contexts": contexts, "official_links": [], "established": identity_summary(known)}

            def payload(group, known, leads):
                return {
                    "question": brief["question"],
                    "reader_goal": brief["reader_goal"],
                    "source": {"url": page.canonical_url, "title": page.title},
                    "source_identity_context": {
                        **identity_for(known),
                        "passage_context_ids": [f"P{i}" for i in range(len(group))],
                    },
                    "established_source_identity": identity_summary(known) if known else None,
                    "related_source_leads": leads,
                    "criteria": [
                        {"criterion_id": q, "description": criteria[c].description} for q, c in qa.items()
                    ],
                    "passages": [
                        {
                            "passage_id": f"P{i}",
                            "text": c["text"],
                            "table_header": table_header(
                                page.text, c["start_char"], c["start_char"] + len(c["text"])
                            ),
                        }
                        for i, c in enumerate(group)
                    ],
                }

            plan_key = "source-reading-plan:" + cache
            plan = await self.s.db.saved_value(self.s.run_id, plan_key)
            if plan is None:
                broad_task = task.model_copy(
                    update={
                        "question": brief["question"],
                        "success_criteria": list(criteria.values()),
                        "seed_queries": brief["search_terms"] or [brief["question"]],
                    }
                )
                windows = (
                    [{"start_char": 0, "text": page.text}]
                    if self.s.config.get("adaptive_research_workflow_version")
                    else Researcher(self.s).chunks(source_id, page.text, broad_task)
                )
                chunks = reading_passages(
                    source_id, page.text, windows, self.s.config["fetch"]["chunk_chars"]
                )
                if self.s.config.get("adaptive_research_workflow_version"):
                    chunks = coalesce_passages(
                        source_id, page.text, chunks, self.s.config["fetch"]["chunk_chars"]
                    )
                # Form packets with CURRENT original text first. Context may use
                # spare space, but can never cause another paid reading packet.
                groups = await asyncio.to_thread(
                    lambda: list(
                        packets(
                            chunks,
                            lambda g: payload(g, proof, []),
                            self.s.llm,
                            "source_reader",
                            SourceReading,
                            correction=IDENTITY_FEEDBACK,
                        )
                    )
                )
                leads, seen = [], {source_id}
                for note in await self.index():
                    if note["source_id"] in seen or not note["passages"]:
                        continue
                    if not set(note["criterion_ids"]).intersection(criteria):
                        continue
                    source = await self.s.evidence.get_source(note["source_id"])
                    if source["content_sha256"] != note["source_hash"]:
                        raise ValueError("Related source note differs from immutable source")
                    # One exact, attributed bridge per source, never an entire
                    # prior article copied into every packet. The rest stays stored.
                    passage = note["passages"][0]
                    if source["content_text"][passage["start_char"] : passage["end_char"]] != passage["text"]:
                        raise ValueError("Related passage differs from immutable source")
                    leads.append(
                        {
                            "source_id": note["source_id"],
                            "url": source["canonical_url"],
                            "title": source["title"],
                            "source_hash": note["source_hash"],
                            "verified_facts": False,
                            "passages": [
                                {k: passage[k] for k in ("text", "start_char", "end_char", "table_header")}
                            ],
                        }
                    )
                    seen.add(note["source_id"])
                plan = {"groups": groups, "leads": leads, "initial_identity": full_identity}
                await self.s.db.commit_value(
                    self.s.run_id, self.s.fence, plan_key, "source_reading_plan", plan
                )
            full_identity = plan["initial_identity"]
            processed = []
            for group in plan["groups"]:
                group_key = digest([c["chunk_id"] for c in group])
                input_key = "source-reader-input:" + cache + ":" + group_key
                packed = await self.s.db.saved_value(self.s.run_id, input_key)
                if packed is None:
                    current = payload(group, proof, [])
                    # Extra header/footer identity context is optional too: it
                    # must not split the current article into tiny paid packets.
                    if not proof:
                        for field in ["contexts", "official_links"]:
                            for item in full_identity[field]:
                                current["source_identity_context"][field].append(item)
                                try:
                                    self.s.llm.build(
                                        "source_reader", current, SourceReading, correction=IDENTITY_FEEDBACK
                                    )
                                except Stopped as error:
                                    if error.code != "input_budget_exceeded":
                                        raise
                                    current["source_identity_context"][field].pop()
                    for lead in plan["leads"]:
                        current["related_source_leads"].append(lead)
                        try:
                            self.s.llm.build(
                                "source_reader", current, SourceReading, correction=IDENTITY_FEEDBACK
                            )
                        except Stopped as error:
                            if error.code != "input_budget_exceeded":
                                raise
                            current["related_source_leads"].pop()
                    packed = {"payload": current, "proof": proof}
                    await self.s.db.commit_value(
                        self.s.run_id, self.s.fence, input_key, "source_reader_input", packed
                    )
                offered_identity = {
                    **packed["payload"]["source_identity_context"],
                    "contexts": packed["payload"]["source_identity_context"]["contexts"]
                    + [
                        {"context_id": f"P{i}", "start_char": c["start_char"], "text": c["text"]}
                        for i, c in enumerate(group)
                    ],
                }
                packet_proof = packed["proof"]

                def validate(value):
                    validate_ids(value.passage_ids, [f"P{i}" for i in range(len(group))])
                    validate_ids(value.addressed_criterion_ids, qa)
                    validate_ids(value.firsthand_criterion_ids, value.addressed_criterion_ids)
                    if (
                        not self.s.config.get("adaptive_research_workflow_version")
                        and value.firsthand_criterion_ids
                        and not (value.source_identity or packet_proof)
                    ):
                        raise ValueError("First-hand material requires traceable source identity")
                    if value.addressed_criterion_ids and not value.passage_ids:
                        raise ValueError("A contribution needs original passages")
                    if value.material_role == "irrelevant" and (
                        value.passage_ids or value.addressed_criterion_ids
                    ):
                        raise ValueError("Irrelevant material cannot contribute to the answer")
                    if (
                        not self.s.config.get("adaptive_research_workflow_version")
                        and value.source_identity
                        and not packet_proof
                        and not resolve_identity(value.source_identity, offered_identity, source_id, page)
                    ):
                        raise ValueError(IDENTITY_FEEDBACK)

                value = await self.s.llm.generate(
                    "source-reader:" + cache + ":" + group_key,
                    "source_reader",
                    packed["payload"],
                    SourceReading,
                    task_id=task.task_id,
                    validator=validate,
                )
                proof = packet_proof or resolve_identity(
                    value.source_identity, offered_identity, source_id, page
                )
                if not proof and (value.firsthand_criterion_ids or value.source_identity):
                    # Unproven authority is withheld, not converted to a paid
                    # formatting retry or a failed ordinary reading packet.
                    value = value.model_copy(update={"firsthand_criterion_ids": []})
                    await self.s.warn("reading.identity_unverified", source_id=source_id)
                if proof and not packet_proof:
                    await self.s.db.commit_value(
                        self.s.run_id,
                        self.s.fence,
                        "source-identity:" + source_id,
                        "source_identity",
                        {"proof": proof},
                    )
                chosen = [group[int(pid[1:])] for pid in dict.fromkeys(value.passage_ids)]
                note = {
                    "source_id": source_id,
                    "source_hash": page.content_sha256,
                    "brief_hash": digest(brief),
                    "notes": value.notes,
                    "material_role": value.material_role,
                    "open_questions": value.open_questions,
                    "criterion_ids": [qa[q] for q in value.addressed_criterion_ids],
                    "firsthand_criterion_ids": [qa[q] for q in value.firsthand_criterion_ids],
                    "identity": proof,
                    "identity_claim": value.source_identity.model_dump()
                    if value.source_identity and not packet_proof
                    else None,
                    "passages": [
                        {
                            "passage_id": c["chunk_id"],
                            "source_id": source_id,
                            "start_char": c["start_char"],
                            "end_char": c["start_char"] + len(c["text"]),
                            "text": c["text"],
                            "table_header": table_header(
                                page.text, c["start_char"], c["start_char"] + len(c["text"])
                            ),
                        }
                        for c in chosen
                    ],
                }
                await self.s.db.commit_value(
                    self.s.run_id, self.s.fence, "source-note:" + cache + ":" + group_key, "source_note", note
                )
                processed.extend(c["chunk_id"] for c in group)
            result = {"chunk_ids": sorted(set(processed))}
            await self.s.db.commit_value(
                self.s.run_id, self.s.fence, "source-read:" + cache, "source_read", result
            )
            return result["chunk_ids"]

    async def readiness(self):
        from .adaptive_research import material_assessment

        return await material_assessment(self.s, list((await self.criteria()).values()))

    async def assessments(self, criteria):
        if self.s.config.get("adaptive_research_workflow_version"):
            from .adaptive_research import semantic_readiness

            # All parallel tasks share one meaning-based assessment of the
            # same workspace; do not pay again for each overlapping subset.
            all_criteria = await self.criteria()
            results = await semantic_readiness(self.s, list(all_criteria.values()))
            requested = {c.criterion_id for c in criteria}
            return [r for r in results if r.criterion_id in requested]
        notes = await self.index()
        # Readiness only. No evidence, claims, coverage or verified flags written.
        results = []
        for c in criteria:
            matches = [n for n in notes if c.criterion_id in n["criterion_ids"] and n["passages"]]
            primary = any(
                n["identity"] and c.criterion_id in n.get("firsthand_criterion_ids", []) for n in matches
            )
            rows = [await self.s.evidence.get_source(sid) for sid in {n["source_id"] for n in matches}]
            enough = (
                primary
                if c.kind == "authoritative_fact"
                else independent_origins(rows) >= 2
                and (self.s.understanding["source_policy"] != "primary_required" or primary)
            )
            results.append(
                CriterionAssessment(
                    criterion_id=c.criterion_id,
                    status="covered" if enough else "missing",
                    supporting_claim_ids=[],
                    contradicting_claim_ids=[],
                    explanation="研究材料可供写作；最终事实与覆盖尚待核验。"
                    if enough
                    else "尚缺适合回答该问题的来源材料。",
                )
            )
        return results
