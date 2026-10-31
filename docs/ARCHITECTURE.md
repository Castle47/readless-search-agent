# Architecture

## Workflow

The root LangGraph persists every phase boundary in PostgreSQL. It generates a structured conversation summary, normalizes the question, creates separate public/private queries, recalls long-term memory, surveys candidates, dispatches independent research tasks, merges evidence, assesses gaps, writes a report, and verifies every answer block.

Each parallel research subgraph alternates between a bounded search/read action and a semantic readiness decision. Search snippets are candidate leads only. A source becomes usable after its fetched text is frozen, relevant passages are selected, and provenance is retained. The final writer receives original passages; notes and memory never substitute for evidence.

## Query rewrite and privacy boundary

The rewrite contract returns a standalone question, public search terms, a private query, and a `local`, `web`, or `hybrid` scope. A local-only scope disables SearxNG and public fetching. For hybrid requests, public query terms are fixed before private retrieval so private text cannot be copied into later public searches.

## Private retrieval

The indexer reads a bind-mounted vault in read-only mode. It rejects symlinks and hidden paths, hashes each file revision, chunks extracted text, and asks local Ollama for 768-dimensional `nomic-embed-text` vectors. SQLite stores the chunks, embeddings, revision metadata, and incremental progress.

At query time the application loads current vectors, performs an exact cosine scan, adds a small lexical-overlap score, and returns the highest-ranked current-revision chunks. This is intentionally simple for a personal corpus; it is not ANN retrieval. Retrieved passages enter the same immutable source/evidence pipeline as public sources.

## State, evidence, and memory

- LangGraph state plus PostgreSQL checkpoints are working memory for one run.
- The operation ledger commits remote-call outcomes before returning them to graph nodes, enabling replay after checkpoint loss.
- PostgreSQL stores canonical source snapshots, claims, evidence spans, citations, budgets, warnings, and reports.
- Letta is the only cross-run memory authority. It stores explicit preferences and provenance-aware lessons, source patterns, open questions, and conclusions. Recalled memory is an untrusted planning hint; final facts require current evidence.

## Failure model

Every run has search, fetch, HTTP-attempt, token, and wall-time limits. Unknown remote outcomes are conservatively reserved and reconciled. Source and model failures degrade to bounded retries or partial reports rather than unbounded loops. API requests are authenticated and idempotent.
