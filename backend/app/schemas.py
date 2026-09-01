"""
Pydantic response models used to serialize database ORM objects into JSON for the API.

Role: These schemas sit between the SQLAlchemy models (models.py) and the FastAPI
route handlers (main.py). Each API endpoint returns one of these models, which
controls exactly what fields are exposed to clients and how they are typed.
Requires: models.py (ORM objects are converted via from_attributes=True),
          pydantic (validated automatically by FastAPI on response).
"""

# --- Imports ---

from pydantic import BaseModel, BeforeValidator
from datetime import date, time, datetime
from typing import Annotated, Optional


# --- Shared field types ---

def _none_if_not_absolute_http_url(value):
    """Coerce a non-absolute-http(s) ``ticket_url``/``image_url`` to ``None``.

    Both fields are third-party-sourced (see
    ``app.scrapers.base.ScrapedEvent.__post_init__``, which applies the same rule at
    ingestion) and are rendered into HTML attributes by the web client. This is a
    second, independent gate at the API boundary: it covers rows written before that
    ingestion-time normalization existed, and anything that reaches the database by
    a path other than a scraper. A relative path, a ``javascript:``/``data:`` scheme,
    or a non-string value must never reach a client as-is, but a single malformed
    field must not fail the whole event, so this normalizes rather than raises.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value.lower().startswith(("http://", "https://")) else None


# A ticket_url/image_url, normalized to an absolute http(s) string or None.
OptionalHttpUrl = Annotated[Optional[str], BeforeValidator(_none_if_not_absolute_http_url)]


# --- Venue Schema ---

class VenueResponse(BaseModel):
    """Venue data returned by GET /api/venues."""
    id: int
    name: str
    slug: str
    city: str
    capacity: Optional[int] = None
    size_category: str
    website: Optional[str] = None
    scraper_type: str
    color: str  # Hex color used for calendar event styling per venue

    # Allow constructing directly from a SQLAlchemy Venue ORM instance
    model_config = {"from_attributes": True}


# --- Event Schema ---

class EventResponse(BaseModel):
    """Full event detail returned by the events list endpoint."""
    id: int
    venue_id: int
    name: str
    artist: Optional[str] = None
    support_artists: Optional[str] = None
    date: date
    doors_time: Optional[time] = None
    show_time: Optional[time] = None
    ticket_url: OptionalHttpUrl = None
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    image_url: OptionalHttpUrl = None
    genre: Optional[str] = None
    subgenre: Optional[str] = None
    status: str
    age_restriction: Optional[str] = None
    description: Optional[str] = None
    source: str
    is_live_music: bool = True  # False => karaoke/trivia/theme night/comedy/etc. (see app/classifier.py)

    # Denormalized venue fields — joined in the query so clients don't need
    # a separate /api/venues request to display venue info alongside events
    venue_name: Optional[str] = None
    venue_slug: Optional[str] = None
    venue_city: Optional[str] = None
    venue_color: Optional[str] = None

    model_config = {"from_attributes": True}


# --- FullCalendar Schema ---

class FullCalendarEvent(BaseModel):
    """Event shaped for the FullCalendar v6 JS library (GET /api/events/fullcalendar)."""
    id: int
    title: str
    start: str  # ISO datetime string expected by FullCalendar (e.g. "2025-08-01T20:00:00")
    end: Optional[str] = None
    backgroundColor: str
    borderColor: str
    textColor: str = "#ffffff"
    extendedProps: dict  # Arbitrary metadata passed through to FullCalendar event handlers


# --- Paginated Event List Schema ---

class EventListResponse(BaseModel):
    """Wrapper for paginated event results."""
    events: list[EventResponse]
    total: int
    page: int
    per_page: int
    pages: int


# --- Health Check Schema ---

class HealthResponse(BaseModel):
    """Response for GET /api/health -- reports system and scrape status."""
    status: str
    event_count: int
    venue_count: int
    last_scrape: Optional[datetime] = None  # None if no scrape has run yet
    version: Optional[str] = None
