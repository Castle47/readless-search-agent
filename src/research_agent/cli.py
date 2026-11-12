import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from uuid import uuid4

import httpx

from .config import Settings


def main():
    parser = argparse.ArgumentParser(prog="research-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--query", required=True)
    run.add_argument("--wait", action="store_true")
    run.add_argument("--output", type=Path)
    run.add_argument("--memory-mode", choices=["off", "read_only", "read_write"], default="read_write")
    for name in ["status", "cancel"]:
        sub.add_parser(name).add_argument("run_id")
    sub.add_parser("provision-memory")
    sub.add_parser("bootstrap")
    sub.add_parser("worker")
    sub.add_parser("memory-worker")
    sub.add_parser("api")
    vault = sub.add_parser("index-vault")
    vault.add_argument("--match", help="Index matching file names without removing other indexed notes")
    sub.add_parser("index-blogs")
    sub.add_parser("index-site")
    sub.add_parser("index-folder").add_argument("folder_id")
    memory_import = sub.add_parser("import-preferences")
    memory_import.add_argument("path", type=Path)
    args = parser.parse_args()
    settings = Settings().refresh_secrets()
    if args.command == "index-folder":
        from .services.folder_rag import index_folder

        print(json.dumps(asyncio.run(index_folder(settings, args.folder_id)), ensure_ascii=False))
        return
    if args.command == "index-site":
        from .services.site_rag import index_site

        print(json.dumps(asyncio.run(index_site(settings)), ensure_ascii=False))
        return
    if args.command == "index-blogs":
        from .services.readless_rag import index_blogs

        print(json.dumps(asyncio.run(index_blogs(settings)), ensure_ascii=False))
        return
    if args.command == "index-vault":
        from .services.private_rag import index_vault

        paths = (
            [p for p in settings.vault_path.rglob("*") if args.match.lower() in p.name.lower()]
            if args.match
            else None
        )
        print(json.dumps(asyncio.run(index_vault(settings, paths)), ensure_ascii=False))
        return
    if args.command == "import-preferences":
        from .services.user_preferences import import_preferences

        print(json.dumps(asyncio.run(import_preferences(settings, args.path)), ensure_ascii=False))
        return
    if args.command == "bootstrap":
        from alembic import command
        from alembic.config import Config

        from .storage.checkpoint import bootstrap

        command.upgrade(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head")
        asyncio.run(bootstrap(settings))
        return
    if args.command == "provision-memory":
        from .adapters.letta import Memory
        from .storage.db import Database

        async def provision():
            db = Database(settings.database_url.get_secret_value())
            await db.open()
            memory = Memory(settings, db)
            try:
                binding = await memory.provision()
                print(json.dumps({"agent_id": binding["letta_agent_id"], "scope": binding["scope"]}))
            finally:
                await memory.close()
                await db.close()

        asyncio.run(provision())
        return
    if args.command in {"worker", "memory-worker"}:
        if args.command == "worker":
            from .workers.research_worker import main as worker
        else:
            from .workers.memory_worker import main as worker
        worker()
        return
    if args.command == "api":
        import uvicorn

        uvicorn.run(
            "research_agent.api.app:app",
            host=os.getenv("RA_BIND_HOST", "127.0.0.1"),
            port=8000,
            access_log=False,
        )
        return
    with httpx.Client(
        base_url=settings.api_url,
        headers={"Authorization": "Bearer " + settings.api_bearer_token.get_secret_value()},
        timeout=20,
    ) as client:
        if args.command == "run":
            response = client.post(
                "/v1/research",
                headers={"Idempotency-Key": str(uuid4())},
                json={"query": args.query, "memory_mode": args.memory_mode},
            )
            response.raise_for_status()
            result = response.json()
            rid = result["run_id"]
            print(json.dumps(result, ensure_ascii=False))
            if not args.wait:
                return
            while result["status"] not in {"completed", "partial", "rejected", "failed", "cancelled"}:
                time.sleep(2)
                response = client.get("/v1/research/" + rid)
                response.raise_for_status()
                result = response.json()
            if args.output and result.get("report_id"):
                response = client.get("/v1/research/" + rid + "/report.md")
                response.raise_for_status()
                args.output.write_text(response.text)
            print(json.dumps(result, ensure_ascii=False))
            raise SystemExit(
                {"completed": 0, "partial": 2, "rejected": 3, "failed": 4, "cancelled": 130}[result["status"]]
            )
        response = (
            client.post("/v1/research/" + args.run_id + "/cancel", json={})
            if args.command == "cancel"
            else client.get("/v1/research/" + args.run_id)
        )
        response.raise_for_status()
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
