"""Scraper for WordPress sites using the EventON calendar plugin."""

import logging
import re

import httpx
from bs4 import BeautifulSoup

from app.scrapers.base import BROWSER_HEADERS, ScrapedEvent
from app.scrapers.mec import MECScraper


logger = logging.getLogger(__name__)


class EventONScraper(MECScraper):
    """Extract EventON's embedded schema.org events from its listing page."""

    async def scrape(self) -> list[ScrapedEvent]:
        url = self.config.get("url", "")
        if not url:
            raise ValueError(f"No URL configured for {self.venue_slug}")

        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True, headers=BROWSER_HEADERS
        ) as client:
            response = await client.get(url)
            response.raise_for_status()

        soup = BeautifulSoup(response.text, "lxml")
        if soup.select_one(".ajde_evcal_calendar") is None:
            raise RuntimeError(f"EventON calendar not found for {self.venue_slug}")

        events = self._extract_jsonld_events(soup, source_url=url)
        if not events:
            raise RuntimeError(f"EventON calendar contained no parseable events for {self.venue_slug}")

        unique = {event.hash: event for event in events}
        logger.info("[EventON] Found %s events for %s", len(unique), self.venue_slug)
        return list(unique.values())

    def _parse_jsonld_event(self, data: dict, source_url: str = ""):
        normalized = dict(data)
        start = normalized.get("startDate")
        if isinstance(start, str):
            # EventON emits values such as 2026-9-5T19:00+0:00. Python's ISO
            # parser requires zero-padded month/day and a two-digit offset hour.
            match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(T.*)$", start)
            if match:
                year, month, day, remainder = match.groups()
                start = f"{year}-{int(month):02d}-{int(day):02d}{remainder}"
            start = re.sub(r"([+-])(\d):", r"\g<1>0\2:", start)
            normalized["startDate"] = start

        event_url = normalized.get("url") or source_url
        description = normalized.get("description")
        if not normalized.get("offers") and isinstance(description, str):
            link = BeautifulSoup(description, "lxml").find("a", href=True)
            if link:
                normalized["offers"] = {"url": link["href"]}

        parsed = super()._parse_jsonld_event(normalized, source_url=event_url)
        if parsed:
            parsed.source = "eventon"
            parsed.external_id = normalized.get("@id")
        return parsed
