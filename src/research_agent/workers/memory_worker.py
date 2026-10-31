import asyncio
import json
from ..config import Settings
from ..storage.db import Database, one, J
from ..adapters.letta import Memory
from ..errors import ResearchError


async def tick(db, memory):
    # A single worker owns an advisory lock for the entire remote reconciliation operation.
    async with db.pool.connection() as conn:
        lock = await one(conn, "SELECT pg_try_advisory_lock(83469513) AS acquired")
        if not lock["acquired"]:
            return False
        try:
            async with conn.transaction():
                job = await one(
                    conn,
                    "SELECT * FROM research.memory_outbox WHERE status IN ('pending','processing','needs_reconciliation') AND next_attempt_at<=now() ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1",
                )
                if not job:
                    return False
                await conn.execute(
                    "UPDATE research.memory_outbox SET status='processing',attempts=attempts+1,next_attempt_at=now()+interval '60 seconds' WHERE id=%s",
                    (job["id"],),
                )
            await conn.commit()

            async def save_progress(value):
                async with db.pool.connection() as progress_conn:
                    await progress_conn.execute(
                        "UPDATE research.memory_outbox SET remote_ids=%s WHERE id=%s", (J(value), job["id"])
                    )

            try:
                receipt = await memory.reconcile_and_apply(job, save_progress)
                async with db.transaction(str(job["run_id"])) as tx:
                    await tx.execute(
                        "UPDATE research.memory_outbox SET status='applied',remote_ids=%s,error=NULL WHERE id=%s",
                        (J(receipt), job["id"]),
                    )
                    await tx.execute(
                        "UPDATE research.runs SET memory_status='applied' WHERE id=%s", (job["run_id"],)
                    )
                    await tx.execute(
                        "UPDATE research.memory_bindings SET revision=revision+1,updated_at=now() WHERE owner_id=%s AND scope=%s",
                        (db.owner, job["agent_scope"]),
                    )
                    await db.event(tx, str(job["run_id"]), "memory.applied")
            except Exception as error:
                unknown = isinstance(error, ResearchError) and error.code == "needs_reconciliation"
                attempts = job["attempts"] + 1
                status = (
                    "dead_letter"
                    if attempts >= (10 if unknown else 6)
                    else "needs_reconciliation"
                    if unknown
                    else "pending"
                )
                delay = 60 if unknown else [10, 60, 300, 1800, 3600][min(attempts - 1, 4)]
                code = error.code if isinstance(error, ResearchError) else "memory_unavailable"
                async with db.transaction(str(job["run_id"])) as tx:
                    await tx.execute(
                        "UPDATE research.memory_outbox SET status=%s,error=%s,next_attempt_at=now()+make_interval(secs=>%s) WHERE id=%s",
                        (status, J({"code": code}), delay, job["id"]),
                    )
                    await tx.execute(
                        "UPDATE research.runs SET memory_status=%s WHERE id=%s", (status, job["run_id"])
                    )
                    await db.event(tx, str(job["run_id"]), "memory." + status, {"code": code})
            return True
        finally:
            await conn.execute("SELECT pg_advisory_unlock(83469513)")
            await conn.commit()


async def main_async():
    settings = Settings().refresh_secrets()
    db = Database(settings.database_url.get_secret_value())
    await db.open()
    memory = Memory(settings, db)
    try:
        while True:
            try:
                await tick(db, memory)
            except Exception:
                print(json.dumps({"event": "memory_worker.infrastructure_wait"}), flush=True)
            await asyncio.sleep(2)
    finally:
        await memory.close()
        await db.close()


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
