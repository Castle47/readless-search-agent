"""Rank fusion and evidence eligibility are deliberately separate decisions."""

METHOD = "bm25-vector-rrf-v1"
RRF_K = 60
CANDIDATE_WINDOW = 50
MIN_SEMANTIC = 0.3
MIN_LEXICAL_COVERAGE = 0.5


def rrf_score(semantic_rank, lexical_rank):
    return sum(1.0 / (RRF_K + rank) for rank in (semantic_rank, lexical_rank) if rank is not None)


def fuse(rows, limit):
    """Global vector ranking + each corpus's BM25 rank; never compare raw BM25 across corpora."""
    if limit <= 0:
        return []
    rows = [dict(row) for row in rows]
    semantic = sorted(
        (row for row in rows if row["semantic_score"] >= MIN_SEMANTIC),
        key=lambda row: (-row["semantic_score"], row.get("corpus_id", ""), row["id"]),
    )[:CANDIDATE_WINDOW]
    ranks = {(row.get("corpus_id", ""), row["id"]): i for i, row in enumerate(semantic, 1)}
    results = []
    for row in rows:
        semantic_rank = ranks.get((row.get("corpus_id", ""), row["id"]))
        lexical_rank = row.get("bm25_rank")
        lexical_relevant = lexical_rank is not None and row["lexical_coverage"] >= MIN_LEXICAL_COVERAGE
        if semantic_rank is None and not lexical_relevant:
            continue
        # A stray word must not give an unrelated document an additional fusion vote.
        if not lexical_relevant:
            lexical_rank = None
        row["semantic_rank"] = semantic_rank
        row["score"] = rrf_score(semantic_rank, lexical_rank)
        row["retrieval_method"] = METHOD
        row["strong_match"] = bool(
            semantic_rank is not None
            and semantic_rank <= 5
            and lexical_rank is not None
            and lexical_rank <= 5
            and row["semantic_score"] >= 0.8
            and row["lexical_coverage"] >= 0.5
        )
        results.append(row)
    return sorted(
        results,
        key=lambda row: (
            -row["score"],
            -row["semantic_score"],
            -row["lexical_coverage"],
            row.get("corpus_id", ""),
            row["id"],
        ),
    )[:limit]


def is_strong_source(source):
    if source.get("retrieval_method") == METHOD:
        return source.get("strong_match") is True
    # Replayed pre-upgrade runs retain their original scoring contract.
    return not source.get("retrieval_method") and source.get("retrieval_score", 0) >= 0.8
