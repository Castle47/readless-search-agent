import asyncio
import json
import signal
from uuid import uuid4
import psycopg
from ..config import Settings
from ..contracts import TERMINAL, NarrativeDraft
from ..errors import Stopped, Fenced, ResearchError
from ..storage.db import Database
from ..storage.checkpoint import saver
from ..services.runtime import Services
from ..graph.builder import ResearchGraph
from ..services.reports import digest_draft, render_report


async def execute(settings, db, run, checkpointer, services=None):
    if run["cancel_requested"]:
        await db.terminal(str(run["id"]), run["fence"], "cancelled", "cancelled")
        return
    try:
        services = services or Services(settings, db, run)
    except Exception:
        await db.terminal(
            str(run["id"]),
            run["fence"],
            "failed",
            "service_initialization_failed",
            {"code": "service_initialization_failed"},
        )
        return
    graph = ResearchGraph(services).compile(checkpointer)
    config = {"configurable": {"thread_id": str(run["id"])}, "max_concurrency": 3, "recursion_limit": 1000}
    prior = await graph.aget_state(config)
    if prior.values:
        services.understanding = prior.values.get("understanding", {})
        rewrite = prior.values.get("query_rewrite", {})
        services.search.private_scope = rewrite.get("scope") == "local"
        services.fetch.private_scope = services.search.private_scope
        services.search.public_queries = (
            set(rewrite.get("search_terms", [])) if prior.values.get("local_sources") else None
        )
    current = settings.snapshot()
    if (
        run["config"].get("prompt_version") != current["prompt_version"]
        or run["config"].get("adapter_version") != current["adapter_version"]
        or run["config"].get("model_id") != settings.model
        or run["config"].get("prompt_hash") != current["prompt_hash"]
    ):
        await db.terminal(
            services.run_id,
            services.fence,
            "failed",
            "incompatible_checkpoint",
            {"code": "incompatible_checkpoint"},
        )
        await services.close()
        return

    def incomplete(bundle, reasons):
        if services.config.get("source_first_workflow_version"):
            return NarrativeDraft(
                schema_version=3,
                title=services.request.query,
                sections=[],
                limitations=reasons,
                unresolved_questions=[services.request.query],
                quality_status="not_checked",
            )
        return digest_draft(services.request.query, bundle, reasons, services.request.output_language)

    async def work():
        try:
            if run["cancel_requested"]:
                raise Stopped("cancelled")
            from ..contracts import utcnow

            if run["deadline_at"] <= utcnow():
                bundle = await services.evidence.bundle()
                draft = incomplete(bundle, ["任务恢复时运行时限已到。"])
                report = render_report(services.run_id, draft, bundle, "partial", services.request)
                await services.evidence.finalize(report, [], settings.memory_scope, "skipped", "deadline")
                return
            initial = (
                None
                if prior.values
                else {
                    "schema_version": 1,
                    "run_id": services.run_id,
                    "request": run["request"],
                    "config_snapshot": run["config"],
                    "task_results": {},
                    "warnings": {},
                    "plan_history": [],
                    "batch_no": 0,
                    "stop_reason": None,
                }
            )
            await graph.ainvoke(initial, config, durability="sync")
        except Stopped as error:
            current = await db.get_run(services.run_id)
            if current["status"] in TERMINAL:
                return
            if error.code == "cancelled":
                await db.terminal(services.run_id, services.fence, "cancelled", "cancelled")
            else:
                bundle = await services.evidence.bundle()
                draft = incomplete(bundle, [error.code])
                await services.evidence.finalize(
                    render_report(services.run_id, draft, bundle, "partial", services.request),
                    [],
                    settings.memory_scope,
                    "skipped",
                    error.code,
                )
        except Fenced:
            raise
        except psycopg.Error:
            raise
        except Exception as error:
            code = error.code if isinstance(error, ResearchError) else "internal_error"
            # Never write provider bodies or exception strings (which may contain secrets).
            await db.terminal(
                services.run_id, services.fence, "failed", code, {"code": code, "type": type(error).__name__}
            )
            print(
                json.dumps(
                    {
                        "event": "run.failed",
                        "run_id": services.run_id,
                        "code": code,
                        "type": type(error).__name__,
                    }
                ),
                flush=True,
            )
            if not isinstance(error, ResearchError):
                import traceback

                # File/line/type only, excluding exception values and local variables.
                print(
                    json.dumps(
                        {
                            "event": "trace",
                            "frames": [
                                {"file": f.filename, "line": f.lineno, "function": f.name}
                                for f in traceback.extract_tb(error.__traceback__)
                            ],
                        }
                    ),
                    flush=True,
                )

    task = asyncio.create_task(work())

    async def watchdog():
        ticks = 0
        while not task.done():
            await asyncio.sleep(1)
            ticks += 1
            current = await db.get_run(services.run_id)
            if current["status"] in TERMINAL:
                return
            if current["fence"] != services.fence:
                task.cancel()
                raise Fenced()
            if current["cancel_requested"]:
                task.cancel()
                try:
                    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=5)
                except TimeoutError:
                    pass
                await db.terminal(services.run_id, services.fence, "cancelled", "cancelled")
                return
            if ticks % 10 == 0:
                await db.heartbeat(
                    services.run_id, services.fence, seconds=services.config["worker"]["lease_seconds"]
                )

    monitor = asyncio.create_task(watchdog())
    try:
        done, _ = await asyncio.wait([task, monitor], return_when=asyncio.FIRST_COMPLETED)
        for finished in done:
            await finished
        if not task.done():
            await task
    except asyncio.CancelledError:
        # A user cancellation can finish the graph task before the watchdog's
        # terminal transaction. Commit it before cancelling that watchdog.
        current = await db.get_run(services.run_id)
        if not current["cancel_requested"] or current["fence"] != services.fence:
            raise
        await db.terminal(services.run_id, services.fence, "cancelled", "cancelled")
    finally:
        monitor.cancel()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, monitor, return_exceptions=True)
        await services.close()


async def main_async():
    settings = Settings().refresh_secrets()
    db = Database(settings.database_url.get_secret_value())
    await db.open()
    worker_id = str(uuid4())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in [signal.SIGINT, signal.SIGTERM]:
        loop.add_signal_handler(sig, stop.set)
    try:
        async with saver(settings) as checkpointer:
            while not stop.is_set():
                try:
                    run = await db.claim(
                        worker_id, lease_seconds=settings.snapshot()["worker"]["lease_seconds"]
                    )
                    if run:
                        # Graceful shutdown cancels work; lease expires and a new worker resumes.
                        work = asyncio.create_task(execute(settings, db, run, checkpointer))
                        shutdown = asyncio.create_task(stop.wait())
                        done, _ = await asyncio.wait([work, shutdown], return_when=asyncio.FIRST_COMPLETED)
                        if shutdown in done and not work.done():
                            work.cancel()
                        await asyncio.gather(work, return_exceptions=False)
                        shutdown.cancel()
                    else:
                        try:
                            await asyncio.wait_for(stop.wait(), timeout=1)
                        except TimeoutError:
                            pass
                except (psycopg.Error, Fenced):
                    print(json.dumps({"event": "worker.infrastructure_wait"}), flush=True)
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=5)
                    except TimeoutError:
                        pass
    finally:
        await db.close()


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
