"""
FastAPI application entry point — initializes the database, applies migrations,
seeds venues, optionally starts a background scheduler, and mounts all routes.

Role: First code executed at server startup. The lifespan context manager runs
before any requests are served; Cloud Scheduler later hits POST /api/scrape to
trigger periodic re-scrapes every 6 hours.

Requires: DATABASE_URL, LOG_LEVEL, ENABLE_SCHEDULER env vars (via app.config);
asyncpg-compatible PostgreSQL; Alembic migrations in backend/alembic/.
"""
# --- Imports ---
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import async_session
from app.seed import seed_venues
from app.scheduler import scheduler, configure_scheduler
from app.api import events, venues, health, feeds
from app import tokens

# --- Logging setup ---
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# --- Startup helpers ---

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
        # Non-fatal: the API should still serve cached data even if the scrape fails
        logger.warning(f"Startup scrape failed: {e}")


# --- Lifespan (startup / shutdown) ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic."""
    logger.info("Starting Triangle Shows API...")

    # Report which origin gates are live before serving anything, so a gate that is
    # configured wrong (and therefore doing nothing) is visible in the boot logs rather
    # than mistaken for protection.
    tokens.log_enforcement_state()

    # Apply any pending Alembic migrations — creates tables on fresh DBs, updates schema on existing ones
    await asyncio.to_thread(_run_migrations)
    logger.info("Migrations applied")

    # Seed venues
    await seed_venues()
    logger.info("Venues seeded")

    # Kick off a scrape immediately in the background (disabled in CI via ENABLE_STARTUP_SCRAPE=false)
    if settings.ENABLE_STARTUP_SCRAPE:
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


# --- App instantiation ---

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


# --- Origin enforcement on /admin ---

@app.middleware("http")
async def enforce_admin_access(request: Request, call_next):
    """Require a valid Cloudflare Access token on /admin/*, when configured.

    Guarding by path rather than inside the admin router is deliberate. The admin
    subsite arrives with a separate branch (PR #36); a path check protects those routes
    from the moment they land, so the admin surface cannot reach production without the
    origin gate in front of it. Until then this is inert — /admin merely 404s.

    The login page is guarded too, not just the API beneath it. Reaching a password
    prompt on the origin is the exposure being closed; an unauthenticated caller should
    not see that the admin subsite exists at all.
    """
    if not request.url.path.startswith("/admin"):
        return await call_next(request)

    if not tokens.cloudflare_access_configured():
        return await call_next(request)

    try:
        claims = tokens.verify_cloudflare_access(
            request.headers.get("cf-access-jwt-assertion")
        )
    except tokens.TokenError as exc:
        # Logged at info, not warning: on a public origin, unauthenticated probes of
        # /admin are background noise rather than incidents.
        logger.info(f"[admin] rejected {request.method} {request.url.path}: {exc}")
        return JSONResponse(
            {"detail": "This surface is reachable only through Cloudflare Access."},
            status_code=403,
        )

    # Downstream handlers can attribute an action to a person rather than to whoever
    # holds a shared password.
    request.state.access_email = tokens.access_identity(claims)
    return await call_next(request)

# --- Route registration ---

# API routes
app.include_router(events.router)
app.include_router(venues.router)
app.include_router(health.router)
app.include_router(feeds.router)

# --- Origin enforcement on POST /api/scrape ---

async def require_scrape_token(request: Request) -> Optional[str]:
    """Require a Google-issued OIDC token from an allowlisted service account.

    Cloud Scheduler already attaches this token; Cloud Run cannot enforce it, because the
    service has to accept unauthenticated requests for the public site to work. So the
    check belongs here.

    Inert until SCRAPE_ALLOWED_SERVICE_ACCOUNTS names at least one account, which keeps
    local dev and CI working. Returns the calling service account for the audit log.
    """
    from fastapi import HTTPException

    if not tokens.scrape_token_configured():
        return None

    token = tokens.bearer_token(request.headers.get("authorization"))
    try:
        claims = tokens.verify_scrape_token(token)
    except tokens.TokenError as exc:
        logger.warning(f"[scrape] rejected trigger: {exc}")
        raise HTTPException(status_code=403, detail="Not authorized to trigger a scrape.")

    return claims.get("email")


# Scrape trigger. Called by Cloud Scheduler every 6 hours in production; also usable by
# hand in local dev, where the gate above is inert.
@app.post("/api/scrape")
async def trigger_scrape(
    scraper_type: str = None,
    caller: Optional[str] = Depends(require_scrape_token),
):
    """Trigger a scrape of every venue, or of one scraper_type."""
    from app.database import async_session
    from app.scrapers.manager import ScrapeManager
    from fastapi import HTTPException

    if caller:
        logger.info(f"[scrape] triggered by {caller}")

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


# --- Static file serving ---

# Serve frontend static files
# Check multiple possible locations (local dev vs Docker)
frontend_candidates = [
    Path(__file__).parent.parent.parent / "frontend",  # local dev
    Path("/frontend"),  # Docker
]
for frontend_dir in frontend_candidates:
    if frontend_dir.exists():
        # Mounted last so API routes take priority over the catch-all html=True handler
        app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
        break
