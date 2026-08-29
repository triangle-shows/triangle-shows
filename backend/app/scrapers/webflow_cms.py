"""Webflow CMS scraper for venues that embed events in a CMS collection list.

Role: One of several venue-specific scraper implementations; instantiated and run
by scrapers/manager.py when the scheduler triggers POST /api/scrape every 6 hours.
Requires: httpx, beautifulsoup4/lxml, and a venue config dict with at least a 'url' key.
Currently used by: Pour House.
"""

# --- Imports ---
import logging
import re
from datetime import datetime

import httpx
from bs4 import BeautifulSoup

from app.scrapers.base import BaseScraper, ScrapedEvent, BROWSER_HEADERS

# --- Module Setup ---
logger = logging.getLogger(__name__)


# --- Scraper Class ---

class WebflowCMSScraper(BaseScraper):
    """Scrape events from a Webflow CMS collection list embedded in the page HTML.

    Used by: Pour House

    Config keys:
        url             - calendar page URL
        base_url        - base URL for constructing event links
        item_selector   - CSS selector for each event item (default: .show-collection-item)
        name_selector   - CSS selector for event name within item (default: .show-name)
        date_selector   - CSS selector for event date within item (default: .show-start-date)
        slug_selector   - CSS selector for slug within item (default: .show-slug)
        shows_path      - path prefix for event pages (default: /shows/)
        date_format     - strptime format string (default: %B %d, %Y)
        image_selector  - CSS selector for the show-flyer <img> inside a detail-page
                          link (default: img)

    Image note: the live Pour House page renders every show twice — once in the
    `.show-collection-item` list this scraper otherwise parses (name/date/slug
    only, no image anywhere inside it), and again in a separate Webflow "grid"
    list whose entries wrap an `<a href="{shows_path}<slug>">` around the flyer
    `<img>`. There is nothing to select inside item_selector's own elements, so
    the flyer is picked up by cross-referencing the two lists on slug.
    """

    async def scrape(self) -> list[ScrapedEvent]:
        """Fetch the venue's Webflow page and extract events from the CMS collection markup."""
        url = self.config.get("url", "")
        if not url:
            raise ValueError(f"No URL configured for {self.venue_slug}")

        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=BROWSER_HEADERS) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

        events = self._parse_soup(soup)

        logger.info(f"[WebflowCMS] Found {len(events)} events for {self.venue_slug}")
        return events

    def _parse_soup(self, soup: BeautifulSoup) -> list[ScrapedEvent]:
        """Extract events from an already-fetched page soup."""
        # Pull selector/format overrides from config, falling back to Pour House defaults
        base_url = self.config.get("base_url", "").rstrip("/")
        item_sel = self.config.get("item_selector", ".show-collection-item")
        name_sel = self.config.get("name_selector", ".show-name")
        date_sel = self.config.get("date_selector", ".show-start-date")
        slug_sel = self.config.get("slug_selector", ".show-slug")
        shows_path = self.config.get("shows_path", "/shows/")
        date_fmt = self.config.get("date_format", "%B %d, %Y")
        image_sel = self.config.get("image_selector", "img")

        events = []

        image_by_slug = self._build_image_map(soup, shows_path, image_sel)

        # --- Parse Each Event Item ---
        for item in soup.select(item_sel):
            name_el = item.select_one(name_sel)
            date_el = item.select_one(date_sel)
            slug_el = item.select_one(slug_sel)

            # Skip items missing required fields
            if not name_el or not date_el:
                continue

            name = name_el.get_text(strip=True)
            date_str = date_el.get_text(strip=True)
            # Slug is optional — used for the ticket URL and the flyer lookup
            slug = slug_el.get_text(strip=True) if slug_el else None

            if not name or not date_str:
                continue

            try:
                event_date = datetime.strptime(date_str, date_fmt).date()
            except ValueError:
                logger.warning(f"[WebflowCMS] Cannot parse date '{date_str}' for {self.venue_slug}")
                continue

            # Build the event detail URL only when both base URL and slug are available
            ticket_url = f"{base_url}{shows_path}{slug}" if (base_url and slug) else None

            # Extract age restriction from name prefix like "(18+) Artist Name"
            age_restriction = None
            age_match = re.match(r'^\((\d+\+)\)\s*', name)
            if age_match:
                age_restriction = age_match.group(1)
                # Strip the age prefix so the stored name is just the artist/show title
                name = name[age_match.end():]

            # Best-effort: a slug with no counterpart in the grid list (markup drift,
            # a show pulled from one list but not the other) just yields None. It
            # never raises and never drops the event.
            image_url = image_by_slug.get(slug) if slug else None

            events.append(ScrapedEvent(
                name=name,
                date=event_date,
                venue_slug=self.venue_slug,
                source="webflow_cms",
                artist=name,
                ticket_url=ticket_url,
                source_url=ticket_url,
                age_restriction=age_restriction,
                image_url=image_url,
            ))

        return events

    @staticmethod
    def _build_image_map(soup: BeautifulSoup, shows_path: str, image_sel: str) -> dict[str, str]:
        """Map each show's slug to its flyer URL, read from the page's grid list.

        Scoping by `a[href^=shows_path]` rather than by the grid list's own
        auto-generated Webflow wrapper class keeps this working through a redesign
        that renames the wrapper but keeps the detail-page link convention. Where a
        slug appears more than once, the first entry in document order wins, so the
        result is deterministic.
        """
        image_by_slug: dict[str, str] = {}
        if not shows_path:
            return image_by_slug

        for link_el in soup.select(f"a[href^='{shows_path}']"):
            href = link_el.get("href") or ""
            slug = href.rstrip("/").rsplit("/", 1)[-1]
            if not slug:
                continue

            img_el = link_el.select_one(image_sel)
            if not img_el:
                continue

            # Webflow lazy-loads: some renders carry the real URL in data-src only.
            src = img_el.get("src") or img_el.get("data-src")
            if src and slug not in image_by_slug:
                image_by_slug[slug] = src

        return image_by_slug
