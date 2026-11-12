"""Transactional spend authorization and replay; adapters use this single retry boundary."""

import asyncio
import random
import weakref
from contextvars import ContextVar

from ..contracts import TERMINAL, digest, stable_id, utcnow
from ..errors import InvariantViolation, Stopped, ToolFailure
from ..storage.db import J, one

_NETWORK_LIMITS = weakref.WeakKeyDictionary()
_HAS_NETWORK_SLOT = ContextVar("research_network_slot", default=False)
FINALIZATION_TRANSFERS = {
    "writer": ("tokens_writer", "tokens_report_verifier", None),
    "report_verifier": ("tokens_report_verifier", "tokens_repair_and_recheck", "writer"),
    "quality": ("tokens_quality", "tokens_repair_and_recheck", "report_verifier"),
}


def protected_tokens(config, limits, used, reserved, partition, requested):
    """Protect existing phase allocations without increasing any limit."""
    if not config.get("reader_budget_workflow_version"):
        return 0
    if partition == "finalization" and config.get("shared_finalization_budget"):
        return 0
    keys = ["tokens_finalization", "tokens_memory"] if partition == "research" else []
    if partition == "finalization" and not requested.get("tokens_quality"):
        keys = ["tokens_quality"]
    return sum(max(0, limits[k] - used.get(k, 0) - reserved.get(k, 0)) for k in keys)


def closed_stage_key(stage):
    return "budget:finalization:closed:" + stage + ":v1"


def network_limit():
    loop = asyncio.get_running_loop()
    if loop not in _NETWORK_LIMITS:
        _NETWORK_LIMITS[loop] = asyncio.Semaphore(3)
    return _NETWORK_LIMITS[loop]


