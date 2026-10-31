from types import SimpleNamespace
from unittest.mock import AsyncMock

from research_agent.adaptive_contracts import ReadinessReview
from research_agent.contracts import Criterion
from research_agent.services.adaptive_research import material_assessment
from research_agent.services.research_breadth import independent_works, separate_queries


def test_supplement_does_not_overwrite_sparse_source_aliases():
    from copy import deepcopy
    import pytest
    from research_agent.services.adaptive_answer import extend_materials, validate_material_bindings
    from research_agent.errors import InvariantViolation

    def passage(sid, alias, text):
        return {"source_id": sid, "source_alias": alias, "start_char": 0, "end_char": len(text), "text": text}

    old = {
        "sources": {"S0": {"id": "a"}, "S1": {"id": "b"}, "S3": {"id": "c"}},
        "passages": {"P0": passage("c", "S3", "Existing teaching text")},
    }
    new = {
        "sources": {"S0": {"id": "paper"}, "S1": {"id": "c"}},
        "passages": {
            "P0": passage("paper", "S0", "Original paper text"),
            "P1": passage("c", "S1", "Existing teaching text"),
        },
    }
    before = deepcopy(old)
    merged = extend_materials(old, new)
    assert old == before
    assert merged["sources"]["S3"]["id"] == "c"
    assert merged["sources"]["S4"]["id"] == "paper"
    assert merged["passages"]["P0"] == old["passages"]["P0"]
    assert merged["passages"]["P1"]["source_alias"] == "S4"
    broken = deepcopy(merged)
    broken["passages"]["P1"]["source_alias"] = "S3"
    with pytest.raises(InvariantViolation):
        validate_material_bindings(broken)


def test_explicit_complete_bibliographies_bridge_publisher_and_full_paper_without_title_guessing():
    from research_agent.services.documents import work_identity, same_work

    html = '<meta name="citation_title" content="A Test Paper"><meta name="citation_author" content="Family, Given"><meta name="citation_publication_date" content="2020">'
    other = html.replace("Family, Given", "Given Family").replace("2020", "2020/06/12")
    record = work_identity("https://publisher.example/paper", html)
    abstract = work_identity("https://arxiv.org/abs/2001.00001", other)
    full = work_identity("https://arxiv.org/html/2001.00001v2")
    assert same_work(record, abstract) and same_work(abstract, full)
    assert independent_works(
        [
            {"id": sid, "canonical_url": url, "metadata": {"document": doc}}
            for sid, url, doc in [
                ("record", "https://publisher.example/paper", record),
                ("abstract", "https://arxiv.org/abs/2001.00001", abstract),
                ("full", "https://arxiv.org/html/2001.00001v2", full),
            ]
        ]
    ) == [["abstract", "full", "record"]]
    assert not same_work(
        record,
        work_identity(
            "https://tutorial.example/explain", '<meta name="citation_title" content="A Test Paper">'
        ),
    )
    assert not same_work(
        record,
        work_identity("https://other.example/paper", html.replace("Family, Given", "Different Author")),
    )
    assert not same_work(record, work_identity("https://other.example/paper", html.replace("2020", "2021")))


def test_bilingual_search_alternatives_do_not_destroy_distinct_query_intentions():
    assert separate_queries(
        [
            "Example model overview / 示例模型概览",
            'Example "Original Paper" / 示例 原论文',
        ]
    ) == ["Example model overview", 'Example "Original Paper"', "示例模型概览", "示例 原论文"]
    assert separate_queries(["C/C++ guide", "https://example.org/a/b", "C/C++ guide"]) == [
        "C/C++ guide",
        "https://example.org/a/b",
    ]


def test_paper_views_and_exact_content_mirrors_are_one_work():
    sources = [
        {"id": "abs", "canonical_url": "https://arxiv.org/abs/1706.03762", "content_sha256": "abs"},
        {"id": "full", "canonical_url": "https://arxiv.org/html/1706.03762v7", "content_sha256": "body"},
        {"id": "mirror", "canonical_url": "https://mirror.example/paper", "content_sha256": "body"},
        {"id": "teaching", "canonical_url": "https://teaching.example/explain", "content_sha256": "other"},
    ]
    assert independent_works(sources) == [["abs", "full", "mirror"], ["teaching"]]


