"""
Admin subsite mounted at /admin: password-gated UI + JSON API for reviewing and
overriding live-music classification.

Role: Lets a site admin flip an event's is_live_music flag (per-event manual
override), manage series-level overrides (SeriesOverride), and view the automatic
detection criteria. Auth is a single shared password (settings.ADMIN_PASSWORD)
exchanged for an itsdangerous-signed session cookie; if ADMIN_PASSWORD is blank the
admin is disabled (fails closed). Must be registered before the "/" static mount in
main.py so these routes take priority over the catch-all.
Requires: async PostgreSQL session, app.classifier, app.scrapers.manager, itsdangerous.
"""
import secrets
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from pydantic import BaseModel
from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.config import settings
from app.database import get_session
from app.models import Event, Venue, SeriesOverride
from app.classifier import normalize_series_name, criteria_summary
from app.scrapers.manager import ScrapeManager
from app.admin_ui import LOGIN_HTML, ADMIN_HTML

router = APIRouter(prefix="/admin", tags=["admin"])

# --- Session cookie handling ---

COOKIE_NAME = "ts_admin"
SESSION_MAX_AGE = 60 * 60 * 12  # 12 hours

# Prefer the configured secret; fall back to an ephemeral per-process key for local
# dev (cookies won't survive a restart and won't validate across multiple Cloud Run
# instances — SESSION_SECRET must be set in production).
_signing_key = settings.SESSION_SECRET or secrets.token_urlsafe(32)
_serializer = URLSafeTimedSerializer(_signing_key, salt="ts-admin-session")


def _admin_enabled() -> bool:
    """Admin is reachable only when a password is configured."""
    return bool(settings.ADMIN_PASSWORD)


def _issue_cookie(response) -> None:
    token = _serializer.dumps("admin")
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",  # blocks the cookie on cross-site POSTs -> basic CSRF protection
        secure=(settings.APP_ENV == "production"),
        path="/admin",
    )


