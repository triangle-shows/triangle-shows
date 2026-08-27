"""
Scrapes concert events from Squarespace-hosted venue sites using their internal JSON API.

Role: One of several concrete scraper implementations; instantiated and called by
scrapers/manager.py during each scrape cycle (triggered via POST /api/scrape every 6 hours).
Requires: Venue config with a "url" key pointing to a Squarespace /events?format=json endpoint.
"""

# --- Imports ---
import logging
from datetime import datetime, date, time
import pytz
from typing import Optional
from bs4 import BeautifulSoup
import httpx

from app.scrapers.base import BaseScraper, ScrapedEvent, BROWSER_HEADERS
from app.scrapers.html_text import clean_html_text

# --- Module-level setup ---

logger = logging.getLogger(__name__)


# --- Scraper class ---

class SquarespaceScraper(BaseScraper):
    """Scrape events from Squarespace's JSON events endpoint.

    Squarespace sites expose /events?format=json which returns event data.
    Used by: Neptune's Parlour, Moon Room
    """

    async def scrape(self) -> list[ScrapedEvent]:
        """Fetch all upcoming events from the configured Squarespace events feed."""
        url = self.config.get("url", "")
        if not url:
            raise ValueError(f"No URL configured for {self.venue_slug}")

        events = []

        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=BROWSER_HEADERS) as client:
            # Squarespace paginate with ?format=json&month=MM-YYYY or just returns upcoming
            resp = await client.get(url, headers={"Accept": "application/json"})
            resp.raise_for_status()

            try:
                data = resp.json()
            except Exception:
                logger.warning(f"[Squarespace] Non-JSON response from {url}")
                return []

            items = self._select_items(data)

            for item in items:
                parsed = self._parse_event(item)
                if parsed:
                    events.append(parsed)

        logger.info(f"[Squarespace] Found {len(events)} events for {self.venue_slug}")
        return events

    @staticmethod
    def _select_items(data: dict) -> list:
        """Pick the event list out of a Squarespace JSON payload.

        Which key holds the events depends on the site's template — Neptune's Parlour
        uses "items", Boom Club uses "upcoming".

        Chained with `or` rather than nested get() defaults on purpose: a default only
        applies when the key is ABSENT, so a feed carrying "items": [] would take that
        empty list and stop, never reaching the "upcoming" key holding its events. The
        venue would silently report zero events with no error anywhere.
        """
        return data.get("items") or data.get("upcoming") or data.get("events") or []

    def _extract_description(self, item: dict, title: str) -> Optional[str]:
        """Pull a human-readable description out of a Squarespace event.

        Two shapes of source content, told apart by which field they came from:

        - `excerpt`, when present, is a genuinely separate summary field: bare `<p>`
          tags with real content, never a repeat of the title (confirmed against
          Boom Club, which fills in excerpts but leaves `body` looking like an empty
          Post Body block). Its paragraphs are kept as-is.
        - `body` is the Post Body WYSIWYG editor's output, wrapped in Squarespace's
          layout markup, and its first paragraph always repeats the event's own
          title (confirmed against Neptune's Parlour) — dropped via
          `drop_first_paragraph_if` rather than a positional skip, so it only ever
          removes an actual title repeat and never a paragraph of real content that
          happens to come first (#13, #17).

        An event whose body is empty layout scaffolding with no readable text
        returns None — that is never a reason to discard the event itself (#35).
        """
        try:
            excerpt = item.get("excerpt")
            if excerpt:
                # A real excerpt is never dropped for matching the title — only
                # body's known title-repeat paragraph is.
                return clean_html_text(excerpt)
            return clean_html_text(item.get("body") or "", drop_first_paragraph_if=title)
        except Exception as e:
            # Description is optional metadata. Losing it must never lose the event, so
            # this stays outside the caller's try block for the required fields.
            logger.warning(
                f"[Squarespace] {self.venue_slug}: could not read description for "
                f"{item.get('title', '?')!r}: {e}"
            )
            return None

    def _parse_event(self, item: dict) -> Optional[ScrapedEvent]:
        """Parse a single Squarespace event dict into a ScrapedEvent, returning None on failure."""
        try:
            title = item.get("title", "").strip()
            if not title:
                return None

            # Skip events whose titles are in the venue-level exclusion list
            exclude = self.config.get("exclude_titles", [])
            if any(title.lower() == ex.lower() for ex in exclude):
                return None

            # Squarespace uses millisecond timestamps
            start_ts = item.get("startDate")
            if not start_ts:
                return None
            timezone = pytz.timezone("US/Eastern")
            # Convert from milliseconds
            if isinstance(start_ts, (int, float)):
                dt = datetime.fromtimestamp(start_ts / 1000, timezone)
            else:
                dt = datetime.fromisoformat(str(start_ts).replace("Z", "+00:00"))

            event_date = dt.date()
            # Treat midnight as "no time set" — store None so the UI doesn't show 12:00 AM
            show_time = dt.time().replace(tzinfo=None) if dt.time() != time(0, 0) else None

            # End date (usually same day)
            end_ts = item.get("endDate")

            description = self._extract_description(item, title)
            # Image
            image_url = None
            if item.get("assetUrl"):
                image_url = item["assetUrl"]
            elif item.get("systemDataVariants"):
                # Build image URL from Squarespace image system
                pass

            # URL
            source_url = item.get("fullUrl") or item.get("sourceUrl")
            if source_url and not source_url.startswith("http"):
                # Relative URL — construct from venue website
                website = self.config.get("url", "").replace("?format=json", "").replace("/events", "")
                source_url = website.rstrip("/") + source_url

            # Price parsing from title or body
            price_min = None
            price_max = None
            if description:
                price_min, price_max = self.parse_price_range(description)
            if price_min is None:
                # Fall back to scanning the title if description had no price
                price_min, price_max = self.parse_price_range(title)

            # Status — inferred from keywords in the title
            status = "on_sale"
            title_lower = title.lower()
            if "sold out" in title_lower:
                status = "sold_out"
            elif "cancelled" in title_lower or "canceled" in title_lower:
                status = "cancelled"
            elif "free" in title_lower or price_min == 0:
                status = "free"

            return ScrapedEvent(
                name=title,
                date=event_date,
                venue_slug=self.venue_slug,
                source="squarespace",
                artist=title,
                show_time=show_time,
                ticket_url=source_url,
                price_min=price_min,
                price_max=price_max,
                image_url=image_url,
                status=status,
                description=description,
                source_url=source_url,
            )
        except Exception as e:
            # Name the venue and the event: the previous message identified neither,
            # which is why four silently dropped Boom Club events went unnoticed.
            logger.warning(
                f"[Squarespace] {self.venue_slug}: failed to parse "
                f"{item.get('title', '?')!r}: {e}"
            )
            return None
