import hashlib
from datetime import datetime

from ..contracts import CriterionAssessment, Report, canonical, normalize, stable_id, validate_ids
from ..errors import InvariantViolation
from .db import J, all_rows, one


def row_json(row):
    return {
        k: v.isoformat() if isinstance(v, datetime) else str(v) if type(v).__name__ == "UUID" else v
        for k, v in row.items()
    }


def matching_identity(claim, proof):
    return bool(
        claim
        and proof
        and claim.get("basis") == proof.get("basis")
        and normalize(claim.get("publisher_name", "")).casefold()
        == normalize(proof.get("publisher_name", "")).casefold()
    )


class EvidenceStore:
    def __init__(self, db, run_id, fence):
        self.db = db
        self.run_id = run_id
        self.fence = fence

    async def source(self, page):
        sid = stable_id("source", self.run_id, page.canonical_url, page.content_sha256)
        if hashlib.sha256(page.text.encode()).hexdigest() != page.content_sha256:
            raise InvariantViolation("source hash mismatch")
        async with self.db.transaction(self.run_id, self.fence) as conn:
            prior = await one(
                conn, "SELECT * FROM research.sources WHERE run_id=%s AND id=%s", (self.run_id, sid)
            )
            if prior:
                if prior["content_text"] != page.text:
                    raise InvariantViolation("immutable source changed")
                return sid
            await conn.execute(
                "INSERT INTO research.sources(id,run_id,requested_url,canonical_url,title,publisher_key,source_type,media_type,content_text,content_sha256,published_at,retrieved_at,metadata) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    sid,
                    self.run_id,
                    page.requested_url,
                    page.canonical_url,
                    page.title,
                    page.publisher_key,
                    page.source_type,
                    page.media_type,
                    page.text,
                    page.content_sha256,
                    page.published_at,
                    page.retrieved_at,
                    J(page.metadata),
                ),
            )
        return sid

    async def persist(
        self, task, candidates, primary_provenance=None, identity_claim=None, *, assertion_purpose="core_fact"
    ):
        accepted = []
        confirmed = {}
        async with self.db.transaction(self.run_id, self.fence) as conn:
            for candidate, verdict in candidates:
                sid = candidate.source_id
                source = await one(
                    conn, "SELECT * FROM research.sources WHERE run_id=%s AND id=%s", (self.run_id, sid)
                )
                if (
                    not source
                    or source["content_text"][candidate.start_char : candidate.end_char] != candidate.quote
                ):
                    raise InvariantViolation("quote span mismatch")
                validate_ids(candidate.criterion_ids, [c.criterion_id for c in task.success_criteria])
                validate_ids(verdict.criterion_ids, [c.criterion_id for c in task.success_criteria])
                if verdict.relation == "insufficient" or not verdict.criterion_ids:
                    continue
                if verdict.relation != candidate.relation:
                    continue
                eid = stable_id("evidence", sid, candidate.start_char, candidate.end_char)
                cid = stable_id(
                    "claim", self.run_id, normalize(candidate.claim_text), candidate.temporal_scope or ""
                )
                await conn.execute(
                    "INSERT INTO research.claims(id,run_id,canonical_text,normalized_text,temporal_scope,temporal_key,status,confidence) VALUES(%s,%s,%s,%s,%s,%s,'unverified','low') ON CONFLICT DO NOTHING",
                    (
                        cid,
                        self.run_id,
                        candidate.claim_text,
                        normalize(candidate.claim_text),
                        candidate.temporal_scope,
                        candidate.temporal_scope or "",
                    ),
                )
                direct = "direct" if verdict.direct and candidate.directness == "direct" else "indirect"
                await conn.execute(
                    "INSERT INTO research.evidence(id,run_id,source_id,quote,start_char,end_char,directness,verified) VALUES(%s,%s,%s,%s,%s,%s,%s,true) ON CONFLICT DO NOTHING",
                    (
                        eid,
                        self.run_id,
                        sid,
                        candidate.quote,
                        candidate.start_char,
                        candidate.end_char,
                        direct,
                    ),
                )
                previous = await one(
                    conn,
                    "SELECT verification FROM research.claim_evidence WHERE run_id=%s AND claim_id=%s AND evidence_id=%s AND relation=%s",
                    (self.run_id, cid, eid, verdict.relation),
                )
                verification = verdict.model_dump(mode="json")
                from ..services.provenance import valid_stored_identity

                support = (
                    source
                    if primary_provenance and primary_provenance["support_source_id"] == str(source["id"])
                    else (
                        await one(
                            conn,
                            "SELECT * FROM research.sources WHERE run_id=%s AND id=%s",
                            (self.run_id, primary_provenance["support_source_id"]),
                        )
                        if primary_provenance
                        else None
                    )
                )
                qualified = valid_stored_identity(primary_provenance, source, support)
                proof = primary_provenance if qualified else None
                # Identity belongs to the immutable source snapshot. Reuse it
                # only for an identical publisher/basis assertion; a secondary
                # claim must never be promoted merely by sharing a website.
                if not proof and identity_claim:
                    recorded = await all_rows(
                        conn,
                        "SELECT ce.verification FROM research.claim_evidence ce JOIN research.evidence e ON e.id=ce.evidence_id AND e.run_id=ce.run_id WHERE ce.run_id=%s AND e.source_id=%s AND jsonb_typeof(ce.verification->'primary_provenance')='object'",
                        (self.run_id, sid),
                    )
                    for item in recorded:
                        cached = item["verification"].get("primary_provenance")
                        if not matching_identity(identity_claim, cached):
                            continue
                        cached_support = await one(
                            conn,
                            "SELECT * FROM research.sources WHERE run_id=%s AND id=%s",
                            (self.run_id, cached["support_source_id"]),
                        )
                        if valid_stored_identity(cached, source, cached_support):
                            proof = cached
                            break
                qualified = bool(proof)
                verification["primary_for_claim"] = verdict.primary_for_claim and qualified
                verification["primary_provenance"] = proof
                verification["primary_claimed_by_model"] = verdict.primary_for_claim
                verification["primary_identity_claim"] = identity_claim
                from ..services.assertion_policy import ordinary_explanation

                verification["assertion_purpose"] = (
                    "explanation"
                    if ordinary_explanation(candidate.claim_text, assertion_purpose)
                    else "core_fact"
                )
                verification["body_evidence_requirement"] = (
                    "traceable_direct" if verification["assertion_purpose"] == "explanation" else "criterion"
                )
                if previous:
                    old = previous["verification"]
                    verification["criterion_ids"] = sorted(
                        set(old["criterion_ids"] + verification["criterion_ids"])
                    )
                    verification["direct"] = old["direct"] and verification["direct"]
                    verification["primary_claimed_by_model"] = (
                        old.get("primary_claimed_by_model", old["primary_for_claim"])
                        and verification["primary_claimed_by_model"]
                    )
                    verification["primary_for_claim"] = verification["primary_claimed_by_model"] and qualified
                    if old.get("assertion_purpose", "core_fact") != verification["assertion_purpose"]:
                        verification.update(
                            assertion_purpose="core_fact", body_evidence_requirement="criterion"
                        )
                await conn.execute(
                    "INSERT INTO research.claim_evidence(run_id,claim_id,evidence_id,relation,verification) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(claim_id,evidence_id,relation) DO UPDATE SET verification=excluded.verification",
                    (self.run_id, cid, eid, verdict.relation, J(verification)),
                )
                for criterion in verdict.criterion_ids:
                    await conn.execute(
                        "INSERT INTO research.task_claims(run_id,task_id,criterion_id,claim_id) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (self.run_id, task.task_id, criterion, cid),
                    )
                accepted.append(eid)
                if proof:
                    confirmed[sid] = proof
                if not previous or previous["verification"]["criterion_ids"] != verification["criterion_ids"]:
                    await self.db.event(
                        conn,
                        self.run_id,
                        "evidence.accepted",
                        {"task_id": task.task_id, "evidence_id": eid, "claim_id": cid, "source_id": sid},
                    )
            for sid, proof in confirmed.items():
                pending = await all_rows(
                    conn,
                    "SELECT ce.* FROM research.claim_evidence ce JOIN research.evidence e ON e.id=ce.evidence_id AND e.run_id=ce.run_id WHERE ce.run_id=%s AND e.source_id=%s AND ce.verification->>'primary_claimed_by_model'='true' AND ce.verification->>'primary_for_claim'='false'",
                    (self.run_id, sid),
                )
                updated = 0
                for relation in pending:
                    v = relation["verification"]
                    if (
                        v.get("primary_claimed_by_model")
                        and not v.get("primary_for_claim")
                        and matching_identity(v.get("primary_identity_claim"), proof)
                    ):
                        v.update(
                            primary_for_claim=True,
                            primary_provenance=proof,
                            primary_identity_reused_from_same_snapshot=True,
                        )
                        await conn.execute(
                            "UPDATE research.claim_evidence SET verification=%s WHERE run_id=%s AND claim_id=%s AND evidence_id=%s AND relation=%s",
                            (
                                J(v),
                                self.run_id,
                                relation["claim_id"],
                                relation["evidence_id"],
                                relation["relation"],
                            ),
                        )
                        updated += 1
                if updated:
                    await self.db.event(
                        conn,
                        self.run_id,
                        "source.identity_reconciled",
                        {"source_id": sid, "source_sha256": proof["source_sha256"], "relations": updated},
                    )
        return sorted(set(accepted))

    async def source_index(self):
        await self.db.get_run(self.run_id)
        async with self.db.pool.connection() as conn:
            rows = await all_rows(
                conn,
                "SELECT id,canonical_url,title,content_sha256,metadata FROM research.sources WHERE run_id=%s ORDER BY id",
                (self.run_id,),
            )
        return [row_json(row) for row in rows]

    async def bundle(self, task_id=None):
        await self.db.get_run(self.run_id)
        async with self.db.pool.connection() as conn:
            tasks = await all_rows(
                conn,
                "SELECT id,specification,snapshot,status FROM research.tasks WHERE run_id=%s ORDER BY id",
                (self.run_id,),
            )
            claims = await all_rows(
                conn, "SELECT * FROM research.claims WHERE run_id=%s ORDER BY id", (self.run_id,)
            )
            evidence = await all_rows(
                conn, "SELECT * FROM research.evidence WHERE run_id=%s ORDER BY id", (self.run_id,)
            )
            sources = await all_rows(
                conn,
                "SELECT id,canonical_url,title,publisher_key,source_type,content_sha256,published_at,retrieved_at,metadata FROM research.sources WHERE run_id=%s ORDER BY id",
                (self.run_id,),
            )
            relations = await all_rows(
                conn,
                "SELECT * FROM research.claim_evidence WHERE run_id=%s ORDER BY claim_id,evidence_id",
                (self.run_id,),
            )
            mappings = await all_rows(
                conn,
                "SELECT * FROM research.task_claims WHERE run_id=%s ORDER BY task_id,criterion_id,claim_id",
                (self.run_id,),
            )
            conflicts = await all_rows(
                conn,
                "SELECT * FROM research.claim_conflicts WHERE run_id=%s AND status='open' ORDER BY id",
                (self.run_id,),
            )
        if task_id:
            # Supplemental tasks inherit original-criterion evidence.
            task = next((t for t in tasks if str(t["id"]) == task_id), None)
            criterion_ids = {
                c["criterion_id"] for c in (task["specification"]["success_criteria"] if task else [])
            }
            mappings = [m for m in mappings if m["criterion_id"] in criterion_ids]
            claim_ids = {str(m["claim_id"]) for m in mappings}
            claims = [c for c in claims if str(c["id"]) in claim_ids]
            relations = [r for r in relations if str(r["claim_id"]) in claim_ids]
            eids = {str(r["evidence_id"]) for r in relations}
            evidence = [e for e in evidence if str(e["id"]) in eids]
            sids = {str(e["source_id"]) for e in evidence}
            sources = [s for s in sources if str(s["id"]) in sids]
        return {
            key: [row_json(row) for row in rows]
            for key, rows in [
                ("tasks", tasks),
                ("claims", claims),
                ("evidence", evidence),
                ("sources", sources),
                ("relations", relations),
                ("mappings", mappings),
                ("conflicts", conflicts),
            ]
        }

    async def get_source(self, source_id):
        await self.db.get_run(self.run_id)
        async with self.db.pool.connection() as conn:
            row = await one(
                conn, "SELECT * FROM research.sources WHERE run_id=%s AND id=%s", (self.run_id, source_id)
            )
            if not row:
                from ..errors import ResearchError

                raise ResearchError("not_found")
            return row_json(row)

    async def assessments(self, criteria, policy, batch_no=0):
        from ..services.assertion_policy import explanation_supported

        bundle = await self.bundle()
        results = assess_criteria(criteria, bundle, policy)
        async with self.db.transaction(self.run_id, self.fence) as conn:
            for result in results:
                await conn.execute(
                    "INSERT INTO research.criterion_assessments(run_id,criterion_id,batch_no,assessment) VALUES(%s,%s,%s,%s) ON CONFLICT(run_id,criterion_id,batch_no) DO UPDATE SET assessment=excluded.assessment,updated_at=now()",
                    (self.run_id, result.criterion_id, batch_no, J(result.model_dump(mode="json"))),
                )
            for claim in bundle["claims"]:
                if claim["merged_into"] or claim["status"] == "superseded":
                    continue
                related = [
                    r for r in results if claim["id"] in r.supporting_claim_ids + r.contradicting_claim_ids
                ]
                body_supported = explanation_supported(claim, bundle)
                if not related and not body_supported:
                    continue
                contested = any(r.status == "conflicted" for r in related)
                covered = any(
                    r.status == "covered" and claim["id"] in r.supporting_claim_ids for r in related
                )
                supported = covered or body_supported
                status = "contested" if contested else "supported" if supported else "unverified"
                primary = any(
                    rel["claim_id"] == claim["id"] and rel["verification"].get("primary_for_claim")
                    for rel in bundle["relations"]
                )
                confidence = "low" if not supported or contested else "high" if primary else "medium"
                await conn.execute(
                    "UPDATE research.claims SET status=%s,confidence=%s WHERE id=%s AND run_id=%s",
                    (status, confidence, claim["id"], self.run_id),
                )
        return results

    async def apply_merge(self, proposal):
        bundle = await self.bundle()
        claim_ids = {c["id"] for c in bundle["claims"]}
        claims = {c["id"]: c for c in bundle["claims"]}
        conflicts = bundle["conflicts"]

        def root(cid):
            seen = set()
            while claims[cid]["merged_into"]:
                if cid in seen:
                    raise InvariantViolation("cyclic claim merge")
                seen.add(cid)
                cid = claims[cid]["merged_into"]
            return cid

        for pair in proposal.pairs:
            validate_ids([pair.claim_a_id, pair.claim_b_id], claim_ids)
        async with self.db.transaction(self.run_id, self.fence) as conn:
            for pair in proposal.pairs:
                a, b = sorted([root(pair.claim_a_id), root(pair.claim_b_id)])
                if a == b or pair.relation == "distinct":
                    continue
                ca, cb = claims[a], claims[b]
                if ca["temporal_key"] != cb["temporal_key"]:
                    continue
                if pair.relation == "conflict":
                    await conn.execute(
                        "INSERT INTO research.claim_conflicts(id,run_id,claim_a_id,claim_b_id,reason,status) VALUES(%s,%s,%s,%s,%s,'open') ON CONFLICT DO NOTHING",
                        (stable_id("conflict", self.run_id, a, b), self.run_id, a, b, pair.reason),
                    )
                    conflicts.append(
                        {"id": stable_id("conflict", self.run_id, a, b), "claim_a_id": a, "claim_b_id": b}
                    )
                elif pair.relation == "equivalent":
                    if any({root(c["claim_a_id"]), root(c["claim_b_id"])} == {a, b} for c in conflicts):
                        continue  # Equivalence must not erase an unresolved conflict.
                    relations = await all_rows(
                        conn,
                        "SELECT * FROM research.claim_evidence WHERE run_id=%s AND claim_id=%s",
                        (self.run_id, b),
                    )
                    for rel in relations:
                        prior = await one(
                            conn,
                            "SELECT verification FROM research.claim_evidence WHERE run_id=%s AND claim_id=%s AND evidence_id=%s AND relation=%s",
                            (self.run_id, a, rel["evidence_id"], rel["relation"]),
                        )
                        verification = dict(rel["verification"])
                        if prior:
                            old = prior["verification"]
                            verification["criterion_ids"] = sorted(
                                set(old["criterion_ids"] + verification["criterion_ids"])
                            )
                            for field in ["direct", "primary_for_claim"]:
                                verification[field] = old[field] and verification[field]
                        await conn.execute(
                            "INSERT INTO research.claim_evidence(run_id,claim_id,evidence_id,relation,verification) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(claim_id,evidence_id,relation) DO UPDATE SET verification=excluded.verification",
                            (self.run_id, a, rel["evidence_id"], rel["relation"], J(verification)),
                        )
                    await conn.execute(
                        "INSERT INTO research.task_claims(run_id,task_id,criterion_id,claim_id) SELECT run_id,task_id,criterion_id,%s FROM research.task_claims WHERE run_id=%s AND claim_id=%s ON CONFLICT DO NOTHING",
                        (a, self.run_id, b),
                    )
                    cb["merged_into"] = a
                    for conflict in conflicts:
                        old_pair = (conflict["claim_a_id"], conflict["claim_b_id"])
                        new_pair = tuple(sorted(root(x) for x in old_pair))
                        if old_pair != new_pair:
                            await conn.execute(
                                "UPDATE research.claim_conflicts SET claim_a_id=%s,claim_b_id=%s WHERE run_id=%s AND id=%s",
                                (*new_pair, self.run_id, conflict["id"]),
                            )
                            conflict["claim_a_id"], conflict["claim_b_id"] = new_pair
                    await conn.execute(
                        "UPDATE research.claims SET merged_into=%s WHERE run_id=%s AND id=%s",
                        (a, self.run_id, b),
                    )

    async def finalize(
        self, report, memory_items, scope, memory_status="skipped", stop_reason=None, coverage=None
    ):
        async with self.db.transaction(self.run_id, self.fence, allow_terminal=True) as conn:
            row = await one(
                conn,
                "SELECT status,report_id,cancel_requested FROM research.runs WHERE id=%s",
                (self.run_id,),
            )
            from ..contracts import TERMINAL

            if row["status"] in TERMINAL:
                return str(row["report_id"]) if row["report_id"] else None
            if row["cancel_requested"]:
                await conn.execute(
                    "UPDATE research.runs SET status='cancelled',stop_reason='cancelled',phase='cancelled',finished_at=now(),memory_status='skipped' WHERE id=%s",
                    (self.run_id,),
                )
                await self.db.event(conn, self.run_id, "run.finished", {"status": "cancelled"})
                return None
            # Recheck every citation within the final transaction, including the immutable quote span.
            statement_ids = {s.statement_id for sec in report.structured.sections for s in sec.statements}
            paragraph_map = {
                p.paragraph_id: p
                for sec in report.structured.sections
                for p in getattr(sec, "paragraphs", [])
            }
            for citation in report.citations:
                if citation.statement_id not in statement_ids:
                    raise InvariantViolation("unknown statement")
                if paragraph_map:
                    from ..services.narrative import anchor_span

                    paragraph = paragraph_map.get(citation.paragraph_id)
                    assertion = (
                        next(
                            (a for a in paragraph.assertions if a.statement_id == citation.statement_id), None
                        )
                        if paragraph
                        else None
                    )
                    if not assertion or anchor_span(paragraph, assertion) != (
                        citation.start_char,
                        citation.end_char,
                    ):
                        raise InvariantViolation("invalid narrative citation location")
                relation = await one(
                    conn,
                    "SELECT e.*,s.content_text FROM research.claim_evidence ce JOIN research.evidence e ON (e.run_id,e.id)=(ce.run_id,ce.evidence_id) JOIN research.sources s ON (s.run_id,s.id)=(e.run_id,e.source_id) WHERE ce.run_id=%s AND ce.claim_id=%s AND ce.evidence_id=%s AND e.verified",
                    (self.run_id, citation.claim_id, citation.evidence_id),
                )
                if (
                    not relation
                    or relation["content_text"][relation["start_char"] : relation["end_char"]]
                    != relation["quote"]
                ):
                    raise InvariantViolation("invalid final citation")
            await conn.execute(
                "INSERT INTO research.reports(id,run_id,version,status,markdown,structured,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (
                    report.report_id,
                    self.run_id,
                    report.version,
                    report.status,
                    report.markdown,
                    J(report.structured.model_dump(mode="json")),
                    report.created_at,
                ),
            )
            for c in report.citations:
                await conn.execute(
                    "INSERT INTO research.citations(id,run_id,report_id,statement_id,claim_id,evidence_id,ordinal,paragraph_id,start_char,end_char) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        c.citation_id,
                        self.run_id,
                        report.report_id,
                        c.statement_id,
                        c.claim_id,
                        c.evidence_id,
                        c.ordinal,
                        c.paragraph_id,
                        c.start_char,
                        c.end_char,
                    ),
                )
            if memory_items:
                memory_status = "pending"
                await conn.execute(
                    "INSERT INTO research.memory_outbox(id,run_id,agent_scope,kind,payload,status,attempts,remote_ids) VALUES(%s,%s,%s,'report_memory',%s,'pending',0,%s) ON CONFLICT DO NOTHING",
                    (
                        stable_id("outbox", self.run_id, "report_memory"),
                        self.run_id,
                        scope,
                        J({"schema_version": 1, "items": memory_items}),
                        J({}),
                    ),
                )
            await conn.execute(
                "UPDATE research.runs SET report_id=%s,status=%s,phase=%s,finished_at=now(),memory_status=%s,stop_reason=%s,coverage=COALESCE(%s,coverage) WHERE id=%s",
                (
                    report.report_id,
                    report.status,
                    report.status,
                    memory_status,
                    stop_reason,
                    coverage,
                    self.run_id,
                ),
            )
            await self.db.event(
                conn, self.run_id, "run.finished", {"status": report.status, "report_id": report.report_id}
            )
            return report.report_id

    async def report(self):
        run = await self.db.get_run(self.run_id)
        if not run["report_id"]:
            from ..contracts import TERMINAL
            from ..errors import ResearchError

            raise ResearchError("no_report" if run["status"] in TERMINAL else "result_not_ready")
        async with self.db.pool.connection() as conn:
            row = await one(conn, "SELECT * FROM research.reports WHERE run_id=%s", (self.run_id,))
            citations = await all_rows(
                conn,
                "SELECT id AS citation_id,statement_id,claim_id,evidence_id,ordinal,paragraph_id,start_char,end_char FROM research.citations WHERE run_id=%s ORDER BY ordinal,statement_id,id",
                (self.run_id,),
            )
            uncertain = await one(
                conn,
                "SELECT COALESCE(sum(charged_tokens),0) AS tokens FROM research.operation_attempts WHERE run_id=%s AND status='unknown'",
                (self.run_id,),
            )
            costs = await one(
                conn,
                "SELECT sum((result->'usage'->>'cost')::numeric) AS cost,count(*) AS calls,count(result->'usage'->>'cost') AS priced FROM research.operations WHERE run_id=%s AND kind='llm' AND status='succeeded'",
                (self.run_id,),
            )
        import math

        cost = float(costs["cost"]) if costs["cost"] is not None else None
        if cost is not None and not math.isfinite(cost):
            cost = None
        used = run["budget"]["used"]
        execution = {
            "model_id": run["config"]["model_id"],
            "reasoning_efforts": run["config"].get("reasoning_efforts"),
            "search_calls": used.get("search_calls", 0),
            "fetched_pages": used.get("fetched_pages", 0),
            "tokens": used.get("total_tokens", 0),
            "unknown_tokens": int(uncertain["tokens"]),
            "stop_reason": run["stop_reason"],
            "memory_status": run["memory_status"],
            "known_cost_usd": cost,
            "cost_usd": cost if not uncertain["tokens"] and costs["calls"] == costs["priced"] else None,
        }
        return Report.model_validate_json(
            canonical(
                {
                    "schema_version": 1,
                    "report_id": str(row["id"]),
                    "run_id": self.run_id,
                    "version": row["version"],
                    "status": row["status"],
                    "markdown": row["markdown"],
                    "structured": row["structured"],
                    "citations": citations,
                    "created_at": row["created_at"],
                    "execution": execution,
                }
            )
        )


