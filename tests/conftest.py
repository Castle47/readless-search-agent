import pytest
import pytest_asyncio
from research_agent.config import Settings
from research_agent.storage.db import Database


# Offline suite never uses production credentials or adapters implicitly.
@pytest.fixture
def settings(tmp_path):
    # Legacy regressions explicitly exercise the retained old pipeline.
    # Source-first tests opt into the production default separately.
    import yaml

    config = Settings().defaults()
    config["context_workflow_version"] = 0
    config["source_first_workflow_version"] = 0
    config["adaptive_research_workflow_version"] = 0
    # Preserve pre-014 snapshot budgets for recovery/rollover regressions.
    config["research"]["token_partitions"].update(research=244000, finalization=48000)
    config["research"]["finalization_seconds"] = 90
    config["model"]["finalization_split"].update(
        report_verifier=16000, repair_and_recheck=10000, quality=6000
    )
    config["model"]["roles"]["report_verifier"].update(max_input=6000, total_cap=16000)
    config["model"]["roles"]["editor"]["max_input"] = 12000
    config["model"]["roles"]["quality"]["total_cap"] = 6000
    defaults = tmp_path / "legacy-defaults.yaml"
    defaults.write_text(yaml.safe_dump(config))
    return Settings(
        model="openai/gpt-5.6-luna",
        defaults_file=defaults,
        secrets_file=tmp_path / "no-secrets.json",
        api_bearer_token="test-token",
        openrouter_api_key="sk-or-test-offline",
    )


@pytest_asyncio.fixture
async def db(settings):
    from uuid import uuid4

    database = Database(settings.database_url.get_secret_value(), owner="test-" + str(uuid4()))
    await database.open()
    yield database
    await database.close()
