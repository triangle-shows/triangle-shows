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
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from pydantic import BaseModel
from sqlalchemy import select, and_, or_, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.config import settings
from app.database import get_session
from app.models import Event, Venue, SeriesOverride
from app.classifier import normalize_series_name, criteria_summary, reclassify_floor
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
    future_only: bool = Query(False, description="Hide events before today"),
    show_approved: bool = Query(False, description="Include events already reviewed"),
    limit: int = Query(1000, ge=1, le=5000),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """List events with classification + override state for moderation.

    Defaults to the same floor reclassification uses, so a series row shows every
    event a series action will actually affect — listing only future dates would
    understate scope. future_only narrows the view to today onward; it does not
    change what a series action touches.
    """
    floor = date.today() if future_only else reclassify_floor()

    # Split so the approved filter can be counted separately: `base` is everything the
    # user asked for, `conditions` adds the approved exclusion actually applied.
    base = [Event.date >= floor]
    if filter == "non_live":
        base.append(Event.is_live_music.is_(False))
    elif filter == "live":
        base.append(Event.is_live_music.is_(True))
    if search:
        term = f"%{search}%"
        base.append(or_(Event.name.ilike(term), Event.artist.ilike(term)))

    conditions = list(base)
    if not show_approved:
        conditions.append(Event.approved_at.is_(None))

    result = await session.execute(
        select(Event)
        .options(joinedload(Event.venue))
        .where(and_(*conditions))
        .order_by(Event.date)
        .limit(limit)
    )
    events = result.unique().scalars().all()

    # Total matching rows, ignoring `limit`. Without this the response could only
    # report how many rows came back, so a truncated page was indistinguishable from
    # a complete one — the UI would say "1000 event(s)" whether that was all of them
    # or the first 1000 of several thousand.
    total = (
        await session.execute(
            select(func.count()).select_from(Event).where(and_(*conditions))
        )
    ).scalar_one()

    # How many rows the approved filter is holding back, so that omission is visible
    # too rather than silently shrinking the queue.
    approved_hidden = 0
    if not show_approved:
        approved_hidden = (
            await session.execute(
                select(func.count()).select_from(Event)
                .where(and_(*base, Event.approved_at.is_not(None)))
            )
        ).scalar_one()

    # Annotate each event with any matching series override.
    so_result = await session.execute(select(SeriesOverride))
    series = {(s.venue_id, s.normalized_name): s for s in so_result.scalars().all()}

    # Series sizes across the whole reclassify range, deliberately ignoring both
    # `future_only` and `limit`: a series action reaches every matching event, so the
    # button label must count them all or it understates its own blast radius.
    # Grouping happens in Python because normalize_series_name has no SQL equivalent.
    span_rows = await session.execute(
        select(Event.venue_id, Event.name).where(Event.date >= reclassify_floor())
    )
    series_sizes: dict[tuple[int, str], int] = {}
    for venue_id, name in span_rows.all():
        key = (venue_id, normalize_series_name(name))
        series_sizes[key] = series_sizes.get(key, 0) + 1

    out = []
    for e in events:
        series_key = normalize_series_name(e.name)
        so = series.get((e.venue_id, series_key))
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
            "is_approved": e.approved_at is not None,
            # Detail shown when a row is expanded, to judge live-vs-not by hand.
            # Descriptions are sparse (~10% of events), so the ticket link is often
            # the only way to settle an ambiguous one.
            "description": e.description,
            "genre": e.genre,
            "ticket_url": e.ticket_url,
            "age_restriction": e.age_restriction,
            # Exposed so the dashboard can collapse a series into one row using the
            # same key a series override matches on — group and override stay in sync.
            "series_key": series_key,
            # How many events a series action on this row would actually affect.
            "series_size": series_sizes.get((e.venue_id, series_key), 1),
            "series_override_id": so.id if so else None,
            "series_override_is_live": so.is_live_music if so else None,
        })
    return {
        "events": out,
        "count": len(out),
        "total": total,
        "approved_hidden": approved_hidden,
    }


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
    # Setting the flag by hand *is* the review, so approve it too rather than asking
    # the admin to confirm a decision they just made.
    event.approved_at = datetime.utcnow()
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
    # Dropping the override returns the event to unreviewed automatic classification,
    # so the approval that came with the override goes too.
    event.approved_at = None
    await session.commit()
    # Recompute now so the UI reflects the automatic result immediately.
    await ScrapeManager(session).reclassify_all()
    return {"ok": True, "id": event_id}


@router.post("/api/events/{event_id}/approve", dependencies=[Depends(require_admin)])
async def approve_event(
    event_id: int,
    series: bool = Query(False, description="Approve every event in this event's series"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Mark a classification as reviewed and correct, hiding it from the default queue.

    With series=true the event acts as a sample: every event sharing its venue and
    normalized name is approved too. Classification is largely per-series, so
    approving 20-odd recurring dates one at a time would be punishing.
    """
    event = await session.get(Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    now = datetime.utcnow()
    if not series:
        event.approved_at = now
        await session.commit()
        return {"ok": True, "id": event_id, "approved": 1}

    # Same key the series overrides and the dashboard's grouping use, so "approve
    # series" covers exactly the rows the group row displays.
    key = normalize_series_name(event.name)
    rows = (await session.execute(
        select(Event).where(
            Event.venue_id == event.venue_id,
            Event.date >= reclassify_floor(),
        )
    )).scalars().all()
    approved = 0
    for ev in rows:
        if normalize_series_name(ev.name) == key and ev.approved_at is None:
            ev.approved_at = now
            approved += 1
    await session.commit()
    return {"ok": True, "id": event_id, "approved": approved}


@router.post("/api/events/{event_id}/unapprove", dependencies=[Depends(require_admin)])
async def unapprove_event(
    event_id: int,
    series: bool = Query(False, description="Un-approve every event in this event's series"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Return an event (or its whole series) to the review queue."""
    event = await session.get(Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    if not series:
        event.approved_at = None
        await session.commit()
        return {"ok": True, "id": event_id, "unapproved": 1}

    key = normalize_series_name(event.name)
    rows = (await session.execute(
        select(Event).where(
            Event.venue_id == event.venue_id,
            Event.date >= reclassify_floor(),
        )
    )).scalars().all()
    unapproved = 0
    for ev in rows:
        if normalize_series_name(ev.name) == key and ev.approved_at is not None:
            ev.approved_at = None
            unapproved += 1
    await session.commit()
    return {"ok": True, "id": event_id, "unapproved": unapproved}


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
