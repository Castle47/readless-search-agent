"""Explicit user-provided preferences; never mined from vault documents."""

import json

from ..adapters.letta import Memory
from ..contracts import MemoryItem, canonical, digest, utcnow
from ..storage.db import Database


async def import_preferences(settings, path):
    data = json.loads(path.read_text())
    if not isinstance(data.get("source"), str) or not data["source"].strip():
        raise ValueError("An explicit source is required")
    preferences = data["preferences"]
    if not isinstance(preferences, list) or not 1 <= len(preferences) <= 20:
        raise ValueError("Supply 1–20 explicit preferences")
    items = [
        MemoryItem(
            memory_id="preference-" + digest([data["source"], text])[:24],
            kind="lesson",
            text="用户声明的偏好（非事实证据）：" + text,
            topic_tags=["user-preference"],
            source_run_id="user-import-" + digest(data["source"])[:24],
            claim_ids=[],
            evidence_ids=[],
            created_at=utcnow(),
            expires_at=None,
            status="active",
            supersedes=[],
        )
        for text in preferences
    ]
    db = Database(settings.database_url.get_secret_value())
    await db.open()
    memory = Memory(settings, db)
    try:
        binding = await memory.provision()
        value = canonical(
            {
                "schema_version": 1,
                "scope": settings.memory_scope,
                "source": data["source"],
                "items": [item.model_dump(mode="json") for item in items],
            }
        )
        blocks = [b async for b in memory.client.agents.blocks.list(binding["letta_agent_id"])]
        existing = next((b for b in blocks if b.label == "user_preferences"), None)
        if existing:
            # Idempotent replacement of this explicit preference profile only.
            if json.loads(existing.value).get("source") != data["source"]:
                raise ValueError(
                    "Existing preferences have another source; merge explicitly before replacing"
                )
            await memory.client.blocks.update(existing.id, value=value)
            block_id = existing.id
        else:
            block = await memory.client.blocks.create(label="user_preferences", value=value, limit=20000)
            await memory.client.agents.blocks.attach(block.id, agent_id=binding["letta_agent_id"])
            block_id = block.id
        from ..storage.db import J

        async with db.pool.connection() as conn:
            await conn.execute(
                "UPDATE research.memory_bindings SET block_ids=%s,revision=revision+1 WHERE owner_id=%s AND scope=%s",
                (
                    J({**binding["block_ids"], "user_preferences": block_id}),
                    settings.owner_id,
                    settings.memory_scope,
                ),
            )
        return {"imported": len(items), "source": data["source"]}
    finally:
        await memory.close()
        await db.close()
