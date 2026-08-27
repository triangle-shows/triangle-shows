"""
Integration cover for the concurrent-migration race (review item #7).

Migrations run from the FastAPI lifespan handler rather than as a deploy step, so two
Cloud Run containers booting together both call `upgrade head` against the same
database. Before the advisory lock in alembic/env.py, the loser of that race failed on
an object the winner had just created and never became healthy.

These tests need a real database — they create and drop a scratch one — and skip
cleanly when there isn't a reachable server, so a local `pytest` without Postgres
still passes. CI stands up a Postgres service container, so they do run there.

Note this is a race, so the concurrent test is not guaranteed to fail if the lock is
removed — it failed on every one of several attempts, but timing decides. What it does
guarantee is the other direction: that the lock never makes concurrent upgrades fail.
"""

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

BACKEND = Path(__file__).resolve().parent.parent
SCRATCH_DB = "alembic_race_test"
RUNNERS = 3


# --- Helpers ---

def _sync_url(url: str) -> str:
    """Convert to a sync driver URL for psycopg2's own use here.

    DATABASE_URL itself must keep the `+asyncpg` form when handed to a subprocess:
    alembic/env.py imports app.database, which builds an async engine from it and
    rejects a sync driver. env.py does its own conversion for alembic internally.
    """
    return url.replace("postgresql+asyncpg://", "postgresql://").replace(
        "ssl=require", "sslmode=require"
    )


def _with_database(url: str, dbname: str) -> str:
    return urlunsplit(urlsplit(url)._replace(path=f"/{dbname}"))


def _alembic(url: str, *args: str) -> subprocess.CompletedProcess:
    """Run the alembic CLI the way the app and CI do, against `url`."""
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.fixture
def scratch_db():
    """Yield a URL for an empty throwaway database, dropped afterwards."""
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not set")

    try:
        import psycopg2
    except ImportError:
        pytest.skip("psycopg2 is not installed")

    admin_url = _with_database(_sync_url(url), "postgres")
    try:
        conn = psycopg2.connect(admin_url, connect_timeout=5)
    except Exception as exc:  # no server, wrong credentials, not listening
        pytest.skip(f"no reachable database: {exc}")

    conn.autocommit = True  # CREATE/DROP DATABASE cannot run inside a transaction
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)")
            cur.execute(f"CREATE DATABASE {SCRATCH_DB}")
        # Async form on purpose — this is passed through as DATABASE_URL.
        yield _with_database(url, SCRATCH_DB)
    finally:
        with conn.cursor() as cur:
            # FORCE so a leaked connection from a failed run cannot block cleanup.
            cur.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)")
        conn.close()


def _describe(results) -> str:
    return "\n\n".join(
        f"--- runner {i} (exit {r.returncode}) ---\n{r.stdout}\n{r.stderr}"
        for i, r in enumerate(results)
    )


# --- Tests ---

def test_concurrent_upgrades_all_succeed(scratch_db):
    """The regression: without the lock, one runner dies on the winner's objects."""
    with ThreadPoolExecutor(max_workers=RUNNERS) as pool:
        results = [f.result() for f in
                   [pool.submit(_alembic, scratch_db, "upgrade", "head")
                    for _ in range(RUNNERS)]]

    failed = [i for i, r in enumerate(results) if r.returncode != 0]
    assert not failed, f"runners {failed} failed:\n{_describe(results)}"


def test_concurrent_upgrades_leave_the_schema_at_head(scratch_db):
    """Serializing must still apply the migrations, not just avoid the crash."""
    with ThreadPoolExecutor(max_workers=RUNNERS) as pool:
        [f.result() for f in
         [pool.submit(_alembic, scratch_db, "upgrade", "head") for _ in range(RUNNERS)]]

    current = _alembic(scratch_db, "current")
    assert current.returncode == 0, current.stderr
    assert "(head)" in current.stdout, f"not at head:\n{current.stdout}\n{current.stderr}"


def test_upgrade_is_idempotent(scratch_db):
    """A container booting against an already-migrated database must no-op, not fail."""
    first = _alembic(scratch_db, "upgrade", "head")
    assert first.returncode == 0, first.stderr

    second = _alembic(scratch_db, "upgrade", "head")
    assert second.returncode == 0, f"re-running at head failed:\n{second.stderr}"


def test_the_lock_is_transaction_scoped(scratch_db):
    """pg_advisory_xact_lock, not pg_advisory_lock.

    A session-scoped lock would survive a crashed migration and wedge every later
    boot, and would be invisible across Neon's transaction-mode pooler. Asserting no
    advisory lock outlives the upgrade is the observable form of that requirement.
    """
    import psycopg2

    assert _alembic(scratch_db, "upgrade", "head").returncode == 0

    conn = psycopg2.connect(_sync_url(scratch_db), connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")
            assert cur.fetchone()[0] == 0, "an advisory lock outlived the migration"
    finally:
        conn.close()
