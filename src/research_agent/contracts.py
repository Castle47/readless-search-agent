"""Strict public and model contracts. Model-proposed IDs are never trusted."""

from __future__ import annotations
from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID, uuid5
import hashlib
import json
import re
import unicodedata
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator
from .model_policy import ModelId

NAMESPACE = UUID("f43d0466-b16c-4d53-87c7-b7aa50ef50ce")
Text = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
ID = Text
SourceType = Literal["official", "paper", "repository", "independent", "discussion", "unknown"]
StopReason = Literal[
    "cancelled",
    "deadline",
    "budget_exhausted",
    "sufficient",
    "max_rounds",
    "no_progress",
    "no_new_query",
    "provider_unavailable",
    "unsupported_content",
    "validation_failed",
    "internal_error",
]
TERMINAL = {"completed", "partial", "rejected", "failed", "cancelled"}


def utcnow():
    return datetime.now(timezone.utc)


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=lambda x: x.isoformat() if isinstance(x, datetime) else str(x),
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def stable_id(kind, *values):
    return str(uuid5(NAMESPACE, kind + ":" + canonical(values)))


def normalize(text):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_default=True)


class Payload(Contract):
    schema_version: Literal[1] = 1


class TimeRange(Contract):
    start: datetime | None = None
    end: datetime | None = None

    @model_validator(mode="after")
    def order(self):
        if any(value is not None and value.tzinfo is None for value in [self.start, self.end]):
            raise ValueError("dates require a timezone")
        if self.start and self.end and self.start > self.end:
            raise ValueError("time_range.start must not exceed end")
        return self


class RequestLimits(Contract):
    search_calls: Annotated[int, Field(ge=1, le=16)] | None = None
    fetched_pages: Annotated[int, Field(ge=1, le=30)] | None = None
    total_tokens: Annotated[int, Field(ge=12000, le=400000)] | None = None
    wall_seconds: Annotated[int, Field(ge=30, le=1200)] | None = None


class ResearchRequest(Payload):
    query: Annotated[Text, Field(max_length=8000)]
    context: Annotated[str, Field(max_length=12000)] = ""
    output_language: Literal["zh-CN", "en"] = "zh-CN"
    as_of: datetime | None = None
    time_range: TimeRange | None = None
    allowed_domains: list[Text] = Field(default_factory=list, max_length=30)
    blocked_domains: list[Text] = Field(default_factory=list, max_length=30)
    memory_mode: Literal["read_write", "read_only", "off"] = "read_write"
    limits: RequestLimits = Field(default_factory=RequestLimits)

    @field_validator("allowed_domains", "blocked_domains")
    @classmethod
    def domains(cls, values):
        result = []
        for item in values:
            domain = item.lower().rstrip(".").encode("idna").decode("ascii")
            if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}", domain):
                raise ValueError("domain must be a hostname without a URL, wildcard or port")
            result.append(domain)
        return sorted(set(result))

    @model_validator(mode="after")
    def dates(self):
        dates = [self.as_of]
        if self.time_range:
            dates += [self.time_range.start, self.time_range.end]
        for value in dates:
            if value and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("dates require a timezone")
        if self.as_of and self.as_of > utcnow():
            raise ValueError("as_of cannot be in the future")
        return self


class Facet(Contract):
    facet_id: ID
    question: Text
    required: bool


class Understanding(Payload):
    standalone_query: Text
    intent: Literal["fact", "comparison", "explanation", "survey"]
    assumptions: list[Text]
    facets: list[Facet] = Field(min_length=1, max_length=8)
    source_policy: Literal["primary_required", "primary_preferred"]
    freshness: Literal["current", "historical", "timeless"]
    search_languages: list[Text] = Field(min_length=1, max_length=2)
    reader_goal: str = ""
    needs_discovery: bool = False
    search_terms: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def required(self):
        if not any(f.required for f in self.facets):
            raise ValueError("at least one required facet")
        if len({f.facet_id for f in self.facets}) != len(self.facets):
            raise ValueError("duplicate facet IDs")
        return self


class UnderstandingDecision(Contract):
    understanding: Understanding | None
    rejection: Literal["ambiguous_query", "unsupported_intent"] | None

    @model_validator(mode="after")
    def either(self):
        if (self.understanding is None) == (self.rejection is None):
            raise ValueError("exactly one understanding or rejection")
        return self


