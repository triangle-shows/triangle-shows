"""Scraper for WordPress sites using the EventON calendar plugin."""

import logging
import re

import httpx
from bs4 import BeautifulSoup

from app.scrapers.base import BROWSER_HEADERS, ScrapedEvent
from app.scrapers.mec import MECScraper


logger = logging.getLogger(__name__)

TICKET_LINK_HINTS = (
    "ticket",
    "tix",
    "admission",
    "buy now",
    "purchase",
    "eventbrite",
    "etix",
)


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
            match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(.*)$", start)
            if match:
                year, month, day, remainder = match.groups()
                start = f"{year}-{int(month):02d}-{int(day):02d}{remainder}"
            start = re.sub(r"([+-])(\d):", r"\g<1>0\2:", start)
            normalized["startDate"] = start

        event_url = normalized.get("url") or source_url
        description = normalized.get("description")
        offer = self._normalize_offer(normalized.get("offers"))
        if not offer.get("url") and isinstance(description, str):
            ticket_url = self._find_ticket_url(description)
            if ticket_url:
                offer["url"] = ticket_url
        if offer:
            normalized["offers"] = offer

        parsed = super()._parse_jsonld_event(normalized, source_url=event_url)
        if parsed:
            parsed.source = "eventon"
            parsed.external_id = normalized.get("@id")
        return parsed

    @staticmethod
    def _normalize_offer(offers) -> dict:
        """Preserve first-offer metadata while finding a URL in any offer."""
        if isinstance(offers, dict):
            return dict(offers)
        if not isinstance(offers, list):
            return {}

        offer = next((dict(item) for item in offers if isinstance(item, dict)), {})
        if not offer.get("url"):
            offer_with_url = next(
                (
                    item
                    for item in offers
                    if isinstance(item, dict) and item.get("url")
                ),
                None,
            )
            if offer_with_url:
                offer["url"] = offer_with_url["url"]
        return offer

    @staticmethod
    def _find_ticket_url(description: str) -> str | None:
        """Return a ticket-like description link without guessing from any link."""
        soup = BeautifulSoup(description, "lxml")
        for link in soup.find_all("a", href=True):
            href = link["href"].strip()
            signal = f"{link.get_text(' ', strip=True)} {href}".lower()
            if href and any(hint in signal for hint in TICKET_LINK_HINTS):
                return href
        return None
