"""
SQLAlchemy ORM models defining the database schema for Venue, Event, and ScrapeLog.

Role: Shared data layer — imported by database.py (Base), scrapers/manager.py (upsert
logic), and API route handlers. Migrations are managed by Alembic using these definitions.
Requires: app.database (Base), PostgreSQL via asyncpg/SQLAlchemy async.
"""

# --- Imports ---

from datetime import datetime, date, time
from typing import Optional
from sqlalchemy import String, Integer, Float, Text, Date, Time, DateTime, ForeignKey, JSON, Boolean, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
import enum

from app.database import Base


# --- Enums ---

class EventStatus(str, enum.Enum):
    """Possible ticket/availability states for an event."""
    on_sale = "on_sale"
    sold_out = "sold_out"
    cancelled = "cancelled"
    free = "free"


class ScrapeStatus(str, enum.Enum):
    """Lifecycle states written to ScrapeLog during and after a scrape run."""
    running = "running"
    success = "success"
    failed = "failed"


# --- Models ---

class Venue(Base):
    """A physical concert venue in the Triangle area."""
    __tablename__ = "venues"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)  # URL-safe identifier, e.g. "cats-cradle"
    city: Mapped[str] = mapped_column(String(50))
    capacity: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    size_category: Mapped[str] = mapped_column(String(20))  # small, medium, large
    website: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    ticketmaster_venue_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # used by Ticketmaster scraper
    scraper_type: Mapped[str] = mapped_column(String(50))  # selects which scraper class to use
    scraper_config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # extra params passed to the scraper (e.g. API keys, URL overrides)
    color: Mapped[str] = mapped_column(String(7), default="#6366f1")  # hex color shown on the calendar
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    events: Mapped[list["Event"]] = relationship(back_populates="venue", cascade="all, delete-orphan")
    scrape_logs: Mapped[list["ScrapeLog"]] = relationship(back_populates="venue", cascade="all, delete-orphan")
    # Retiring a venue must not be blocked by its series rules. seed_venues() deletes
    # venues listed in REMOVED_SLUGS on every startup via session.delete(), which cascades
    # only through relationships SQLAlchemy knows about — so without this, the first
    # retirement of a venue holding a series override raises ForeignKeyViolation inside the
    # lifespan handler and the container never becomes healthy.
    series_overrides: Mapped[list["SeriesOverride"]] = relationship(
        back_populates="venue", cascade="all, delete-orphan"
    )


class Event(Base):
    """A single concert or show at a venue."""
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)  # ID from the source system (e.g. Ticketmaster event ID)
    venue_id: Mapped[int] = mapped_column(ForeignKey("venues.id"), index=True)
    name: Mapped[str] = mapped_column(String(500))
    artist: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    support_artists: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    doors_time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    show_time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    ticket_url: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    price_min: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price_max: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    image_url: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    genre: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    subgenre: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=EventStatus.on_sale.value)
    age_restriction: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(50))  # scraper name that produced this event, e.g. "ticketmaster"
    source_url: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # SHA-256 of key fields; used by manager.py to deduplicate on upsert

    # --- Live-music classification (see app/classifier.py) ---
    # Effective flag the calendar and iCal feeds filter on. Materialized (not computed
    # on read) so filtering is a simple `WHERE is_live_music = true`. Recomputed after
    # every scrape by ScrapeManager.reclassify_all() — except on rows an admin has
    # manually overridden (is_manual_override=True), which are left untouched.
    is_live_music: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_manual_override: Mapped[bool] = mapped_column(Boolean, default=False)  # True once an admin sets the flag by hand
    # True when an admin created this row by hand rather than a scraper finding it.
    #
    # Distinct from is_manual_override above, which records a hand-set live-music
    # *verdict*. A row can be manually created and auto-classified, or scraped and
    # manually overridden; conflating the two makes either signal unreadable.
    #
    # Load-bearing for reconcile: plan_upsert() deletes any row inside the scraped date
    # window that the scrape did not return, and a hand-added event never is. Nothing in
    # the scraper path writes this flag — deliberately, because _apply_scraped() does
    # write `source`, so filtering orphans on source would lose the protection the first
    # time a venue listed a show that had already been added by hand.
    is_manually_created: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    classification_reason: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)  # human-readable why, e.g. "recurring: 6 dates", "keyword: trivia", "manual"
    # Set when an admin confirms the current classification is correct; the admin list
    # hides approved events by default. Records agreement with a *verdict*, not the
    # event: reclassify_all() clears this whenever it changes is_live_music, so a
    # re-classified event returns to the review queue rather than staying hidden.
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    venue: Mapped["Venue"] = relationship(back_populates="events")


class ScrapeLog(Base):
    """Audit record written by the scrape manager for each venue scrape attempt."""
    __tablename__ = "scrape_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    venue_id: Mapped[int] = mapped_column(ForeignKey("venues.id"), index=True)
    scraper_type: Mapped[str] = mapped_column(String(50))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # null while still running
    status: Mapped[str] = mapped_column(String(20), default=ScrapeStatus.running.value)
    events_found: Mapped[int] = mapped_column(Integer, default=0)   # total events returned by the scraper
    events_created: Mapped[int] = mapped_column(Integer, default=0)  # net-new events inserted
    events_updated: Mapped[int] = mapped_column(Integer, default=0)  # existing events that were updated
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # populated on failure
    duration_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    venue: Mapped["Venue"] = relationship(back_populates="scrape_logs")


class SeriesOverride(Base):
    """An admin decision that applies to a whole recurring series, not one event.

    A recurring series (weekly karaoke, a standing jazz jam, a monthly comedy
    showcase, ...) produces many Event rows over time, and new ones appear on each
    scrape. A per-event override can't cover future instances, so a series-level
    rule is keyed by (venue_id, normalized_name) — the same key the recurrence
    detector groups on (see app.classifier.normalize_series_name). During
    ScrapeManager.reclassify_all(), a matching rule forces is_live_music for every
    event in the series, including instances scraped later.

    Precedence, most specific first: a per-event Event.is_manual_override wins over
    a SeriesOverride, which in turn wins over automatic classification.
    """
    __tablename__ = "series_overrides"

    id: Mapped[int] = mapped_column(primary_key=True)
    # ondelete="CASCADE" as well as the ORM relationship on Venue: the relationship covers
    # session.delete(), the database constraint covers everything else (a manual DELETE, a
    # future bulk query that bypasses the ORM's cascade).
    venue_id: Mapped[int] = mapped_column(
        ForeignKey("venues.id", ondelete="CASCADE"), index=True
    )
    normalized_name: Mapped[str] = mapped_column(String(200))  # matches app.classifier.normalize_series_name(event.name)
    display_name: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)  # a readable example name for the admin UI
    is_live_music: Mapped[bool] = mapped_column(Boolean)  # the value forced onto every event in the series
    note: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)  # optional admin note explaining the decision
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    venue: Mapped["Venue"] = relationship(back_populates="series_overrides")

    # One rule per (venue, series name); the admin endpoint upserts on this key.
    __table_args__ = (
        UniqueConstraint("venue_id", "normalized_name", name="uq_series_override_key"),
    )
