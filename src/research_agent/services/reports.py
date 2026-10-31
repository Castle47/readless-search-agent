from urllib.parse import quote
from ..contracts import (
    ReportDraft,
    ReportSection,
    ReportStatement,
    NarrativeDraft,
    Report,
    Citation,
    stable_id,
    utcnow,
)
from ..errors import InvariantViolation


def source_link(source):
    metadata = source.get("metadata") or {}
    if metadata.get("private") and metadata.get("path"):
        return "obsidian://open?vault=Obsidian%20Vault&file=" + quote(metadata["path"], safe="")
    return source["canonical_url"]


def evidence_for_claim(claim, bundle):
    evidence = {e["id"]: e for e in bundle["evidence"]}
    sources = {s["id"]: s for s in bundle["sources"]}
    relations = [r for r in bundle["relations"] if r["claim_id"] == claim["id"]]
    selected = []
    for relation in ["supports", "contradicts"]:
        matches = [r for r in relations if r["relation"] == relation]
        matches.sort(key=lambda r: (not r["verification"].get("primary_for_claim"), r["evidence_id"]))
        for item in matches[:2]:
            e = evidence[item["evidence_id"]]
            s = sources[e["source_id"]]
            selected.append(
                {
                    "evidence_id": e["id"],
                    "quote": e["quote"],
                    "relation": relation,
                    "source_id": s["id"],
                    "title": s["title"],
                    "url": s["canonical_url"],
                    "published_at": s["published_at"],
                    "primary": item["verification"].get("primary_for_claim", False),
                }
            )
    return selected


def writer_bundle(bundle):
    result = []
    for claim in bundle["claims"]:
        if claim["merged_into"] or claim["status"] in {"unverified", "superseded"}:
            continue
        ev = evidence_for_claim(claim, bundle)
        if not ev:
            continue
        result.append(
            {
                "claim_id": claim["id"],
                "text": claim["canonical_text"],
                "status": claim["status"],
                "temporal_scope": claim["temporal_scope"],
                "criterion_ids": sorted(
                    {m["criterion_id"] for m in bundle["mappings"] if m["claim_id"] == claim["id"]}
                ),
                "evidence": ev,
            }
        )
    return result


def cited_support_bundle(draft, bundle):
    cited = {
        (cid, eid)
        for section in draft.sections
        for statement in section.statements
        for cid in statement.claim_ids
        for eid in statement.evidence_ids
    }
    # Completion must be supported by evidence actually cited in the answer.
    # Keep every known contradiction/conflict, so selective citing cannot hide it.
    return {
        **bundle,
        "relations": [
            r
            for r in bundle["relations"]
            if r["relation"] != "supports" or (r["claim_id"], r["evidence_id"]) in cited
        ],
    }


def validate_statement(statement, bundle):
    claims = {c["id"]: c for c in bundle["claims"]}
    evidence = {e["id"]: e for e in bundle["evidence"]}
    if set(statement.claim_ids) - claims.keys() or set(statement.evidence_ids) - evidence.keys():
        raise ValueError("unknown citation ID")
    if statement.kind == "limitation":
        # Only a deterministic service limitation is allowed without evidence.
        if statement.claim_ids or statement.evidence_ids:
            raise ValueError("limitations must be service-generated")
        raise ValueError("model limitation statements must use the limitations field")
    if not statement.claim_ids or not statement.evidence_ids:
        raise ValueError("missing factual premises")
    if (
        statement.kind == "inference"
        and not hasattr(statement, "anchor")
        and not any(s in statement.text for s in ["推断", "Inference", "inference"])
    ):
        raise ValueError("inference must be labelled")
    for cid in statement.claim_ids:
        if claims[cid]["merged_into"] or claims[cid]["status"] in {"unverified", "superseded"}:
            raise ValueError("unverified premise")
        if statement.kind == "fact" and claims[cid]["status"] == "contested":
            raise ValueError("contested claim cannot be unilateral fact")
        if not any(
            r["claim_id"] == cid
            and r["evidence_id"] in statement.evidence_ids
            and (statement.kind != "fact" or r["relation"] == "supports")
            for r in bundle["relations"]
        ):
            raise ValueError("claim lacks linked evidence")
    for eid in statement.evidence_ids:
        if not evidence[eid]["verified"] or not any(
            r["evidence_id"] == eid and r["claim_id"] in statement.claim_ids for r in bundle["relations"]
        ):
            raise ValueError("unlinked evidence")


