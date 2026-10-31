import json
from pathlib import Path
from types import SimpleNamespace
from research_agent.adapters.llm import LLM
from research_agent.contracts import ResearchRequest
from research_agent.config import Settings
from research_agent.services.documents import work_identity, same_work, redundant_view
from research_agent.services.conversation import understanding_payload
from research_agent.services.admission import fallback_understanding
from research_agent.graph.source_researcher import research_reference, material_progress


def test_paper_identity_preserves_views_editions_and_rejects_lookalike_hosts():
    a = work_identity("https://arxiv.org/abs/1706.03762")
    b = work_identity("https://arxiv.org/html/1706.03762v7")
    c = work_identity("https://ar5iv.labs.arxiv.org/html/1706.03762")
    assert same_work(a, b) and same_work(b, c)
    assert redundant_view(a, b)
    assert not redundant_view(work_identity("https://arxiv.org/html/1706.03762v1"), b)
    assert not same_work(a, work_identity("https://arxiv.org.evil.example/abs/1706.03762"))
    assert not same_work(a, work_identity("https://arxiv.org/abs/1706.03763"))
    assert work_identity("https://arxiv.org/pdf/hep-th/9901001v2.pdf")["edition"] == "v2"


def test_doi_metadata_is_own_page_citation_not_a_bibliography_scan():
    d = work_identity(
        "https://journal.example/article",
        '<meta name="citation_doi" content="10.1000/Abc"><p>References: 10.1000/Other</p>',
    )
    assert d["identifiers"] == ["doi:10.1000/abc"]
    assert not work_identity(
        "https://journal.example/article", '<a href="https://doi.org/10.1000/Other">Reference</a>'
    )["identifiers"]
    assert not work_identity(
        "https://journal.example/article",
        '<meta name="citation_doi" content="10.1000/a"><meta name="citation_doi" content="10.1000/b">',
    )["identifiers"]


def test_fulltext_link_not_host_navigation_is_research_material():
    base = "https://arxiv.org/abs/1706.03762"
    for label, url in [
        ("arXiv system status", "https://status.arxiv.org/"),
        ("Donate", "https://info.arxiv.org/about/donate.html"),
        ("About arXiv", "https://info.arxiv.org/about/index.html"),
    ]:
        assert not research_reference(base, {"label": label, "url": url}, {"arxiv.org"})
    assert research_reference(
        base,
        {"label": "HTML (experimental)", "url": "https://arxiv.org/html/1706.03762v7", "full_text": True},
        set(),
    )


async def test_actual_long_history_keeps_roles_and_fits_authorized_input():
    request = json.loads(
        (Path(__file__).parents[1] / "fixtures/frozen/transformer-diagnosis/run.json").read_text()
    )["request"]
    req = ResearchRequest.model_validate_json(json.dumps(request))
    settings = Settings(secrets_file=Path("/nonexistent/offline"))
    llm = LLM(settings, SimpleNamespace(config=settings.snapshot()))
    try:
        payload, omitted = understanding_payload(req, llm)
        assert not omitted
        assert [m["role"] for m in payload["history"]] == ["user", "assistant"]
        assert (
            "Jev" in payload["history"][0]["content"]
            and "Transformer" not in payload["history"][0]["content"]
        )
        assert payload["query"] == req.query
        assert req.context not in " ".join(fallback_understanding(req).assumptions)
        assert len(" ".join(fallback_understanding(req).assumptions)) < len(req.context)
    finally:
        await llm.close()


def test_capacity_packing_only_omits_assistant_prose_and_retains_user_constraint():
    from research_agent.errors import Stopped

    class Limited:
        def build(self, role, payload, schema):
            if any(m["role"] == "assistant" and m["content"] for m in payload["history"]):
                raise Stopped("input_budget_exceeded")

    req = ResearchRequest(
        query="进一步解释",
        context=json.dumps(
            {
                "format": "readless-conversation-v1",
                "messages": [
                    {"role": "user", "content": "请用中文，说明训练与推理的区别。"},
                    {"role": "assistant", "content": "old long answer"},
                ],
            },
            ensure_ascii=False,
        ),
    )
    payload, omitted = understanding_payload(req, Limited())
    assert omitted == [1] and payload["history"][0]["content"] == "请用中文，说明训练与推理的区别。"
    assert payload["query"] == req.query and "old long answer" in req.context


def test_empty_notes_and_same_work_duplicate_views_do_not_count_as_new_progress():
    sources = {
        "a": {"canonical_url": "https://arxiv.org/abs/1706.03762"},
        "b": {"canonical_url": "https://arxiv.org/html/1706.03762v7"},
    }
    assert (
        material_progress([{"source_id": "a", "passages": [], "notes": "Navigation was read"}], sources) == []
    )
    notes = [{"source_id": sid, "passages": [{"text": "Same original words"}]} for sid in sources]
    assert len(material_progress(notes, sources)) == 1
    notes[1]["passages"].append({"text": "The full text explains the mechanism."})
    assert len(material_progress(notes, sources)) == 2


def test_same_work_doi_across_publishers_is_not_independent_corroboration():
    from research_agent.storage.evidence import independent_origins

    a = {
        "publisher_key": "preprint.example",
        "content_sha256": "a",
        "metadata": {"document": {"identifiers": ["doi:10.1000/abc"]}},
    }
    b = {
        "publisher_key": "journal.example",
        "content_sha256": "b",
        "metadata": {"document": {"identifiers": ["doi:10.1000/abc"]}},
    }
    c = {"publisher_key": "different.example", "content_sha256": "c"}
    assert independent_origins([a, b]) == 1
    assert independent_origins([a, b, c]) == 2