class Criterion(Contract):
    required: bool = True  # Historical plans retain their original mandatory semantics.
    criterion_id: ID
    description: Text
    kind: Literal["authoritative_fact", "corroborated"]
    facet_ids: list[ID] = Field(default_factory=list)


class TaskProposal(Contract):
    question: Text
    facet_ids: list[ID] = Field(min_length=1)
    priority: int = Field(ge=1, le=3)
    scope: Text
    exclusions: list[Text]
    seed_queries: list[Text] = Field(min_length=1, max_length=2)
    source_targets: list[SourceType]
    source_urls: list[Text] = Field(default_factory=list)
    success_criteria: list[Criterion] = Field(min_length=1, max_length=3)
    parent_gap_ids: list[ID]


class PlanProposal(Contract):
    outline: list[Text]
    tasks: list[TaskProposal] = Field(min_length=1, max_length=5)
    validation_note: str


class ResearchTask(TaskProposal):
    task_id: ID
    max_rounds: int = Field(ge=1, le=3)


class FacetAssignment(Contract):
    facet_id: ID
    task_ids: list[ID] = Field(min_length=1)


class ResearchPlan(Payload):
    plan_id: ID
    run_id: ID
    batch_no: Literal[0, 1]
    outline: list[Text]
    tasks: list[ResearchTask] = Field(min_length=1, max_length=5)
    facet_assignments: list[FacetAssignment]


class NextAction(Contract):
    action: Literal["search", "finish"]
    query: Text | None
    language: Text | None
    target_criterion_ids: list[ID]
    rationale_summary: Annotated[Text, Field(max_length=400)]


class CriterionAssessment(Contract):
    criterion_id: ID
    status: Literal["covered", "missing", "conflicted"]
    supporting_claim_ids: list[ID]
    contradicting_claim_ids: list[ID]
    explanation: str = Field(max_length=500)


class ResearchResult(Payload):
    task_id: ID
    batch_no: Literal[0, 1]
    rounds: int = Field(ge=0, le=3)
    status: Literal["sufficient", "partial", "empty", "failed", "cancelled"]
    stop_reason: StopReason
    claim_ids: list[ID]
    evidence_ids: list[ID]
    criterion_results: list[CriterionAssessment]
    unresolved: list[Text]
    error_ids: list[ID]


class SearchHit(Contract):
    url: Text
    title: str
    snippet: str
    rank: int = Field(ge=1)
    published_at: datetime | None
    engines: list[str] = Field(default_factory=list)


class FetchResult(Payload):
    requested_url: Text
    canonical_url: Text
    title: Text
    media_type: Text
    text: Text
    content_sha256: Text
    retrieved_at: datetime
    published_at: datetime | None
    publisher_key: Text
    source_type: SourceType
    metadata: dict


class EvidenceCandidate(Contract):
    source_id: ID
    chunk_id: ID
    quote: Annotated[Text, Field(max_length=1200)]
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)
    claim_text: Annotated[Text, Field(max_length=1000)]
    relation: Literal["supports", "contradicts"]
    criterion_ids: list[ID] = Field(min_length=1)
    temporal_scope: Text | None
    directness: Literal["direct", "indirect"]


class Extraction(Contract):
    candidates: list[EvidenceCandidate] = Field(max_length=5)


class EvidenceVerdict(Contract):
    candidate_index: int = Field(ge=0)
    relation: Literal["supports", "contradicts", "insufficient"]
    direct: bool
    primary_for_claim: bool
    criterion_ids: list[ID]
    explanation: str = Field(max_length=400)


class SourceIdentity(Contract):
    basis: Literal[
        "original_publication", "official_documentation", "firsthand_record", "observed_official_link"
    ]
    publisher_name: Annotated[Text, Field(max_length=160)]
    context_id: ID | None
    quote: str = Field(max_length=500)
    link_id: ID | None
    explanation: str = Field(max_length=300)


class EvidenceVerification(Contract):
    verdicts: list[EvidenceVerdict] = Field(max_length=5)
    source_identity: SourceIdentity | None = None


class Claim(Payload):
    claim_id: ID
    text: Text
    temporal_scope: Text | None
    status: Literal["supported", "contested", "unverified", "superseded"]
    confidence: Literal["high", "medium", "low"]
    evidence_ids: list[ID]
    criterion_ids: list[ID]


