"""
Generates the iCal subscription feed served at GET /feeds/events.ics.

Role: Consumed directly by calendar clients (Apple Calendar, Google Calendar, Outlook).
Users subscribe once; the feed stays live and reflects whatever the scraper has loaded
into the database. Optionally filtered to one or more venues via ?venue= slug.
Requires: PostgreSQL (via app.database), app.models (Event, Venue), icalendar library.
"""

# --- Standard library imports ---
import zoneinfo
from datetime import date, datetime, timedelta, timezone
from typing import Optional

# --- Third-party imports ---
from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from icalendar import Calendar, Event as ICalEvent, vText
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

# --- Internal imports ---
from app.database import get_session
from app.models import Event, Venue

# --- Router setup ---
router = APIRouter(prefix="/feeds", tags=["feeds"])


# --- iCal feed endpoint ---

@router.get("/events.ics", response_class=Response)
async def get_ical_feed(
    venue: Optional[str] = Query(None, description="Comma-separated venue slugs. Omit for all venues."),
    include_non_music: bool = Query(
        False,
        description="Include non-live-music events (karaoke, trivia, theme nights, etc.). "
                    "Off by default so subscribers get live music only.",
    ),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Live iCal subscription feed. Add to Apple Calendar, Google Calendar, or Outlook once;
    new shows appear automatically as the scraper finds them."""

    today = date.today()
    # Only include upcoming events — no historical clutter in subscribers' calendars
    conditions = [Event.date >= today]
    # A row an admin folded into another as a duplicate never reaches a subscriber
    # (issue #63). No opt-in, unlike include_non_music: a duplicate is not a kind of
    # event someone might want, it is a listing that should not exist. This matters more
    # here than on the calendar — a subscribed feed writes into someone's own calendar,
    # where a duplicate entry is not merely clutter but a second reminder for one show.
    conditions.append(Event.duplicate_of_id.is_(None))
    # Mirror the on-site calendar: hide non-live-music events unless opted in.
    if not include_non_music:
        conditions.append(Event.is_live_music.is_(True))

    needs_join = bool(venue)
    if venue:
        # Support multi-venue filtering: ?venue=cat's-cradle,motorco
        slugs = [s.strip() for s in venue.split(",") if s.strip()]
        conditions.append(Venue.slug.in_(slugs))

    query = select(Event).options(joinedload(Event.venue))
    if needs_join:
        # JOIN is only needed when filtering by venue slug
        query = query.join(Event.venue)
    query = query.where(and_(*conditions)).order_by(Event.date)

    result = await session.execute(query)
    # .unique() is required after joinedload to collapse duplicate rows from the JOIN
    events = result.unique().scalars().all()

    # --- Build the iCal Calendar object ---

    cal = Calendar()
    cal.add("prodid", "-//triangle-shows.net//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", vText("Triangle Shows"))
    cal.add("x-wr-caldesc", vText("Live music across the Triangle — triangle-shows.net"))
    cal.add("x-wr-timezone", vText("America/New_York"))
    # Suggest clients refresh every 6 hours (matches scraper cadence)
    cal.add("refresh-interval;value=duration", "PT6H")
    cal.add("x-published-ttl", "PT6H")

    now = datetime.now(timezone.utc)

    # --- Serialize each event as an iCal VEVENT component ---

    for event in events:
        venue_obj = event.venue
        iev = ICalEvent()

        iev.add("uid",     vText(f"{event.id}@triangle-shows.org"))
        iev.add("dtstamp", now)

        # Summary: prefer artist name, fall back to event name
        summary = event.artist or event.name
        iev.add("summary", vText(summary))

        # All-day or timed event — iCal uses DATE vs DATETIME depending on whether time is known
        if event.show_time:
            tz = zoneinfo.ZoneInfo("America/New_York")
            start = datetime.combine(event.date, event.show_time, tzinfo=tz)
            iev.add("dtstart", start)
            # Assume 3-hour show duration when no end time is scraped
            iev.add("dtend",   start + timedelta(hours=3))
        else:
            iev.add("dtstart", event.date)
            iev.add("dtend",   event.date + timedelta(days=1))

        # Location
        if venue_obj:
            iev.add("location", vText(f"{venue_obj.name}, {venue_obj.city}, NC"))

        # Description — pack in the useful bits
        desc_parts = []
        if event.name and event.name != summary:
            # Include full event name when it differs from the headline artist
            desc_parts.append(event.name)
        if event.support_artists:
            desc_parts.append(f"w/ {event.support_artists}")
        if event.doors_time:
            desc_parts.append(f"Doors: {event.doors_time.strftime('%-I:%M %p')}")
        if event.show_time:
            desc_parts.append(f"Show: {event.show_time.strftime('%-I:%M %p')}")
        if event.price_min is not None:
            if event.price_min == 0:
                desc_parts.append("Free")
            elif event.price_max and event.price_max != event.price_min:
                desc_parts.append(f"${event.price_min:.0f}–${event.price_max:.0f}")
            else:
                desc_parts.append(f"${event.price_min:.0f}")
        if event.age_restriction:
            desc_parts.append(event.age_restriction)
        if event.ticket_url:
            # Separate ticket URL onto its own line for readability in calendar apps
            desc_parts.append(f"\n{event.ticket_url}")
        if desc_parts:
            iev.add("description", vText("\n".join(desc_parts)))

        # URL
        if event.ticket_url:
            iev.add("url", event.ticket_url)

        cal.add_component(iev)

    # --- Serialize and return the .ics response ---

    ical_bytes = cal.to_ical()
    return Response(
        content=ical_bytes,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="triangle-shows.ics"',
            # Cache for 1 hour on CDN/proxies; scraper runs every 6 hours so this is safe
            "Cache-Control": "public, max-age=3600",
        },
    )
