import math
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from research_agent.services import private_rag as rag
from research_agent.services import rag_ranking as ranking
from research_agent.services import rag_store as store
from research_agent.services.rag_lexical import query_terms, tokens


def vector(cosine):
    return [cosine, math.sqrt(1 - cosine**2)] + [0.0] * 766


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    settings = SimpleNamespace(vault_path=root, rag_index=tmp_path / "index.sqlite")

    async def embed(settings, texts, query=False):
        return [vector(1) for _ in texts]

    monkeypatch.setattr(rag, "embed", embed)
    return settings


async def populate(settings, documents, *, titles=None):
    for name, body in documents.items():
        (settings.vault_path / name).write_text(body)
    await rag.index_vault(
        settings, provenance={name: {"title": title} for name, title in (titles or {}).items()}
    )


def bm25_paths(settings, query):
    db = store.connect(settings.rag_index)
    try:
        paths = {row["id"]: row["path"] for row in db.execute("SELECT id,path FROM chunks")}
        return [paths[row["id"]] for row in store.bm25_matches(db, query)]
    finally:
        db.close()


def test_chinese_words_and_normalized_english_use_same_tokenizer():
    terms = query_terms("请介绍一下我的笔记里面的注意力机制与前缀缓存，以及ＦＬＡＳＨＡＴＴＥＮＴＩＯＮ")
    assert {"注意力", "前缀", "缓存", "flashattention"} <= set(terms)
    assert not {"我", "请", "笔记", "里面", "的"} & set(terms)
    assert "缓" not in tokens("缓存")
    assert "存" not in tokens("缓存")


@pytest.mark.asyncio
async def test_bm25_rare_term_outweighs_common_term(corpus):
    docs = {"rare.md": "zephyr engine", "common.md": "cache " * 6}
    docs.update({f"background{i}.md": "cache storage background" for i in range(12)})
    await populate(corpus, docs)
    assert bm25_paths(corpus, "zephyr cache")[0] == "rare.md"


@pytest.mark.asyncio
async def test_title_weight_and_length_normalization(corpus):
    await populate(
        corpus,
        {
            "a.md": "engine design",
            "b.md": "zephyr design",
            "c.md": "zephyr " + "background " * 120,
        },
        titles={"a.md": "zephyr", "b.md": "engine", "c.md": "engine"},
    )
    order = bm25_paths(corpus, "zephyr")
    assert order.index("a.md") < order.index("b.md") < order.index("c.md")


@pytest.mark.asyncio
async def test_chinese_compound_does_not_match_scattered_characters(corpus):
    await populate(corpus, {"good.md": "前缀缓存复用已经计算的状态", "bad.md": "前方 缀饰 缓慢 存在"})
    assert bm25_paths(corpus, "前缀缓存") == ["good.md"]
    assert bm25_paths(corpus, '" OR 前缀* NOT 缓存') == ["good.md"]


@pytest.mark.asyncio
async def test_keyword_only_recall_and_strong_preview_are_separate(corpus, monkeypatch):
    async def low_semantic(settings, texts, query=False):
        return [vector(0) for _ in texts]

    monkeypatch.setattr(rag, "embed", low_semantic)
    await populate(corpus, {"answer.md": "quasar kernel optimization", "unrelated.md": "gardening flowers"})
    hits = await rag.search(corpus, "quasar", query_vector=vector(1))
    assert [hit["path"] for hit in hits] == ["answer.md"]
    assert hits[0]["semantic_rank"] is None
    assert hits[0]["bm25_rank"] == 1
    assert hits[0]["score"] == pytest.approx(1 / 61)
    assert not hits[0]["strong_match"]
    assert await rag.search(corpus, "missingword", query_vector=vector(1)) == []


@pytest.mark.asyncio
async def test_semantic_only_recall_and_empty_query(corpus):
    await populate(corpus, {"semantic.md": "saved intermediate model states"})
    hits = await rag.search(corpus, "prefix caching", query_vector=vector(1))
    assert hits[0]["bm25_rank"] is None and hits[0]["semantic_rank"] == 1
    assert not hits[0]["strong_match"]
    assert await rag.search(corpus, "请介绍一下", query_vector=vector(1)) == []
    assert await rag.search(corpus, "prefix", limit=0, query_vector=vector(1)) == []


