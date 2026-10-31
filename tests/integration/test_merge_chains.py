import hashlib
from uuid import uuid4

from research_agent.contracts import FetchResult, MergeProposal, utcnow
from research_agent.storage.db import J
from research_agent.storage.evidence import EvidenceStore
from tests.integration.test_storage import new_run


async def test_transitive_merge_keeps_all_evidence_and_redirects_conflicts(db, settings):
    run = await new_run(db, settings)
    rid = str(run["id"])
    store = EvidenceStore(db, rid, run["fence"])
    a, b, c, d = sorted(str(uuid4()) for _ in range(4))
    text = "The fixture accepts text."
    sid = await store.source(
        FetchResult(
            requested_url="https://example.org/fixture",
            canonical_url="https://example.org/fixture",
            title="Fixture",
            media_type="text/plain",
            text=text,
            content_sha256=hashlib.sha256(text.encode()).hexdigest(),
            retrieved_at=utcnow(),
            published_at=None,
            publisher_key="example.org",
            source_type="official",
            metadata={},
        )
    )
    eid = str(uuid4())
    async with db.transaction(rid, run["fence"]) as conn:
        await conn.execute(
            "INSERT INTO research.evidence(id,run_id,source_id,quote,start_char,end_char,directness,verified) VALUES(%s,%s,%s,%s,0,%s,'direct',true)",
            (eid, rid, sid, text, len(text)),
        )
        for i, cid in enumerate([a, b, c, d]):
            await conn.execute(
                "INSERT INTO research.claims(id,run_id,canonical_text,normalized_text,temporal_key,status,confidence) VALUES(%s,%s,%s,%s,'','supported','high')",
                (cid, rid, f"Fixture assertion {i}", f"fixture assertion {i}"),
            )
            await conn.execute(
                "INSERT INTO research.claim_evidence(run_id,claim_id,evidence_id,relation,verification) VALUES(%s,%s,%s,'supports',%s)",
                (
                    rid,
                    cid,
                    eid,
                    J({"criterion_ids": [f"criterion-{i}"], "direct": True, "primary_for_claim": i != 2}),
                ),
            )

    def proposal(*pairs):
        return MergeProposal(
            pairs=[
                {"claim_a_id": x, "claim_b_id": y, "relation": kind, "reason": "Controlled merge fixture"}
                for x, y, kind in pairs
            ]
        )

    await store.apply_merge(proposal((b, d, "conflict"), (a, b, "equivalent"), (b, c, "equivalent")))
    bundle = await store.bundle()
    byid = {x["id"]: x for x in bundle["claims"]}
    assert byid[b]["merged_into"] == a and byid[c]["merged_into"] == a
    relation = next(r for r in bundle["relations"] if r["claim_id"] == a)
    assert set(relation["verification"]["criterion_ids"]) == {"criterion-0", "criterion-1", "criterion-2"}
    assert relation["verification"]["primary_for_claim"] is False
    assert {bundle["conflicts"][0]["claim_a_id"], bundle["conflicts"][0]["claim_b_id"]} == {a, d}
    # A later packet uses an already merged ID; it must not hide the conflict.
    await store.apply_merge(proposal((c, d, "equivalent")))
    assert next(x for x in (await store.bundle())["claims"] if x["id"] == d)["merged_into"] is None
