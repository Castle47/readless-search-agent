import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4
import psycopg
from psycopg import sql


def test_bootstrap_from_empty_database(settings):
    url = settings.database_url.get_secret_value()
    name = "readless_bootstrap_" + uuid4().hex
    parts = urlsplit(url)
    test_url = urlunsplit((parts.scheme, parts.netloc, "/" + name, parts.query, parts.fragment))
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        directory = Path(__file__).parents[2]
        result = subprocess.run(
            [str(directory / ".venv/bin/research-agent"), "bootstrap"],
            cwd=directory,
            env={**os.environ, "RA_DATABASE_URL": test_url},
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert result.returncode == 0, result.stderr
        with psycopg.connect(test_url) as conn:
            assert conn.execute("SELECT version_num FROM research.alembic_version").fetchone()[0] == "0003"
            assert conn.execute("SELECT count(*) FROM checkpoints.checkpoint_migrations").fetchone()[0] > 0
            assert (
                conn.execute(
                    "SELECT count(*) FROM information_schema.tables WHERE table_schema='research'"
                ).fetchone()[0]
                >= 19
            )
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
