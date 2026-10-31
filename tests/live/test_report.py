"""Read-only live acceptance of an explicitly selected real report; no model regeneration."""

import os
import httpx
import pytest
from research_agent.config import Settings

pytestmark = pytest.mark.live


async def test_live_selected_report_trace_and_export():
    settings = Settings().refresh_secrets()
    if not settings.allow_live_tests:
        pytest.skip("Explicit live opt-in required")
    run_id = os.environ.get("READLESS_LIVE_RUN_ID")
    assert run_id, (
        "Set READLESS_LIVE_RUN_ID to a real report created through the UI or CLI; this test never creates a paid run implicitly."
    )
    async with httpx.AsyncClient(
        base_url=settings.api_url,
        headers={"Authorization": "Bearer " + settings.api_bearer_token.get_secret_value()},
        timeout=20,
    ) as client:
        result = await client.get("/v1/research/" + run_id + "/result")
        assert result.status_code == 200
        report = result.json()
        assert report["status"] in {"completed", "partial"} and report["citations"]
        markdown = await client.get("/v1/research/" + run_id + "/report.md")
        assert markdown.text == report["markdown"]
        bundle = (await client.get("/v1/research/" + run_id + "/evidence", params={"limit": 200})).json()
        evidence = {e["id"]: e for e in bundle["evidence"]}
        for citation in report["citations"]:
            entry = evidence[citation["evidence_id"]]
            source = (await client.get("/v1/research/" + run_id + "/sources/" + entry["source_id"])).json()
            assert source["content_text"][entry["start_char"] : entry["end_char"]] == entry["quote"]
        assert report["execution"]["model_id"] == "openai/gpt-5.6-luna"
        assert report["execution"]["search_calls"] >= 1
        assert report["execution"]["fetched_pages"] >= 1
