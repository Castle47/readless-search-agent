import hashlib
from itertools import pairwise
from types import SimpleNamespace

import pytest

from research_agent.services import private_rag as rag
from research_agent.services import rag_english
from research_agent.services import rag_store as store
from research_agent.services.rag_chunking import TEXT_INDEX_VERSION, VERSION, chunks
from research_agent.services.rag_english import MAX_CHARS, EnglishText, pipeline


def word_count(text):
    parsed = EnglishText(text)
    return parsed.count(0, len(text))


def assert_original_coverage(text, parts):
    covered = set()
    previous_end = 0
    for start, value in parts:
        assert text[start : start + len(value)] == value
        assert start + len(value) > previous_end
        covered.update(range(start, start + len(value)))
        previous_end = start + len(value)
    assert {i for i, char in enumerate(text) if not char.isspace()} <= covered


def test_only_blank_english_and_non_trainable_sentencizer_are_loaded(monkeypatch):
    import spacy

    def reject(*args, **kwargs):
        raise AssertionError("No trained pipeline should be loaded")

    monkeypatch.setattr(spacy, "load", reject)
    pipeline.cache_clear()
    nlp = pipeline()
    assert nlp.lang == "en" and nlp.pipe_names == ["sentencizer"]
    assert not nlp.get_pipe("sentencizer").is_trainable
    assert nlp.vocab.vectors.size == 0


@pytest.mark.parametrize(
    "first",
    [
        "Dr. Smith reviews the cache.",
        "Use e.g. prefix caching for speed.",
        "Use i.e. a local copy.",
        "The rate is 3.14.",
        "J. R. Smith visited the U.S. office.",
    ],
)
def test_abbreviations_initials_and_decimals_do_not_create_false_sentence_boundaries(first):
    text = first + " Next sentence."
    parsed = EnglishText(text)
    assert parsed.sentences == [len(first), len(text)]


def test_default_is_300_words_and_30_word_overlap_not_1800_characters():
    sentence = " ".join(f"architecture{i}" for i in range(10)) + ". "
    text = sentence * 70
    parts = list(chunks(text))
    assert_original_coverage(text, parts)
    assert word_count(parts[0][1]) == 300
    assert len(parts[0][1]) > 1800
    assert all(word_count(value) <= 300 for _, value in parts)
    for (start, value), (next_start, _) in pairwise(parts):
        assert word_count(text[next_start : start + len(value)]) == 30
    assert all(value.rstrip().endswith(".") for _, value in parts)


def test_whole_sentence_wins_over_filling_300_words():
    first = " ".join(f"first{i}" for i in range(140)) + ". "
    second = " ".join(f"second{i}" for i in range(180)) + "."
    text = first + second
    parts = list(chunks(text))
    assert_original_coverage(text, parts)
    assert parts[0][1].rstrip() == first.rstrip()
    assert all(word_count(value) <= 300 for _, value in parts)
    assert all(word_count(text[b : a + len(value)]) <= 30 for (a, value), (b, _) in pairwise(parts))


def test_overlong_sentence_falls_back_to_words_and_does_not_cut_contractions():
    text = " ".join(["can't"] * 330) + "."
    parts = list(chunks(text))
    assert_original_coverage(text, parts)
    assert all(word_count(value) <= 300 for _, value in parts)
    assert all(start == 0 or text[start - 1].isspace() for start, _ in parts)
    assert all(value.rstrip().endswith(("can't", ".")) for _, value in parts)


@pytest.mark.parametrize(
    "block",
    [
        "```python\n" + "result = compute(value)\n" * 80 + "```\n",
        "| Name | Result |\n| --- | --- |\n" + "| feature_name | supported |\n" * 100,
    ],
)
def test_structures_over_1800_characters_but_under_300_words_remain_whole(block):
    assert len(block) > 1800 and word_count(block) < 300
    prefix = "intro " * 100 + "\n\n"
    text = prefix + block + "\n" + "followup " * 100
    parts = list(chunks(text))
    assert_original_coverage(text, parts)
    begin, end = len(prefix), len(prefix) + len(block)
    for start, value in parts:
        if start < end and start + len(value) > begin:
            assert start <= begin and start + len(value) >= end
        assert word_count(value) <= 300


def test_structure_over_word_budget_splits_only_at_rows():
    text = "```python\n" + "result = compute(value)\n" * 250 + "```\n"
    stats = {}
    parts = list(chunks(text, diagnostics=stats))
    assert_original_coverage(text, parts)
    assert stats["oversized_code_blocks"] == 1
    for start, value in parts:
        assert start == 0 or text[start - 1] == "\n"
        assert value.endswith("\n")
        assert word_count(value) <= 300


