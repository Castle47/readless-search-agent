from types import SimpleNamespace
from research_agent.contracts import Criterion
from research_agent.services.reports import cited_support_bundle
from research_agent.storage.evidence import assess_criteria


def fixture():
    bundle = {
        "claims": [{"id": "c", "merged_into": None, "status": "supported"}],
        "mappings": [{"criterion_id": "q", "claim_id": "c"}],
        "conflicts": [],
        "sources": [
            {"id": "s1", "publisher_key": "one", "content_sha256": "h1"},
            {"id": "s2", "publisher_key": "two", "content_sha256": "h2"},
        ],
        "evidence": [
            {"id": f"e{i}", "source_id": f"s{i}", "verified": True, "directness": "direct"} for i in (1, 2)
        ],
        "relations": [
            {
                "claim_id": "c",
                "evidence_id": f"e{i}",
                "relation": "supports",
                "verification": {"direct": True, "primary_for_claim": i == 1, "criterion_ids": ["q"]},
            }
            for i in (1, 2)
        ],
    }

    def draft(eids):
        return SimpleNamespace(
            sections=[SimpleNamespace(statements=[SimpleNamespace(claim_ids=["c"], evidence_ids=eids)])]
        )

    return bundle, draft


def test_uncited_primary_evidence_does_not_inflate_answer_coverage():
    bundle, draft = fixture()
    criterion = Criterion(criterion_id="q", description="An official definition", kind="authoritative_fact")
    assert assess_criteria([criterion], bundle, "primary_required")[0].status == "covered"
    assert (
        assess_criteria([criterion], cited_support_bundle(draft(["e2"]), bundle), "primary_required")[
            0
        ].status
        == "missing"
    )
    assert (
        assess_criteria([criterion], cited_support_bundle(draft(["e1"]), bundle), "primary_required")[
            0
        ].status
        == "covered"
    )


def test_answer_must_cite_both_required_origins_and_cannot_hide_known_conflicts():
    bundle, draft = fixture()
    criterion = Criterion(criterion_id="q", description="Independent corroboration", kind="corroborated")
    assert (
        assess_criteria([criterion], cited_support_bundle(draft(["e1"]), bundle), "primary_required")[
            0
        ].status
        == "missing"
    )
    assert (
        assess_criteria([criterion], cited_support_bundle(draft(["e1", "e2"]), bundle), "primary_required")[
            0
        ].status
        == "covered"
    )
    bundle["relations"][1]["relation"] = "contradicts"
    assert (
        assess_criteria([criterion], cited_support_bundle(draft(["e1"]), bundle), "primary_required")[
            0
        ].status
        == "conflicted"
    )
