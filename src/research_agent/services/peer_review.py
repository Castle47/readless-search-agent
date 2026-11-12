"""Bounded peer consultation over frozen passages, with replayable addressed messages."""

from copy import deepcopy

from ..contracts import digest, stable_id
from ..errors import InvariantViolation, Stopped, ToolFailure
from ..peer_contracts import PeerQuestion, PeerQuestions, PeerReply
from .source_notes import SourceNotes


def enabled(services):
    return bool(
        services.config.get("peer_review_workflow_version")
        and services.config.get("adaptive_research_workflow_version")
    )


def feedback(discussion):
    """No peer passage aliases leak into the writer's independent citation namespace."""
    passages = discussion.get("passages", {})
    return [
        {
            "question": message["question"]["question"],
            "criterion_id": message["question"]["criterion_id"],
            "reported_status": message["reply"]["status"],
            "researcher_response": message["reply"]["answer"],
            "source_locations": [
                {k: passages[pid][k] for k in ("source_id", "start_char", "end_char")}
                for pid in message["reply"]["passage_ids"]
                if pid in passages
            ],
        }
        for message in discussion.get("messages", [])
    ]


def writing_feedback(state):
    items = feedback(state.get("peer_consultation", {})) + feedback(state.get("postwrite_consultation", {}))
    return (
        {
            "peer_consultation": {
                "status": "advisory_only_not_verified_evidence",
                "instruction": "Recheck these earlier interpretations against CURRENT offered original passages. "
                "Do not cite peer messages, assume consensus proves facts, or reuse peer IDs as citations.",
                "items": items,
            }
        }
        if items
        else {}
    )


def pending_messages(discussion):
    messages = discussion.get("messages", [])
    # A follow-up can clarify the same issue; retain the latest disposition in that chain.
    by_id = {m["question_id"]: m for m in messages}
    superseded = set()
    for message in messages:
        ancestor = (
            message["question"].get("followup_to") if message["reply"]["status"] == "clarified" else None
        )
        while ancestor and ancestor not in superseded:
            superseded.add(ancestor)
            ancestor = by_id.get(ancestor, {}).get("question", {}).get("followup_to")
    return [m for m in messages if m["question_id"] not in superseded and m["reply"]["status"] != "clarified"]


def task_questions(state, task):
    if not state.get("batch_no"):
        return []
    discussion = state.get("peer_consultation", {})
    cids = {c["criterion_id"] for c in task["success_criteria"]}
    passages = discussion.get("passages", {})
    result = []
    for message in pending_messages(discussion):
        question = message["question"]
        if question["criterion_id"] not in cids:
            continue
        ids = question["passage_ids"] + message["reply"]["passage_ids"]
        source_ids = {passages[pid]["source_id"] for pid in ids if pid in passages}
        if not source_ids:
            source_ids = {p["source_id"] for p in passages.values()}
        result.append(
            {
                "question_id": message["question_id"],
                "criterion_id": question["criterion_id"],
                "question": question["question"],
                "source_ids": sorted(source_ids),
            }
        )
    return result


def supplemental_owner(state, gap):
    discussion = state.get("peer_consultation", {})
    message = next(
        (m for m in pending_messages(discussion) if m["question"]["criterion_id"] == gap["criterion_id"]),
        None,
    )
    if not message:
        return {}
    return next(
        t for t in state["plan_history"][0]["tasks"] if t["task_id"] == message["question"]["target_task_id"]
    )


def supplemental_seeds(state, gap, default):
    owner = supplemental_owner(state, gap)
    if not owner:
        return default
    rewrite = state.get("query_rewrite", {})
    # Never turn private peer discussions into new public search terms.
    if rewrite.get("scope") in {"hybrid", "local"} and rewrite.get("search_terms"):
        return rewrite["search_terms"][:2]
    return owner["seed_queries"][:2]


