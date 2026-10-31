from typing import Annotated, TypedDict
from ..errors import InvariantViolation


def merge_immutable_map(left, right):
    result = dict(left or {})
    for key, value in (right or {}).items():
        if key in result and result[key] != value:
            raise InvariantViolation("immutable reducer collision")
        result[key] = value
    return dict(sorted(result.items()))


class ResearchState(TypedDict, total=False):
    schema_version: int
    run_id: str
    request: dict
    config_snapshot: dict
    understanding: dict
    conversation_summary: dict
    query_rewrite: dict
    local_sources: list[dict]
    discovery: dict
    memory_snapshot: dict
    plan_history: list[dict]
    batch_no: int
    expected_task_ids: list[str]
    task_results: Annotated[dict, merge_immutable_map]
    warnings: Annotated[dict, merge_immutable_map]
    claim_ids: list[str]
    gap_assessment: dict
    report_draft: dict
    report_id: str | None
    memory_candidates: list[dict]
    budget_snapshot: dict
    stop_reason: str | None
    rejection: str | None
    report_partial: bool
    report_coverage: float
    fallback_report: bool
    quality_review: dict
    quality_failed: bool
    repaired_paragraphs: list[str]
    initial_audit: dict
    editorial_revision: dict
    source_draft: dict
    source_materials: dict
    whole_answer_review: dict
    adaptive_review_key: str
    adaptive_revision: dict


class TaskInput(TypedDict):
    run_id: str
    batch_no: int
    task: dict
    memory_snapshot: dict
    discovery: dict


class TaskOutput(TypedDict):
    task_results: dict
    warnings: dict


class TaskState(TypedDict, total=False):
    retrieval_feedback: dict
    material_note_keys: list[str]
    run_id: str
    batch_no: int
    task: dict
    memory_snapshot: dict
    discovery: dict
    round: int
    no_progress_count: int
    query_keys: list[str]
    searched_queries: list[str]
    visited_source_ids: list[str]
    claim_ids: list[str]
    evidence_ids: list[str]
    criterion_results: list[dict]
    pending_action: dict | None
    pending_hits: list[dict]
    pending_source_ids: list[str]
    read_chunks: dict[str, list[str]]
    primary_domains: list[str]
    direct_read: bool
    stop_reason: str | None
    search_errors: list[str]
    task_results: dict
    warnings: dict
