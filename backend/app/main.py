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
import os
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import async_session
from app.redaction import redact_credentials, redact_handler
from app.seed import seed_venues
from app.scheduler import scheduler, configure_scheduler
from app.api import events, venues, health, feeds, admin
from app import tokens

# --- Logging setup ---

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def configure_logging() -> None:
    """Configure process-wide logging, with credential redaction on every sink.

    Two measures, because the Ticketmaster Discovery API authenticates with a query
    parameter and so every request URL the scraper builds is a live credential:

    * ``httpx`` is pinned to WARNING. It logs the full URL of every request at INFO,
      which put the key in the log store once per Ticketmaster venue per scrape cycle.
      Our scrapers already emit their own per-request line naming the venue, so what is
      lost is the HTTP status of a *successful* request; a failure still raises and is
      logged.
    * **Every handler in the process** — root's and everybody else's — is wrapped by
      :func:`~app.redaction.redact_handler`, which scrubs the values out of anything
      that renders a URL, including the exception tracebacks the httpx pin cannot
      reach (``httpx.HTTPStatusError`` carries the request URL in its own message
      regardless of the logger's level).

      Root handlers alone are not enough. Starlette's ``ServerErrorMiddleware`` always
      re-raises after invoking the bare-``Exception`` handler, and the ASGI server then
      logs the full traceback itself on a logger it configures with ``propagate=False``
      and its own handler — which no root handler ever sees. Under uvicorn that is the
      ``uvicorn`` logger; under ``gunicorn -k UvicornWorker`` it is ``gunicorn.error``.
      Naming those loggers here instead would make this a snapshot of today's
      dependency set whose failure mode is a *silent* one: swap the server — an
      ordinary deployment change touching no application code — and the sink reopens
      with no signal and no failing test, because a test can only reach for the same
      names the code does. Sweeping every handler is immune to that by construction.

      This is safe to apply indiscriminately precisely because ``redact_handler``
      wraps rather than replaces: a handler somebody else owns (uvicorn's access
      formatter, a structured/JSON handler a deployment installs on root) keeps its
      exact layout, and the wrap is idempotent, so repeat calls never nest.

    Called at import, where the plain ``basicConfig`` call it replaces used to sit, so
    the configuration is in place before the app serves anything — and exposed as a
    function so tests can assert on it rather than importing for its side effects
    alone. uvicorn builds its logging config in ``Config.__init__``, before it imports
    the app, so its handlers already exist by the time this runs under a real server;
    the sweep simply finds fewer of them under pytest or a bare interpreter.

    Residue worth knowing: handlers installed *after* this runs are not covered. That
    is a narrower bet than the one a name-based enumeration makes, but it is still a
    bet — if a future dependency installs a handler lazily, call this again once it has.
    """
    logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL), format=LOG_FORMAT)

    # Snapshot the registry before walking it. loggerDict is the live dict that
    # logging.getLogger() inserts into, and the filter below runs Python between
    # iterations, so a thread creating a logger mid-sweep would raise "dictionary
    # changed size during iteration" out of a function that runs at import — turning a
    # logging refinement into the app failing to boot. list() materializes in one
    # C-level pass, which cannot observe a concurrent resize.
    loggers = [logging.getLogger()] + [
        existing
        for existing in list(logging.root.manager.loggerDict.values())
        if isinstance(existing, logging.Logger)
    ]
    for each in loggers:
        for handler in each.handlers:
            redact_handler(handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)


