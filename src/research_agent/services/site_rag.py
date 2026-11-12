"""Site content snapshots with category provenance and per-run access refresh."""

import hashlib
import json
import time
from pathlib import Path

from lxml import html

from . import private_rag


def corpus(settings):
    return settings.model_copy(
        update={
            "vault_path": settings.rag_index.parent / "site-sources",
            "rag_index": settings.rag_index.parent / "site.sqlite",
        }
    )


def manifest(settings):
    try:
        return json.loads((settings.site_sources_path / "manifest.json").read_text())["entries"]
    except FileNotFoundError:
        return {}


def enabled(settings):
    try:
        value = json.loads((settings.site_sources_path / "access.json").read_text())
        if not 0 <= time.time() * 1000 - value["checkedAt"] <= 15 * 60 * 1000:
            return set()
        return set(value["enabled"])
    except (OSError, ValueError, KeyError):
        return set()


def article_text(source):
    tree = html.fromstring(source)
    candidates = tree.xpath("//article")
    if not candidates:
        raise ValueError("No article body; refusing navigation-only text")
    body = candidates[0]
    for node in body.xpath(".//script|.//style|.//nav|.//header|.//footer|.//button"):
        node.drop_tree()
    for node in body.iter():
        if node.tag in {"p", "div", "section", "h1", "h2", "h3", "h4", "li", "tr", "pre", "br"}:
            node.tail = "\n" + (node.tail or "")
        if node.tag in {"td", "th"}:
            node.tail = " | " + (node.tail or "")
    return body.text_content().strip()


async def index_site(settings):
    target = corpus(settings).vault_path
    target.mkdir(parents=True, exist_ok=True)
    records = manifest(settings)
    active = enabled(settings)
    exported, skipped = {}, []
    for file, entry in records.items():
        if file not in active:
            continue
        try:
            source = settings.site_sources_path / file
            if Path(file).name != file or source.is_symlink():
                raise ValueError("Invalid site snapshot path")
            data = source.read_bytes()
            if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise ValueError("Site snapshot checksum mismatch")
            name = Path(file).stem + (".pdf" if entry["format"] == "pdf" else ".md")
            content = (
                data
                if entry["format"] == "pdf"
                else (
                    "# "
                    + entry["title"]
                    + "\n\n"
                    + (article_text(data) if entry["format"] == "html" else data.decode("utf-8-sig"))
                ).encode()
            )
            (target / name).write_bytes(content)
            exported[name] = {**entry, "snapshot": file}
        except (OSError, ValueError) as error:
            skipped.append({"path": file, "reason": str(error)})
    for old in target.iterdir():
        if old.suffix in {".md", ".pdf"} and old.name not in exported:
            old.unlink()
    (target / "manifest.json").write_text(json.dumps(exported, ensure_ascii=False))
    report = await private_rag.index_vault(corpus(settings), provenance=exported)
    db = private_rag.connect(corpus(settings).rag_index)
    try:
        indexed_paths = {row[0] for row in db.execute("SELECT DISTINCT path FROM chunks")}
    finally:
        db.close()
    return {
        **report,
        "skipped": skipped + report["skipped"],
        "sources": {
            category: sum(e["category"] == category and name in indexed_paths for name, e in exported.items())
            for category in ["curated", "blogs", "knowledge", "papers"]
        },
    }


def has_index(settings):
    return corpus(settings).rag_index.exists()


async def search_site(settings, query, *, query_vector=None, limit=8):
    target = corpus(settings)
    try:
        indexed = json.loads((target.vault_path / "manifest.json").read_text())
    except FileNotFoundError:
        return []
    current, active = manifest(settings), enabled(settings)
    if not active:
        return []
    allowed = {
        name
        for name, entry in indexed.items()
        if entry["snapshot"] in active and current.get(entry["snapshot"], {}).get("sha256") == entry["sha256"]
    }
    hits = await private_rag.search(
        target,
        query,
        limit=limit,
        query_vector=query_vector,
        titles={name: value["title"] for name, value in indexed.items()},
        allowed_paths=allowed,
    )
    # Re-read access after asynchronous retrieval; a revoked source must not reach generation.
    current, active = manifest(settings), enabled(settings)
    result = []
    for hit in hits:
        entry = indexed.get(hit["path"])
        if not entry or entry["snapshot"] not in active:
            continue
        if current.get(entry["snapshot"], {}).get("sha256") != entry["sha256"]:
            continue
        hit["readless"] = {
            "title": entry["title"],
            "category": entry["category"],
            "href": entry["href"],
            "source_id": entry["id"],
            "article_id": entry.get("article_id"),
            "source_sha256": entry["sha256"],
            "metadata_only": entry.get("metadataOnly", False),
            "reference_links": [{"url": entry["href"], "title": entry["title"], "full_text": False}]
            if entry.get("metadataOnly")
            else [],
        }
        hit["path"] = "site/" + hit["path"]
        result.append(hit)
    return result
