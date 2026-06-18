"""ScrapeManager: orchestrates all scrapers, upserts events, logs results."""
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.models import Venue, Event, ScrapeLog
from app.scrapers.base import BaseScraper, ScrapedEvent
from app.scrapers.ticketmaster import TicketmasterScraper

logger = logging.getLogger(__name__)


class ScrapeManager:
    """Orchestrates scraping for all venues."""

    def __init__(self, session: AsyncSession):
        self.session = session

    def _get_scraper(self, venue: Venue) -> Optional[BaseScraper]:
        """Instantiate the correct scraper for a venue."""
        if venue.scraper_type == "ticketmaster":
            if not venue.ticketmaster_venue_id:
                logger.warning(f"No TM venue ID for {venue.slug}")
                return None
            return TicketmasterScraper(
                venue_slug=venue.slug,
                venue_tm_id=venue.ticketmaster_venue_id,
                api_key=settings.TICKETMASTER_API_KEY,
                config=venue.scraper_config,
            )
        elif venue.scraper_type == "rhp_events":
            from app.scrapers.rhp_events import RHPEventsScraper
            return RHPEventsScraper(venue.slug, venue.scraper_config)
        elif venue.scraper_type == "tribe_events":
            from app.scrapers.tribe_events import TribeEventsScraper
            return TribeEventsScraper(venue.slug, venue.scraper_config)
        elif venue.scraper_type == "squarespace":
            from app.scrapers.squarespace import SquarespaceScraper
            return SquarespaceScraper(venue.slug, venue.scraper_config)
        elif venue.scraper_type == "eventprime":
            from app.scrapers.eventprime import EventPrimeScraper
            return EventPrimeScraper(venue.slug, venue.scraper_config)
        elif venue.scraper_type == "motorco":
            from app.scrapers.motorco import MotorcoScraper
            return MotorcoScraper(venue.slug, venue.scraper_config)
        elif venue.scraper_type == "carolina_theatre":
            from app.scrapers.carolina_theatre import CarolinaTheatreScraper
            return CarolinaTheatreScraper(venue.slug, venue.scraper_config)
        elif venue.scraper_type == "venuepilot":
            from app.scrapers.venuepilot import VenuePilotScraper
            return VenuePilotScraper(venue.slug, venue.scraper_config)
        elif venue.scraper_type == "koka_booth":
            from app.scrapers.koka_booth import KokaBoothScraper
            return KokaBoothScraper(venue.slug, venue.scraper_config)
        elif venue.scraper_type == "mec":
            from app.scrapers.mec import MECScraper
            return MECScraper(venue.slug, venue.scraper_config)
        elif venue.scraper_type == "webflow_cms":
            from app.scrapers.webflow_cms import WebflowCMSScraper
            return WebflowCMSScraper(venue.slug, venue.scraper_config)
        elif venue.scraper_type == "tickpick_organizer":
            from app.scrapers.tickpick_organizer import TickPickOrganizerScraper
            return TickPickOrganizerScraper(venue.slug, venue.scraper_config)
        else:
            logger.warning(f"Unknown scraper type: {venue.scraper_type}")
            return None

    async def scrape_venue(self, venue: Venue) -> dict:
        """Scrape a single venue and upsert events."""
        # Refresh venue before accessing any attributes. If a previous scrape_venue call
        # ended in a rollback, all ORM objects in the session are expired; accessing an
        # expired attribute on an AsyncSession triggers a sync lazy-load → greenlet error.
        await self.session.refresh(venue)
        venue_slug = venue.slug
        venue_id = venue.id
        scraper_type = venue.scraper_type

        log = ScrapeLog(
            venue_id=venue_id,
            scraper_type=scraper_type,
            started_at=datetime.utcnow(),
        )
        try:
            self.session.add(log)
            await self.session.flush()

            scraper = self._get_scraper(venue)
            if not scraper:
                raise ValueError(f"No scraper available for {venue_slug}")

            scraped_events = await scraper.scrape()
            created, updated = await self._upsert_events(venue_id, scraped_events)

            log.status = "success"
            log.events_found = len(scraped_events)
            log.events_created = created
            log.events_updated = updated
            log.finished_at = datetime.utcnow()
            log.duration_seconds = (log.finished_at - log.started_at).total_seconds()
            await self.session.commit()

            logger.info(
                f"[{venue_slug}] Scrape complete: {len(scraped_events)} found, "
                f"{created} created, {updated} updated"
            )
            return {
                "venue": venue_slug,
                "status": "success",
                "found": len(scraped_events),
                "created": created,
                "updated": updated,
            }

        except Exception as e:
            logger.error(f"[{venue_slug}] Scrape failed: {e}")
            try:
                await self.session.rollback()
                log.status = "failed"
                log.error_message = str(e)[:2000]
                log.finished_at = datetime.utcnow()
                log.duration_seconds = (log.finished_at - log.started_at).total_seconds()
                self.session.add(log)
                await self.session.commit()
            except Exception as log_err:
                logger.warning(f"[{venue_slug}] Could not write error log: {log_err}")
            return {"venue": venue_slug, "status": "failed", "error": str(e)}

    async def _upsert_events(self, venue_id: int, scraped_events: list[ScrapedEvent]) -> tuple[int, int]:
        """Upsert events using hash-based dedup. Returns (created, updated) counts."""
        if not scraped_events:
            return 0, 0

        # Deduplicate scraped events by hash (sites sometimes list the same event
        # twice — e.g. a featured section + main listing — which would cause a
        # UniqueViolationError when both end up in the same INSERT flush batch.
        seen: dict[str, ScrapedEvent] = {}
        for se in scraped_events:
            seen.setdefault(se.hash, se)
        scraped_events = list(seen.values())

        # Fetch all matching existing events in one query instead of one per event.
        hashes = [se.hash for se in scraped_events]
        result = await self.session.execute(
            select(Event).where(Event.hash.in_(hashes))
        )
        existing_by_hash = {e.hash: e for e in result.scalars().all()}

        created = 0
        updated = 0

        for se in scraped_events:
            existing = existing_by_hash.get(se.hash)

            if existing:
                # Update mutable fields
                existing.price_min = se.price_min
                existing.price_max = se.price_max
                existing.status = se.status
                existing.image_url = se.image_url or existing.image_url
                existing.ticket_url = se.ticket_url or existing.ticket_url
                existing.doors_time = se.doors_time or existing.doors_time
                existing.show_time = se.show_time or existing.show_time
                existing.support_artists = se.support_artists or existing.support_artists
                existing.genre = se.genre or existing.genre
                existing.subgenre = se.subgenre or existing.subgenre
                existing.age_restriction = se.age_restriction or existing.age_restriction
                existing.description = se.description or existing.description
                existing.updated_at = datetime.utcnow()
                updated += 1
            else:
                event = Event(
                    external_id=se.external_id,
                    venue_id=venue_id,
                    name=se.name,
                    artist=se.artist,
                    support_artists=se.support_artists,
                    date=se.date,
                    doors_time=se.doors_time,
                    show_time=se.show_time,
                    ticket_url=se.ticket_url,
                    price_min=se.price_min,
                    price_max=se.price_max,
                    image_url=se.image_url,
                    genre=se.genre,
                    subgenre=se.subgenre,
                    status=se.status,
                    age_restriction=se.age_restriction,
                    description=se.description,
                    source=se.source,
                    source_url=se.source_url,
                    hash=se.hash,
                )
                self.session.add(event)
                created += 1

        await self.session.flush()
        return created, updated

    async def scrape_all(self, scraper_types: Optional[list[str]] = None) -> list[dict]:
        """Scrape all venues (or those matching given scraper_types)."""
        query = select(Venue)
        if scraper_types:
            query = query.where(Venue.scraper_type.in_(scraper_types))
        result = await self.session.execute(query)
        venues = result.scalars().all()

        results = []
        for venue in venues:
            r = await self.scrape_venue(venue)
            results.append(r)

        return results

    async def scrape_ticketmaster(self) -> list[dict]:
        """Scrape only Ticketmaster venues."""
        return await self.scrape_all(scraper_types=["ticketmaster"])

    async def scrape_indie(self) -> list[dict]:
        """Scrape only non-Ticketmaster venues."""
        query = select(Venue).where(Venue.scraper_type != "ticketmaster")
        result = await self.session.execute(query)
        venues = result.scalars().all()

        results = []
        for venue in venues:
            r = await self.scrape_venue(venue)
            results.append(r)
        return results
