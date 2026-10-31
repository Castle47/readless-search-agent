from uuid import uuid4
import httpx
from research_agent.api.app import create_app


async def test_http_idempotency_auth_scope_and_export_errors(db, settings):
    app = create_app(settings, db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        assert (await client.get("/v1/research")).status_code == 401
        client.headers["Authorization"] = "Bearer test-token"
        key = str(uuid4())
        first = await client.post(
            "/v1/research",
            headers={"Idempotency-Key": key},
            json={"query": "API fixture", "memory_mode": "off"},
        )
        assert first.status_code == 202, first.text
        again = await client.post(
            "/v1/research",
            headers={"Idempotency-Key": key},
            json={"query": "API fixture", "memory_mode": "off"},
        )
        assert first.json()["run_id"] == again.json()["run_id"]
        rid = first.json()["run_id"]
        status = await client.get("/v1/research/" + rid)
        assert status.json()["status"] == "queued"
        assert (await client.get("/v1/research/" + rid + "/result")).json()["error"][
            "code"
        ] == "result_not_ready"
        assert (await client.get("/v1/research/" + rid + "/events?cursor=bad")).status_code == 422
        assert (await client.get("/v1/research/not-a-uuid")).status_code == 404
        assert (await client.post("/v1/research/" + rid + "/cancel")).status_code == 202
        assert (await client.post("/v1/research/" + rid + "/cancel")).status_code == 200
        assert (await client.get("/v1/research/" + rid + "/result")).json()["error"]["code"] == "no_report"
        assert "test-token" not in (await client.get("/readyz")).text


async def test_readiness_accepts_the_actual_migrated_head(db, settings):
    app = create_app(settings, db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        response = await client.get("/readyz")
        assert response.status_code == 200, response.text
        assert response.json()["ready"] is True


async def test_unverified_draft_is_explicit_and_owner_scoped(db, settings):
    from research_agent.contracts import ResearchRequest

    await db.create_run(
        ResearchRequest(query="Draft API fixture", memory_mode="off"), str(uuid4()), settings.snapshot()
    )
    run = await db.claim("draft-fixture")
    rid = str(run["id"])
    draft = {
        "draft_version": 1,
        "title": "Unverified",
        "sections": [
            {
                "heading": "",
                "paragraphs": [
                    {
                        "paragraph_id": "p1",
                        "text": "Draft text",
                        "block_type": "paragraph",
                        "language": None,
                        "illustrative": False,
                        "passage_ids": ["P0"],
                    }
                ],
            }
        ],
    }
    await db.commit_value(rid, run["fence"], "draft:initial:v1", "unverified_draft", draft)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings, db)), base_url="http://fixture"
    ) as c:
        assert (await c.get("/v1/research/" + rid + "/draft")).status_code == 401
        c.headers["Authorization"] = "Bearer test-token"
        value = (await c.get("/v1/research/" + rid + "/draft")).json()
        assert value["kind"] == "unverified_draft" and value["draft"] == draft
        assert (await c.get("/v1/research/" + rid)).json()["has_unverified_draft"]
        assert (await c.get("/v1/research/" + str(uuid4()) + "/draft")).status_code == 404
    await db.terminal(rid, run["fence"], "cancelled", "fixture_done")
