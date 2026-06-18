"""Health check endpoint."""
import os
from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import Event, Venue, ScrapeLog
from app.schemas import HealthResponse

router = APIRouter(prefix="/api/health", tags=["health"])


@router.get("", response_model=HealthResponse)
async def health_check(session: AsyncSession = Depends(get_session)):
    """Health check with event count and last scrape info."""
    event_count = (await session.execute(select(func.count(Event.id)))).scalar()
    venue_count = (await session.execute(select(func.count(Venue.id)))).scalar()

    last_scrape_result = await session.execute(
        select(ScrapeLog.finished_at)
        .where(ScrapeLog.status == "success")
        .order_by(ScrapeLog.finished_at.desc())
        .limit(1)
    )
    last_scrape = last_scrape_result.scalar_one_or_none()

    return HealthResponse(
        status="ok",
        event_count=event_count,
        venue_count=venue_count,
        last_scrape=last_scrape,
        version=os.environ.get("GIT_COMMIT", "unknown"),
    )
