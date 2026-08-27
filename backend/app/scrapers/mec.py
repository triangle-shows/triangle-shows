"""Scraper for venues running the Modern Events Calendar (MEC) WordPress plugin.

Role: Instantiated and called by the scrape manager (scrapers/manager.py) when
POST /api/scrape is triggered. Fetches an MEC-powered events listing page,
extracts structured JSON-LD schema.org Event data, and falls back to HTML
parsing if JSON-LD is absent.
Requires: httpx, beautifulsoup4/lxml; venue config must supply a "url" key.
"""

# --- Imports ---
import json
import logging
from datetime import datetime, date, time
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from app.scrapers.base import BaseScraper, ScrapedEvent, BROWSER_HEADERS
from app.scrapers.html_text import clean_html_text

# --- Module-level setup ---
logger = logging.getLogger(__name__)


# --- Scraper class ---

class MECScraper(BaseScraper):
    """Scrape events from the Modern Events Calendar WordPress plugin.

    MEC emits JSON-LD schema.org Event markup on event pages.
    Strategy:
      1. Fetch the events listing page.
      2. Extract JSON-LD events directly (MEC sometimes includes them on the index).
      3. Collect event detail links via MEC-specific selectors.
      4. Fetch each detail page and extract JSON-LD.

    Used by: Shadowbox Studio
    """

    # CSS selectors for event links on MEC listing pages.
    # Listed from most specific to least; we stop at the first one that matches.
    LINK_SELECTORS = [
        ".mec-event-title a",
        ".mec-event-article a.mec-event-title",
        ".mec-event-article h4 a",
        ".mec-event-article h3 a",
        ".mec-events-event-image a",
        "article.mec-event a[href*='/event']",
        "article.mec-event a[href*='/events/']",
        ".mec-wrap a[href*='/event']",
    ]

    async def scrape(self) -> list[ScrapedEvent]:
        """Fetch events from an MEC listing page and its linked detail pages."""
        url = self.config.get("url", "")
        if not url:
            raise ValueError(f"No URL configured for {self.venue_slug}")

        events: list[ScrapedEvent] = []
        seen_hashes: set[str] = set()  # Deduplicate across listing + detail pages

        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=BROWSER_HEADERS) as client:
            # --- Step 1: listing page ---
            resp = await client.get(url)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

            # Try JSON-LD on listing page first
            for ev in self._extract_jsonld_events(soup, source_url=url):
                if ev.hash not in seen_hashes:
                    seen_hashes.add(ev.hash)
                    events.append(ev)

            # --- Step 2: collect detail page links ---
            detail_links: list[str] = []
            for selector in self.LINK_SELECTORS:
                for tag in soup.select(selector):
                    href = tag.get("href", "")
                    # Skip anchor-only links and duplicates
                    if href and "#" not in href and href not in detail_links:
                        detail_links.append(href)
                if detail_links:
                    break  # stop at first selector that finds links

            # --- Step 3: fetch each detail page ---
            for href in detail_links:
                try:
                    detail_resp = await client.get(href)
                    detail_resp.raise_for_status()
                    detail_soup = BeautifulSoup(detail_resp.text, "lxml")
                    for ev in self._extract_jsonld_events(detail_soup, source_url=href):
                        if ev.hash not in seen_hashes:
                            seen_hashes.add(ev.hash)
                            events.append(ev)
                except Exception as e:
                    logger.warning(f"[MEC] Failed to fetch detail page {href}: {e}")

            # --- Step 4: if JSON-LD yielded nothing, fall back to HTML parsing ---
            if not events:
                logger.info(f"[MEC] No JSON-LD found; trying HTML parse for {self.venue_slug}")
                for ev in self._parse_html_events(soup, url):
                    if ev.hash not in seen_hashes:
                        seen_hashes.add(ev.hash)
                        events.append(ev)

        logger.info(f"[MEC] Found {len(events)} events for {self.venue_slug}")
        return events

    # ------------------------------------------------------------------
    # JSON-LD extraction (same pattern as TribeEventsScraper)
    # ------------------------------------------------------------------

    def _extract_jsonld_events(self, soup: BeautifulSoup, source_url: str = "") -> list[ScrapedEvent]:
        """Find all <script type='application/ld+json'> blocks and parse any Event items."""
        events = []
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.get_text())
            except (json.JSONDecodeError, TypeError):
                continue

            # JSON-LD may be a single object or an array of objects
            items = data if isinstance(data, list) else [data]
            for item in items:
                item_type = item.get("@type", "")
                # @type can be a list (e.g. ["Event", "MusicEvent"]) or a plain string
                if isinstance(item_type, list):
                    if "Event" not in item_type and "MusicEvent" not in item_type:
                        continue
                elif item_type not in ("Event", "MusicEvent"):
                    continue
                parsed = self._parse_jsonld_event(item, source_url)
                if parsed:
                    events.append(parsed)
        return events

    def _parse_jsonld_event(self, data: dict, source_url: str = "") -> Optional[ScrapedEvent]:
        """Convert a single schema.org Event dict into a ScrapedEvent; returns None on failure."""
        try:
            name = data.get("name", "").strip()
            if not name:
                return None

            start = data.get("startDate", "")
            if not start:
                return None
            try:
                if "T" in start:
                    # Full ISO datetime — extract date and time separately
                    dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                    event_date = dt.date()
                    show_time = dt.time().replace(tzinfo=None)
                    if show_time == time(0, 0):
                        # Midnight usually means no specific time was set
                        show_time = None
                else:
                    event_date = date.fromisoformat(start[:10])
                    show_time = None
            except ValueError:
                return None

            # Performers
            artist = None
            support: list[str] = []
            performers = data.get("performer", [])
            if isinstance(performers, dict):
                performers = [performers]
            elif not isinstance(performers, list):
                performers = []
            for i, p in enumerate(performers):
                p_name = p.get("name", "")
                if i == 0:
                    artist = p_name
                else:
                    support.append(p_name)
            if not artist:
                # Fall back to event title when no explicit performer is listed
                artist = name

            # Offers / price
            price_min = None
            price_max = None
            ticket_url = None
            offers = data.get("offers", {})
            # Normalize to a single offer dict if a list is provided
            if isinstance(offers, list) and offers:
                offers = offers[0]
            if isinstance(offers, dict):
                raw_min = offers.get("lowPrice") or offers.get("price")
                raw_max = offers.get("highPrice") or raw_min
                if isinstance(raw_min, str):
                    price_min = self.parse_price(raw_min)
                elif raw_min is not None:
                    price_min = float(raw_min)
                if isinstance(raw_max, str):
                    price_max = self.parse_price(raw_max)
                elif raw_max is not None:
                    price_max = float(raw_max)
                ticket_url = offers.get("url")

            # Image
            image = data.get("image", "")
            if isinstance(image, list):
                image = image[0] if image else ""
            if isinstance(image, dict):
                image = image.get("url", "")
            image_url = image or None

            # Description — MEC's JSON-LD field stores markup that has itself been through
            # an HTML-entity pass, e.g. the literal text '&lt;p&gt;&lt;strong&gt;...'
            # rather than an actual '<p>' tag, so it needs both an entity-decode and a
            # tag-strip before it is readable (#13). Truncated after cleaning — truncating
            # the raw markup first risks cutting mid-tag or mid-entity.
            description = clean_html_text(data.get("description"), limit=500)

            # Status — derive from schema eventStatus or price
            event_status = data.get("eventStatus", "")
            if "Cancelled" in event_status:
                status = "cancelled"
            elif price_min == 0:
                status = "free"
            else:
                status = "on_sale"

            return ScrapedEvent(
                name=name,
                date=event_date,
                venue_slug=self.venue_slug,
                source="mec",
                artist=artist,
                support_artists=", ".join(support) if support else None,
                show_time=show_time,
                ticket_url=ticket_url or source_url or None,
                price_min=price_min,
                price_max=price_max,
                image_url=image_url,
                status=status,
                description=description,
                source_url=source_url or data.get("url") or None,
            )
        except Exception as e:
            logger.warning(f"[MEC] Failed to parse JSON-LD event: {e}")
            return None

    # ------------------------------------------------------------------
    # HTML fallback: parse MEC listing markup directly
    # ------------------------------------------------------------------

    def _parse_html_events(self, soup: BeautifulSoup, base_url: str) -> list[ScrapedEvent]:
        """Best-effort HTML parse for MEC event listing markup."""
        events = []
        articles = soup.select("article.mec-event, .mec-event-article, li.mec-event")
        for article in articles:
            try:
                # Title
                title_tag = article.select_one(
                    ".mec-event-title a, h4.mec-event-title a, h3.mec-event-title a, .mec-event-article-title a"
                )
                if not title_tag:
                    continue
                name = title_tag.get_text(strip=True)
                event_url = title_tag.get("href") or base_url

                # Date — MEC puts start date in a time[datetime] element
                time_tag = article.select_one("time[datetime], abbr[title]")
                if not time_tag:
                    continue
                raw_date = time_tag.get("datetime") or time_tag.get("title", "")
                try:
                    event_date = date.fromisoformat(raw_date[:10])
                except (ValueError, TypeError):
                    continue

                # Time — MEC may have .mec-start-time or similar
                show_time = None
                time_text_tag = article.select_one(".mec-start-time, .mec-event-time")
                if time_text_tag:
                    show_time = self.parse_time(time_text_tag.get_text(strip=True))

                events.append(ScrapedEvent(
                    name=name,
                    date=event_date,
                    venue_slug=self.venue_slug,
                    source="mec",
                    artist=name,
                    show_time=show_time,
                    ticket_url=event_url,
                    source_url=event_url,
                    status="on_sale",
                ))
            except Exception as e:
                logger.warning(f"[MEC] HTML parse error: {e}")
        return events
