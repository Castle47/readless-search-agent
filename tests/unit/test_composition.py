from copy import deepcopy
from types import SimpleNamespace
import pytest

from research_agent.contracts import ProseComposition
from research_agent.services.composition import pack_materials, prose_draft, validate_prose


def material():
    e = {
        "evidence_id": "E0",
        "source_id": "source-uuid",
        "quote": "The model returns a typed value.",
        "reading_context": "Before. The model returns a typed value. After.",
        "section": "Output",
        "title": "Original spec",
        "url": "https://example.org/spec",
        "published_at": None,
        "relation": "supports",
        "primary": True,
    }
    claims = [
        {
            "claim_id": "C0",
            "text": "A typed value is returned.",
            "status": "supported",
            "temporal_scope": None,
            "criterion_ids": ["criterion-uuid"],
            "evidence": [e],
        },
        {
            "claim_id": "C1",
            "text": "The returned value has a defined type.",
            "status": "supported",
            "temporal_scope": None,
            "criterion_ids": ["criterion-uuid"],
            "evidence": [{**e, "primary": False, "relation": "contradicts"}],
        },
    ]
    return claims, {"criterion-uuid": SimpleNamespace(description="Explain the output contract")}


def test_packing_deduplicates_content_without_erasing_claim_specific_relationships():
    claims, criteria = material()
    original = deepcopy(claims)
    packed = pack_materials(claims, criteria)
    assert len(packed["evidence"]) == 1 and len(packed["sources"]) == 1
    assert packed["evidence"][0]["quote"] == claims[0]["evidence"][0]["quote"]
    assert packed["evidence"][0]["reading_context"] == claims[0]["evidence"][0]["reading_context"]
    assert packed["claims"][0]["evidence_links"][0] == {
        "evidence_id": "E0",
        "relation": "supports",
        "primary": True,
    }
    assert packed["claims"][1]["evidence_links"][0] == {
        "evidence_id": "E0",
        "relation": "contradicts",
        "primary": False,
    }
    assert packed["research_questions"] == [
        {"question_id": "Q0", "description": "Explain the output contract"}
    ]
    assert claims == original


def test_writer_only_supplies_prose_hints_and_runtime_allocates_stable_paragraph_ids():
    claims, criteria = material()
    packed = pack_materials(claims, criteria)
    value = ProseComposition.model_validate(
        {
            "title": "How it works",
            "sections": [
                {
                    "heading": "",
                    "paragraphs": [
                        {
                            "text": "The output is a value with a defined type, so the caller receives a typed result.",
                            "claim_ids": ["C0", "C1"],
                            "evidence_ids": ["E0"],
                        }
                    ],
                }
            ],
        }
    )
    validate_prose(value, packed)
    draft = prose_draft(value, {"C0": "c0-uuid", "C1": "c1-uuid"}, {"E0": "e-uuid"})
    p = draft.sections[0].paragraphs[0]
    assert p.paragraph_id == "p1" and len(p.assertions) == 1
    assert p.text == value.sections[0].paragraphs[0].text == p.assertions[0].anchor
    assert p.assertions[0].claim_ids == ["c0-uuid", "c1-uuid"]
    assert draft.quality_status == "pending" and not draft.paragraph_answers
    value.sections[0].paragraphs[0].evidence_ids = ["invented"]
    with pytest.raises(ValueError):
        validate_prose(value, packed)


def test_known_evidence_hints_need_independent_audit_not_writer_pair_completeness():
    claims, criteria = material()
    packed = pack_materials(claims, criteria)
    packed["evidence"].append({**packed["evidence"][0], "evidence_id": "E1"})
    value = ProseComposition.model_validate(
        {
            "title": "T",
            "sections": [
                {
                    "heading": "",
                    "paragraphs": [{"text": "A typed output.", "claim_ids": ["C0"], "evidence_ids": ["E1"]}],
                }
            ],
        }
    )
    validate_prose(value, packed)
    draft = prose_draft(value, {"C0": "c0"}, {"E1": "e1"})
    assert draft.quality_status == "pending"
    assert not draft.paragraph_answers