def apply_peer_gaps(state, assessment, criteria):
    """Only add unresolved work; peer agreement never promotes coverage or accepted claims."""
    if state.get("batch_no", 0) != 0:
        return assessment
    result = {**assessment, "gaps": [dict(g) for g in assessment["gaps"]]}
    facets = {f["facet_id"]: f for f in state["understanding"]["facets"]}
    tasks = state["plan_history"][0]["tasks"]
    affected = set()
    for message in pending_messages(state.get("peer_consultation", {})):
        question = message["question"]
        cid = question["criterion_id"]
        criterion = criteria[cid]
        facet_ids = criterion.facet_ids or next(
            t["facet_ids"] for t in tasks if any(c["criterion_id"] == cid for c in t["success_criteria"])
        )
        for fid in facet_ids:
            required = criterion.required and facets[fid]["required"]
            if required:
                affected.add(fid)
            existing = next(
                (g for g in result["gaps"] if g["criterion_id"] == cid and g["facet_id"] == fid), None
            )
            question_text = criterion.description + "；待核对：" + question["question"]
            if existing:
                existing["question"] = question_text
                if question["kind"] == "conflict":
                    existing["type"] = "conflict"
            else:
                result["gaps"].append(
                    {
                        "gap_id": stable_id("peer-gap", state["run_id"], fid, cid),
                        "facet_id": fid,
                        "criterion_id": cid,
                        "type": "conflict" if question["kind"] == "conflict" else "missing",
                        "priority": 1 if required else 3,
                        "question": question_text,
                        "evidence_ids": [],
                    }
                )
    result["gaps"].sort(key=lambda g: (g["priority"], g["facet_id"], g["criterion_id"] or ""))
    if affected:
        required_count = sum(f["required"] for f in facets.values())
        missing = {g["facet_id"] for g in result["gaps"] if g["priority"] == 1}
        result["required_coverage"] = min(
            result["required_coverage"], (required_count - len(missing)) / max(1, required_count)
        )
        result["can_answer"] = False
        result["explanation"] += " 研究者交叉质询仍有待核对问题；讨论未作为事实验收。"
    return result


