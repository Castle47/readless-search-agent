from uuid import uuid4
import pytest
from psycopg.errors import ForeignKeyViolation
from research_agent.contracts import ResearchRequest, FetchResult, utcnow
from research_agent.storage.evidence import EvidenceStore
from research_agent.storage.db import J, one


async def test_database_rejects_cross_run_evidence_and_reports(db, settings):
    a = await db.create_run(ResearchRequest(query="Scope A"), str(uuid4()), settings.snapshot())
    b = await db.create_run(ResearchRequest(query="Scope B"), str(uuid4()), settings.snapshot())
    a_id, b_id = str(a["id"]), str(b["id"])
    text = "This is a source snapshot for cross-run database validation."
    import hashlib

    page = FetchResult(
        requested_url="https://example.org/a",
        canonical_url="https://example.org/a",
        title="Scope fixture",
        media_type="text/plain",
        text=text,
        content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        retrieved_at=utcnow(),
        published_at=None,
        publisher_key="example.org",
        source_type="official",
        metadata={},
    )
    sid = await EvidenceStore(db, a_id, None).source(page)
    with pytest.raises(ForeignKeyViolation):
        async with db.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO research.evidence(id,run_id,source_id,quote,start_char,end_char,directness,verified) VALUES(%s,%s,%s,'This',0,4,'direct',true)",
                (str(uuid4()), b_id, sid),
            )
    report_id = str(uuid4())
    async with db.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO research.reports(id,run_id,version,status,markdown,structured) VALUES(%s,%s,1,'partial','fixture',%s)",
            (report_id, a_id, J({})),
        )
    with pytest.raises(ForeignKeyViolation):
        async with db.pool.connection() as conn:
            await conn.execute("UPDATE research.runs SET report_id=%s WHERE id=%s", (report_id, b_id))
    async with db.pool.connection() as conn:
        row = await one(conn, "SELECT report_id FROM research.runs WHERE id=%s", (b_id,))
        assert row["report_id"] is None
    await db.cancel(a_id)
    await db.cancel(b_id)
