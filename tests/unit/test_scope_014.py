from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from research_agent.config import Settings, effective_limits
from research_agent.contracts import Criterion, ResearchRequest
from research_agent.services.source_notes import SourceNotes


def test_production_capacity_and_historical_snapshot_budgets(settings):
    current = Settings().snapshot()
    limits = effective_limits(ResearchRequest(query="x"), current)
    assert limits["total_tokens"] == 400000
    assert [limits["tokens_" + k] for k in ("research", "finalization", "memory")] == [180000, 212000, 8000]
    assert [
        limits["tokens_" + k] for k in ("writer", "report_verifier", "quality", "repair_and_recheck")
    ] == [212000, 212000, 212000, 212000]
    assert current["shared_finalization_budget"] is True
    historical = effective_limits(ResearchRequest(query="x"), {**current, "shared_finalization_budget": False})
    assert [historical["tokens_" + k] for k in ("writer", "report_verifier", "quality", "repair_and_recheck")] == [56000, 70000, 14000, 72000]
    assert current["research"]["wall_seconds"] == 1200
    assert current["research"]["finalization_seconds"] == 420
    assert current["model"]["roles"]["report_verifier"]["max_input"] == 64000
    assert current["model"]["roles"]["editor"]["max_input"] == 64000
    old = effective_limits(ResearchRequest(query="x"), settings.snapshot())
    assert old["tokens_finalization"] == 48000 and old["tokens_report_verifier"] == 16000
    reduced = effective_limits(ResearchRequest(query="x", limits={"total_tokens": 150000}), current)
    assert sum(reduced["tokens_" + k] for k in ("research", "finalization", "memory")) == 150000
    assert reduced["tokens_finalization"] == 79500


async def test_identity_does_not_grant_firsthand_authority_for_every_criterion():
    s = SimpleNamespace(
        config={},
        understanding={"source_policy": "primary_required"},
        evidence=SimpleNamespace(
            get_source=AsyncMock(return_value={"publisher_key": "guide.example", "content_sha256": "hash"})
        ),
    )
    notes = SourceNotes(s)
    notes.index = AsyncMock(
        return_value=[
            {
                "source_id": "s",
                "criterion_ids": ["api", "guide"],
                "passages": [{"text": "independent guide"}],
                "identity": {"publisher_name": "Independent guide"},
                "firsthand_criterion_ids": ["guide"],
            }
        ]
    )
    criteria = [Criterion(criterion_id=c, description=c, kind="authoritative_fact") for c in ["api", "guide"]]
    result = await notes.assessments(criteria)
    assert [r.status for r in result] == ["missing", "covered"]
    # Older notes without scoped authority cannot silently acquire that authority.
    notes.index.return_value[0].pop("firsthand_criterion_ids")
    assert all(r.status == "missing" for r in await notes.assessments(criteria))


def test_historical_criteria_remain_required():
    assert Criterion(criterion_id="old", description="old", kind="authoritative_fact").required


def test_source_reader_packet_packing_includes_existing_correction_feedback():
    from research_agent.services.packets import packets
    from research_agent.services.source_notes import IDENTITY_FEEDBACK
    from research_agent.errors import Stopped

    calls = []

    class LLM:
        def build(self, role, payload, schema, correction=None):
            calls.append(correction)
            if len(payload) + bool(correction) > 2:
                raise Stopped("input_budget_exceeded")

    groups = list(
        packets(["first", "second"], lambda x: x, LLM(), "source_reader", None, correction=IDENTITY_FEEDBACK)
    )
    assert groups == [["first"], ["second"]]
    assert all(c == IDENTITY_FEEDBACK for c in calls)


def test_audit_neighbors_are_context_not_evidence():
    from research_agent.source_contracts import SourceDraft, DraftBlock
    from research_agent.services.source_workflow import SourceWorkflow

    code = DraftBlock(
        paragraph_id="example",
        text="value = 0.86",
        passage_ids=["P0"],
        block_type="code",
        language="text",
        illustrative=True,
    )
    prose = DraftBlock(
        paragraph_id="explanation",
        text="The example crosses the threshold.",
        passage_ids=["P0"],
        block_type="paragraph",
        language=None,
        illustrative=True,
    )
    from research_agent.source_contracts import DraftSection

    draft = SourceDraft(title="Example", sections=[DraftSection(heading="Example", paragraphs=[code, prose])])
    material = {
        "sources": {"S0": {"title": "API", "canonical_url": "https://example.org", "identity": None}},
        "passages": {
            "P0": {
                "source_alias": "S0",
                "source_id": "source",
                "passage_id": "span",
                "criterion_ids": [],
                "text": "Returns a probability.",
                "table_header": None,
                "start_char": 0,
                "end_char": 22,
            }
        },
    }
    workflow = SourceWorkflow(
        SimpleNamespace(llm=SimpleNamespace(build=lambda *args: ({}, 0))),
        SimpleNamespace(all_criteria=lambda s: {}),
    )
    payload, offered, _ = workflow.audit_payload(
        [prose], {"source_draft": draft.model_dump(), "source_materials": material}
    )
    assert payload["surrounding_answer_context"][0]["text"] == code.text
    assert payload["surrounding_answer_context"][0]["illustrative"] is True
    assert [p["paragraph_id"] for p in payload["paragraphs"]] == ["explanation"]
    assert all(p["text"] != code.text for p in offered.values())


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/acme/demo/blob/main/docs/usage.md", True),
        ("https://github.com/acme/demo-other", False),
        ("https://docs.github.com/en", False),
        ("https://archiveprogram.github.com/", False),
        ("https://github.com/features/actions", False),
        ("https://publisher.example/model", True),
    ],
)
def test_repository_navigation_stays_with_project_or_external_publisher(url, expected):
    from research_agent.graph.source_researcher import research_reference

    assert (
        research_reference(
            "https://github.com/acme/demo/blob/main/README.md",
            {"url": url, "official_link": True},
            {"github.com"},
        )
        is expected
    )