def digest_draft(query, bundle, limitations, language="zh-CN"):
    english = language == "en"
    statements = []
    for e in bundle["evidence"]:
        if not e["verified"]:
            continue
        related = sorted({r["claim_id"] for r in bundle["relations"] if r["evidence_id"] == e["id"]})
        if not related:
            continue
        statements.append(
            ReportStatement(
                statement_id=stable_id("quote", e["id"]),
                text=(
                    "Source excerpt (not a synthesized conclusion): "
                    if english
                    else "原文摘录（尚未形成综合结论）："
                )
                + e["quote"],
                kind="fact",
                claim_ids=related,
                evidence_ids=[e["id"]],
            )
        )
    return ReportDraft(
        title=query,
        sections=[
            ReportSection(
                heading="Verified source excerpts" if english else "已核对的来源摘录", statements=statements
            )
        ],
        limitations=list(
            dict.fromkeys(
                limitations
                + (
                    ["This research did not obtain enough verifiable evidence."]
                    if english
                    else ["本次未获得足够可核验的证据。"]
                )
            )
        ),
        unresolved_questions=[query],
    )


def markdown_escape(text):
    text = text.replace("<", "&lt;").replace(">", "&gt;")
    for char in ["\\", "[", "]", "*", "_", "`"]:
        text = text.replace(char, "\\" + char)
    return text


def render_report(run_id, draft, bundle, status, request):
    if isinstance(draft, NarrativeDraft):
        return render_narrative(run_id, draft, bundle, status, request)
    report_id = stable_id("report", run_id, 1)
    sources = {s["id"]: s for s in bundle["sources"]}
    evidence = {e["id"]: e for e in bundle["evidence"]}
    numbers = {}
    citations = []
    english = request.output_language == "en"
    lines = [
        "# " + markdown_escape(draft.title),
        "",
        ("## Question and scope" if english else "## 问题与范围"),
        "",
        markdown_escape(request.query),
        "",
        ("Knowledge cutoff: " if english else "资料截止时间：") + request.as_of.isoformat(),
        "",
    ]
    for section in draft.sections:
        lines += ["## " + markdown_escape(section.heading), ""]
        for statement in section.statements:
            marks = []
            for eid in statement.evidence_ids:
                if eid not in evidence:
                    raise InvariantViolation("unknown report evidence")
                sid = evidence[eid]["source_id"]
                source = sources[sid]
                if sid not in numbers:
                    numbers[sid] = len(numbers) + 1
                ordinal = numbers[sid]
                url = quote(source_link(source), safe=":/?&=,%#@+~!$;-._")
                mark = f"[{ordinal}]({url})"
                if mark not in marks:
                    marks.append(mark)
                for cid in statement.claim_ids:
                    if any(r["claim_id"] == cid and r["evidence_id"] == eid for r in bundle["relations"]):
                        citations.append(
                            Citation(
                                citation_id=stable_id(
                                    "citation", report_id, statement.statement_id, cid, eid
                                ),
                                statement_id=statement.statement_id,
                                claim_id=cid,
                                evidence_id=eid,
                                ordinal=ordinal,
                            )
                        )
            lines += [markdown_escape(statement.text) + (" " + " ".join(marks) if marks else ""), ""]
    lines += ["## " + ("Limitations" if english else "局限"), ""]
    lines += ["- " + markdown_escape(x) for x in draft.limitations] or [
        "- " + ("No additional limitation recorded." if english else "未记录额外局限。")
    ]
    lines += ["", "## " + ("Unresolved questions" if english else "未解问题"), ""]
    lines += ["- " + markdown_escape(x) for x in draft.unresolved_questions] or [
        "- " + ("None recorded." if english else "无。")
    ]
    lines += ["", "## " + ("Sources" if english else "来源"), ""]
    for sid, ordinal in numbers.items():
        source = sources[sid]
        url = quote(source_link(source), safe=":/?&=,%#@+~!$;-._")
        published = source["published_at"] or ("unknown" if english else "未知")
        lines.append(
            f"{ordinal}. [{markdown_escape(source['title'])}]({url}) · "
            + ("Published/updated: " if english else "发布／更新：")
            + published
            + " · "
            + ("Retrieved: " if english else "抓取：")
            + source["retrieved_at"]
        )
    return Report(
        report_id=report_id,
        run_id=run_id,
        status=status,
        markdown="\n".join(lines) + "\n",
        structured=draft,
        citations=citations,
        created_at=utcnow(),
    )


def sentence_citations(text, citations):
    """Show each source once per sentence; never change stored atomic citations."""
    sentence_at, sentence = [], 0
    for char in text:
        sentence_at.append(sentence)
        if char in "。！？.!?\n":
            sentence += 1
    visible = {}
    for citation in citations:
        end = citation.end_char
        if end is None or not 0 <= end <= len(text):
            continue
        key = (sentence_at[max(0, end - 1)] if sentence_at else 0, citation.ordinal)
        if key not in visible or visible[key].end_char <= end:
            visible[key] = citation
    return list(visible.values())


