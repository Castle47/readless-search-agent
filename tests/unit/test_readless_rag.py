import json

import pytest

from research_agent.config import Settings
from research_agent.services import private_rag
from research_agent.services.readless_rag import export_blogs, index_blogs, search_blogs


def article(root, name, text, published=True, revision=1):
    folder = root / name
    snapshot = folder / "revisions" / str(revision)
    snapshot.mkdir(parents=True, exist_ok=True)
    (snapshot / "content.md").write_text(text)
    (folder / "article.json").write_text(
        json.dumps(
            {
                "id": name,
                "title": "Kev inference",
                "kind": "authored",
                "published": published,
                "publication": {"revision": revision},
                "href": name + ".html",
            }
        )
    )


@pytest.mark.asyncio
async def test_original_blog_index_preserves_publication_and_excludes_drafts(tmp_path, monkeypatch):
    local, cloud = tmp_path / "local", tmp_path / "cloud"
    local.mkdir()
    cloud.mkdir()
    article(local, "blog-a", "local old publication")
    article(cloud, "blog-a", "Kev fused inference implementation", revision=2)
    article(local, "blog-draft", "private unfinished material", published=False)
    settings = Settings(
        readless_articles_path=local, readless_cloud_path=cloud, rag_index=tmp_path / "index" / "vault.sqlite"
    )

    async def embed(settings, texts, query=False):
        return [[1.0] + [0.0] * 767 for _ in texts]

    monkeypatch.setattr(private_rag, "embed", embed)
    result = await index_blogs(settings)
    assert result["published_blogs"] == 1
    hits = await search_blogs(settings, "Kev inference")
    assert hits[0]["readless"]["article_revision"] == 2
    assert "fused inference" in hits[0]["text"]
    assert hits[0]["readless"]["source_path"] == "blog-a/revisions/2/content.md"
    article(cloud, "blog-a", "withdrawn", published=False, revision=2)
    assert (await index_blogs(settings))["chunks"] == 0


def test_requested_default_model_uses_high_reasoning():
    settings = Settings()
    assert settings.model == "openai/gpt-6-luna"
    assert set(settings.snapshot()["reasoning_efforts"].values()) == {"high"}


def test_image_payloads_are_not_embedded_and_offsets_are_original():
    text = "真实内容开始\n![图](data:image/png;base64," + "A" * 500000 + ")\n真实内容结尾"
    parts = list(private_rag.chunks(text))
    assert len(parts) == 2
    assert sum(len(value) for _, value in parts) < 100
    assert all(text[start : start + len(value)] == value for start, value in parts)
    assert "真实内容结尾" in parts[-1][1]


def test_cloud_owned_draft_is_not_website_content(tmp_path):
    local, cloud = tmp_path / "local", tmp_path / "cloud"
    local.mkdir()
    cloud.mkdir()
    article(cloud, "blog-draft", "Unverified Kev explanation", published=False, revision=2)
    path = cloud / "blog-draft" / "article.json"
    data = json.loads(path.read_text())
    data.update(revision=2, ragOrigin={"kind": "readless-cloud"})
    path.write_text(json.dumps(data))
    settings = Settings(
        readless_articles_path=local, readless_cloud_path=cloud, rag_index=tmp_path / "index" / "vault.sqlite"
    )
    manifest = export_blogs(settings)
    assert manifest == {}
