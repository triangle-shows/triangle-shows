"""
Scraper for venues whose public calendar is the InstantSeats list widget served from
clickgobuynow.com.

The venue's own site embeds that widget in an iframe and adds nothing to it — Sharp 9
Gallery's /concerts.html is a full-page iframe of https://clickgobuynow.com/durham/ — so the
widget URL is the canonical listing and is what `scraper_config["url"]` should point at.
Scrape the widget rather than the venue page: the iframe wrapper carries no event data, and
the widget also offers a calendar view whose day cells hold markup this does not parse.

Role: Instantiated and called by scrapers/manager.py during each scrape cycle,
triggered every 6 hours via POST /api/scrape (Cloud Scheduler or internal APScheduler).
Requires: The venue row must exist in the DB (seeded on startup) with
          scraper_config = {"url": "<the clickgobuynow list view>"}. No API key.
"""

# --- Imports ---
import logging
import re
from collections import defaultdict
from datetime import date
from typing import Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from app.scrapers.base import BaseScraper, ScrapedEvent, BROWSER_HEADERS

# --- Module-level setup ---

logger = logging.getLogger(__name__)

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

# The details block is three unlabelled lines — "$20 - $35", "Thursday 9/24", "7:00 pm" — so
# each is recognised by shape rather than by position. A listing that omits the price would
# otherwise shift the other two up and be silently misread.
_WEEKDAY_LINE = re.compile(
    r"\b(" + "|".join(WEEKDAYS) + r")\b\s+(\d{1,2})/(\d{1,2})", re.IGNORECASE
)
_TIME_LINE = re.compile(r"\b\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?\b", re.IGNORECASE)
_EVENT_ID = re.compile(r"eventID=([0-9A-Za-z-]+)", re.IGNORECASE)


# --- Scraper class ---

