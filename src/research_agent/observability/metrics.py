"""Low-cardinality DB-derived metrics survive worker restarts."""

from ..storage.db import all_rows, one


async def database_metrics(db):
    lines = []

    def metric(name, value, labels=None):
        tag = "{" + ",".join(k + '="' + str(v) + '"' for k, v in labels.items()) + "}" if labels else ""
        lines.append("readless_research_" + name + tag + " " + str(float(value or 0)))

    async with db.pool.connection() as conn:
        for row in await all_rows(
            conn,
            "SELECT status,count(*) AS count FROM research.runs WHERE owner_id=%s GROUP BY status",
            (db.owner,),
        ):
            metric("runs", row["count"], {"status": row["status"]})
        row = await one(
            conn,
            "SELECT avg(extract(epoch FROM (finished_at-started_at))) AS latency,avg(extract(epoch FROM (started_at-created_at))) AS queue_latency,avg(coverage) AS coverage FROM research.runs WHERE owner_id=%s",
            (db.owner,),
        )
        metric("run_latency_seconds", row["latency"])
        metric("queue_latency_seconds", row["queue_latency"])
        metric("required_coverage", row["coverage"])
        for row in await all_rows(
            conn,
            "SELECT o.kind,o.status,count(*) AS count FROM research.operations o JOIN research.runs r ON r.id=o.run_id WHERE r.owner_id=%s GROUP BY o.kind,o.status",
            (db.owner,),
        ):
            metric("operations", row["count"], {"kind": row["kind"], "status": row["status"]})
        row = await one(
            conn,
            "SELECT sum((b.used->>'total_tokens')::bigint) AS tokens FROM research.budgets b JOIN research.runs r ON r.id=b.run_id WHERE r.owner_id=%s",
            (db.owner,),
        )
        metric("llm_tokens", row["tokens"])
        for kind in [
            "budget.denied",
            "checkpoint.resumed",
            "evidence.rejected",
            "evidence.accepted",
            "report.validation_failed",
            "memory.degraded",
            "memory.applied",
        ]:
            row = await one(
                conn,
                "SELECT count(*) AS count FROM research.events e JOIN research.runs r ON r.id=e.run_id WHERE r.owner_id=%s AND e.event_type=%s",
                (db.owner, kind),
            )
            metric("events", row["count"], {"event": kind})
        row = await one(
            conn,
            "SELECT max(extract(epoch FROM (now()-o.created_at))) AS lag FROM research.memory_outbox o JOIN research.runs r ON r.id=o.run_id WHERE r.owner_id=%s AND o.status IN ('pending','processing','needs_reconciliation')",
            (db.owner,),
        )
        metric("memory_outbox_lag_seconds", row["lag"])
    return ("\n".join(lines) + "\n").encode()
