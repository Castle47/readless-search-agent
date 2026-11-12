from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .contracts import digest
from .model_policy import DEFAULT_MODEL, ModelId

ROOT = Path(__file__).resolve().parents[2]
DEFAULTS = Path(__file__).resolve().parents[2] / "configs/default.yaml"
MODEL = DEFAULT_MODEL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="forbid", env_prefix="RA_")
    database_url: SecretStr = SecretStr("postgresql://research:research-local@127.0.0.1:55432/research")
    checkpoint_url: SecretStr | None = None
    api_bearer_token: SecretStr = SecretStr("")
    openrouter_api_key: SecretStr = SecretStr("")
    model: ModelId = MODEL
    model_endpoint: str = "https://openrouter.ai/api/v1"
    tokenizer: str = "o200k_base"
    web_dns_endpoint: Literal["system", "https://cloudflare-dns.com/dns-query"] = "system"
    searxng_url: str = "http://127.0.0.1:8088"
    letta_url: str = "http://127.0.0.1:8283"
    letta_token: SecretStr = SecretStr("")
    letta_agent_id: str | None = None
    secrets_file: Path = ROOT / ".secrets/secrets.json"
    defaults_file: Path = DEFAULTS
    api_url: str = "http://127.0.0.1:8000"
    owner_id: Literal["local"] = "local"
    memory_scope: str = "research-general"
    allow_live_tests: bool = False
    site_sources_path: Path = Path("/knowledge/readless-site")
    user_folders_path: Path = Path("/knowledge/user-folders")
    vault_path: Path = Path("/knowledge/obsidian")
    readless_cloud_path: Path = Path("/knowledge/readless-cloud-articles")
    readless_articles_path: Path = Path("/knowledge/readless-articles")
    rag_index: Path = Path("/run/readless-index/vault.sqlite")
    embedding_url: str = "http://embeddings:11434"

    @model_validator(mode="after")
    def endpoints(self):
        from urllib.parse import urlsplit

        for endpoint in [self.model_endpoint, self.searxng_url, self.letta_url]:
            parsed = urlsplit(endpoint)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError("invalid service endpoint")
        if self.model_endpoint != "https://openrouter.ai/api/v1":
            raise ValueError("This deployment only authorizes the OpenRouter endpoint")
        return self

    def refresh_secrets(self):
        try:
            data = json.loads(self.secrets_file.read_text())
        except FileNotFoundError:
            return self
        for name in ["openrouter_api_key", "api_bearer_token", "letta_token"]:
            if data.get(name):
                setattr(self, name, SecretStr(data[name]))
        return self

    def defaults(self):
        config = yaml.safe_load(self.defaults_file.read_text())
        validate_defaults(config)
        return config

    def snapshot(self):
        from .adapters.llm import EFFORT, ROLE_INSTRUCTIONS, SYSTEM
        from .adaptive_prompts import ADAPTIVE_ROLE_INSTRUCTIONS
        from .peer_contracts import CONTEXT_INSTRUCTIONS as PEER_CONTEXT_INSTRUCTIONS
        from .source_prompts import SOURCE_ROLE_INSTRUCTIONS

        if set(EFFORT) != set(ROLE_INSTRUCTIONS) or set(EFFORT.values()) - {"none", "low", "medium"}:
            raise ValueError("Invalid role reasoning configuration")
        config = self.defaults()
        astra_profile = config.pop("astra_profile", {})
        adaptive = bool(
            config.get("adaptive_research_workflow_version") and config.get("source_first_workflow_version")
        )
        profile = config.pop("adaptive_profile", {})
        if adaptive:

            def merge(target, patch):
                for key, value in patch.items():
                    if isinstance(value, dict) and isinstance(target.get(key), dict):
                        merge(target[key], value)
                    else:
                        target[key] = value

            merge(config, profile)
            validate_defaults(config)
        if self.model == "openai/gpt-6-astra":
            # Model-specific capacity includes internal reasoning, not a prose target.
            config["model"]["timeout_seconds"] = astra_profile.get("timeout_seconds", 300)
            for role, cap in config["model"]["roles"].items():
                cap["max_output"] = max(cap["max_output"], astra_profile.get("min_output", 8000))
                cap["max_output"] = astra_profile.get("role_outputs", {}).get(role, cap["max_output"])
            config["research"].update(astra_profile.get("research", {}))
            validate_defaults(config)
        if self.model == "openai/gpt-6-luna":
            config["model"]["timeout_seconds"] = 180
            for role, cap in config["model"]["roles"].items():
                cap["max_output"] = max(
                    cap["max_output"], 16000 if role in {"writer", "editor", "report_verifier"} else 8000
                )
        efforts = (
            {role: "high" for role in EFFORT}
            if self.model in {"openai/gpt-6-astra", "openai/gpt-6-luna"}
            else dict(EFFORT)
        )
        config["adaptive_research_workflow_version"] = int(adaptive)
        config["peer_review_workflow_version"] = int(
            adaptive and bool(config.get("peer_review_workflow_version"))
        )
        config["model"]["temperature"] = None
        config.update(
            schema_version=1,
            model_id=self.model,
            tokenizer=self.tokenizer,
            prompt_version="2",
            adapter_version="52",
            reasoning_efforts=efforts,
            retrieval_workflow_version=2,
            early_draft_version=1,
            reader_budget_workflow_version=int(bool(config.get("source_first_workflow_version"))),
            editorial_workflow_version=1,
            finalization_rollover_version=1,
            owner_id=self.owner_id,
            web_dns_endpoint=self.web_dns_endpoint,
        )
        config["prompt_hash"] = digest(
            {
                "system": SYSTEM,
                "roles": ROLE_INSTRUCTIONS,
                "source_roles": SOURCE_ROLE_INSTRUCTIONS,
                "adaptive_roles": ADAPTIVE_ROLE_INSTRUCTIONS,
                "peer_context": PEER_CONTEXT_INSTRUCTIONS if config["peer_review_workflow_version"] else {},
                "efforts": efforts,
            }
        )
        config["config_hash"] = digest(config)
        return config