class MergePair(Contract):
    claim_a_id: ID
    claim_b_id: ID
    relation: Literal["equivalent", "conflict", "distinct"]
    reason: Text


class MergeProposal(Contract):
    pairs: list[MergePair] = Field(max_length=30)


class Gap(Contract):
    gap_id: ID
    facet_id: ID
    criterion_id: ID | None
    type: Literal["missing", "conflict", "stale", "weak_source"]
    priority: int = Field(ge=1, le=3)
    question: Text
    evidence_ids: list[ID]


class GapAssessment(Payload):
    gaps: list[Gap]
    required_coverage: float = Field(ge=0, le=1)
    can_answer: bool
    explanation: Text


class ReportStatement(Contract):
    statement_id: ID
    text: Text
    kind: Literal["fact", "inference", "recommendation", "limitation"]
    claim_ids: list[ID]
    evidence_ids: list[ID]


class ReportSection(Contract):
    heading: Text
    statements: list[ReportStatement]


class ReportDraft(Payload):
    title: Text
    sections: list[ReportSection]
    limitations: list[Text]
    unresolved_questions: list[Text]


class StatementVerdict(Contract):
    statement_id: ID
    verdict: Literal["entailment", "contradiction", "insufficient"]
    atomic: bool
    answered_criterion_ids: list[ID]
    explanation: str = Field(max_length=400)


class ReportVerification(Contract):
    verdicts: list[StatementVerdict]


class ReportRepair(Contract):
    statements: list[ReportStatement]


class NarrativeAssertion(ReportStatement):
    kind: Literal["fact", "inference", "recommendation", "example"]
    anchor: Text


class BlockContent(Contract):
    block_type: Literal["paragraph", "list", "table", "code"] = "paragraph"
    text: Text
    language: str | None = None
    illustrative: bool = False

    @model_validator(mode="after")
    def well_formed_block(self):
        if self.block_type == "table":
            rows = [line.split("\t") for line in self.text.splitlines()]
            if len(rows) < 2 or len(rows[0]) < 2 or any(len(r) != len(rows[0]) for r in rows):
                raise ValueError("Table text must be rectangular TSV with a header and data rows")
        if self.language is not None and (
            self.block_type != "code" or not re.fullmatch(r"[A-Za-z0-9_+.#-]*", self.language)
        ):
            raise ValueError("Language is a plain code-block language identifier")
        return self


class NarrativeParagraph(BlockContent):
    paragraph_id: ID
    assertions: list[NarrativeAssertion] = Field(min_length=1)


class NarrativeSection(Contract):
    heading: str
    paragraphs: list[NarrativeParagraph]

    @property
    def statements(self):
        return [a for p in self.paragraphs for a in p.assertions]


class QuestionCoverage(Contract):
    question: Text
    required: bool
    status: Literal["answered", "partial", "unresolved"]


class NarrativeDraft(Payload):
    schema_version: Literal[2, 3] = 2
    title: Text
    sections: list[NarrativeSection]
    limitations: list[Text]
    unresolved_questions: list[Text]
    # Runtime fields are excluded from the writer schema via NarrativeComposition.
    coverage: list[QuestionCoverage] = Field(default_factory=list)
    paragraph_answers: dict[str, list[ID]] = Field(default_factory=dict)
    quality_status: Literal["pending", "passed", "revised", "not_checked", "issues_remaining"] = "pending"


class NarrativeComposition(Contract):
    title: Text
    sections: list[NarrativeSection]


class ComposedParagraph(BlockContent):
    claim_ids: list[ID] = Field(min_length=1)
    evidence_ids: list[ID] = Field(min_length=1)


class ComposedSection(Contract):
    heading: str
    paragraphs: list[ComposedParagraph] = Field(min_length=1)


class ProseComposition(Contract):
    """Private writer output; precise assertions are produced by the auditor."""

    title: Text
    sections: list[ComposedSection] = Field(min_length=1)


class UnresolvedEditorialIssue(Contract):
    feedback_id: ID
    description: Text


class EditorialRevision(ProseComposition):
    addressed_feedback_ids: list[ID]
    remaining_issues: list[UnresolvedEditorialIssue]


class ParagraphVerification(Contract):
    paragraph_id: ID
    verdicts: list[StatementVerdict]
    complete: bool
    unsupported_text: list[Text]
    explanation: str


