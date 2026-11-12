import asyncio
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from research_agent.services import private_rag as rag
from research_agent.services import rag_store as store

VECTOR = [1.0] + [0.0] * 767


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    settings = SimpleNamespace(vault_path=root, rag_index=tmp_path / "index.sqlite")
    calls = []

    async def embed(settings, texts, query=False):
        if not query:
            calls.extend(texts)
        return [VECTOR for _ in texts]

    monkeypatch.setattr(rag, "embed", embed)
    return settings, calls


def legacy_index(path, text="缓存 prefix", vector=VECTOR):
    revision = hashlib.sha256(text.encode()).hexdigest()
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE chunks(id TEXT PRIMARY KEY,path TEXT,revision TEXT,location TEXT,text TEXT,vector TEXT);
            CREATE TABLE embedding_cache(id TEXT PRIMARY KEY,vector TEXT);
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
            INSERT INTO metadata VALUES('text_index_version','2');
            INSERT INTO metadata VALUES('report','{"files":1}');
        """)
        db.execute(
            "INSERT INTO chunks VALUES(?,?,?,?,?,?)",
            (
                "stable-citation-id",
                "note.md",
                revision,
                f"text;chars:0-{len(text)};line:1",
                text,
                json.dumps(vector),
            ),
        )
        db.execute("INSERT INTO embedding_cache VALUES(?,?)", ("stable-citation-id", json.dumps(vector)))
    return revision


@pytest.mark.asyncio
async def test_migration_preserves_citations_and_is_searchable_without_embedding(corpus):
    settings, calls = corpus
    (settings.vault_path / "note.md").write_text("缓存 prefix")
    revision = legacy_index(settings.rag_index)
    db = store.connect(settings.rag_index)
    try:
        row = db.execute("SELECT * FROM chunks").fetchone()
        assert row["id"] == "stable-citation-id"
        assert row["start_char"] == 0 and row["end_char"] == len("缓存 prefix") and row["line_no"] == 1
        assert db.execute("SELECT revision FROM files").fetchone()[0] == revision
        assert db.execute("SELECT length(vector),typeof(vector) FROM embedding_cache").fetchone()[:] == (
            3072,
            "blob",
        )
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert store.keyword_matches(db, "缓存 prefix") == {"stable-citation-id"}
        assert db.execute("SELECT value FROM metadata WHERE key='report'").fetchone()[0] == '{"files":1}'
    finally:
        db.close()
    backup = settings.rag_index.with_name("index.sqlite.v1-backup.sqlite")
    with sqlite3.connect(backup) as old:
        assert old.execute("SELECT vector FROM chunks").fetchone()[0] == json.dumps(VECTOR)
    saved_backup = backup.read_bytes()
    db = store.connect(settings.rag_index)
    db.close()
    assert backup.read_bytes() == saved_backup
    hits = await rag.search(settings, "prefix", query_vector=VECTOR)
    assert hits[0]["id"] == "stable-citation-id" and hits[0]["revision"] == revision
    await rag.index_vault(settings)
    assert calls == []


def test_migration_failure_rolls_back_and_preserves_backup(tmp_path):
    path = tmp_path / "legacy.sqlite"
    legacy_index(path, vector=[1.0])
    with pytest.raises(ValueError, match="embedding shape"):
        store.connect(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT vector FROM chunks").fetchone()[0] == "[1.0]"
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='files'").fetchone()
    assert path.with_name("legacy.sqlite.v1-backup.sqlite").exists()


@pytest.mark.asyncio
async def test_incremental_reuses_unchanged_chunks_and_cleans_deletions(corpus):
    settings, calls = corpus
    path = settings.vault_path / "note.md"
    original = "A" * 1800 + "旧" * 1400
    path.write_text(original)
    first = await rag.index_vault(settings)
    assert first["embedded_chunks"] == 2
    assert (await rag.index_vault(settings))["embedded_chunks"] == 0
    calls.clear()
    path.write_text(original[:-1] + "新")
    updated = await rag.index_vault(settings)
    assert updated["embedded_chunks"] == 1 and updated["reused_chunks"] == 1
    assert len(calls) == 1
    db = store.connect(settings.rag_index)
    try:
        assert db.execute("SELECT count(*) FROM embedding_cache").fetchone()[0] == 2
        assert db.execute("SELECT content_hash FROM chunks ORDER BY chunk_index").fetchone()[
            0
        ] == store.text_hash("A" * 1800)
    finally:
        db.close()
    path.unlink()
    assert (await rag.index_vault(settings))["chunks"] == 0
    db = store.connect(settings.rag_index)
    try:
        for table in ("files", "chunks", "embedding_cache"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert not store.keyword_matches(db, "新")
    finally:
        db.close()


@pytest.mark.asyncio
async def test_file_failure_retries_and_preserves_other_files(corpus, monkeypatch):
    settings, _ = corpus
    note = settings.vault_path / "note.md"
    note.write_text("old")
    (settings.vault_path / "other.md").write_text("other")
    await rag.index_vault(settings)
    note.write_text("replacement")
    original_embed = rag.embed

    async def broken(*args, **kwargs):
        raise ValueError("embedding failed")

    monkeypatch.setattr(rag, "embed", broken)
    report = await rag.index_vault(settings, paths=[note])
    assert len(report["skipped"]) == 1
    db = store.connect(settings.rag_index)
    try:
        row = db.execute("SELECT * FROM files WHERE path='note.md'").fetchone()
        assert row["status"] == "failed" and "embedding failed" in row["error"]
        assert db.execute("SELECT count(*) FROM chunks WHERE path='note.md'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM files").fetchone()[0] == 2
    finally:
        db.close()
    assert {h["path"] for h in await rag.search(settings, "old", query_vector=VECTOR)} == {"other.md"}
    monkeypatch.setattr(rag, "embed", original_embed)
    assert (await rag.index_vault(settings, paths=[note]))["files"] == 1


@pytest.mark.asyncio
async def test_changed_during_embedding_never_published(corpus, monkeypatch):
    settings, _ = corpus
    note = settings.vault_path / "note.md"
    note.write_text("before")

    async def mutate(settings, texts, query=False):
        note.write_text("after")
        return [VECTOR for _ in texts]

    monkeypatch.setattr(rag, "embed", mutate)
    report = await rag.index_vault(settings)
    assert "Source changed" in report["skipped"][0]["reason"]
    assert report["chunks"] == 0
    assert await rag.search(settings, "before", query_vector=VECTOR) == []


@pytest.mark.asyncio
async def test_fulltext_tracks_title_changes_and_deleted_chunks(corpus):
    settings, calls = corpus
    note = settings.vault_path / "note.md"
    note.write_text("注意力 attention")
    provenance = {
        "note.md": {"title": "firsttitle", "article_id": "a", "article_revision": 3, "published": True}
    }
    await rag.index_vault(settings, provenance=provenance)
    calls.clear()
    provenance["note.md"]["title"] = "secondtitle"
    await rag.index_vault(settings, provenance=provenance)
    assert calls == []
    db = store.connect(settings.rag_index)
    try:
        assert store.keyword_matches(db, "注意力")
        assert store.keyword_matches(db, '" OR secondtitle*')
        assert not store.keyword_matches(db, "firsttitle")
        assert store.keyword_matches(db, "note")
        file = db.execute("SELECT * FROM files").fetchone()
        assert file["article_id"] == "a" and file["article_revision"] == 3 and file["published"] == 1
        assert file["size_bytes"] == len(note.read_bytes())
        db.execute("INSERT INTO chunks_fts(chunks_fts,rank) VALUES('integrity-check',1)")
    finally:
        db.close()
    note.write_text("different")
    await rag.index_vault(settings)
    db = store.connect(settings.rag_index)
    try:
        assert not store.keyword_matches(db, "注意力")
        assert store.keyword_matches(db, "different")
    finally:
        db.close()


@pytest.mark.asyncio
async def test_cancellation_and_single_writer_lock(corpus, monkeypatch):
    settings, _ = corpus
    (settings.vault_path / "note.md").write_text("test")
    entered = asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(rag, "embed", blocked)
    task = asyncio.create_task(rag.index_vault(settings))
    await entered.wait()
    with pytest.raises(ValueError, match="already being indexed"):
        await rag.index_vault(settings)
    assert await rag.search(settings, "test", query_vector=VECTOR) == []
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    db = store.connect(settings.rag_index)
    try:
        assert db.execute("SELECT status FROM files").fetchone()[0] == "indexing"
    finally:
        db.close()


def test_model_identity_and_float32_validation():
    assert store.embedding_identity("note.md", "same") != store.embedding_identity("other.md", "same")
    assert store.unpack_vector(store.pack_vector(VECTOR)) == tuple(VECTOR)
    for vector in ([1.0], [float("nan")] * 768, [float("inf")] * 768, [1e100] * 768):
        with pytest.raises(ValueError):
            store.pack_vector(vector)


@pytest.mark.asyncio
async def test_pdf_page_offsets_are_structured(corpus, monkeypatch):
    settings, _ = corpus
    (settings.vault_path / "paper.pdf").write_bytes(b"fixture")
    monkeypatch.setattr(rag, "segments", lambda path: iter([("page:2", "page text")]))
    await rag.index_vault(settings)
    db = store.connect(settings.rag_index)
    try:
        row = db.execute("SELECT * FROM chunks").fetchone()
        assert row["page_no"] == 2 and row["line_no"] is None
        assert row["start_char"] == 0 and row["end_char"] == 9
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM embedding_cache")
    finally:
        db.close()


@pytest.mark.asyncio
async def test_vacuum_preserves_fulltext_row_identity(corpus):
    settings, _ = corpus
    for name in ("a.md", "b.md", "c.md"):
        (settings.vault_path / name).write_text(name + " attention")
    await rag.index_vault(settings)
    db = store.connect(settings.rag_index)
    try:
        with db:
            db.execute("DELETE FROM files WHERE path='a.md'")
        before = db.execute("SELECT rowid,id FROM chunks ORDER BY rowid").fetchall()
        db.execute("VACUUM")
        assert db.execute("SELECT rowid,id FROM chunks ORDER BY rowid").fetchall() == before
        assert store.keyword_matches(db, "attention") == {r["id"] for r in before}
        db.execute("INSERT INTO chunks_fts(chunks_fts,rank) VALUES('integrity-check',1)")
    finally:
        db.close()
