import hashlib
import random
from itertools import pairwise
from types import SimpleNamespace

import pytest

from research_agent.services import private_rag as rag
from research_agent.services import rag_store as store
from research_agent.services.rag_chunking import TEXT_INDEX_VERSION, VERSION, chunks


def assert_slices(text, parts, size, excluded=()):
    covered = set()
    last_start, last_end = -1, 0
    for start, value in parts:
        assert start > last_start
        assert 0 < len(value) <= size
        assert value == text[start : start + len(value)]
        assert start + len(value) > last_end
        assert value.encode("utf-8").decode("utf-8") == value
        covered.update(range(start, start + len(value)))
        last_start, last_end = start, start + len(value)
    omitted = {i for begin, end in excluded for i in range(begin, end)}
    assert {i for i, char in enumerate(text) if not char.isspace()} - omitted <= covered
    assert not covered & omitted


def test_paragraph_preferred_and_overlap_is_bounded():
    paragraphs = [str(i) + " alpha beta gamma" * 5 + "\n\n" for i in range(7)]
    text = "".join(paragraphs)
    parts = list(chunks(text, size=190, overlap=30))
    assert_slices(text, parts, 190)
    assert all(value.endswith("\n\n") for _, value in parts)
    assert all(0 <= a + len(value) - b <= 30 for (a, value), (b, _) in pairwise(parts))


@pytest.mark.parametrize("sentence", ["这是一个完整的中文句子。", "A complete English sentence. "])
def test_sentence_boundaries_preserve_language_units(sentence):
    text = sentence * 45
    parts = list(chunks(text, size=200, overlap=40))
    assert_slices(text, parts, 200)
    assert all(value.rstrip().endswith(("。", ".")) for _, value in parts)
    assert all(start == 0 or text[:start].rstrip().endswith(("。", ".")) for start, _ in parts)


def test_word_fallback_avoids_cutting_english_words():
    text = "retrieval documentation mechanism " * 80
    parts = list(chunks(text, size=180, overlap=25))
    assert_slices(text, parts, 180)
    for start, value in parts:
        assert start == 0 or text[start - 1].isspace()
        assert start + len(value) == len(text) or value[-1].isspace()


@pytest.mark.parametrize("marker", ["```", "~~~~"])
def test_fenced_code_is_kept_whole_even_when_overlap_would_cut_it(marker):
    prefix = "A short sentence. " * 6 + "\n\n"
    code = marker + "python\n" + "x = call('value')\n" * 8 + marker + "\n"
    text = prefix + code + "\n" + "Following explanation. " * 20
    parts = list(chunks(text, size=200, overlap=40))
    assert_slices(text, parts, 200)
    begin, end = len(prefix), len(prefix) + len(code)
    for start, value in parts:
        stop = start + len(value)
        if start < end and stop > begin:
            assert start <= begin and stop >= end
    assert sum(code in value for _, value in parts) >= 1


@pytest.mark.parametrize(
    "table",
    [
        "| Name | Value |\n| --- | ---: |\n| alpha | 1 |\n| beta | 2 |\n",
        "| Name |\n| --- |\n| alpha |\n| beta |\n",
    ],
)
def test_table_stays_whole_and_heading_starts_a_new_section(table):
    prefix = "Explanation sentence. " * 7 + "\n\n"
    text = prefix + table + "\n## Next section\n" + "Further detail. " * 25
    parts = list(chunks(text, size=200, overlap=35))
    assert_slices(text, parts, 200)
    begin, end = len(prefix), len(prefix) + len(table)
    for start, value in parts:
        if start < end and start + len(value) > begin:
            assert start <= begin and start + len(value) >= end


@pytest.mark.parametrize("kind", ["code", "table"])
def test_oversized_structures_split_on_rows_and_report_limit(kind):
    text = (
        "```python\n" + "result = call(value)\n" * 45 + "```\n"
        if kind == "code"
        else "| Name | Value |\n| --- | --- |\n" + "| name | some value |\n" * 45
    )
    stats = {}
    parts = list(chunks(text, size=180, overlap=40, diagnostics=stats))
    assert_slices(text, parts, 180)
    for start, value in parts:
        assert start == 0 or text[start - 1] == "\n"
        assert value.endswith("\n")
    assert stats["oversized_code_blocks" if kind == "code" else "oversized_tables"] == 1
    assert stats["hard_splits"] == 0