class Operations:
    def __init__(self, db, run_id, fence, config):
        self.db = db
        self.run_id = run_id
        self.fence = fence
        self.config = config
        self.locks = {}

    async def close_finalization_stage(self, stage):
        """Close a donor and transfer only its unused allocation, atomically.

        Used/unknown spend is retained; unsettled reservations prevent closing.
        The immutable marker makes replay after a crash/concurrent call a no-op.
        Total/partition limits and the recipient's per-request limits never grow.
        """
        if not self.config.get("finalization_rollover_version"):
            return None
        donor, recipient, prerequisite = FINALIZATION_TRANSFERS[stage]
        key = closed_stage_key(stage)
        async with self.db.transaction(self.run_id, self.fence) as conn:
            previous = await one(
                conn,
                "SELECT result FROM research.operations WHERE run_id=%s AND operation_key=%s",
                (self.run_id, key),
            )
            if previous:
                return previous["result"]
            if prerequisite and not await one(
                conn,
                "SELECT id FROM research.operations WHERE run_id=%s AND operation_key=%s",
                (self.run_id, closed_stage_key(prerequisite)),
            ):
                raise InvariantViolation("Finalization stages must close in order")
            budget = await one(
                conn, "SELECT * FROM research.budgets WHERE run_id=%s FOR UPDATE", (self.run_id,)
            )
            if budget["reserved"].get(donor, 0):
                raise InvariantViolation("Cannot close a stage with unsettled reservations")
            limits = dict(budget["limits"])
            amount = (
                0
                if self.config.get("shared_finalization_budget")
                else max(0, limits[donor] - budget["used"].get(donor, 0))
            )
            limits[donor] -= amount
            limits[recipient] += amount
            result = {
                "stage": stage,
                **({"shared_pool": True} if self.config.get("shared_finalization_budget") else {}),
                "from": donor,
                "to": recipient,
                "tokens": amount,
                "donor_limit": limits[donor],
                "recipient_limit": limits[recipient],
                "finalization_limit": limits["tokens_finalization"],
            }
            await conn.execute(
                "UPDATE research.budgets SET limits=%s,updated_at=now() WHERE run_id=%s",
                (J(limits), self.run_id),
            )
            await conn.execute(
                "INSERT INTO research.operations(id,run_id,operation_key,kind,input_hash,status,result,attempts) VALUES(%s,%s,%s,'budget_transfer',%s,'succeeded',%s,0)",
                (
                    stable_id("operation", self.run_id, key),
                    self.run_id,
                    key,
                    digest({"stage": stage, "version": 1}),
                    J(result),
                ),
            )
            await self.db.event(conn, self.run_id, "budget.finalization_transferred", result)
            return result

    async def guard(self, partition="research"):
        run = await self.db.get_run(self.run_id)
        if run["cancel_requested"] or run["status"] == "cancelled":
            raise Stopped("cancelled")
        if run["status"] in TERMINAL:
            raise Stopped(run["status"])
        from ..errors import Fenced

        if run["fence"] != self.fence or run["lease_until"] <= utcnow():
            raise Fenced()
        remaining = (run["deadline_at"] - utcnow()).total_seconds()
        if partition == "research":
            remaining -= min(
                self.config["research"]["finalization_seconds"], run["budget"]["limits"]["wall_seconds"] * 0.3
            )
        if remaining <= 0:
            raise Stopped("deadline")
        if any(w["code"] == "budget_anomaly" for w in run["warnings"]):
            raise Stopped("budget_exhausted")
        return remaining

    async def cached(self, key, input_hash):
        async with self.db.pool.connection() as conn:
            row = await one(
                conn,
                "SELECT * FROM research.operations WHERE run_id=%s AND operation_key=%s",
                (self.run_id, key),
            )
            if row and row["input_hash"] != input_hash:
                raise InvariantViolation("operation input changed on replay")
            return row

    async def research_headroom(self):
        """Advisory stage-entry check; reserve() remains the atomic spend gate."""
        run = await self.db.get_run(self.run_id)
        b = run["budget"]
        protected = protected_tokens(self.config, b["limits"], b["used"], b["reserved"], "research", {})
        available = [
            b["limits"]["tokens_research"]
            - b["used"].get("tokens_research", 0)
            - b["reserved"].get("tokens_research", 0),
            b["limits"]["total_tokens"]
            - b["used"].get("total_tokens", 0)
            - b["reserved"].get("total_tokens", 0)
            - protected,
        ]
        async with self.db.pool.connection() as conn:
            for field in ["validation_pool", "validation_proposal", "validation_batch", "validation_stage"]:
                pool = self.config.get(field)
                if not pool:
                    continue
                row = await one(
                    conn,
                    "SELECT COALESCE(sum(COALESCE((b.used->>'total_tokens')::bigint,0)+COALESCE((b.reserved->>'total_tokens')::bigint,0)),0) AS committed FROM research.budgets b JOIN research.runs r ON r.id=b.run_id WHERE r.config->%s->>'id'=%s",
                    (field, pool["id"]),
                )
                available.append(
                    pool["limit"] - pool.get("prior_tokens", 0) - int(row["committed"]) - protected
                )
        return max(0, min(available))

    async def reserve(self, key, kind, input_hash, requested, task_id, partition):
        await self.guard(partition)
        denied = False
        async with self.db.transaction(self.run_id, self.fence) as conn:
            run = await one(
                conn, "SELECT cancel_requested,deadline_at FROM research.runs WHERE id=%s", (self.run_id,)
            )
            if run["cancel_requested"]:
                raise Stopped("cancelled")
            row = await one(
                conn,
                "SELECT * FROM research.operations WHERE run_id=%s AND operation_key=%s FOR UPDATE",
                (self.run_id, key),
            )
            if row and row["input_hash"] != input_hash:
                raise InvariantViolation("operation input mismatch")
            if row and row["status"] == "succeeded":
                return {"cached": row["result"]}
            if row and row["attempts"] >= self.config["retry"]["max_attempts"]:
                raise ToolFailure("provider_unavailable")
            budget = await one(
                conn, "SELECT * FROM research.budgets WHERE run_id=%s FOR UPDATE", (self.run_id,)
            )
            used = budget["used"]
            reserved = budget["reserved"]
            limits = budget["limits"]
            requested = dict(requested)
            closed_donor = False
            quality_protected = False
            if self.config.get("finalization_rollover_version"):
                for stage, (donor, _, _) in FINALIZATION_TRANSFERS.items():
                    if requested.get(donor) and await one(
                        conn,
                        "SELECT id FROM research.operations WHERE run_id=%s AND operation_key=%s",
                        (self.run_id, closed_stage_key(stage)),
                    ):
                        closed_donor = True
                # Smaller request-level budgets still cannot spend the quality
                # reservation elsewhere. It is clipped by the existing limits.
                if (
                    requested.get("tokens_finalization")
                    and not requested.get("tokens_quality")
                    and not self.config.get("shared_finalization_budget")
                ):
                    protected = max(
                        0,
                        limits["tokens_quality"]
                        - used.get("tokens_quality", 0)
                        - reserved.get("tokens_quality", 0),
                    )
                    quality_protected = (
                        used.get("tokens_finalization", 0)
                        + reserved.get("tokens_finalization", 0)
                        + requested["tokens_finalization"]
                        + protected
                        > limits["tokens_finalization"]
                    )
            pool_denied = False
            protected = protected_tokens(self.config, limits, used, reserved, partition, requested)
            denied_pools = []
            # Global development grant, proposal grant, and this batch are
            # separate cumulative guards. Always acquire them in this order.
            for field in ["validation_pool", "validation_proposal", "validation_batch", "validation_stage"]:
                pool = self.config.get(field)
                if not pool or not requested.get("total_tokens"):
                    continue
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (pool["id"],))
                pooled = await one(
                    conn,
                    "SELECT COALESCE(sum(COALESCE((b.used->>'total_tokens')::bigint,0)+COALESCE((b.reserved->>'total_tokens')::bigint,0)),0) AS committed FROM research.budgets b JOIN research.runs r ON r.id=b.run_id WHERE r.config->%s->>'id'=%s",
                    (field, pool["id"]),
                )
                available = pool["limit"] - int(pooled["committed"]) - pool.get("prior_tokens", 0)
                if requested["total_tokens"] + protected > available:
                    pool_denied = True
                    denied_pools.append(
                        {
                            "scope": field,
                            "available": available,
                            "requested": requested["total_tokens"],
                            "protected": protected,
                        }
                    )
            phase_protected = bool(requested.get("total_tokens")) and (
                used.get("total_tokens", 0)
                + reserved.get("total_tokens", 0)
                + requested.get("total_tokens", 0)
                + protected
                > limits["total_tokens"]
            )
            if row:
                requested.pop("search_calls", None)
                requested.pop("fetched_pages", None)
            if (
                closed_donor
                or quality_protected
                or pool_denied
                or phase_protected
                or any(used.get(k, 0) + reserved.get(k, 0) + v > limits[k] for k, v in requested.items())
            ):
                denied = True
                await self.db.event(
                    conn,
                    self.run_id,
                    "budget.denied",
                    {
                        "kind": kind,
                        "partition": partition,
                        "validation_pool": pool_denied,
                        "closed_stage": closed_donor,
                        "quality_protected": quality_protected,
                        "phase_protected": phase_protected,
                        "protected_tokens": protected,
                        "denied_pools": denied_pools,
                    },
                )
            else:
                op_id = stable_id("operation", self.run_id, key)
                attempt = (row["attempts"] if row else 0) + 1
                if not row:
                    await conn.execute(
                        "INSERT INTO research.operations(id,run_id,operation_key,task_id,kind,input_hash,status,attempts) VALUES(%s,%s,%s,%s,%s,%s,'running',%s)",
                        (op_id, self.run_id, key, task_id, kind, input_hash, attempt),
                    )
                else:
                    await conn.execute(
                        "UPDATE research.operations SET status='running',attempts=%s,updated_at=now() WHERE id=%s",
                        (attempt, op_id),
                    )
                # Logical quotas are spent once on admission, even when transport fails.
                charged = {k: requested.pop(k) for k in ["search_calls", "fetched_pages"] if k in requested}
                for k, v in charged.items():
                    used[k] = used.get(k, 0) + v
                for k, v in requested.items():
                    reserved[k] = reserved.get(k, 0) + v
                attempt_id = stable_id("attempt", op_id, attempt)
                await conn.execute(
                    "INSERT INTO research.operation_attempts(id,run_id,operation_id,attempt_no,status,reserved_tokens,charged_tokens,reservation,charged) VALUES(%s,%s,%s,%s,'running',%s,0,%s,%s)",
                    (
                        attempt_id,
                        self.run_id,
                        op_id,
                        attempt,
                        requested.get("total_tokens", 0),
                        J(requested),
                        J(charged),
                    ),
                )
                await conn.execute(
                    "UPDATE research.budgets SET used=%s,reserved=%s,updated_at=now() WHERE run_id=%s",
                    (J(used), J(reserved), self.run_id),
                )
        if denied:
            raise Stopped("budget_exhausted")
        return {"id": attempt_id, "op_id": op_id, "reservation": requested, "attempt": attempt}

    async def settle(self, attempt, result=None, actual=None, error=None, unknown=False, provider_id=None):
        async with self.db.transaction(self.run_id, self.fence) as conn:
            stored = await one(
                conn,
                "SELECT * FROM research.operation_attempts WHERE id=%s AND run_id=%s FOR UPDATE",
                (attempt["id"], self.run_id),
            )
            if stored["status"] != "running":
                raise InvariantViolation("attempt already settled")
            budget = await one(
                conn, "SELECT * FROM research.budgets WHERE run_id=%s FOR UPDATE", (self.run_id,)
            )
            reserved = budget["reserved"]
            used = budget["used"]
            charge = actual if actual is not None else attempt["reservation"]
            for k, v in attempt["reservation"].items():
                reserved[k] = max(0, reserved.get(k, 0) - v)
            for k, v in charge.items():
                used[k] = used.get(k, 0) + v
            anomaly = any(v > attempt["reservation"].get(k, 0) for k, v in charge.items())
            status = "unknown" if unknown else "failed" if error else "succeeded"
            await conn.execute(
                "UPDATE research.operation_attempts SET status=%s,charged_tokens=%s,charged=%s,ended_at=now(),provider_request_id=%s,error=%s WHERE id=%s",
                (
                    status,
                    charge.get("total_tokens", 0),
                    J({**stored["charged"], **charge}),
                    provider_id,
                    J(error) if error else None,
                    attempt["id"],
                ),
            )
            await conn.execute(
                "UPDATE research.operations SET status=%s,result=%s,error=%s,updated_at=now() WHERE id=%s",
                (
                    status,
                    J(result) if result is not None else None,
                    J(error) if error else None,
                    attempt["op_id"],
                ),
            )
            await conn.execute(
                "UPDATE research.budgets SET used=%s,reserved=%s,updated_at=now() WHERE run_id=%s",
                (J(used), J(reserved), self.run_id),
            )
            if anomaly:
                run = await one(conn, "SELECT warnings FROM research.runs WHERE id=%s", (self.run_id,))
                await conn.execute(
                    "UPDATE research.runs SET warnings=%s WHERE id=%s",
                    (J(run["warnings"] + [{"code": "budget_anomaly"}]), self.run_id),
                )
                await self.db.event(conn, self.run_id, "budget_anomaly")
            if not error:
                await self.db.event(
                    conn,
                    self.run_id,
                    "operation.finished",
                    {"operation_id": attempt["op_id"], "tokens": charge.get("total_tokens", 0)},
                )

    async def monitored(self, call, timeout, partition):
        async def bounded():
            if _HAS_NETWORK_SLOT.get():
                return await call(min(timeout, await self.guard(partition)))
            async with network_limit():
                token = _HAS_NETWORK_SLOT.set(True)
                try:
                    remaining = await self.guard(partition)
                    return await call(min(timeout, remaining))
                finally:
                    _HAS_NETWORK_SLOT.reset(token)

        task = asyncio.create_task(bounded())
        try:
            while True:
                done, _ = await asyncio.wait([task], timeout=1)
                if done:
                    return await task
                await self.guard(partition)
        finally:
            if not task.done():
                task.cancel()
            try:
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=2)
            except TimeoutError:
                pass

    async def run(
        self,
        key,
        kind,
        payload,
        call,
        *,
        task_id=None,
        partition="research",
        tokens=0,
        timeout=20,
        model_role=None,
        is_recheck=False,
    ):
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            hashed = digest(payload)
            row = await self.cached(key, hashed)
            if row and row["status"] == "succeeded":
                return row["result"]
            if row and row["status"] == "failed" and not (row["error"] or {}).get("retryable"):
                raise ToolFailure(
                    (row["error"] or {}).get("code", "provider_unavailable"),
                    fatal=(row["error"] or {}).get("fatal", False),
                )
            while True:
                remaining = await self.guard(partition)
                requested = {}
                if kind == "search":
                    requested = {"search_calls": 1, "search_http_attempts": 1}
                elif kind == "fetch":
                    requested = {"fetched_pages": 1, "fetch_http_attempts": 1}
                elif kind == "fetch_aux":
                    requested = {"fetch_http_attempts": 1}
                elif kind == "llm":
                    requested = {
                        "llm_http_attempts": 1,
                        "total_tokens": tokens,
                        "tokens_" + partition: tokens,
                    }
                    if model_role == "report_verifier" and not is_recheck:
                        requested["tokens_report_verifier"] = tokens
                    if model_role in {"writer", "quality"}:
                        requested["tokens_" + model_role] = tokens
                    if model_role in {"repair", "editor"} or is_recheck:
                        requested["tokens_repair_and_recheck"] = tokens
                    if model_role in {"peer_questioner", "peer_responder"}:
                        if not self.config.get("peer_review_workflow_version"):
                            raise ToolFailure("peer_review_disabled")
                        requested["tokens_peer_review"] = tokens
                        requested["peer_review_http_attempts"] = 1
                attempt = await self.reserve(key, kind, hashed, requested, task_id, partition)
                if "cached" in attempt:
                    return attempt["cached"]
                try:
                    result = await self.monitored(call, min(timeout, remaining), partition)
                    actual = dict(attempt["reservation"])
                    if kind == "llm":
                        actual["total_tokens"] = result["usage"]["total_tokens"]
                        for name in actual:
                            if name.startswith("tokens_"):
                                actual[name] = actual["total_tokens"]
                    await self.settle(
                        attempt,
                        result,
                        actual=actual,
                        provider_id=result.get("provider_request_id") if isinstance(result, dict) else None,
                    )
                    return result
                except ToolFailure as error:
                    # Non-response timeouts have unknown cost. Known HTTP failures still consume attempts,
                    # but not model tokens when the provider explicitly rejected the request.
                    charge = dict(attempt["reservation"])
                    if error.code in {
                        "http_400",
                        "http_401",
                        "http_403",
                        "http_404",
                        "http_410",
                        "http_422",
                        "http_429",
                    }:
                        for k in list(charge):
                            if k == "total_tokens" or k.startswith("tokens_"):
                                charge[k] = 0
                    await self.settle(
                        attempt,
                        error={"code": error.code, "retryable": error.retryable, "fatal": error.fatal},
                        unknown=error.code in {"timeout", "network_error", "http_408"}
                        or error.code.startswith("http_5"),
                        actual=charge,
                    )
                    if (
                        error.fatal
                        or not error.retryable
                        or attempt["attempt"] >= self.config["retry"]["max_attempts"]
                    ):
                        raise
                    delay = (
                        min(
                            error.retry_after or 2 ** (attempt["attempt"] - 1),
                            self.config["retry"]["retry_after_cap_seconds"],
                        )
                        + random.random() * 0.25
                    )
                    if delay >= await self.guard(partition):
                        raise Stopped("deadline")
                    await asyncio.sleep(delay)
                except (Stopped, asyncio.CancelledError):
                    # Do not release possibly charged spend. Stale worker cannot settle a newer lease.
                    await self.settle(attempt, error={"code": "interrupted"}, unknown=True)
                    raise
