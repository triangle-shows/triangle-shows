"""
Exposes the GET /api/venues endpoint, returning all venues for the frontend filter UI.

Role: Called by the frontend on page load to populate the venue filter sidebar;
      not part of the scrape pipeline — purely a read-only query endpoint.
Requires: app.database (async PostgreSQL session), app.models.Venue, app.schemas.VenueResponse.
"""

# --- Imports ---
from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import Venue, Event, MANUAL_SCRAPER_TYPE
from app.schemas import VenueResponse

# --- Router ---

router = APIRouter(prefix="/api/venues", tags=["venues"])


# --- Endpoints ---

@router.get("", response_model=list[VenueResponse])
async def list_venues(session: AsyncSession = Depends(get_session)):
    """Get all venues with metadata for the filter UI.

    Hand-added venues (promoters, festivals, one-off series) are omitted while they have no
    upcoming events. A scraped venue with an empty calendar is still a real place worth
    showing — it has a quiet month, and it will fill up again on the next scrape. A promoter
    exists only as somewhere to hang events, so once its events are past it is a filter that
    selects nothing, and every one an admin ever creates would accumulate in the sidebar
    forever. Scraped venues are always listed, whatever their count, so this cannot quietly
    remove a real venue from the filters.
    """
    # Order by city first so the frontend can group venues by market (Raleigh, Durham, Chapel Hill)
    result = await session.execute(select(Venue).order_by(Venue.city, Venue.name))
    venues = result.scalars().all()

    # One grouped query rather than a count per venue. Mirrors the public read filters:
    # today onward, and excluding rows an admin flagged as duplicates (#63), so the number
    # matches what the calendar would actually show.
    counts = dict((await session.execute(
        select(Event.venue_id, func.count())
        .where(Event.date >= date.today(), Event.duplicate_of_id.is_(None))
        .group_by(Event.venue_id)
    )).all())

    out = []
    for venue in venues:
        count = counts.get(venue.id, 0)
        if venue.scraper_type == MANUAL_SCRAPER_TYPE and count == 0:
            continue
        response = VenueResponse.model_validate(venue)
        response.upcoming_event_count = count
        out.append(response)
    return out
