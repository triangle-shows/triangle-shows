"""Scraper for the Arkon Bar Manager WordPress calendar used by Slim's."""

import logging
from datetime import date
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from app.scrapers.base import BaseScraper, BROWSER_HEADERS, ScrapedEvent
from app.scrapers.html_text import clean_html_text


logger = logging.getLogger(__name__)


class ArkonBarManagerScraper(BaseScraper):
    """Parse the initial ABM calendar and follow its cursor-based pagination."""

    MAX_PAGES = 20

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
            calendar = soup.select_one(".abm-calendar")
            if calendar is None:
                logger.warning("[ABM] Calendar not found for %s", self.venue_slug)
                return []

            events = self._parse_events(calendar, url)
            ajax_url = calendar.get("data-ajax", "")
            cursor = calendar.get("data-cursor", "")
            count = calendar.get("data-more", "10")
            last_month = calendar.get("data-last-month", "")
            category = calendar.get("data-category", "")

            for _ in range(self.MAX_PAGES):
                if not ajax_url or not cursor:
                    break
                page = await client.post(
                    urljoin(url, ajax_url),
                    data={
                        "action": "abm_load_events",
                        "cursor": cursor,
                        "count": count,
                        "last_month": last_month,
                        "category": category,
                    },
                )
                page.raise_for_status()
                payload = page.json()
                data = payload.get("data", {}) if payload.get("success") else {}
                if not data:
                    break

                events.extend(
                    self._parse_events(BeautifulSoup(data.get("html", ""), "lxml"), url)
                )
                if not data.get("has_more"):
                    break

                next_cursor = data.get("cursor", "")
                if not next_cursor or next_cursor == cursor:
                    logger.warning("[ABM] Pagination cursor did not advance for %s", self.venue_slug)
                    break
                cursor = next_cursor
                last_month = data.get("last_month", last_month)
            else:
                logger.warning(
                    "[ABM] Stopped %s pagination after the %s-page safety limit",
                    self.venue_slug,
                    self.MAX_PAGES,
                )

        unique = {event.hash: event for event in events}
        logger.info("[ABM] Found %s events for %s", len(unique), self.venue_slug)
        return list(unique.values())

    def _parse_events(self, soup: BeautifulSoup, base_url: str) -> list[ScrapedEvent]:
        events = []
        for article in soup.select("article.abm-event[data-date]"):
            title = article.select_one(".abm-event-title a")
            if title is None:
                continue
            try:
                event_date = date.fromisoformat(article.get("data-date", ""))
            except ValueError:
                continue

            name = title.get_text(" ", strip=True)
            if not name:
                continue
            event_url = urljoin(base_url, title.get("href", ""))
            time_element = article.select_one(".abm-meta-time > span")
            time_text = time_element.get_text(" ", strip=True) if time_element else ""
            blurb = article.select_one(".abm-event-blurb")
            image = article.select_one(".abm-event-flyer img")

            events.append(
                ScrapedEvent(
                    name=name,
                    date=event_date,
                    venue_slug=self.venue_slug,
                    source="arkon_bar_manager",
                    artist=name,
                    show_time=self.parse_time(time_text),
                    ticket_url=event_url,
                    image_url=urljoin(base_url, image.get("src", "")) if image else None,
                    description=clean_html_text(blurb.get_text(" ", strip=True)) if blurb else None,
                    source_url=event_url,
                    status="on_sale",
                )
            )
        return events
