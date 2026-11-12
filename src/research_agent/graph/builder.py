import asyncio
import re

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from ..contracts import (
    Criterion,
    FacetAssignment,
    Gap,
    GapAssessment,
    MemoryProposal,
    MemorySnapshot,
    PlanProposal,
    ResearchPlan,
    ResearchTask,
    UnderstandingDecision,
    stable_id,
    utcnow,
    validate_ids,
)
from ..errors import InvariantViolation, Stopped, ToolFailure
from ..services.admission import fallback_understanding, missing_referent, validate_intro_scope
from ..services.conversation import context_urls, understanding_payload
from ..services.reports import render_report
from .researcher import Researcher
from .state import ResearchState

RESEARCH_SEMAPHORE = asyncio.Semaphore(3)


class ResearchGraph:
    def __init__(self, services):
        self.s = services

    async def summarize(self, state):
        if not self.s.config.get("context_workflow_version"):
            return {}
        from ..services.query_rewrite import summarize

        return await summarize(self.s)

    async def rewrite(self, state):
        if not self.s.config.get("context_workflow_version"):
            return {}
        from ..services.query_rewrite import rewrite

        return await rewrite(self.s, state)

    async def retrieve_local(self, state):
        if not self.s.config.get("context_workflow_version"):
            return {}
        from ..services.private_rag import retrieve

        result = await retrieve(self.s, state)
        from ..services.rag_ranking import is_strong_source

        strong_sources = [source for source in result.get("local_sources", []) if is_strong_source(source)]
        if strong_sources and self.s.config.get("early_draft_version"):
            import asyncio

            from ..services.early_answer import write_preview

            if self.s.preview_task is None:
                self.s.preview_task = asyncio.create_task(
                    write_preview(self.s, strong_sources, state["understanding"])
                )
        return result

    async def understand(self, state):
        await self.s.db.phase(self.s.run_id, self.s.fence, "understand")
        if missing_referent(self.s.request):
            return {"rejection": "ambiguous_query"}
        try:
            payload, omitted = understanding_payload(
                self.s.request, self.s.llm, state.get("conversation_summary")
            )
            await self.s.db.commit_value(
                self.s.run_id,
                self.s.fence,
                "understand-input:v1",
                "input_pack",
                {"payload": payload, "omitted_assistant_messages": omitted},
            )
            value = await self.s.llm.generate(
                "understand:v1",
                "understand",
                payload,
                UnderstandingDecision,
                validator=lambda v: (
                    validate_intro_scope(
                        self.s.request, [f.question for f in v.understanding.facets if f.required]
                    )
                    if v.understanding and self.s.config.get("reader_budget_workflow_version")
                    else None
                ),
            )
            if value.rejection == "ambiguous_query":
                # A model's knowledge gap is a discovery task, not missing user input.
                await self.s.warn("understand_discovery_fallback")
                understanding = fallback_understanding(self.s.request, discover=True)
            elif value.rejection:
                return {"rejection": value.rejection}
            else:
                understanding = value.understanding
        except (ToolFailure, Stopped) as error:
            if isinstance(error, ToolFailure) and error.fatal:
                raise
            if error.code == "cancelled":
                raise
            await self.s.warn("understand_degraded")
            understanding = fallback_understanding(self.s.request)
        self.s.understanding = understanding.model_dump(mode="json")
        return {"understanding": self.s.understanding, "rejection": None}

    async def recall(self, state):
        self.s.understanding = state["understanding"]
        await self.s.db.phase(self.s.run_id, self.s.fence, "recall")
        key = "memory:recall:v1"
        cached = await self.s.db.saved_value(self.s.run_id, key)
        if cached is not None:
            return {"memory_snapshot": cached}
        mode = self.s.request.memory_mode
        snapshot = MemorySnapshot(items=[], recalled_at=utcnow(), remote_revision=None, degraded=False)
        if mode != "off":
            try:
                snapshot = await asyncio.wait_for(
                    self.s.memory.recall(state["understanding"]["standalone_query"]), timeout=10
                )
            except Exception:  # noqa: BLE001 - optional third-party memory failures must not abort research.
                snapshot = snapshot.model_copy(update={"degraded": True})
                await self.s.warn("memory.degraded")
        value = snapshot.model_dump(mode="json")
        await self.s.db.commit_value(self.s.run_id, self.s.fence, key, "memory_snapshot", value)
        return {"memory_snapshot": value}

    async def discover(self, state):
        rewrite = state.get("query_rewrite", {})
        self.s.search.private_scope = rewrite.get("scope") == "local"
        self.s.fetch.private_scope = self.s.search.private_scope
        self.s.search.public_queries = (
            set(rewrite.get("search_terms", [])) if state.get("local_sources") else None
        )
        if self.s.search.private_scope:
            result = {"sources": state.get("local_sources", []), "searched": False, "private_only": True}
            await self.s.db.commit_value(
                self.s.run_id, self.s.fence, "private-discovery:v1", "discovery", result
            )
            return {"discovery": result}
        if self.s.config.get("adaptive_research_workflow_version"):
            from ..services.adaptive_research import survey

            state = {
                **state,
                "understanding": {
                    **state["understanding"],
                    "local_topic_context": [
                        {"title": source["title"], "excerpt": source.get("identity_context", "")[:2000]}
                        for source in state.get("local_sources", [])[:4]
                    ],
                },
            }
            result = await survey(self.s, state)
            result["discovery"]["sources"] += state.get("local_sources", [])
            return result
        understanding = state["understanding"]
        self.s.understanding = understanding
        if not understanding.get("needs_discovery"):
            return {"discovery": {"sources": [], "searched": False}}
        await self.s.db.phase(self.s.run_id, self.s.fence, "discover")
        key = "discovery:v2"
        cached = await self.s.db.saved_value(self.s.run_id, key)
        if cached is not None:
            return {"discovery": cached}
        value = {"sources": [], "searched": True}
        try:
            query = (understanding.get("search_terms") or [understanding["standalone_query"]])[0]
            language = understanding["search_languages"][0]
            if (
                understanding["source_policy"] == "primary_required"
                and "en" in understanding["search_languages"]
            ):
                original_language_query = next(
                    (
                        q
                        for q in understanding.get("search_terms", [])
                        if re.search(r"[A-Za-z]", q) and not re.search(r"[\u4e00-\u9fff]", q)
                    ),
                    None,
                )
                if original_language_query:
                    query, language = original_language_query, "en"
                    if not re.search(r"\bofficial\b", query, re.IGNORECASE):
                        query += " official"
            hits = await self.s.search.search(query, language)
            if self.s.config.get("retrieval_workflow_version") and self.s.config.get(
                "source_first_workflow_version"
            ):
                from ..contracts import SearchHit
                from ..services.retrieval import select_sources

                selected = await select_sources(
                    self.s,
                    [h.model_dump(mode="json") for h in hits],
                    understanding["standalone_query"],
                    query=query,
                )
                hits = [
                    SearchHit.model_validate({k: h[k] for k in SearchHit.model_fields if k in h})
                    for h in selected
                ]
            for hit in hits[: self.s.config["research"]["pages_per_round"]]:
                try:
                    page = await self.s.fetch.fetch(hit.url)
                    sid = await self.s.evidence.source(page)
                    value["sources"].append(
                        {
                            "source_id": sid,
                            "url": page.canonical_url,
                            "title": page.title,
                            "identity_context": page.text
                            if len(page.text) <= 1600
                            else page.text[:798] + "\n…\n" + page.text[-798:],
                            "reference_links": page.metadata.get("reference_links", []),
                        }
                    )
                except (ToolFailure, Stopped) as error:
                    if getattr(error, "fatal", False) or error.code == "cancelled":
                        raise
                    await self.s.warn("discovery.page_unavailable", reason=error.code)
        except (ToolFailure, Stopped) as error:
            if getattr(error, "fatal", False) or error.code == "cancelled":
                raise
            await self.s.warn("discovery.unavailable", reason=error.code)
        await self.s.db.commit_value(self.s.run_id, self.s.fence, key, "discovery", value)
        return {"discovery": value}

    def all_criteria(self, state):
        return {
            c["criterion_id"]: Criterion.model_validate(c)
            for task in state["plan_history"][0]["tasks"]
            for c in task["success_criteria"]
        }

    def fallback_tasks(self, state, batch):
        if not batch:
            facets = state["understanding"]["facets"]
            tasks = []
            for offset in range(0, len(facets), 3):
                group = facets[offset : offset + 3]
                tasks.append(
                    ResearchTask(
                        task_id=stable_id("task", self.s.run_id, 0, offset // 3),
                        question=state["understanding"]["standalone_query"],
                        facet_ids=[f["facet_id"] for f in group],
                        priority=1,
                        scope=state["understanding"]["standalone_query"],
                        exclusions=[],
                        seed_queries=[state["understanding"]["standalone_query"]],
                        source_targets=["official"],
                        success_criteria=[
                            Criterion(
                                criterion_id=stable_id("criterion", self.s.run_id, 0, offset + i),
                                description=f["question"],
                                kind="authoritative_fact",
                                facet_ids=[f["facet_id"]],
                            )
                            for i, f in enumerate(group)
                        ],
                        max_rounds=3,
                        parent_gap_ids=[],
                    )
                )
            return tasks
        criteria = self.all_criteria(state)
        gaps = state["gap_assessment"]["gaps"][:2]
        from ..services.peer_review import supplemental_owner, supplemental_seeds

        return [
            ResearchTask(
                task_id=stable_id("task", self.s.run_id, 1, i),
                question=gap["question"],
                facet_ids=[gap["facet_id"]],
                priority=gap["priority"],
                scope=gap["question"],
                exclusions=supplemental_owner(state, gap).get("exclusions", []),
                seed_queries=supplemental_seeds(state, gap, [gap["question"]]),
                source_urls=supplemental_owner(state, gap).get("source_urls", []),
                source_targets=["official", "paper", "independent"],
                success_criteria=[criteria[gap["criterion_id"]]],
                max_rounds=2,
                parent_gap_ids=[gap["gap_id"]],
            )
            for i, gap in enumerate(gaps)
            if gap["criterion_id"] in criteria
        ]

    async def plan(self, state):
        from ..adapters.fetch import canonical_url
        from ..contracts import TaskProposal
        from ..source_contracts import SourcePlan

        source_mode = (
            self.s.config.get("source_first_workflow_version", 0) if hasattr(self.s, "config") else False
        )
        schema = SourcePlan if source_mode else PlanProposal
        self.s.understanding = state["understanding"]
        batch = 1 if state.get("plan_history") else 0
        await self.s.db.phase(self.s.run_id, self.s.fence, "planning" if not batch else "supplemental")
        if batch and self.s.config.get("adaptive_research_workflow_version"):
            # Identified gaps need focused work, not another open-ended plan.
            tasks = self.fallback_tasks(state, batch)
            plan = ResearchPlan(
                plan_id=stable_id("plan", self.s.run_id, batch),
                run_id=self.s.run_id,
                batch_no=batch,
                outline=state["plan_history"][0]["outline"],
                tasks=tasks,
                facet_assignments=[
                    FacetAssignment(facet_id=f, task_ids=[t.task_id for t in tasks if f in t.facet_ids])
                    for f in sorted({f for t in tasks for f in t.facet_ids})
                ],
            )
            await self.s.db.save_plan(self.s.run_id, self.s.fence, plan)
            return {
                "plan_history": state["plan_history"] + [plan.model_dump(mode="json")],
                "batch_no": batch,
                "expected_task_ids": [t.task_id for t in tasks],
            }
        facets = {f["facet_id"] for f in state["understanding"]["facets"]}
        required_gaps = state.get("gap_assessment", {}).get("gaps", [])
        supplied_urls = set()
        from ..services.request_urls import request_urls

        for url in request_urls(self.s.request.query):
            try:
                supplied_urls.add(canonical_url(url.rstrip(".,，。;；:：!?！？'\"）)]}>")))
            except (ToolFailure, ValueError):
                continue
        payload = {
            "user_question": self.s.request.query,
            "provided_source_urls": sorted(supplied_urls),
            "historical_user_source_urls": sorted(context_urls(self.s.request.context)),
            "understanding": state["understanding"],
            "discovery": state.get("discovery", {}),
            "memory": state["memory_snapshot"],
            "batch_no": batch,
            "gaps": required_gaps[:2],
            "original_criteria": [c.model_dump() for c in self.all_criteria(state).values()] if batch else [],
            "limits": self.s.request.limits.model_dump(exclude_none=True),
        }

        def check_plan_input():
            # Packing must leave room for the existing incomplete-output retry.
            # This uses its actual message, not a new numeric context threshold.
            return self.s.llm.build(
                "planner", payload, schema, "model output incomplete" if source_mode else None
            )

        criterion_aliases = {f"Q{i}": g["criterion_id"] for i, g in enumerate(required_gaps[:2])}
        gap_aliases = {f"G{i}": g["gap_id"] for i, g in enumerate(required_gaps[:2])}
        known_urls = set(payload["historical_user_source_urls"])
        if batch:
            payload["discovery"] = {}
            original = self.all_criteria(state)
            payload["original_criteria"] = [
                {**original[cid].model_dump(), "criterion_id": alias}
                for alias, cid in criterion_aliases.items()
            ]
            payload["gaps"] = [
                {**g, "criterion_id": f"Q{i}", "gap_id": f"G{i}"} for i, g in enumerate(required_gaps[:2])
            ]
            bundle = await self.s.evidence.bundle()
            evidence = {e["id"]: e for e in bundle["evidence"]}
            primary = {
                evidence[r["evidence_id"]]["source_id"]
                for r in bundle["relations"]
                if r["verification"].get("primary_for_claim")
            }
            verified_primary = set(primary)
            if self.s.config.get("source_first_workflow_version"):
                from ..services.source_notes import SourceNotes

                primary |= {
                    n["source_id"]
                    for n in await SourceNotes(self.s).index()
                    if n["identity"] and n.get("firsthand_criterion_ids")
                }
            payload["known_sources"] = []
            for source in sorted(bundle["sources"], key=lambda s: s["id"] not in primary):
                known_urls.add(canonical_url(source["canonical_url"]))
                offered = {
                    "url": source["canonical_url"],
                    "title": source["title"][:200],
                    "contains_verified_primary_evidence": source["id"] in verified_primary,
                    **(
                        {"contains_firsthand_research_material": source["id"] in primary}
                        if source_mode
                        else {}
                    ),
                }
                payload["known_sources"].append(offered)
                try:
                    check_plan_input()
                except Stopped as error:
                    if error.code != "input_budget_exceeded":
                        raise
                    payload["known_sources"].pop()
        else:
            # Keep the observed links in the checkpoint; offer complete link
            # records to the planner only while its fixed input cap permits.
            discovery = {"sources": [], "searched": state.get("discovery", {}).get("searched", False)}
            payload["discovery"] = discovery
            from ..adapters.fetch import publisher
            from .source_researcher import research_reference

            admitted = []
            for source in state.get("discovery", {}).get("sources", []):
                offered = {**source, "identity_context": "", "reference_links": []}
                discovery["sources"].append(offered)
                try:
                    check_plan_input()
                except Stopped as error:
                    if error.code != "input_budget_exceeded":
                        raise
                    discovery["sources"].pop()
                    continue
                links = [
                    link
                    for link in source.get("reference_links", [])
                    if research_reference(source["url"], link, {publisher(source["url"])})
                    or publisher(link["url"]) != publisher(source["url"])
                ]
                links.sort(
                    key=lambda link: (
                        not (
                            link.get("official_link") and publisher(link["url"]) != publisher(source["url"])
                        ),
                        not link.get("official_link"),
                        not link.get("documentation"),
                    )
                )
                admitted.append((source, offered, links))

            def offer_link(offered, link):
                offered["reference_links"].append(link)
                try:
                    check_plan_input()
                except Stopped as error:
                    if error.code != "input_budget_exceeded":
                        raise
                    offered["reference_links"].pop()

            # Give every discovery source a chance to expose its publisher link
            # before one repository's prose/navigation consumes the whole input.
            for source, offered, links in admitted:
                if links:
                    offer_link(offered, links.pop(0))
            for source, offered, links in admitted:
                offered["identity_context"] = source.get("identity_context", "")
                try:
                    check_plan_input()
                except Stopped as error:
                    if error.code != "input_budget_exceeded":
                        raise
                    offered["identity_context"] = ""
            while any(links for _, _, links in admitted):
                for source, offered, links in admitted:
                    if links:
                        offer_link(offered, links.pop(0))

        def unpack(value):
            if not source_mode:
                return value
            tasks = []
            originals = {c["criterion_id"]: c for c in payload["original_criteria"]}
            for ti, t in enumerate(value.tasks):
                if batch:
                    validate_ids(t.existing_criterion_ids, originals)
                    if t.criteria or not t.existing_criterion_ids:
                        raise ValueError("Supplemental tasks reuse existing criteria only")
                    criteria = [Criterion.model_validate(originals[cid]) for cid in t.existing_criterion_ids]
                else:
                    if t.existing_criterion_ids or t.gap_ids or not t.criteria:
                        raise ValueError("Initial tasks define substantive criteria")
                    criteria = [
                        Criterion(
                            criterion_id=f"C{ti}_{i}",
                            description=c.question,
                            required=c.required,
                            kind=c.kind,
                            facet_ids=c.facet_ids,
                        )
                        for i, c in enumerate(t.criteria)
                    ]
                tasks.append(
                    TaskProposal(
                        question=t.question,
                        facet_ids=t.facet_ids,
                        priority=1,
                        scope=state["understanding"]["standalone_query"],
                        exclusions=[],
                        seed_queries=t.queries,
                        source_targets=t.source_targets,
                        source_urls=t.source_urls,
                        success_criteria=criteria,
                        parent_gap_ids=t.gap_ids,
                    )
                )
            return PlanProposal(
                outline=[f["question"] for f in state["understanding"]["facets"]],
                tasks=tasks,
                validation_note="Scope and constraints inherited from the frozen reader brief.",
            )

        def expand(proposal):
            if not batch:
                return proposal
            tasks = []
            for t in proposal.tasks:
                validate_ids([c.criterion_id for c in t.success_criteria], criterion_aliases)
                validate_ids(t.parent_gap_ids, gap_aliases)
                tasks.append(
                    t.model_copy(
                        update={
                            "success_criteria": [
                                self.all_criteria(state)[cid]
                                for cid in dict.fromkeys(
                                    criterion_aliases[c.criterion_id] for c in t.success_criteria
                                )
                            ],
                            "parent_gap_ids": [gap_aliases[g] for g in t.parent_gap_ids],
                        }
                    )
                )
            return proposal.model_copy(update={"tasks": tasks})

        def validate(proposal):
            assigned = set()
            criterion_assigned = set()
            criteria = set()
            if not batch and self.s.config.get("reader_budget_workflow_version"):
                validate_intro_scope(
                    self.s.request,
                    [c.description for t in proposal.tasks for c in t.success_criteria if c.required],
                )
            observed_urls = {
                canonical_url(link["url"])
                for source in state.get("discovery", {}).get("sources", [])
                for link in source.get("reference_links", [])
            }
            observed_urls.update(
                canonical_url(s["url"]) for s in state.get("discovery", {}).get("sources", [])
            )
            observed_urls.update(supplied_urls)
            observed_urls.update(known_urls)
            if batch and len(proposal.tasks) > 2:
                raise ValueError("max two supplemental tasks")
            for task in proposal.tasks:
                validate_ids([canonical_url(url) for url in task.source_urls], observed_urls)
                if (
                    state["understanding"]["intent"] == "fact"
                    and not batch
                    and len(task.success_criteria) != 1
                ):
                    raise ValueError(
                        "A single factual question needs one substantive criterion; do not duplicate it as an answer-writing or corroboration requirement"
                    )
                validate_ids(task.facet_ids, facets)
                assigned.update(task.facet_ids)
                for criterion in task.success_criteria:
                    validate_ids(criterion.facet_ids, task.facet_ids)
                    if criterion.required:
                        criterion_assigned.update(criterion.facet_ids or task.facet_ids)
                    requested = self.s.request.query + " " + self.s.request.context
                    required_facets = {
                        f["facet_id"] for f in state["understanding"]["facets"] if f["required"]
                    }
                    if (
                        criterion.required
                        and required_facets.intersection(criterion.facet_ids or task.facet_ids)
                        and re.search(
                            r"独立.{0,12}(?:性能|成本|速度|准确|评测)|independent.{0,20}(?:performance|cost|benchmark|evaluation)",
                            criterion.description,
                            re.IGNORECASE,
                        )
                        and not re.search(
                            r"比较|对比|评测|实测|独立|性能|速度|成本|价格|benchmark|compar|performance|latency|cost|evaluat",
                            requested,
                            re.IGNORECASE,
                        )
                    ):
                        raise ValueError(
                            "Independent performance evaluation was not requested; do not make it a required overview criterion. Keep documented uses and boundaries, with attributed vendor claims where relevant."
                        )
                    if re.search(
                        r"^(?:记录|保存|收集).{0,24}(?:标题|链接|URL|页面|上下文)|^(?:record|save|collect).{0,35}(?:title|url|metadata)",
                        criterion.description,
                        re.IGNORECASE,
                    ):
                        raise ValueError(
                            "Source metadata is stored by the service; criteria must answer the requested substantive facets, not recording steps"
                        )
                    if not batch and criterion.criterion_id in criteria:
                        raise ValueError("duplicate criterion index")
                    criteria.add(criterion.criterion_id)
                if batch:
                    validate_ids([c.criterion_id for c in task.success_criteria], self.all_criteria(state))
                    validate_ids(task.parent_gap_ids, [g["gap_id"] for g in required_gaps[:2]])
                    if not task.parent_gap_ids:
                        raise ValueError("supplemental task must target gap")
                elif task.parent_gap_ids:
                    raise ValueError("initial task cannot reference gaps")
            required = {f["facet_id"] for f in state["understanding"]["facets"] if f["required"]}
            if not batch and (assigned != facets or not required <= criterion_assigned):
                raise ValueError("missing facet assignment")

        try:
            proposal = await self.s.llm.generate(
                f"plan:batch={batch}:v1",
                "planner",
                payload,
                schema,
                validator=lambda p: validate(expand(unpack(p))),
            )
            proposal = expand(unpack(proposal))
            tasks = []
            for index, t in enumerate(proposal.tasks):
                criteria = (
                    t.success_criteria
                    if batch
                    else [
                        c.model_copy(
                            update={
                                "criterion_id": stable_id("criterion", self.s.run_id, index, i),
                                "facet_ids": c.facet_ids or t.facet_ids,
                            }
                        )
                        for i, c in enumerate(t.success_criteria)
                    ]
                )
                if batch:
                    criteria = [self.all_criteria(state)[c.criterion_id] for c in criteria]
                tasks.append(
                    ResearchTask(
                        **t.model_dump(exclude={"success_criteria", "source_urls"}),
                        source_urls=[canonical_url(url) for url in t.source_urls],
                        success_criteria=criteria,
                        task_id=stable_id("task", self.s.run_id, batch, index),
                        max_rounds=2 if batch else 3,
                    )
                )
            outline = proposal.outline
        except (ToolFailure, Stopped) as error:
            if isinstance(error, ToolFailure) and error.fatal:
                raise
            if error.code == "cancelled":
                raise
            await self.s.warn("planner_degraded", batch_no=batch, reason=error.code)
            tasks = self.fallback_tasks(state, batch)
            outline = [f["question"] for f in state["understanding"]["facets"]]
        plan = ResearchPlan(
            plan_id=stable_id("plan", self.s.run_id, batch),
            run_id=self.s.run_id,
            batch_no=batch,
            outline=outline,
            tasks=tasks,
            facet_assignments=[
                FacetAssignment(facet_id=f, task_ids=[t.task_id for t in tasks if f in t.facet_ids])
                for f in sorted({f for t in tasks for f in t.facet_ids})
            ],
        )
        await self.s.db.save_plan(self.s.run_id, self.s.fence, plan)
        return {
            "plan_history": state.get("plan_history", []) + [plan.model_dump(mode="json")],
            "batch_no": batch,
            "expected_task_ids": [t.task_id for t in tasks],
        }

    def dispatch(self, state):
        from ..services.peer_review import task_questions

        return [
            Send(
                "research_task",
                {
                    "run_id": self.s.run_id,
                    "batch_no": state["batch_no"],
                    "task": task,
                    "memory_snapshot": state["memory_snapshot"],
                    "discovery": state.get("discovery", {}),
                    "peer_questions": task_questions(state, task),
                },
            )
            for task in state["plan_history"][-1]["tasks"]
        ]

    async def join(self, state):
        expected = {f"task:{state['batch_no']}:{tid}" for tid in state["expected_task_ids"]}
        if expected - set(state.get("task_results", {})):
            raise InvariantViolation("join before all tasks finished")
        return {}

    async def merge(self, state):
        if self.s.config.get("source_first_workflow_version"):
            # Notes remain original-source research material. There are no
            # pre-writing claims to merge or promote into verified facts.
            return {}
        from ..services.merging import merge_claims

        self.s.understanding = state["understanding"]
        await self.s.db.phase(self.s.run_id, self.s.fence, "merge")
        return await merge_claims(self.s, state)

    async def peer_consultation(self, state):
        from ..services.peer_review import PeerConsultation

        return await PeerConsultation(self.s).before_write(state)

    async def gaps(self, state):
        criteria = self.all_criteria(state)
        if self.s.config.get("source_first_workflow_version"):
            from ..services.source_notes import SourceNotes

            results = await SourceNotes(self.s).assessments(list(criteria.values()))
        else:
            results = await self.s.evidence.assessments(
                list(criteria.values()), state["understanding"]["source_policy"], state["batch_no"]
            )
        indexed = {r.criterion_id: r for r in results}
        gaps = []
        covered = 0
        required = 0
        for facet in state["understanding"]["facets"]:
            ids = {
                c["criterion_id"]
                for t in state["plan_history"][0]["tasks"]
                if facet["facet_id"] in t["facet_ids"]
                for c in t["success_criteria"]
                if facet["facet_id"] in (c.get("facet_ids") or t["facet_ids"])
            }
            required_ids = {cid for cid in ids if criteria[cid].required}
            completion_ids = required_ids if facet["required"] else ids
            passed = bool(completion_ids) and all(indexed[c].status == "covered" for c in completion_ids)
            if facet["required"]:
                required += 1
                covered += int(passed)
            for cid in sorted(ids):
                if indexed[cid].status == "covered":
                    continue
                gaps.append(
                    Gap(
                        gap_id=stable_id("gap", self.s.run_id, facet["facet_id"], cid),
                        facet_id=facet["facet_id"],
                        criterion_id=cid,
                        type="conflict" if indexed[cid].status == "conflicted" else "missing",
                        priority=1 if facet["required"] and criteria[cid].required else 3,
                        question=criteria[cid].description,
                        evidence_ids=[],
                    )
                )
        gaps.sort(key=lambda g: (g.priority, g.facet_id, g.criterion_id or ""))
        assessment = GapAssessment(
            gaps=gaps,
            required_coverage=covered / max(1, required),
            can_answer=covered == required,
            explanation="按全部初始必需维度和证据门槛重新计算。",
        )
        if self.s.config.get("source_first_workflow_version"):
            assessment.explanation = "这是写作材料就绪程度；最终事实和问题覆盖仍需回答核验。"
            from ..services.peer_review import apply_peer_gaps

            return {"gap_assessment": apply_peer_gaps(state, assessment.model_dump(mode="json"), criteria)}
        async with self.s.db.transaction(self.s.run_id, self.s.fence) as conn:
            await conn.execute(
                "UPDATE research.runs SET coverage=%s WHERE id=%s",
                (assessment.required_coverage, self.s.run_id),
            )
            if gaps:
                await self.s.db.event(
                    conn,
                    self.s.run_id,
                    "gap.detected",
                    {"count": len(gaps), "coverage": assessment.required_coverage},
                )
        return {"gap_assessment": assessment.model_dump(mode="json")}

    async def after_gaps(self, state):
        if state["batch_no"] != 0 or not any(g["priority"] == 1 for g in state["gap_assessment"]["gaps"]):
            return "write"
        try:
            remaining = await self.s.ops.guard()
            if remaining < 60:
                return "write"
        except Stopped:
            return "write"
        run = await self.s.db.get_run(self.s.run_id)
        b = run["budget"]
        if (
            self.s.config.get("reader_budget_workflow_version")
            and await self.s.ops.research_headroom() < self.s.config["research"]["min_supplemental_tokens"]
        ):
            return "write"
        from ..services.peer_review import pending_messages

        # Peer challenges first reread frozen sources, including private-only runs with no web allowance.
        requirements = {"tokens_research": 20000}
        if not pending_messages(state.get("peer_consultation", {})):
            requirements.update(search_calls=2, fetched_pages=2)
        if all(
            b["limits"][k] - b["used"].get(k, 0) - b["reserved"].get(k, 0) >= v
            for k, v in requirements.items()
        ):
            return "plan"
        return "write"

    async def write(self, state):
        if self.s.config.get("adaptive_research_workflow_version"):
            from ..services.adaptive_answer import AdaptiveAnswerWorkflow

            return await AdaptiveAnswerWorkflow(self.s, self).write(state)
        if self.s.config.get("source_first_workflow_version"):
            from ..services.source_workflow import SourceWorkflow

            return await SourceWorkflow(self.s, self).write(state)
        from ..services.narrative import NarrativeWorkflow

        return await NarrativeWorkflow(self.s, self).write(state)

    async def quality(self, state):
        if self.s.config.get("adaptive_research_workflow_version"):
            from ..services.adaptive_answer import AdaptiveAnswerWorkflow
            from ..services.peer_review import PeerConsultation

            peer = await PeerConsultation(self.s).after_review(state)
            return {**peer, **await AdaptiveAnswerWorkflow(self.s, self).resolve({**state, **peer})}
        if self.s.config.get("source_first_workflow_version"):
            from ..services.source_workflow import SourceWorkflow

            return await SourceWorkflow(self.s, self).quality(state)
        from ..services.narrative import NarrativeWorkflow

        if state.get("report_draft", {}).get("schema_version") == 3:
            from ..services.editorial import EditorialWorkflow

            return await EditorialWorkflow(self.s, self).quality(state)
        return await NarrativeWorkflow(self.s, self).quality(state)

    async def validate_report(self, state):
        if self.s.config.get("adaptive_research_workflow_version"):
            from ..services.adaptive_answer import AdaptiveAnswerWorkflow

            return await AdaptiveAnswerWorkflow(self.s, self).review(state)
        if self.s.config.get("source_first_workflow_version"):
            from ..services.source_workflow import SourceWorkflow

            return await SourceWorkflow(self.s, self).validate(state)
        from ..services.narrative import NarrativeWorkflow

        if state.get("report_draft", {}).get("schema_version") == 3:
            from ..services.editorial import EditorialWorkflow

            return await EditorialWorkflow(self.s, self).validate(state)
        return await NarrativeWorkflow(self.s, self).validate(state)

    async def finalize(self, state):
        if state.get("rejection"):
            await self.s.db.terminal(
                self.s.run_id, self.s.fence, "rejected", state["rejection"], {"code": state["rejection"]}
            )
            return {}
        run = await self.s.db.get_run(self.s.run_id)
        if run["cancel_requested"]:
            await self.s.db.terminal(self.s.run_id, self.s.fence, "cancelled", "cancelled")
            return {}
        bundle = await self.s.evidence.bundle()
        from ..contracts import parse_draft

        draft = parse_draft(state["report_draft"])
        report = render_report(
            self.s.run_id,
            draft,
            bundle,
            "partial" if state.get("report_partial") else "completed",
            self.s.request,
        )
        items = []
        memory_status = (
            "off"
            if self.s.request.memory_mode == "off"
            else "read_only"
            if self.s.request.memory_mode == "read_only"
            else "degraded"
            if state.get("memory_snapshot", {}).get("degraded")
            else "skipped"
        )
        if self.s.request.memory_mode == "read_write":
            try:
                reported_ids = list(
                    dict.fromkeys(
                        cid
                        for section in draft.sections
                        for statement in section.statements
                        if not self.s.config.get("source_first_workflow_version") or statement.kind == "fact"
                        for cid in statement.claim_ids
                    )
                )
                eligible = [
                    next(c for c in bundle["claims"] if c["id"] == cid)
                    for cid in reported_ids
                    if any(
                        c["id"] == cid and c["status"] == "supported" and not c["merged_into"]
                        for c in bundle["claims"]
                    )
                ][:3]
                recalled_items = state.get("memory_snapshot", {}).get("items", [])
                memory_aliases = {f"M{i}": item["memory_id"] for i, item in enumerate(recalled_items)}
                claim_aliases = {f"C{i}": c["id"] for i, c in enumerate(eligible)}
                selected_evidence = sorted(
                    {
                        r["evidence_id"]
                        for r in bundle["relations"]
                        if r["claim_id"] in claim_aliases.values() and r["relation"] == "supports"
                    }
                )
                evidence_aliases = {f"E{i}": eid for i, eid in enumerate(selected_evidence)}
                reverse_evidence = {v: k for k, v in evidence_aliases.items()}
                memory_context = [
                    {
                        "claim_id": f"C{i}",
                        "text": c["canonical_text"],
                        "evidence_ids": [
                            reverse_evidence[r["evidence_id"]]
                            for r in bundle["relations"]
                            if r["claim_id"] == c["id"] and r["relation"] == "supports"
                        ],
                    }
                    for i, c in enumerate(eligible)
                ]

                def validate_memory(value):
                    for item in value.items:
                        validate_ids(item.claim_ids, claim_aliases)
                        validate_ids(item.evidence_ids, evidence_aliases)
                        validate_ids(item.supersedes, memory_aliases)

                proposal = await self.s.llm.generate(
                    "memory:candidates:v2",
                    "memory_candidates",
                    {
                        "run_id": self.s.run_id,
                        "query": self.s.request.query,
                        "verified_claims": memory_context,
                        "unresolved": draft.unresolved_questions,
                        "recalled_items": [
                            {"memory_id": f"M{i}", "kind": item["kind"], "text": item["text"][:300]}
                            for i, item in enumerate(recalled_items)
                        ],
                    },
                    MemoryProposal,
                    partition="memory",
                    validator=validate_memory,
                )
                proposal = proposal.model_copy(
                    update={
                        "items": [
                            item.model_copy(
                                update={
                                    "claim_ids": [claim_aliases[cid] for cid in item.claim_ids],
                                    "evidence_ids": [evidence_aliases[eid] for eid in item.evidence_ids],
                                    "supersedes": [memory_aliases[mid] for mid in item.supersedes],
                                }
                            )
                            for item in proposal.items
                        ]
                    }
                )
                items = self.s.memory.validate_candidates(
                    proposal,
                    bundle,
                    self.s.run_id,
                    state.get("memory_snapshot", {}),
                    draft.unresolved_questions,
                )
            except (ToolFailure, Stopped, ValueError):
                pass
        stop = state.get("stop_reason") or next(
            (
                v["stop_reason"]
                for v in state.get("task_results", {}).values()
                if v["stop_reason"] in {"deadline", "budget_exhausted"}
            ),
            None,
        )
        report_id = await self.s.evidence.finalize(
            report,
            items,
            self.s.settings.memory_scope,
            memory_status,
            stop,
            coverage=state.get(
                "report_coverage",
                0.0
                if state.get("fallback_report")
                else state.get("gap_assessment", {}).get("required_coverage", 0.0),
            ),
        )
        return {"report_id": report_id, "memory_candidates": items}

    def compile(self, checkpointer):
        g = StateGraph(ResearchState)
        for name in [
            "summarize",
            "understand",
            "rewrite",
            "retrieve_local",
            "recall",
            "discover",
            "plan",
            "join",
            "merge",
            "peer_consultation",
            "gaps",
            "write",
            "quality",
            "validate_report",
            "finalize",
        ]:
            g.add_node(name, getattr(self, name))
        if self.s.config.get("source_first_workflow_version"):
            from .source_researcher import SourceResearcher

            child = SourceResearcher(self.s).compile()
        else:
            child = Researcher(self.s).compile()
        # The child is a compiled subgraph with its own checkpoint boundaries and output-only patch.
        g.add_node("research_task", child)
        context_enabled = bool(self.s.config.get("context_workflow_version"))
        g.add_edge(START, "summarize" if context_enabled else "understand")
        g.add_edge("summarize", "understand")
        next_node = "rewrite" if context_enabled else "recall"
        g.add_conditional_edges(
            "understand",
            lambda s: "finalize" if s.get("rejection") else next_node,
            {"finalize": "finalize", next_node: next_node},
        )
        g.add_edge("rewrite", "retrieve_local")
        g.add_edge("retrieve_local", "recall")
        g.add_edge("recall", "discover")
        g.add_edge("discover", "plan")
        g.add_conditional_edges("plan", self.dispatch, ["research_task"])
        g.add_edge("research_task", "join")
        g.add_edge("join", "merge")
        if self.s.config.get("peer_review_workflow_version"):
            g.add_edge("merge", "peer_consultation")
            g.add_edge("peer_consultation", "gaps")
        else:
            g.add_edge("merge", "gaps")
        g.add_conditional_edges("gaps", self.after_gaps, {"plan": "plan", "write": "write"})
        g.add_edge("write", "validate_report")
        g.add_edge("validate_report", "quality")
        g.add_edge("quality", "finalize")
        g.add_edge("finalize", END)
        return g.compile(checkpointer=checkpointer)
