"""FastAPI application entry point."""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import init_db, async_session
from app.seed import seed_venues
from app.scheduler import scheduler, configure_scheduler
from app.api import events, venues, health, feeds

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _run_migrations():
    """Run alembic upgrade head synchronously (called via asyncio.to_thread)."""
    from alembic import command
    from alembic.config import Config

    # alembic.ini lives one directory above this file (i.e. /app/alembic.ini in Docker)
    ini_path = Path(__file__).parent.parent / "alembic.ini"
    cfg = Config(str(ini_path))
    command.upgrade(cfg, "head")


async def _startup_scrape():
    """Run a full scrape in the background on startup."""
    logger.info("Startup scrape: beginning...")
    try:
        from app.scrapers.manager import ScrapeManager
        async with async_session() as session:
            manager = ScrapeManager(session)
            results = await manager.scrape_all()
            for r in results:
                logger.info(f"  [startup] {r}")
        logger.info("Startup scrape: complete")
    except Exception as e:
        logger.warning(f"Startup scrape failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic."""
    logger.info("Starting Triangle Shows API...")

    # Initialize database tables
    await init_db()
    logger.info("Database initialized")

    # Apply any pending Alembic migrations (e.g. new columns)
    await asyncio.to_thread(_run_migrations)
    logger.info("Migrations applied")

    # Seed venues
    await seed_venues()
    logger.info("Venues seeded")

    # Kick off a scrape immediately in the background
    asyncio.create_task(_startup_scrape())
    logger.info("Startup scrape scheduled")

    # Start scheduler if enabled
    if settings.ENABLE_SCHEDULER:
        configure_scheduler()
        scheduler.start()
        logger.info("Scheduler started")

    yield

    # Shutdown
    if scheduler.running:
        scheduler.shutdown()
        logger.info("Scheduler shut down")


app = FastAPI(
    title="Triangle Shows",
    description="Concert calendar for the Raleigh-Durham-Chapel Hill area",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API routes
app.include_router(events.router)
app.include_router(venues.router)
app.include_router(health.router)
app.include_router(feeds.router)

# Manual scrape trigger (dev only)
@app.post("/api/scrape")
async def trigger_scrape(scraper_type: str = None):
    """Manually trigger a scrape (development use)."""
    from app.database import async_session
    from app.scrapers.manager import ScrapeManager
    from fastapi import HTTPException

    try:
        async with async_session() as session:
            manager = ScrapeManager(session)
            if scraper_type:
                results = await manager.scrape_all(scraper_types=[scraper_type])
            else:
                results = await manager.scrape_all()
            return {"results": results}
    except Exception as e:
        logger.error(f"[trigger_scrape] Unhandled error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# Serve frontend static files
# Check multiple possible locations (local dev vs Docker)
frontend_candidates = [
    Path(__file__).parent.parent.parent / "frontend",  # local dev
    Path("/frontend"),  # Docker
]
for frontend_dir in frontend_candidates:
    if frontend_dir.exists():
        app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
        break
