"""Seed venue data into the database."""
import asyncio
import logging
from sqlalchemy import select
from app.database import async_session, init_db
from app.models import Venue

logger = logging.getLogger(__name__)

VENUES = [
    # Phase 1: Ticketmaster venues
    {
        "name": "Koka Booth Amphitheatre",
        "slug": "koka-booth",
        "city": "Raleigh",
        "capacity": 7000,
        "size_category": "large",
        "website": "https://www.boothamphitheatre.com/",
        "ticketmaster_venue_id": "KovZpZAIAnkA",
        "scraper_type": "ticketmaster",
        "color": "#5a2892",  # amethyst
    },
    {
        "name": "Red Hat Amphitheater",
        "slug": "red-hat",
        "city": "Raleigh",
        "capacity": 6000,
        "size_category": "large",
        "website": "https://redhatamphitheater.com/",
        "ticketmaster_venue_id": "KovZpZAdEEvA",
        "scraper_type": "ticketmaster",
        "color": "#8b2d3c",  # ruby (Raleigh)
    },
    {
        "name": "DPAC",
        "slug": "dpac",
        "city": "Durham",
        "capacity": 2700,
        "size_category": "large",
        "website": "https://www.dpacnc.com/",
        "ticketmaster_venue_id": "KovZpa2X8e",
        "scraper_type": "ticketmaster",
        "color": "#1e428a",  # sapphire (Durham)
    },
    {
        "name": "The Ritz",
        "slug": "the-ritz",
        "city": "Raleigh",
        "capacity": 1150,
        "size_category": "medium",
        "website": "https://www.rfraleigh.com/the-ritz/",
        "ticketmaster_venue_id": "KovZpZAJIedA",
        "scraper_type": "ticketmaster",
        "color": "#8c3820",  # garnet (Raleigh)
    },
    # Phase 2: Indie venues
    {
        "name": "Lincoln Theatre",
        "slug": "lincoln-theatre",
        "city": "Raleigh",
        "capacity": 750,
        "size_category": "medium",
        "website": "https://www.lincolntheatre.com/",
        "scraper_type": "rhp_events",
        "scraper_config": {"url": "https://www.lincolntheatre.com/events/"},
        "color": "#7a2040",  # deep crimson (Raleigh)
    },
    {
        "name": "Cat's Cradle",
        "slug": "cats-cradle",
        "city": "Chapel Hill-Carrboro",
        "capacity": 750,
        "size_category": "medium",
        "website": "https://catscradle.com/",
        "scraper_type": "rhp_events",
        "scraper_config": {"url": "https://catscradle.com/events/", "venue_filter": "Cat's Cradle", "venue_filter_not": "Back Room"},
        "color": "#1e5c3c",  # emerald (Carrboro)
    },
    {
        "name": "Motorco Music Hall",
        "slug": "motorco",
        "city": "Durham",
        "capacity": 450,
        "size_category": "medium",
        "website": "https://www.motorcomusic.com/",
        "scraper_type": "motorco",
        "scraper_config": {"url": "https://motorcomusic.com/calendar/"},
        "color": "#1a5e76",  # peacock teal (Durham)
    },
    {
        "name": "Local 506",
        "slug": "local-506",
        "city": "Chapel Hill-Carrboro",
        "capacity": 250,
        "size_category": "small",
        "website": "https://local506.com/",
        "scraper_type": "rhp_events",
        "scraper_config": {"url": "https://local506.com/events/"},
        "color": "#1a5e50",  # jade (Chapel Hill)
    },
    {
        "name": "The Pinhook",
        "slug": "the-pinhook",
        "city": "Durham",
        "capacity": 250,
        "size_category": "small",
        "website": "https://www.thepinhook.com/",
        "scraper_type": "rhp_events",
        "scraper_config": {"url": "https://www.thepinhook.com/events/"},
        "color": "#2a5494",  # cornflower sapphire (Durham)
    },
    {
        "name": "Kings",
        "slug": "kings",
        "city": "Raleigh",
        "capacity": 250,
        "size_category": "small",
        "website": "https://www.kingsraleigh.com/",
        "scraper_type": "eventprime",
        "scraper_config": {"url": "https://www.kingsraleigh.com/"},
        "color": "#7a4230",  # warm garnet (Raleigh)
    },
    {
        "name": "Cat's Cradle Back Room",
        "slug": "cats-cradle-back-room",
        "city": "Chapel Hill-Carrboro",
        "capacity": None,
        "size_category": "small",
        "website": "https://catscradle.com/",
        "scraper_type": "rhp_events",
        "scraper_config": {"url": "https://catscradle.com/events/", "venue_filter": "Back Room"},
        "color": "#1f6b47",  # lighter emerald (Carrboro)
    },
    {
        "name": "The Cave",
        "slug": "the-cave",
        "city": "Chapel Hill-Carrboro",
        "capacity": 100,
        "size_category": "small",
        "website": "https://www.caverntavern.com/",
        "scraper_type": "tribe_events",
        "scraper_config": {"url": "https://caverntavern.com/"},
        "color": "#1e4c38",  # forest jade (Chapel Hill)
    },
    {
        "name": "Haw River Ballroom",
        "slug": "haw-river-ballroom",
        "city": "Saxapahaw",
        "capacity": 600,
        "size_category": "medium",
        "website": "https://www.hawriverballroom.com/",
        "scraper_type": "venuepilot",
        "scraper_config": {"account_id": 477},
        "color": "#72268c",  # deep orchid (Saxapahaw)
    },
    {
        "name": "Neptune's Parlour",
        "slug": "neptunes-parlour",
        "city": "Raleigh",
        "capacity": None,
        "size_category": "small",
        "website": "https://neptunesraleigh.com/",
        "scraper_type": "squarespace",
        "scraper_config": {"url": "https://neptunesraleigh.com/events?format=json"},
        "color": "#6e2040",  # deep rose (Raleigh)
    },
    {
        "name": "Shadowbox Studio",
        "slug": "shadowbox-studio",
        "city": "Durham",
        "capacity": None,
        "size_category": "small",
        "website": "https://shadowboxstudio.org/",
        "scraper_type": "mec",
        "scraper_config": {"url": "https://shadowboxstudio.org/events/"},
        "color": "#2a4e88",  # denim sapphire (Durham)
    },
    {
        "name": "Rubies on Five Points",
        "slug": "rubies",
        "city": "Durham",
        "capacity": 150,
        "size_category": "small",
        "website": "https://rubiesnc.com/",
        "scraper_type": "venuepilot",
        "scraper_config": {"account_id": 3095},
        "color": "#7a1e3c",  # deep ruby (Durham)
    },
    {
        "name": "Stanczyks",
        "slug": "stancyks",
        "city": "Durham",
        "capacity": 100,
        "size_category": "small",
        "website": "https://www.stanczyksdurham.com/",
        "scraper_type": "venuepilot",
        "scraper_config": {"account_id": 3433},
        "color": "#5a3a20",  # warm espresso (Durham)
    },
    {
        "name": "Boom Club",
        "slug": "boom-club",
        "city": "Durham",
        "capacity": None,
        "size_category": "small",
        "website": "https://www.boom-club.org/",
        "scraper_type": "squarespace",
        "scraper_config": {
            "url": "https://www.boom-club.org/events?format=json",
            "exclude_titles": ["Synth Library open", "Synth Library closed"],
        },
        "color": "#3a1e6e",  # deep violet (Durham)
    },
    {
        "name": "Chapel of Bones",
        "slug": "chapel-of-bones",
        "city": "Raleigh",
        "capacity": None,
        "size_category": "small",
        "website": "https://chapelofbones.com/",
        "scraper_type": "tickpick_organizer",
        "scraper_config": {"organizer_id": "chapel-of-bones"},
        "color": "#2a2840",  # dark ash (Raleigh)
    },
    {
        "name": "Pour House",
        "slug": "pour-house",
        "city": "Raleigh",
        "capacity": None,
        "size_category": "small",
        "website": "https://www.pourhouseraleigh.com/",
        "scraper_type": "webflow_cms",
        "scraper_config": {
            "url": "https://www.pourhouseraleigh.com/calendar",
            "base_url": "https://www.pourhouseraleigh.com",
        },
        "color": "#6a3828",  # warm brick (Raleigh)
    },
    {
        "name": "Slim's",
        "slug": "slims",
        "city": "Raleigh",
        "capacity": 200,
        "size_category": "small",
        "website": "https://slimsdivebar.com/",
        "scraper_type": "mec",
        "scraper_config": {"url": "https://slimsdivebar.com/music-and-events/"},
        "color": "#4a3a6a",  # deep plum (Raleigh)
    },
]


async def seed_venues():
    """Insert or update all venues."""
    await init_db()
    async with async_session() as session:
        # Remove discontinued venues (cascade deletes their events)
        REMOVED_SLUGS = ["carolina-theatre"]
        for slug in REMOVED_SLUGS:
            result = await session.execute(select(Venue).where(Venue.slug == slug))
            venue = result.scalar_one_or_none()
            if venue:
                await session.delete(venue)
                logger.info(f"Deleted discontinued venue: {slug}")
        await session.commit()

        count_new = 0
        count_updated = 0
        for venue_data in VENUES:
            result = await session.execute(
                select(Venue).where(Venue.slug == venue_data["slug"])
            )
            existing = result.scalar_one_or_none()
            if existing:
                for key, value in venue_data.items():
                    setattr(existing, key, value)
                count_updated += 1
            else:
                session.add(Venue(**venue_data))
                count_new += 1
        await session.commit()
        logger.info(f"Seed complete: {count_new} new, {count_updated} updated venues")
        print(f"Seed complete: {count_new} new, {count_updated} updated venues")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(seed_venues())
