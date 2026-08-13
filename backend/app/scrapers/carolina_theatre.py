"""
Scraper for Carolina Theatre Durham, which uses a custom WordPress theme with
server-rendered event cards (no JS required).

Role: Instantiated and called by scrapers/manager.py during each scrape cycle,
triggered every 6 hours via POST /api/scrape (Cloud Scheduler or internal APScheduler).
Requires: The venue row for 'carolina-theatre' must exist in the DB (seeded on startup).
          No API key needed — fetches the public events page directly.
"""

# --- Imports ---
import logging
import re
from datetime import datetime, date
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from app.scrapers.base import BaseScraper, ScrapedEvent, BROWSER_HEADERS

# --- Module-level setup ---

logger = logging.getLogger(__name__)

# Month abbreviations as used by the site
MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


# --- Scraper class ---

class CarolinaTheatreScraper(BaseScraper):
    """Scrape events from Carolina Theatre Durham's website.

    The events page renders event cards server-side with CSS classes:
      - Container:  div.card.eventCard
      - Date box:   div.event__dateBox > span.day + span.month
      - Title:      p.card__title
      - Time:       p containing <i class="far fa-clock"> followed by time text
      - Image:      div.eventCard__image > img
      - Link:       the wrapping <a href="..."> inside the card

    Used by: Carolina Theatre
    """

    async def scrape(self) -> list[ScrapedEvent]:
        """Fetch the events listing page and return deduplicated ScrapedEvent objects."""
        url = self.config.get("url", "https://carolinatheatre.org/events/")
        events = []

        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=BROWSER_HEADERS) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

        # Each event on the listing page is wrapped in a div.eventCard
        for card in soup.select("div.eventCard"):
            parsed = self._parse_card(card)
            if parsed:
                events.append(parsed)

        # Deduplicate by hash in case the same event appears more than once on the page
        seen = set()
        unique = []
        for ev in events:
            if ev.hash not in seen:
                seen.add(ev.hash)
                unique.append(ev)

        logger.info(f"[CarolinaTheatre] Found {len(unique)} events for {self.venue_slug}")
        return unique

    def _parse_card(self, card) -> Optional[ScrapedEvent]:
        """Extract event fields from a single eventCard element; returns None on any failure."""
        try:
            if -1 == (card.attrs['data-filterme'].find('music')):
                return None
            # URL — the wrapping <a> inside the card
            a = card.select_one("a[href]")
            if not a:
                return None
            event_url = a.get("href", "")

            # Title
            title_el = card.select_one("p.card__title")
            if not title_el:
                return None
            name = title_el.get_text(strip=True)
            if not name:
                return None

            # Date — "26" + "Feb" → date object
            day_el = card.select_one(".event__dateBox .day")
            month_el = card.select_one(".event__dateBox .month")
            if not day_el or not month_el:
                return None

            event_date = self._parse_day_month(day_el.get_text(strip=True), month_el.get_text(strip=True))
            if not event_date:
                return None

            # Time — find <p> containing the clock icon
            show_time = None
            for p in card.select("div.card__info p"):
                if p.select_one("i.fa-clock"):
                    # Text is everything in the <p> after the <i>
                    time_text = p.get_text(strip=True)
                    show_time = self.parse_time(time_text)
                    break

            # Image
            img_el = card.select_one(".eventCard__image img")
            image_url = img_el.get("src") if img_el else None

            return ScrapedEvent(
                name=name,
                date=event_date,
                venue_slug=self.venue_slug,
                source="carolina_theatre",
                artist=name,
                show_time=show_time,
                ticket_url=event_url or None,
                image_url=image_url or None,
                source_url=event_url or None,
            )
        except Exception as e:
            logger.warning(f"[CarolinaTheatre] Card parse error: {e}")
            return None

    # --- Helpers ---

    @staticmethod
    def _parse_day_month(day_str: str, month_str: str) -> Optional[date]:
        """Convert e.g. '26' + 'Feb' into a date, inferring the year."""
        try:
            day = int(day_str.strip())
            month_key = month_str.strip().lower()[:3]
            month = MONTHS.get(month_key)
            if not month:
                return None

            today = date.today()
            year = today.year
            candidate = date(year, month, day)
            # If the date is in the past by more than a week, bump to next year
            if (candidate - today).days < -7:
                candidate = date(year + 1, month, day)
            return candidate
        except (ValueError, TypeError):
            return None