class PeerConsultation:
    def __init__(self, services):
        self.s = services
        self.options = services.config.get("peer_review", {})

    async def save(self, key, kind, value, task_id=None):
        return await self.s.db.commit_value(self.s.run_id, self.s.fence, key, kind, value, task_id)

    async def snapshot(self, state, key, *, all_tasks=False, preferred_sources=()):
        saved = await self.s.db.saved_value(self.s.run_id, key)
        if saved is not None:
            return saved
        tasks = state["plan_history"][0]["tasks"]
        if not all_tasks:
            tasks = tasks[: self.options["max_participants"]]
        notes = await SourceNotes(self.s).index()
        source_ids, researchers = set(), []
        for task in tasks:
            context = await self.s.db.saved_value(self.s.run_id, "reader-context:0:" + task["task_id"])
            read_ids = context.get("source_ids", []) if context else []
            source_ids.update(read_ids)
            researchers.append(
                {
                    "task_id": task["task_id"],
                    "question": task["question"],
                    "scope": task["scope"],
                    "exclusions": task.get("exclusions", []),
                    "criteria": task["success_criteria"],
                    "read_source_ids": read_ids,
                }
            )
        # Include task-relevant newer material without claiming the original researcher personally read it.
        cids = {c["criterion_id"] for t in tasks for c in t["success_criteria"]}
        source_ids.update(n["source_id"] for n in notes if set(n["criterion_ids"]) & cids)
        sources = {sid: await self.s.evidence.get_source(sid) for sid in sorted(source_ids)}
        candidates = {}
        for note in notes:
            sid = note["source_id"]
            if sid not in sources or note.get("material_role") == "irrelevant":
                continue
            source = sources[sid]
            if source["content_sha256"] != note["source_hash"]:
                raise InvariantViolation("Peer material differs from frozen source hash")
            for p in note["passages"]:
                if (
                    p["source_id"] != sid
                    or not 0 <= p["start_char"] < p["end_char"] <= len(source["content_text"])
                    or source["content_text"][p["start_char"] : p["end_char"]] != p["text"]
                ):
                    raise InvariantViolation("Peer passage differs from frozen source")
                candidates[(sid, p["start_char"], p["end_char"])] = {
                    "source_id": sid,
                    "start_char": p["start_char"],
                    "end_char": p["end_char"],
                    "text": p["text"],
                    "source_title": source["title"],
                    "source_sha256": source["content_sha256"],
                }
        # Round-robin by source, so one long document cannot monopolize the packet.
        source_order = sorted(sources, key=lambda sid: (sid not in preferred_sources, sid))
        queues = [
            [p for (source_id, _, _), p in sorted(candidates.items()) if source_id == sid]
            for sid in source_order
        ]
        passages = {}
        while any(queues) and len(passages) < 12:
            for queue in queues:
                if queue and len(passages) < 12:
                    passages[f"E{len(passages)}"] = queue.pop(0)
        snapshot = {
            "researchers": researchers,
            "passages": passages,
            "omitted_passages": len(candidates) - len(passages),
        }
        return await self.save(key, "peer_snapshot", snapshot)

    def pack(self, role, payload, schema):
        payload = deepcopy(payload)
        payload.setdefault("omitted_passages", 0)
        while True:
            try:
                self.s.llm.build(role, payload, schema, "bounded validation feedback")
                return payload
            except Stopped as error:
                if error.code != "input_budget_exceeded" or not payload["passages"]:
                    raise
                payload["passages"].pop()
                payload["omitted_passages"] += 1

    async def call(self, key, role, payload, schema, validator, *, actor, partition):
        frozen = await self.s.db.saved_value(self.s.run_id, key + ":input")
        if frozen is None:
            frozen = self.pack(role, payload, schema)
            await self.save(key + ":input", "peer_input", frozen, actor)
        saved = await self.s.db.saved_value(self.s.run_id, key + ":result")
        if saved is not None:
            value = schema.model_validate(saved)
            validator(value, frozen)
            return value
        recorded_model = await self.s.db.saved_value(self.s.run_id, key + ":model")
        if (
            recorded_model is None
            and await self.s.ops.guard(partition) < self.options["min_seconds_remaining"]
        ):
            raise Stopped("deadline")
        value = await self.s.llm.generate(
            key + ":model",
            role,
            frozen,
            schema,
            task_id=actor,
            partition=partition,
            validator=lambda value: validator(value, frozen),
        )
        validator(value, frozen)
        await self.save(key + ":result", role, value.model_dump(mode="json"), actor)
        return value

    @staticmethod
    def validate_questions(value, payload):
        tasks = {t["task_id"]: t for t in payload["researchers"]}
        cids = {c["criterion_id"] for t in tasks.values() for c in t["criteria"]}
        offered = {p["passage_id"] for p in payload["passages"]}
        prior = {m["question_id"]: m for m in payload["messages"]}
        for question in value.questions:
            if question.target_task_id not in tasks or question.target_task_id == payload["speaker_task_id"]:
                raise ValueError("Address one other offered researcher")
            if question.criterion_id not in cids or not set(question.passage_ids) <= offered:
                raise ValueError("Use offered criteria and original passages only")
            if question.kind != "missing_evidence" and not question.passage_ids:
                raise ValueError("A factual clarification/conflict must identify original passages")
            if question.kind == "conflict" and len(set(question.passage_ids)) < 2:
                raise ValueError("A conflict requires at least two offered passages")
            if question.followup_to:
                old = prior.get(question.followup_to)
                if (
                    not old
                    or old["question"]["criterion_id"] != question.criterion_id
                    or payload["speaker_task_id"]
                    not in {old["sender_task_id"], old["question"]["target_task_id"]}
                ):
                    raise ValueError("Follow up on your own offered exchange and the same criterion")

    @staticmethod
    def validate_reply(value, payload):
        offered = {p["passage_id"] for p in payload["passages"]}
        if value.question_id != payload["question_id"] or not set(value.passage_ids) <= offered:
            raise ValueError("Reply to the offered question using offered original passages")
        if value.status == "clarified" and not value.passage_ids:
            raise ValueError("Clarification needs original passages, not peer agreement")

    def payload(self, state, snapshot, messages):
        return {
            "question": self.s.request.query,
            "researchers": snapshot["researchers"],
            "passages": [{"passage_id": pid, **p} for pid, p in snapshot["passages"].items()],
            "omitted_passages": snapshot["omitted_passages"],
            "messages": messages,
            "scope": state.get("query_rewrite", {}).get("scope", "unknown"),
            "discussion_is_not_verified_evidence": True,
        }

    @staticmethod
    def message(snapshot, question_id, round_no, sender, question, reply):
        ids = list(dict.fromkeys(question.passage_ids + reply.passage_ids))
        return {
            "question_id": question_id,
            "round": round_no,
            "sender_task_id": sender,
            "question": question.model_dump(),
            "reply": reply.model_dump(),
            "evidence_locations": {
                pid: {
                    k: snapshot["passages"][pid][k]
                    for k in ("source_id", "start_char", "end_char", "source_sha256")
                }
                for pid in ids
                if pid in snapshot["passages"]
            },
        }

    async def respond(self, key, payload, question_id, question, partition):
        # Retain cited passages first if input packing needs to drop whole passages.
        payload = {
            **payload,
            "question_id": question_id,
            "question_to_answer": question.model_dump(),
            "speaker_task_id": question.target_task_id,
        }
        payload["passages"] = sorted(
            payload["passages"], key=lambda p: p["passage_id"] not in question.passage_ids
        )
        try:
            return await self.call(
                key,
                "peer_responder",
                payload,
                PeerReply,
                self.validate_reply,
                actor=question.target_task_id,
                partition=partition,
            )
        except (Stopped, ToolFailure) as error:
            if error.code == "cancelled" or getattr(error, "fatal", False):
                raise
            await self.s.warn("peer_reply.unavailable", reason=error.code)
            value = PeerReply(
                question_id=question_id,
                status="unresolved",
                answer="本次未完成研究者回应：" + error.code,
                passage_ids=[],
            )
            await self.save(
                key + ":result", "peer_reply_failure", value.model_dump(), question.target_task_id
            )
            return value

    async def before_write(self, state):
        if not enabled(self.s) or state.get("batch_no", 0) != 0:
            return {}
        key = "peer-consultation:prewrite:v1"
        prior = await self.s.db.saved_value(self.s.run_id, key)
        if prior is not None:
            return {"peer_consultation": prior}
        snapshot = await self.snapshot(state, key + ":snapshot")
        result = {
            "stage": "prewrite",
            "status": "completed",
            "messages": [],
            "passages": snapshot["passages"],
            "researchers": snapshot["researchers"],
            "rounds": 0,
            "omitted_passages": snapshot["omitted_passages"],
        }
        researchers = snapshot["researchers"]
        if len(researchers) < 2 or not snapshot["passages"]:
            result.update(status="skipped", reason="insufficient_researchers_or_material")
        else:
            await self.s.db.phase(self.s.run_id, self.s.fence, "peer_review")
            try:
                for round_no in range(1, self.options["max_rounds"] + 1):
                    result["rounds"] = round_no
                    asked = 0
                    order = researchers[round_no - 1 :] + researchers[: round_no - 1]
                    for researcher in order:
                        actor = researcher["task_id"]
                        payload = {
                            **self.payload(state, snapshot, result["messages"]),
                            "speaker_task_id": actor,
                            "round": round_no,
                        }
                        questions = await self.call(
                            f"{key}:round:{round_no}:ask:{actor}",
                            "peer_questioner",
                            payload,
                            PeerQuestions,
                            self.validate_questions,
                            actor=actor,
                            partition="research",
                        )
                        for question in questions.questions:
                            if any(
                                m["question"]["question"].strip() == question.question.strip()
                                and m["question"]["criterion_id"] == question.criterion_id
                                for m in result["messages"]
                            ):
                                continue
                            qid = stable_id(
                                "peer-question", self.s.run_id, round_no, actor, question.model_dump()
                            )
                            reply = await self.respond(
                                key + ":reply:" + qid, payload, qid, question, "research"
                            )
                            message = self.message(snapshot, qid, round_no, actor, question, reply)
                            await self.save(key + ":message:" + qid, "peer_message", message, actor)
                            result["messages"].append(message)
                            asked += 1
                        if asked >= self.options["max_questions_per_round"]:
                            break
                    if not asked:
                        break
            except (Stopped, ToolFailure) as error:
                if error.code == "cancelled" or getattr(error, "fatal", False):
                    raise
                result.update(status="partial", reason=error.code)
                await self.s.warn("peer_consultation.unavailable", reason=error.code)
        result["pending_count"] = len(pending_messages(result))
        result = await self.save(key, "peer_consultation", result)
        return {"peer_consultation": result}

    async def after_review(self, state):
        """The answer verifier directly asks the responsible researcher; never clears its findings."""
        if (
            not enabled(self.s)
            or state.get("fallback_report")
            or not self.options["max_post_write_questions"]
        ):
            return {}
        review = state.get("whole_answer_review", {})
        if review.get("execution_error"):
            return {}
        findings = [i for i in review.get("issues", []) if i["kind"] == "fact"]
        findings += [
            {"paragraph_ids": [f["paragraph_id"]], "problem": f["statement"]}
            for f in review.get("important_facts", [])
            if not f["supported"]
        ]
        if not findings:
            return {}
        key = "peer-consultation:postwrite:v1:" + digest([state["source_draft"], review])
        prior = await self.s.db.saved_value(self.s.run_id, key)
        if prior is not None:
            return {"postwrite_consultation": prior}
        paragraphs = {
            p["paragraph_id"]: p
            for section in state["source_draft"]["sections"]
            for p in section["paragraphs"]
        }
        material = state["source_materials"]
        preferred = {
            material["passages"][pid]["source_id"]
            for finding in findings
            for paragraph_id in finding["paragraph_ids"]
            for pid in paragraphs.get(paragraph_id, {}).get("passage_ids", [])
            if pid in material["passages"]
        }
        snapshot = await self.snapshot(state, key + ":snapshot", all_tasks=True, preferred_sources=preferred)
        researchers = snapshot["researchers"]
        result = {
            "stage": "postwrite",
            "status": "completed",
            "messages": [],
            "passages": snapshot["passages"],
        }
        if not researchers or not snapshot["passages"]:
            result.update(status="skipped", reason="no_material")
        else:
            criteria_order = list(
                dict.fromkeys(
                    c["criterion_id"]
                    for t in state["plan_history"][0]["tasks"]
                    for c in t["success_criteria"]
                )
            )
            seen = set()
            for finding in findings:
                identity = digest([finding["paragraph_ids"], finding["problem"]])
                if identity in seen:
                    continue
                seen.add(identity)
                referenced = [
                    material["passages"][pid]
                    for paragraph_id in finding["paragraph_ids"]
                    for pid in paragraphs.get(paragraph_id, {}).get("passage_ids", [])
                    if pid in material["passages"]
                ]
                source_ids = {p["source_id"] for p in referenced}
                relevant = {
                    criteria_order[int(c["criterion_id"][1:])]
                    for c in review.get("coverage", [])
                    if c["criterion_id"].startswith("Q")
                    and c["criterion_id"][1:].isdigit()
                    and int(c["criterion_id"][1:]) < len(criteria_order)
                    and set(c["paragraph_ids"]) & set(finding["paragraph_ids"])
                }
                target = max(
                    researchers,
                    key=lambda t: (
                        len(source_ids & set(t["read_source_ids"])),
                        len(relevant & {c["criterion_id"] for c in t["criteria"]}),
                    ),
                )
                evidence = [pid for pid, p in snapshot["passages"].items() if p["source_id"] in source_ids][
                    :6
                ]
                cid = next(
                    (cid for cid in criteria_order if cid in relevant), target["criteria"][0]["criterion_id"]
                )
                question = PeerQuestion(
                    target_task_id=target["task_id"],
                    criterion_id=cid,
                    kind="missing_evidence",
                    question=finding["problem"][:700],
                    passage_ids=evidence,
                )
                qid = stable_id("peer-verifier-question", self.s.run_id, identity)
                payload = self.payload(state, snapshot, [])
                payload["researchers"] = [target]
                payload["answer_blocks"] = [
                    {"paragraph_id": pid, "text": paragraphs[pid]["text"]}
                    for pid in finding["paragraph_ids"]
                    if pid in paragraphs
                ]
                reply = await self.respond(key + ":reply:" + qid, payload, qid, question, "finalization")
                message = self.message(snapshot, qid, 1, "answer_verifier", question, reply)
                await self.save(key + ":message:" + qid, "peer_message", message, target["task_id"])
                result["messages"].append(message)
                if len(result["messages"]) >= self.options["max_post_write_questions"]:
                    break
        result["pending_count"] = len(pending_messages(result))
        result = await self.save(key, "peer_consultation", result)
        return {"postwrite_consultation": result}
