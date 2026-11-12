# Architecture

## Workflow

The root LangGraph persists every phase boundary in PostgreSQL. It generates a structured conversation summary, normalizes the question, creates separate public/private queries, recalls long-term memory, surveys candidates, dispatches independent research tasks, merges evidence, runs bounded peer consultation, assesses gaps, writes a report, and verifies every answer block.

Each parallel research subgraph alternates between a bounded search/read action and a semantic readiness decision. Search snippets are candidate leads only. A source becomes usable after its fetched text is frozen, relevant passages are selected, and provenance is retained. The final writer receives original passages; notes and memory never substitute for evidence.

## Query rewrite and privacy boundary

The rewrite contract returns a standalone question, public search terms, a private query, and a `local`, `web`, or `hybrid` scope. A local-only scope disables SearxNG and public fetching. For hybrid requests, public query terms are fixed before private retrieval so private text cannot be copied into later public searches.

## Private retrieval

The indexer reads a bind-mounted vault in read-only mode. It rejects symlinks and hidden paths, hashes each file revision, chunks extracted text, and asks local Ollama for 768-dimensional `nomic-embed-text` vectors. SQLite stores the chunks, embeddings, revision metadata, and incremental progress.

English passages use spaCy's blank-English tokenizer and rule-based Sentencizer, with up to 300 words and 30 words of overlap. Chinese/mixed input uses up to 1,800 characters and 200 characters of overlap. Fenced code and pipe tables are protected where they fit; all chunks remain exact original-text slices.

SQLite schema v3 separates files, chunks, reusable float32 embeddings, metadata and a three-field FTS5 index. At query time, source validity is checked before limiting candidates. Exact cosine retrieval and BM25 (path/title/body weights 0.25/3/1) each select up to 50 candidates. RRF combines ranks; cross-corpus fusion retains up to 8 chunks before document grouping. This is not ANN retrieval and scores are not factual confidence.

## Researcher consultation

After the initial research tasks join, up to three task identities exchange bounded, addressed questions over frozen passages. At most two questions per round and two rounds are permitted. Outstanding questions can create supplemental rereads, but cannot create verified claims or expand required criteria. During answer repair, the verifier can ask the relevant researcher up to two targeted questions. The original independent answer-review findings remain in force.

Question/reply inputs, material snapshots and messages are recorded before graph advancement. Run events include participant IDs and exact source locations. The existing operation ledger accounts for a 48,000-token and 16-attempt peer sublimit inside the total run budget. Old run snapshots lacking the flag skip this path.

## State, evidence, and memory

- LangGraph state plus PostgreSQL checkpoints are working memory for one run.
- The operation ledger commits remote-call outcomes before returning them to graph nodes, enabling replay after checkpoint loss.
- PostgreSQL stores canonical source snapshots, claims, evidence spans, citations, budgets, warnings, and reports.
- Letta is the only cross-run memory authority. It stores explicit preferences and provenance-aware lessons, source patterns, open questions, and conclusions. Recalled memory is an untrusted planning hint; final facts require current evidence.

## Failure model

Every run has search, fetch, HTTP-attempt, token, and wall-time limits. Unknown remote outcomes are conservatively reserved and reconciled. Source and model failures degrade to bounded retries or partial reports rather than unbounded loops. API requests are authenticated and idempotent.
