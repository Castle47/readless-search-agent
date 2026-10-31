"""Private source-first contracts. Notes and drafts are NOT verified evidence."""

from typing import Literal

from pydantic import Field, model_validator

from .contracts import ID, BlockContent, Contract, SourceIdentity, Text, UnresolvedEditorialIssue


class SourceReading(Contract):
    passage_ids: list[ID]
    addressed_criterion_ids: list[ID]
    firsthand_criterion_ids: list[ID]
    notes: str
    source_identity: SourceIdentity | None
    material_role: Literal["original", "explanation", "implementation", "irrelevant"] = "original"
    open_questions: list[str] = Field(default_factory=list)


class SourceBlock(BlockContent):
    passage_ids: list[ID] = Field(min_length=1)


class SourceSection(Contract):
    heading: str
    paragraphs: list[SourceBlock] = Field(min_length=1)


class SourceComposition(Contract):
    title: Text
    sections: list[SourceSection] = Field(min_length=1)


class EditedSourceBlock(Contract):
    reuse_paragraph_id: ID | None
    replacement: SourceBlock | None

    @model_validator(mode="after")
    def exactly_one(self):
        if (self.reuse_paragraph_id is None) == (self.replacement is None):
            raise ValueError("Reuse an unchanged block OR provide its replacement")
        return self


class EditedSourceSection(Contract):
    heading: str
    paragraphs: list[EditedSourceBlock] = Field(min_length=1)


class SourceRevision(Contract):
    title: Text
    sections: list[EditedSourceSection] = Field(min_length=1)
    addressed_feedback_ids: list[ID]
    remaining_issues: list[UnresolvedEditorialIssue]


class DraftBlock(SourceBlock):
    paragraph_id: ID


class DraftSection(Contract):
    heading: str
    paragraphs: list[DraftBlock]


class SourceDraft(Contract):
    # Never serialized as a public Report; final reports retain schema 3.
    draft_version: Literal[1] = 1
    title: Text
    sections: list[DraftSection]


class SourceSupport(Contract):
    passage_id: ID
    # Null means the supplied passage's exact original span. A focused quote
    # remains available, but reproducing long source paragraphs is unnecessary.
    quote: Text | None
    primary_for_claim: bool
    direct: bool


class SourceAtom(Contract):
    anchor_ids: list[ID] = Field(min_length=1)
    anchor: Text | None = Field(default=None, max_length=1000)
    kind: Literal["fact", "inference", "recommendation", "example"]
    purpose: Literal["core_fact", "explanation"] = "core_fact"
    verdict: Literal["entailment", "contradiction", "insufficient"]
    supports: list[SourceSupport]
    criterion_ids: list[ID] = Field(min_length=1)
    answered_criterion_ids: list[ID]


class SourceParagraphAudit(Contract):
    paragraph_id: ID
    atoms: list[SourceAtom]
    discourse_ids: list[ID]
    complete: bool
    headings_supported: bool
    unsupported_text: list[Text]
    explanation: str


class SourceBatchAudit(Contract):
    paragraphs: list[SourceParagraphAudit]


class SourcePlanCriterion(Contract):
    required: bool
    question: Text
    kind: Literal["authoritative_fact", "corroborated"]
    facet_ids: list[ID] = Field(min_length=1)


class SourcePlanTask(Contract):
    question: Text
    facet_ids: list[ID] = Field(min_length=1)
    queries: list[Text] = Field(min_length=1, max_length=2)
    source_targets: list[Literal["official", "paper", "repository", "independent", "discussion", "unknown"]]
    source_urls: list[Text]
    criteria: list[SourcePlanCriterion] = Field(max_length=3)
    existing_criterion_ids: list[ID]
    gap_ids: list[ID]


class SourcePlan(Contract):
    tasks: list[SourcePlanTask] = Field(min_length=1, max_length=5)