def independent_origins(sources):
    from ..services.documents import work_identity

    groups = []
    for source in sources:
        document = source.get("metadata", {}).get("document") or work_identity(
            source.get("canonical_url", "")
        )
        works = set(document.get("identifiers", []))
        matches = [
            g
            for g in groups
            if source["publisher_key"] in g["publishers"]
            or source["content_sha256"] in g["hashes"]
            or works.intersection(g["works"])
        ]
        group = {
            "publishers": {source["publisher_key"]},
            "hashes": {source["content_sha256"]},
            "works": works,
        }
        for matched in matches:
            group["publishers"].update(matched["publishers"])
            group["hashes"].update(matched["hashes"])
            group["works"].update(matched["works"])
            groups.remove(matched)
        groups.append(group)
    return len(groups)


def source_threshold(criterion, sources, primary, policy):
    """One source requirement shared by audit consistency and stored coverage."""
    if criterion.kind == "authoritative_fact":
        return bool(primary)
    return independent_origins(sources) >= 2 and (policy != "primary_required" or primary)


def assess_criteria(criteria, bundle, policy):
    results = []
    evidence = {e["id"]: e for e in bundle["evidence"]}
    sources = {s["id"]: s for s in bundle["sources"]}
    claims = {c["id"]: c for c in bundle["claims"]}
    for criterion in criteria:
        ids = {
            m["claim_id"]
            for m in bundle["mappings"]
            if m["criterion_id"] == criterion.criterion_id
            and not claims[m["claim_id"]]["merged_into"]
            and claims[m["claim_id"]]["status"] != "superseded"
        }
        supporting = []
        opposing = []
        by_claim = {}
        for rel in bundle["relations"]:
            if rel["claim_id"] not in ids:
                continue
            e = evidence[rel["evidence_id"]]
            v = rel["verification"]
            if (
                not e["verified"]
                or e["directness"] != "direct"
                or not v.get("direct")
                or criterion.criterion_id not in v.get("criterion_ids", [])
            ):
                continue
            if rel["relation"] == "supports":
                supporting.append(rel["claim_id"])
                group = by_claim.setdefault(rel["claim_id"], {"sources": [], "primary": False})
                group["sources"].append(sources[e["source_id"]])
                group["primary"] = group["primary"] or v.get("primary_for_claim", False)
            else:
                opposing.append(rel["claim_id"])
        conflict = bool(opposing) or any(
            c["claim_a_id"] in ids or c["claim_b_id"] in ids for c in bundle["conflicts"]
        )
        eligible = set()
        for cid, group in by_claim.items():
            if source_threshold(criterion, group["sources"], group["primary"], policy):
                eligible.add(cid)
        threshold = bool(eligible)
        if threshold:
            supporting = sorted(eligible)
        status = "conflicted" if conflict else "covered" if threshold else "missing"
        results.append(
            CriterionAssessment(
                criterion_id=criterion.criterion_id,
                status=status,
                supporting_claim_ids=sorted(set(supporting)),
                contradicting_claim_ids=sorted(set(opposing)),
                explanation={
                    "covered": "证据通过语义与来源门槛。",
                    "missing": "有效直接证据或独立来源不足。",
                    "conflicted": "存在未解决的相反证据。",
                }[status],
            )
        )
    return results
