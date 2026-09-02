"""
Pydantic Settings configuration — loads environment variables for the application.

Role: Imported at startup by main.py and any module that needs runtime config (database URL,
scheduler toggle, API keys). The `settings` singleton is created at import time, so .env
must be present (or env vars set) before any module imports this file.
Requires: .env file (or environment variables) providing DATABASE_URL, TICKETMASTER_API_KEY,
ENABLE_SCHEDULER, APP_ENV, and LOG_LEVEL.
"""

# --- Imports ---
from pydantic_settings import BaseSettings
from typing import Optional


# --- Settings ---

class Settings(BaseSettings):
    """Application-wide configuration, populated from environment variables or .env."""

    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/triangle_shows"
    TICKETMASTER_API_KEY: str = ""
    ENABLE_SCHEDULER: bool = False  # Set to True in production to run scrapes on a cron schedule
    ENABLE_STARTUP_SCRAPE: bool = True  # Set to False in CI/testing to skip the on-boot scrape
    APP_ENV: str = "development"
    LOG_LEVEL: str = "INFO"

    # --- Public query API (issue #62) ---
    # Whether the queryable read endpoints — GET /api/events and GET /api/events/{id} —
    # answer for anyone. Closed by default while a registration process is designed.
    #
    # A flag rather than deleted routes, because this is a policy decision that is meant to
    # be reversed: when #62 lands, the endpoints come back gated by a key instead of by
    # this. Deleting them would mean rebuilding the handlers, the schemas and their tests
    # to reopen something that was only ever meant to be paused.
    #
    # Closing these does not stop bulk collection, and should not be mistaken for it.
    # /api/events/fullcalendar is what the site itself loads, unauthenticated and with no
    # date bound, so the full dataset stays one request away — see the issue for why the
    # payload split is the change that would actually raise the cost.
    PUBLIC_QUERY_API_ENABLED: bool = False

    # --- Origin enforcement (see app.tokens) ---
    # The Cloud Run service accepts unauthenticated requests, so its .run.app hostnames
    # reach the app without passing Cloudflare. These settings let the app itself verify
    # who a request came from on the two routes that must not be open there.
    #
    # Each gate is inert until configured, deliberately: local dev and CI have no
    # Cloudflare and no Cloud Scheduler in front of them. app.tokens.log_enforcement_state()
    # logs which gates are live on every boot, so "inert" is never silent.

    # Cloudflare Access gate on /admin/*. Both are required to enforce.
    CF_ACCESS_TEAM_DOMAIN: str = ""  # e.g. "triangleshows.cloudflareaccess.com"
    CF_ACCESS_AUD: str = ""  # the Access application's AUD tag

    # Google OIDC gate on POST /api/scrape. Cloud Scheduler already sends the token.
    SCRAPE_ALLOWED_SERVICE_ACCOUNTS: str = ""  # comma-separated service-account emails
    SCRAPE_OIDC_AUDIENCE: str = ""  # optional; the audience configured on the scheduler job

    # Admin subsite (/admin). Both must be set for the admin to be reachable —
    # if ADMIN_PASSWORD is blank, the admin login is disabled (fails closed).
    ADMIN_PASSWORD: str = ""       # shared password exchanged for a session cookie
    SESSION_SECRET: str = ""       # signs the admin session cookie; MUST be set in
                                   # production so cookies validate across instances
                                   # (a random per-process key is used if left blank)

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


# --- Singleton ---

# Instantiated once at import time; all modules import this object directly.
settings = Settings()
