"""Index published original blog revisions with a read-only source mount."""

import hashlib
import json


def corpus_settings(settings):
    return settings.model_copy(
        update={
            "vault_path": settings.rag_index.parent / "readless-sources",
            "rag_index": settings.rag_index.parent / "readless.sqlite",
        }
    )


def export_blogs(settings):
    root = settings.readless_articles_path.resolve(strict=True)
    target = corpus_settings(settings).vault_path
    target.mkdir(parents=True, exist_ok=True)
    manifest = {}
    sources = {metadata.parent.name: metadata for metadata in root.glob("*/article.json")}
    cloud = settings.readless_cloud_path
    if cloud.exists():
        sources.update({metadata.parent.name: metadata for metadata in cloud.glob("*/article.json")})
    for metadata in sorted(sources.values()):
        root = metadata.parent.parent.resolve(strict=True)
        if metadata.is_symlink() or not metadata.resolve().is_relative_to(root):
            continue
        article = json.loads(metadata.read_text())
        if article.get("kind") != "authored" or article.get("deletedAt"):
            continue
        if not article.get("published"):
            continue
        article_id = article["id"]
        if article_id != metadata.parent.name:
            raise ValueError("Article identity mismatch")
        revision = (
            article.get("publication", {}).get("revision")
            if article.get("published")
            else article.get("revision")
        )
        if not isinstance(revision, int) or revision < 1:
            continue
        source = metadata.parent / "revisions" / str(revision) / "content.md"
        if source.is_symlink() or not source.resolve().is_relative_to(root):
            raise ValueError("Blog source escapes the read-only corpus")
        content = source.read_text()
        name = article_id + ".md"
        output = target / name
        temporary = output.with_suffix(".tmp")
        temporary.write_text("# " + article["title"] + "\n\n" + content)
        temporary.replace(output)
        manifest[name] = {
            "article_id": article_id,
            "article_revision": revision,
            "title": article["title"] + ("" if article.get("published") else "（未发布草稿）"),
            "published": bool(article.get("published")),
            "source_path": str(source.relative_to(root)),
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "origin": {
                **article.get("ragOrigin", {}),
                "kind": "readless-cloud"
                if root == settings.readless_cloud_path.resolve()
                else "readless-local",
            },
            "href": article["href"],
        }
    for previous in target.glob("*.md"):
        if previous.name not in manifest:
            previous.unlink()
    temporary = target / "manifest.tmp"
    temporary.write_text(json.dumps(manifest, ensure_ascii=False))
    temporary.replace(target / "manifest.json")
    return manifest


async def index_blogs(settings):
    from .private_rag import index_vault

    manifest = export_blogs(settings)
    report = await index_vault(corpus_settings(settings), provenance=manifest)
    return {
        **report,
        "indexed_blogs": len(manifest),
        "published_blogs": sum(item["published"] for item in manifest.values()),
        "draft_blogs": sum(not item["published"] for item in manifest.values()),
    }


async def search_blogs(settings, query, limit=5, *, query_vector=None):
    from .private_rag import search

    corpus = corpus_settings(settings)
    manifest_path = corpus.vault_path / "manifest.json"
    if not manifest_path.exists():
        return []
    manifest = json.loads(manifest_path.read_text())

    def current(name):
        provenance = manifest[name]
        source_root = (
            settings.readless_cloud_path
            if provenance.get("origin", {}).get("kind") == "readless-cloud"
            else settings.readless_articles_path
        )
        source = source_root / provenance["source_path"]
        metadata = source_root / provenance["article_id"] / "article.json"
        try:
            article = json.loads(metadata.read_text())
            revision = (
                article.get("publication", {}).get("revision")
                if article.get("published")
                else article.get("revision")
            )
            return bool(
                article.get("published")
                and not article.get("deletedAt")
                and revision == provenance["article_revision"]
                and bool(article.get("published")) == provenance["published"]
                and source.resolve().is_relative_to(source_root.resolve())
                and hashlib.sha256(source.read_bytes()).hexdigest() == provenance["source_sha256"]
            )
        except (OSError, ValueError):
            return False

    allowed = {name for name in manifest if current(name)}
    hits = await search(
        corpus,
        query,
        limit,
        query_vector=query_vector,
        titles={name: item["title"] for name, item in manifest.items()},
        allowed_paths=allowed,
    )
    current_hits = []
    for hit in hits:
        if hit["path"] in allowed and current(hit["path"]):
            hit["readless"] = manifest[hit["path"]]
            current_hits.append(hit)
    return current_hits
