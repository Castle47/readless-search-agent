from types import SimpleNamespace
from unittest.mock import AsyncMock
from research_agent.services.merging import merge_claims
from research_agent.contracts import MergeProposal
from research_agent.errors import Stopped


async def test_merge_covers_cross_packet_pairs_without_discarding_or_repeating():
    claims = [
        {"id": str(i), "canonical_text": f"Claim {i}", "temporal_scope": None, "merged_into": None}
        for i in range(5)
    ]
    mappings = [{"claim_id": str(i), "criterion_id": "criterion"} for i in range(5)]
    seen = []

    def build(role, payload, schema):
        if len(payload["claims"]) > 3:
            raise Stopped("input_budget_exceeded")

    async def generate(key, role, payload, schema, validator):
        build(role, payload, schema)
        seen.extend(payload["candidate_pairs"])
        result = MergeProposal(pairs=[])
        validator(result)
        return result

    services = SimpleNamespace(
        run_id="run",
        fence=1,
        warn=AsyncMock(),
        db=SimpleNamespace(saved_value=AsyncMock(return_value=None), commit_value=AsyncMock()),
        evidence=SimpleNamespace(
            bundle=AsyncMock(return_value={"claims": claims, "mappings": mappings}), apply_merge=AsyncMock()
        ),
        llm=SimpleNamespace(build=build, generate=generate),
    )
    result = await merge_claims(services, {"batch_no": 0})
    expected = {(f"C{i}", f"C{j}") for i in range(5) for j in range(i + 1, 5)}
    assert set(seen) == expected and len(seen) == len(expected)
    assert not result["report_partial"]


async def test_related_claims_from_one_source_are_compared_across_task_criteria():
    claims = [
        {"id": str(i), "canonical_text": f"Claim {i}", "temporal_scope": None, "merged_into": None}
        for i in range(3)
    ]
    bundle = {
        "claims": list(reversed(claims)),
        "mappings": [{"claim_id": str(i), "criterion_id": f"criterion-{i}"} for i in range(3)],
        "evidence": [{"id": "e1", "source_id": "shared-source"}, {"id": "e2", "source_id": "different"}],
        "relations": [
            {"claim_id": "0", "evidence_id": "e1"},
            {"claim_id": "1", "evidence_id": "e1"},
            {"claim_id": "2", "evidence_id": "e2"},
        ],
    }
    seen = []

    async def generate(key, role, payload, schema, validator):
        seen.extend(payload["candidate_pairs"])
        value = MergeProposal(
            pairs=[
                {
                    "claim_a_id": "C0",
                    "claim_b_id": "C1",
                    "relation": "equivalent",
                    "reason": "Same assertion in two task scopes.",
                }
            ]
        )
        validator(value)
        return value

    services = SimpleNamespace(
        run_id="run",
        fence=1,
        warn=AsyncMock(),
        db=SimpleNamespace(saved_value=AsyncMock(return_value=None), commit_value=AsyncMock()),
        evidence=SimpleNamespace(bundle=AsyncMock(return_value=bundle), apply_merge=AsyncMock()),
        llm=SimpleNamespace(build=lambda *args: None, generate=generate),
    )
    result = await merge_claims(services, {"batch_no": 0})
    assert seen == [("C0", "C1")]
    assert not result["report_partial"]
    assert services.evidence.apply_merge.await_count == 1
