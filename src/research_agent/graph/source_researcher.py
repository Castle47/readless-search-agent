"""Research over original passages, selected by their contribution to the question."""

import re
from urllib.parse import urlsplit, urlunsplit

from ..adapters.fetch import publisher
from ..contracts import digest, normalize
from ..errors import Stopped
from ..services.documents import same_work, work_identity
from ..services.provenance import linked_target_matches
from ..services.retrieval import eligible_candidates, select_sources
from ..services.source_notes import SourceNotes
from .researcher import Researcher


def research_reference(source_url, link, follow_domains):
    source = urlsplit(source_url)
    if re.fullmatch(
        r"status|system status|about(?: us| arxiv)?|donate|give to arxiv|our members|membership|privacy|terms|help|contact|log ?in|sign up",
        link.get("label", "").strip(),
        re.IGNORECASE,
    ):
        return False
    if link.get("full_text") and same_work(work_identity(source_url), work_identity(link["url"])):
        return True
    if source.hostname in {"github.com", "gitlab.com", "huggingface.co", "bitbucket.org"} and publisher(
        link["url"]
    ) == publisher(source_url):
        parts = [p for p in source.path.split("/") if p]
        if len(parts) < 2:
            return False
        root = urlunsplit((source.scheme, source.netloc, "/" + "/".join(parts[:2]), "", ""))
        return linked_target_matches(root, link["url"]) and bool(
            link.get("documentation") or link.get("official_link")
        )
    return bool(
        link.get("official_link") or (link.get("documentation") and publisher(link["url"]) in follow_domains)
    )


def material_progress(notes, sources):
    """Only actual selected material advances research; duplicate views do not."""
    keys = set()
    for n in notes:
        if not n.get("passages") or n.get("material_role") == "irrelevant":
            continue
        source = sources[n["source_id"]]
        identity = source.get("metadata", {}).get("document") or work_identity(source["canonical_url"])
        work = identity["work_id"] or source["canonical_url"]
        for p in n["passages"]:
            keys.add(digest([work, normalize(p["text"])]))
    return sorted(keys)


