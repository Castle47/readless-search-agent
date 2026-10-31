import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from research_agent.services import private_rag as rag
from research_agent.services.query_rewrite import ConversationSummary, QueryRewrite, rewrite, summarize


def test_private_export_links_open_original_vault_note():
    from research_agent.services.reports import source_link
    from urllib.parse import parse_qs, urlsplit
    url = source_link({"canonical_url": "https://readless.invalid/vault/id", "metadata": {"private": True, "path": "文件夹/含 空格.md"}})
    assert urlsplit(url).scheme == "obsidian"
    assert parse_qs(urlsplit(url).query)["file"] == ["文件夹/含 空格.md"]


@pytest.mark.asyncio
async def test_index_update_delete_and_search(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    note = root / "note.md"
    note.write_text("attention机制和Query改写")
    settings = SimpleNamespace(vault_path=root, rag_index=tmp_path / "index.sqlite")

    async def embedding(*args, **kwargs):
        return [[1.0] + [0.0] * 767 for _ in args[1]]

    monkeypatch.setattr(rag, "embed", embedding)
    report = await rag.index_vault(settings)
    assert report["files"] == 1 and report["chunks"] == 1
    assert (await rag.search(settings, "attention"))[0]["path"] == "note.md"
    note.write_text("changed revision")
    assert await rag.search(settings, "attention") == []
    await rag.index_vault(settings)
    assert (await rag.search(settings, "changed"))[0]["text"] == "changed revision"
    note.unlink()
    assert (await rag.index_vault(settings))["chunks"] == 0


@pytest.mark.asyncio
async def test_summary_is_generated_and_replayed():
    s = SimpleNamespace(
        request=SimpleNamespace(
            context=json.dumps(
                {
                    "format": "readless-conversation-v1",
                    "messages": [{"role": "user", "content": "研究Transformer"}],
                }
            )
        ),
        db=SimpleNamespace(saved_value=AsyncMock(return_value=None), commit_value=AsyncMock()),
        run_id="r",
        fence=1,
        llm=SimpleNamespace(
            generate=AsyncMock(
                return_value=ConversationSummary(
                    summary="用户关注Transformer", user_constraints=[], unresolved_questions=[]
                )
            )
        ),
    )
    state = await summarize(s)
    assert state["conversation_summary"]["message_count"] == 1
    s.db.saved_value.return_value = state["conversation_summary"]
    assert await summarize(s) == state
    assert s.llm.generate.await_count == 1


@pytest.mark.asyncio
async def test_rewrite_preserves_understanding_and_scope():
    s = SimpleNamespace(
        request=SimpleNamespace(query="我的笔记里怎么介绍它？", context="用户：Transformer"),
        llm=SimpleNamespace(
            generate=AsyncMock(
                return_value=QueryRewrite(
                    standalone_query="笔记中的Transformer",
                    local_query="Transformer",
                    search_terms=["Transformer", "Transformer"],
                    scope="local",
                )
            )
        ),
    )
    result = await rewrite(s, {"understanding": {"reader_goal": "理解机制"}})
    assert result["understanding"]["reader_goal"] == "理解机制"
    assert result["understanding"]["search_terms"] == ["Transformer"]
    assert result["query_rewrite"]["scope"] == "local"
