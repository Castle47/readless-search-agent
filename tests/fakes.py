import asyncio
import hashlib
import json

from research_agent.contracts import FetchResult, MemorySnapshot, SearchHit, canonical, stable_id, utcnow


class ScriptedLLM:
    def __init__(self, ops, scenario):
        self.ops = ops
        self.scenario = scenario
        self.calls = []
        self.max_active = 0
        self.active = 0

    def build(self, role, payload, schema, correction=None):
        return {}, 500

    async def generate(
        self,
        key,
        role,
        payload,
        schema,
        *,
        task_id=None,
        partition="research",
        validator=None,
        is_recheck=False,
    ):
        self.calls.append((role, payload))

        async def call(timeout):
            value = self.response(role, payload)
            if schema.__name__ == "CompactBatchParagraphAudit":
                for p in value["paragraphs"]:
                    for atom in p["atoms"]:
                        atom.pop("text", None)
            await asyncio.sleep(0.001)
            return {"payload": value, "usage": {"total_tokens": 100}}

        raw = await self.ops.run(
            key,
            "llm",
            payload,
            call,
            task_id=task_id,
            partition=partition,
            tokens=500,
            model_role=role,
            is_recheck=is_recheck,
        )
        value = schema.model_validate_json(canonical(raw["payload"]))
        if validator:
            validator(value)
        return value

    def response(self, role, p):
        scenario = self.scenario
        count = scenario.get("tasks", 1)
        if role == "query_rewrite":
            return {"standalone_query": p["question"], "local_query": p["question"], "search_terms": p["understanding"].get("search_terms") or [p["question"]], "scope": "web"}
        if role == "conversation_summary":
            return {"summary": "Fixture conversation summary", "user_constraints": [], "unresolved_questions": []}
        if role == "source_selector":
            return {"decisions": [{"candidate_id": c["candidate_id"], "read": i < p["max_pages"], "reason": "Relevant fixture source"} for i, c in enumerate(p["candidates"])]}
        if role == "understand":
            return {
                "understanding": {
                    "standalone_query": p["query"],
                    "intent": "comparison" if count > 1 else "fact",
                    "assumptions": [],
                    "facets": [
                        {"facet_id": f"f{i}", "question": f"fixture dimension {i}", "required": True}
                        for i in range(count)
                    ],
                    "source_policy": "primary_required",
                    "freshness": scenario.get("freshness", "current"),
                    "search_languages": ["en"],
                },
                "rejection": None,
            }
        if role == "planner":
            if p["batch_no"]:
                tasks = []
                for i, gap in enumerate(p["gaps"][:2]):
                    c = next(c for c in p["original_criteria"] if c["criterion_id"] == gap["criterion_id"])
                    tasks.append(
                        {
                            "question": gap["question"],
                            "facet_ids": [gap["facet_id"]],
                            "priority": 1,
                            "scope": gap["question"],
                            "exclusions": [],
                            "seed_queries": [f"fixture supplement {i}"],
                            "source_targets": ["official"],
                            "success_criteria": [c],
                            "parent_gap_ids": [gap["gap_id"]],
                        }
                    )
            else:
                tasks = [
                    {
                        "question": f"fixture dimension {i}",
                        "facet_ids": [f"f{i}"],
                        "priority": 1,
                        "scope": "fixture",
                        "exclusions": [],
                        "seed_queries": [f"fixture dimension {i}"],
                        "source_targets": ["official"],
                        "success_criteria": [
                            {
                                "criterion_id": f"c{i}",
                                "description": f"fixture criterion {i}",
                                "kind": scenario.get("criterion_kind", "authoritative_fact"),
                            }
                        ],
                        "parent_gap_ids": [],
                    }
                    for i in range(count)
                ]
            return {"outline": ["Findings"], "tasks": tasks, "validation_note": ""}
        if role == "reason":
            if p["criterion_results"] and all(c["status"] == "covered" for c in p["criterion_results"]):
                return {
                    "action": "finish",
                    "query": None,
                    "language": None,
                    "target_criterion_ids": [],
                    "rationale_summary": "Fixture material explains the task.",
                }
            return {
                "action": "search",
                "query": p["task"]["seed_queries"][0] + " attempt " + str(len(p["searched_queries"])),
                "language": "en",
                "target_criterion_ids": [c["criterion_id"] for c in p["task"]["success_criteria"]],
                "rationale_summary": "Resolve missing fixture evidence.",
            }
        if role == "extract":
            page = next(page for page in scenario["pages"] if page["url"] == p["url"])
            chunk = next((c for c in p["chunks"] if page["quote"] in c["text"]), None)
            if not chunk:
                return {"candidates": []}
            offset = chunk["text"].index(page["quote"])
            return {
                "candidates": [
                    {
                        "source_id": p["source_id"],
                        "chunk_id": chunk["chunk_id"],
                        "quote": page["quote"],
                        "start_char": offset,
                        "end_char": offset + len(page["quote"]),
                        "claim_text": page["claim"],
                        "relation": "supports",
                        "criterion_ids": [c["criterion_id"] for c in p["criteria"]],
                        "temporal_scope": None,
                        "directness": "direct",
                    }
                ]
            }
        if role == "evidence_verifier":
            from urllib.parse import urlsplit

            publisher = urlsplit(p["source"]["url"]).hostname
            quote = f"Published by {publisher}. This is our original specification."
            context = next(x for x in p["source_identity_context"]["contexts"] if quote in x["text"])
            return {
                "source_identity": {
                    "basis": "original_publication",
                    "publisher_name": publisher,
                    "context_id": context["context_id"],
                    "quote": quote,
                    "link_id": None,
                    "explanation": "Explicit publisher and original-publication statement.",
                },
                "verdicts": [
                    {
                        "candidate_index": i,
                        "relation": "supports",
                        "direct": True,
                        "primary_for_claim": True,
                        "criterion_ids": c["criterion_ids"],
                        "explanation": "Fixture quote entails this atomic claim.",
                    }
                    for i, c in enumerate(p["candidates"])
                ],
            }
        if role == "merge":
            if scenario.get("conflict") and len(p["claims"]) >= 2:
                return {
                    "pairs": [
                        {
                            "claim_a_id": p["claims"][0]["claim_id"],
                            "claim_b_id": p["claims"][1]["claim_id"],
                            "relation": "conflict",
                            "reason": "Mutually exclusive fixture values at the same time.",
                        }
                    ]
                }
            return {"pairs": []}
        if role == "writer":
            ps = []
            for c in p["claims"]:
                text = "推断：存在未解决的来源分歧。" if c["status"] == "contested" else c["text"]
                ps.append(
                    {
                        "text": text,
                        "claim_ids": [c["claim_id"]],
                        "evidence_ids": [e["evidence_id"] for e in c["evidence_links"]],
                    }
                )
            return {"title": "Fixture report", "sections": [{"heading": "Findings", "paragraphs": ps}]}
        if role == "quality":
            return {"issues": [], "strengths": ["Fixture has clear supported content."]}
        if role == "editor":
            value = json.loads(json.dumps(p["draft"]))
            for section in value["sections"]:
                if any(f["feedback_id"].startswith("H") for f in p["feedback"]):
                    section["heading"] = ""
                for block in section["paragraphs"]:
                    block.pop("paragraph_id", None)
            return {
                **value,
                "addressed_feedback_ids": [f["feedback_id"] for f in p["feedback"]],
                "remaining_issues": [],
            }
        if role == "report_verifier":
            return {
                "paragraphs": [
                    {
                        "paragraph_id": para["paragraph_id"],
                        "atoms": [
                            {
                                "text": hint["anchor"],
                                "anchor": hint["anchor"],
                                "kind": "inference" if "推断" in para["text"] else "fact",
                                "claim_ids": hint["claim_ids"],
                                "evidence_ids": hint["evidence_ids"],
                                "verdict": "entailment",
                                "answered_criterion_ids": [c["criterion_id"] for c in p["criteria"]],
                            }
                            for hint in para["citation_hints"]
                        ],
                        "complete": True,
                        "headings_supported": True,
                        "unsupported_text": [],
                        "explanation": "All actual prose is audited.",
                    }
                    for para in p["paragraphs"]
                ]
            }
        if role == "memory_candidates":
            return {"items": []}
        if role == "repair":
            return {"paragraphs": []}
        raise AssertionError(role)


