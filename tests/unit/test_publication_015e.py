from copy import deepcopy
from pathlib import Path
import json
import pytest
from research_agent.services.publication_identity import publication_proof
from research_agent.services.provenance import valid_stored_identity, resolve_identity
from research_agent.services.conversation import context_urls
from research_agent.services.admission import validate_intro_scope
from research_agent.services.sections import retained_bibliography
from research_agent.contracts import ResearchRequest, SourceIdentity
from types import SimpleNamespace

FROZEN = Path(__file__).parents[1] / "fixtures/frozen/transformer-retry"


def paper_sources():
    data = json.loads((FROZEN / "sources.json").read_text())
    return next(s for s in data if s["canonical_url"] == "https://arxiv.org/abs/1706.03762"), next(
        s for s in data if s["canonical_url"] == "https://arxiv.org/html/1706.03762v7"
    )


def test_frozen_record_and_observed_fulltext_link_establish_work_but_not_named_author():
    record, full = paper_sources()
    proof = publication_proof(full, record)
    assert valid_stored_identity(proof, full, record)
    assert proof["publisher_name"] == "Attention Is All You Need"
    assert "uncaptured author names" in proof["identity_scope"]
    assert record["content_text"][proof["start_char"] : proof["end_char"]] == proof["quote"]
    assert proof["link_url"] == full["canonical_url"]
    for mutation in ["wrong_work", "wrong_version", "missing_link", "wrong_hash", "wrong_content"]:
        a, b = deepcopy(record), deepcopy(full)
        if mutation == "wrong_work":
            b["canonical_url"] = "https://arxiv.org/html/1706.03763v7"
        if mutation == "wrong_version":
            b["canonical_url"] = "https://arxiv.org/html/1706.03762v1"
        if mutation == "missing_link":
            a["metadata"]["reference_links"] = []
        if mutation == "wrong_hash":
            a["content_sha256"] = "changed"
        if mutation == "wrong_content":
            b["content_text"] = "Temporarily unavailable"
        assert not valid_stored_identity(proof, b, a), mutation


def test_copyright_or_copied_title_cannot_establish_work_authorship():
    record, full = paper_sources()
    assert publication_proof(full, full) is None
    fake = {**record, "canonical_url": "https://arxiv.org.evil.example/abs/1706.03762"}
    assert publication_proof(full, fake) is None
    quote = "Google hereby grants permission to reproduce the tables and figures."
    identity = SourceIdentity(
        basis="original_publication",
        publisher_name="Google",
        context_id="I0",
        quote=quote,
        link_id=None,
        explanation="License",
    )
    assert (
        resolve_identity(
            identity,
            {"contexts": [{"context_id": "I0", "start_char": 0, "text": quote}], "official_links": []},
            "s",
            SimpleNamespace(content_sha256="x"),
        )
        is None
    )


def test_authors_are_preserved_from_actual_html_fields_only():
    kept, fields = retained_bibliography(
        '<meta name="citation_author" content="Alice A"><meta name="citation_author" content="Bob B"><p>Body</p>',
        "Body",
        2500,
    )
    assert kept == ["Alice A", "Bob B"] and all(f["field"] == "author" for f in fields)
    assert retained_bibliography("<footer>Copyright Example</footer>", "Body", 2500) == ([], [])


def test_old_assistant_bibliography_never_enters_user_source_allowlist():
    req = json.loads((FROZEN / "manifest.json").read_text())["request"]
    assert context_urls(req["context"]) == set()
    assert context_urls(
        "用户：参考 https://docs.example.org/api\n上次回答（作为对话背景，事实仍需核验）：https://wrong.example/"
    ) == {"https://docs.example.org/api"}


def test_intro_scope_cannot_invent_mandatory_model_history():
    with pytest.raises(ValueError):
        validate_intro_scope(
            ResearchRequest(query="介绍一下原生 Transformer 模型"), ["它与后来的语言模型是什么关系？"]
        )
    validate_intro_scope(
        ResearchRequest(query="介绍一下原生 Transformer 模型"), ["它是什么，核心机制是什么？"]
    )
    validate_intro_scope(
        ResearchRequest(query="介绍一下 Transformer 的发展史及后续模型"), ["后续模型是什么？"]
    )