async def assessment_fixture(monkeypatch, count, candidates=(), extra_searches=0, read_candidates=False):
    notes = [
        {
            "source_id": str(i),
            "identity": None,
            "material_role": "explanation",
            "notes": f"Mechanism {i}",
            "passages": [{"passage_id": f"p{i}", "text": f"Substantive explanation {i}."}],
        }
        for i in range(count)
    ]
    frontier = {
        "minimum_useful_works": 3,
        "read_work_count": count,
        "read_works": [[str(i)] for i in range(count)],
        "candidates": list(candidates),
        "extra_searches": extra_searches,
        "extra_search_limit": 2,
    }
    monkeypatch.setattr(
        "research_agent.services.source_notes.SourceNotes.index", AsyncMock(return_value=notes)
    )
    monkeypatch.setattr(
        "research_agent.services.adaptive_research.reading_frontier", AsyncMock(return_value=frontier)
    )
    cache = {}

    async def save(run, fence, key, kind, result):
        cache[key] = result

    async def generate(key, role, p, schema, validator):
        value = ReadinessReview.model_validate(
            {
                "criteria": [
                    {
                        "criterion_id": "Q0",
                        "ready": True,
                        "explained_parts": ["Whole mechanism"],
                        "missing_parts": [],
                        "source_ids": [s["source_id"] for s in p["sources"]],
                    }
                ],
                "synthesis_notes": "Explain how the steps connect.",
                "source_contributions": [
                    {
                        "source_id": s["source_id"],
                        "adds_value": True,
                        "contribution": "A distinct part of the explanation.",
                    }
                    for s in p["sources"]
                ],
                "candidate_decisions": [
                    {
                        "candidate_id": c["candidate_id"],
                        "read": read_candidates,
                        "reason": "Useful worked example"
                        if read_candidates
                        else "Same-work translation adds no explanation",
                    }
                    for c in p["unread_candidates"]
                ],
                "search_queries": [],
            }
        )
        validator(value)
        return value

    s = SimpleNamespace(
        run_id="unit",
        fence=1,
        understanding={"standalone_query": "Explain a mechanism"},
        request=SimpleNamespace(query="Explain a mechanism"),
        db=SimpleNamespace(
            saved_value=AsyncMock(side_effect=lambda run, key: cache.get(key)),
            commit_value=AsyncMock(side_effect=save),
        ),
        evidence=SimpleNamespace(
            get_source=AsyncMock(
                side_effect=lambda sid: {
                    "id": sid,
                    "title": sid,
                    "canonical_url": "https://example.org/" + sid,
                }
            )
        ),
        llm=SimpleNamespace(build=lambda *a: ({}, 100), generate=AsyncMock(side_effect=generate)),
        warn=AsyncMock(),
    )
    criteria = [
        Criterion(criterion_id="criterion", description="Explain the mechanism", kind="authoritative_fact")
    ]
    return s, criteria


async def test_two_read_sources_cannot_end_research_while_selected_material_is_unread(monkeypatch):
    pending = {"source_id": "third", "url": "https://third.example/", "fetched": True}
    s, criteria = await assessment_fixture(monkeypatch, 2, [pending])
    result = await material_assessment(s, criteria)
    assert result["assessments"][0]["status"] == "missing"
    assert result["next_sources"] == [pending]
    s.llm.generate.assert_not_awaited()
    assert await material_assessment(s, criteria) == result


async def test_topic_coverage_cannot_override_positive_unread_candidate_decision(monkeypatch):
    pending = {"source_id": None, "url": "https://implementation.example/", "fetched": False}
    s, criteria = await assessment_fixture(monkeypatch, 3, [pending], read_candidates=True)
    result = await material_assessment(s, criteria)
    assert result["next_sources"] == [pending]
    assert result["assessments"][0]["status"] == "missing"


async def test_documented_skip_permits_stop_and_cached_replay_buys_no_new_review(monkeypatch):
    pending = {"source_id": None, "url": "https://translation.example/", "fetched": False}
    s, criteria = await assessment_fixture(monkeypatch, 3, [pending])
    result = await material_assessment(s, criteria)
    assert result["assessments"][0]["status"] == "covered"
    assert result["next_sources"] == []
    assert result["review"]["candidate_decisions"][0]["reason"]
    assert await material_assessment(s, criteria) == result
    assert s.llm.generate.await_count == 1


async def test_scarce_material_gets_bounded_search_not_infinite_count_chasing(monkeypatch):
    s, criteria = await assessment_fixture(monkeypatch, 1)
    result = await material_assessment(s, criteria)
    assert result["needs_search"] and result["assessments"][0]["status"] == "missing"
    s, criteria = await assessment_fixture(monkeypatch, 1, extra_searches=2)
    result = await material_assessment(s, criteria)
    assert not result["needs_search"] and result["assessments"][0]["status"] == "covered"
    assert result["input"]["breadth_policy"]["read_work_count"] == 1


async def test_publication_record_and_author_footer_reach_writing_without_another_model_read(monkeypatch):
    from research_agent.services.source_workflow import SourceWorkflow

    title = "A Test Paper"
    record_text = (
        "[Submitted on 12 Jun 2017]\nTitle:" + title + "\nAbstract: An original design.\nVaswani, Ashish"
    )
    rows = {
        "body": {
            "id": "body",
            "canonical_url": "https://arxiv.org/html/2001.00001v1",
            "content_text": "Body of the paper.",
            "metadata": {},
        },
        "record": {
            "id": "record",
            "canonical_url": "https://arxiv.org/abs/2001.00001",
            "content_text": record_text,
            "metadata": {
                "bibliographic_fields": [
                    {"field": "author", "value": "Vaswani, Ashish"},
                    {"field": "author", "value": "Absent author"},
                ]
            },
        },
    }
    proof = {"support_source_id": "body", "basis": "publication_record"}
    monkeypatch.setattr(
        "research_agent.services.publication_identity.established_identity",
        AsyncMock(
            side_effect=lambda services, sid: {"support_source_id": sid, "basis": "publication_record"}
        ),
    )
    services = SimpleNamespace(
        config={"fetch": {"chunk_chars": 2500}},
        evidence=SimpleNamespace(
            get_source=AsyncMock(side_effect=lambda sid: rows[sid]),
            source_index=AsyncMock(return_value=list(rows.values())),
        ),
    )
    by_source = {"body": {"identity": proof, "passages": {}}}
    await SourceWorkflow(services, None).add_bibliography(by_source)
    passages = list(by_source["record"]["passages"].values())
    assert any("2017" in p["text"] and "Vaswani" in p["text"] for p in passages)
    assert all(
        p["source_id"] == "record" and record_text[p["start_char"] : p["end_char"]] == p["text"]
        for p in passages
    )
    assert not any("Absent author" in p["text"] for p in passages)
