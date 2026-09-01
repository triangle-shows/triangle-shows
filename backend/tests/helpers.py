"""Shared test doubles for the scrape failure path.

``ScrapeManager.scrape_venue`` writes a ScrapeLog row, so reaching its ``except`` block
normally needs a database. These stand in for the narrow slice of AsyncSession and Venue
that the failure path actually touches, which keeps those tests in CI's unit-test step —
that step runs *before* ``alembic upgrade head``, so the schema does not exist yet.

Two test modules now assert on that block from different angles (credential redaction in
``test_redaction.py``, non-empty diagnostics in ``test_scrape_observability.py``), so the
doubles live here rather than being copied into each.
"""


class StubSession:
    """The slice of AsyncSession that scrape_venue's failure path touches."""

    def __init__(self):
        self.added = []

    async def refresh(self, obj):
        return None

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None

    async def rollback(self):
        return None

    async def commit(self):
        return None


class StubVenue:
    slug = "red-hat"
    id = 1
    scraper_type = "ticketmaster"


def scrape_logs(session: StubSession) -> list:
    """The ScrapeLog rows `session` was asked to persist.

    Identified by the presence of `error_message` rather than by importing the model, so
    these helpers stay usable without the SQLAlchemy metadata being configured.
    """
    return [row for row in session.added if hasattr(row, "error_message")]
