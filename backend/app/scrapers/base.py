"""
Abstract base class and shared data structures for all venue scrapers.

Role: Defines the ScrapedEvent dataclass (the unit of data each scraper returns)
and BaseScraper ABC (the interface every venue scraper must implement). The scrape
manager (scrapers/manager.py) imports BaseScraper subclasses, calls their scrape()
method, and uses ScrapedEvent.hash for deduplication before upserting to PostgreSQL.
Requires: No env vars or external services — pure Python stdlib only.
"""
import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, time, datetime
from typing import Optional


# --- ScrapedEvent Dataclass ---

@dataclass
class ScrapedEvent:
    # Required fields — every scraper must supply these
    name: str
    date: date
    venue_slug: str
    source: str
    # Optional fields — scrapers fill in what their venue page exposes
    external_id: Optional[str] = None
    artist: Optional[str] = None
    support_artists: Optional[str] = None
    doors_time: Optional[time] = None
    show_time: Optional[time] = None
    ticket_url: Optional[str] = None
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    image_url: Optional[str] = None
    genre: Optional[str] = None
    subgenre: Optional[str] = None
    status: str = "on_sale"
    age_restriction: Optional[str] = None
    description: Optional[str] = None
    source_url: Optional[str] = None

    @property
    def hash(self) -> str:
        """Generate dedup hash from venue_slug + date + normalized name.

        Strip the words 'box'/'boxes' before normalizing so DPAC events
        like 'Piano Box Series' and 'Piano Boxes Series' collapse to one hash.
        """
        name = re.sub(r'\b(box|boxes)\b', '', self.name, flags=re.IGNORECASE)
        normalized = re.sub(r'[^a-z0-9]', '', name.lower().strip())
        raw = f"{self.venue_slug}|{self.date.isoformat()}|{normalized}"
        return hashlib.sha256(raw.encode()).hexdigest()


# --- Shared HTTP Headers ---

# Mimic a real browser so venue sites don't block the scraper as a bot
BROWSER_HEADERS = {
    "User-Agent": (
        "curl/8.5.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


# --- BaseScraper ABC ---

class BaseScraper(ABC):
    """Abstract base class for all venue scrapers."""

    def __init__(self, venue_slug: str, config: Optional[dict] = None):
        self.venue_slug = venue_slug
        # config allows per-venue overrides (e.g. custom URLs, feature flags)
        self.config = config or {}

    @abstractmethod
    async def scrape(self) -> list[ScrapedEvent]:
        """Scrape events and return a list of ScrapedEvent objects."""
        ...

    # --- Parsing Helpers ---
    # Shared utility methods so individual scrapers don't duplicate price/time logic

    @staticmethod
    def parse_price(text: str) -> Optional[float]:
        """Extract a numeric price from text like '$15.00', 'Free', '$20'."""
        if not text:
            return None
        text = text.strip().lower()
        if text in ("free", "free!", "no cover", "$0", "$0.00"):
            return 0.0
        match = re.search(r'\$?\s*(\d+(?:\.\d{2})?)', text)
        if match:
            return float(match.group(1))
        return None

    @staticmethod
    def parse_price_range(text: str) -> tuple[Optional[float], Optional[float]]:
        """Parse price range like '$15-$25', '$20', 'Free'."""
        if not text:
            return None, None
        text = text.strip().lower()
        if any(free_words in text for free_words in ["free", "free!", "no cover"]):
            return 0.0, 0.0

        prices = re.findall(r'[$€]{1}(?P<amount>[\d,\.]+(?>\.\d{2}){0,})\b', text)
        if len(prices) >= 2:
            return float(prices[0]), float(prices[1])
        elif len(prices) == 1:
            # Single price — treat it as both min and max
            p = float(prices[0])
            return p, p
        return None, None

    @staticmethod
    def parse_time(text: str) -> Optional[time]:
        """Parse time from various formats: '7 pm', '8:30 PM', '19:00', '7pm'."""
        if not text:
            return None
        text = text.strip().lower().replace('.', '')
        # Try HH:MM AM/PM
        match = re.search(r'(\d{1,2}):(\d{2})\s*(am|pm)', text)
        if match:
            h, m, ampm = int(match.group(1)), int(match.group(2)), match.group(3)
            if ampm == 'pm' and h != 12:
                h += 12
            elif ampm == 'am' and h == 12:
                h = 0
            return time(h, m)
        # Try H AM/PM (no minutes)
        match = re.search(r'(\d{1,2})\s*(am|pm)', text)
        if match:
            h, ampm = int(match.group(1)), match.group(2)
            if ampm == 'pm' and h != 12:
                h += 12
            elif ampm == 'am' and h == 12:
                h = 0
            return time(h, 0)
        # Try 24-hour HH:MM
        match = re.search(r'(\d{1,2}):(\d{2})', text)
        if match:
            h, m = int(match.group(1)), int(match.group(2))
            if 0 <= h <= 23 and 0 <= m <= 59:
                return time(h, m)
        return None

    @staticmethod
    def normalize_name(name: str) -> str:
        """Normalize event/artist name for comparison."""
        return re.sub(r'\s+', ' ', name.strip()) #this is essentially dead code, only used by ticketmaster
