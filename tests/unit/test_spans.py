from research_agent.services.spans import locate_quote


def test_repair_restores_whitespace_and_unicode_offsets():
    text = "标题🐿️\nTuples are immutable\nsequences. e\u0301 中文"
    span = locate_quote(text, "Tuples are immutable sequences.", 0, 1)
    assert span and text[span[0] : span[1]] == span[2] == "Tuples are immutable\nsequences."
    assert span[3] is True
    span = locate_quote(text, "e\u0301 中文", 0, 1)
    assert text[span[0] : span[1]] == span[2]


def test_repair_never_invents_or_ambiguously_picks_words():
    assert locate_quote("The value is 42.", "The value is 99.", 0, 1) is None
    assert locate_quote("same\nsame", "same", 99, 100) is None
    assert locate_quote("same\nsame", "same", 0, 4) == (0, 4, "same", False)


def test_chinese_task_uses_english_seed_terms_to_select_source_chunks():
    from types import SimpleNamespace
    from research_agent.graph.researcher import Researcher

    text = (
        ("Numbers and immutable tuples are described here. " * 200)
        + "\nLists are mutable sequences. "
        + ("list append mutable list " * 80)
    )
    task = SimpleNamespace(
        question="列表是否可变？",
        seed_queries=["Python list mutable sequence"],
        success_criteria=[SimpleNamespace(description="判断列表的可变性")],
    )
    r = Researcher(
        SimpleNamespace(
            config={"fetch": {"chunk_chars": 1500, "chunk_overlap_chars": 100, "chunks_per_page": 2}}
        )
    )
    chunks = r.chunks("source", text, task)
    assert any("Lists are mutable sequences." in c["text"] for c in chunks)
    assert all(text[c["start_char"] : c["start_char"] + len(c["text"])] == c["text"] for c in chunks)