def candidate(name, semantic, lexical_rank, coverage=1, corpus="one"):
    return {
        "id": name,
        "corpus_id": corpus,
        "semantic_score": semantic,
        "bm25_rank": lexical_rank,
        "bm25_score": -1.0,
        "lexical_coverage": coverage,
    }


def test_rrf_rewards_agreement_and_never_compares_raw_bm25():
    rows = [
        candidate("vector-only", 0.95, None, 0),
        candidate("both", 0.9, 2),
        candidate("lexical-only", 0.1, 1),
    ]
    result = ranking.fuse(rows, 5)
    assert result[0]["id"] == "both"
    assert result[0]["score"] == pytest.approx(2 / 62)
    assert result[0]["strong_match"]
    rows[2]["bm25_score"] = -1e20
    assert [(r["id"], r["score"]) for r in ranking.fuse(rows, 5)] == [(r["id"], r["score"]) for r in result]
    # Low-quality first place in a tiny corpus is not a relevance/confidence guarantee.
    assert ranking.fuse([candidate("weak", 0.1, 1, 0.1)], 5) == []


def test_federated_vector_ranks_are_global_and_ties_are_deterministic():
    rows = [candidate("a", 0.9, 2, corpus="one"), candidate("b", 0.8, 1, corpus="two")]
    result = ranking.fuse(rows, 8)
    assert [row["semantic_rank"] for row in result] == [1, 2]
    assert ranking.fuse(list(reversed(rows)), 8) == result
    assert ranking.is_strong_source({"retrieval_method": ranking.METHOD, "retrieval_score": 10}) is False
    assert ranking.is_strong_source({"retrieval_method": ranking.METHOD, "strong_match": True})
    assert ranking.is_strong_source({"retrieval_score": 0.9})  # saved legacy run only


@pytest.mark.asyncio
async def test_stale_and_disallowed_candidates_do_not_exhaust_top_k(corpus):
    await populate(
        corpus,
        {f"stale{i}.md": "quasar" for i in range(55)}
        | {"revoked.md": "quasar", "good.md": "quasar documentation", "more.md": "quasar material"},
    )
    for path in corpus.vault_path.glob("stale*.md"):
        path.write_text("changed")
    hits = await rag.search(corpus, "quasar", limit=1, query_vector=vector(1), allowed_paths={"good.md"})
    assert len(hits) == 1 and hits[0]["path"] == "good.md"
    assert hits[0]["bm25_rank"] == 1


@pytest.mark.asyncio
async def test_title_refresh_updates_bm25_without_embedding(corpus, monkeypatch):
    await populate(corpus, {"document.md": "implementation details"}, titles={"document.md": "oldtitle"})
    monkeypatch.setattr(rag, "embed", AsyncMock(side_effect=AssertionError("must reuse vector")))
    hits = await rag.search(corpus, "newtitle", query_vector=vector(0), titles={"document.md": "newtitle"})
    assert hits[0]["bm25_rank"] == 1
    assert bm25_paths(corpus, "oldtitle") == []


