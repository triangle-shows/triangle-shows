"""
Event API endpoints for the Triangle Shows calendar.

Role: Serves GET /api/events/fullcalendar (the primary frontend feed), GET /api/events/{id},
and GET /api/events (paginated list). These endpoints are called by the Vanilla JS +
FullCalendar v6 frontend on page load and whenever the user navigates the calendar.
Requires: async PostgreSQL session (app.database), Event/Venue ORM models (app.models),
response schemas (app.schemas).
"""
import re
from datetime import date, datetime, time
from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.database import get_session
from app.models import Event, Venue
from app.schemas import EventResponse, FullCalendarEvent, EventListResponse

# --- Router setup ---

router = APIRouter(prefix="/api/events", tags=["events"])


# --- Helpers ---

def _completeness(event: Event) -> tuple:
    """Rank two listings of the same show: richer record wins, then most recently scraped.

    Every component is total and the id breaks the last tie, so the winner does not depend
    on the order the database returned rows in.
    """
    return (
        bool(event.image_url) + bool(event.ticket_url) + (event.price_min is not None),
        event.updated_at or datetime.min,
        event.id,
    )


def _event_to_response(event: Event) -> EventResponse:
    """Map an ORM Event (with venue eagerly loaded) to the EventResponse schema."""
    return EventResponse(
        id=event.id,
        venue_id=event.venue_id,
        name=event.name,
        artist=event.artist,
        support_artists=event.support_artists,
        date=event.date,
        doors_time=event.doors_time,
        show_time=event.show_time,
        ticket_url=event.ticket_url,
        price_min=event.price_min,
        price_max=event.price_max,
        image_url=event.image_url,
        genre=event.genre,
        subgenre=event.subgenre,
        status=event.status,
        age_restriction=event.age_restriction,
        description=event.description,
        source=event.source,
        venue_name=event.venue.name if event.venue else None,
        venue_slug=event.venue.slug if event.venue else None,
        venue_city=event.venue.city if event.venue else None,
        venue_color=event.venue.color if event.venue else None,
    )


# --- Endpoints ---

