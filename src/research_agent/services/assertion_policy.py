"""Body support is distinct from completion of a frozen research criterion.

The independent auditor classifies meaning; these conservative checks reject
obvious attempts to relabel primary/empirical claims as ordinary explanation.
Neither this classification nor a document's identity establishes primary
authority or independent replication.
"""

import re


CORE_ASSERTION = re.compile(
    r"官方|保证|承诺|首次|最早|最初|提出|发布于|发明|作者|由.{0,100}(?:发布|开发|设计)"
    r"|(?:原始|原生)[^，。；“”\n]{0,35}(?:使用|采用|是|包含|架构)|原论文|原始论文"
    r"|接口|端点|(?:API|SDK)\b|实测|测量|实验结果|基准|准确率|延迟|吞吐|参数量"
    r"|\b(?:official|guarantee\w*|promise\w*|invent\w*|author\w*|introduced|released|"
    r"first proposed|benchmark\w*|measured|latency|throughput|accuracy|endpoint\w*)\b"
    r"|\boriginal\b.{0,100}\b(?:architecture|uses|used|is|was|contains)\b",
    re.I,
)


def ordinary_explanation(text, purpose):
    return purpose == "explanation" and not CORE_ASSERTION.search(text)


def explanation_supported(claim, bundle):
    """Permit a directly supported body claim without promoting its criterion."""
    if not ordinary_explanation(claim["canonical_text"], "explanation"):
        return False
    evidence = {e["id"]: e for e in bundle["evidence"]}
    relations = [r for r in bundle["relations"] if r["claim_id"] == claim["id"]]
    if any(r["relation"] == "contradicts" for r in relations) or any(
        claim["id"] in {c["claim_a_id"], c["claim_b_id"]} for c in bundle["conflicts"]
    ):
        return False
    return any(
        r["relation"] == "supports"
        and r["verification"].get("assertion_purpose") == "explanation"
        and r["verification"].get("body_evidence_requirement") == "traceable_direct"
        and r["verification"].get("primary_provenance")
        and r["verification"].get("direct")
        and evidence[r["evidence_id"]]["verified"]
        and evidence[r["evidence_id"]]["directness"] == "direct"
        for r in relations
    )
