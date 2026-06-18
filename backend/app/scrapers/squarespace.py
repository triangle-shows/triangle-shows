"""Squarespace JSON API scraper for Neptune's Parlour and Moon Room."""
import logging
from datetime import datetime, date, time
from typing import Optional

import httpx

from app.scrapers.base import BaseScraper, ScrapedEvent, BROWSER_HEADERS

logger = logging.getLogger(__name__)


class SquarespaceScraper(BaseScraper):
    """Scrape events from Squarespace's JSON events endpoint.

    Squarespace sites expose /events?format=json which returns event data.
    Used by: Neptune's Parlour, Moon Room
    """

    async def scrape(self) -> list[ScrapedEvent]:
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

            # Squarespace can return events under different keys
            items = data.get("items", data.get("upcoming", data.get("events", [])))

            for item in items:
                parsed = self._parse_event(item)
                if parsed:
                    events.append(parsed)

        logger.info(f"[Squarespace] Found {len(events)} events for {self.venue_slug}")
        return events

    def _parse_event(self, item: dict) -> Optional[ScrapedEvent]:
        try:
            title = item.get("title", "").strip()
            if not title:
                return None

            exclude = self.config.get("exclude_titles", [])
            if any(title.lower() == ex.lower() for ex in exclude):
                return None

            # Squarespace uses millisecond timestamps
            start_ts = item.get("startDate")
            if not start_ts:
                return None

            # Convert from milliseconds
            if isinstance(start_ts, (int, float)):
                dt = datetime.fromtimestamp(start_ts / 1000)
            else:
                dt = datetime.fromisoformat(str(start_ts).replace("Z", "+00:00"))

            event_date = dt.date()
            show_time = dt.time().replace(tzinfo=None) if dt.time() != time(0, 0) else None

            # End date (usually same day)
            end_ts = item.get("endDate")

            # Extract body/description
            excerpt = item.get("excerpt", "") or item.get("body", "")
            if isinstance(excerpt, str):
                description = excerpt[:500].strip() or None
            else:
                description = None

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
                price_min, price_max = self.parse_price_range(title)

            # Status
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
            logger.warning(f"[Squarespace] Failed to parse event: {e}")
            return None
