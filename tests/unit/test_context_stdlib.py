"""Offline regressions runnable with only production dependencies."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from research_agent.adapters.searxng import Search
from research_agent.services import private_rag as rag
from research_agent.services.query_rewrite import ConversationSummary, QueryRewrite, rewrite, summarize


class ContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_index_revision_deletion_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "vault"
            root.mkdir()
            note = root / "note.md"
            note.write_text("attention机制和Query改写")
            outside = Path(tmp) / "private.md"
            outside.write_text("excluded")
            (root / "link.md").symlink_to(outside)
            s = SimpleNamespace(vault_path=root, rag_index=Path(tmp) / "index.sqlite")

            async def embed(*args, **kwargs):
                return [[1.0] + [0.0] * 767 for _ in args[1]]

            with patch.object(rag, "embed", embed):
                report = await rag.index_vault(s)
                self.assertEqual(report["files"], 1)
                self.assertEqual((await rag.search(s, "attention"))[0]["path"], "note.md")
                note.write_text("new revision")
                self.assertEqual(await rag.search(s, "attention"), [])
                await rag.index_vault(s)
                self.assertEqual((await rag.search(s, "revision"))[0]["text"], "new revision")
                note.unlink()
                self.assertEqual((await rag.index_vault(s))["chunks"], 0)

    async def test_generated_summary_replay(self):
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
        result = await summarize(s)
        self.assertEqual(result["conversation_summary"]["message_count"], 1)
        s.db.saved_value.return_value = result["conversation_summary"]
        self.assertEqual(await summarize(s), result)
        self.assertEqual(s.llm.generate.await_count, 1)

    async def test_rewrite(self):
        s = SimpleNamespace(
            request=SimpleNamespace(query="我的笔记怎么介绍它？", context="用户：Transformer"),
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
        self.assertEqual(result["understanding"]["reader_goal"], "理解机制")
        self.assertEqual(result["understanding"]["search_terms"], ["Transformer"])

    async def test_private_search_guard(self):
        search = object.__new__(Search)
        search.private_scope = True
        self.assertEqual(await search.search("secret", "en"), [])
        search.private_scope = False
        search.public_queries = {"approved public topic"}
        self.assertEqual(await search.search("secret", "en"), [])


if __name__ == "__main__":
    unittest.main()
