from types import SimpleNamespace

from research_agent.services.reading_passages import reading_passages
from research_agent.services.source_workflow import SourceWorkflow
from research_agent.source_contracts import DraftBlock


def test_reader_paragraphs_preserve_original_offsets_and_table_context_without_window_duplicates():
    text = "Title\nA complete paragraph about the mechanism.\n|Type|Meaning|\n|---|---|\n|A|B|\nAnother paragraph.\n"
    windows = [{"start_char": 10, "text": text[10:70]}, {"start_char": 40, "text": text[40:]}]
    ps = reading_passages("source", text, windows, 2500)
    assert [p["text"] for p in ps] == [
        "A complete paragraph about the mechanism.\n",
        "|Type|Meaning|\n|---|---|\n|A|B|\n",
        "Another paragraph.\n",
    ]
    assert len({p["chunk_id"] for p in ps}) == len(ps)
    assert all(text[p["start_char"] : p["start_char"] + len(p["text"])] == p["text"] for p in ps)


def test_audit_output_capacity_splits_dense_blocks_before_the_four_block_limit(settings):
    s = SimpleNamespace(config=settings.snapshot(), llm=SimpleNamespace(build=lambda *args: None))
    workflow = SourceWorkflow(s, None)
    workflow.audit_payload = lambda *args: ({}, {}, {})
    ps = [
        DraftBlock(
            paragraph_id=f"p{i}",
            text="A sourced observation, followed by an explanation. " * 12,
            passage_ids=["P0", "P1"],
        )
        for i in range(4)
    ]
    packets = list(workflow.audit_packets(ps, {}))
    assert all(workflow.estimated_audit_output(g) <= 2400 for g in packets)
    assert len(packets) > 1
    assert [p.paragraph_id for g in packets for p in g] == [f"p{i}" for i in range(4)]


def test_body_segment_ids_are_lossless_and_select_exact_unicode_anchors():
    import pytest
    from research_agent.services.source_workflow import answer_segments, audited_anchor
    from research_agent.source_contracts import SourceAtom

    text = "输入🐿️，输出判断。\n接着解释用途。"
    p = DraftBlock(paragraph_id="p", text=text, passage_ids=["P0"])
    segments = answer_segments(text)
    assert "".join(s["text"] for s in segments.values()) == text
    a = SourceAtom(
        anchor_ids=["A0", "A1"],
        anchor=None,
        kind="fact",
        verdict="entailment",
        supports=[],
        criterion_ids=["Q0"],
        answered_criterion_ids=[],
    )
    assert audited_anchor(p, a) == "输入🐿️，输出判断。"
    with pytest.raises(ValueError, match="contiguous"):
        audited_anchor(p, a.model_copy(update={"anchor_ids": ["A0", "A2"]}))
    with pytest.raises(ValueError, match="substring"):
        audited_anchor(p, a.model_copy(update={"anchor": "不在正文中的改写"}))
