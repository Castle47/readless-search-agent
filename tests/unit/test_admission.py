from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from research_agent.contracts import ResearchRequest, UnderstandingDecision
from research_agent.graph.builder import ResearchGraph
from research_agent.services.admission import fallback_understanding, missing_referent


@pytest.mark.parametrize("query", ["介绍一下原生的 transformer 模型", "Please explain Cedar"])
async def test_invalid_understanding_preserves_intro_intent_and_broad_discovery(query):
    from research_agent.errors import ToolFailure

    services = SimpleNamespace(
        request=ResearchRequest(query=query, context="用户：介绍 Jev\n上次回答（未核验）：旧主题长回答"),
        db=SimpleNamespace(phase=AsyncMock(), commit_value=AsyncMock()),
        run_id="test",
        fence=1,
        warn=AsyncMock(),
        llm=SimpleNamespace(
            build=lambda *a, **k: ({}, 1), generate=AsyncMock(side_effect=ToolFailure("invalid_output"))
        ),
    )
    result = (await ResearchGraph(services).understand({}))["understanding"]
    assert result["intent"] == "explanation" and result["needs_discovery"]
    assert len(result["search_terms"]) == 3
    assert result["standalone_query"] == query
    assert all("Jev" not in q and "旧主题" not in q for q in result["search_terms"])


def test_failed_narrow_fact_understanding_is_not_recast_as_an_introduction():
    result = fallback_understanding(ResearchRequest(query="Cedar was published in which year?"))
    assert result.intent == "survey" and len(result.search_terms) == 1


@pytest.mark.parametrize(
    "query", ["它如何实现", "介绍一下这个模型。", "这个是什么？", "How does it work?", "Explain that model."]
)
async def test_only_missing_object_stops_before_model_or_search(query):
    services = SimpleNamespace(
        request=ResearchRequest(query=query),
        db=SimpleNamespace(phase=AsyncMock(), commit_value=AsyncMock()),
        run_id="test",
        fence=1,
        llm=SimpleNamespace(generate=AsyncMock()),
    )
    assert await ResearchGraph(services).understand({}) == {"rejection": "ambiguous_query"}
    services.llm.generate.assert_not_called()


@pytest.mark.parametrize(
    "query",
    [
        "介绍一下 Jev 模型",
        "介绍一下 泽岚 模型",
        "Explain Qzora-17",
        "这个 Qzora 模型是什么？",
        "This Qzora model: how does it work?",
        "it 这个单词的用法",
        "解释量子计算",
        "Orion",
    ],
)
async def test_model_unfamiliarity_cannot_reject_searchable_topics(query):
    services = SimpleNamespace(
        request=ResearchRequest(query=query),
        db=SimpleNamespace(phase=AsyncMock(), commit_value=AsyncMock()),
        run_id="test",
        fence=1,
        warn=AsyncMock(),
        llm=SimpleNamespace(
            build=lambda *a, **k: ({}, 1),
            generate=AsyncMock(
                return_value=UnderstandingDecision(
                    understanding=None,
                    rejection="ambiguous_query",
                )
            ),
        ),
    )
    result = await ResearchGraph(services).understand({})
    assert result["rejection"] is None
    assert result["understanding"]["standalone_query"] == query
    assert result["understanding"]["facets"][0]["required"] is True
    assert result["understanding"]["assumptions"]
    services.llm.generate.assert_awaited_once()


async def test_explicit_context_resolves_followup_and_survives_discovery_fallback():
    request = ResearchRequest(query="它如何实现", context="前面讨论的是 Qzora 模型。")
    assert not missing_referent(request)
    resolved = fallback_understanding(request)
    resolved.standalone_query = "Qzora 模型如何实现"
    services = SimpleNamespace(
        request=request,
        db=SimpleNamespace(phase=AsyncMock(), commit_value=AsyncMock()),
        run_id="test",
        fence=1,
        llm=SimpleNamespace(
            build=lambda *a, **k: ({}, 1),
            generate=AsyncMock(
                return_value=UnderstandingDecision(
                    understanding=resolved,
                    rejection=None,
                )
            ),
        ),
    )
    result = await ResearchGraph(services).understand({})
    assert result["understanding"]["standalone_query"] == "Qzora 模型如何实现"
    assert request.context not in " ".join(fallback_understanding(request, discover=True).assumptions)
    assert request.context == "前面讨论的是 Qzora 模型。"
    assert missing_referent(ResearchRequest(query="它如何实现", context="  "))


@pytest.mark.parametrize("query", ["你好", "写一首关于月亮的诗"])
async def test_unsupported_intent_still_rejected(query):
    services = SimpleNamespace(
        request=ResearchRequest(query=query),
        db=SimpleNamespace(phase=AsyncMock(), commit_value=AsyncMock()),
        run_id="test",
        fence=1,
        llm=SimpleNamespace(
            build=lambda *a, **k: ({}, 1),
            generate=AsyncMock(
                return_value=UnderstandingDecision(
                    understanding=None,
                    rejection="unsupported_intent",
                )
            ),
        ),
    )
    assert await ResearchGraph(services).understand({}) == {"rejection": "unsupported_intent"}


