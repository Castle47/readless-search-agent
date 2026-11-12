# ReadLess Search Agent

[中文文档](README.zh-CN.md) · English

An evidence-first search agent built with Python and LangGraph. It rewrites conversational questions, plans parallel research, retrieves public and private knowledge, preserves source snapshots, and produces cited answers that survive interruption and replay.

## What it includes

- Stateful LangGraph workflow: conversation summary → intent understanding → query rewrite → private retrieval → memory recall → discovery → planning → parallel research → evidence merge → bounded peer consultation → gap analysis → writing → verification.
- OpenRouter model adapter with strict JSON Schema outputs, per-role token limits, operation replay, bounded retries, and explicit cost accounting.
- Self-hosted SearxNG metasearch configured for Bing and Yahoo candidates.
- Read-only private RAG for Markdown, text, and text-based PDF files. Ollama runs `nomic-embed-text` locally; SQLite schema v3 stores versioned files, exact text spans, reusable float32 embeddings, and a synchronized FTS5 index. Retrieval combines exact vector search and title-weighted BM25 using RRF.
- PostgreSQL-backed checkpoints, source snapshots, claims, evidence spans, citations, budgets, and operation ledger.
- Letta-backed cross-run memory for explicit preferences, reusable research lessons, source patterns, open questions, and evidence-bound conclusions.
- English chunks use a local, untrained spaCy Sentencizer with a 300-word budget and up to 30-word overlap; Chinese/mixed text retains a 1,800-character budget and up to 200-character overlap.
- Bounded researcher consultation before writing and verifier-to-researcher follow-up after review, with source-checked references, replay and dedicated budget caps.
- Citation-preserving Markdown and JSON exports.

## Architecture

```text
Question + recent conversation
        │
        ▼
Conversation summary → Intent → Query rewrite
        │                         ├── public queries → SearxNG
        │                         └── private query → SQLite vector index
        ▼
Research plan → parallel source reading → peer consultation → gap-directed search
        ▼
Draft → source-grounded verification → targeted peer follow-up → edit/recheck → cited report

LangGraph state/checkpoints ─────────────── PostgreSQL
Cross-run semantic memory ───────────────── Letta + local Ollama embeddings
```

More detail is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Quick start

Requirements: Docker Desktop or Docker Engine with Compose.

```bash
cp .env.example .env
# Edit .env: set OPENROUTER_API_KEY, API_BEARER_TOKEN,
# POSTGRES_PASSWORD, and VAULT_PATH.

docker compose up -d --build
docker compose exec embeddings ollama pull nomic-embed-text
docker compose exec api research-agent provision-memory
docker compose --profile indexing run --rm vault-indexer
curl http://127.0.0.1:8000/readyz
```

Open the API documentation at <http://127.0.0.1:8000/docs>.

Run a research request from inside the API container:

```bash
docker compose exec api research-agent run \
  --query 'Explain prefix caching and cite the sources.' \
  --wait --output /tmp/report.md
```

The API requires the bearer token configured in `.env`. `POST /v1/research` also requires an `Idempotency-Key`; repeated requests with the same key and body replay the same run.

## Private knowledge RAG

Set `VAULT_PATH` to a local folder. Compose mounts it read-only at `/knowledge/obsidian`; the original files are never modified. The indexer:

1. extracts Markdown, UTF-8 text, and text-based PDF pages;
2. chunks text while retaining file revision and line/page location;
3. asks local Ollama to generate 768-dimensional `nomic-embed-text` vectors;
4. stores vectors in a private SQLite index;
5. rejects stale index entries when the source-file hash changes.

Retrieval is an exact full scan over the current index, not an approximate-nearest-neighbor service. Each corpus returns up to 50 fused candidates from vector and BM25 retrieval; cross-corpus fusion retains up to 8 chunks before grouping them by document. BM25 weights are path/title/body = 0.25/3/1. Ranking does not establish factual support. Re-run the indexer after changing the vault:

```bash
docker compose --profile indexing run --rm vault-indexer
```

Scanned PDFs and images are not OCRed. Files with extraction warnings are skipped for review.

Embedding is local, but retrieved private passages are sent to the configured OpenRouter model when it writes and verifies an answer. Explicitly private-only requests disable public search and public web fetching.

SQLite migrations back up older databases before rebuilding the lexical index. Chunk-policy changes are applied on the next index refresh; unchanged model inputs reuse embeddings. No trained spaCy model is downloaded.

Optional adapters for ReadLess website snapshots, published blogs and registered folders are included in Python. The standalone Compose file mounts only `VAULT_PATH`; using the additional corpora requires the host manifests and read-only mounts described in [integrations/readless](integrations/readless/README.md). That directory contains host-specific overlay sources, not a standalone website.

Details: [SQLite schema](docs/sqlite-rag-bm25-v3.md), [chunking](docs/rag-chunking-v2.md), [peer consultation](docs/peer-consultation-v1.md).

## Peer consultation

Up to three existing research tasks can exchange evidence-bound questions for at most two rounds, with at most two questions per round. Unresolved issues enter the existing supplemental-research path. The answer verifier can then direct up to two questions to a relevant researcher. Discussion never marks a claim as verified or overrides the independent final review.

The two peer roles share a 48,000-token / 16-HTTP-attempt cap within the existing total budget. Old run snapshots without the feature flag retain the prior path. Messages and citation locations are recorded in the authenticated run-events API. The default generation model is now `openai/gpt-6-luna` with high reasoning.

## Memory model

- **Working memory:** LangGraph state and PostgreSQL checkpoints for the current run.
- **Conversation summary:** model-generated structured summary with user constraints and unresolved questions; original user messages retain priority.
- **Long-term memory:** Letta stores explicit user preferences and reusable, provenance-aware research memory. Memory is planning context, never final evidence.
- **Caches:** SQLite caches chunk embeddings; PostgreSQL replays completed model, search, fetch, and source-reading operations within a run.

To import explicit preferences, copy the example and edit it before running:

```bash
cp configs/user-preferences.example.json configs/user-preferences.json
docker compose exec api research-agent import-preferences \
  /app/configs/user-preferences.json
```

`configs/user-preferences.json` is ignored by Git.

## Development

```bash
uv sync --frozen
uv run pytest tests/unit -q
# With the test PostgreSQL service available:
uv run pytest -m 'not live'
```

Unit tests use controlled model/search/fetch adapters and do not spend API credits. Integration tests additionally require PostgreSQL. Live tests require explicit opt-in and an already completed run.

## Scope and security

This repository is designed for a single local owner. The API binds to loopback in Compose, but it is not a production multi-tenant service. Add real authentication, tenant isolation, TLS, per-user quotas, and secret management before exposing it publicly.

Web pages, search snippets, private documents, conversation summaries, and recalled memory are treated as untrusted data. Only frozen source passages can become cited evidence. The fetcher validates public destinations, observes robots rules, limits response size, and does not bypass login or paywalls.

No license is granted by this repository unless a license file is added by the owner.
