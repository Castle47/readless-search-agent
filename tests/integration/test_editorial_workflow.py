from tests.integration.test_narrative_graph import ConnectedModel, run_fixture


async def test_optional_facet_does_not_invalidate_an_otherwise_complete_plan(db, settings):
    class OptionalFacet(ConnectedModel):
        def response(self, role, p):
            value = super().response(role, p)
            if role == "understand":
                value["understanding"]["facets"].append(
                    {"facet_id": "optional", "question": "Optional context", "required": False}
                )
            if role == "planner" and not p["batch_no"]:
                value["tasks"][0]["facet_ids"].append("optional")
                value["tasks"][0]["success_criteria"][0]["facet_ids"] = ["f0"]
            return value

    _, run, report = await run_fixture(db, settings, OptionalFacet)
    assert run["status"] == "completed", run.get("warnings")
    assert not any(w["code"] == "planner_degraded" for w in run["warnings"])
    optional = next(c for c in report.structured.coverage if not c.required)
    assert optional.status == "unresolved"


async def test_complete_draft_reaches_review_and_one_editor_before_removal(db, settings):
    seen = []

    class WholeAnswer(ConnectedModel):
        def response(self, role, p):
            value = super().response(role, p)
            if role == "writer":
                first = value["sections"][0]["paragraphs"][0]
                value["sections"][0]["paragraphs"].append(
                    {**first, "text": first["text"] + " It is always correct."}
                )
            if role == "report_verifier":
                for para, audit in zip(p["paragraphs"], value["paragraphs"], strict=True):
                    if "always correct" in para["text"]:
                        audit.update(complete=False, unsupported_text=["It is always correct."])
            if role == "quality":
                assert len(p["paragraphs"]) == 2 and "always correct" in p["paragraphs"][1]["text"]
                seen.append("intact_review")
                return {
                    "issues": [
                        {
                            "paragraph_ids": ["p1", "p2"],
                            "problem": "Repeated explanation.",
                            "suggestion": "Keep one supported explanation.",
                        }
                    ],
                    "strengths": [],
                }
            if role == "editor":
                assert len(p["draft"]["sections"][0]["paragraphs"]) == 2
                assert {f["feedback_id"][0] for f in p["feedback"]} == {"F", "Q"}
                seen.append("combined_feedback")
                value["sections"][0]["paragraphs"] = value["sections"][0]["paragraphs"][:1]
            return value

    s, run, report = await run_fixture(db, settings, WholeAnswer)
    assert run["status"] == "completed", run.get("warnings")
    assert seen == ["intact_review", "combined_feedback"]
    assert report.structured.quality_status == "revised"
    assert len(report.structured.sections[0].paragraphs) == 1
    assert "always correct" not in report.markdown
    assert [r for r, _ in s.llm.calls].count("editor") == 1
    assert [r for r, _ in s.llm.calls].count("report_verifier") == 1  # Unchanged checked block is reused.
    assert "repair" not in [r for r, _ in s.llm.calls]


async def test_rich_blocks_roundtrip_without_execution_or_lost_anchors(db, settings):
    class RichAnswer(ConnectedModel):
        def response(self, role, p):
            value = super().response(role, p)
            if role == "writer":
                first = value["sections"][0]["paragraphs"][0]
                value["sections"][0]["paragraphs"] += [
                    {
                        **first,
                        "block_type": "table",
                        "text": "Input\tOutput\nCedar accepts raw text\treturns a boolean",
                    },
                    {
                        **first,
                        "block_type": "code",
                        "language": "python",
                        "illustrative": True,
                        "text": "# Cedar accepts raw text\n# returns a boolean\npayload = '<script>literal text</script>'",
                    },
                    {**first, "block_type": "list", "text": "Cedar accepts raw text\nreturns a boolean"},
                ]
            return value

    _, run, report = await run_fixture(db, settings, RichAnswer)
    assert run["status"] == "completed", run.get("warnings")
    ps = report.structured.sections[0].paragraphs
    assert [p.block_type for p in ps] == ["paragraph", "table", "code", "list"]
    assert "```python" in report.markdown and "| Input | Output |" in report.markdown
    assert len(report.citations) == 8
    assert "研究局限" not in report.markdown and "尚未解决的问题" not in report.markdown
    for p in ps:
        assert not p.text.startswith("推断：")
        for c in [c for c in report.citations if c.paragraph_id == p.paragraph_id]:
            a = next(a for a in p.assertions if a.statement_id == c.statement_id)
            assert p.text[c.start_char : c.end_char] == a.anchor