@pytest.mark.parametrize("failure", ["unknown_url", "missing_required_criterion"])
async def test_plan_still_rejects_invented_urls_and_missing_required_coverage(failure):
    from research_agent.contracts import PlanProposal

    facets = [{"facet_id": "f0", "question": "Identity", "required": True}]
    task_facets = ["f0"]
    if failure == "missing_required_criterion":
        facets.append({"facet_id": "f1", "question": "Mechanism", "required": True})
        task_facets.append("f1")
    proposal = PlanProposal.model_validate(
        {
            "outline": ["Explanation"],
            "validation_note": "",
            "tasks": [
                {
                    "question": "Explain Cedar",
                    "facet_ids": task_facets,
                    "priority": 1,
                    "scope": "Explain the model",
                    "exclusions": [],
                    "seed_queries": ["Cedar original documentation"],
                    "source_targets": ["official"],
                    "source_urls": ["https://unobserved.example/"] if failure == "unknown_url" else [],
                    "success_criteria": [
                        {
                            "criterion_id": "c0",
                            "description": "Identify the model",
                            "kind": "authoritative_fact",
                            "facet_ids": ["f0"],
                        }
                    ],
                    "parent_gap_ids": [],
                }
            ],
        }
    )

    async def generate(*args, validator, **kwargs):
        validator(proposal)
        return proposal

    services = SimpleNamespace(
        request=ResearchRequest(query="Explain Cedar"),
        config={},
        run_id="fixture",
        fence=1,
        db=SimpleNamespace(phase=AsyncMock(), save_plan=AsyncMock()),
        llm=SimpleNamespace(generate=generate, build=lambda *args: ({}, 0)),
    )
    with pytest.raises(ValueError):
        await ResearchGraph(services).plan(
            {
                "understanding": {"intent": "explanation", "facets": facets},
                "memory_snapshot": {"items": []},
                "discovery": {"sources": []},
            }
        )
    services.db.save_plan.assert_not_called()


@pytest.mark.parametrize("duplicate", [False, True])
@pytest.mark.parametrize("required", [True, False])
async def test_supplement_planner_sees_and_can_use_sources_found_after_discovery(duplicate, required):
    from research_agent.contracts import PlanProposal

    criterion = {
        "criterion_id": "old-c",
        "required": required,
        "description": "Explain the mechanism",
        "kind": "authoritative_fact",
        "facet_ids": ["f0", "f1"] if duplicate else ["f0"],
    }
    url = "https://publisher.example/original"

    async def generate(key, role, payload, schema, validator):
        assert payload["known_sources"] == [
            {"url": url, "title": "Original documentation", "contains_verified_primary_evidence": True}
        ]
        proposal = PlanProposal.model_validate(
            {
                "outline": ["Explanation"],
                "validation_note": "",
                "tasks": [
                    {
                        "question": "Explain the mechanism",
                        "facet_ids": criterion["facet_ids"],
                        "priority": 1,
                        "scope": "mechanism",
                        "exclusions": [],
                        "seed_queries": ["Cedar mechanism"],
                        "source_targets": ["official"],
                        "source_urls": [url],
                        "success_criteria": [{**criterion, "criterion_id": "Q0", "required": not required}]
                        + ([{**criterion, "criterion_id": "Q1"}] if duplicate else []),
                        "parent_gap_ids": ["G0", "G1"] if duplicate else ["G0"],
                    }
                ],
            }
        )
        validator(proposal)
        return proposal

    services = SimpleNamespace(
        request=ResearchRequest(query="Explain Cedar"),
        config={},
        run_id="fixture",
        fence=1,
        db=SimpleNamespace(phase=AsyncMock(), save_plan=AsyncMock()),
        llm=SimpleNamespace(generate=generate, build=lambda *args: ({}, 0)),
        evidence=SimpleNamespace(
            bundle=AsyncMock(
                return_value={
                    "sources": [{"id": "s1", "canonical_url": url, "title": "Original documentation"}],
                    "evidence": [{"id": "e1", "source_id": "s1"}],
                    "relations": [{"evidence_id": "e1", "verification": {"primary_for_claim": True}}],
                }
            )
        ),
    )
    state = {
        "understanding": {
            "intent": "explanation",
            "facets": [
                {"facet_id": f, "question": "Mechanism", "required": True} for f in criterion["facet_ids"]
            ],
        },
        "memory_snapshot": {"items": []},
        "discovery": {"sources": []},
        "plan_history": [{"tasks": [{"success_criteria": [criterion]}]}],
        "gap_assessment": {
            "gaps": [{"gap_id": "old-g", "criterion_id": "old-c", "facet_id": "f0", "question": "Mechanism"}]
            + (
                [{"gap_id": "other-g", "criterion_id": "old-c", "facet_id": "f1", "question": "Mechanism"}]
                if duplicate
                else []
            )
        },
    }
    result = await ResearchGraph(services).plan(state)
    task = result["plan_history"][-1]["tasks"][0]
    assert task["source_urls"] == [url]
    assert task["success_criteria"][0] == criterion
    assert len(task["success_criteria"]) == 1
    assert task["parent_gap_ids"] == (["old-g", "other-g"] if duplicate else ["old-g"])
