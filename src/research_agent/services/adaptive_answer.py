"""Whole-answer synthesis with focused repair, never automatic paragraph deletion."""

from copy import deepcopy
import re

from ..adaptive_contracts import WholeAnswerReview, AnswerIssue
from ..contracts import (
    EvidenceCandidate,
    EvidenceVerdict,
    NarrativeAssertion,
    NarrativeDraft,
    NarrativeParagraph,
    NarrativeSection,
    QuestionCoverage,
    ResearchTask,
    digest,
    normalize,
    stable_id,
    validate_ids,
)
from ..errors import Stopped, ToolFailure, InvariantViolation
from ..source_contracts import SourceComposition, SourceDraft
from ..storage.db import J
from .source_workflow import SourceWorkflow, audit_spans, blocks, draft_of
from .source_notes import SourceNotes
from .retrieval import select_sources


def ordered_payload(workflow, materials):
    validate_material_bindings(materials)
    payload = workflow.payload(materials)
    payload.pop("source_policy", None)
    payload["evidence_policy"] = {
        "primary_required": "Authorship, publication dates, claimed original contributions, measurements, real API guarantees and specific historically attributed design choices need appropriate original evidence.",
        "ordinary_explanation": "Definitions, architecture categories and mathematical consequences can use traceable teaching/technical sources or a valid derivation from documented premises. Do not require inventor papers for every general comparison.",
        "scope": "Apply this distinction to the actual statement, not every sentence in an introduction merely because a reader question is labelled authoritative_fact.",
    }
    # Restore document context after fair capacity admission. The model sees
    # each source's original paragraphs in order, rather than interleaved cards.
    payload["passages"].sort(
        key=lambda p: (
            p["source_id"],
            materials["passages"][p["passage_id"]]["start_char"],
        )
    )
    return payload


def validate_material_bindings(materials):
    for passage in materials["passages"].values():
        source = materials["sources"].get(passage["source_alias"])
        if not source or passage["source_id"] != source["id"]:
            raise InvariantViolation("Source alias differs from passage provenance")


def extend_materials(old, new):
    """Add new reading without changing citation IDs already used in a draft."""
    result = deepcopy(old)
    sources = {s["id"]: alias for alias, s in result["sources"].items()}
    next_alias = max((int(alias[1:]) for alias in result["sources"]), default=-1) + 1
    source_map = {}
    for alias, source in new["sources"].items():
        mapped = sources.get(source["id"])
        if mapped is None:
            mapped = f"S{next_alias}"
            next_alias += 1
            sources[source["id"]] = mapped
        source_map[alias] = mapped
        result["sources"][mapped] = source

    def identity(p, material):
        return (material["sources"][p["source_alias"]]["id"], p["start_char"], p["end_char"], p["text"])

    known = {identity(p, old) for p in old["passages"].values()}
    index = max((int(k[1:]) for k in result["passages"]), default=-1) + 1
    for p in new["passages"].values():
        key = identity(p, new)
        if key not in known:
            result["passages"][f"P{index}"] = {**p, "source_alias": source_map[p["source_alias"]]}
            known.add(key)
            index += 1
    validate_material_bindings(result)
    return result


def claim_spans(text):
    """Storage-sized exact body spans, after the whole block was reviewed."""
    start = 0
    while start < len(text):
        end = min(len(text), start + 1000)
        if end < len(text):
            boundary = max(
                text.rfind("。", start, end), text.rfind("\n", start, end), text.rfind(". ", start, end)
            )
            if boundary > start:
                end = boundary + 1
        yield text[start:end]
        start = end