def paragraph_citations(text, citations):
    valid = [c for c in citations if c.end_char is not None and 0 <= c.end_char <= len(text)]
    # One marker per source at block end, for both single and mixed sources.
    # These are presentation copies; stored atomic evidence spans never move.
    visible = {c.ordinal: c for c in valid}
    return [c.model_copy(update={"end_char": len(text)}) for c in visible.values()]


def example_label_required(paragraph):
    import re

    kinds = {a.kind for a in paragraph.assertions}
    explicit_mixed = (
        paragraph.block_type == "paragraph"
        and {"fact", "example"} <= kinds
        and re.search(r"例如|比如|举例|假设|假如|for example|suppose|imagine", paragraph.text, re.I)
    )
    return paragraph.illustrative and not explicit_mixed


def render_narrative(run_id, draft, bundle, status, request):
    from .narrative import anchor_span

    # Reuse the exact same source/citation identity rules as legacy reports.
    ordered = []
    positions = {}
    for section in draft.sections:
        for p in section.paragraphs:
            for a in sorted(p.assertions, key=lambda a: anchor_span(p, a)[1]):
                start, end = anchor_span(p, a)
                positions[a.statement_id] = (p.paragraph_id, start, end)
                ordered.append(
                    ReportStatement(
                        **a.model_dump(exclude={"anchor"}),
                    )
                    if a.kind != "example"
                    else ReportStatement(**{**a.model_dump(exclude={"anchor"}), "kind": "inference"})
                )
    proxy = ReportDraft(
        title=draft.title,
        sections=[ReportSection(heading="Findings", statements=ordered)],
        limitations=draft.limitations,
        unresolved_questions=draft.unresolved_questions,
    )
    base = render_report(run_id, proxy, bundle, status, request)
    citations = [
        c.model_copy(
            update={
                "paragraph_id": positions[c.statement_id][0],
                "start_char": positions[c.statement_id][1],
                "end_char": positions[c.statement_id][2],
            }
        )
        for c in base.citations
    ]
    source_map = {s["id"]: s for s in bundle["sources"]}
    evmap = {e["id"]: e for e in bundle["evidence"]}
    english = request.output_language == "en"
    lines = ["# " + markdown_escape(draft.title), ""]
    if status == "partial":
        lines += ["This answer still needs revision." if english else "本次回答尚待完善。", ""]
    if not any(s.paragraphs for s in draft.sections):
        lines += [
            "The research did not produce a verified answer; fetched sources remain in the research record."
            if english
            else "本次研究未能完成可靠的综合回答，已获取的资料仍保存在研究记录中。",
            "",
        ]
    for section in draft.sections:
        if section.heading:
            lines += ["## " + markdown_escape(section.heading), ""]
        for p in section.paragraphs:
            marks = {}
            for c in paragraph_citations(p.text, [c for c in citations if c.paragraph_id == p.paragraph_id]):
                url = source_link(source_map[evmap[c.evidence_id]["source_id"]])
                mark = f"[{c.ordinal}]({quote(url, safe=':/?&=,%#@+~!$;-._')})"
                marks.setdefault(c.end_char, [])
                if mark not in marks[c.end_char]:
                    marks[c.end_char].append(mark)
            if p.block_type != "paragraph":
                if p.illustrative:
                    lines += ["Illustration:" if english else "示意：", ""]
                if p.block_type == "code":
                    import re

                    fence = "`" * max(3, max((len(x) + 1 for x in re.findall(r"`+", p.text)), default=3))
                    lines += [fence + (p.language or ""), p.text, fence]
                elif p.block_type == "list":
                    lines += ["- " + markdown_escape(line) for line in p.text.splitlines() if line.strip()]
                else:
                    rows = [
                        [markdown_escape(cell).replace("|", "\\|") for cell in row.split("\t")]
                        for row in p.text.splitlines()
                    ]
                    lines += [
                        "| " + " | ".join(rows[0]) + " |",
                        "| " + " | ".join("---" for _ in rows[0]) + " |",
                    ]
                    lines += ["| " + " | ".join(row) + " |" for row in rows[1:]]
                lines += [" ".join(dict.fromkeys(mark for items in marks.values() for mark in items)), ""]
                continue
            cursor, parts = 0, []
            if example_label_required(p):
                parts.append("Illustration: " if english else "示意：")
            for end in sorted(marks):
                parts += [markdown_escape(p.text[cursor:end]), " " + " ".join(marks[end])]
                cursor = end
            parts.append(markdown_escape(p.text[cursor:]))
            lines += ["".join(parts), ""]
    sources_heading = "## Sources" if english else "## 来源"
    if citations:
        lines += [sources_heading + base.markdown.rsplit(sources_heading, 1)[1]]
    return base.model_copy(
        update={"structured": draft, "citations": citations, "markdown": "\n".join(lines).strip() + "\n"}
    )
