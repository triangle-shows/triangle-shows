"""
Scraper for Duke's university calendar, which runs Bedework and publishes a real feed.

`calendar.duke.edu` takes a `format` parameter alongside its category filter, so the
Concert/Music listing is available as JSON without parsing any HTML:

    https://calendar.duke.edu/index?cf%5B%5D=Concert%2FMusic&format=json

Do not scrape `arts.duke.edu`. It looks like the right source and is not: its WordPress
`event` post type carries no event date (the `date` field is the post date), and its own
cards link out to calendar.duke.edu. Measured 2026-09-22, its /events/ page listed 37
events reaching 21 October against this feed's 40 reaching 15 October — fewer events and
six more days, for a great deal more work. See issue #23.

Two properties of the feed shape everything below.

**It is a fixed window of 40 events, and it cannot be widened.** `days`, `count`,
`listMode`, `setappvar(maxEntries)`, `start`, `end` and an `fexpr` date range all return
the identical 40, and `start` is ignored rather than honoured, so it cannot be paged
either. There is no `/feeder/` application deployed. Measured against today that window
spans 23 days, so Duke events appear about three weeks out and no further. That is a
property of the source, not a bug here.

**One feed serves many venues.** The 40 events sit at Duke Chapel, Baldwin Auditorium,
American Tobacco Campus and others, and the manager gives one scraper run one venue — it
assigns `venue_id = venue.id` and reconciles with `WHERE Event.venue_id == venue_id`. So
each Duke venue gets its own row pointing at this same scraper, and `scraper_config`
says which locations that row claims. The feed is fetched once per cycle and shared;
see `_feed_cache`.

Role: Instantiated and called by scrapers/manager.py during each scrape cycle,
triggered every 6 hours via POST /api/scrape (Cloud Scheduler or internal APScheduler).
Requires: The venue row must exist in the DB (seeded on startup) with either
          scraper_config = {"location_uids": ["<uid>", ...]}  — claims those locations
          or     scraper_config = {"catch_all": true, "exclude_uids": [...]}  — takes
          the rest. No API key.
"""

# --- Imports ---
import asyncio
import logging
import time
from datetime import date, datetime, time as time_of_day
from typing import Any, Optional

import httpx

from app.scrapers.base import BaseScraper, ScrapedEvent, BROWSER_HEADERS

# --- Module-level setup ---

logger = logging.getLogger(__name__)

FEED_URL = "https://calendar.duke.edu/index?cf%5B%5D=Concert%2FMusic&format=json"
EVENT_URL = "https://calendar.duke.edu/show?fq=id:{guid}"

# How long a fetched feed may be reused. The manager scrapes venues one after another
# within a cycle, so every Duke row in a single run shares one fetch; a cycle is six
# hours apart, far outside this, so each cycle fetches fresh.
FEED_TTL_SECONDS = 120

_feed_cache: dict[str, Any] = {"at": 0.0, "events": None}
_feed_lock = asyncio.Lock()


def _reset_feed_cache() -> None:
    """Drop the shared feed. For tests, which must not inherit each other's fetches."""
    _feed_cache["at"] = 0.0
    _feed_cache["events"] = None


# --- Feed access ---

async def _fetch_feed(url: str) -> list[dict]:
    """The feed's event objects, fetched at most once per FEED_TTL_SECONDS.

    The lock matters even though the manager is sequential today: two Duke venues
    scraped concurrently would otherwise both miss the cache and both fetch.
    """
    async with _feed_lock:
        fresh = (
            _feed_cache["events"] is not None
            and (time.monotonic() - _feed_cache["at"]) < FEED_TTL_SECONDS
        )
        if fresh:
            return _feed_cache["events"]

        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True, headers=BROWSER_HEADERS
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            payload = resp.json()

        events = [e["event"] for e in payload.get("events", []) if isinstance(e, dict) and "event" in e]
        _feed_cache["events"] = events
        _feed_cache["at"] = time.monotonic()
        logger.info(f"[DukeBedework] fetched {len(events)} events from the Concert/Music feed")
        return events


# --- Field helpers ---

def location_uid(raw: dict) -> Optional[str]:
    """The stable id of the room an event is in, or None.

    The uid is used rather than `location.address` because the address is a display
    name Duke can reword at will, and a venue row that matched on the old wording would
    quietly stop claiming its own events.
    """
    loc = raw.get("location") or {}
    uid = loc.get("uid")
    return uid or None


def location_name(raw: dict) -> Optional[str]:
    """The room's display name, for logging an unclaimed location usefully."""
    loc = raw.get("location") or {}
    name = (loc.get("address") or "").strip()
    return name or None