class AuditedAtom(Contract):
    text: Text
    anchor: Text
    kind: Literal["fact", "inference", "recommendation", "example"]
    claim_ids: list[ID]
    evidence_ids: list[ID]
    verdict: Literal["entailment", "contradiction", "insufficient"]
    answered_criterion_ids: list[ID]


class ParagraphAudit(Contract):
    paragraph_id: ID
    atoms: list[AuditedAtom]
    complete: bool
    headings_supported: bool
    unsupported_text: list[Text]
    explanation: str


class BatchParagraphAudit(Contract):
    paragraphs: list[ParagraphAudit]


class CompactAuditedAtom(Contract):
    """The exact anchor already contains the prose; do not paraphrase it again."""

    anchor: Text
    kind: Literal["fact", "inference", "recommendation", "example"]
    claim_ids: list[ID]
    evidence_ids: list[ID]
    verdict: Literal["entailment", "contradiction", "insufficient"]
    answered_criterion_ids: list[ID]


class CompactParagraphAudit(ParagraphAudit):
    atoms: list[CompactAuditedAtom]


class CompactBatchParagraphAudit(Contract):
    paragraphs: list[CompactParagraphAudit]


class QualityIssue(Contract):
    paragraph_ids: list[ID]
    problem: Text
    suggestion: Text


class QualityReview(Contract):
    issues: list[QualityIssue]
    strengths: list[Text]


class ParagraphRepair(Contract):
    paragraphs: list[NarrativeParagraph]


class RevisedProse(Contract):
    paragraph_id: ID
    text: Text
    claim_ids: list[ID]
    evidence_ids: list[ID]


class ProseRepair(Contract):
    paragraphs: list[RevisedProse]


def parse_draft(value):
    return (NarrativeDraft if value.get("schema_version") in {2, 3} else ReportDraft).model_validate(value)


class Citation(Contract):
    citation_id: ID
    statement_id: ID
    claim_id: ID
    evidence_id: ID
    ordinal: int = Field(gt=0)
    paragraph_id: ID | None = None
    start_char: int | None = None
    end_char: int | None = None


class ExecutionSummary(Contract):
    model_id: ModelId
    reasoning_efforts: dict[str, str] | None = None
    search_calls: int = Field(ge=0)
    fetched_pages: int = Field(ge=0)
    tokens: int = Field(ge=0)
    unknown_tokens: int = Field(ge=0)
    stop_reason: str | None
    memory_status: Literal[
        "off", "read_only", "skipped", "pending", "applied", "degraded", "needs_reconciliation", "dead_letter"
    ]
    cost_usd: float | None = Field(default=None, ge=0)
    known_cost_usd: float | None = Field(default=None, ge=0)


class Report(Payload):
    report_id: ID
    run_id: ID
    version: Literal[1] = 1
    status: Literal["completed", "partial"]
    markdown: Text
    structured: ReportDraft | NarrativeDraft = Field(discriminator="schema_version")
    citations: list[Citation]
    created_at: datetime
    execution: ExecutionSummary | None = None


class MemoryItem(Payload):
    memory_id: ID
    kind: Literal["lesson", "source_pattern", "open_question", "conclusion", "failed_strategy"]
    text: Annotated[Text, Field(max_length=1200)]
    topic_tags: list[Text] = Field(max_length=8)
    source_run_id: ID
    claim_ids: list[ID]
    evidence_ids: list[ID]
    created_at: datetime
    expires_at: datetime | None
    status: Literal["active", "superseded"]
    supersedes: list[ID]


class MemorySnapshot(Payload):
    items: list[MemoryItem]
    recalled_at: datetime
    remote_revision: str | None
    degraded: bool


class MemoryCandidate(Contract):
    kind: Literal["lesson", "source_pattern", "open_question", "conclusion", "failed_strategy"]
    text: Annotated[Text, Field(max_length=1200)]
    topic_tags: list[Text] = Field(max_length=8)
    claim_ids: list[ID]
    evidence_ids: list[ID]
    supersedes: list[ID]


class MemoryProposal(Contract):
    items: list[MemoryCandidate] = Field(max_length=5)


def validate_ids(values, allowed, label="IDs"):
    unknown = set(values) - set(allowed)
    if unknown:
        raise ValueError(f"{label}: references outside current input allowlist")
