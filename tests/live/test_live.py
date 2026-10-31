"""Explicit opt-in only. Never run this file to troubleshoot ordinary offline failures."""

import pytest
import httpx
from research_agent.config import Settings

pytestmark = pytest.mark.live


@pytest.fixture
def live_settings():
    settings = Settings().refresh_secrets()
    if not settings.allow_live_tests:
        pytest.skip("Set RA_ALLOW_LIVE_TESTS=true explicitly; external services and paid Luna required")
    assert settings.openrouter_api_key.get_secret_value(), "Configure the key via ReadLess research settings"
    return settings


async def test_live_service_configuration(live_settings):
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        response = await client.get(live_settings.api_url + "/readyz")
        assert response.status_code == 200, response.text
        response = await client.get(
            live_settings.searxng_url + "/search",
            params={"q": "Python official documentation list tuple", "format": "json"},
        )
        assert response.status_code == 200
        assert response.json().get("results")


async def test_live_letta_roundtrip(live_settings):
    from research_agent.storage.db import Database
    from research_agent.adapters.letta import Memory

    db = Database(live_settings.database_url.get_secret_value())
    await db.open()
    memory = Memory(live_settings, db)
    try:
        binding = await memory.binding()
        assert binding["letta_agent_id"]
        for label, bid in binding["block_ids"].items():
            block = await memory.client.blocks.retrieve(bid)
            assert block.label == label
        snapshot = await memory.recall("primary source documentation")
        assert snapshot.degraded is False
    finally:
        await memory.close()
        await db.close()
