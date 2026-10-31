"""Sourced prose with independent paragraph entailment and bounded reader review.

Reference responsibilities: Open Deep Research findings/answer separation,
GPT Researcher synthesis, and STORM whole-article polish (ADR 006).
"""

from copy import deepcopy

from ..contracts import (
    BatchParagraphAudit,
    CompactBatchParagraphAudit,
    NarrativeAssertion,
    NarrativeDraft,
    NarrativeParagraph,
    ProseComposition,
    ProseRepair,
    QualityReview,
    QuestionCoverage,
    canonical,
    digest,
    validate_ids,
)
from ..errors import Stopped, ToolFailure
from .reports import digest_draft, validate_statement, writer_bundle


def paragraphs(draft):
    return [p for section in draft.sections for p in section.paragraphs]


def anchor_span(paragraph, assertion):
    anchor = assertion.anchor
    if paragraph.text.count(anchor) != 1:
        from .spans import locate_quote

        repaired = locate_quote(paragraph.text, anchor, 0, 0)
        if not repaired:
            raise ValueError("Assertion anchor must be a unique exact clause in its paragraph")
        start, end, exact, _ = repaired
        assertion.anchor = exact
        return start, end
    start = paragraph.text.index(anchor)
    return start, start + len(anchor)


def validate_composition(value, claim_ids, evidence_ids, *, repair_hints=False):
    pids, sids = set(), set()
    for section in value.sections:
        for p in section.paragraphs:
            if p.paragraph_id in pids:
                raise ValueError("duplicate paragraph ID")
            pids.add(p.paragraph_id)
            for a in p.assertions:
                if a.statement_id in sids:
                    raise ValueError("duplicate assertion ID")
                sids.add(a.statement_id)
                validate_ids(a.claim_ids, claim_ids)
                validate_ids(a.evidence_ids, evidence_ids)
                if not a.claim_ids or not a.evidence_ids:
                    raise ValueError("Every substantive assertion needs evidence")
                try:
                    anchor_span(p, a)
                except ValueError:
                    if not repair_hints:
                        raise
                    # Writer annotations are retrieval hints, not approved
                    # citations. Retain their evidence at paragraph scope when
                    # an anchor is misplaced; the independent auditor must
                    # still decompose and ground EVERY actual prose assertion.
                    a.anchor = p.text