configure_logging()
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
            logger.info(f"Startup scrape: reclassify {manager.last_reclassify}")
        logger.info("Startup scrape: complete")
    except Exception as e:
        # Non-fatal: the API should still serve cached data even if the scrape fails.
        # ERROR with a traceback, not a bare WARNING: this handler swallows every failure
        # in the scrape and reclassify path, so without the traceback the only signal that
        # anything went wrong was a one-line message with no stack.
        logger.error(f"Startup scrape failed: {e}", exc_info=True)


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
#
# allow_credentials is False, and that single flag is what makes the "*" safe. The exact
# mechanics, measured rather than assumed (see tests/test_cors.py):
#
#   Starlette replaces the "*" with the caller's own Origin whenever the request carries a
#   Cookie header. That happens either way and is not the problem. The problem was pairing
#   it with allow_credentials=True, which adds Access-Control-Allow-Credentials: true —
#   the header a browser requires before it will hand a credentialed cross-origin response
#   back to the page that asked for it. Origin echo plus that header is what let a hostile
#   page read authenticated responses, leaving the admin cookie's samesite="lax" as the
#   only control. Without the header the echo is inert: the browser discards the response.
#
# Nothing here needs cross-origin credentials, or cross-origin requests of any kind.
# frontend/js/config.js sets API_BASE to window.location.origin and the admin UI fetches
# relative paths, so every in-app request is same-origin, where CORS does not apply.
#
# Keeping "*" rather than an allowlist is deliberate: the read API is public, so leaving it
# usable from any page costs nothing once credentials are off. Narrowing allow_origins to
# the real site origins would stop third parties consuming it from a browser — a product
# decision, not a security one.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
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
# Admin subsite — must be registered before the "/" static mount below so its
# routes take priority over the catch-all StaticFiles handler.
app.include_router(admin.router)

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
            # Report the reclassify pass alongside the per-venue results. A caller that
            # triggers a scrape by hand can then see whether the classifier actually did
            # anything, instead of having to find a log line — which is how a pass that
            # classified nothing went unnoticed through a whole release.
            return {"results": results, "reclassified": manager.last_reclassify}
    except Exception as e:
        logger.error(f"[trigger_scrape] Unhandled error: {e}", exc_info=True)
        # This endpoint is unauthenticated whenever SCRAPE_ALLOWED_SERVICE_ACCOUNTS is
        # unset, and failures here happen *outside* scrape_venue (session construction,
        # a scraper import, the reclassify pass) — before the manager's own redaction
        # runs — so this catch-all has to scrub independently rather than rely on the
        # per-venue result dict having been scrubbed already.
        raise HTTPException(status_code=500, detail=redact_credentials(str(e)))


# --- Static file serving ---

# Serve frontend static files
# Check multiple possible locations (local dev vs Docker)
frontend_candidates = [
    Path(__file__).parent.parent.parent / "frontend",  # local dev
    Path("/frontend"),  # Docker
]

_frontend_dir: Optional[Path] = None
for candidate in frontend_candidates:
    if candidate.exists():
        _frontend_dir = candidate
        break


# --- Asset versioning on index.html ---
#
# index.html references /css/*.css and /js/*.js at fixed URLs. Cloudflare caches those at
# the edge for hours on the free plan, while index.html itself is never edge-cached
# (cf-cache-status: DYNAMIC). After a deploy that combination serves *new* HTML against
# *old* assets, which is worse than serving an entirely stale page: on the 2026-08-28
# release the new markup loaded a new equalizer.js against a styles.css that had no rules
# for it, and visitors got a row of unstyled buttons in the sidebar.
#
# Substituting {{ASSET_VERSION}} with the commit SHA gives every deploy fresh asset URLs,
# so the edge treats them as new objects and there is nothing left to purge by hand.
#
# Done at request time from the env var rather than by rewriting the file at image build:
# GIT_COMMIT is already in the container (cloudbuild.yaml passes it as a build arg,
# Dockerfile promotes it to ENV, health.py reads it), and this way the checked-in
# index.html stays a file that runs as-is in local dev.

ASSET_VERSION_PLACEHOLDER = "{{ASSET_VERSION}}"


def _asset_version() -> str:
    """Cache-busting token for static asset URLs.

    The commit SHA in production. Unset locally and in CI, where "dev" is correct: there is
    no CDN in front of either, and the browser revalidates against the local server.
    """
    return os.environ.get("GIT_COMMIT", "dev")[:12]


@lru_cache(maxsize=1)
def _index_html() -> Optional[str]:
    """index.html with the asset version substituted, read once per container.

    Cached because neither the file nor GIT_COMMIT changes within the life of a container.
    Returns None when there is no frontend directory, which is the case in the API-only
    test runs -- the caller then falls through to a 404 rather than raising.
    """
    if _frontend_dir is None:
        return None

    index_path = _frontend_dir / "index.html"
    if not index_path.is_file():
        return None

    html = index_path.read_text(encoding="utf-8")
    return html.replace(ASSET_VERSION_PLACEHOLDER, _asset_version())


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
async def serve_index():
    """Serve the templated index.html.

    Registered before the StaticFiles mount below, which would otherwise answer "/" with
    the raw file and leave the placeholder in the markup.

    no-store rather than a short max-age: this document is what names the versioned asset
    URLs, so a cached copy of it defeats the whole mechanism by continuing to point at the
    previous deploy's files.
    """
    from fastapi.responses import HTMLResponse, PlainTextResponse

    html = _index_html()
    if html is None:
        return PlainTextResponse("Frontend not available", status_code=404)

    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store, must-revalidate",
            # Surfaces which build a page came from without opening devtools, which is how
            # the stale-asset problem went unnoticed for as long as it did.
            "X-Asset-Version": _asset_version(),
        },
    )


if _frontend_dir is not None:
    # Mounted last so API routes and serve_index above take priority over the catch-all
    # html=True handler.
    app.mount("/", StaticFiles(directory=str(_frontend_dir), html=True), name="frontend")