class SourceResearcher(Researcher):
    def restore_peer_context(self, state):
        if not self.s.config.get("peer_review_workflow_version"):
            return
        task = self.task(state)
        if not hasattr(self.s, "peer_task_questions"):
            self.s.peer_task_questions = {}
        self.s.peer_task_questions[task.task_id] = state.get("peer_questions", [])

    async def read_existing(self, state):
        self.restore_peer_context(state)
        return await super().read_existing(state)

    async def fetch_extract(self, state):
        self.restore_peer_context(state)
        return await super().fetch_extract(state)

    async def guard(self, state):
        self.restore_peer_context(state)
        result = await super().guard(state)
        if (
            self.s.config.get("peer_review_workflow_version")
            and state.get("peer_questions")
            and not result["round"]
            and result["stop_reason"] == "sufficient"
        ):
            # The earlier global readiness predates this challenge. Perform a focused reread first.
            result["stop_reason"] = None
        return result

    async def reason(self, state):
        task = self.task(state)
        if self.s.config.get("adaptive_research_workflow_version"):
            from ..services.research_breadth import query_language, separate_queries

            assessment = await SourceNotes(self.s).readiness()
            used = {normalize(q).casefold() for q in state.get("searched_queries", [])}
            used.update(normalize(q).casefold() for q in state.get("discovery", {}).get("survey_queries", []))
            if state.get("discovery", {}).get("replacement_query"):
                used.add(normalize(state["discovery"]["replacement_query"]).casefold())
            queries = separate_queries(assessment["search_queries"] + task.seed_queries)
            selected = next((q for q in queries if q.casefold() not in used), None)
            if selected:
                return {
                    "pending_action": {"query": selected, "language": query_language(selected)},
                    "stop_reason": None,
                }
        # The planner already supplied a query for the initial investigation.
        # Execute it instead of asking a second model call to expand it again.
        if (
            not self.s.config.get("adaptive_research_workflow_version")
            and not state["round"]
            and not state["searched_queries"]
            and task.seed_queries
        ):
            query = task.seed_queries[0]
            languages = self.s.understanding["search_languages"]
            language = (
                next((l for l in languages if l == "en"), languages[0])
                if not re.search(r"[\u4e00-\u9fff]", query)
                else next((l for l in languages if l.startswith("zh")), languages[0])
            )
            result = {"pending_action": {"query": query, "language": language}, "stop_reason": None}
            await self.s.db.commit_value(
                self.s.run_id,
                self.s.fence,
                f"planned-query:{task.task_id}:v1",
                "planned_query",
                result,
                task.task_id,
            )
            return result
        return await super().reason(state)

    async def criterion_assessments(self, task, batch_no):
        return await SourceNotes(self.s).assessments(task.success_criteria)

    async def extract(self, task, source_id, page, excluded=(), primary_domains=(), focus=""):
        return await SourceNotes(self.s).read(source_id, page, task)

    async def select_read(self, state):
        self.restore_peer_context(state)
        task = self.task(state)
        if (
            self.s.config.get("peer_review_workflow_version")
            and state.get("peer_questions")
            and not state.get("round", 0)
        ):
            known = {s["id"] for s in await self.s.evidence.source_index()}
            wanted = list(
                dict.fromkeys(sid for q in state["peer_questions"] for sid in q["source_ids"] if sid in known)
            )
            if wanted:
                return {
                    "pending_source_ids": wanted[: self.s.config["research"]["pages_per_round"]],
                    "pending_hits": [],
                    "direct_read": False,
                    "primary_domains": [],
                }
        if self.s.config.get("adaptive_research_workflow_version"):
            assessment = await SourceNotes(self.s).readiness()
            selected = assessment["next_sources"]
            # One frontier is consumed by both stop and reading decisions;
            # skipped candidates cannot be silently reintroduced downstream.
            fetched = [c for c in selected if c["fetched"]]
            direct = [c for c in selected if not c["fetched"]]
            cap = self.s.config["research"]["pages_per_round"]
            return {
                "pending_source_ids": [c["source_id"] for c in fetched[:cap]],
                "pending_hits": direct[:cap] if not fetched else [],
                "direct_read": bool(direct) and not fetched,
                "primary_domains": [],
            }
        notes = await SourceNotes(self.s).index()
        sources = await self.s.evidence.source_index()
        primary = {n["source_id"] for n in notes if n["identity"] and n.get("firsthand_criterion_ids")}
        visited = set(state["visited_source_ids"])
        domains = {publisher(s["canonical_url"]) for s in sources if s["id"] in primary}
        follow = domains | {publisher(u) for u in task.source_urls}
        known_read = {n["source_id"] for n in notes}
        if self.s.config.get("adaptive_research_workflow_version"):
            preferred = list(
                dict.fromkeys(
                    task.source_urls + [s["url"] for s in state.get("discovery", {}).get("sources", [])]
                )
            )
            sources.sort(
                key=lambda s: (
                    preferred.index(s["canonical_url"]) if s["canonical_url"] in preferred else len(preferred)
                )
            )
            if self.s.understanding.get("intent") == "explanation":
                roles = {
                    s["source_id"]: s.get("selection_role")
                    for s in state.get("discovery", {}).get("sources", [])
                }
                front = []
                for wanted in [{"original"}, {"explanation", "implementation"}]:
                    found = next((s for s in sources if roles.get(s["id"]) in wanted), None)
                    if found:
                        front.append(found)
                chosen = {s["id"] for s in front}
                sources = front + [s for s in sources if s["id"] not in chosen]
        pending = [s["id"] for s in sources if s["id"] not in visited and s["id"] not in known_read]
        direct = []
        navigation_rejections = []
        for s in sources:
            if s["id"] not in known_read:
                continue
            for link in s.get("metadata", {}).get("reference_links", []):
                if research_reference(s["canonical_url"], link, follow):
                    direct.append(
                        {
                            "url": link["url"],
                            "title": link.get("label", ""),
                            "snippet": "Observed link from " + s["canonical_url"],
                            "rank": len(direct) + 1,
                        }
                    )
                else:
                    navigation_rejections.append(
                        {"url": link["url"], "reason": "not_a_research_reference", "from": s["canonical_url"]}
                    )
        for source in state.get("discovery", {}).get("sources", []):
            for link in source.get("reference_links", []):
                if link.get("official_link") or (
                    link.get("documentation") and publisher(link["url"]) != publisher(source["url"])
                ):
                    direct.append(
                        {
                            "url": link["url"],
                            "title": link.get("label", "Observed documentation"),
                            "snippet": "Observed documentation link",
                            "rank": len(direct) + 1,
                        }
                    )
        # These URLs were explicitly selected by the planner from observed inputs.
        direct += [
            {"url": u, "title": "Planned observed source", "snippet": "", "rank": len(direct) + i + 1}
            for i, u in enumerate(task.source_urls)
        ]
        direct, rejected = await eligible_candidates(self.s, direct)
        if self.s.config.get("adaptive_research_workflow_version") and pending:
            # Read the complementary survey selection before following another
            # chain of same-publisher links. Actual readiness decides when done.
            direct = []
        rejected += navigation_rejections
        # A full-text upgrade is useful before generic publisher documentation.
        direct.sort(key=lambda h: h["document"]["representation"] != "full_text")
        if rejected:
            key = "navigation-filter:" + digest([task.task_id, state["round"], direct, rejected])
            await self.s.db.commit_value(
                self.s.run_id,
                self.s.fence,
                key,
                "navigation_selection",
                {"selected": direct[: self.s.config["research"]["pages_per_round"]], "rejected": rejected},
                task.task_id,
            )
        return {
            "pending_source_ids": pending[: self.s.config["research"]["pages_per_round"]],
            "primary_domains": sorted(domains),
            "direct_read": bool(direct),
            "pending_hits": direct[: self.s.config["research"]["pages_per_round"]],
        }

    async def search(self, state):
        patch = await super().search(state)
        feedback = {**state["pending_action"]}
        if patch.get("stop_reason") == "provider_unavailable":
            # A failed query consumes this existing round. It does not prove
            # that another query/language in the remaining rounds must fail.
            return {
                **patch,
                "stop_reason": None,
                "retrieval_feedback": {
                    **feedback,
                    "outcome": "search_unavailable",
                    "errors": patch.get("search_errors", []),
                },
            }
        if patch.get("stop_reason"):
            return patch
        task = self.task(state)
        try:
            selected = await select_sources(
                self.s,
                patch["pending_hits"],
                task.question,
                [c.description for c in task.success_criteria],
                state["pending_action"]["query"],
            )
        except Stopped as error:
            return {**patch, "pending_hits": [], "stop_reason": error.code}
        outcome = (
            "selected"
            if selected
            else "all_candidates_irrelevant_or_unreadable"
            if patch["pending_hits"]
            else "empty_results"
        )
        return {
            **patch,
            "pending_hits": selected,
            "retrieval_feedback": {
                **feedback,
                "outcome": outcome,
                "returned_titles": [h["title"] for h in patch["pending_hits"]] if not selected else [],
            },
        }

    async def assess(self, state):
        task = self.task(state)
        key = f"round:batch={state['batch_no']}:task={task.task_id}:round={state['round'] + 1}"
        saved = await self.s.db.saved_value(self.s.run_id, key)
        if saved is not None:
            return saved
        results = await self.criterion_assessments(task, state["batch_no"])
        notes = await SourceNotes(self.s).index()
        sources = {s["id"]: s for s in await self.s.evidence.source_index()}
        keys = material_progress(notes, sources)
        progress = bool(set(keys) - set(state.get("material_note_keys", [])))
        patch = {
            "round": state["round"] + 1,
            "no_progress_count": 0 if progress else state["no_progress_count"] + 1,
            "material_note_keys": keys,
            "claim_ids": [],
            "evidence_ids": [],
            "criterion_results": [r.model_dump() for r in results],
        }
        await self.s.db.commit_value(self.s.run_id, self.s.fence, key, "round", patch, task.task_id)
        return patch
