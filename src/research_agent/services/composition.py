"""Lossless deduplication of writer material and paragraph-scoped citation hints."""

from ..contracts import (
    NarrativeAssertion,
    NarrativeDraft,
    NarrativeParagraph,
    NarrativeSection,
    validate_ids,
)


def pack_materials(claims, criteria):
    sources, evidence, rows, passages = {}, {}, [], {}
    questions = {cid: f"Q{i}" for i, cid in enumerate(criteria)}
    for claim in claims:
        links = []
        for item in claim["evidence"]:
            sid = item["source_id"]
            if sid not in sources:
                sources[sid] = {
                    "source_id": f"S{len(sources)}",
                    "title": item["title"],
                    "url": item["url"],
                    "published_at": item["published_at"],
                }
            eid = item["evidence_id"]
            passage = item.get("reading_passage")
            passage_id = None
            if passage:
                key = (sid, passage["start_char"], passage["end_char"])
                if key not in passages:
                    passages[key] = {
                        "passage_id": f"P{len(passages)}",
                        "source_id": sources[sid]["source_id"],
                        **passage,
                    }
                passage_id = passages[key]["passage_id"]
            value = {
                "evidence_id": eid,
                "source_id": sources[sid]["source_id"],
                "quote": item["quote"],
                "reading_context": "" if passage else item["reading_context"],
                "passage_id": passage_id,
                "section": item["section"],
                "table_header": item.get("table_header", ""),
            }
            if eid in evidence and evidence[eid] != value:
                raise ValueError("One immutable evidence ID cannot have different text")
            evidence[eid] = value
            links.append({"evidence_id": eid, "relation": item["relation"], "primary": item["primary"]})
        rows.append(
            {
                "claim_id": claim["claim_id"],
                "text": claim["text"],
                "status": claim["status"],
                "temporal_scope": claim["temporal_scope"],
                "question_ids": [questions[cid] for cid in claim["criterion_ids"]],
                "evidence_links": links,
            }
        )
    return {
        "claims": rows,
        "evidence": list(evidence.values()),
        "sources": list(sources.values()),
        "reading_passages": list(passages.values()),
        "research_questions": [
            {"question_id": questions[cid], "description": c.description} for cid, c in criteria.items()
        ],
    }


def validate_prose(value, materials):
    # These are retrieval hints, not assertions or approved citations. A writer
    # may mention an extra known passage without repeating its claim ID. Only
    # the independent paragraph audit can create citations, and it checks the
    # actual claim/evidence relationships and the complete prose separately.
    claims = {c["claim_id"]: c for c in materials["claims"]}
    evidence = {e["evidence_id"] for e in materials["evidence"]}
    for section in value.sections:
        for p in section.paragraphs:
            validate_ids(p.claim_ids, claims)
            validate_ids(p.evidence_ids, evidence)


def prose_draft(value, claim_map, evidence_map):
    sections, index = [], 0
    for section in value.sections:
        ps = []
        for p in section.paragraphs:
            index += 1
            pid = f"p{index}"
            ps.append(
                NarrativeParagraph(
                    paragraph_id=pid,
                    text=p.text,
                    block_type=p.block_type,
                    language=p.language,
                    illustrative=p.illustrative,
                    assertions=[
                        NarrativeAssertion(
                            statement_id=pid + ":hint",
                            text=p.text,
                            anchor=p.text,
                            kind="fact",
                            claim_ids=[claim_map[x] for x in dict.fromkeys(p.claim_ids)],
                            evidence_ids=[evidence_map[x] for x in dict.fromkeys(p.evidence_ids)],
                        )
                    ],
                )
            )
        sections.append(NarrativeSection(heading=section.heading, paragraphs=ps))
    return NarrativeDraft(
        schema_version=3, title=value.title, sections=sections, limitations=[], unresolved_questions=[]
    )
