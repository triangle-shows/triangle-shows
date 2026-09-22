"""
Scraper for Duke's university calendar, which runs Bedework and publishes a real feed.

`calendar.duke.edu` takes a `format` parameter alongside its filters, so its listings
are available as JSON without parsing any HTML. Two are read and unioned:

    https://calendar.duke.edu/index?topic=Arts&format=json
    https://calendar.duke.edu/index?cf%5B%5D=Concert%2FMusic&format=json

`arts.duke.edu/events/` is a *different* view of the same calendar and is worth reading
too, though not here — see issue #23 and the draft that follows this one.

An earlier version of this comment said not to scrape it, on two grounds that do not hold.
The first was a bad measurement: it counted `calendar.duke.edu` links in the page and found
37, when the page carries 85 `article.post-event` cards and most do not link out that way.
The second was that the page has no structured dates, which is true of its WordPress REST
API and false of the rendered page, where `.event-date-alt` carries one per card.

Measured 2026-09-22 the page held 85 cards over 30 days — 67 distinct titles against 35
from both feeds here, with 48 titles these feeds do not carry at all, among them An Evening
with Fran Lebowitz and A Conversation with David Rubenstein & Ken Burns. Its `data-post-id`
is distinct per occurrence, so it has a usable external_id, and its cards link back to the
same guids these feeds use, so the two sources dedupe against each other.

What is true is that it costs more to read: the dates carry no year (0 of 81), the listing
mixes in lectures and workshops, and it is HTML rather than a documented feed. That makes
it a third source to add rather than a replacement for these two.

Two properties of the feed shape everything below.

**It is a fixed window of 40 events, and it cannot be widened.** `days`, `count`,
`listMode`, `setappvar(maxEntries)`, `start`, `end` and an `fexpr` date range all return
the identical 40, and `start` is ignored rather than honoured, so it cannot be paged
either. There is no `/feeder/` application deployed.

**Which is why there are two of them.** Each filter gets its own 40, and neither
contains the other: measured 2026-09-22 they shared just 7 guids. The Arts topic spends
its budget on film screenings, exhibitions, dance classes and talks and reached only
nine days ahead; Concert/Music reached twenty-three but carried no film or dance, and
left out nothing musical. Following Arts alone would have dropped eight concerts,
VOCES8 and the Duke Symphony Orchestra centenary among them.

So both are read and unioned on external_id, which is the feed's own identifier and
dedupes the overlap exactly. Measured 2026-09-22 that was 80 raw records collapsing to
64 events -- the 7 shared guids are 16 shared instances once recurrences are counted --
with the longer of the two windows, 23 days. Adding a third filter is one entry in
FEED_URLS.

**One feed serves many venues.** The 40 events are spread over eighteen rooms — Duke
Chapel, six separate spaces inside the Rubenstein Arts Center, Page Auditorium, Smith
Warehouse, the lawn at American Tobacco, even Durham County Library — and the manager
gives one scraper run one venue: it
assigns `venue_id = venue.id` and reconciles with `WHERE Event.venue_id == venue_id`. So
each Duke venue gets its own row pointing at this same scraper, and `scraper_config`
says which locations that row claims. The feed is fetched once per cycle and shared;
see `_feed_cache`.

Role: Instantiated and called by scrapers/manager.py during each scrape cycle,
triggered every 6 hours via POST /api/scrape (Cloud Scheduler or internal APScheduler).
Requires: The venue row must exist in the DB (seeded on startup) with either
          scraper_config = {"location_uids": ["<uid>", ...]}  — claims those locations
          or     scraper_config = {"catch_all": true, "exclude_uids": [...]}  — takes
          the rest. Either may add "feeds": [...] to override FEED_URLS. No API key.
"""

# --- Imports ---
import asyncio
import logging
import time
from datetime import date, datetime, time as time_of_day
from typing import Optional

import httpx

from app.scrapers.base import BaseScraper, ScrapedEvent, BROWSER_HEADERS

# --- Module-level setup ---

logger = logging.getLogger(__name__)

# Read in order, and the first feed to carry an event wins the tie. Both describe the
# same events identically where they overlap, so the order is about determinism rather
# than preference.
FEED_URLS = (
    "https://calendar.duke.edu/index?topic=Arts&format=json",
    "https://calendar.duke.edu/index?cf%5B%5D=Concert%2FMusic&format=json",
)
EVENT_URL = "https://calendar.duke.edu/show?fq=id:{guid}"

# How long a fetched feed may be reused. The manager scrapes venues one after another
# within a cycle, so every Duke row in a single run shares one fetch per feed; a cycle
# is six hours apart, far outside this, so each cycle fetches fresh.
FEED_TTL_SECONDS = 120

# Keyed by URL, because there is more than one feed and they must not shadow each other.
_feed_cache: dict[str, tuple[float, list[dict]]] = {}
_feed_lock = asyncio.Lock()


def _reset_feed_cache() -> None:
    """Drop every cached feed. For tests, which must not inherit each other's fetches."""
    _feed_cache.clear()


# --- Feed access ---

