import asyncio
from ..contracts import ResearchRequest, canonical
from ..adapters.llm import LLM
from ..adapters.fetch import Fetcher
from ..adapters.searxng import Search
from ..adapters.letta import Memory
from ..storage.evidence import EvidenceStore
from .operations import Operations


class Services:
    def __init__(self, settings, db, run, *, llm=None, search=None, fetch=None, memory=None):
        self.settings = settings
        self.db = db
        self.run_id = str(run["id"])
        self.fence = run["fence"]
        self.config = run["config"]
        self.understanding = {}
        self.request = ResearchRequest.model_validate_json(canonical(run["request"]))
        self.ops = Operations(db, self.run_id, self.fence, self.config)
        self.llm = llm or LLM(settings, self.ops)
        self.search = search or Search(settings, self.ops, self.request)
        self.fetch = fetch or Fetcher(self.ops, self.request)
        self.memory = memory or Memory(settings, db)
        self.evidence = EvidenceStore(db, self.run_id, self.fence)

    async def warn(self, code, **details):
        await self.db.warning(self.run_id, self.fence, code, details)

    async def close(self):
        await asyncio.gather(
            *(
                adapter.close()
                for adapter in [self.llm, self.search, self.fetch, self.memory]
                if hasattr(adapter, "close")
            ),
            return_exceptions=True,
        )
