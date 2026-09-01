"""
APScheduler job definitions for periodic scraping and data maintenance.

Role: Started during FastAPI app startup (main.py) when ENABLE_SCHEDULER=true.
      Runs scrape jobs on a fixed cron schedule as an alternative to Cloud Scheduler
      HTTP triggers — both ultimately call the same ScrapeManager logic.
Requires: ENABLE_SCHEDULER env var (via config.py), app.scrapers.manager.ScrapeManager,
          app.database.async_session, and a running async event loop (provided by FastAPI).
"""

# --- Imports ---
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import delete

from app.database import async_session
from app.models import Event
from app.scrapers.manager import ScrapeManager

# --- Module-level setup ---

logger = logging.getLogger(__name__)

# Singleton scheduler instance — started/stopped in main.py lifespan handler
scheduler = AsyncIOScheduler()


# --- Scheduled job callbacks ---

async def scrape_ticketmaster_job():
    """Scrape Ticketmaster venues."""
    logger.info("Starting scheduled Ticketmaster scrape")
    async with async_session() as session:
        manager = ScrapeManager(session)
        results = await manager.scrape_ticketmaster()
        for r in results:
            logger.info(f"  {r}")


async def scrape_indie_job():
    """Scrape indie venues."""
    logger.info("Starting scheduled indie venue scrape")
    async with async_session() as session:
        manager = ScrapeManager(session)
        results = await manager.scrape_indie()
        for r in results:
            logger.info(f"  {r}")


async def cleanup_past_events_job():
    """Delete events more than 7 days in the past, except rows a human owns.

    This is the third deletion path in the app, and the two exclusions below bring it into
    line with the other two. It issues a bulk DELETE rather than going through the ORM, so
    it bypasses ScrapeManager.scraper_must_not_delete entirely — the predicate that stops
    reconcile removing hand-added events and admin-flagged duplicates. Without them, this
    job silently undoes both guarantees a week after the fact:

      * a hand-added event (#87) disappears, though nothing else in the app will touch it
      * a row flagged as a duplicate (#63) disappears, destroying the mapping that made the
        decision reversible and auditable — and taking the "3 hidden duplicates" count on
        its survivor with it

    Only dormant today because ENABLE_SCHEDULER is false in production (cloudbuild.yaml);
    it runs on any local or self-hosted instance with the scheduler on.

    Deliberately not routed through scraper_must_not_delete: that takes a row, and the
    point of a bulk DELETE is not to load any. The conditions are duplicated here on
    purpose, with this comment as the link.
    """
    logger.info("Cleaning up past events")
    # Keep a 7-day buffer so recently-ended events don't vanish immediately
    cutoff = datetime.utcnow().date() - timedelta(days=7)
    async with async_session() as session:
        result = await session.execute(
            delete(Event).where(
                Event.date < cutoff,
                Event.is_manually_created.is_(False),
                Event.duplicate_of_id.is_(None),
            )
        )
        await session.commit()
        logger.info(f"Deleted {result.rowcount} past events")


# --- Scheduler configuration ---

def configure_scheduler():
    """Add all scheduled jobs."""
    # Ticketmaster: 6 AM + 6 PM ET
    scheduler.add_job(
        scrape_ticketmaster_job,
        CronTrigger(hour="6,18", timezone="US/Eastern"),
        id="scrape_ticketmaster",
        replace_existing=True,  # safe to call multiple times (e.g., on hot reload)
    )

    # Indie venues: 6 AM + 12 PM + 6 PM ET
    scheduler.add_job(
        scrape_indie_job,
        CronTrigger(hour="6,12,18", timezone="US/Eastern"),
        id="scrape_indie",
        replace_existing=True,
    )

    # Past event cleanup: 3 AM ET
    scheduler.add_job(
        cleanup_past_events_job,
        CronTrigger(hour=3, timezone="US/Eastern"),
        id="cleanup_past_events",
        replace_existing=True,
    )