class FixtureSearch:
    def __init__(self, ops, scenario, delay=0):
        self.ops = ops
        self.scenario = scenario
        self.calls = 0
        self.queries = []
        self.delay = delay
        self.active = 0
        self.max_active = 0

    async def search(self, query, language, task_id=None):
        async def call(timeout):
            self.calls += 1
            self.queries.append(query)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(self.delay)
            finally:
                self.active -= 1
            return {
                "hits": [
                    SearchHit(
                        url=p["url"],
                        title="Fixture",
                        snippet="UNVERIFIED_SNIPPET_DO_NOT_CITE",
                        rank=i + 1,
                        published_at=None,
                    ).model_dump()
                    for i, p in enumerate(self.scenario["pages"])
                ]
            }

        value = await self.ops.run(
            "search:" + stable_id("query", query, language), "search", {"query": query}, call, task_id=task_id
        )
        return [SearchHit.model_validate(v) for v in value["hits"]]


class FixtureFetch:
    def __init__(self, ops, scenario):
        self.ops = ops
        self.scenario = scenario
        self.calls = 0

    async def fetch(self, url, task_id=None):
        async def call(timeout):
            self.calls += 1
            page = next(p for p in self.scenario["pages"] if p["url"] == url)
            from urllib.parse import urlsplit

            text = (
                page["text"] + f"\nPublished by {urlsplit(url).hostname}. This is our original specification."
            )
            return {
                "requested_url": url,
                "canonical_url": url,
                "title": "Fixture source",
                "media_type": "text/plain",
                "text": text,
                "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "retrieved_at": utcnow().isoformat(),
                "published_at": page.get("published_at"),
                "publisher_key": url.split("/")[2],
                "source_type": "official",
                "metadata": page.get("metadata", {}),
            }

        raw = await self.ops.run(
            "fetch:" + stable_id("url", url), "fetch", {"url": url}, call, task_id=task_id
        )
        return FetchResult.model_validate_json(canonical(raw))


class FixtureMemory:
    def __init__(self, scenario=None):
        self.scenario = scenario or {}

    async def recall(self, query):
        from research_agent.adapters.letta import Memory
        from research_agent.contracts import MemoryItem

        parsed = [MemoryItem.model_validate_json(canonical(i)) for i in self.scenario.get("memory_items", [])]
        items = Memory.filter_items(self, parsed)
        return MemorySnapshot(items=items, recalled_at=utcnow(), remote_revision="fixture", degraded=False)

    def validate_candidates(self, *args):
        return []
