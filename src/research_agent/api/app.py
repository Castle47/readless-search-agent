import base64
import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
from pydantic import ValidationError

from ..config import Settings
from ..contracts import TERMINAL, Report, ResearchRequest, canonical
from ..errors import ResearchError
from ..storage.db import Database, one
from ..storage.evidence import EvidenceStore

REQUESTS = Counter("readless_research_http_requests_total", "Research HTTP requests", ["method", "status"])
MESSAGES = {
    "not_found": "找不到此研究任务。",
    "idempotency_conflict": "同一请求编号对应了不同的问题。",
    "result_not_ready": "报告尚未生成。",
    "no_report": "此任务没有研究报告。",
    "unauthorized": "研究服务认证失败。",
    "invalid_request": "研究请求格式或限制不正确。",
    "setup_required": "请先配置 OpenRouter API key。",
    "invalid_idempotency_key": "缺少有效的请求编号。",
    "invalid_cursor": "分页位置无效。",
}


def public_run(row):
    return {
        "run_id": str(row["id"]),
        "status": row["status"],
        "phase": row["phase"],
        "coverage": row["coverage"],
        "budget": row.get("budget"),
        "warnings": row["warnings"],
        "memory_status": row["memory_status"],
        "report_id": str(row["report_id"]) if row["report_id"] else None,
        "stop_reason": row["stop_reason"],
        "error": row["error"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "deadline_at": row["deadline_at"],
        "cancel_requested": row["cancel_requested"],
        "model": row["config"]["model_id"],
        "reasoning_efforts": row["config"].get("reasoning_efforts"),
        "cost_usd": None,
    }


def create_app(settings=None, database=None):
    settings = settings or Settings()
    db = database or Database(settings.database_url.get_secret_value())

    @asynccontextmanager
    async def lifespan(app):
        await db.open()
        try:
            yield
        finally:
            await db.close()

    app = FastAPI(title="ReadLess Research Agent", version="0.1.0", lifespan=lifespan)
    app.state.db = db

    @app.middleware("http")
    async def request_context(request, call_next):
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        REQUESTS.labels(request.method, str(response.status_code)).inc()
        return response

    def failure(request, code, status, details=None):
        return JSONResponse(
            {
                "error": {
                    "code": code,
                    "message": MESSAGES.get(code, "研究服务暂时无法完成此操作。"),
                    "details": details or {},
                    "request_id": getattr(request.state, "request_id", str(uuid4())),
                }
            },
            status_code=status,
        )

    @app.exception_handler(ResearchError)
    async def known_error(request, error):
        code = error.code
        status = (
            404
            if code == "not_found"
            else 401
            if code == "unauthorized"
            else 503
            if code == "setup_required"
            else 422
            if code in {"invalid_request", "invalid_cursor", "invalid_idempotency_key"}
            else 409
        )
        return failure(request, code, status, error.details)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return failure(request, "invalid_request", 422)

    @app.exception_handler(Exception)
    async def internal_error(request, error):
        return failure(request, "internal_error", 500)

    bearer = HTTPBearer(auto_error=False)
    bearer_dependency = Depends(bearer)

    async def auth(request: Request, credentials: HTTPAuthorizationCredentials | None = bearer_dependency):
        settings.refresh_secrets()
        token = settings.api_bearer_token.get_secret_value()
        if (
            not token
            or credentials is None
            or credentials.scheme.lower() != "bearer"
            or not hmac.compare_digest(credentials.credentials.encode(), token.encode())
        ):
            raise ResearchError("unauthorized")

    async def get_run(run_id: str):
        try:
            UUID(run_id)
        except ValueError:
            raise ResearchError("not_found") from None
        return await db.get_run(run_id)

    def cursor_encode(run_id, kind, offset):
        text = canonical({"run_id": run_id, "kind": kind, "offset": offset})
        mac = hmac.new(
            settings.api_bearer_token.get_secret_value().encode(), text.encode(), hashlib.sha256
        ).hexdigest()
        return base64.urlsafe_b64encode((text + "." + mac).encode()).decode().rstrip("=")

    def cursor_decode(value, run_id, kind):
        if not value:
            return 0
        try:
            text, mac = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode().rsplit(".", 1)
            actual = hmac.new(
                settings.api_bearer_token.get_secret_value().encode(), text.encode(), hashlib.sha256
            ).hexdigest()
            data = json.loads(text)
            if (
                not hmac.compare_digest(mac, actual)
                or data["run_id"] != run_id
                or data["kind"] != kind
                or not isinstance(data["offset"], int)
                or data["offset"] < 0
            ):
                raise ValueError()
            return data["offset"]
        except (ValueError, TypeError, KeyError):
            raise ResearchError("invalid_cursor") from None

    @app.get("/healthz")
    async def health():
        return {"alive": True}

    @app.get("/readyz")
    async def ready():
        settings.refresh_secrets()
        try:
            config = settings.snapshot()
            async with db.pool.connection() as conn:
                version = await one(conn, "SELECT version_num FROM research.alembic_version")
                await conn.execute("SELECT 1 FROM checkpoints.checkpoint_migrations LIMIT 1")
            from pathlib import Path

            from alembic.config import Config
            from alembic.script import ScriptDirectory

            migrations = ScriptDirectory.from_config(
                Config(str(Path(__file__).resolve().parents[3] / "alembic.ini"))
            )
            if version["version_num"] != migrations.get_current_head():
                raise ValueError()
            if (
                not settings.openrouter_api_key.get_secret_value()
                or not settings.api_bearer_token.get_secret_value()
            ):
                return JSONResponse(
                    {"ready": False, "message": "请先保存 OpenRouter API key。"}, status_code=503
                )
            return {
                "ready": True,
                "message": "研究服务已就绪。",
                "model": settings.model,
                "reasoning_efforts": config["reasoning_efforts"],
            }
        except Exception:  # noqa: BLE001 - readiness must contain arbitrary database/driver failures.
            return JSONResponse({"ready": False, "message": "研究数据库或配置尚未就绪。"}, status_code=503)

    @app.get("/metrics", dependencies=[Depends(auth)])
    async def metrics():
        from ..observability.metrics import database_metrics

        return Response(generate_latest() + await database_metrics(db), media_type=CONTENT_TYPE_LATEST)

    @app.post(
        "/v1/research",
        dependencies=[Depends(auth)],
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ResearchRequest"}}},
            }
        },
    )
    async def create(request: Request):
        if not settings.refresh_secrets().openrouter_api_key.get_secret_value():
            raise ResearchError("setup_required")
        raw = await request.body()
        if len(raw) > 65536:
            raise ResearchError("invalid_request")
        try:
            value = ResearchRequest.model_validate_json(raw)
        except ValidationError:
            raise ResearchError("invalid_request") from None
        row = await db.create_run(value, request.headers.get("idempotency-key"), settings.snapshot())
        rid = str(row["id"])
        return JSONResponse(
            {
                "run_id": rid,
                "status": row["status"],
                "links": {
                    k: f"/v1/research/{rid}" + suffix
                    for k, suffix in [("status", ""), ("result", "/result"), ("report", "/report.md")]
                },
            },
            status_code=200 if row["status"] in TERMINAL else 202,
        )

    @app.get("/v1/research", dependencies=[Depends(auth)])
    async def list_runs(limit: int = Query(50, ge=1, le=200)):
        return {"runs": await db.list_runs(limit)}

    @app.get("/v1/research/{run_id}", dependencies=[Depends(auth)])
    async def status(run_id: str):
        result = public_run(await get_run(run_id))
        result["has_unverified_draft"] = (
            await db.saved_value(run_id, "draft:edited:v1") is not None
            or await db.saved_value(run_id, "draft:initial:v1") is not None
            or await db.saved_value(run_id, "draft:preview:v1") is not None
        )
        return result

    @app.get("/v1/research/{run_id}/draft", dependencies=[Depends(auth)])
    async def unverified_draft(run_id: str):
        await get_run(run_id)
        value = (
            await db.saved_value(run_id, "draft:edited:v1")
            or await db.saved_value(run_id, "draft:initial:v1")
            or await db.saved_value(run_id, "draft:preview:v1")
        )
        if value is None:
            raise ResearchError("not_found")
        return {"kind": "unverified_draft", "label": "尚未完成事实核验和质量验收", "draft": value}

    @app.get("/v1/research/{run_id}/result", dependencies=[Depends(auth)], response_model=Report)
    async def result(run_id: str):
        await get_run(run_id)
        return await EvidenceStore(db, run_id, None).report()

    @app.get("/v1/research/{run_id}/report.md", dependencies=[Depends(auth)])
    async def report(run_id: str):
        await get_run(run_id)
        value = await EvidenceStore(db, run_id, None).report()
        return Response(
            value.markdown,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="research-{run_id}.md"'},
        )

    @app.get("/v1/research/{run_id}/evidence", dependencies=[Depends(auth)])
    async def evidence(
        run_id: str,
        task_id: str | None = None,
        limit: int = Query(50, ge=1, le=200),
        cursor: str | None = None,
    ):
        await get_run(run_id)
        bundle = await EvidenceStore(db, run_id, None).bundle(task_id)
        offset = cursor_decode(cursor, run_id, "evidence:" + str(task_id))
        values = bundle["evidence"]
        bundle["evidence"] = values[offset : offset + limit]
        bundle["next_cursor"] = (
            cursor_encode(run_id, "evidence:" + str(task_id), offset + limit)
            if offset + limit < len(values)
            else None
        )
        return bundle

    @app.get("/v1/research/{run_id}/sources/{source_id}", dependencies=[Depends(auth)])
    async def source(run_id: str, source_id: str):
        await get_run(run_id)
        try:
            UUID(source_id)
        except ValueError:
            raise ResearchError("not_found") from None
        return await EvidenceStore(db, run_id, None).get_source(source_id)

    @app.get("/v1/research/{run_id}/events", dependencies=[Depends(auth)])
    async def events(
        run_id: str,
        after_seq: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=200),
        cursor: str | None = None,
    ):
        await get_run(run_id)
        after = cursor_decode(cursor, run_id, "events") if cursor else after_seq
        events = await db.events(run_id, after, limit)
        return {
            "events": events,
            "next_cursor": cursor_encode(run_id, "events", events[-1]["seq"])
            if len(events) == limit
            else None,
        }

    @app.post("/v1/research/{run_id}/cancel", dependencies=[Depends(auth)])
    async def cancel(run_id: str):
        await get_run(run_id)
        cancelled = await db.cancel(run_id)
        value = public_run(cancelled)
        from fastapi.encoders import jsonable_encoder

        return JSONResponse(
            jsonable_encoder(value), status_code=202 if cancelled["cancellation_accepted"] else 200
        )

    def openapi_schema():
        if app.openapi_schema:
            return app.openapi_schema
        from fastapi.openapi.utils import get_openapi

        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        request_schema = ResearchRequest.model_json_schema(ref_template="#/components/schemas/{model}")
        definitions = request_schema.pop("$defs", {})
        schema.setdefault("components", {}).setdefault("schemas", {}).update(definitions)
        schema["components"]["schemas"]["ResearchRequest"] = request_schema
        app.openapi_schema = schema
        return schema

    app.openapi = openapi_schema
    return app


app = create_app()