def parse_start(raw: dict) -> Optional[tuple[date, Optional[time_of_day]]]:
    """The local date and start time of an event.

    Bedework gives both a UTC stamp and an `unformatted` local one. The local value is
    what is wanted: an 8pm show in Durham is on the day Durham says it is, and reading
    the UTC stamp would move a late show to the next day for half the year. This is the
    same trap frontend/js/lineup.js documents for stored favourites.
    """
    start = raw.get("start") or {}
    stamp = start.get("unformatted") or ""
    try:
        parsed = datetime.strptime(stamp, "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    if start.get("allday") == "true":
        return parsed.date(), None
    return parsed.date(), parsed.time()


def external_id(raw: dict) -> Optional[str]:
    """A stable id for one instance of a possibly-recurring event.

    The guid alone is not unique: a recurring series shares one across every instance —
    all eighteen carillon recitals in one sample carried the same guid — so the
    recurrence id has to come with it. This is the form Duke's own links use, e.g.
    `...demobedework@mysite.edu_20260922T170000Z`.

    plan_upsert matches on (external_id, date) before falling back to the title hash,
    which is the path that survives a show being renamed, so this is worth getting right.
    """
    guid = (raw.get("guid") or "").strip()
    if not guid:
        return None
    rid = (raw.get("recurrenceId") or "").strip()
    return f"{guid}_{rid}" if rid else guid


def _clean(value: Optional[str]) -> Optional[str]:
    text = (value or "").strip()
    return text or None


# --- Scraper class ---

class DukeBedeworkScraper(BaseScraper):
    """Scrape one Duke venue's share of the university Concert/Music feed.

    Which share is decided by `scraper_config`:

      {"location_uids": ["18832edc-..."]}      this row's rooms, by stable location uid
      {"catch_all": true, "exclude_uids": [...]}   everything the named rows did not take

    The catch-all exists because Duke's set of locations cannot be enumerated — there is
    no locations endpoint, and the feed only ever shows the next 40 events — so a fixed
    list of rooms is guaranteed to be incomplete. Without it, a concert in a room nobody
    had seen yet would be dropped in silence. With it, the event lands under the
    university's own name and can be moved to a room of its own later.

    Used by: Duke University, which is the catch-all row and currently the only one.
    The `location_uids` form is what a room promoted to its own venue would use.
    """

    async def scrape(self) -> list[ScrapedEvent]:
        url = self.config.get("url") or FEED_URL
        claimed = set(self.config.get("location_uids") or [])
        catch_all = bool(self.config.get("catch_all"))
        excluded = set(self.config.get("exclude_uids") or [])

        if not claimed and not catch_all:
            raise ValueError(
                f"scraper_config for {self.venue_slug} names no location_uids and is not "
                "the catch_all row, so it would claim nothing."
            )

        raw_events = await _fetch_feed(url)

        events: list[ScrapedEvent] = []
        rooms: dict[str, int] = {}
        for raw in raw_events:
            uid = location_uid(raw)
            if catch_all:
                if uid in excluded:
                    continue
            elif uid not in claimed:
                continue

            parsed = self._to_event(raw)
            if not parsed:
                continue
            events.append(parsed)
            name = location_name(raw) or "(no location)"
            rooms[name] = rooms.get(name, 0) + 1

        # The rooms this row swept up, with counts. Duke publishes no list of its
        # locations and the feed only ever shows the next 40 events, so there is no way
        # to know the full set in advance -- this line is the only record of which rooms
        # are actually in use, and it is what says when one has earned a venue row of
        # its own. Named locations carry a stable uid, so promoting one is a matter of
        # reading the uid out of the feed rather than matching on a display name.
        if rooms:
            breakdown = ", ".join(f"{name} x{n}" for name, n in
                                  sorted(rooms.items(), key=lambda kv: -kv[1]))
            logger.info(f"[DukeBedework] {self.venue_slug} rooms: {breakdown}")

        logger.info(f"[DukeBedework] Found {len(events)} events for {self.venue_slug}")
        return events

    def _to_event(self, raw: dict) -> Optional[ScrapedEvent]:
        """One feed record as a ScrapedEvent, or None if it has no usable date."""
        name = _clean(raw.get("summary"))
        if not name:
            return None
        when = parse_start(raw)
        if not when:
            return None
        on, at = when

        guid = (raw.get("guid") or "").strip()
        return ScrapedEvent(
            name=name,
            date=on,
            venue_slug=self.venue_slug,
            source="duke_bedework",
            external_id=external_id(raw),
            show_time=at,
            description=_clean(raw.get("description")),
            # `link` is set on about one event in eight, so the calendar's own page for
            # the event is the dependable link and the ticket link is a bonus.
            ticket_url=_clean(raw.get("link")),
            source_url=EVENT_URL.format(guid=guid) if guid else None,
            status="on_sale",
        )