def validate_defaults(config):
    r = config["research"]
    w = config["worker"]
    if r["max_supplemental_batches"] != 1 or not 1 <= r["parallel_researchers"] <= 3:
        raise ValueError("invalid graph limits")
    if config.get("peer_review_workflow_version"):
        peer = config["peer_review"]
        if not (
            1 <= peer["max_rounds"] <= 2
            and 2 <= peer["max_participants"] <= 3
            and 1 <= peer["max_questions_per_round"] <= 2
            and 0 <= peer["max_post_write_questions"] <= 2
            and 1 <= peer["max_http_attempts"] <= 16
            and 0 < peer["token_cap"] <= 48000
            and peer["min_seconds_remaining"] >= 30
        ):
            raise ValueError("invalid peer consultation limits")
    if sum(r["token_partitions"].values()) != r["total_tokens"]:
        raise ValueError("token partitions must match total")
    if w["lease_seconds"] <= w["heartbeat_seconds"]:
        raise ValueError("lease must exceed heartbeat")
    if config["model"]["finalization_split"]["report_verifier"] > r["token_partitions"]["finalization"]:
        raise ValueError("invalid finalization budget")
    for cap in config["model"]["roles"].values():
        if cap["max_input"] + cap["max_output"] > 1050000:
            raise ValueError("model context exceeded")


def effective_limits(request, config):
    defaults = config["research"]
    limits = {
        key: defaults[key]
        for key in [
            "search_calls",
            "fetched_pages",
            "search_http_attempts",
            "fetch_http_attempts",
            "llm_http_attempts",
            "total_tokens",
            "wall_seconds",
        ]
    }
    limits.update(request.limits.model_dump(exclude_none=True))
    total = limits["total_tokens"]
    # Reduced user budgets retain independent finalization/memory reservations.
    partitions = defaults["token_partitions"]
    finalization = min(
        partitions["finalization"], max(4000, total * partitions["finalization"] // defaults["total_tokens"])
    )
    memory = min(partitions["memory"], total * partitions["memory"] // defaults["total_tokens"])
    limits.update(
        tokens_research=total - finalization - memory, tokens_finalization=finalization, tokens_memory=memory
    )
    if config.get("peer_review_workflow_version"):
        limits["tokens_peer_review"] = min(config["peer_review"]["token_cap"], total)
        limits["peer_review_http_attempts"] = config["peer_review"]["max_http_attempts"]
    limits.update(
        {
            "tokens_" + role: finalization
            if config.get("shared_finalization_budget")
            else min(cap, finalization)
            for role, cap in config["model"]["finalization_split"].items()
        }
    )
    return limits