class InstantSeatsScraper(BaseScraper):
    """Scrape events from an InstantSeats list view on clickgobuynow.com.

    Each event is a ``div.row`` containing:
      - Date box:  div.event-date > p.event-month + p.event-day  (no year — see _parse_date)
      - Title:     h6.title
      - Details:   div.details > p  — price, weekday + M/D, and start time, one per line
      - Image:     div.photofit img
      - Links:     an InstantSeats "Buy Tickets" anchor (absent once a show sells out) and
                   an "Event Info" anchor (always present), both carrying ?eventID=<guid>
      - Sold out:  span.alert, which replaces the Buy Tickets anchor

    Used by: Sharp 9 Gallery
    """

    async def scrape(self) -> list[ScrapedEvent]:
        """Fetch the list view and return ScrapedEvent objects."""
        url = self.config.get("url")
        if not url:
            raise ValueError(f"No 'url' in scraper_config for {self.venue_slug}")

        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=BROWSER_HEADERS) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

        today = date.today()
        events = []
        for title_el in soup.select("h6.title"):
            row = title_el.find_parent("div", class_="row")
            if row is None:
                continue
            parsed = self._parse_row(row, title_el, url, today)
            if parsed:
                events.append(parsed)

        events = self._disambiguate_same_night_sets(events)

        logger.info(f"[InstantSeats] Found {len(events)} events for {self.venue_slug}")
        return events

    # --- Row parsing ---

    def _parse_row(self, row, title_el, page_url: str, today: date) -> Optional[ScrapedEvent]:
        """Extract event fields from a single row; returns None on any failure."""
        try:
            name = title_el.get_text(strip=True)
            if not name:
                return None

            month_el = row.select_one("p.event-month")
            day_el = row.select_one("p.event-day")
            if not month_el or not day_el:
                return None

            # Lines of the details block, e.g. ["$20 - $35", "Thursday 9/24", "7:00 pm"].
            details_el = row.select_one("div.details p")
            lines = list(details_el.stripped_strings) if details_el else []

            price_text = next((ln for ln in lines if "$" in ln or "free" in ln.lower()), "")
            weekday_match = next(
                (m for m in (_WEEKDAY_LINE.search(ln) for ln in lines) if m), None
            )
            # Exclude the line the date came from. "Thursday 9/24" carries no am/pm today,
            # but keeping the two separate means a format change cannot cross them.
            time_text = next(
                (ln for ln in lines if _TIME_LINE.search(ln) and not _WEEKDAY_LINE.search(ln)),
                "",
            )

            event_date = self._parse_date(
                month_el.get_text(strip=True), day_el.get_text(strip=True), weekday_match, today
            )
            if not event_date:
                return None

            # The Event Info anchor survives a sell-out; the Buy Tickets one does not. Take
            # the ID from whichever is present, so a show that sells out keeps a stable
            # identity — without it the row would rematch on the title hash alone, which is
            # what breaks for this venue's repeated artist names and double bills.
            buy_el = row.select_one('a[href*="buy.event"]')
            info_el = row.select_one('a[href*="home.event"]')
            buy_url = urljoin(page_url, buy_el["href"]) if buy_el and buy_el.get("href") else None
            info_url = urljoin(page_url, info_el["href"]) if info_el and info_el.get("href") else None

            external_id = None
            for candidate in (buy_url, info_url):
                if candidate:
                    m = _EVENT_ID.search(candidate)
                    if m:
                        external_id = m.group(1)
                        break

            sold_out = row.select_one("span.alert") is not None

            img_el = row.select_one("div.photofit img")
            image_url = urljoin(page_url, img_el["src"]) if img_el and img_el.get("src") else None

            price_min, price_max = self.parse_price_range(price_text)

            return ScrapedEvent(
                name=name,
                date=event_date,
                venue_slug=self.venue_slug,
                source="instantseats",
                external_id=external_id,
                artist=name,
                show_time=self.parse_time(time_text),
                # Falls back to the info page so a sold-out show still links somewhere. The
                # modal renders a "Sold Out" badge from status alongside the button.
                ticket_url=buy_url or info_url,
                price_min=price_min,
                price_max=price_max,
                image_url=image_url,
                status="sold_out" if sold_out else "on_sale",
                source_url=info_url or page_url,
            )
        except Exception as e:
            logger.warning(f"[InstantSeats] Row parse error for {self.venue_slug}: {e}")
            return None

    # --- Helpers ---

    @staticmethod
    def _parse_date(month_str: str, day_str: str, weekday_match, today: date) -> Optional[date]:
        """Convert the date box plus the details line into a date.

        The listing states no year anywhere, so it has to be inferred. It does state the
        weekday, which pins the year exactly: a given month and day falls on a different
        weekday in any two consecutive years, so at most one candidate can match.
        """
        try:
            day = int(day_str.strip())
        except (ValueError, TypeError):
            return None
        month = MONTHS.get(month_str.strip().lower()[:3])
        if not month:
            return None

        weekday = None
        if weekday_match:
            # The details line repeats the date box as M/D. Only trust its weekday when the
            # two agree, so a mismatch degrades to the date-only rule instead of pinning the
            # year off the wrong date.
            line_month, line_day = int(weekday_match.group(2)), int(weekday_match.group(3))
            if (line_month, line_day) == (month, day):
                weekday = WEEKDAYS[weekday_match.group(1).lower()]

        candidates = []
        for year in (today.year, today.year + 1):
            try:
                candidates.append(date(year, month, day))
            except ValueError:
                continue  # 29 February in a common year
        if not candidates:
            return None

        if weekday is not None:
            matching = [d for d in candidates if d.weekday() == weekday]
            if matching:
                return matching[0]
            # Nothing matched. The site changed shape, or the listing is simply wrong; fall
            # through to the date-only rule rather than dropping a real event over it.
            logger.warning(
                f"[InstantSeats] Weekday in listing matches no candidate year for {month}/{day}"
            )

        # A listing more than a week past is next year's show far more often than it is a
        # genuinely stale one, which is the same rule the Carolina Theatre scraper uses.
        for candidate in candidates:
            if (candidate - today).days >= -7:
                return candidate
        return candidates[-1]

    @staticmethod
    def _disambiguate_same_night_sets(events: list[ScrapedEvent]) -> list[ScrapedEvent]:
        """Append the start time to titles that would otherwise collide on the dedup hash.

        A jazz club plays the same artist at an early and a late set, which the listing
        gives as two entries sharing a date and a title. ScrapedEvent.hash is
        venue + date + title, so the two hash alike: dedupe_scraped in the manager drops the
        second, and Event.hash is unique in the database besides. Both sets are real shows,
        sold separately, so the fix is to make the titles differ rather than to lose one —
        "Pasquale Grasso (7:00 pm)" and "Pasquale Grasso (9:00 pm)".

        Renaming is stable across runs because plan_upsert matches on external_id before the
        hash, so a stored row is updated to the new name rather than left behind as an
        orphan while the renamed event inserts alongside it.

        Only collisions are touched; a single show keeps the title the venue gave it.
        """
        by_hash: dict[str, list[ScrapedEvent]] = defaultdict(list)
        for ev in events:
            by_hash[ev.hash].append(ev)

        for group in by_hash.values():
            if len(group) < 2:
                continue
            times = [ev.show_time for ev in group]
            if any(t is None for t in times) or len(set(times)) != len(times):
                # Nothing to tell them apart by. Leave the titles alone and let the manager's
                # dedup drop the extras, rather than inventing a distinction that is not there.
                logger.warning(
                    f"[InstantSeats] {len(group)} listings share a date and title with no "
                    f"distinct times: {group[0].name!r} on {group[0].date}"
                )
                continue
            for ev in group:
                # %-I is not portable to Windows, where the tests also run, so strip the
                # leading zero by hand.
                label = ev.show_time.strftime("%I:%M %p").lstrip("0").lower()
                ev.name = f"{ev.name} ({label})"

        return events
