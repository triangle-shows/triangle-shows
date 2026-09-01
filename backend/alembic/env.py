"""
Alembic migration environment — imports SQLAlchemy models and runs schema migrations.

Role: Invoked by `alembic upgrade head` (manually or during deploy); not part of the
      runtime request path. Must run before the app starts if the schema is out of date.
Requires: DATABASE_URL env var (falls back to alembic.ini), app.database.Base,
          app.models (Venue, Event, ScrapeLog).
"""

# --- Imports ---

import logging
import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool, text
from alembic import context

# Add parent directory to path so we can import app modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database import Base
from app.models import Venue, Event, ScrapeLog  # noqa: F401 - ensure models are imported

# --- Alembic Config & URL Setup ---

config = context.config

# Override sqlalchemy.url with env var if available
db_url = os.getenv("DATABASE_URL", config.get_main_option("sqlalchemy.url"))
# Alembic needs sync driver; also convert asyncpg-style SSL param to psycopg2-style
if db_url:
    db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")
    db_url = db_url.replace("ssl=require", "sslmode=require")
    config.set_main_option("sqlalchemy.url", db_url)

# Configure logging from alembic.ini — but only when nothing has configured it already.
#
# main.py applies migrations from inside its lifespan handler, which runs this file in the
# app's own process. fileConfig defaults to disable_existing_loggers=True, so an
# unconditional call here disabled every app.* logger and replaced root's handlers with
# alembic.ini's. Two things broke, both silently:
#
#   * Nothing logged after migrations was emitted — no "Migrations applied", no
#     per-request lines, and not one ERROR-severity entry across a period containing
#     hundreds of HTTP 500s. That is issue #79, and it is why those 500s have no
#     traceback to diagnose.
#   * Root's handlers lost the redact_handler wrapper from #77, reopening the
#     Ticketmaster credential sink for anything logging through root. #77's docstring
#     names this residue exactly: "handlers installed *after* this runs are not
#     covered".
#
# A root logger with handlers means somebody has already configured logging deliberately
# — under the alembic CLI it has none, so the ini still applies there, which is the case
# this call exists for. main.py additionally re-applies its own configuration after
# migrations, so the fix does not depend on this check alone being right.
if config.config_file_name is not None and not logging.getLogger().handlers:
    fileConfig(config.config_file_name)

# Point Alembic at the full set of ORM models so it can diff against the live schema
target_metadata = Base.metadata

# Advisory lock held while migrations run, so two processes cannot apply them at once.
# The value is arbitrary but must never change: processes serialize only if they ask
# for the same key.
MIGRATION_LOCK_KEY = 4021755


# --- Migration Runners ---

def run_migrations_offline():
    """Run migrations without a live DB connection, emitting SQL to stdout or a file."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    """Run migrations against a live database connection, one process at a time.

    Migrations run from the FastAPI lifespan handler (app/main.py), not as a deploy
    step, so with Cloud Run at max-instances=2 two containers can boot together and
    both call `upgrade head` against the same database. Both read alembic_version,
    both decide the same work is outstanding, and whichever loses the race then fails
    on an object the winner has already created. The exception escapes `lifespan`, so
    that container never becomes healthy — a deploy that half-succeeds.

    Taking an advisory lock *before* run_migrations() reads the version table
    serializes them. The loser waits at the lock, then re-reads alembic_version and
    correctly finds nothing to do.

    pg_advisory_xact_lock rather than pg_advisory_lock, for two reasons. It releases
    on commit or rollback with no explicit unlock, so a crashed or killed migration
    cannot leave a lock wedging every future boot. And it stays correct through Neon's
    transaction-mode pooler, which pins a transaction to a single backend but is free
    to route the next statement of a *session* somewhere else — where a session-scoped
    lock would be invisible.

    Postgres-only, like the rest of this project.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # No connection pooling needed for one-shot migration runs
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            # Must precede run_migrations(): the point is to hold the lock across the
            # read of alembic_version *and* the migrations themselves, so the decision
            # about what is outstanding cannot go stale between the two.
            connection.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": MIGRATION_LOCK_KEY},
            )
            context.run_migrations()


# --- Entry Point ---

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