async def _fetch_feed(url: str) -> list[dict]:
    """One feed's event objects, fetched at most once per FEED_TTL_SECONDS.

    The lock matters even though the manager is sequential today: two Duke venues
    scraped concurrently would otherwise both miss the cache and both fetch.
    """
    async with _feed_lock:
        cached = _feed_cache.get(url)
        if cached and (time.monotonic() - cached[0]) < FEED_TTL_SECONDS:
            return cached[1]

        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True, headers=BROWSER_HEADERS
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            payload = resp.json()

        events = [e["event"] for e in payload.get("events", []) if isinstance(e, dict) and "event" in e]
        _feed_cache[url] = (time.monotonic(), events)
        logger.info(f"[DukeBedework] fetched {len(events)} events from {url}")
        return events


async def _fetch_union(urls) -> list[dict]:
    """Every feed's events, with the overlap removed.

    Deduped on external_id, which is the feed's own identifier for one instance of one
    event, so the same show appearing under two filters collapses exactly rather than
    approximately. An event with no guid -- none seen so far -- falls back to its title
    and start, which is the same pair ScrapedEvent.hash would end up leaning on.
    """
    seen: dict = {}
    for url in urls:
        for raw in await _fetch_feed(url):
            key = external_id(raw) or (
                raw.get("summary"), (raw.get("start") or {}).get("unformatted")
            )
            seen.setdefault(key, raw)
    return list(seen.values())


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
    """The room's display name, or None when the feed does not name one.

    Duke writes the *string* "None" rather than a null when an event has no location --
    two of forty in one sample -- so it has to be filtered by value. Without that, the
    room line below would read "None" and the log would count a room by that name.
    """
    loc = raw.get("location") or {}
    name = (loc.get("address") or "").strip()
    if not name or name.lower() == "none":
        return None
    return name


def location_line(raw: dict) -> Optional[str]:
    """The room as it should read to a visitor: the name, and the building it sits in.

    `subaddress` is set on some rooms and is the difference between "Goodson Chapel" and
    "Goodson Chapel, Westbrook Building" -- worth having for somewhere nobody can find.
    """
    name = location_name(raw)
    if not name:
        return None
    loc = raw.get("location") or {}
    sub = (loc.get("subaddress") or "").strip()
    return f"{name}, {sub}" if sub and sub.lower() != "none" else name


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


def event_status(raw: dict) -> str:
    """The feed's status, in this project's vocabulary.

    Bedework sets `status` to CANCELLED on an event that has been called off and leaves
    it in the feed — one of the forty in the Arts window was a cancelled film screening,
    with CANCELLED in its title too. Publishing that as `on_sale` would put a show on
    the calendar that is not happening.

    The frontend has been ready for this for longer than any scraper has produced it:
    modal.js renders a Cancelled badge for exactly this value, and until now nothing
    ever set it.
    """
    if (raw.get("status") or "").strip().upper() == "CANCELLED":
        return "cancelled"
    return "on_sale"


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
    had seen yet would be dropped in silence. With it, the event lands under Duke Arts
    and can be moved to a room of its own later.

    Used by: Duke Arts, which is the catch-all row and currently the only one.
    The `location_uids` form is what a room promoted to its own venue would use.
    """

    async def scrape(self) -> list[ScrapedEvent]:
        # `feeds` overrides the pair above; `url` is the single-feed form, kept because
        # a row that wants one filter should not have to write a list to say so.
        urls = self.config.get("feeds")
        if not urls:
            single = self.config.get("url")
            urls = [single] if single else list(FEED_URLS)
        claimed = set(self.config.get("location_uids") or [])
        catch_all = bool(self.config.get("catch_all"))
        excluded = set(self.config.get("exclude_uids") or [])

        if not claimed and not catch_all:
            raise ValueError(
                f"scraper_config for {self.venue_slug} names no location_uids and is not "
                "the catch_all row, so it would claim nothing."
            )

        raw_events = await _fetch_union(urls)

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

        # The room goes at the top of the description, because the venue row cannot
        # carry it. Every Duke event is filed under one "Duke Arts" venue, so the
        # calendar tile says "Duke Arts" whether the show is in Duke Chapel or on the
        # lawn at American Tobacco -- and those are a mile apart and nothing alike.
        # The description is where a visitor can actually be told which.
        #
        # Its own line rather than inline: .modal-description is styled `white-space:
        # pre-line`, so the break renders, and the room reads as a heading above the
        # blurb instead of running into the first sentence.
        description = _clean(raw.get("description"))
        room = location_line(raw)
        if room:
            description = f"{room}\n\n{description}" if description else room

        guid = (raw.get("guid") or "").strip()
        return ScrapedEvent(
            name=name,
            date=on,
            venue_slug=self.venue_slug,
            source="duke_bedework",
            external_id=external_id(raw),
            show_time=at,
            description=description,
            # `link` is set on about one event in eight, so the calendar's own page for
            # the event is the dependable link and the ticket link is a bonus.
            ticket_url=_clean(raw.get("link")),
            source_url=EVENT_URL.format(guid=guid) if guid else None,
            status=event_status(raw),
        )
