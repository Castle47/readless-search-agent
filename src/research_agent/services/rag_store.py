"""Versioned SQLite RAG storage. Source IDs and citation offsets survive migration."""

import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .rag_chunking import VERSION as CHUNKING_VERSION
from .rag_english import MAX_CHARS, SENTENCIZER_VERSION, WORD_LIMIT, WORD_OVERLAP
from .rag_lexical import TOKENIZER_VERSION, fields, query_terms

SCHEMA_VERSION = 3
MODEL = "nomic-embed-text"
DIMENSIONS = 768
EMBEDDING_VERSION = "search_document:v1"
LEGACY_INDEX_SIGNATURE = "text-v2:chars1800:overlap200:" + MODEL + ":768:" + EMBEDDING_VERSION
INDEX_SIGNATURE = (
    f"text-v4:{CHUNKING_VERSION}:chars1800:overlap200:en-words{WORD_LIMIT}:en-overlap{WORD_OVERLAP}:"
    f"maxchars{MAX_CHARS}:{SENTENCIZER_VERSION}:{MODEL}:768:{EMBEDDING_VERSION}"
)


@contextmanager
def writer_lock(path):
    with open(str(path) + ".writer.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("This corpus is already being indexed; retry after it finishes") from error
        yield


def now():
    return datetime.now(UTC).isoformat()


def text_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def embedding_identity(path, text):
    # Hash the actual model input, not the containing file's revision or offset.
    input_hash = text_hash("search_document: " + path + "\n" + text)
    key = text_hash(f"{MODEL}:{DIMENSIONS}:{EMBEDDING_VERSION}:{input_hash}")
    return key, input_hash


def pack_vector(vector):
    if len(vector) != DIMENSIONS or not all(math.isfinite(x) for x in vector):
        raise ValueError("Invalid embedding shape")
    try:
        return struct.pack(f"<{DIMENSIONS}f", *vector)
    except (OverflowError, struct.error) as error:
        raise ValueError("Embedding cannot be encoded as float32") from error


def unpack_vector(blob):
    if len(blob) != DIMENSIONS * 4:
        raise ValueError("Invalid embedding byte length")
    vector = struct.unpack(f"<{DIMENSIONS}f", blob)
    if not all(math.isfinite(x) for x in vector):
        raise ValueError("Invalid embedding values")
    return vector


DDL = [
    """CREATE TABLE files (
        path TEXT PRIMARY KEY NOT NULL,
        revision TEXT NOT NULL,
        size_bytes INTEGER,
        mtime_ns INTEGER,
        status TEXT NOT NULL CHECK(status IN ('indexed','indexing','failed')),
        error TEXT,
        title TEXT NOT NULL,
        source_type TEXT NOT NULL,
        article_id TEXT,
        article_revision INTEGER,
        published INTEGER CHECK(published IN (0,1)),
        provenance_json TEXT NOT NULL DEFAULT '{}',
        index_signature TEXT NOT NULL,
        indexed_at TEXT,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE embedding_cache (
        id TEXT PRIMARY KEY NOT NULL,
        input_hash TEXT NOT NULL,
        model TEXT NOT NULL,
        input_version TEXT NOT NULL,
        dimensions INTEGER NOT NULL CHECK(dimensions > 0),
        vector BLOB NOT NULL CHECK(typeof(vector) = 'blob' AND length(vector) = dimensions * 4),
        created_at TEXT NOT NULL,
        UNIQUE(input_hash, model, input_version, dimensions)
    )""",
    """CREATE TABLE chunks (
        rowid INTEGER PRIMARY KEY,
        id TEXT NOT NULL UNIQUE,
        path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
        chunk_index INTEGER NOT NULL,
        location TEXT NOT NULL,
        start_char INTEGER NOT NULL CHECK(start_char >= 0),
        end_char INTEGER NOT NULL CHECK(end_char >= start_char),
        page_no INTEGER,
        line_no INTEGER,
        text TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        embedding_id TEXT NOT NULL REFERENCES embedding_cache(id),
        search_text TEXT NOT NULL,
        search_path TEXT NOT NULL,
        search_title TEXT NOT NULL,
        UNIQUE(path, chunk_index)
    )""",
    "CREATE INDEX chunks_content_hash ON chunks(content_hash)",
    "CREATE INDEX chunks_embedding_id ON chunks(embedding_id)",
    "CREATE INDEX files_status ON files(status)",
    "CREATE INDEX files_article_id ON files(article_id)",
    "CREATE INDEX files_source_type ON files(source_type)",
    "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)",
    "CREATE VIRTUAL TABLE chunks_fts USING fts5(search_path, search_title, search_text, content='chunks', content_rowid='rowid')",
    """CREATE TRIGGER chunks_insert AFTER INSERT ON chunks BEGIN
        INSERT INTO chunks_fts(rowid, search_path, search_title, search_text)
        VALUES (new.rowid, new.search_path, new.search_title, new.search_text);
    END""",
    """CREATE TRIGGER chunks_delete AFTER DELETE ON chunks BEGIN
        INSERT INTO chunks_fts(chunks_fts, rowid, search_path, search_title, search_text)
        VALUES ('delete', old.rowid, old.search_path, old.search_title, old.search_text);
    END""",
    """CREATE TRIGGER chunks_update AFTER UPDATE ON chunks BEGIN
        INSERT INTO chunks_fts(chunks_fts, rowid, search_path, search_title, search_text)
        VALUES ('delete', old.rowid, old.search_path, old.search_title, old.search_text);
        INSERT INTO chunks_fts(rowid, search_path, search_title, search_text)
        VALUES (new.rowid, new.search_path, new.search_title, new.search_text);
    END""",
]


def put_embedding(db, path, text, vector):
    key, input_hash = embedding_identity(path, text)
    blob = pack_vector(vector)
    db.execute(
        "INSERT OR IGNORE INTO embedding_cache VALUES (?,?,?,?,?,?,?)",
        (key, input_hash, MODEL, EMBEDDING_VERSION, DIMENSIONS, blob, now()),
    )
    return key


def put_file(
    db, path, revision, *, status, stat=None, provenance=None, error=None, signature=INDEX_SIGNATURE
):
    provenance = provenance or {}
    title = provenance.get("title", path)
    source_type = provenance.get("category") or (
        "blog" if provenance.get("article_id") else Path(path).suffix[1:]
    )
    stamp = now()
    db.execute(
        """INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(path) DO UPDATE SET revision=excluded.revision, size_bytes=excluded.size_bytes,
        mtime_ns=excluded.mtime_ns, status=excluded.status, error=excluded.error, title=excluded.title,
        source_type=excluded.source_type, article_id=excluded.article_id,
        article_revision=excluded.article_revision, published=excluded.published,
        provenance_json=excluded.provenance_json, index_signature=excluded.index_signature,
        indexed_at=CASE WHEN excluded.status='indexed' THEN excluded.indexed_at ELSE files.indexed_at END,
        updated_at=excluded.updated_at""",
        (
            path,
            revision,
            stat.st_size if stat else None,
            stat.st_mtime_ns if stat else None,
            status,
            error,
            title,
            source_type,
            provenance.get("article_id"),
            provenance.get("article_revision"),
            int(provenance["published"]) if "published" in provenance else None,
            json.dumps(provenance, ensure_ascii=False),
            signature,
            stamp if status == "indexed" else None,
            stamp,
        ),
    )


def put_chunk(db, *, chunk_id, path, index, location, text, embedding_id, title):
    search_path, search_title, search_text = fields(path, title, text)
    span = re.search(r"chars:(\d+)-(\d+)", location)
    if not span or int(span[2]) - int(span[1]) != len(text):
        raise ValueError("Chunk location differs from source text")
    page = re.search(r"(?:^|;)page:(\d+)", location)
    line = re.search(r"(?:^|;)line:(\d+)", location)
    db.execute(
        """INSERT INTO chunks
        (id,path,chunk_index,location,start_char,end_char,page_no,line_no,text,content_hash,embedding_id,search_text,search_path,search_title)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            chunk_id,
            path,
            index,
            location,
            int(span[1]),
            int(span[2]),
            int(page[1]) if page else None,
            int(line[1]) if line else None,
            text,
            text_hash(text),
            embedding_id,
            search_text,
            search_path,
            search_title,
        ),
    )


def _migrate_legacy(db):
    db.execute("ALTER TABLE chunks RENAME TO legacy_chunks")
    cache_exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='embedding_cache'").fetchone()
    if cache_exists:
        db.execute("ALTER TABLE embedding_cache RENAME TO legacy_embedding_cache")
    for statement in DDL:
        db.execute(statement)
    version = db.execute("SELECT value FROM metadata WHERE key='text_index_version'").fetchone()
    signature = LEGACY_INDEX_SIGNATURE if version and version[0] == "2" else "legacy:needs-refresh"
    for file in db.execute(
        "SELECT path, min(revision) revision, count(DISTINCT revision) versions FROM legacy_chunks GROUP BY path"
    ).fetchall():
        if file["versions"] != 1:
            raise ValueError("Legacy index has mixed file revisions")
        put_file(db, file["path"], file["revision"], status="indexed", signature=signature)
        for i, row in enumerate(
            db.execute("SELECT * FROM legacy_chunks WHERE path=? ORDER BY rowid", (file["path"],))
        ):
            key = put_embedding(db, row["path"], row["text"], json.loads(row["vector"]))
            put_chunk(
                db,
                chunk_id=row["id"],
                path=row["path"],
                index=i,
                location=row["location"],
                text=row["text"],
                embedding_id=key,
                title=row["path"],
            )
    # Cache-only entries have no recoverable model input. They remain in the backup.
    db.execute("DROP TABLE legacy_chunks")
    if cache_exists:
        db.execute("DROP TABLE legacy_embedding_cache")


def _migrate_v2(db):
    # Only lexical projections change: IDs, citations, original text and vectors remain untouched.
    for name in ("chunks_insert", "chunks_delete", "chunks_update"):
        db.execute(f"DROP TRIGGER {name}")
    db.execute("DROP TABLE chunks_fts")
    db.execute("ALTER TABLE chunks ADD COLUMN search_path TEXT NOT NULL DEFAULT ''")
    db.execute("ALTER TABLE chunks ADD COLUMN search_title TEXT NOT NULL DEFAULT ''")
    for row in db.execute(
        "SELECT c.id,c.path,c.text,f.title FROM chunks c JOIN files f ON f.path=c.path"
    ).fetchall():
        search_path, search_title, search_text = fields(row["path"], row["title"], row["text"])
        db.execute(
            "UPDATE chunks SET search_path=?,search_title=?,search_text=? WHERE id=?",
            (search_path, search_title, search_text, row["id"]),
        )
    for statement in DDL:
        if statement.startswith(("CREATE VIRTUAL TABLE", "CREATE TRIGGER")):
            db.execute(statement)
    db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise ValueError("RAG index was created by a newer schema")
        if version != SCHEMA_VERSION:
            legacy = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks'").fetchone()
            if legacy:
                # SQLite backup includes committed WAL data. Never overwrite an earlier backup.
                backup = path.with_name(path.name + f".v{version or 1}-backup.sqlite")
                if not backup.exists():
                    handle, temporary_name = tempfile.mkstemp(prefix=backup.name + ".", dir=path.parent)
                    os.close(handle)
                    temporary = Path(temporary_name)
                    try:
                        target = sqlite3.connect(temporary)
                        try:
                            db.backup(target)
                        finally:
                            target.close()
                        try:
                            os.link(temporary, backup)
                        except FileExistsError:
                            pass
                    finally:
                        temporary.unlink(missing_ok=True)
            db.execute("BEGIN IMMEDIATE")
            try:
                # Another new-code process may have migrated while we waited.
                current = db.execute("PRAGMA user_version").fetchone()[0]
                if current > SCHEMA_VERSION:
                    raise ValueError("RAG index was created by a newer schema")
                if current != SCHEMA_VERSION:
                    if current == 2:
                        _migrate_v2(db)
                    elif legacy:
                        _migrate_legacy(db)
                    else:
                        for statement in DDL:
                            db.execute(statement)
                    db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    db.execute(
                        "INSERT OR REPLACE INTO metadata VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
                    )
                    db.execute(
                        "INSERT OR REPLACE INTO metadata VALUES ('tokenizer_version', ?)",
                        (TOKENIZER_VERSION,),
                    )
                db.commit()
            except BaseException:
                db.rollback()
                raise
        db.execute("PRAGMA journal_mode=WAL")
        return db
    except BaseException:
        db.close()
        raise


def bm25_matches(db, query):
    terms = query_terms(query)
    if not terms:
        return []
    expression = " OR ".join('"' + term + '"' for term in terms)
    # FTS5's negative BM25 values sort ascending (smaller is better).
    # Do not apply LIMIT before source/permission validation in the caller.
    return [
        dict(row)
        for row in db.execute(
            """SELECT c.id, bm25(chunks_fts, 0.25, 3.0, 1.0) AS bm25_score
        FROM chunks_fts JOIN chunks c ON c.rowid=chunks_fts.rowid
        JOIN files f ON f.path=c.path WHERE chunks_fts MATCH ? AND f.status='indexed'
        ORDER BY bm25_score, c.id""",
            (expression,),
        )
    ]


def keyword_matches(db, query):
    return {row["id"] for row in bm25_matches(db, query)}