def _is_authenticated(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return False
    try:
        _serializer.loads(token, max_age=SESSION_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


async def require_admin(request: Request) -> bool:
    """Dependency guarding /admin/api/* — raises 401 for the frontend to redirect on."""
    if not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return True


# --- Request bodies ---

class LoginBody(BaseModel):
    password: str


class OverrideBody(BaseModel):
    is_live_music: bool


class SeriesBody(BaseModel):
    event_id: int
    is_live_music: bool
    note: Optional[str] = None


# --- Auth + page routes ---

@router.get("/login", response_class=HTMLResponse)
async def login_page() -> HTMLResponse:
    return HTMLResponse(LOGIN_HTML)


@router.post("/login")
async def login_submit(body: LoginBody):
    # compare_digest guards against timing attacks; also fails closed when no
    # password is configured (compare against "" would otherwise accept "").
    if _admin_enabled() and secrets.compare_digest(body.password, settings.ADMIN_PASSWORD):
        response = JSONResponse({"ok": True})
        _issue_cookie(response)
        return response
    return JSONResponse({"ok": False, "error": "invalid"}, status_code=401)


@router.get("/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/admin")
    return response


@router.get("", response_class=HTMLResponse)
async def admin_page(request: Request):
    if not _is_authenticated(request):
        return RedirectResponse("/admin/login", status_code=303)
    return HTMLResponse(ADMIN_HTML)


# --- JSON API (all guarded) ---

@router.get("/api/rules", dependencies=[Depends(require_admin)])
async def admin_rules() -> dict:
    """Return the current automatic detection criteria for display."""
    return criteria_summary()


@router.get("/api/events", dependencies=[Depends(require_admin)])
async def admin_events(
    filter: str = Query("non_live", pattern="^(non_live|live|all)$"),
    search: Optional[str] = None,
    limit: int = Query(200, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """List upcoming events with classification + override state for moderation."""
    conditions = [Event.date >= date.today()]
    if filter == "non_live":
        conditions.append(Event.is_live_music.is_(False))
    elif filter == "live":
        conditions.append(Event.is_live_music.is_(True))
    if search:
        term = f"%{search}%"
        conditions.append(or_(Event.name.ilike(term), Event.artist.ilike(term)))

    result = await session.execute(
        select(Event)
        .options(joinedload(Event.venue))
        .where(and_(*conditions))
        .order_by(Event.date)
        .limit(limit)
    )
    events = result.unique().scalars().all()

    # Annotate each event with any matching series override.
    so_result = await session.execute(select(SeriesOverride))
    series = {(s.venue_id, s.normalized_name): s for s in so_result.scalars().all()}

    out = []
    for e in events:
        so = series.get((e.venue_id, normalize_series_name(e.name)))
        out.append({
            "id": e.id,
            "name": e.name,
            "artist": e.artist,
            "date": e.date.isoformat(),
            "venue_name": e.venue.name if e.venue else None,
            "venue_slug": e.venue.slug if e.venue else None,
            "is_live_music": e.is_live_music,
            "is_manual_override": e.is_manual_override,
            "classification_reason": e.classification_reason,
            "series_override_id": so.id if so else None,
            "series_override_is_live": so.is_live_music if so else None,
        })
    return {"events": out, "count": len(out)}


@router.post("/api/events/{event_id}/override", dependencies=[Depends(require_admin)])
async def set_override(
    event_id: int,
    body: OverrideBody,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Force a single event's flag. Marked as a manual override so re-scrapes keep it."""
    event = await session.get(Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    event.is_live_music = body.is_live_music
    event.is_manual_override = True
    event.classification_reason = "manual"
    await session.commit()
    return {"ok": True, "id": event_id, "is_live_music": body.is_live_music}


@router.post("/api/events/{event_id}/clear-override", dependencies=[Depends(require_admin)])
async def clear_override(
    event_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Drop a per-event override and recompute automatic/series/venue classification."""
    event = await session.get(Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    event.is_manual_override = False
    await session.commit()
    # Recompute now so the UI reflects the automatic result immediately.
    await ScrapeManager(session).reclassify_all()
    return {"ok": True, "id": event_id}


@router.get("/api/series", dependencies=[Depends(require_admin)])
async def list_series(session: AsyncSession = Depends(get_session)) -> dict:
    """List all series-level overrides with their venue name."""
    result = await session.execute(
        select(SeriesOverride, Venue)
        .join(Venue, SeriesOverride.venue_id == Venue.id)
        .order_by(SeriesOverride.created_at.desc())
    )
    return {"series": [{
        "id": so.id,
        "venue_id": so.venue_id,
        "venue_name": venue.name,
        "normalized_name": so.normalized_name,
        "display_name": so.display_name,
        "is_live_music": so.is_live_music,
        "note": so.note,
    } for so, venue in result.all()]}


@router.post("/api/series", dependencies=[Depends(require_admin)])
async def upsert_series(
    body: SeriesBody,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Create/update a series override from an example event, then reclassify so all
    matching instances (present and future) pick it up."""
    event = await session.get(Event, body.event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    normalized = normalize_series_name(event.name)
    existing = (await session.execute(
        select(SeriesOverride).where(
            SeriesOverride.venue_id == event.venue_id,
            SeriesOverride.normalized_name == normalized,
        )
    )).scalar_one_or_none()

    if existing:
        existing.is_live_music = body.is_live_music
        existing.note = body.note
        existing.display_name = event.name
    else:
        session.add(SeriesOverride(
            venue_id=event.venue_id,
            normalized_name=normalized,
            display_name=event.name,
            is_live_music=body.is_live_music,
            note=body.note,
        ))
    await session.commit()
    await ScrapeManager(session).reclassify_all()
    return {"ok": True}


@router.delete("/api/series/{series_id}", dependencies=[Depends(require_admin)])
async def delete_series(
    series_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Remove a series override, then reclassify so its events revert to automatic."""
    override = await session.get(SeriesOverride, series_id)
    if not override:
        raise HTTPException(status_code=404, detail="Series override not found")
    await session.delete(override)
    await session.commit()
    await ScrapeManager(session).reclassify_all()
    return {"ok": True}
