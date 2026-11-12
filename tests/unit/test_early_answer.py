import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from research_agent.services.early_answer import write_preview
from research_agent.services.runtime import Services
from research_agent.source_contracts import SourceBlock, SourceComposition, SourceSection


@pytest.mark.asyncio
async def test_preview_is_stored_separately_and_replayed_without_another_model_call():
    composition = SourceComposition(
        title="Kev",
        sections=[
            SourceSection(
                heading="加速机制", paragraphs=[SourceBlock(text="融合减少中间搬运。", passage_ids=["P1"])]
            )
        ],
    )
    services = SimpleNamespace(
        run_id="run",
        fence=1,
        request=SimpleNamespace(query="介绍 Kev", output_language="zh-CN"),
        db=SimpleNamespace(saved_value=AsyncMock(return_value=None), commit_value=AsyncMock()),
        llm=SimpleNamespace(generate=AsyncMock(return_value=composition)),
        warn=AsyncMock(),
    )
    sources = [
        {
            "source_id": "source",
            "title": "Kev 笔记",
            "url": "https://readless.invalid/vault/source",
            "identity_context": "原始资料",
        }
    ]
    await write_preview(services, sources, {})
    saved = services.db.commit_value.call_args.args
    assert saved[2:4] == ("draft:preview:v1", "unverified_draft")
    assert saved[4]["sections"][0]["paragraphs"][0]["text"] == "融合减少中间搬运。"
    services.llm.generate.call_args.kwargs["validator"](composition)
    services.db.saved_value.return_value = saved[4]
    services.llm.generate.reset_mock()
    await write_preview(services, sources, {})
    services.llm.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_closing_services_cancels_an_unfinished_preview():
    services = Services.__new__(Services)
    services.preview_task = asyncio.create_task(asyncio.Event().wait())
    adapters = [SimpleNamespace(close=AsyncMock()) for _ in range(4)]
    services.llm, services.search, services.fetch, services.memory = adapters
    await services.close()
    assert services.preview_task.cancelled()
    for adapter in adapters:
        adapter.close.assert_awaited_once()
