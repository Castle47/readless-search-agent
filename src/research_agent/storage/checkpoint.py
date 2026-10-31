from contextlib import asynccontextmanager
from psycopg.conninfo import make_conninfo
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver


def checkpoint_url(settings):
    return make_conninfo(
        (settings.checkpoint_url or settings.database_url).get_secret_value(),
        options="-c search_path=checkpoints",
    )


@asynccontextmanager
async def saver(settings):
    async with AsyncPostgresSaver.from_conn_string(checkpoint_url(settings)) as checkpointer:
        yield checkpointer


async def bootstrap(settings):
    async with saver(settings) as checkpointer:
        await checkpointer.setup()