@router.get("/fullcalendar")
async def get_fullcalendar_events(
    start: Optional[str] = Query(None, description="ISO date start"),
    end: Optional[str] = Query(None, description="ISO date end"),
    city: Optional[str] = Query(None),
    size: Optional[str] = Query(None),
    venue: Optional[str] = Query(None, description="Comma-separated venue slugs"),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """FullCalendar JSON feed endpoint."""
    # Only join the Venue table when a venue-level filter is actually present;
    # skipping the join avoids unnecessary overhead for unfiltered calendar loads.
    needs_venue_join = bool(city or size or venue)

    conditions = []

    if start:
        try:
            # Truncate to date portion in case FullCalendar sends a full ISO datetime string.
            start_date = date.fromisoformat(start[:10])
            conditions.append(Event.date >= start_date)
        except ValueError:
            pass

    if end:
        try:
            end_date = date.fromisoformat(end[:10])
            conditions.append(Event.date <= end_date)
        except ValueError:
            pass

    if city:
        # Accept comma-separated city names so the frontend can filter multiple cities at once.
        conditions.append(Venue.city.in_([c.strip() for c in city.split(",")]))

    if size:
        conditions.append(Venue.size_category.in_([s.strip() for s in size.split(",")]))

    if venue:
        conditions.append(Venue.slug.in_([s.strip() for s in venue.split(",")]))

    # Eagerly load venue so we can read venue fields without additional queries.
    query = select(Event).options(joinedload(Event.venue))
    if needs_venue_join:
        query = query.join(Event.venue)
    if conditions:
        query = query.where(and_(*conditions))
    query = query.order_by(Event.date)

    result = await session.execute(query)
    # unique() is required when using joinedload to collapse duplicate rows from the JOIN.
    events = result.unique().scalars().all()

    # --- Same-venue duplicate guard ---
    # scrapers/manager.py is where duplicates are actually prevented; this is a display-time
    # backstop covering the window between a venue editing an event and the next scrape.
    #
    # The key is scoped to the venue deliberately. An earlier version keyed only on
    # (date, artist), which also collapsed listings at *different* venues — and two venues
    # showing the same artist on one night is a moved or miscopied show, not a duplicate.
    # Discarding one of them hid a real listing, and picked which venue survived from
    # whichever row the database happened to return first.
    _dedup_best: dict[tuple, Event] = {}
    for event in events:
        label = event.artist or event.name
        # Normalize to lowercase alphanumeric so minor name differences don't prevent matching.
        norm = re.sub(r"[^a-z0-9]", "", label.lower())
        key = (event.venue_id, event.date, norm)
        incumbent = _dedup_best.get(key)
        if incumbent is None or _completeness(event) > _completeness(incumbent):
            _dedup_best[key] = event
    kept = {ev.id for ev in _dedup_best.values()}
    events = [e for e in events if e.id in kept]

    # --- Build FullCalendar event objects ---

    fc_events = []
    for event in events:
        venue_obj = event.venue
        color = venue_obj.color if venue_obj else "#6366f1"

        # Always use date-only so FullCalendar renders all events as
        # all-day blocks in month view (consistent colored boxes).
        # The actual show time is still available in extendedProps.show_time.
        start_str = event.date.isoformat()

        # --- Price formatting ---
        price_str = None
        if event.price_min is not None:
            if event.price_min == 0 and (event.price_max is None or event.price_max == 0):
                price_str = "Free"
            elif event.price_max and event.price_max != event.price_min:
                price_str = f"${event.price_min:.0f}-${event.price_max:.0f}"
            else:
                price_str = f"${event.price_min:.0f}"

        fc_events.append({
            "id": event.id,
            "title": event.artist or event.name,
            "start": start_str,
            "allDay": True,
            "backgroundColor": color,
            "borderColor": color,
            "textColor": "#ffffff",
            # extendedProps are passed through to the FullCalendar eventDidMount / click
            # handlers in the frontend so the detail popover can display full event info.
            "extendedProps": {
                "event_id": event.id,
                "name": event.name,
                "artist": event.artist,
                "support_artists": event.support_artists,
                "venue_name": venue_obj.name if venue_obj else None,
                "venue_slug": venue_obj.slug if venue_obj else None,
                "venue_city": venue_obj.city if venue_obj else None,
                "venue_color": color,
                "date": event.date.isoformat(),
                # Strip leading zero from hour for display (e.g. "9:00 PM" not "09:00 PM").
                "doors_time": event.doors_time.strftime("%I:%M %p").lstrip("0") if event.doors_time else None,
                "show_time": event.show_time.strftime("%I:%M %p").lstrip("0") if event.show_time else None,
                "ticket_url": event.ticket_url,
                "price": price_str,
                "price_min": event.price_min,
                "price_max": event.price_max,
                "image_url": event.image_url,
                "genre": event.genre,
                "subgenre": event.subgenre,
                "status": event.status,
                "age_restriction": event.age_restriction,
                "description": event.description,
            },
        })

    return fc_events


@router.get("/{event_id}")
async def get_event(
    event_id: int,
    session: AsyncSession = Depends(get_session),
) -> EventResponse:
    """Get a single event by ID."""
    result = await session.execute(
        select(Event).options(joinedload(Event.venue)).where(Event.id == event_id)
    )
    event = result.unique().scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return _event_to_response(event)


@router.get("")
async def list_events(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    genre: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> EventListResponse:
    """List events with filters and pagination."""
    query = select(Event).options(joinedload(Event.venue)).order_by(Event.date)
    count_query = select(func.count(Event.id))

    conditions = []

    if start:
        try:
            conditions.append(Event.date >= date.fromisoformat(start[:10]))
        except ValueError:
            pass
    if end:
        try:
            conditions.append(Event.date <= date.fromisoformat(end[:10]))
        except ValueError:
            pass
    if search:
        search_term = f"%{search}%"
        # Match against both the event name and artist fields.
        conditions.append(
            Event.name.ilike(search_term) | Event.artist.ilike(search_term)
        )
    if genre:
        conditions.append(Event.genre.ilike(f"%{genre}%"))
    if status:
        conditions.append(Event.status == status)

    if conditions:
        query = query.where(and_(*conditions))
        count_query = count_query.where(and_(*conditions))

    # Run the count query before pagination so we can return the total for the UI.
    total_result = await session.execute(count_query)
    total = total_result.scalar()

    offset = (page - 1) * per_page
    query = query.offset(offset).limit(per_page)

    result = await session.execute(query)
    events = result.unique().scalars().all()

    return EventListResponse(
        events=[_event_to_response(e) for e in events],
        total=total,
        page=page,
        per_page=per_page,
        # Integer ceiling division to get total page count.
        pages=(total + per_page - 1) // per_page if total else 0,
    )
