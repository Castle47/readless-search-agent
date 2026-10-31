from types import SimpleNamespace

import pytest

from research_agent.contracts import NarrativeComposition, NarrativeParagraph
from research_agent.errors import Stopped
from research_agent.services.narrative import anchor_span, validate_composition
from research_agent.services.packets import packets


def paragraph(text="输入🐿️以后，软件可以处理输出。"):
    return NarrativeParagraph(
        paragraph_id="p1",
        text=text,
        assertions=[
            {
                "statement_id": "a1",
                "text": "软件可以处理输出。",
                "anchor": "软件可以处理输出。",
                "kind": "fact",
                "claim_ids": ["c1"],
                "evidence_ids": ["e1"],
            }
        ],
    )


def test_narrative_spans_are_exact_unicode_positions_and_unknown_ids_rejected():
    p = paragraph()
    a = p.assertions[0]
    start, end = anchor_span(p, a)
    assert p.text[start:end] == a.anchor
    composition = NarrativeComposition(title="主题", sections=[{"heading": "", "paragraphs": [p]}])
    validate_composition(composition, ["c1"], ["e1"])
    with pytest.raises(ValueError):
        validate_composition(composition, ["c2"], ["e1"])
    a.anchor = "软件"
    p.text = "软件如何工作？软件可以处理输出。"
    with pytest.raises(ValueError, match="unique exact"):
        anchor_span(p, a)


def test_token_packet_split_preserves_later_items_after_oversized_one():
    def build(role, payload, schema):
        if sum(payload) > 5:
            raise Stopped("input_budget_exceeded")

    groups = list(packets([2, 2, 9, 1, 2], lambda x: x, SimpleNamespace(build=build), "extract", None))
    assert groups == [[2, 2], [9], [1, 2]]
    assert [v for g in groups for v in g] == [2, 2, 9, 1, 2]


def test_audit_batches_limit_paragraph_count_even_when_annotations_are_short(settings):
    from research_agent.services.narrative import NarrativeWorkflow

    workflow = NarrativeWorkflow(
        SimpleNamespace(config=settings.snapshot(), llm=SimpleNamespace(build=lambda *args: None)), None
    )
    workflow.audit_payload = lambda *args: ({}, {}, {}, {})
    ps = [paragraph().model_copy(update={"paragraph_id": f"p{i}"}) for i in range(5)]
    groups = list(workflow.audit_packets(ps, {}, {}))
    assert [len(g) for g in groups] == [4, 1]
    assert [p.paragraph_id for g in groups for p in g] == [f"p{i}" for i in range(5)]


def test_writer_hint_recovery_changes_no_prose_and_rejects_unknown_evidence():
    p = paragraph()
    p.assertions[0].anchor = "absent phrase"
    composition = NarrativeComposition(title="主题", sections=[{"heading": "", "paragraphs": [p]}])
    before = p.text
    validate_composition(composition, ["c1"], ["e1"], repair_hints=True)
    assert p.text == before and p.assertions[0].anchor == before
    with pytest.raises(ValueError):
        validate_composition(composition, ["c1"], [], repair_hints=True)


def test_visible_citations_group_only_within_sentence_and_preserve_evidence_spans():
    from research_agent.contracts import Citation
    from research_agent.services.reports import sentence_citations

    text = "分类、路由、评分。随后返回🐿️结果。"
    refs = [
        Citation(
            citation_id=f"c{i}",
            statement_id=f"s{i}",
            claim_id=f"claim{i}",
            evidence_id=f"e{i}",
            paragraph_id="p",
            start_char=0,
            end_char=end,
            ordinal=ordinal,
        )
        for i, (end, ordinal) in enumerate([(2, 1), (5, 1), (8, 1), (8, 2), (len(text), 1)])
    ]
    before = [c.model_dump() for c in refs]
    assert [c.evidence_id for c in sentence_citations(text, refs)] == ["e2", "e3", "e4"]
    assert [c.model_dump() for c in refs] == before


def test_mixed_source_paragraph_citations_preserve_underlying_spans():
    from research_agent.contracts import Citation
    from research_agent.services.reports import paragraph_citations

    text = "输入🐿。随后解释用途。"
    refs = [
        Citation(
            citation_id=f"c{i}",
            ordinal=ordinal,
            claim_id=f"cl{i}",
            evidence_id=f"e{i}",
            statement_id=f"a{i}",
            paragraph_id="p",
            start_char=0,
            end_char=end,
        )
        for i, (ordinal, end) in enumerate([(1, 4), (2, 5), (1, len(text))])
    ]
    before = [c.model_dump() for c in refs]
    visible = paragraph_citations(text, refs)
    assert [c.ordinal for c in visible] == [1, 2]
    assert all(c.end_char == len(text) for c in visible)
    assert [c.model_dump() for c in refs] == before
