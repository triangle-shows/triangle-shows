"""
GET /api/health — returns current status, event/venue counts, last scrape time, and git SHA.

Role: Lightweight liveness/readiness probe consumed by Cloud Run, uptime monitors, and
      the /deploy skill to confirm a new revision is live. Also exposes scrape freshness
      so operators can tell at a glance whether data is up to date.
Requires: async DB session (app.database), Event/Venue/ScrapeLog models, HealthResponse
          schema, the GIT_COMMIT env var (injected at build time by Cloud Build), and
          settings.APP_ENV to decide whether freshness is enforced.

Returns 503 in production when scrape data has gone stale, so an uptime monitor catches
silently-stopped scraping as well as an outright outage. The body is unchanged, so
callers that only want the deployed SHA can still read it from a 503 response.
"""
# --- Imports ---
import os
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_session
from app.models import Event, Venue, ScrapeLog
from app.schemas import HealthResponse

# --- Config ---

# Cloud Scheduler runs scrapes every 6 hours (0 */6 * * *). Allow two missed cycles
# plus an hour of slack before reporting stale, so one slow or failed run doesn't
# raise an alert on its own.
STALE_AFTER = timedelta(hours=13)

# --- Router ---
router = APIRouter(prefix="/api/health", tags=["health"])


# --- Endpoint ---

@router.api_route("", methods=["GET", "HEAD"], response_model=HealthResponse)
async def health_check(response: Response, session: AsyncSession = Depends(get_session)):
    """Health check with event count and last scrape info.

    HEAD is accepted as well as GET because UptimeRobot sends HEAD, and a GET-only route
    answers that with 404 — so the uptime monitor was recording a failure on every check
    against a perfectly healthy service. Starlette discards the body for a HEAD request,
    so the status code still reflects the staleness logic below.

    Reports 503 with status="stale" when the most recent successful scrape is older
    than STALE_AFTER. Freshness is only enforced in production: CI and local runs
    disable scraping on purpose, so an empty or old ScrapeLog is expected there and
    must not fail the CI smoke test.
    """
    event_count = (await session.execute(select(func.count(Event.id)))).scalar()
    venue_count = (await session.execute(select(func.count(Venue.id)))).scalar()

    # Pull the most recent successful scrape timestamp for freshness reporting
    last_scrape_result = await session.execute(
        select(ScrapeLog.finished_at)
        .where(ScrapeLog.status == "success")
        .order_by(ScrapeLog.finished_at.desc())
        .limit(1)
    )
    last_scrape = last_scrape_result.scalar_one_or_none()

    # finished_at is stored naive in UTC (models use datetime.utcnow), so compare
    # against utcnow() — an aware value here would raise on subtraction.
    is_stale = settings.APP_ENV == "production" and (
        last_scrape is None or (datetime.utcnow() - last_scrape) > STALE_AFTER
    )
    if is_stale:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(
        status="stale" if is_stale else "ok",
        event_count=event_count,
        venue_count=venue_count,
        last_scrape=last_scrape,
        version=os.environ.get("GIT_COMMIT", "unknown"),  # set by Cloud Build at image build time
    )
