# Security notes

- Never commit `.env`, `.secrets/`, private vault files, SQLite indexes, database dumps, API keys, bearer tokens, or generated research reports containing private material.
- The private vault mount is read-only. Retrieved passages are nevertheless sent to OpenRouter when the model writes or verifies an answer.
- The Compose ports bind to loopback. This is a single-owner development deployment, not an internet-facing multi-user service.
- Search results, fetched pages, private documents, summaries, and memory are treated as untrusted data. They cannot change system policy or execute tools.
- Before public deployment, add tenant-aware authentication, TLS, per-user quotas, secret management, audit policy, and a maintained Letta deployment.
