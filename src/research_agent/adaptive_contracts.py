"""Whole-answer review: important factual checks and source-grounded exposition."""

from typing import Literal
from pydantic import Field
from .contracts import Contract, ID, Text


class MaterialReadiness(Contract):
    criterion_id: ID
    ready: bool
    explained_parts: list[str]
    missing_parts: list[str]
    source_ids: list[ID]


class SourceContribution(Contract):
    source_id: ID
    adds_value: bool
    contribution: Text


class UnreadCandidateDecision(Contract):
    candidate_id: ID
    read: bool
    reason: Text


class ReadinessReview(Contract):
    criteria: list[MaterialReadiness]
    synthesis_notes: str
    source_contributions: list[SourceContribution]
    candidate_decisions: list[UnreadCandidateDecision]
    search_queries: list[Text] = Field(max_length=2)


class BlockGrounding(Contract):
    paragraph_id: ID
    supported: bool
    kind: Literal["fact", "inference", "example"]
    passage_ids: list[ID]


class ImportantFact(Contract):
    paragraph_id: ID
    statement: Text = Field(max_length=1000)
    kind: Literal[
        "identity", "date", "empirical_result", "product_contract", "contested_comparison", "original_design"
    ]
    supported: bool
    passage_ids: list[ID]
    primary_passage_ids: list[ID]
    explanation: str


class AnswerIssue(Contract):
    paragraph_ids: list[ID]
    kind: Literal["fact", "missing_explanation", "clarity"]
    problem: Text
    suggestion: Text


class AnswerCoverage(Contract):
    criterion_id: ID
    answered: bool
    paragraph_ids: list[ID]
    missing: str


class WholeAnswerReview(Contract):
    blocks: list[BlockGrounding]
    important_facts: list[ImportantFact]
    coverage: list[AnswerCoverage]
    issues: list[AnswerIssue]
    research_queries: list[Text] = Field(max_length=2)
