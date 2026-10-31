import importlib
from pathlib import Path
from uuid import uuid4

from research_agent.contracts import ResearchRequest
from research_agent.storage.checkpoint import saver
from research_agent.storage.evidence import EvidenceStore
from tests.integration.test_narrative_graph import ConnectedModel, run_fixture


async def test_frozen_copy_preserves_text_dates_and_remaps_run_references(db, settings, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    copy_materials = importlib.import_module("revalidate_frozen").copy_materials
    s, old, report = await run_fixture(db, settings, ConnectedModel)
    before = await s.evidence.bundle()
    async with saver(settings) as cp:
        saved = await cp.aget_tuple({"configurable": {"thread_id": str(old["id"])}})
    plans = saved.checkpoint["channel_values"]["plan_history"]
    await db.create_run(
        ResearchRequest(query="Explain Cedar input and output.", memory_mode="off"),
        str(uuid4()),
        settings.snapshot(),
    )
    run = await db.claim("frozen-copy-test")
    target = EvidenceStore(db, str(run["id"]), run["fence"])
    copied_plans = await copy_materials(s.evidence, target, plans)
    after = await target.bundle()
    assert await s.evidence.bundle() == before
    assert (await s.evidence.report()).model_dump() == report.model_dump()
    assert copied_plans[0]["run_id"] == str(run["id"])
    assert copied_plans[0]["plan_id"] != plans[0]["plan_id"]
    for table in ["sources", "evidence", "claims", "tasks"]:
        assert len(before[table]) == len(after[table])
        assert not {x["id"] for x in before[table]} & {x["id"] for x in after[table]}
    assert {s["retrieved_at"] for s in before["sources"]} == {s["retrieved_at"] for s in after["sources"]}
    for e in after["evidence"]:
        source = await target.get_source(e["source_id"])
        assert source["content_text"][e["start_char"] : e["end_char"]] == e["quote"]
        assert source["metadata"]["reused_from_run"] == str(old["id"])
    assert {m["criterion_id"] for m in after["mappings"]} == {
        c["criterion_id"] for p in copied_plans for t in p["tasks"] for c in t["success_criteria"]
    }
