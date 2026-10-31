"""Conservative admission: resolve unknown subjects through research, not model memory."""

import re

from ..contracts import Facet, Understanding


def validate_intro_scope(request, required_questions):
    """Reject self-added mandatory history before the research brief is frozen."""
    from .conversation import history_messages

    query = request.query.strip()
    if not re.match(r"^(?:请|帮我|请你)?\s*(?:介绍|解释一下|introduce\b|explain\b|describe\b)", query, re.I):
        return
    expansion = re.compile(
        r"后来的|后续模型|发展史|演进史|later (?:language )?models|subsequent models|evolution|history of",
        re.I,
    )
    user_context = " ".join(m["content"] for m in history_messages(request.context) if m["role"] == "user")
    if expansion.search(query + " " + user_context):
        return
    if any(expansion.search(question) for question in required_questions):
        raise ValueError(
            "Keep unrequested later-model history/evolution optional; core questions concern this subject's purpose, mechanism and documented uses"
        )


# Full sentences only. Anything beyond these generic referential forms may be a
# searchable subject and must reach research. Never maintain a known-name list.
_ZH_REFERENCE_ONLY = re.compile(
    r"(?:请|请你|帮我|能否)?(?:介绍|解释|分析|研究|说明|说说|讲讲|比较|对比)?(?:一下|下)?"
    r"(?:它们?|这个|那个|这些|那些|上述|上面|前面|刚才)(?:的)?"
    r"(?:模型|方案|方法|项目|技术|文章|问题|东西|内容|结果)?"
    r"(?:是什么|是啥|如何实现|怎么实现|如何工作|怎么工作|怎么样|如何|的原理|"
    r"有什么用|有哪些特点|有哪些问题|有什么区别|的区别|的主要观点)?"
)
_EN_REFERENCE = (
    r"(?:it|them|this|that|these|those)(?: (?:model|models|method|project|article|approach|result))?"
)
_EN_REFERENCE_ONLY = re.compile(
    rf"(?:please )?(?:(?:explain|introduce|describe|research|compare|summarize) {_EN_REFERENCE}|"
    rf"(?:what (?:is|are)|how (?:does|do)) {_EN_REFERENCE}(?: work)?|{_EN_REFERENCE})",
    re.IGNORECASE,
)


def missing_referent(request):
    if request.context.strip():
        return False
    query = request.query.strip().strip("。？！.!? ")
    if re.search(r"\b[A-Z]{2,}\b", query):
        return False  # An acronym such as IT can itself be the research subject.
    return bool(
        _ZH_REFERENCE_ONLY.fullmatch(re.sub(r"\s+", "", query))
        or _EN_REFERENCE_ONLY.fullmatch(re.sub(r"\s+", " ", query))
    )


def fallback_understanding(request, *, discover=False):
    assumptions = []
    if discover:
        assumptions.append("研究对象的含义及背景尚待检索确认；先按原问题调查，不预设其作者、全称或性质。")
    if request.context.strip():
        assumptions.append("本次理解步骤未能完成；完整历史保存在请求中，不能作为事实证据或整段搜索词。")
    intro = re.match(
        r"^(?:请|帮我|请你|please\s+)?\s*(?:介绍|解释|讲解|讲讲|introduce\b|explain\b|describe\b)(?:一下|下)?\s*",
        request.query.strip(),
        re.IGNORECASE,
    )
    subject = request.query[intro.end() :].strip(" 。？！.!?") if intro else request.query
    terms = [subject or request.query]
    if intro:
        # Keep the user's named subject and explanatory intent on a failed
        # structured response; do not reduce it to a single fact lookup.
        chinese = bool(re.search(r"[\u4e00-\u9fff]", request.query))
        terms += [
            terms[0] + (" 原始资料" if chinese else " original source"),
            terms[0] + (" 原理 讲解 实例" if chinese else " explanation worked example"),
        ]
    return Understanding(
        standalone_query=request.query,
        intent="explanation" if intro else "survey",
        assumptions=assumptions,
        facets=[Facet(facet_id="main", question=request.query, required=True)],
        source_policy="primary_required",
        freshness="current",
        search_languages=["zh", "en"],
        reader_goal=request.query,
        needs_discovery=discover or bool(intro),
        search_terms=terms,
    )