class AdaptiveAnswerWorkflow:
    def __init__(self, services, graph):
        self.s, self.graph = services, graph
        self.base = SourceWorkflow(services, graph)

    async def write(self, state):
        await self.s.db.phase(self.s.run_id, self.s.fence, "write")
        self.s.understanding = state["understanding"]
        materials = await self.base.material(state)
        if not materials["passages"]:
            return await self.base.fallback("本次尚未取得足以组织回答的资料。")
        try:
            value = await self.s.llm.generate(
                "write:reader-centred:v1",
                "writer",
                ordered_payload(self.base, materials),
                SourceComposition,
                partition="finalization",
                validator=lambda v: self.base.validate_composition(v, materials),
            )
            draft = draft_of(value).model_dump(mode="json")
            await self.s.db.commit_value(
                self.s.run_id, self.s.fence, "draft:initial:v1", "unverified_draft", draft
            )
            return {"source_draft": draft, "source_materials": materials, "fallback_report": False}
        except (Stopped, ToolFailure) as error:
            if error.code == "cancelled" or getattr(error, "fatal", False):
                raise
            result = await self.base.fallback("本次未完成回答写作，研究资料已保留。")
            return {**result, "stop_reason": error.code}

    def review_payload(self, state):
        materials = state["source_materials"]
        draft = SourceDraft.model_validate(state["source_draft"])
        offered, aliases = audit_spans(materials["passages"])
        payload = ordered_payload(self.base, materials)
        criteria = self.graph.all_criteria(state)
        qa = {f"Q{i}": cid for i, cid in enumerate(criteria)}
        payload["answer_criteria"] = [
            {
                "criterion_id": q,
                "question": criteria[cid].description,
                "required": criteria[cid].required,
                "kind": criteria[cid].kind,
            }
            for q, cid in qa.items()
        ]
        payload["passages"] = [
            {
                "passage_id": pid,
                "source_id": p["source_alias"],
                "text": p["text"],
                "table_header": p["table_header"],
            }
            for pid, p in sorted(
                offered.items(), key=lambda row: (row[1]["source_alias"], row[1]["start_char"])
            )
        ]
        payload["draft"] = {
            "title": draft.title,
            "sections": [
                {
                    "heading": sec.heading,
                    "paragraphs": [
                        {
                            **p.model_dump(),
                            "passage_ids": [alias for pid in p.passage_ids for alias in aliases[pid]],
                        }
                        for p in sec.paragraphs
                    ],
                }
                for sec in draft.sections
            ],
        }
        return payload, offered, qa

    def validate_review(self, value, state, offered, qa):
        ps = {p.paragraph_id: p for p in blocks(SourceDraft.model_validate(state["source_draft"]))}
        if len(value.blocks) != len(ps) or {p.paragraph_id for p in value.blocks} != set(ps):
            raise ValueError("Review each complete answer block exactly once")
        for p in value.blocks:
            validate_ids(p.passage_ids, offered)
        for fact in value.important_facts:
            validate_ids([fact.paragraph_id], ps)
            validate_ids(fact.passage_ids, offered)
            validate_ids(fact.primary_passage_ids, fact.passage_ids)
            if fact.statement not in ps[fact.paragraph_id].text:
                raise ValueError("Important fact must identify exact answer wording")
        if len(value.coverage) != len(qa) or {c.criterion_id for c in value.coverage} != set(qa):
            raise ValueError("Assess every offered reader question exactly once")
        for c in value.coverage:
            validate_ids(c.paragraph_ids, ps)
        for issue in value.issues:
            validate_ids(issue.paragraph_ids, ps)

    def reconcile_review(self, value, state, offered):
        """Turn semantic gaps into repair findings, not parsing retries.

        Original provider output stays immutable. This projection can only
        downgrade unsupported judgments; it never promotes a missing proof.
        """
        result = value.model_copy(deep=True)
        for fact in result.important_facts:
            qualified = [
                pid
                for pid in fact.primary_passage_ids
                if state["source_materials"]["sources"][offered[pid]["source_alias"]].get("identity")
            ]
            if fact.supported and not qualified:
                fact.supported = False
                result.issues.append(
                    AnswerIssue(
                        paragraph_ids=[fact.paragraph_id],
                        kind="fact",
                        problem="重要事实尚缺对应的一手依据：" + fact.statement,
                        suggestion="补充原始依据，或缩小/修正这句表述，保留有依据的上下文。",
                    )
                )
        bad = {f.paragraph_id for f in result.important_facts if not f.supported}
        bad |= {pid for issue in result.issues if issue.kind == "fact" for pid in issue.paragraph_ids}
        for block in result.blocks:
            if not block.passage_ids:
                bad.add(block.paragraph_id)
                result.issues.append(
                    AnswerIssue(
                        paragraph_ids=[block.paragraph_id],
                        kind="fact",
                        problem="正文缺少可追溯的依据。",
                        suggestion="关联实际支持的原文，或修正不受支持的具体表述。",
                    )
                )
            if block.paragraph_id in bad:
                block.supported = False
        accepted = {b.paragraph_id for b in result.blocks if b.supported}
        for item in result.coverage:
            if item.answered and (
                not item.paragraph_ids or not set(item.paragraph_ids) <= accepted or item.missing
            ):
                item.answered = False
                item.missing = item.missing or "相关正文仍有事实或依据待核实。"
        return result

    async def review(self, state, *, recheck=False):
        if state.get("fallback_report"):
            return {}
        await self.s.db.phase(self.s.run_id, self.s.fence, "validate_report")
        if not recheck:
            await self.s.ops.close_finalization_stage("writer")
        payload, offered, qa = self.review_payload(state)
        key = "answer-review:" + digest(payload) + (":recheck" if recheck else "")
        try:
            value = await self.s.llm.generate(
                key,
                "report_verifier",
                payload,
                WholeAnswerReview,
                partition="finalization",
                is_recheck=recheck,
                validator=lambda v: self.validate_review(v, state, offered, qa),
            )
            value = self.reconcile_review(value, state, offered)
            result = {"whole_answer_review": value.model_dump(mode="json"), "adaptive_review_key": key}
        except (Stopped, ToolFailure) as error:
            if error.code == "cancelled" or getattr(error, "fatal", False):
                raise
            result = {"whole_answer_review": {"execution_error": error.code}, "adaptive_review_key": key}
            if error.code in {"deadline", "budget_exhausted"}:
                result["stop_reason"] = error.code
        if not recheck:
            await self.s.ops.close_finalization_stage("report_verifier")
            # Quality is included in the whole-answer review; its unused
            # allocation remains available for a genuinely necessary repair.
            await self.s.ops.close_finalization_stage("quality")
        return result

    def accepted(self, review, state):
        criteria = self.graph.all_criteria(state)
        qa = list(criteria.values())
        return (
            not review.issues
            and all(p.supported for p in review.blocks)
            and all(f.supported for f in review.important_facts)
            and all(c.answered for c in review.coverage if qa[int(c.criterion_id[1:])].required)
        )

    async def supplement(self, state, queries):
        from .adaptive_research import readable_full_text
        from .documents import work_identity
        from ..graph.researcher import Researcher

        task = ResearchTask.model_validate(state["plan_history"][0]["tasks"][0])
        remaining = self.s.config["research"]["post_write_pages"]
        read_sources = []
        already_read = {n["source_id"] for n in await SourceNotes(self.s).index() if n.get("passages")}
        saved_sources = await self.s.evidence.source_index()
        saved_sources.sort(key=lambda source: not bool(work_identity(source["canonical_url"])["work_id"]))
        for source in saved_sources:
            if not remaining or source["id"] in already_read:
                continue
            try:
                stored = await self.s.evidence.get_source(source["id"])
                page = await readable_full_text(self.s, Researcher.stored_page(stored), task.task_id)
                sid = await self.s.evidence.source(page)
                await SourceNotes(self.s).read(sid, page, task)
                read_sources.append(sid)
                remaining -= 1
            except (Stopped, ToolFailure) as error:
                if error.code == "cancelled" or getattr(error, "fatal", False):
                    raise
                await self.s.warn("answer_supplement.saved_source_unavailable", reason=error.code)
        for query in queries[: self.s.config["research"]["post_write_searches"]]:
            if not remaining:
                break
            try:
                hits = await self.s.search.search(
                    query, "zh-CN" if re.search(r"[\u4e00-\u9fff]", query) else "en", task.task_id
                )
                selected = await select_sources(
                    self.s,
                    [h.model_dump(mode="json") for h in hits],
                    self.s.request.query,
                    query=query,
                    max_pages=remaining,
                )
                for hit in selected:
                    try:
                        page = await self.s.fetch.fetch(hit["url"], task_id=task.task_id)
                        await self.s.evidence.source(page)
                        page = await readable_full_text(self.s, page, task.task_id)
                        sid = await self.s.evidence.source(page)
                        await SourceNotes(self.s).read(sid, page, task)
                        read_sources.append(sid)
                        remaining -= 1
                    except (Stopped, ToolFailure) as error:
                        if error.code == "cancelled" or getattr(error, "fatal", False):
                            raise
                        await self.s.warn("answer_supplement.page_unavailable", reason=error.code)
            except (Stopped, ToolFailure) as error:
                if error.code == "cancelled" or getattr(error, "fatal", False):
                    raise
                await self.s.warn("answer_supplement.unavailable", reason=error.code)
        material = await self.base.material(state)
        return extend_materials(state["source_materials"], material), read_sources

    async def resolve(self, state):
        if state.get("fallback_report"):
            return {}
        self.s.understanding = state["understanding"]
        if state["whole_answer_review"].get("execution_error"):
            return await self.incomplete(state, "本次完整草稿已保存，但未完成核验。")
        review = WholeAnswerReview.model_validate(state["whole_answer_review"])
        if self.accepted(review, state):
            return await self.publishable(state, review, revised=False)
        await self.s.db.phase(self.s.run_id, self.s.fence, "quality")
        materials = state["source_materials"]
        extra = []
        if review.research_queries or any(not f.supported for f in review.important_facts):
            if not review.research_queries:
                review.research_queries = [f.statement for f in review.important_facts if not f.supported][
                    : self.s.config["research"]["post_write_searches"]
                ]
            materials, extra = await self.supplement(state, review.research_queries)
        payload = {
            **ordered_payload(self.base, materials),
            "draft": state["source_draft"],
            "feedback": review.model_dump(mode="json"),
        }
        try:
            revised = await self.s.llm.generate(
                "edit:whole-answer:" + digest(payload),
                "editor",
                payload,
                SourceComposition,
                partition="finalization",
                validator=lambda v: self.base.validate_composition(v, materials),
            )
            draft = draft_of(revised).model_dump(mode="json")
            await self.s.db.commit_value(
                self.s.run_id, self.s.fence, "draft:edited:v1", "unverified_draft", draft
            )
            updated = {**state, "source_draft": draft, "source_materials": materials}
            updated.update(await self.review(updated, recheck=True))
            updated["adaptive_revision"] = {"supplemental_source_ids": extra, "whole_answer_rechecked": True}
            if not updated["whole_answer_review"].get("execution_error"):
                final_review = WholeAnswerReview.model_validate(updated["whole_answer_review"])
                if self.accepted(final_review, updated):
                    return {
                        **{
                            k: updated[k]
                            for k in [
                                "source_draft",
                                "source_materials",
                                "whole_answer_review",
                                "adaptive_review_key",
                                "adaptive_revision",
                            ]
                        },
                        **await self.publishable(updated, final_review, revised=True),
                    }
            return {
                **{
                    k: updated[k]
                    for k in [
                        "source_draft",
                        "source_materials",
                        "whole_answer_review",
                        "adaptive_review_key",
                        "adaptive_revision",
                    ]
                },
                **await self.incomplete(updated, "完整草稿仍有待核实或补充之处，已保留全文供查看。"),
            }
        except (Stopped, ToolFailure) as error:
            if error.code == "cancelled" or getattr(error, "fatal", False):
                raise
            return {
                **await self.incomplete(state, "完整草稿已保留，本次未完成修订。"),
                "stop_reason": error.code,
            }

    async def incomplete(self, state, message):
        # Never silently splice accepted paragraphs into an incoherent answer.
        return {
            **await self.base.fallback(message),
            "quality_failed": True,
            **({"stop_reason": state["stop_reason"]} if state.get("stop_reason") else {}),
        }

    async def publishable(self, state, review, *, revised):
        """Persist only a complete, reviewed body; questions stay a separate judgment."""
        if not self.accepted(review, state):
            raise ValueError("Unaccepted whole answer cannot become a report")
        draft = SourceDraft.model_validate(state["source_draft"])
        _, offered, qa = self.review_payload(state)
        self.validate_review(review, state, offered, qa)
        body_review = {b.paragraph_id: b for b in review.blocks}
        tasks = [ResearchTask.model_validate(t) for t in state["plan_history"][0]["tasks"]]
        all_ids = list(self.graph.all_criteria(state))
        paragraphs = {}
        claim_ids = set()
        primary_by_claim = {}
        # Keep important fact checks separately queryable. Their authority is
        # never inferred from a paragraph's general explanatory support.
        for fact in review.important_facts:
            mapped = (
                sorted({qa[c.criterion_id] for c in review.coverage if fact.paragraph_id in c.paragraph_ids})
                or all_ids
            )
            cid = stable_id("claim", self.s.run_id, normalize(fact.statement), "")
            for pid in fact.passage_ids:
                passage = offered[pid]
                source = state["source_materials"]["sources"][passage["source_alias"]]
                primary = pid in fact.primary_passage_ids
                candidate = EvidenceCandidate(
                    source_id=source["id"],
                    chunk_id=pid,
                    quote=passage["text"],
                    start_char=passage["start_char"],
                    end_char=passage["end_char"],
                    claim_text=fact.statement,
                    relation="supports",
                    criterion_ids=mapped,
                    temporal_scope=None,
                    directness="direct",
                )
                for task in tasks:
                    ids = sorted(set(mapped) & {c.criterion_id for c in task.success_criteria})
                    if ids:
                        verdict = EvidenceVerdict(
                            candidate_index=0,
                            relation="supports",
                            direct=True,
                            primary_for_claim=primary,
                            criterion_ids=ids,
                            explanation=fact.explanation[:400],
                        )
                        await self.s.evidence.persist(
                            task,
                            [(candidate.model_copy(update={"criterion_ids": ids}), verdict)],
                            primary_provenance=source.get("identity"),
                            identity_claim=source.get("identity_claim"),
                        )
                if primary:
                    primary_by_claim.setdefault(cid, set()).add(pid)
            claim_ids.add(cid)
        for p in blocks(draft):
            assessed = body_review[p.paragraph_id]
            related = {qa[c.criterion_id] for c in review.coverage if p.paragraph_id in c.paragraph_ids}
            mapped = sorted(related) or all_ids
            assertions = []
            for index, anchor in enumerate(claim_spans(p.text)):
                if not anchor.strip():
                    continue
                cid = stable_id("claim", self.s.run_id, normalize(anchor), "")
                eids = set()
                for pid in assessed.passage_ids:
                    passage = offered[pid]
                    source = state["source_materials"]["sources"][passage["source_alias"]]
                    candidate = EvidenceCandidate(
                        source_id=source["id"],
                        chunk_id=pid,
                        quote=passage["text"],
                        start_char=passage["start_char"],
                        end_char=passage["end_char"],
                        claim_text=anchor,
                        relation="supports",
                        criterion_ids=mapped,
                        temporal_scope=None,
                        directness="direct",
                    )
                    for task in tasks:
                        ids = sorted(set(mapped) & {c.criterion_id for c in task.success_criteria})
                        if not ids:
                            continue
                        verdict = EvidenceVerdict(
                            candidate_index=0,
                            relation="supports",
                            direct=True,
                            primary_for_claim=pid in primary_by_claim.get(cid, set()),
                            criterion_ids=ids,
                            explanation="Whole-answer review confirmed this explanation and its original factual premises; not independent empirical corroboration.",
                        )
                        await self.s.evidence.persist(
                            task,
                            [(candidate.model_copy(update={"criterion_ids": ids}), verdict)],
                            primary_provenance=source.get("identity"),
                            identity_claim=source.get("identity_claim"),
                        )
                    eids.add(stable_id("evidence", source["id"], passage["start_char"], passage["end_char"]))
                claim_ids.add(cid)
                assertions.append(
                    NarrativeAssertion(
                        statement_id=f"{p.paragraph_id}:grounding:{index}",
                        text=anchor,
                        anchor=anchor,
                        kind=assessed.kind,
                        claim_ids=[cid],
                        evidence_ids=sorted(eids),
                    )
                )
            paragraphs[p.paragraph_id] = NarrativeParagraph(
                **p.model_dump(exclude={"passage_ids"}), assertions=assertions
            )
        receipt = {
            "review_operation": state["adaptive_review_key"],
            "draft_sha256": digest(state["source_draft"]),
            "review": review.model_dump(mode="json"),
            "claim_ids": sorted(claim_ids),
            "source_hashes": {
                s["id"]: s["content_sha256"] for s in state["source_materials"]["sources"].values()
            },
        }
        await self.s.db.commit_value(
            self.s.run_id, self.s.fence, "answer:accepted:v1", "whole_answer_acceptance", receipt
        )
        async with self.s.db.transaction(self.s.run_id, self.s.fence) as conn:
            for cid in claim_ids:
                await conn.execute(
                    "UPDATE research.claims SET status='supported',confidence='medium' WHERE run_id=%s AND id=%s",
                    (self.s.run_id, cid),
                )
                await conn.execute(
                    "UPDATE research.claim_evidence SET verification=verification || %s::jsonb WHERE run_id=%s AND claim_id=%s",
                    (
                        J(
                            {
                                "whole_answer_review": state["adaptive_review_key"],
                                "body_evidence_requirement": "reviewed_original_premises",
                            }
                        ),
                        self.s.run_id,
                        cid,
                    ),
                )
        covered = {qa[c.criterion_id] for c in review.coverage if c.answered}
        criteria = self.graph.all_criteria(state)
        coverage = []
        for facet in state["understanding"]["facets"]:
            relevant = {
                c.criterion_id
                for t in tasks
                for c in t.success_criteria
                if facet["facet_id"] in (c.facet_ids or t.facet_ids) and (not facet["required"] or c.required)
            }
            coverage.append(
                QuestionCoverage(
                    question=facet["question"],
                    required=facet["required"],
                    status="answered"
                    if relevant and relevant <= covered
                    else "partial"
                    if relevant & covered
                    else "unresolved",
                )
            )
        final = NarrativeDraft(
            schema_version=3,
            title=draft.title,
            sections=[
                NarrativeSection(
                    heading=sec.heading, paragraphs=[paragraphs[p.paragraph_id] for p in sec.paragraphs]
                )
                for sec in draft.sections
            ],
            limitations=[],
            unresolved_questions=[c.question for c in coverage if c.required and c.status != "answered"],
            quality_status="revised" if revised else "passed",
            coverage=coverage,
            paragraph_answers={
                p.paragraph_id: sorted(
                    qa[c.criterion_id]
                    for c in review.coverage
                    if c.answered and p.paragraph_id in c.paragraph_ids
                )
                for p in blocks(draft)
            },
        )
        required = {c.criterion_id for c in criteria.values() if c.required}
        return {
            "report_draft": final.model_dump(mode="json"),
            "report_partial": bool(final.unresolved_questions),
            "report_coverage": len(required & covered) / max(1, len(required)),
            "quality_failed": False,
            "fallback_report": False,
        }
