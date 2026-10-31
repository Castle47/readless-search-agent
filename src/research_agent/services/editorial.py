"""Keep the complete draft through fact and reader review; edit the whole once."""

from collections import Counter

from ..contracts import (
    EditorialRevision,
    NarrativeDraft,
    NarrativeParagraph,
    QualityReview,
    digest,
    validate_ids,
)
from ..errors import Stopped, ToolFailure
from .composition import pack_materials, prose_draft, validate_prose
from .narrative import NarrativeWorkflow, paragraphs
from .reports import digest_draft


def block_signature(p):
    return (p.block_type, p.language, p.illustrative, p.text)


class EditorialWorkflow(NarrativeWorkflow):
    async def validate(self, state):
        await self.s.ops.close_finalization_stage("writer")
        if state.get("fallback_report"):
            return {}
        await self.s.db.phase(self.s.run_id, self.s.fence, "validate_report")
        draft = NarrativeDraft.model_validate(state["report_draft"])
        bundle = await self.audit_bundle()
        accepted, errors, headings = {}, {}, []
        for group in self.audit_packets(paragraphs(draft), state, bundle):
            key = "editorial:initial:" + digest([p.model_dump() for p in group])[:16]
            try:
                good, bad, heads = await self.audit(group, state, bundle, key)
                accepted.update(
                    {
                        pid: {"paragraph": p.model_dump(mode="json"), "answers": sorted(qs)}
                        for pid, (p, qs) in good.items()
                    }
                )
                errors.update(bad)
                headings.extend(heads)
            except (Stopped, ToolFailure, ValueError) as error:
                if getattr(error, "fatal", False) or getattr(error, "code", None) == "cancelled":
                    raise
                errors.update({p.paragraph_id: getattr(error, "code", str(error)) for p in group})
        await self.s.ops.close_finalization_stage("report_verifier")
        # No removal or rewriting here: the editor needs the intact explanation.
        return {"initial_audit": {"accepted": accepted, "errors": errors, "headings": headings}}

    async def edit(self, draft, state, feedback):
        _bundle, claims, cm, em = await self.package(state)
        rc, re = {v: k for k, v in cm.items()}, {v: k for k, v in em.items()}
        original = {
            "title": draft.title,
            "sections": [
                {
                    "heading": sec.heading,
                    "paragraphs": [
                        {
                            "paragraph_id": p.paragraph_id,
                            "block_type": p.block_type,
                            "language": p.language,
                            "illustrative": p.illustrative,
                            "text": p.text,
                            "claim_ids": list(
                                dict.fromkeys(rc[c] for a in p.assertions for c in a.claim_ids)
                            ),
                            "evidence_ids": list(
                                dict.fromkeys(re[e] for a in p.assertions for e in a.evidence_ids)
                            ),
                        }
                        for p in sec.paragraphs
                    ],
                }
                for sec in draft.sections
            ],
        }
        used = {c for sec in original["sections"] for p in sec["paragraphs"] for c in p["claim_ids"]}
        selected = [c for c in claims if c["claim_id"] in used]
        base = {
            "question": self.s.request.query,
            "reader_goal": state["understanding"].get("reader_goal"),
            "draft": original,
            "feedback": feedback,
            "output_language": self.s.request.output_language,
        }
        criteria = self.graph.all_criteria(state)
        # Preserve every original premise first. Extra material is admitted as
        # complete records when it fits, never by deleting evidence list tails.
        self.s.llm.build("editor", {**base, **pack_materials(selected, criteria)}, EditorialRevision)
        for c in claims:
            if c["claim_id"] in used:
                continue
            try:
                self.s.llm.build(
                    "editor", {**base, **pack_materials(selected + [c], criteria)}, EditorialRevision
                )
                selected.append(c)
            except Stopped as error:
                if error.code != "input_budget_exceeded":
                    raise
        payload = {**base, **pack_materials(selected, criteria)}

        def validate(value):
            validate_prose(value, payload)
            validate_ids(value.addressed_feedback_ids, [f["feedback_id"] for f in feedback])
            validate_ids(
                [i.feedback_id for i in value.remaining_issues], [f["feedback_id"] for f in feedback]
            )

        revision = await self.s.llm.generate(
            "editorial:revision:v1",
            "editor",
            payload,
            EditorialRevision,
            partition="finalization",
            validator=validate,
        )
        revised = prose_draft(revision, cm, em)
        return revised, revision

    async def quality(self, state):
        if state.get("fallback_report"):
            return {}
        await self.s.db.phase(self.s.run_id, self.s.fence, "quality")
        draft = NarrativeDraft.model_validate(state["report_draft"])
        initial = state["initial_audit"]
        accepted = {
            pid: (NarrativeParagraph.model_validate(v["paragraph"]), set(v["answers"]))
            for pid, v in initial["accepted"].items()
        }
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
            "paragraphs": [
                {"paragraph_id": p.paragraph_id, "block_type": p.block_type, "text": p.text} for p in ps
            ],
            "fact_feedback": initial["errors"],
        }
        bundle = await self.audit_bundle()
        try:
            review = await self.s.llm.generate(
                "quality:whole-draft:v3",
                "quality",
                payload,
                QualityReview,
                partition="finalization",
                validator=lambda r: validate_ids(
                    [pid for issue in r.issues for pid in issue.paragraph_ids], [p.paragraph_id for p in ps]
                ),
            )
        except (Stopped, ToolFailure, ValueError) as error:
            if getattr(error, "fatal", False) or getattr(error, "code", None) == "cancelled":
                raise
            await self.s.warn("quality.not_checked", reason=getattr(error, "code", str(error)))
            return await self.finish(draft, accepted, state, bundle, "not_checked", True)
        await self.s.ops.close_finalization_stage("quality")
        feedback = [
            {
                "feedback_id": f"Q{i}",
                "paragraph_ids": q.paragraph_ids,
                "problem": q.problem,
                "suggestion": q.suggestion,
            }
            for i, q in enumerate(review.issues)
        ]
        feedback += [
            {
                "feedback_id": f"F{i}",
                "paragraph_ids": [pid],
                "problem": error,
                "suggestion": "Correct the unsupported content while preserving the explanation.",
            }
            for i, (pid, error) in enumerate(initial["errors"].items())
        ]
        feedback += [
            {
                "feedback_id": f"H{i}",
                "paragraph_ids": [pid],
                "problem": "Unsupported factual heading/title",
                "suggestion": "Use a supported heading or neutral navigation.",
            }
            for i, pid in enumerate(initial["headings"])
        ]
        if not feedback:
            result = await self.finish(draft, accepted, state, bundle, "passed", False)
            return {**result, "quality_review": review.model_dump(mode="json")}
        try:
            revised, revision = await self.edit(draft, state, feedback)
        except (Stopped, ToolFailure, ValueError) as error:
            if getattr(error, "fatal", False) or getattr(error, "code", None) == "cancelled":
                raise
            await self.s.warn("editorial.unavailable", reason=getattr(error, "code", str(error)))
            result = await self.finish(draft, accepted, state, bundle, "issues_remaining", True)
            return {**result, "quality_review": review.model_dump(mode="json")}
        # Unchanged blocks retain their previous independent fact checks. Any
        # changed text, format, heading or title is independently checked again.
        prior = {}
        for sec in draft.sections:
            for p in sec.paragraphs:
                if p.paragraph_id in accepted and p.paragraph_id not in initial["headings"]:
                    prior[block_signature(p)] = (sec.heading, accepted[p.paragraph_id])
        good, pending, reused = {}, [], set()
        for section_index, sec in enumerate(revised.sections):
            for i, p in enumerate(sec.paragraphs):
                previous = prior.get(block_signature(p))
                needs_heading_check = (
                    i == 0
                    and previous
                    and (previous[0] != sec.heading or (section_index == 0 and revised.title != draft.title))
                )
                saved = previous[1] if previous and not needs_heading_check else None
                if saved and saved[0].paragraph_id not in reused:
                    old, answers = saved
                    sec.paragraphs[i] = old
                    good[old.paragraph_id] = (old, answers)
                    reused.add(old.paragraph_id)
                else:
                    pid = "edited-" + p.paragraph_id
                    p = p.model_copy(
                        update={
                            "paragraph_id": pid,
                            "assertions": [
                                a.model_copy(update={"statement_id": pid + ":hint"}) for a in p.assertions
                            ],
                        }
                    )
                    sec.paragraphs[i] = p
                    pending.append(p)
        audit_state = {**state, "report_draft": revised.model_dump(mode="json")}
        # A missing self-reported acknowledgement is not a new quality failure.
        # Actual unchanged problem blocks and unresolved, in-scope feedback are.
        failed = bool(revision.remaining_issues)
        old_counts = Counter(block_signature(p) for p in ps)
        new_counts = Counter(block_signature(p) for p in paragraphs(revised))
        old_places = {
            p.paragraph_id: (si, pi, sec.heading)
            for si, sec in enumerate(draft.sections)
            for pi, p in enumerate(sec.paragraphs)
        }
        new_places = {
            (block_signature(p), si, pi, sec.heading)
            for si, sec in enumerate(revised.sections)
            for pi, p in enumerate(sec.paragraphs)
        }
        original = {p.paragraph_id: p for p in ps}
        for issue in review.issues:
            unchanged = bool(issue.paragraph_ids) and all(
                (block_signature(original[pid]), *old_places[pid]) in new_places
                and new_counts[block_signature(original[pid])] >= old_counts[block_signature(original[pid])]
                for pid in issue.paragraph_ids
            )
            if ps and ps[0].paragraph_id in issue.paragraph_ids and revised.title != draft.title:
                unchanged = False
            if not issue.paragraph_ids or unchanged:
                failed = True  # No actual change to affected prose or its organization.
        bad_headings = []
        for group in self.audit_packets(pending, audit_state, bundle):
            key = "editorial:recheck:" + digest([p.model_dump() for p in group])[:16]
            try:
                checked, errors, heads = await self.audit(group, audit_state, bundle, key, is_recheck=True)
                good.update(checked)
                bad_headings.extend(heads)
                failed |= bool(errors or heads)
            except (Stopped, ToolFailure, ValueError) as error:
                if getattr(error, "fatal", False) or getattr(error, "code", None) == "cancelled":
                    raise
                failed = True
                await self.s.warn("editorial.recheck_unavailable", reason=getattr(error, "code", str(error)))
        result = await self.finish(
            revised,
            good,
            state,
            bundle,
            "issues_remaining" if failed else "revised",
            failed,
            bad_headings=bad_headings,
        )
        return {
            **result,
            "quality_review": review.model_dump(mode="json"),
            "editorial_revision": {
                "addressed_feedback_ids": revision.addressed_feedback_ids,
                "remaining_issues": [i.model_dump(mode="json") for i in revision.remaining_issues],
                "reused_blocks": sorted(reused),
                "rechecked_blocks": [p.paragraph_id for p in pending],
            },
        }

    async def finish(self, draft, accepted, state, bundle, quality_status, failed, bad_headings=None):
        bad_headings = state["initial_audit"]["headings"] if bad_headings is None else bad_headings
        if bad_headings:
            draft.title = self.s.request.query
            for sec in draft.sections:
                if any(p.paragraph_id in bad_headings for p in sec.paragraphs):
                    sec.heading = ""
        missing = [p.paragraph_id for p in paragraphs(draft) if p.paragraph_id not in accepted]
        if missing:
            await self.s.warn("editorial.blocks_rejected", paragraph_ids=missing)
        for sec in draft.sections:
            sec.paragraphs = [
                accepted[p.paragraph_id][0] for p in sec.paragraphs if p.paragraph_id in accepted
            ]
        draft.sections = [s for s in draft.sections if s.paragraphs]
        if not paragraphs(draft):
            return {
                "report_draft": digest_draft(
                    self.s.request.query,
                    bundle,
                    ["本次未能形成通过核验的完整解释。"],
                    self.s.request.output_language,
                ).model_dump(mode="json"),
                "report_partial": True,
                "fallback_report": True,
                "quality_failed": True,
                "report_coverage": 0.0,
            }
        draft.paragraph_answers = {pid: sorted(qs) for pid, (_, qs) in accepted.items()}
        draft.quality_status = quality_status
        if missing:
            draft.limitations.append("部分内容未通过最终核验，已从正文移除。")
        checked_state = {**state, "quality_failed": failed or bool(missing)}
        result = await self.coverage(draft, checked_state, bundle)
        return {**result, "quality_failed": failed or bool(missing)}
