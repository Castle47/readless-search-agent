import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from research_agent.config import Settings
from research_agent.services import private_rag
from research_agent.services.folder_rag import index_folder, search_folders


@pytest.mark.asyncio
async def test_selected_folders_retrieve_and_removal_excludes_existing_index(tmp_path, monkeypatch):
    root = tmp_path / "folders"
    root.mkdir()
    folder_id = str(uuid4())
    source = root / folder_id
    source.mkdir()
    (source / "notes.md").write_text("Kev CUDA Graph kernel acceleration local folder evidence")
    manifest = {"folders": [{"id": folder_id, "name": "研究资料", "enabled": True}]}
    (root / "manifest.json").write_text(json.dumps(manifest))
    settings = Settings(user_folders_path=root, rag_index=tmp_path / "index/vault.sqlite")

    async def embed(settings, texts, query=False):
        return [[1.0] + [0.0] * 767 for _ in texts]

    monkeypatch.setattr(private_rag, "embed", embed)
    assert (await index_folder(settings, folder_id))["files"] == 1
    hits = await search_folders(settings, "Kev CUDA Graph")
    assert hits and hits[0]["folder"]["relative_path"] == "notes.md"
    assert hits[0]["readless"]["title"] == "研究资料 / notes.md"
    captured = []

    async def saved(*args):
        return None

    async def commit(*args):
        return None

    async def warn(*args):
        pass

    async def evidence(page):
        captured.append(page)
        return str(uuid4())

    services = SimpleNamespace(
        settings=settings,
        request=SimpleNamespace(query="Kev CUDA Graph"),
        db=SimpleNamespace(saved_value=saved, commit_value=commit),
        run_id=str(uuid4()),
        fence=1,
        warn=warn,
        evidence=SimpleNamespace(source=evidence),
    )
    result = await private_rag.retrieve(
        services, {"query_rewrite": {"scope": "hybrid", "local_query": "Kev"}}
    )
    assert result["local_sources"] and captured[0].metadata["folder"]["id"] == folder_id
    assert "kernel acceleration" in captured[0].text
    (source / "notes.md").write_text("changed content")
    assert await search_folders(settings, "Kev CUDA Graph") == []
    await index_folder(settings, folder_id)
    manifest["folders"][0]["enabled"] = False
    (root / "manifest.json").write_text(json.dumps(manifest))
    assert await search_folders(settings, "Kev CUDA Graph") == []
