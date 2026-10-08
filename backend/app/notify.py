"""
Push notifications for new show submissions, via ntfy (https://ntfy.sh).

Role: Called by app.api.submissions.create_submission after a submission is saved, so an
admin hears about it without having to check /admin. One HTTPS request per submission;
no new dependency (httpx is already the scrapers' client).

Three rules shape it:

  * Never fails the submission. The row is committed before this runs, and every error
    here is caught and logged. A person who pressed "send it in" gets their thanks
    whether or not ntfy is up.

  * Sent inside the request, with a short timeout, not as a background task. Cloud Run
    allocates CPU only while a request is in flight, so work left running after the
    response is starved — the failure that took out the on-boot scrape (see
    docs/ARCHITECTURE.md). A few hundred milliseconds of the submitter's wait is the
    price of the notification actually being sent.

  * The topic URL is treated as a secret. On a public ntfy server, whoever knows the
    topic name can read and post to it. The topic travels in the JSON body rather than
    the request URL, so httpx's errors do not carry it — but exception text is still never
    logged, only its class, so that stays true if the publishing method ever changes.

Published as JSON to the server root rather than as plain text to the topic URL, because
ntfy reads the title from an HTTP header otherwise, and a show name with a curly quote or
an emoji is not a valid header value.

Requires: NTFY_URL (and optionally NTFY_TOKEN, PUBLIC_SITE_URL) from app.config.
"""

# --- Imports ---
import logging
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Long enough for a cold connection to ntfy.sh, short enough that a hung server does not
# visibly stall the form.
TIMEOUT_SECONDS = 4.0


def configured() -> bool:
    return bool(settings.NTFY_URL.strip())


def _split_topic_url(url: str) -> tuple[str, str]:
    """("https://ntfy.sh", "my-topic") from "https://ntfy.sh/my-topic".

    The last path segment is the topic and everything before it is the server, so a
    self-hosted ntfy under a path prefix works too.
    """
    server, _, topic = url.strip().rstrip("/").rpartition("/")
    return server, topic


def submission_message(
    *, name: str, on, venue: str, new_venue: bool, city: Optional[str], who: str
) -> dict:
    """The ntfy payload, minus the topic. Separate so tests can read it without a server."""
    where = f"{venue} (new venue{', ' + city if city else ''})" if new_venue else venue
    return {
        "title": f"New show proposed: {name}",
        "message": f"{on.strftime('%a %b')} {on.day} · {where}\nfrom {who}",
        "tags": ["calendar"],
        "click": settings.PUBLIC_SITE_URL.rstrip("/") + "/admin",
    }


async def send(payload: dict, *, client: Optional[httpx.AsyncClient] = None) -> bool:
    """Publish `payload` to the configured topic. Returns whether ntfy accepted it.

    Does nothing, and returns False, when NTFY_URL is unset. Never raises.
    `client` exists for tests; the app lets this open its own.
    """
    if not configured():
        return False

    server, topic = _split_topic_url(settings.NTFY_URL)
    headers = {}
    if settings.NTFY_TOKEN.strip():
        headers["Authorization"] = f"Bearer {settings.NTFY_TOKEN.strip()}"

    try:
        if client is None:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as own:
                response = await own.post(server, json={"topic": topic, **payload}, headers=headers)
        else:
            response = await client.post(server, json={"topic": topic, **payload}, headers=headers)
    except Exception as exc:
        # The class only: str(exc) from httpx carries the request URL, i.e. the topic.
        logger.warning(f"[notify] ntfy request failed: {type(exc).__name__}")
        return False

    if response.status_code >= 400:
        # 401/403 almost always means NTFY_TOKEN is missing or lacks access to the topic.
        logger.warning(f"[notify] ntfy refused the notification: HTTP {response.status_code}")
        return False
    return True


def log_state() -> None:
    """Say once per boot whether submissions will notify anyone."""
    if configured():
        auth = "with an access token" if settings.NTFY_TOKEN.strip() else "without an access token"
        logger.info(f"[notify] new submissions: ntfy notifications on, {auth}")
    else:
        logger.info("[notify] new submissions: no notifications (NTFY_URL is not set)")