def test_single_enormous_word_has_bounded_char_fallback_without_one_character_progress():
    text = "x" * 20000
    stats = {}
    parts = list(chunks(text, diagnostics=stats))
    assert_original_coverage(text, parts)
    assert len(parts) == 4
    assert all(len(value) <= MAX_CHARS for _, value in parts)
    assert stats["hard_splits"] == 3


def test_large_english_text_uses_bounded_sentencizer_windows():
    text = "Dr. Smith explains caching. " * 3000
    parsed = EnglishText(text)
    assert parsed.window_count > 1
    assert all(right - left <= rag_english.NLP_WINDOW_CHARS for left, right in rag_english.windows(text))
    assert all(not text[:end].endswith("Dr.") for end in parsed.sentences)
    parts = list(chunks(text))
    assert_original_coverage(text, parts)
    assert all(parsed.count(start, start + len(value)) <= 300 for start, value in parts)


@pytest.mark.parametrize("text", ["完整中文句子。" * 500, "English terminology 和中文解释。" * 250])
def test_chinese_and_mixed_text_keep_character_budget(text):
    stats = {}
    parts = list(chunks(text, diagnostics=stats))
    assert_original_coverage(text, parts)
    assert all(len(value) <= 1800 for _, value in parts)
    assert stats["english_word_chunks"] == 0
    assert all(0 <= a + len(value) - b <= 200 for (a, value), (b, _) in pairwise(parts))


def test_image_payload_exclusion_preserves_offsets_on_both_english_sides():
    left = "A complete sentence. " * 110 + "![image]("
    image = "data:image/png;base64," + "A" * 100000
    right = ")\n" + "Another complete sentence. " * 110
    text = left + image + right
    parts = list(chunks(text))
    assert all(text[start : start + len(value)] == value for start, value in parts)
    assert all(
        not (start < len(left) + len(image) and start + len(value) > len(left)) for start, value in parts
    )
    assert any(start >= len(left) + len(image) for start, _ in parts)
    assert all(word_count(value) <= 300 for _, value in parts)


def test_zero_overlap_and_configurable_word_limits():
    text = "token " * 101
    parts = list(chunks(text, english_words=40, english_overlap=0))
    assert [word_count(value) for _, value in parts] == [40, 40, 21]
    assert all(a + len(value) == b for (a, value), (b, _) in pairwise(parts))
    for maximum, overlap in [(0, 0), (300, -1), (300, 300)]:
        with pytest.raises(ValueError):
            list(chunks(text, english_words=maximum, english_overlap=overlap))


@pytest.mark.asyncio
async def test_new_policy_reindexes_existing_english_with_exact_offsets_and_then_reuses(
    tmp_path, monkeypatch
):
    root = tmp_path / "source"
    root.mkdir()
    text = (" ".join(f"architecture{i}" for i in range(10)) + ". ") * 70
    path = root / "note.md"
    path.write_text(text)
    settings = SimpleNamespace(vault_path=root, rag_index=tmp_path / "index.sqlite")
    revision = hashlib.sha256(path.read_bytes()).hexdigest()
    db = store.connect(settings.rag_index)
    with db:
        store.put_file(
            db, "note.md", revision, status="indexed", signature="text-v3:boundary-v1:chars1800:overlap200"
        )
        for index, (start, value) in enumerate(chunks(text, size=1800, overlap=200)):
            eid = store.put_embedding(db, "note.md", value, [1.0] + [0.0] * 767)
            store.put_chunk(
                db,
                chunk_id=f"old{index}",
                path="note.md",
                index=index,
                location=f"text;chars:{start}-{start + len(value)};line:1",
                text=value,
                embedding_id=eid,
                title="note.md",
            )
    db.close()
    calls = []

    async def embed(settings, texts, query=False):
        calls.extend(texts)
        return [[1.0] + [0.0] * 767 for _ in texts]

    monkeypatch.setattr(rag, "embed", embed)
    report = await rag.index_vault(settings)
    assert calls and report["chunking"]["version"] == VERSION
    assert report["chunking"]["english_word_chunks"] == report["chunks"]
    db = store.connect(settings.rag_index)
    try:
        assert (
            db.execute("SELECT value FROM metadata WHERE key='text_index_version'").fetchone()[0]
            == TEXT_INDEX_VERSION
        )
        assert db.execute("SELECT index_signature FROM files").fetchone()[0] == store.INDEX_SIGNATURE
        for row in db.execute("SELECT * FROM chunks"):
            assert row["text"] == text[row["start_char"] : row["end_char"]]
            assert word_count(row["text"]) <= 300
        db.execute("INSERT INTO chunks_fts(chunks_fts,rank) VALUES('integrity-check',1)")
    finally:
        db.close()
    calls.clear()
    await rag.index_vault(settings)
    assert calls == []
