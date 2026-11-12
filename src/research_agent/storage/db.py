from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from ..config import effective_limits
from ..contracts import TERMINAL, digest, stable_id, utcnow
from ..errors import Fenced, InvariantViolation, ResearchError, Stopped

J = Jsonb


async def one(conn, sql, params=()):
    return await (await conn.execute(sql, params)).fetchone()


async def all_rows(conn, sql, params=()):
    return await (await conn.execute(sql, params)).fetchall()


class Database:
    def __init__(self, url, owner="local"):
        self.owner = owner
        self.pool = AsyncConnectionPool(
            url, kwargs={"row_factory": dict_row}, min_size=1, max_size=12, open=False
        )

    async def open(self):
        await self.pool.open()
        await self.pool.wait(timeout=15)

    async def close(self):
        await self.pool.close()

    @asynccontextmanager
    async def transaction(self, run_id=None, fence=None, allow_terminal=False):
        async with self.pool.connection() as conn:
            if run_id:
                run = await one(
                    conn,
                    "SELECT * FROM research.runs WHERE id=%s AND owner_id=%s FOR UPDATE",
                    (run_id, self.owner),
                )
                if not run:
                    raise ResearchError("not_found")
                if fence is not None:
                    if run["fence"] != fence or not run["lease_until"] or run["lease_until"] <= utcnow():
                        raise Fenced()
                    if run["status"] in TERMINAL and not allow_terminal:
                        raise Stopped(run["status"])
            yield conn

    async def event(self, conn, run_id, kind, payload=None):
        # Callers hold the run row lock (serial sequence allocation).
        row = await one(
            conn, "SELECT COALESCE(MAX(seq),0)+1 AS seq FROM research.events WHERE run_id=%s", (run_id,)
        )
        await conn.execute(
            "INSERT INTO research.events(run_id,seq,event_type,payload) VALUES(%s,%s,%s,%s)",
            (run_id, row["seq"], kind, J(payload or {})),
        )

    async def create_run(self, request, key, config):
        if not key or len(key) > 200:
            raise ResearchError("invalid_idempotency_key")
        raw = request.model_dump(mode="json")
        request_hash = digest(raw)
        run_id = str(uuid4())
        now = utcnow()
        frozen = {**raw, "as_of": raw["as_of"] or now.isoformat()}
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO research.runs(id,owner_id,request,config,request_hash,idempotency_key,status,memory_status,created_at) VALUES(%s,%s,%s,%s,%s,%s,'queued',%s,%s) ON CONFLICT(owner_id,idempotency_key) DO NOTHING",
                (
                    run_id,
                    self.owner,
                    J(frozen),
                    J(config),
                    request_hash,
                    key,
                    "off"
                    if request.memory_mode == "off"
                    else "read_only"
                    if request.memory_mode == "read_only"
                    else "pending",
                    now,
                ),
            )
            row = await one(
                conn,
                "SELECT * FROM research.runs WHERE owner_id=%s AND idempotency_key=%s FOR UPDATE",
                (self.owner, key),
            )
            if row["request_hash"] != request_hash:
                raise ResearchError("idempotency_conflict")
            if str(row["id"]) == run_id:
                await conn.execute(
                    "INSERT INTO research.budgets(run_id,limits,reserved,used) VALUES(%s,%s,%s,%s)",
                    (run_id, J(effective_limits(request, config)), J({}), J({})),
                )
                await self.event(conn, run_id, "run.created")
        return row

    async def get_run(self, run_id):
        async with self.pool.connection() as conn:
            row = await one(
                conn, "SELECT * FROM research.runs WHERE id=%s AND owner_id=%s", (run_id, self.owner)
            )
            if not row:
                raise ResearchError("not_found")
            budget = await one(
                conn, "SELECT limits,reserved,used FROM research.budgets WHERE run_id=%s", (run_id,)
            )
            row["budget"] = budget
            return row

    async def list_runs(self, limit=50):
        async with self.pool.connection() as conn:
            return await all_rows(
                conn,
                "SELECT id,request->>'query' AS query,status,phase,created_at,finished_at FROM research.runs WHERE owner_id=%s ORDER BY created_at DESC LIMIT %s",
                (self.owner, limit),
            )

    async def claim(self, worker_id, lease_seconds=30):
        async with self.pool.connection() as conn:
            # A global advisory lock enforces the single concurrent run deployment.
            await conn.execute("SELECT pg_advisory_xact_lock(83469512)")
            if await one(
                conn,
                "SELECT id FROM research.runs WHERE owner_id=%s AND status IN ('running','recovering') AND lease_until>now() LIMIT 1",
                (self.owner,),
            ):
                return None
            row = await one(
                conn,
                "SELECT * FROM research.runs WHERE owner_id=%s AND (status='queued' OR (status IN ('running','recovering') AND lease_until<=now())) ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1",
                (self.owner,),
            )
            if not row:
                return None
            run_id = str(row["id"])
            resuming = row["started_at"] is not None
            budget = await one(conn, "SELECT * FROM research.budgets WHERE run_id=%s FOR UPDATE", (run_id,))
            # A crashed attempt has an unknown remote outcome: conservatively charge its full reservation.
            attempts = await all_rows(
                conn,
                "SELECT * FROM research.operation_attempts WHERE run_id=%s AND status='running' FOR UPDATE",
                (run_id,),
            )
            used = budget["used"]
            reserved = budget["reserved"]
            for attempt in attempts:
                for key, value in attempt["reservation"].items():
                    reserved[key] = max(0, reserved.get(key, 0) - value)
                    used[key] = used.get(key, 0) + value
                await conn.execute(
                    "UPDATE research.operation_attempts SET status='unknown',charged_tokens=reserved_tokens,charged=reservation,ended_at=now() WHERE id=%s",
                    (attempt["id"],),
                )
            if attempts:
                await conn.execute(
                    "UPDATE research.budgets SET used=%s,reserved=%s,updated_at=now() WHERE run_id=%s",
                    (J(used), J(reserved), run_id),
                )
                await conn.execute(
                    "UPDATE research.operations SET status='unknown',updated_at=now() WHERE run_id=%s AND status='running'",
                    (run_id,),
                )
            started = row["started_at"] or utcnow()
            deadline = row["deadline_at"] or started + timedelta(seconds=budget["limits"]["wall_seconds"])
            row = await one(
                conn,
                "UPDATE research.runs SET status=%s,started_at=%s,deadline_at=%s,lease_owner=%s,lease_until=now()+make_interval(secs=>%s),fence=fence+1 WHERE id=%s RETURNING *",
                (
                    "recovering" if resuming else "running",
                    started,
                    deadline,
                    worker_id,
                    lease_seconds,
                    run_id,
                ),
            )
            await self.event(
                conn,
                run_id,
                "checkpoint.resumed" if resuming else "run.started",
                {"unknown_attempts": len(attempts)},
            )
            return row

    async def heartbeat(self, run_id, fence, seconds=30):
        async with self.transaction(run_id, fence, allow_terminal=True) as conn:
            await conn.execute(
                "UPDATE research.runs SET lease_until=now()+make_interval(secs=>%s) WHERE id=%s",
                (seconds, run_id),
            )

    async def cancel(self, run_id):
        async with self.transaction(run_id) as conn:
            row = await one(conn, "SELECT * FROM research.runs WHERE id=%s", (run_id,))
            accepted = row["status"] not in TERMINAL
            if accepted:
                if row["status"] == "queued":
                    await conn.execute(
                        "UPDATE research.runs SET cancel_requested=true,status='cancelled',phase='cancelled',memory_status='skipped',stop_reason='cancelled',finished_at=now() WHERE id=%s",
                        (run_id,),
                    )
                    await self.event(conn, run_id, "run.finished", {"status": "cancelled"})
                else:
                    await conn.execute(
                        "UPDATE research.runs SET cancel_requested=true WHERE id=%s", (run_id,)
                    )
        return {**await self.get_run(run_id), "cancellation_accepted": accepted}

    async def phase(self, run_id, fence, phase):
        async with self.transaction(run_id, fence) as conn:
            await conn.execute("UPDATE research.runs SET phase=%s WHERE id=%s", (phase, run_id))

    async def warning(self, run_id, fence, code, details=None):
        async with self.transaction(run_id, fence) as conn:
            row = await one(conn, "SELECT warnings FROM research.runs WHERE id=%s", (run_id,))
            value = {"code": code, **(details or {})}
            if value not in row["warnings"]:
                await conn.execute(
                    "UPDATE research.runs SET warnings=%s WHERE id=%s", (J(row["warnings"] + [value]), run_id)
                )
                await self.event(conn, run_id, code, details)

    async def terminal(self, run_id, fence, status, reason, error=None):
        async with self.transaction(run_id, fence, allow_terminal=True) as conn:
            row = await one(conn, "SELECT status,cancel_requested FROM research.runs WHERE id=%s", (run_id,))
            if row["status"] in TERMINAL:
                return
            if row["cancel_requested"]:
                status = reason = "cancelled"
            await conn.execute(
                "UPDATE research.runs SET status=%s,phase=%s,stop_reason=%s,error=%s,finished_at=now(),memory_status='skipped' WHERE id=%s",
                (status, status, reason, J(error) if error else None, run_id),
            )
            if status == "cancelled":
                await conn.execute(
                    "UPDATE research.tasks SET status='cancelled',updated_at=now() WHERE run_id=%s AND status='active'",
                    (run_id,),
                )
            await self.event(conn, run_id, "run.finished", {"status": status, "stop_reason": reason})

    async def commit_value(self, run_id, fence, key, kind, value, task_id=None):
        op_id = stable_id("operation", run_id, key)
        hashed = digest(value)
        async with self.transaction(run_id, fence) as conn:
            previous = await one(
                conn, "SELECT * FROM research.operations WHERE run_id=%s AND operation_key=%s", (run_id, key)
            )
            if previous:
                if previous["input_hash"] != hashed or previous["result"] != value:
                    raise InvariantViolation("immutable operation changed")
                return previous["result"]
            await conn.execute(
                "INSERT INTO research.operations(id,run_id,operation_key,task_id,kind,input_hash,status,result,attempts) VALUES(%s,%s,%s,%s,%s,%s,'succeeded',%s,0)",
                (op_id, run_id, key, task_id, kind, hashed, J(value)),
            )
            if kind == "task":
                await conn.execute(
                    "UPDATE research.tasks SET snapshot=%s,status=%s,updated_at=now() WHERE run_id=%s AND id=%s",
                    (J(value), value["status"], run_id, task_id),
                )
                await self.event(
                    conn, run_id, "task.finished", {"task_id": task_id, "status": value["status"]}
                )
            elif kind == "round":
                await conn.execute(
                    "UPDATE research.tasks SET snapshot=%s,updated_at=now() WHERE run_id=%s AND id=%s",
                    (J(value), run_id, task_id),
                )
                await self.event(
                    conn, run_id, "task.round_finished", {"task_id": task_id, "round": value["round"]}
                )
            elif kind == "peer_message":
                await self.event(conn, run_id, "researcher.exchange", value)
            elif kind == "peer_consultation":
                await self.event(
                    conn,
                    run_id,
                    "researcher.consultation_finished",
                    {
                        "stage": value["stage"],
                        "status": value["status"],
                        "message_count": len(value["messages"]),
                        "pending_count": value.get("pending_count", 0),
                        "reason": value.get("reason"),
                    },
                )
            elif kind == "task_start":
                await conn.execute("UPDATE research.runs SET phase='research' WHERE id=%s", (run_id,))
                await self.event(conn, run_id, "task.started", {"task_id": task_id})
            return value

    async def saved_value(self, run_id, key):
        async with self.pool.connection() as conn:
            row = await one(
                conn,
                "SELECT o.result FROM research.operations o JOIN research.runs r ON r.id=o.run_id WHERE o.run_id=%s AND o.operation_key=%s AND o.status='succeeded' AND r.owner_id=%s",
                (run_id, key, self.owner),
            )
            return row["result"] if row else None

    async def save_plan(self, run_id, fence, plan):
        payload = plan.model_dump(mode="json")
        async with self.transaction(run_id, fence) as conn:
            previous = await one(
                conn,
                "SELECT payload FROM research.plans WHERE run_id=%s AND batch_no=%s",
                (run_id, plan.batch_no),
            )
            if previous:
                if previous["payload"] != payload:
                    raise InvariantViolation("plan changed on replay")
                return
            await conn.execute(
                "INSERT INTO research.plans(id,run_id,batch_no,payload) VALUES(%s,%s,%s,%s)",
                (plan.plan_id, run_id, plan.batch_no, J(payload)),
            )
            for task in plan.tasks:
                await conn.execute(
                    "INSERT INTO research.tasks(id,run_id,plan_id,specification,snapshot,status) VALUES(%s,%s,%s,%s,%s,'active')",
                    (task.task_id, run_id, plan.plan_id, J(task.model_dump(mode="json")), J({})),
                )
            await self.event(
                conn,
                run_id,
                "plan.created",
                {
                    "batch_no": plan.batch_no,
                    "tasks": [{"task_id": t.task_id, "question": t.question} for t in plan.tasks],
                },
            )

    async def events(self, run_id, after=0, limit=50):
        await self.get_run(run_id)
        async with self.pool.connection() as conn:
            return await all_rows(
                conn,
                "SELECT seq,event_type,payload,created_at FROM research.events WHERE run_id=%s AND seq>%s ORDER BY seq LIMIT %s",
                (run_id, after, limit),
            )
