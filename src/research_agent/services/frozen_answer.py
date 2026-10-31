"""Validation continuation: identical source bytes and draft, fresh verdicts."""

from copy import deepcopy

from ..contracts import ResearchPlan, digest, stable_id
from ..graph.researcher import Researcher
from ..storage.db import all_rows
from .provenance import valid_stored_identity


async def copy_frozen_answer(source, services, original, *, reuse_draft=True):
    """Copy only material and unverified prose into a new, separately billed run."""
    if not reuse_draft and any(g["priority"] == 1 for g in original["gap_assessment"]["gaps"]):
        raise ValueError("Frozen writing cannot skip unresolved required research gaps")
    target = services.evidence
    ids = {source.run_id: target.run_id}
    for plan in original["plan_history"]:
        ids[plan["plan_id"]] = stable_id("frozen-plan", target.run_id, plan["plan_id"])
        for task in plan["tasks"]:
            ids[task["task_id"]] = stable_id("frozen-task", target.run_id, task["task_id"])
            for criterion in task["success_criteria"]:
                ids[criterion["criterion_id"]] = stable_id(
                    "frozen-criterion", target.run_id, criterion["criterion_id"]
                )
    snapshots = []
    for item in await source.source_index():
        row = await source.get_source(item["id"])
        page = Researcher.stored_page(row)
        page = page.model_copy(
            update={
                "metadata": {
                    **page.metadata,
                    "reused_from_run": source.run_id,
                    "reused_from_source": row["id"],
                }
            }
        )
        ids[row["id"]] = await target.source(page)
        snapshots.append([row["canonical_url"], row["content_sha256"]])

    def remap(value):
        if isinstance(value, str):
            return ids.get(value, value)
        if isinstance(value, list):
            return [remap(v) for v in value]
        if isinstance(value, dict):
            return {k: remap(v) for k, v in value.items()}
        return value

    plans = remap(original["plan_history"])
    for plan in plans:
        await services.db.save_plan(services.run_id, services.fence, ResearchPlan.model_validate(plan))
    async with source.db.pool.connection() as conn:
        operations = await all_rows(
            conn,
            "SELECT kind,result FROM research.operations WHERE run_id=%s AND kind IN ('source_note','source_identity') AND status='succeeded' ORDER BY operation_key",
            (source.run_id,),
        )
    for row in operations:
        result = remap(row["result"])
        if row["kind"] == "source_identity":
            proof = result["proof"]
            target_source = await target.get_source(proof["source_id"])
            support = await target.get_source(proof["support_source_id"])
            if not valid_stored_identity(proof, target_source, support):
                raise ValueError("Frozen identity proof differs from exact source bytes")
            key = "source-identity:" + proof["source_id"]
        else:
            saved_source = await target.get_source(result["source_id"])
            if saved_source["content_sha256"] != result["source_hash"]:
                raise ValueError("Frozen reading source hash differs")
            for passage in result["passages"]:
                if (
                    saved_source["content_text"][passage["start_char"] : passage["end_char"]]
                    != passage["text"]
                ):
                    raise ValueError("Frozen reading passage differs")
            key = "frozen-note:" + digest(result)
        await services.db.commit_value(services.run_id, services.fence, key, row["kind"], result)

    draft, materials = None, None
    if reuse_draft:
        materials = remap(original["source_materials"])
        for passage in materials["passages"].values():
            entry = materials["sources"][passage["source_alias"]]
            row = await target.get_source(entry["id"])
            if row["content_text"][passage["start_char"] : passage["end_char"]] != passage["text"]:
                raise ValueError("Frozen answer material differs from original source")
        draft = deepcopy(original["source_draft"])
        if not draft.get("sections"):
            raise ValueError("No complete raw draft to recheck")
        await services.db.commit_value(
            services.run_id,
            services.fence,
            "draft:initial:v1",
            "unverified_draft",
            draft,
        )
    await services.db.commit_value(
        services.run_id,
        services.fence,
        "validation:frozen-answer",
        "validation_continuation",
        {
            "source_run": source.run_id,
            "source_snapshots": sorted(snapshots),
            "draft_hash": digest(draft) if reuse_draft else None,
            "reused_prose": reuse_draft,
            "reused_fact_verdicts": False,
            "scope": "fresh whole-answer review over immutable existing materials",
        },
    )
    return {
        "schema_version": 1,
        "run_id": services.run_id,
        "request": services.request.model_dump(mode="json"),
        "config_snapshot": services.config,
        "understanding": deepcopy(original["understanding"]),
        "plan_history": plans,
        **({"source_draft": draft, "source_materials": materials} if reuse_draft else {}),
        "task_results": {},
        "warnings": {},
        "batch_no": 0,
        "stop_reason": None,
        "fallback_report": False,
        "quality_failed": False,
        "gap_assessment": deepcopy(original["gap_assessment"]),
        "memory_snapshot": deepcopy(original["memory_snapshot"]),
    }
