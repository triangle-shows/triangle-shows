"""
APScheduler job definitions for periodic scraping.

Role: Started during FastAPI app startup (main.py) when ENABLE_SCHEDULER=true.
      Runs scrape jobs on a fixed cron schedule as an alternative to Cloud Scheduler
      HTTP triggers — both ultimately call the same ScrapeManager logic. Every job here
      only ever adds or updates events; nothing scheduled deletes them, deliberately
      (see configure_scheduler).
Requires: ENABLE_SCHEDULER env var (via config.py), app.scrapers.manager.ScrapeManager,
          app.database.async_session, and a running async event loop (provided by FastAPI).
"""

# --- Imports ---
#
# No `delete`, no Event model, no date arithmetic: every import here is for scheduling or
# scraping. That is a property worth noticing rather than an accident — this module cannot
# delete a row without someone adding an import first.
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.database import async_session
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

    # No past-event cleanup job, deliberately. Removed rather than left commented out,
    # because commented-out code gets re-enabled by someone who reads the comment as a
    # to-do rather than a decision.
    #
    # There used to be one deleting every event more than 7 days old, nightly at 3 AM ET.
    # It never ran anywhere — ENABLE_SCHEDULER is false in cloudbuild.yaml, in
    # backend/.env.example, in docs/SELF-HOSTING.md and by default in config.py — which is
    # the only reason the archive still exists: production holds events back to January.
    #
    # Past events are wanted, and three separate parts of the app already assume so:
    #
    #   * plan_upsert bounds reconcile to `today <= date <= horizon` precisely so a scrape
    #     cannot delete the archive; its comment says deleting past events "would wipe the
    #     archive every run"
    #   * the admin moderation queue and reclassify_all() both work from reclassify_floor(),
    #     which is today minus 30 days — a 7-day cleanup would delete rows the admin UI is
    #     built to display
    #   * the public calendar can be scrolled backwards, and /api/events applies no floor
    #
    # The job also bypassed the two protections that make hand-added events and
    # admin-flagged duplicates safe: it issued a bulk DELETE, so it never reached
    # ScrapeManager.scraper_must_not_delete, and would have silently undone both a week
    # after the fact.
    #
    # There is no operational pressure the other way. Production is at ~2,900 events after
    # a year across 22 venues; the table is not going to become a problem.
