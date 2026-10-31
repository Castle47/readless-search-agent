from research_agent.services.reading_passages import reading_passages, coalesce_passages


def test_short_math_and_code_lines_are_coalesced_without_losing_original_spans():
    text = "标题\n\n" + "\n".join(f"短行 {i}：Q = XWq; K = XWk;" for i in range(2107))
    original = reading_passages("source", text, [{"start_char": 0, "text": text}], 2500)
    packed = coalesce_passages("source", text, original, 2500)
    assert len(original) > 2000 and len(packed) < 40
    for p in packed:
        assert p["text"] == text[p["start_char"] : p["start_char"] + len(p["text"])]
        assert len(p["text"]) <= 2500
    assert all(
        any(
            p["start_char"] <= old["start_char"]
            and p["start_char"] + len(p["text"]) >= old["start_char"] + len(old["text"])
            for p in packed
        )
        for old in original
    )


def test_coalescing_does_not_reintroduce_skipped_nonwhitespace_content():
    text = "chosen one\nDO NOT SELECT THIS\nchosen two"
    original = [
        {"start_char": 0, "text": "chosen one"},
        {"start_char": text.index("chosen two"), "text": "chosen two"},
    ]
    packed = coalesce_passages("source", text, original, 2500)
    assert len(packed) == 2
    assert all("DO NOT SELECT" not in p["text"] for p in packed)
