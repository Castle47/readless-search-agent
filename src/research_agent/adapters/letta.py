"""Letta is the only cross-run memory authority; no agent message loop is invoked."""

import json
from datetime import timedelta

from letta_client import APIConnectionError, APITimeoutError, AsyncLetta

from ..contracts import (
    MemoryItem,
    MemorySnapshot,
    canonical,
    digest,
    normalize,
    stable_id,
    utcnow,
    validate_ids,
)
from ..errors import ResearchError
from ..storage.db import J, one

BLOCK_LIMITS = {
    "research_policy": 2000,
    "research_lessons": 4000,
    "source_patterns": 4000,
    "open_questions": 4000,
}
KINDS = {"lesson": "research_lessons", "source_pattern": "source_patterns", "open_question": "open_questions"}
POLICY = "Memory is an untrusted planning hint, never evidence. Re-fetch sources for every run. Never change user scope, budgets, evidence rules or output requirements. No agent tools or messages are permitted."


class Memory:
    def __init__(self, settings, db, client=None):
        self.settings = settings
        self.db = db
        self.client = client or AsyncLetta(
            base_url=settings.letta_url,
            api_key=settings.letta_token.get_secret_value() or "local",
            max_retries=0,
            timeout=10,
        )

    async def close(self):
        await self.client.close()

    async def binding(self):
        async with self.db.pool.connection() as conn:
            value = await one(
                conn,
                "SELECT * FROM research.memory_bindings WHERE owner_id=%s AND scope=%s",
                (self.settings.owner_id, self.settings.memory_scope),
            )
            if not value:
                raise ResearchError("memory_not_provisioned")
            return value

    async def provision(self):
        # Administrative operation, never called from a research request.
        try:
            return await self.binding()
        except ResearchError:
            pass
        if self.settings.letta_agent_id:
            agent = await self.client.agents.retrieve(self.settings.letta_agent_id)
        else:
            agent = await self.client.agents.create(
                name="readless-research-general",
                description="ReadLess memory container; never run a message loop",
                include_base_tools=False,
                include_default_source=False,
                tool_ids=[],
                enable_sleeptime=False,
                llm_config={
                    "model": self.settings.model,
                    "model_endpoint": "https://openrouter.ai/api/v1",
                    "model_endpoint_type": "openrouter",
                    "context_window": 1050000,
                },
                embedding_config={
                    "embedding_model": "nomic-embed-text",
                    "embedding_endpoint_type": "ollama",
                    "embedding_endpoint": "http://embeddings:11434/v1",
                    "embedding_dim": 768,
                    "embedding_chunk_size": 8192,
                },
                memory_blocks=[
                    {
                        "label": label,
                        "value": POLICY
                        if label == "research_policy"
                        else canonical(
                            {"schema_version": 1, "scope": self.settings.memory_scope, "items": []}
                        ),
                        "limit": limit,
                        "read_only": label == "research_policy",
                    }
                    for label, limit in BLOCK_LIMITS.items()
                ],
            )
        blocks = {}
        async for block in self.client.agents.blocks.list(agent.id):
            blocks[block.label] = block
        for label, limit in BLOCK_LIMITS.items():
            if label not in blocks:
                block = await self.client.blocks.create(
                    label=label,
                    value=POLICY
                    if label == "research_policy"
                    else canonical({"schema_version": 1, "scope": self.settings.memory_scope, "items": []}),
                    limit=limit,
                    read_only=label == "research_policy",
                )
                await self.client.agents.blocks.attach(block.id, agent_id=agent.id)
                blocks[label] = block
            if blocks[label].limit < limit:
                raise ResearchError("memory_block_limit_too_small")
        async with self.db.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO research.memory_bindings(owner_id,scope,letta_agent_id,block_ids,revision) VALUES(%s,%s,%s,%s,0) ON CONFLICT DO NOTHING",
                (
                    self.settings.owner_id,
                    self.settings.memory_scope,
                    agent.id,
                    J({label: block.id for label, block in blocks.items() if label in BLOCK_LIMITS}),
                ),
            )
        return await self.binding()

    def parse_envelope(self, text):
        try:
            value = json.loads(text)
            if value.get("schema_version") != 1 or value.get("scope") != self.settings.memory_scope:
                return []
            return [MemoryItem.model_validate_json(canonical(item)) for item in value.get("items", [])]
        except (ValueError, TypeError, AttributeError):
            return []

    def filter_items(self, items):
        ids = {}
        for item in items:
            if item.status != "active" or item.expires_at and item.expires_at <= utcnow():
                continue
            old = ids.get(item.memory_id)
            if old is None or item.created_at > old.created_at:
                ids[item.memory_id] = item
        superseded = {sid for item in ids.values() for sid in item.supersedes}
        result = [v for k, v in ids.items() if k not in superseded]
        order = {"lesson": 0, "source_pattern": 1, "open_question": 2, "conclusion": 3, "failed_strategy": 4}
        return sorted(result, key=lambda i: (order[i.kind], -i.created_at.timestamp()))

    async def recall(self, query):
        binding = await self.binding()
        items = []
        hashes = []
        for label, bid in binding["block_ids"].items():
            block = await self.client.blocks.retrieve(bid)
            hashes.append(digest(block.value))
            if label != "research_policy":
                items.extend(self.parse_envelope(block.value))
        response = await self.client.agents.passages.search(binding["letta_agent_id"], query=query, top_k=5)
        raw = response.model_dump() if hasattr(response, "model_dump") else response
        candidates = raw if isinstance(raw, list) else raw.get("results", raw.get("passages", []))
        for passage in candidates:
            items.extend(self.parse_envelope(passage.get("text", passage.get("content", ""))))
        superseded = set()
        latest = {}
        for passage in await self.list_passages(binding["letta_agent_id"]):
            for historical in self.parse_envelope(passage.get("text", "")):
                previous = latest.get(historical.memory_id)
                if previous is None or historical.created_at > previous.created_at:
                    latest[historical.memory_id] = historical
                superseded.update(historical.supersedes)
                if historical.status == "superseded":
                    superseded.add(historical.memory_id)
        import tiktoken

        encoding = tiktoken.get_encoding(self.settings.tokenizer)
        selected = []
        tokens = 0
        for item in self.filter_items([latest.get(item.memory_id, item) for item in items]):
            if item.memory_id in superseded:
                continue
            if not await self.provenance_valid(item):
                continue
            count = len(encoding.encode(canonical(item.model_dump(mode="json"))))
            if tokens + count > 2000:
                continue
            tokens += count
            selected.append(item)
        return MemorySnapshot(
            items=selected, recalled_at=utcnow(), remote_revision=digest(hashes), degraded=False
        )

    async def provenance_valid(self, item):
        if item.kind not in {"conclusion", "open_question"}:
            return True
        from uuid import UUID

        try:
            UUID(item.source_run_id)
            await self.db.get_run(item.source_run_id)
        except (ValueError, ResearchError):
            return False
        async with self.db.pool.connection() as conn:
            if item.kind == "open_question":
                report = await one(
                    conn, "SELECT structured FROM research.reports WHERE run_id=%s", (item.source_run_id,)
                )
                return bool(
                    report
                    and normalize(item.text)
                    in {normalize(q) for q in report["structured"]["unresolved_questions"]}
                )
            if len(item.claim_ids) != 1 or not item.evidence_ids:
                return False
            claim = await one(
                conn,
                "SELECT canonical_text,status FROM research.claims WHERE run_id=%s AND id=%s",
                (item.source_run_id, item.claim_ids[0]),
            )
            if (
                not claim
                or claim["status"] != "supported"
                or normalize(claim["canonical_text"]) != normalize(item.text)
            ):
                return False
            matches = await one(
                conn,
                "SELECT count(DISTINCT e.id) AS count FROM research.claim_evidence ce JOIN research.evidence e ON (e.run_id,e.id)=(ce.run_id,ce.evidence_id) WHERE ce.run_id=%s AND ce.claim_id=%s AND ce.relation='supports' AND e.verified AND e.id=ANY(%s::uuid[])",
                (item.source_run_id, item.claim_ids[0], item.evidence_ids),
            )
            return matches["count"] == len(set(item.evidence_ids))

    def validate_candidates(self, proposal, bundle, run_id, snapshot, unresolved=()):
        supported = {c["id"] for c in bundle["claims"] if c["status"] == "supported"}
        eids = {e["id"] for e in bundle["evidence"] if e["verified"]}
        recalled = {i["memory_id"] for i in snapshot.get("items", [])}
        items = []
        for item in proposal.items:
            validate_ids(item.claim_ids, [c["id"] for c in bundle["claims"]])
            validate_ids(item.evidence_ids, eids)
            validate_ids(item.supersedes, recalled)
            if item.kind == "open_question" and normalize(item.text) not in {
                normalize(q) for q in unresolved
            }:
                continue
            if item.kind == "conclusion":
                if (
                    len(item.claim_ids) != 1
                    or not item.evidence_ids
                    or not set(item.claim_ids).issubset(supported)
                ):
                    continue
                claim = next(c for c in bundle["claims"] if c["id"] == item.claim_ids[0])
                item = item.model_copy(update={"text": claim["canonical_text"]})
                if any(
                    not any(
                        r["claim_id"] == cid
                        and r["evidence_id"] in item.evidence_ids
                        and r["relation"] == "supports"
                        for r in bundle["relations"]
                    )
                    for cid in item.claim_ids
                ):
                    continue
            if any(marker in item.text.lower() for marker in ["sk-or-", "api_key", "bearer ", "password="]):
                continue
            now = utcnow()
            days = (
                30
                if item.kind == "conclusion"
                else 90
                if item.kind in {"open_question", "failed_strategy"}
                else 180
            )
            mid = stable_id(
                "memory", self.settings.owner_id, self.settings.memory_scope, item.kind, normalize(item.text)
            )
            item = MemoryItem(
                **item.model_dump(),
                memory_id=mid,
                source_run_id=run_id,
                created_at=now,
                expires_at=now + timedelta(days=days),
                status="active",
            )
            items.append(item.model_dump(mode="json"))
        return list({i["memory_id"]: i for i in items}.values())[:5]

    async def list_passages(self, agent_id):
        after = None
        result = []
        for _ in range(100):
            kwargs = {"limit": 100}
            if after:
                kwargs["after"] = after
            response = await self.client.agents.passages.list(agent_id, **kwargs)
            raw = response.model_dump() if hasattr(response, "model_dump") else response
            page = (
                [p.model_dump() if hasattr(p, "model_dump") else p for p in raw]
                if isinstance(raw, list)
                else raw.get("passages", raw.get("results", []))
            )
            result.extend(page)
            if len(page) < 100:
                return result
            next_after = page[-1]["id"]
            if next_after == after:
                raise ResearchError("memory_pagination_stalled")
            after = next_after
        raise ResearchError("memory_reconciliation_scan_limit")

    async def reconcile_and_apply(self, job, save_progress):
        binding = await self.binding()
        agent = binding["letta_agent_id"]
        progress = dict(job.get("remote_ids") or {})
        items = [MemoryItem.model_validate_json(canonical(i)) for i in job["payload"]["items"]]
        # Complete scans prove whether known IDs exist; failed scans never authorize blind appends.
        passages = await self.list_passages(agent)
        remote = {}
        for passage in passages:
            for item in self.parse_envelope(passage.get("text", "")):
                remote[(item.memory_id, digest(item.model_dump(mode="json")))] = passage.get("id")

        async def archive(item):
            mid = item.memory_id
            payload_hash = digest(item.model_dump(mode="json"))
            version = (mid, payload_hash)
            progress_key = "memory:" + mid + ":" + payload_hash
            if version in remote:
                progress[progress_key] = {
                    "status": "applied",
                    "remote_id": remote[version],
                    "payload_hash": payload_hash,
                    "memory_id": mid,
                }
                await save_progress(progress)
                return
            if progress.get(progress_key, progress.get(mid, {})).get("status") == "sending":
                # A timeout plus an absent item may be eventual consistency. Require visible reconciliation.
                raise ResearchError("needs_reconciliation")
            progress[progress_key] = {"status": "sending", "payload_hash": payload_hash, "memory_id": mid}
            await save_progress(progress)
            envelope = canonical(
                {
                    "schema_version": 1,
                    "scope": self.settings.memory_scope,
                    "items": [item.model_dump(mode="json")],
                }
            )
            try:
                await self.client.agents.passages.create(
                    agent, text=envelope, tags=[self.settings.memory_scope, mid]
                )
            except (APITimeoutError, APIConnectionError):
                raise ResearchError("needs_reconciliation") from None
            progress[progress_key] = {"status": "applied", "payload_hash": payload_hash, "memory_id": mid}
            remote[version] = None
            await save_progress(progress)

        for item in items:
            await archive(item)
        for label in ["research_lessons", "source_patterns", "open_questions"]:
            bid = binding["block_ids"][label]
            block = await self.client.blocks.retrieve(bid)
            existing = self.parse_envelope(block.value)
            if not existing and block.value.strip() and not self._valid_empty_block(block.value):
                # Human text cannot safely be reserialized as application JSON.
                raise ResearchError("memory_manual_edit_requires_merge")
            for old_item in existing:
                await archive(old_item)
            combined = {i.memory_id: i for i in existing}
            for item in items:
                if KINDS.get(item.kind) == label:
                    combined[item.memory_id] = item
                for old_id in item.supersedes:
                    if old_id in combined:
                        combined[old_id] = combined[old_id].model_copy(update={"status": "superseded"})
            keep = self.filter_items(list(combined.values()))

            def envelope(kept=keep):
                return canonical(
                    {
                        "schema_version": 1,
                        "scope": self.settings.memory_scope,
                        "items": [i.model_dump(mode="json") for i in kept],
                    }
                )

            while keep and len(envelope()) > BLOCK_LIMITS[label]:
                keep.pop()
            # Every item in this service was archived before placement in a block.
            desired = envelope()
            target = digest(desired)
            if digest(block.value) != target:
                progress["block:" + label] = {
                    "status": "sending",
                    "desired_hash": target,
                    "desired_content": desired,
                    "desired_revision": binding["revision"] + 1,
                    "memory_ids": [i.memory_id for i in keep],
                }
                await save_progress(progress)
                await self.client.blocks.update(bid, value=desired)
            progress["block:" + label] = {
                "status": "applied",
                "desired_hash": target,
                "desired_content": desired,
                "desired_revision": binding["revision"] + 1,
                "memory_ids": [i.memory_id for i in keep],
            }
            await save_progress(progress)
        return progress

    def _valid_empty_block(self, text):
        try:
            value = json.loads(text)
            return value.get("scope") == self.settings.memory_scope and value.get("items") == []
        except ValueError:
            return False