class NarrativeWorkflow:
    def __init__(self, services, graph):
        self.s, self.graph = services, graph

    async def package(self, state):
        bundle = await self.s.evidence.bundle()
        claims = writer_bundle(bundle)
        # Prefer useful coverage and primary evidence, never UUID/list-tail order.
        claims.sort(
            key=lambda c: (
                c["status"] != "supported",
                -len(c["criterion_ids"]),
                not any(e["primary"] for e in c["evidence"]),
                c["text"],
            )
        )
        source_cache = {}
        for c in claims:
            for e in c["evidence"]:
                sid = e["source_id"]
                if sid not in source_cache:
                    source_cache[sid] = await self.s.evidence.get_source(sid)
                source = source_cache[sid]
                original = next(x for x in bundle["evidence"] if x["id"] == e["evidence_id"])
                e["reading_context"] = source["content_text"][
                    max(0, original["start_char"] - 200) : original["end_char"] + 200
                ]
                # Retain the complete source paragraph, within the already
                # approved reading-chunk size. Shared passages are deduplicated
                # in the writer packet, not repeated for every extracted fact.
                text = source["content_text"]
                start = text.rfind("\n\n", 0, original["start_char"]) + 2
                start = max(0, start if start >= 2 else 0)
                end = text.find("\n\n", original["end_char"])
                end = len(text) if end < 0 else end
                if end - start <= self.s.config["fetch"]["chunk_chars"]:
                    e["reading_passage"] = {"start_char": start, "end_char": end, "text": text[start:end]}
                from .table_context import table_header

                e["table_header"] = table_header(
                    source["content_text"], original["start_char"], original["end_char"]
                )
                e["section"] = next(
                    (
                        s["title"]
                        for s in reversed(source["metadata"].get("sections", []))
                        if s["start_char"] <= original["start_char"] < s["end_char"]
                    ),
                    "",
                )
        claim_map = {f"C{i}": c["claim_id"] for i, c in enumerate(claims)}
        eids = list(dict.fromkeys(e["evidence_id"] for c in claims for e in c["evidence"]))
        evidence_map = {f"E{i}": eid for i, eid in enumerate(eids)}
        rc, re = {v: k for k, v in claim_map.items()}, {v: k for k, v in evidence_map.items()}
        short = deepcopy(claims)
        for c in short:
            c["claim_id"] = rc[c["claim_id"]]
            for e in c["evidence"]:
                e["evidence_id"] = re[e["evidence_id"]]
        return bundle, short, claim_map, evidence_map

    @staticmethod
    def map_paragraph(p, cm, em):
        return p.model_copy(
            update={
                "assertions": [
                    a.model_copy(
                        update={
                            "statement_id": f"{p.paragraph_id}:a{index}",
                            "claim_ids": [cm[x] for x in a.claim_ids],
                            "evidence_ids": [em[x] for x in a.evidence_ids],
                        }
                    )
                    for index, a in enumerate(p.assertions)
                ]
            }
        )

    async def write(self, state):
        from .composition import pack_materials, prose_draft, validate_prose

        await self.s.db.phase(self.s.run_id, self.s.fence, "write")
        bundle, claims, cm, em = await self.package(state)
        if not claims:
            return {
                "report_draft": digest_draft(
                    self.s.request.query,
                    bundle,
                    ["本次取得的资料尚不足以形成可靠结论。"],
                    self.s.request.output_language,
                ).model_dump(mode="json"),
                "fallback_report": True,
                "report_partial": True,
            }
        brief = {
            "user_question": self.s.request.query,
            "standalone_question": state["understanding"]["standalone_query"],
            "reader_goal": state["understanding"].get("reader_goal"),
            "output_language": self.s.request.output_language,
            "as_of": self.s.request.as_of.isoformat() if self.s.request.as_of else None,
            "time_range": self.s.request.time_range.model_dump(mode="json")
            if self.s.request.time_range
            else None,
        }
        criteria = self.graph.all_criteria(state)
        selected = []
        omitted = []
        for claim in claims:
            candidate = {**brief, **pack_materials(selected + [claim], criteria)}
            try:
                self.s.llm.build("writer", candidate, ProseComposition)
                selected.append(claim)
            except Stopped as error:
                if error.code != "input_budget_exceeded":
                    raise
                omitted.append(cm[claim["claim_id"]])
        if omitted:
            await self.s.warn("writer.material_deferred", claim_ids=omitted)
        if not selected:
            return {
                "report_draft": digest_draft(
                    self.s.request.query,
                    bundle,
                    ["已核验材料超出本次写作容量，尚未完成综合解释。"],
                    self.s.request.output_language,
                ).model_dump(mode="json"),
                "fallback_report": True,
                "report_partial": True,
            }
        payload = {**brief, **pack_materials(selected, criteria)}
        offered_claims = {c["claim_id"] for c in payload["claims"]}
        offered_evidence = {e["evidence_id"] for e in payload["evidence"]}
        cm = {k: v for k, v in cm.items() if k in offered_claims}
        em = {k: v for k, v in em.items() if k in offered_evidence}
        try:
            value = await self.s.llm.generate(
                "write:prose:v4",
                "writer",
                payload,
                ProseComposition,
                partition="finalization",
                validator=lambda v: validate_prose(v, payload),
            )
            draft = prose_draft(value, cm, em)
            return {"report_draft": draft.model_dump(mode="json"), "fallback_report": False}
        except (ToolFailure, Stopped) as error:
            if getattr(error, "fatal", False) or error.code == "cancelled":
                raise
            await self.s.warn("narrative.write_unavailable", reason=error.code)
            return {
                "report_draft": digest_draft(
                    self.s.request.query,
                    bundle,
                    ["本次未能完成综合写作，以下保留已核对的来源材料。"],
                    self.s.request.output_language,
                ).model_dump(mode="json"),
                "fallback_report": True,
                "report_partial": True,
            }

    async def repair(self, paragraph, reason, state, key):
        _bundle, claims, cm, em = await self.package(state)
        rc, re = {v: k for k, v in cm.items()}, {v: k for k, v in em.items()}
        short = self.map_paragraph(paragraph, rc, re)
        relevant = {x for a in short.assertions for x in a.claim_ids}
        selected = [c for c in claims if c["claim_id"] in relevant]
        selected = [
            {
                "claim_id": c["claim_id"],
                "text": c["text"],
                "status": c["status"],
                "evidence": [
                    {k: v for k, v in e.items() if k not in {"reading_context", "source_id", "section"}}
                    for e in c["evidence"]
                ],
            }
            for c in selected
        ]
        payload = {
            "paragraph": {
                "paragraph_id": short.paragraph_id,
                "text": short.text,
                "claim_ids": list(dict.fromkeys(x for a in short.assertions for x in a.claim_ids)),
                "evidence_ids": list(dict.fromkeys(x for a in short.assertions for x in a.evidence_ids)),
            },
            "feedback": reason,
            "question": self.s.request.query,
            "claims": selected,
        }
        original = NarrativeDraft.model_validate(state["report_draft"])
        all_paragraphs = paragraphs(original)
        index = next(
            (i for i, p in enumerate(all_paragraphs) if p.paragraph_id == paragraph.paragraph_id), None
        )
        if index is not None:
            payload["surrounding_paragraphs"] = [
                all_paragraphs[i].text for i in [index - 1, index + 1] if 0 <= i < len(all_paragraphs)
            ]
            if index > 1:
                # A later repair must not reintroduce the definition already
                # given in the opening when removing an unsupported comparison.
                payload["opening_paragraph"] = all_paragraphs[0].text
                try:
                    self.s.llm.build("repair", payload, ProseRepair)
                except Stopped as error:
                    if error.code != "input_budget_exceeded":
                        raise
                    payload.pop("opening_paragraph")
        for c in claims:
            # Fact repair normally needs only the paragraph's own evidence.
            # Filling the input with unrelated material wastes the bounded
            # repair allowance and crowds out subsequent paragraphs' repairs.
            if not key.startswith("quality:"):
                break
            if c["claim_id"] in relevant:
                continue
            extra = {
                "claim_id": c["claim_id"],
                "text": c["text"],
                "status": c["status"],
                "evidence": [
                    {k: v for k, v in e.items() if k not in {"reading_context", "source_id", "section"}}
                    for e in c["evidence"]
                ],
            }
            selected.append(extra)
            try:
                self.s.llm.build("repair", payload, ProseRepair)
            except Stopped as error:
                if error.code != "input_budget_exceeded":
                    raise
                selected.pop()

        offered_claims = {c["claim_id"] for c in selected}
        offered_evidence = {e["evidence_id"] for c in selected for e in c["evidence"]}

        def validate(value):
            if len(value.paragraphs) != 1 or value.paragraphs[0].paragraph_id != paragraph.paragraph_id:
                raise ValueError("Return exactly the requested paragraph, preserving its ID")
            p = value.paragraphs[0]
            validate_ids(p.claim_ids, offered_claims)
            validate_ids(p.evidence_ids, offered_evidence)
            if not p.claim_ids or not p.evidence_ids:
                raise ValueError("Revised prose needs factual premises")

        patch = await self.s.llm.generate(
            key, "repair", payload, ProseRepair, partition="finalization", validator=validate
        )
        p = patch.paragraphs[0]
        return NarrativeParagraph(
            paragraph_id=p.paragraph_id,
            text=p.text,
            assertions=[
                NarrativeAssertion(
                    statement_id=p.paragraph_id + ":hint",
                    text=p.text,
                    anchor=p.text,
                    kind="fact",
                    claim_ids=[cm[x] for x in p.claim_ids],
                    evidence_ids=[em[x] for x in p.evidence_ids],
                )
            ],
        )

    def audit_payload(self, group, state, bundle):
        evidence = {e["id"]: e for e in bundle["evidence"]}
        sources = {s["id"]: s for s in bundle["sources"]}
        claims = {c["id"]: c for c in bundle["claims"]}
        cids = list(dict.fromkeys(cid for p in group for a in p.assertions for cid in a.claim_ids))
        eids = list(dict.fromkeys(eid for p in group for a in p.assertions for eid in a.evidence_ids))
        # Resolve a known passage to its actual verified claims even when the
        # writer's hints omitted one. This is retrieval only: the auditor still
        # has to entail the complete prose and validate each resulting link.
        cids = list(
            dict.fromkeys(
                cids
                + [
                    r["claim_id"]
                    for r in bundle["relations"]
                    if r["evidence_id"] in eids
                    and not claims[r["claim_id"]]["merged_into"]
                    and claims[r["claim_id"]]["status"] not in {"unverified", "superseded"}
                ]
            )
        )
        cm = {f"C{i}": cid for i, cid in enumerate(cids)}
        em = {f"E{i}": eid for i, eid in enumerate(eids)}
        criteria = self.graph.all_criteria(state)
        qa = {f"Q{i}": cid for i, cid in enumerate(criteria)}
        rc, re = {v: k for k, v in cm.items()}, {v: k for k, v in em.items()}
        sids = list(dict.fromkeys(evidence[eid]["source_id"] for eid in eids))
        source_aliases = {sid: f"S{i}" for i, sid in enumerate(sids)}
        first = next(
            (p["paragraph_id"] for sec in state["report_draft"]["sections"] for p in sec["paragraphs"]), None
        )
        payload = {
            "report_title": state["report_draft"]["title"],
            "verify_title": any(p.paragraph_id == first for p in group),
            "paragraphs": [
                {
                    "paragraph_id": p.paragraph_id,
                    "text": p.text,
                    "block_type": p.block_type,
                    "language": p.language,
                    "illustrative": p.illustrative,
                    "heading": next(
                        (
                            sec["heading"]
                            for sec in state["report_draft"]["sections"]
                            if any(q["paragraph_id"] == p.paragraph_id for q in sec["paragraphs"])
                        ),
                        "",
                    ),
                    "citation_hints": [
                        {
                            "anchor": a.anchor,
                            "claim_ids": [rc[x] for x in a.claim_ids],
                            "evidence_ids": [re[x] for x in a.evidence_ids],
                        }
                        for a in p.assertions
                    ],
                }
                for p in group
            ],
            "claims": [
                {
                    "claim_id": k,
                    "text": claims[v]["canonical_text"],
                    "status": claims[v]["status"],
                    "evidence_links": [
                        {
                            "evidence_id": re[r["evidence_id"]],
                            "relation": r["relation"],
                            "primary": r["verification"].get("primary_for_claim", False),
                        }
                        for r in bundle["relations"]
                        if r["claim_id"] == v and r["evidence_id"] in re
                    ],
                }
                for k, v in cm.items()
            ],
            "evidence": [
                {
                    "evidence_id": k,
                    "quote": evidence[v]["quote"],
                    "source_id": source_aliases[evidence[v]["source_id"]],
                    "table_header": bundle.get("table_headers", {}).get(v, ""),
                }
                for k, v in em.items()
            ],
            "sources": [
                {
                    "source_id": source_aliases[sid],
                    **{field: sources[sid][field] for field in ["title", "canonical_url", "published_at"]},
                }
                for sid in sids
            ],
            "criteria": [
                {"criterion_id": k, "description": criteria[v].description, "kind": criteria[v].kind}
                for k, v in qa.items()
            ],
        }
        return payload, cm, em, qa

    async def audit_bundle(self):
        from .table_context import table_header

        bundle = await self.s.evidence.bundle()
        sources = {s["id"]: await self.s.evidence.get_source(s["id"]) for s in bundle["sources"]}
        bundle["table_headers"] = {
            e["id"]: table_header(sources[e["source_id"]]["content_text"], e["start_char"], e["end_char"])
            for e in bundle["evidence"]
        }
        return bundle

    def audit_packets(self, ps, state, bundle):
        import tiktoken

        encoding = tiktoken.get_encoding(self.s.config["tokenizer"])
        schema = (
            CompactBatchParagraphAudit
            if state.get("report_draft", {}).get("schema_version") == 3
            else BatchParagraphAudit
        )
        group = []
        for p in ps:
            if len(group) >= self.s.config["model"]["roles"]["report_verifier"].get("max_paragraphs", 2):
                yield group
                group = []
            candidate = group + [p]
            payload, _, _, _ = self.audit_payload(candidate, state, bundle)
            # Use existing annotations to estimate serialized output, under the
            # approved output cap. This is packing, never a fact/quality threshold.
            estimate = {
                "paragraphs": [
                    {
                        "paragraph_id": q.paragraph_id,
                        "atoms": [
                            {
                                **({"text": a.text} if schema is BatchParagraphAudit else {}),
                                "anchor": a.anchor,
                                "kind": a.kind,
                                "claim_ids": ["C0"],
                                "evidence_ids": ["E0"],
                                "verdict": "entailment",
                                "answered_criterion_ids": ["Q0"],
                            }
                            for a in q.assertions
                        ],
                        "complete": True,
                        "headings_supported": True,
                        "unsupported_text": [],
                        "explanation": "",
                    }
                    for q in candidate
                ]
            }
            too_large = (
                len(encoding.encode(canonical(estimate)))
                > self.s.config["model"]["roles"]["report_verifier"]["max_output"]
            )
            try:
                self.s.llm.build("report_verifier", payload, schema)
            except Stopped as error:
                if error.code != "input_budget_exceeded":
                    raise
                too_large = True
            if group and too_large:
                yield group
                group = []
            group.append(p)
        if group:
            yield group

    async def audit(self, group, state, bundle, key, is_recheck=False):
        payload, cm, em, qa = self.audit_payload(group, state, bundle)
        pmap = {p.paragraph_id: p for p in group}

        def validate(value):
            ids = [v.paragraph_id for v in value.paragraphs]
            if set(ids) != set(pmap) or len(ids) != len(pmap):
                raise ValueError("One audit per requested paragraph required")
            for result in value.paragraphs:
                for atom in result.atoms:
                    validate_ids(atom.claim_ids, cm)
                    validate_ids(atom.evidence_ids, em)
                    validate_ids(atom.answered_criterion_ids, qa)
                    anchor_span(pmap[result.paragraph_id], atom)
                    if atom.verdict == "entailment" and (not atom.claim_ids or not atom.evidence_ids):
                        # Reject this at the structured-response boundary, so
                        # the existing bounded correction can fix an accidental
                        # discourse atom instead of rejecting the whole prose.
                        raise ValueError(
                            "An entailed factual atom requires claim/evidence premises. "
                            "Self-description of this answer (such as labelling an example hypothetical) "
                            "is discourse: keep its prose but do not emit it as an atomic factual assertion. "
                            "External facts and absence claims still require linked evidence."
                        )
                if result.complete and not result.atoms:
                    raise ValueError("An audited paragraph must contain atomic evidence mappings")

        try:
            results = await self.s.llm.generate(
                key,
                "report_verifier",
                payload,
                CompactBatchParagraphAudit
                if state["report_draft"].get("schema_version") == 3
                else BatchParagraphAudit,
                partition="finalization",
                is_recheck=is_recheck,
                validator=validate,
            )
        except ToolFailure as error:
            if error.code != "audit_output_truncated" or len(group) == 1:
                raise
            combined, failures, headings = {}, {}, []
            middle = len(group) // 2
            for suffix, part in [("left", group[:middle]), ("right", group[middle:])]:
                try:
                    good, bad, heads = await self.audit(part, state, bundle, key + ":" + suffix, is_recheck)
                    combined.update(good)
                    failures.update(bad)
                    headings.extend(heads)
                except (ToolFailure, Stopped) as nested:
                    if getattr(nested, "fatal", False) or nested.code == "cancelled":
                        raise
                    failures.update({p.paragraph_id: nested.code for p in part})
            return combined, failures, headings
        accepted = {}
        errors = {}
        headings = []
        for result in results.paragraphs:
            p = pmap[result.paragraph_id]
            if not result.headings_supported:
                headings.append(p.paragraph_id)
            # The body and its heading have separate verdicts. A model can put
            # a rejected heading in unsupported_text despite the typed contract.
            # Only ignore an EXACT heading when it is absent from the body;
            # every body atom still must be independently entailed and complete.
            heading = next(x["heading"] for x in payload["paragraphs"] if x["paragraph_id"] == p.paragraph_id)
            unsupported = [
                x
                for x in result.unsupported_text
                if not (not result.headings_supported and x == heading and x not in p.text)
            ]
            if not result.complete or unsupported or any(a.verdict != "entailment" for a in result.atoms):
                errors[p.paragraph_id] = result.explanation + " " + str(result.unsupported_text)
                continue
            # Keep epistemic classification at assertion scope. A mixed
            # paragraph must not be labelled wholly speculative. Hypothetical
            # examples still need to be visibly distinguished from real events.
            if any(a.kind == "example" for a in result.atoms):
                p = p.model_copy(update={"illustrative": True})
            assertions = []
            answered = set()
            try:
                for i, atom in enumerate(result.atoms):
                    a = NarrativeAssertion(
                        statement_id=f"{p.paragraph_id}:a{i}",
                        text=getattr(atom, "text", atom.anchor),
                        anchor=atom.anchor,
                        kind=atom.kind,
                        claim_ids=[cm[x] for x in atom.claim_ids],
                        evidence_ids=[em[x] for x in atom.evidence_ids],
                    )
                    validate_statement(a, bundle)
                    assertions.append(a)
                    answered.update(qa[x] for x in atom.answered_criterion_ids)
                accepted[p.paragraph_id] = (p.model_copy(update={"assertions": assertions}), answered)
            except ValueError as error:
                errors[p.paragraph_id] = str(error)
        return accepted, errors, headings

    async def quality(self, state):
        if state.get("fallback_report"):
            return {}
        await self.s.db.phase(self.s.run_id, self.s.fence, "quality")
        draft = NarrativeDraft.model_validate(state["report_draft"])
        ps = paragraphs(draft)
        payload = {
            "question": self.s.request.query,
            "reader_goal": state["understanding"].get("reader_goal"),
            "title": draft.title,
            "sections": [
                {"heading": sec.heading, "paragraph_ids": [p.paragraph_id for p in sec.paragraphs]}
                for sec in draft.sections
            ],
            "required_answer_checks": [c.description for c in self.graph.all_criteria(state).values()],
            "paragraphs": [{"paragraph_id": p.paragraph_id, "text": p.text} for p in ps],
        }
        try:
            review = await self.s.llm.generate(
                "quality:report:v2",
                "quality",
                payload,
                QualityReview,
                partition="finalization",
                validator=lambda q: validate_ids(
                    [pid for i in q.issues for pid in i.paragraph_ids], [p.paragraph_id for p in ps]
                ),
            )
        except (ToolFailure, Stopped, ValueError) as error:
            if getattr(error, "fatal", False) or getattr(error, "code", None) == "cancelled":
                raise
            await self.s.warn("quality.not_checked", reason=getattr(error, "code", str(error)))
            draft.quality_status = "not_checked"
            return {
                "report_draft": draft.model_dump(mode="json"),
                "report_partial": True,
                "quality_failed": True,
            }
        await self.s.ops.close_finalization_stage("quality")
        failed = any(not issue.paragraph_ids for issue in review.issues)
        bundle = await self.audit_bundle()
        for sec in draft.sections:
            for i, p in enumerate(sec.paragraphs):
                issues = [issue for issue in review.issues if p.paragraph_id in issue.paragraph_ids]
                if not issues:
                    continue
                if p.paragraph_id in state.get("repaired_paragraphs", []):
                    failed = True
                    continue
                try:
                    patched = await self.repair(
                        p, [issue.model_dump() for issue in issues], state, "quality:repair:" + p.paragraph_id
                    )
                    accepted, errors, headings = await self.audit(
                        [patched], state, bundle, "quality:recheck:" + p.paragraph_id, is_recheck=True
                    )
                    if p.paragraph_id not in accepted:
                        raise ValueError(str(errors))
                    sec.paragraphs[i], answers = accepted[p.paragraph_id]
                    draft.paragraph_answers[p.paragraph_id] = sorted(answers)
                    if headings:
                        draft.title = self.s.request.query
                        sec.heading = ""
                except (ToolFailure, Stopped, ValueError) as error:
                    if getattr(error, "fatal", False) or getattr(error, "code", None) == "cancelled":
                        raise
                    # The previous fact-checked paragraph remains available.
                    failed = True
                    await self.s.warn(
                        "quality.repair_unavailable",
                        paragraph_id=p.paragraph_id,
                        reason=getattr(error, "code", str(error))[:500],
                    )
        draft.quality_status = "issues_remaining" if failed else "revised" if review.issues else "passed"
        if failed:
            draft.limitations.append("解释的完整性或行文仍有待改进，已保留通过事实核验的内容。")
        result = await self.coverage(draft, state, bundle)
        result.update(
            quality_review=review.model_dump(mode="json"),
            quality_failed=failed,
            report_partial=result["report_partial"] or failed or state.get("report_partial", False),
        )
        return result

    async def validate(self, state):
        await self.s.ops.close_finalization_stage("writer")
        if state.get("fallback_report"):
            return {}
        await self.s.db.phase(self.s.run_id, self.s.fence, "validate_report")
        draft = NarrativeDraft.model_validate(state["report_draft"])
        bundle = await self.audit_bundle()
        accepted = {}
        removed = []
        repaired = list(state.get("repaired_paragraphs", []))
        pending = []
        for group in self.audit_packets(paragraphs(draft), state, bundle):
            key = "validate:paragraph-packet:" + digest([p.model_dump() for p in group])[:16]
            try:
                good, errors, headings = await self.audit(group, state, bundle, key)
                accepted.update(good)
                for pid in headings:
                    draft.title = self.s.request.query
                    for sec in draft.sections:
                        if any(p.paragraph_id == pid for p in sec.paragraphs):
                            sec.heading = ""
            except (ToolFailure, Stopped, ValueError) as error:
                if getattr(error, "fatal", False) or getattr(error, "code", None) == "cancelled":
                    raise
                errors = {p.paragraph_id: getattr(error, "code", str(error)) for p in group}
            pending.extend(
                (p, errors.get(p.paragraph_id, "Incomplete audit"))
                for p in group
                if p.paragraph_id not in accepted
            )
        await self.s.ops.close_finalization_stage("report_verifier")
        # Audit the whole answer before spending the repair allowance on early
        # paragraphs. Otherwise one bad opening can starve all later evidence.
        for p, error_reason in pending:
            if p.paragraph_id not in accepted:
                try:
                    if p.paragraph_id in repaired:
                        raise Stopped("repair_already_used")
                    if error_reason in {"budget_exhausted", "deadline"}:
                        raise Stopped(error_reason)  # No factual feedback to repair; do not spend blindly.
                    patched = await self.repair(
                        p,
                        error_reason,
                        state,
                        "repair:paragraph:" + p.paragraph_id,
                    )
                    repaired.append(p.paragraph_id)
                    good, _remaining, heads = await self.audit(
                        [patched], state, bundle, "validate:repaired:" + p.paragraph_id, is_recheck=True
                    )
                    accepted.update(good)
                    if heads:
                        draft.title = self.s.request.query
                except (ToolFailure, Stopped, ValueError) as error:
                    if getattr(error, "fatal", False) or getattr(error, "code", None) == "cancelled":
                        raise
                if p.paragraph_id not in accepted:
                    removed.append(p.paragraph_id)
                    await self.s.warn(
                        "report.paragraph_rejected",
                        paragraph_id=p.paragraph_id,
                        reason=error_reason[:800],
                    )
        for sec in draft.sections:
            sec.paragraphs = [
                accepted[p.paragraph_id][0] for p in sec.paragraphs if p.paragraph_id in accepted
            ]
        draft.sections = [sec for sec in draft.sections if sec.paragraphs]
        draft.paragraph_answers = {pid: sorted(answers) for pid, (_, answers) in accepted.items()}
        if not paragraphs(draft):
            return {
                "report_draft": digest_draft(
                    self.s.request.query,
                    bundle,
                    ["候选解释未通过完整事实核验，以下仅保留来源材料。"],
                    self.s.request.output_language,
                ).model_dump(mode="json"),
                "report_partial": True,
                "fallback_report": True,
                "report_coverage": 0.0,
            }
        if removed:
            draft.limitations.append("部分候选段落未通过事实核验，已移除；正文仅保留通过核验的内容。")
        result = await self.coverage(draft, state, bundle)
        # Removed redundant prose is an audit event. Completion still requires
        # every frozen core question AND the whole-answer quality check.
        result.update(repaired_paragraphs=repaired, report_partial=result["report_partial"])
        return result

    async def coverage(self, draft, state, bundle):
        draft.coverage = []
        draft.unresolved_questions = []
        answered = {cid for ids in draft.paragraph_answers.values() for cid in ids}
        criteria = self.graph.all_criteria(state)
        from ..storage.evidence import assess_criteria
        from .reports import cited_support_bundle

        assessments = assess_criteria(
            list(criteria.values()),
            cited_support_bundle(draft, bundle),
            state["understanding"]["source_policy"],
        )
        supported = {a.criterion_id for a in assessments if a.status == "covered"}
        answered &= supported
        required = set()
        for f in state["understanding"]["facets"]:
            relevant = set()
            for task in state["plan_history"][0]["tasks"]:
                for c in task["success_criteria"]:
                    if f["facet_id"] in (c.get("facet_ids") or task["facet_ids"]) and (
                        not f["required"] or c.get("required", True)
                    ):
                        relevant.add(c["criterion_id"])
            status = (
                "answered"
                if relevant and relevant <= answered
                else "partial"
                if relevant & answered
                else "unresolved"
            )
            draft.coverage.append(
                QuestionCoverage(question=f["question"], required=f["required"], status=status)
            )
            if f["required"]:
                required |= relevant
                if status != "answered":
                    draft.unresolved_questions.append(f["question"])
        if state.get("quality_failed"):
            draft.limitations.append("本次未完成全部写作质量检查，内容仍可能需要进一步整理。")
        if draft.unresolved_questions:
            draft.limitations.append("当前资料未能充分回答下列未解问题，不能据此认定研究对象没有这些能力。")
        draft.limitations = list(dict.fromkeys(draft.limitations))
        coverage = len(required & answered) / len(required) if required else 0.0
        partial = (
            not required
            or not required <= answered
            or bool(draft.unresolved_questions)
            or state.get("quality_failed", False)
            or state.get("report_partial", False)
        )
        return {
            "report_draft": draft.model_dump(mode="json"),
            "report_partial": partial,
            "report_coverage": coverage,
        }
