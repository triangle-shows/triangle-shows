from pydantic import BaseModel
from datetime import date, time, datetime
from typing import Optional


class VenueResponse(BaseModel):
    id: int
    name: str
    slug: str
    city: str
    capacity: Optional[int] = None
    size_category: str
    website: Optional[str] = None
    scraper_type: str
    color: str

    model_config = {"from_attributes": True}


class EventResponse(BaseModel):
    id: int
    venue_id: int
    name: str
    artist: Optional[str] = None
    support_artists: Optional[str] = None
    date: date
    doors_time: Optional[time] = None
    show_time: Optional[time] = None
    ticket_url: Optional[str] = None
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    image_url: Optional[str] = None
    genre: Optional[str] = None
    subgenre: Optional[str] = None
    status: str
    age_restriction: Optional[str] = None
    description: Optional[str] = None
    source: str

    venue_name: Optional[str] = None
    venue_slug: Optional[str] = None
    venue_city: Optional[str] = None
    venue_color: Optional[str] = None

    model_config = {"from_attributes": True}


class FullCalendarEvent(BaseModel):
    id: int
    title: str
    start: str  # ISO datetime
    end: Optional[str] = None
    backgroundColor: str
    borderColor: str
    textColor: str = "#ffffff"
    extendedProps: dict


class EventListResponse(BaseModel):
    events: list[EventResponse]
    total: int
    page: int
    per_page: int
    pages: int


class HealthResponse(BaseModel):
    status: str
    event_count: int
    venue_count: int
    last_scrape: Optional[datetime] = None
    version: Optional[str] = None
