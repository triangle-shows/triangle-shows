"""
Scraper for Motorco Music Hall that extracts events from the venue's WordPress
calendar page by regex-parsing the embedded FullCalendar JS initialization data.

Role: One of many venue scrapers run in parallel by scrapers/manager.py, which is
triggered every 6 hours via POST /api/scrape (called by the scheduler or Cloud Scheduler).
Requires: httpx (HTTP client), app.scrapers.base (BaseScraper, ScrapedEvent, BROWSER_HEADERS).
"""

# --- Imports ---

import logging
import re
from datetime import datetime, date
from typing import Optional

import httpx

from app.scrapers.base import BaseScraper, ScrapedEvent, BROWSER_HEADERS

# --- Module-level setup ---

logger = logging.getLogger(__name__)

# Each JS event object looks like:
#   { title: 'Name', start: '2026-04-03 21:00', url: 'https://...', classNames: '...' }
#
# `title` is free text, so it must be matched as a proper JS string literal rather
# than with a stop-at-the-first-quote pattern. WordPress `esc_js` backslash-escapes
# any apostrophe inside the single-quoted value, and a naive `(.+?)['"]` terminates
# at that escaped quote — truncating e.g. "This Tour Won't Save You" to
# "This Tour Won\". Match the opening quote, then a run of escaped characters
# (`\\.`) or non-quote characters, up to the matching closing quote. The captured
# value still holds its backslashes; `_parse_event` collapses them.
#
# `start` and `url` values never contain a quote, so the simpler form is sufficient
# (and clearer) for them.
_EVENT_PATTERN = re.compile(
    r'\{[^{}]*?title\s*:\s*(?P<q>[\'"])(?P<title>(?:\\.|(?!(?P=q)).)*)(?P=q)'
    r'[^{}]*?start\s*:\s*[\'"](?P<start>\d{4}-\d{2}-\d{2}[^\'\"]*)[\'"]'
    r'[^{}]*?url\s*:\s*[\'"](?P<url>[^\'\"]+)[\'"]',
    re.S,
)

# A JS string escape is a backslash followed by any single character (`\'`, `\"`,
# `\\`, `\/`). `esc_js` only ever emits a backslash as an escape introducer, so
# collapsing each pair to its second character never eats a literal backslash.
_JS_ESCAPE_PATTERN = re.compile(r"\\(.)")


# --- Scraper class ---

class MotorcoScraper(BaseScraper):
    """Scrape events from Motorco Music Hall's WordPress site.

    The calendar page embeds all events directly in the FullCalendar JS init
    as a JS array (single-quoted keys, not valid JSON). We extract each event
    using per-field regex instead of JSON parsing.

    Used by: Motorco Music Hall
    """

    async def scrape(self) -> list[ScrapedEvent]:
        """Fetch the Motorco calendar page and return upcoming ScrapedEvent objects."""
        url = self.config.get("url", "https://motorcomusic.com/calendar/")
        events = []
        today = date.today()

        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=BROWSER_HEADERS) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text

        # Deduplicate by (title, start). `finditer` yields non-overlapping matches,
        # so this guards against the same event genuinely appearing twice in the
        # page markup rather than against overlapping regex matches.
        seen = set()
        for m in _EVENT_PATTERN.finditer(html):
            raw_title, raw_start, raw_url = m.group("title"), m.group("start"), m.group("url")

            # Skip an event object we have already seen
            key = (raw_title, raw_start)
            if key in seen:
                continue
            seen.add(key)

            parsed = self._parse_event(raw_title, raw_start, raw_url, today)
            if parsed:
                events.append(parsed)

        logger.info(f"[Motorco] Found {len(events)} upcoming events for {self.venue_slug}")
        return events

    def _parse_event(self, title: str, start_str: str, url: str, today: date) -> Optional[ScrapedEvent]:
        """Parse raw JS-extracted strings into a ScrapedEvent, or return None on failure."""
        try:
            # Collapse JS string escapes (esc_js turns an apostrophe into \'),
            # then unescape HTML entities in title
            title = _JS_ESCAPE_PATTERN.sub(r"\1", title)
            title = title.replace("&#038;", "&").replace("&amp;", "&").replace("&#8217;", "'")

            # Parse datetime — format is "2026-04-03 21:00" or "2026-04-03"
            dt = None
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(start_str.strip(), fmt)
                    break
                except ValueError:
                    continue

            if not dt:
                return None

            event_date = dt.date()
            if event_date < today:
                return None  # Skip past events

            # Only record show_time if a non-midnight time was actually specified
            show_time = dt.time() if dt.hour != 0 or dt.minute != 0 else None

            return ScrapedEvent(
                name=title,
                date=event_date,
                venue_slug=self.venue_slug,
                source="motorco",
                artist=title,
                show_time=show_time,
                ticket_url=url,
                source_url=url,
            )
        except Exception as e:
            logger.warning(f"[Motorco] Failed to parse event '{title}': {e}")
            return None