def create_v2(path):
    """Historical schema fixture: one combined lexical column, before BM25 field weighting."""
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    for statement in store.DDL:
        if statement.startswith(
            ("CREATE TABLE files", "CREATE TABLE embedding_cache", "CREATE TABLE IF NOT EXISTS metadata")
        ):
            db.execute(statement)
    db.executescript("""
        CREATE TABLE chunks(rowid INTEGER PRIMARY KEY,id TEXT NOT NULL UNIQUE,path TEXT NOT NULL,
          chunk_index INTEGER NOT NULL,location TEXT NOT NULL,start_char INTEGER NOT NULL,end_char INTEGER NOT NULL,
          page_no INTEGER,line_no INTEGER,text TEXT NOT NULL,content_hash TEXT NOT NULL,
          embedding_id TEXT NOT NULL,search_text TEXT NOT NULL);
        CREATE VIRTUAL TABLE chunks_fts USING fts5(search_text,content='chunks',content_rowid='rowid');
        CREATE TRIGGER chunks_insert AFTER INSERT ON chunks BEGIN
          INSERT INTO chunks_fts(rowid,search_text) VALUES(new.rowid,new.search_text); END;
        CREATE TRIGGER chunks_delete AFTER DELETE ON chunks BEGIN
          INSERT INTO chunks_fts(chunks_fts,rowid,search_text) VALUES('delete',old.rowid,old.search_text); END;
        CREATE TRIGGER chunks_update AFTER UPDATE ON chunks BEGIN
          INSERT INTO chunks_fts(chunks_fts,rowid,search_text) VALUES('delete',old.rowid,old.search_text);
          INSERT INTO chunks_fts(rowid,search_text) VALUES(new.rowid,new.search_text); END;
        PRAGMA user_version=2;
    """)
    store.put_file(db, "note.md", "revision", status="indexed", provenance={"title": "前缀缓存"})
    embedding_id = store.put_embedding(db, "note.md", "缓存", vector(1))
    db.execute(
        "INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            7,
            "stable-id",
            "note.md",
            0,
            "text;chars:0-2;line:1",
            0,
            2,
            None,
            1,
            "缓存",
            store.text_hash("缓存"),
            embedding_id,
            "note md 前 缀 缓 存",
        ),
    )
    db.commit()
    db.close()


def test_v2_migration_rebuilds_lexical_index_preserving_vectors_and_citations(tmp_path):
    path = tmp_path / "v2.sqlite"
    create_v2(path)
    db = store.connect(path)
    try:
        row = db.execute("SELECT * FROM chunks").fetchone()
        assert row["rowid"] == 7 and row["id"] == "stable-id" and row["text"] == "缓存"
        assert row["location"] == "text;chars:0-2;line:1"
        assert row["search_title"] == "前缀 缓存"
        assert db.execute("SELECT vector FROM embedding_cache").fetchone()[0] == store.pack_vector(vector(1))
        assert store.keyword_matches(db, "前缀缓存") == {"stable-id"}
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        db.execute("INSERT INTO chunks_fts(chunks_fts,rank) VALUES('integrity-check',1)")
    finally:
        db.close()
    with sqlite3.connect(path.with_name("v2.sqlite.v2-backup.sqlite")) as old:
        assert old.execute("PRAGMA user_version").fetchone()[0] == 2
        assert old.execute("SELECT search_text FROM chunks").fetchone()[0] == "note md 前 缀 缓 存"
    store.connect(path).close()


def test_v2_migration_rolls_back_when_tokenization_fails(tmp_path, monkeypatch):
    path = tmp_path / "v2.sqlite"
    create_v2(path)
    monkeypatch.setattr(store, "fields", lambda *args: (_ for _ in ()).throw(ValueError("tokenizer failure")))
    with pytest.raises(ValueError, match="tokenizer failure"):
        store.connect(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert "search_title" not in {r[1] for r in db.execute("PRAGMA table_info(chunks)")}
        assert db.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'note'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_graph_preview_uses_explicit_strength_and_only_strong_sources(monkeypatch):
    from research_agent.graph.builder import ResearchGraph
    from research_agent.services import early_answer

    weak = {"retrieval_method": ranking.METHOD, "retrieval_score": 0.03, "strong_match": False}
    strong = {"retrieval_method": ranking.METHOD, "retrieval_score": 0.025, "strong_match": True}
    services = SimpleNamespace(
        config={"context_workflow_version": 1, "early_draft_version": 1}, preview_task=None
    )
    preview = AsyncMock()
    monkeypatch.setattr(early_answer, "write_preview", preview)
    retrieve = AsyncMock(return_value={"local_sources": [weak]})
    monkeypatch.setattr(rag, "retrieve", retrieve)
    graph = ResearchGraph(services)
    await graph.retrieve_local({"understanding": {}})
    assert services.preview_task is None
    retrieve.return_value = {"local_sources": [weak, strong]}
    await graph.retrieve_local({"understanding": {}})
    await services.preview_task
    preview.assert_awaited_once_with(services, [strong], {})
