"""Cover related claim pairs in token-bounded, replayable comparison packets."""

from ..contracts import MergeProposal, digest, validate_ids
from ..errors import Stopped, ToolFailure
from .packets import packets


async def merge_claims(services, state):
    key = f"merge:inputs:batch={state['batch_no']}:v4"
    snapshot = await services.db.saved_value(services.run_id, key)
    if snapshot is None:
        bundle = await services.evidence.bundle()
        evidence = {e["id"]: e for e in bundle.get("evidence", [])}
        snapshot = {
            "claims": [
                {
                    "claim_id": c["id"],
                    "text": c["canonical_text"],
                    "temporal_scope": c["temporal_scope"],
                    "criterion_ids": sorted(
                        {m["criterion_id"] for m in bundle["mappings"] if m["claim_id"] == c["id"]}
                    ),
                    "source_ids": sorted(
                        {
                            evidence[r["evidence_id"]]["source_id"]
                            for r in bundle.get("relations", [])
                            if r["claim_id"] == c["id"]
                        }
                    ),
                }
                for c in sorted(
                    bundle["claims"], key=lambda c: (c["canonical_text"], c["temporal_scope"] or "")
                )
                if not c["merged_into"]
            ]
        }
        await services.db.commit_value(services.run_id, services.fence, key, "merge_inputs", snapshot)
    claims = snapshot["claims"]
    aliases = {f"C{i}": c["claim_id"] for i, c in enumerate(claims)}
    byid = {
        f"C{i}": {"claim_id": f"C{i}", "text": c["text"], "temporal_scope": c["temporal_scope"]}
        for i, c in enumerate(claims)
    }
    pairs = [
        (f"C{i}", f"C{j}")
        for i, a in enumerate(claims)
        for j, b in enumerate(claims)
        if i < j
        and (set(a["criterion_ids"]) & set(b["criterion_ids"]) or set(a["source_ids"]) & set(b["source_ids"]))
    ]

    def payload(group):
        ids = list(dict.fromkeys(x for pair in group for x in pair))
        return {"claims": [byid[x] for x in ids], "candidate_pairs": group}

    accepted = []
    incomplete = False
    for group in packets(pairs, payload, services.llm, "merge", MergeProposal):
        allowed = {tuple(sorted(p)) for p in group}

        def validate(value):
            validate_ids([x for p in value.pairs for x in [p.claim_a_id, p.claim_b_id]], aliases)
            if any(tuple(sorted((p.claim_a_id, p.claim_b_id))) not in allowed for p in value.pairs):
                raise ValueError("Only compare supplied candidate pairs")

        try:
            proposal = await services.llm.generate(
                f"merge:batch={state['batch_no']}:packet={digest(group)[:16]}:v4",
                "merge",
                payload(group),
                MergeProposal,
                validator=validate,
            )
            accepted.extend(
                p.model_copy(
                    update={"claim_a_id": aliases[p.claim_a_id], "claim_b_id": aliases[p.claim_b_id]}
                )
                for p in proposal.pairs
            )
        except (ToolFailure, Stopped) as error:
            if getattr(error, "fatal", False) or error.code == "cancelled":
                raise
            incomplete = True
            await services.warn("merge_degraded", reason=error.code)
            if error.code in {"deadline", "budget_exhausted"}:
                break
    for offset in range(0, len(accepted), 30):
        await services.evidence.apply_merge(MergeProposal(pairs=accepted[offset : offset + 30]))
    return {
        "claim_ids": sorted(c["claim_id"] for c in claims),
        "report_partial": state.get("report_partial", False) or incomplete,
    }
