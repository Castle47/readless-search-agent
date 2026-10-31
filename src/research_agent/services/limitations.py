"""Reader-facing limitations; successful internal repairs remain in the audit trail."""

MESSAGES = {
    "search.degraded": (
        "部分搜索引擎暂时不可用，本报告使用了可取得的来源。",
        "Some search engines were unavailable; this report uses the sources obtained.",
    ),
    "source_truncated": (
        "部分来源较长，本次只分析了保存的正文范围。",
        "Some long sources were truncated to the stored text range.",
    ),
    "source_temporal_unknown": (
        "部分来源无法满足本次时间条件，未纳入确定结论。",
        "Some sources did not meet the requested time constraints.",
    ),
    "fetch.failed": (
        "部分候选网页无法取得正文，未作为事实证据。",
        "Some candidate pages could not be fetched and were not used as evidence.",
    ),
    "merge_degraded": (
        "部分主张的语义合并或冲突审核未完成，结论范围受到限制。",
        "Some claim equivalence or conflict checks could not be completed.",
    ),
    "planner_degraded": (
        "问题拆分使用了备用方案，部分研究维度可能仍需补充。",
        "A fallback plan was used; some research dimensions may need more work.",
    ),
    "understand_degraded": (
        "本次按原问题进行研究，问题理解未完成完整分析。",
        "Research proceeded from the original query without full intent analysis.",
    ),
    "extraction_input_too_large": (
        "部分资料无法在输入额度内完成核验，未纳入结论。",
        "Some material could not be verified within the input budget.",
    ),
    "evidence.rejected": (
        "部分原文片段未通过核验，未用于报告事实。",
        "Some extracted passages failed verification and were excluded.",
    ),
}


def warning_limitations(warnings, language="zh-CN"):
    output = []
    for warning in warnings:
        code = warning["code"]
        if code in {"evidence.span_repaired", "memory.degraded"}:
            continue
        text = MESSAGES.get(
            code,
            (
                "部分研究步骤未能完整执行，详情保留在研究记录中。",
                "Some research steps were incomplete; details are retained in the run record.",
            ),
        )[language == "en"]
        if text not in output:
            output.append(text)
    return output