def test_extreme_single_line_has_explicit_hard_fallback():
    text = "```\n" + "x" * 600 + "\n```\n"
    stats = {}
    parts = list(chunks(text, size=180, overlap=30, diagnostics=stats))
    assert_slices(text, parts, 180)
    assert stats["hard_splits"] > 0


def test_unicode_and_excluded_image_payload_keep_original_offsets():
    prefix = "中文🙂 café e\u0301 内容。" * 20 + "![图]("
    image = "data:image/png;base64," + "A" * 10000
    text = prefix + image + ")\n" + "结尾句子。" * 50
    stats = {}
    parts = list(chunks(text, size=180, overlap=30, diagnostics=stats))
    assert_slices(text, parts, 180, excluded=[(len(prefix), len(prefix) + len(image))])
    assert stats["excluded_image_payloads"] == 1
    assert len(parts) < 10


@pytest.mark.parametrize(
    "text",
    [
        "",
        " \r\n\t",
        "x",
        "没有标点的中文" * 100,
        "word" * 200,
        "```python\nunfinished code\n",
        "a\r\n\r\nb\r\n",
    ],
)
def test_empty_short_unpunctuated_and_unclosed_inputs_terminate(text):
    parts = list(chunks(text, size=50, overlap=10))
    assert_slices(text, parts, 50)


@pytest.mark.parametrize("size,overlap", [(0, 0), (100, -1), (100, 100), (100, 101)])
def test_invalid_limits_are_rejected(size, overlap):
    with pytest.raises(ValueError):
        list(chunks("input", size=size, overlap=overlap))


def test_random_compositions_have_no_missing_text_or_duplicate_only_chunks():
    generator = random.Random(1729)
    pieces = [
        "完整句子。",
        "A sentence. ",
        "\n\n",
        "## Header\n",
        "abcdefghijk",
        "🙂",
        "```py\nx = 1\ny = 2\n```\n",
        "| a | b |\n| --- | --- |\n| 1 | 2 |\n",
    ]
    for _ in range(80):
        text = "".join(generator.choice(pieces) for _ in range(60))
        size = generator.randrange(30, 150)
        parts = list(chunks(text, size=size, overlap=size // 5))
        assert_slices(text, parts, size)


@pytest.mark.asyncio
async def test_old_signature_rechunks_unchanged_file_and_preserves_citation_offsets(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    text = "第一段完整内容。" * 110 + "\n\n" + "Second complete paragraph. " * 110
    path = root / "note.md"
    path.write_text(text)
    settings = SimpleNamespace(vault_path=root, rag_index=tmp_path / "index.sqlite")
    revision = hashlib.sha256(path.read_bytes()).hexdigest()
    db = store.connect(settings.rag_index)
    with db:
        store.put_file(db, "note.md", revision, status="indexed", signature=store.LEGACY_INDEX_SIGNATURE)
        for i, start in enumerate(range(0, len(text), 1600)):
            value = text[start : start + 1800]
            eid = store.put_embedding(db, "note.md", value, [1.0] + [0.0] * 767)
            store.put_chunk(
                db,
                chunk_id=f"old{i}",
                path="note.md",
                index=i,
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
    db = store.connect(settings.rag_index)
    try:
        assert db.execute("SELECT index_signature FROM files").fetchone()[0] == store.INDEX_SIGNATURE
        assert (
            db.execute("SELECT value FROM metadata WHERE key='text_index_version'").fetchone()[0]
            == TEXT_INDEX_VERSION
        )
        for row in db.execute("SELECT * FROM chunks"):
            assert row["text"] == text[row["start_char"] : row["end_char"]]
            assert row["line_no"] == text[: row["start_char"]].count("\n") + 1
        db.execute("INSERT INTO chunks_fts(chunks_fts,rank) VALUES('integrity-check',1)")
    finally:
        db.close()
    calls.clear()
    await rag.index_vault(settings)
    assert calls == []
