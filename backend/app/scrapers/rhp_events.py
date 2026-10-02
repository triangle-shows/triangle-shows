"""
Scraper for venues using the RHP Events WordPress plugin (rhptickets.com).

Role: One of several venue scrapers called by scrapers/manager.py during a scrape cycle,
which is triggered every 6 hours via POST /api/scrape (scheduler or Cloud Scheduler).
Covers Lincoln Theatre, Cat's Cradle, Cat's Cradle Back Room, Local 506, and The Pinhook
— all of which share the same RHP Events plugin frontend but may be filtered by venue name.
Requires: httpx, beautifulsoup4 (lxml parser), app.scrapers.base.
"""

# --- Imports ---
import logging
import re
from datetime import datetime, date, time
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from app.scrapers.base import BaseScraper, ScrapedEvent, BROWSER_HEADERS

# --- Module-level setup ---
logger = logging.getLogger(__name__)

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# "Fri, " / "Friday " / "Thurs. " ahead of the date
_WEEKDAY_PREFIX = re.compile(r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*\.?,?\s*", re.IGNORECASE)
# "Jan 08" / "January 8" / "Sept. 3"
_MONTH_DAY = re.compile(r"([A-Za-z]{3})[A-Za-z]*\.?\s+(\d{1,2})")


# --- Scraper class ---

class RHPEventsScraper(BaseScraper):
    """Scrape events from RHP Events WordPress plugin.

    Used by: Lincoln Theatre, Cat's Cradle, Cat's Cradle Back Room,
             Local 506, The Pinhook
    """

    async def scrape(self) -> list[ScrapedEvent]:
        url = self.config.get("url", "")
        if not url:
            raise ValueError(f"No URL configured for {self.venue_slug}")

        # venue_filter narrows results to a specific venue on shared RHP listing pages
        venue_filter = self.config.get("venue_filter")
        # One "today" for the whole run, so the year inference cannot straddle midnight
        today = date.today()
        events = []
        page = 1
        max_pages = 10  # Safety cap to avoid infinite pagination loops

        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=BROWSER_HEADERS) as client:
            while page <= max_pages:
                # RHP pagination follows /page/N/ URL convention
                page_url = url if page == 1 else f"{url.rstrip('/')}/page/{page}/"
                logger.info(f"[RHP] Fetching {page_url}")

                try:
                    resp = await client.get(page_url)
                    if resp.status_code == 404:
                        # 404 means we've gone past the last page
                        break
                    resp.raise_for_status()
                except httpx.HTTPStatusError:
                    break

                soup = BeautifulSoup(resp.text, "lxml")
                # Primary selectors cover the most common RHP Events plugin markup variants
                wrappers = soup.select(".eventWrapper, .rhp-event, .event-listing, article.event")

                if not wrappers:
                    # Try alternative selectors
                    wrappers = soup.select(".type-rhp_event, .rhp-events-list > div, .event-item")

                if not wrappers:
                    # No event elements found — either last page or unexpected markup
                    break

                for wrapper in wrappers:
                    parsed = self._parse_event(wrapper, venue_filter, today)
                    if parsed:
                        events.append(parsed)

                # Check for next page
                next_link = soup.select_one(".nav-next a, .next.page-numbers, a.next")
                if not next_link:
                    break
                page += 1

        logger.info(f"[RHP] Found {len(events)} events for {self.venue_slug}")
        return events

    def _parse_event(
        self, wrapper, venue_filter: Optional[str], today: Optional[date] = None
    ) -> Optional[ScrapedEvent]:
        """Parse a single event wrapper element into a ScrapedEvent, or return None if it should be skipped."""
        try:
            # --- Venue filtering ---
            # venue_filter_not excludes events at a specific sub-venue (e.g., Haw River Ballroom
            # shares a listing page with Cat's Cradle but should not appear in Cat's Cradle results)
            venue_filter_not = self.config.get("venue_filter_not")
            if venue_filter or venue_filter_not:
                venue_el = wrapper.select_one(".rhpVenueContent, .eventVenue, .event-venue, .venue-name")
                def _norm(s):
                    # Normalize curly/special apostrophes to straight apostrophe for reliable matching
                    return re.sub(r"['\u2018\u2019\u02bc\ufffd]", "'", s).lower()
                if not venue_el:
                    # Can't identify venue — skip rather than risk cross-venue duplicates
                    return None
                venue_text = venue_el.get_text(strip=True)
                if venue_filter and venue_filter.lower() not in _norm(venue_text):
                    return None
                if venue_filter_not and venue_filter_not.lower() in _norm(venue_text):
                    return None

            # --- Event name / headliner ---
            title_el = wrapper.select_one(
                ".rhp-event__title--list, .eventTitle, .event-title, .headliner"
            )
            if not title_el:
                title_el = wrapper.select_one("h2, h3")
            if not title_el:
                return None

            name = title_el.get_text(strip=True)
            if not name:
                return None

            # Get link from nearest ancestor/sibling <a>
            link = None
            link_el = wrapper.select_one("a.url[href]")
            if link_el:
                link = link_el.get("href")

            # --- Date ---
            date_el = wrapper.select_one(
                ".singleEventDate, .eventDate, .event-date, .date, time, .rhp-event-date"
            )
            event_date = None
            if date_el:
                # Try datetime attribute first — machine-readable and more reliable than display text
                dt_attr = date_el.get("datetime") or date_el.get("content")
                if dt_attr:
                    try:
                        event_date = date.fromisoformat(dt_attr[:10])
                    except ValueError:
                        pass

                if not event_date:
                    date_text = date_el.get_text(strip=True)
                    event_date = self._parse_date_text(date_text, today)

            if not event_date:
                # Without a date the event is unusable — skip it
                return None

            # --- Support artists ---
            support_el = wrapper.select_one(
                ".eventSupport, .support, .event-support, .opener, .supporting"
            )
            support = support_el.get_text(strip=True) if support_el else None

            # --- Doors / Show time ---
            doors_time = None
            show_time = None
            time_el = wrapper.select_one(
                ".eventDoorTime, .eventShowTime, .event-time, .time, .doors-show"
            )
            if time_el:
                time_text = time_el.get_text(strip=True)
                # Try to find "Doors: X / Show: Y" pattern
                doors_match = re.search(r'doors?\s*:?\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm))', time_text, re.I)
                show_match = re.search(r'show\s*:?\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm))', time_text, re.I)
                if doors_match:
                    doors_time = self.parse_time(doors_match.group(1))
                if show_match:
                    show_time = self.parse_time(show_match.group(1))
                elif not doors_match:
                    # Just a single time — treat it as show time
                    show_time = self.parse_time(time_text)

            # Also check for separate door/show elements (some RHP themes split them)
            if not doors_time:
                d_el = wrapper.select_one(".eventDoorTime, .doors-time")
                if d_el:
                    doors_time = self.parse_time(d_el.get_text(strip=True))
            if not show_time:
                s_el = wrapper.select_one(".eventShowTime, .show-time")
                if s_el:
                    show_time = self.parse_time(s_el.get_text(strip=True))

            # --- Price ---
            price_min = None
            price_max = None
            price_el = wrapper.select_one(
                ".eventCost, .event-price, .price, .ticket-price, .cost"
            )
            if price_el:
                price_text = price_el.get_text(strip=True)
                price_min, price_max = self.parse_price_range(price_text)

            # --- Status ---
            status = "on_sale"
            cta_el = wrapper.select_one(
                ".eventCTA, .event-cta, .ticket-link, .btn, a.tickets"
            )
            if cta_el:
                cta_text = cta_el.get_text(strip=True).lower()
                if "sold out" in cta_text:
                    status = "sold_out"
                elif "free" in cta_text:
                    status = "free"

            # Also infer free status from parsed price (overrides CTA check)
            if price_min == 0 and (price_max is None or price_max == 0):
                status = "free"

            # --- Ticket URL ---
            ticket_url = None
            if cta_el and cta_el.name == "a":
                ticket_url = cta_el.get("href")
            if not ticket_url and link:
                # Fall back to the event detail page if no direct ticket link is found
                ticket_url = link

            # --- Image ---
            image_url = None
            img_el = wrapper.select_one("img.eventImage, img.event-image, .event-image img, img")
            if img_el:
                image_url = img_el.get("src") or img_el.get("data-src")

            return ScrapedEvent(
                name=name,
                date=event_date,
                venue_slug=self.venue_slug,
                source="rhp_events",
                artist=name,
                support_artists=support,
                doors_time=doors_time,
                show_time=show_time,
                ticket_url=ticket_url,
                price_min=price_min,
                price_max=price_max,
                image_url=image_url,
                status=status,
                source_url=link,
            )
        except Exception as e:
            logger.warning(f"[RHP] Failed to parse event: {e}")
            return None

    # --- Date parsing helper ---

    @staticmethod
    def _parse_date_text(text: str, today: Optional[date] = None) -> Optional[date]:
        """Parse date from various text formats."""
        text = text.strip()
        # Clean common prefixes, keeping the weekday: it is what pins the year below
        weekday = None
        prefix = _WEEKDAY_PREFIX.match(text)
        if prefix:
            weekday = WEEKDAYS[prefix.group(1).lower()]
            text = text[prefix.end():].strip()
        # Try common formats
        formats = [
            "%B %d, %Y",      # January 15, 2025
            "%b %d, %Y",      # Jan 15, 2025
            "%m/%d/%Y",        # 01/15/2025
            "%m-%d-%Y",        # 01-15-2025
            "%Y-%m-%d",        # 2025-01-15
            "%A, %B %d, %Y",  # Wednesday, January 15, 2025
            "%a, %b %d, %Y",  # Wed, Jan 15, 2025
        ]
        for fmt in formats:
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        # Month and day only. This is the common case, not a fallback: the listing pages
        # render "Fri, Jan 08" with no year and no datetime attribute. Assuming the current
        # year filed every January-onward show a year early, in the past.
        month_day = _MONTH_DAY.fullmatch(text)
        if month_day:
            month = MONTHS.get(month_day.group(1).lower()[:3])
            if month:
                return BaseScraper.infer_year(
                    month, int(month_day.group(2)), today or date.today(), weekday
                )
        return None
