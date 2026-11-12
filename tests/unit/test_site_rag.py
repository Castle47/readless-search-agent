import hashlib
import json
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from research_agent.config import Settings
from research_agent.services import private_rag
from research_agent.services.site_rag import article_text, index_site, search_site


def test_html_excludes_navigation_scripts_but_keeps_original_translation_tables_code():
    text = article_text(
        "<nav>ignore navbar</nav><article><header>ignore tools</header><h2>Title</h2><p>Original Kev</p><p>译文加速</p><table><tr><td>kernel</td><td>2ms</td></tr></table><pre>print(1)</pre><script>evil()</script></article><footer>ignore links</footer>"
    )
    for wanted in ["Original Kev", "译文加速", "kernel", "2ms", "print(1)"]:
        assert wanted in text
    assert "ignore" not in text and "evil" not in text


@pytest.mark.asyncio
async def test_site_categories_index_retrieve_and_revoke_with_frozen_source_checks(tmp_path, monkeypatch):
    source = tmp_path / "site"
    source.mkdir()
    entries = {}
    for category, format, body in [
        ("curated", "html", "<article><p>Kev kernel 原文</p><p>中文译文</p></article>"),
        ("blogs", "md", "# Kev original blog"),
        ("knowledge", "md", "# Kev kernel knowledge"),
    ]:
        name = category + "." + format
        (source / name).write_text(body)
        entries[name] = {
            "id": category + ":test",
            "category": category,
            "title": category,
            "href": "source.html",
            "format": format,
            "sha256": hashlib.sha256(body.encode()).hexdigest(),
        }
    # A blank/scanned PDF is reported, never fabricated into searchable text.
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(source / "paper.pdf")
    entries["paper.pdf"] = {
        "id": "paper:test",
        "category": "papers",
        "title": "Scan",
        "href": "papers.html",
        "format": "pdf",
        "sha256": hashlib.sha256((source / "paper.pdf").read_bytes()).hexdigest(),
    }
    (source / "manifest.json").write_text(json.dumps({"entries": entries}))
    (source / "access.json").write_text(
        json.dumps({"checkedAt": time.time() * 1000, "enabled": list(entries)})
    )
    settings = Settings(
        site_sources_path=source,
        rag_index=tmp_path / "index/vault.sqlite",
        vault_path=tmp_path / "empty",
        readless_articles_path=tmp_path / "articles",
    )

    async def embed(settings, texts, query=False):
        return [[1.0] + [0.0] * 767 for _ in texts]

    monkeypatch.setattr(private_rag, "embed", embed)
    report = await index_site(settings)
    assert report["files"] == 3
    assert report["sources"] == {"curated": 1, "blogs": 1, "knowledge": 1, "papers": 0}
    assert len(report["skipped"]) == 1
    hits = await search_site(settings, "Kev kernel")
    assert {h["readless"]["category"] for h in hits} == {"curated", "blogs", "knowledge"}
    captured = []

    async def saved(*a):
        return None

    async def commit(*a):
        pass

    async def warn(*a):
        pass

    async def evidence(page):
        captured.append(page)
        return str(uuid4())

    services = SimpleNamespace(
        settings=settings,
        request=SimpleNamespace(query="Kev kernel"),
        db=SimpleNamespace(saved_value=saved, commit_value=commit),
        warn=warn,
        evidence=SimpleNamespace(source=evidence),
        run_id=str(uuid4()),
        fence=1,
    )
    result = await private_rag.retrieve(
        services, {"query_rewrite": {"scope": "hybrid", "local_query": "Kev kernel"}}
    )
    assert result["local_sources"]
    assert any(p.metadata["readless"]["category"] == "curated" for p in captured)
    (source / "access.json").write_text(json.dumps({"checkedAt": time.time() * 1000, "enabled": []}))
    assert await search_site(settings, "Kev") == []
    (source / "access.json").write_text(json.dumps({"checkedAt": 0, "enabled": list(entries)}))
    assert await search_site(settings, "Kev") == []


def test_pdf_text_extraction_keeps_bilingual_text_page_numbers_and_original_bytes(tmp_path):
    import pymupdf

    path = tmp_path / "bilingual.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), "Kev kernel inference acceleration")
        page.insert_text((72, 120), "推理加速", fontname="china-s")
        page = document.new_page()
        page.insert_text((72, 72), "CUDA Graph second page")
        document.save(path)
    original = path.read_bytes()
    pages = list(private_rag.segments(path))
    assert len(pages) == 2
    assert pages[0][0].startswith("page:1;")
    assert "推理加速" in pages[0][1]
    assert "CUDA Graph" in pages[1][1]
    assert path.read_bytes() == original
