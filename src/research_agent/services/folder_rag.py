"""User-selected local folder snapshots; only enabled folders enter new research."""

import asyncio
import json
import re

from . import private_rag


def folders(settings):
    try:
        rows = json.loads((settings.user_folders_path / "manifest.json").read_text())["folders"]
    except FileNotFoundError:
        return []
    return [row for row in rows if re.fullmatch(r"[a-f0-9-]{36}", row.get("id", ""))]


def corpus(settings, folder_id):
    if not re.fullmatch(r"[a-f0-9-]{36}", folder_id):
        raise ValueError("Invalid folder ID")
    return settings.model_copy(
        update={
            "vault_path": settings.user_folders_path / folder_id,
            "rag_index": settings.rag_index.parent / ("folder-" + folder_id + ".sqlite"),
        }
    )


async def index_folder(settings, folder_id):
    if not any(row["id"] == folder_id for row in folders(settings)):
        raise ValueError("Folder no longer registered")
    return await private_rag.index_vault(corpus(settings, folder_id))


def has_indexes(settings):
    return any(
        row.get("enabled") and corpus(settings, row["id"]).rag_index.exists() for row in folders(settings)
    )


async def search_folders(settings, query, query_vector=None, *, limit=5):
    async def one(row):
        hits = await private_rag.search(
            corpus(settings, row["id"]), query, limit=limit, query_vector=query_vector
        )
        for hit in hits:
            hit["folder"] = {"id": row["id"], "name": row["name"], "relative_path": hit["path"]}
            hit["path"] = "folders/" + row["id"] + "/" + hit["path"]
            hit["readless"] = {"title": row["name"] + " / " + hit["folder"]["relative_path"]}
        return hits

    groups = await asyncio.gather(*(one(row) for row in folders(settings) if row.get("enabled")))
    # A folder removed while retrieval ran must not contribute to a new result.
    active = {row["id"] for row in folders(settings) if row.get("enabled")}
    return [hit for group in groups for hit in group if hit["folder"]["id"] in active]
