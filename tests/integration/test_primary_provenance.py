import pytest

from tests.integration.test_narrative_graph import ConnectedModel, run_fixture


class IdentityContractModel(ConnectedModel):
    """Exercise the real bounded structured correction without any network."""

    async def generate(self, key, role, payload, schema, **kwargs):
        if role != "evidence_verifier":
            return await super().generate(key, role, payload, schema, **kwargs)
        import json

        from research_agent.adapters.llm import LLM
        from research_agent.config import Settings

        adapter = LLM(Settings(model=self.ops.config["model_id"]), self.ops)

        async def request(body, timeout):
            self.identity_attempts = getattr(self, "identity_attempts", 0) + 1
            return {
                "content": json.dumps(self.response(role, payload)),
                "finish_reason": "stop",
                "usage": {"total_tokens": 100},
            }

        adapter.request = request
        try:
            return await adapter.generate(key, role, payload, schema, **kwargs)
        finally:
            await adapter.close()


async def test_model_primary_flag_cannot_replace_missing_identity_evidence(db, settings):
    class NoIdentity(ConnectedModel):
        def response(self, role, p):
            value = super().response(role, p)
            if role == "evidence_verifier":
                value["source_identity"] = None
                assert all(v["primary_for_claim"] for v in value["verdicts"])
            return value

    s, run, _ = await run_fixture(db, settings, NoIdentity)
    assert run["status"] == "partial"
    bundle = await s.evidence.bundle()
    assert bundle["evidence"]
    assert all(not r["verification"]["primary_for_claim"] for r in bundle["relations"])
    assert all(r["verification"]["primary_claimed_by_model"] for r in bundle["relations"])
    assert all(r["verification"]["primary_provenance"] is None for r in bundle["relations"])


async def test_fabricated_author_quote_cannot_satisfy_primary_threshold(db, settings):
    class ForgedIdentity(IdentityContractModel):
        def response(self, role, p):
            value = super().response(role, p)
            if role == "evidence_verifier":
                value["source_identity"]["quote"] = "I am the inventor, an invented byline."
            return value

    s, run, _ = await run_fixture(db, settings, ForgedIdentity)
    assert run["status"] == "partial"
    assert all(not r["verification"]["primary_for_claim"] for r in (await s.evidence.bundle())["relations"])


async def test_invalid_identity_quote_gets_bounded_correction_before_losing_primary_status(db, settings):
    class CorrectedIdentity(IdentityContractModel):
        def response(self, role, p):
            value = super().response(role, p)
            if role == "evidence_verifier" and self.identity_attempts == 1:
                value["source_identity"]["quote"] = "Publisher ... invented splice"
            return value

    s, run, _ = await run_fixture(db, settings, CorrectedIdentity)
    assert run["status"] == "completed"
    assert s.llm.identity_attempts == 2
    assert all(r["verification"]["primary_for_claim"] for r in (await s.evidence.bundle())["relations"])


async def test_valid_identity_is_stored_separately_with_exact_original_span(db, settings):
    s, run, _ = await run_fixture(db, settings, ConnectedModel)
    assert run["status"] == "completed"
    for relation in (await s.evidence.bundle())["relations"]:
        proof = relation["verification"]["primary_provenance"]
        assert relation["verification"]["primary_for_claim"]
        source = await s.evidence.get_source(proof["support_source_id"])
        assert source["content_sha256"] == proof["support_sha256"]
        assert source["content_text"][proof["start_char"] : proof["end_char"]] == proof["quote"]


@pytest.mark.parametrize("valid_first", [False, True])
async def test_source_identity_is_shared_across_packets_without_promoting_secondary_claims(
    db, settings, monkeypatch, valid_first
):
    import importlib
    from pathlib import Path
    from uuid import uuid4

    from research_agent.contracts import EvidenceCandidate, EvidenceVerdict, ResearchRequest, ResearchTask
    from research_agent.storage.checkpoint import saver
    from research_agent.storage.evidence import EvidenceStore

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    copy_materials = importlib.import_module("revalidate_frozen").copy_materials
    s, old, _ = await run_fixture(db, settings, ConnectedModel)
    original = await s.evidence.bundle()
    async with saver(settings) as cp:
        state = await cp.aget_tuple({"configurable": {"thread_id": str(old["id"])}})
    await db.create_run(
        ResearchRequest(query="Identity replay", memory_mode="off"), str(uuid4()), settings.snapshot()
    )
    run = await db.claim("identity-replay")
    target = EvidenceStore(db, str(run["id"]), run["fence"])
    plans = await copy_materials(
        s.evidence, target, state.checkpoint["channel_values"]["plan_history"], copy_evidence=False
    )
    task = ResearchTask.model_validate(plans[0]["tasks"][0])
    source = (await target.bundle())["sources"][0]
    old_proof = original["relations"][0]["verification"]["primary_provenance"]
    proof = {**old_proof, "source_id": source["id"], "support_source_id": source["id"]}
    identity = {
        "publisher_name": proof["publisher_name"],
        "basis": proof["basis"],
        "quote": "abbreviated ... byline",
    }
    by_claim = {c["id"]: c for c in original["claims"]}
    by_evidence = {e["id"]: e for e in original["evidence"]}
    pairs = []
    for rel in original["relations"]:
        e, c = by_evidence[rel["evidence_id"]], by_claim[rel["claim_id"]]
        candidate = EvidenceCandidate(
            source_id=source["id"],
            chunk_id="replay",
            quote=e["quote"],
            start_char=e["start_char"],
            end_char=e["end_char"],
            claim_text=c["canonical_text"],
            relation="supports",
            criterion_ids=[task.success_criteria[0].criterion_id],
            temporal_scope=None,
            directness="direct",
        )
        verdict = EvidenceVerdict(
            candidate_index=0,
            relation="supports",
            direct=True,
            primary_for_claim=True,
            criterion_ids=candidate.criterion_ids,
            explanation="Frozen semantic verdict.",
        )
        pairs.append((candidate, verdict))
    assert len(pairs) == 2
    first_proof, second_proof = (proof, None) if valid_first else (None, proof)
    await target.persist(task, [pairs[0]], primary_provenance=first_proof, identity_claim=identity)
    await target.persist(task, [pairs[1]], primary_provenance=second_proof, identity_claim=identity)
    relations = (await target.bundle())["relations"]
    assert all(r["verification"]["primary_for_claim"] for r in relations)
    assert all(r["verification"]["primary_provenance"]["source_id"] == source["id"] for r in relations)
    # A contrary/secondary classification survives even though identity exists.
    candidate, verdict = pairs[0]
    await target.persist(
        task,
        [(candidate, verdict.model_copy(update={"primary_for_claim": False}))],
        primary_provenance=proof,
        identity_claim=identity,
    )
    await target.persist(task, [pairs[1]], primary_provenance=proof, identity_claim=identity)
    assert any(not r["verification"]["primary_for_claim"] for r in (await target.bundle())["relations"])
    assert await s.evidence.bundle() == original
