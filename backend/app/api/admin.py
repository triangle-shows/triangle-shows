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
import logging
import secrets
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from pydantic import BaseModel
from sqlalchemy import select, and_, or_, func, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.config import settings
from app.database import get_session
from app.models import Event, Venue, SeriesOverride
from app.classifier import normalize_series_name, criteria_summary, reclassify_floor
from app.duplicates import FoldError, build_clusters, validate_fold
from app.scrapers.manager import ScrapeManager
from app.admin_ui import LOGIN_HTML, ADMIN_HTML
from app import tokens

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

# --- Session cookie handling ---

COOKIE_NAME = "ts_admin"
SESSION_MAX_AGE = 60 * 60 * 12  # 12 hours

# Prefer the configured secret; fall back to an ephemeral per-process key for local
# dev (cookies won't survive a restart and won't validate across multiple Cloud Run
# instances — SESSION_SECRET must be set in production).
_signing_key = settings.SESSION_SECRET or secrets.token_urlsafe(32)
_serializer = URLSafeTimedSerializer(_signing_key, salt="ts-admin-session")


def _password_login_enabled() -> bool:
    """Whether the shared-password login is usable at all.

    Kept separate from _admin_enabled() on purpose. This is the check that stops
    compare_digest comparing a guess against "" — which would accept an empty password —
    so it must stay pinned to ADMIN_PASSWORD alone and must not widen when some other
    authentication method becomes available.
    """
    return bool(settings.ADMIN_PASSWORD)


def _admin_enabled() -> bool:
    """Whether /admin can authenticate anyone at all — by either method.

    Cloudflare Access counts. When the origin gate is enforcing, every request that
    reaches a handler has already presented a token Cloudflare signed for a person on the
    Access policy, which is strictly stronger identification than a password shared by
    everyone. So a password is no longer required for the admin surface to exist.
    """
    return _password_login_enabled() or tokens.cloudflare_access_configured()


def _access_identity(request: Request) -> Optional[str]:
    """The Cloudflare-verified person behind this request, if there is one.

    Set by the enforce_admin_access middleware in app.main, which has already checked the
    token's signature, expiry, audience and issuer. Absent when the gate is not
    configured — in which case there is no verified identity and the cookie path applies.

    Trusting this is only sound because the gate rejects tokenless requests at the origin.
    If /admin were reachable without a token, treating its absence as "fall back to the
    cookie" would be fine, but treating its presence as proof would not be — the header
    can be set by any client. The middleware never sets this from an unverified header.
    """
    if not tokens.cloudflare_access_configured():
        return None
    return getattr(request.state, "access_email", None)


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


async def require_admin(request: Request) -> str:
    """Dependency guarding /admin/api/* — raises 401 for the frontend to redirect on.

    Returns who is acting: their email when Cloudflare Access identified them, otherwise
    "admin" for a password session, which carries no identity. Handlers can take it as a
    parameter to attribute an action to a person.
    """
    identity = _access_identity(request)
    if identity:
        return identity
    if _is_authenticated(request):
        return "admin"
    raise HTTPException(status_code=401, detail="Not authenticated")


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
async def login_page(request: Request):
    # Already identified by Cloudflare Access — there is nothing to log in to, so don't
    # show a password box that would only reject them. A visitor who reached this page at
    # all has passed the origin gate.
    if _access_identity(request):
        return RedirectResponse("/admin", status_code=303)
    if not _password_login_enabled():
        # No password configured and no Access identity: nothing can authenticate here.
        # Say so rather than presenting a form that cannot succeed.
        return HTMLResponse(
            "<h1>Admin login is disabled</h1><p>No password is configured, and this "
            "request did not arrive through Cloudflare Access.</p>",
            status_code=403,
        )
    return HTMLResponse(LOGIN_HTML)


@router.post("/login")
async def login_submit(body: LoginBody):
    # compare_digest guards against timing attacks; also fails closed when no
    # password is configured (compare against "" would otherwise accept "").
    #
    # Both operands are encoded to bytes because compare_digest rejects a str containing
    # any non-ASCII character, raising TypeError rather than returning False. Passed
    # strings directly, a password with an accented character turned a wrong-password
    # attempt into a 500 instead of a clean 401 — and, since the message differs, told the
    # caller something about the configured password. UTF-8 on both sides compares the
    # same bytes the client sent.
    #
    # Gated on _password_login_enabled(), not _admin_enabled(): the latter is now true
    # whenever Cloudflare Access is configured, and with no ADMIN_PASSWORD set that would
    # let compare_digest("", "") succeed and hand out a session to anyone posting an empty
    # password. The two checks exist separately for exactly this reason.
    if _password_login_enabled() and secrets.compare_digest(
        body.password.encode("utf-8"), settings.ADMIN_PASSWORD.encode("utf-8")
    ):
        response = JSONResponse({"ok": True})
        _issue_cookie(response)
        return response
    return JSONResponse({"ok": False, "error": "invalid"}, status_code=401)


@router.get("/logout")
async def logout() -> RedirectResponse:
    """Log out of whichever session is actually in force.

    Deleting the app's cookie does nothing to a Cloudflare Access session, so with the
    gate enforcing, the old behavior sent you to /admin/login, which recognised your still
    valid Access identity and bounced you straight back to the dashboard — a logout button
    that visibly did nothing. Cloudflare's own logout endpoint is what ends that session.
    """
    target = "/admin/login"
    if tokens.cloudflare_access_configured():
        team_domain = settings.CF_ACCESS_TEAM_DOMAIN.strip().rstrip("/")
        if "://" in team_domain:
            team_domain = team_domain.split("://", 1)[1]
        target = f"https://{team_domain}/cdn-cgi/access/logout"

    response = RedirectResponse(target, status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/admin")
    return response


@router.get("", response_class=HTMLResponse)
async def admin_page(request: Request):
    identity = _access_identity(request)
    if identity:
        logger.info(f"[admin] dashboard opened by {identity}")
        return HTMLResponse(ADMIN_HTML)
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

    # How many rows are folded into each event on this page, so a survivor can say
    # "2 hidden duplicates" instead of the folded rows simply being absent. Scoped to the
    # ids actually shown rather than counting the whole table.
    fold_counts: dict[int, int] = {}
    if events:
        fold_rows = await session.execute(
            select(Event.duplicate_of_id, func.count())
            .where(Event.duplicate_of_id.in_([e.id for e in events]))
            .group_by(Event.duplicate_of_id)
        )
        fold_counts = {target: n for target, n in fold_rows.all()}

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
            "is_manually_created": e.is_manually_created,
            # Duplicate state (#63). A folded row stays visible here — it is only hidden
            # from the public calendar — so the admin can see what was suppressed and undo
            # it, which is the entire reason duplicate_of_id is a pointer and not a delete.
            "duplicate_of_id": e.duplicate_of_id,
            "hidden_duplicate_count": fold_counts.get(e.id, 0),
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


# --- Duplicate flagging (issue #63) ---


class FoldBody(BaseModel):
    """Ids to fold into the survivor named in the path."""

    duplicate_ids: list[int]


@router.get("/api/duplicates", dependencies=[Depends(require_admin)])
async def list_duplicate_candidates(
    future_only: bool = Query(True, description="Hide clusters before today"),
    limit: int = Query(200, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Clusters of events that might be duplicates: same venue, same date, two or more.

    Candidate selection is by fact rather than by title inference, deliberately — see
    app/duplicates.py for why. Similarity only orders the result.

    Defaults to future dates only, the opposite of /api/events. The classification queue
    wants history because a series action reaches back; a duplicate is a listing problem
    on a specific night, and past ones are archive debris nobody needs prompting about
    (#63 notes the reconcile pass deliberately never touches them either).
    """
    floor = date.today() if future_only else reclassify_floor()

    # Two queries rather than loading every event and grouping in Python: find the
    # (venue, date) pairs that have two or more *unfolded* rows, then fetch the rows for
    # those pairs only. Keeps both bounded on a table that grows without limit.
    #
    # Counting only unfolded rows is what makes a cluster resolve itself — folding a row
    # drops it out of this count, so a reviewed cluster disappears with nothing recording
    # that it was reviewed.
    pair_rows = await session.execute(
        select(Event.venue_id, Event.date)
        .where(Event.date >= floor, Event.duplicate_of_id.is_(None))
        .group_by(Event.venue_id, Event.date)
        .having(func.count() > 1)
    )
    pairs = pair_rows.all()
    if not pairs:
        return {"clusters": [], "count": 0, "total": 0}

    # Folded rows are fetched too, so a cluster can show what was previously absorbed.
    result = await session.execute(
        select(Event)
        .options(joinedload(Event.venue))
        .where(tuple_(Event.venue_id, Event.date).in_([(v, d) for v, d in pairs]))
    )
    rows = result.unique().scalars().all()

    clusters = build_clusters(list(rows))
    total = len(clusters)

    out = []
    for cluster in clusters[:limit]:
        members = cluster["members"]
        venue = next((m.venue for m in members if m.venue), None)
        out.append({
            "venue_id": cluster["venue_id"],
            "venue_name": venue.name if venue else None,
            "date": cluster["date"].isoformat(),
            # Surfaced so the ordering is legible rather than mysterious, and so a queue
            # topped by low scores is visibly "nothing likely left" instead of looking
            # like a bug.
            "score": round(cluster["score"], 3),
            "all_approved": cluster["all_approved"],
            "events": [{
                "id": m.id,
                "name": m.name,
                "artist": m.artist,
                "show_time": m.show_time.isoformat() if m.show_time else None,
                "doors_time": m.doors_time.isoformat() if m.doors_time else None,
                "price_min": m.price_min,
                "ticket_url": m.ticket_url,
                "is_live_music": m.is_live_music,
                "is_manually_created": m.is_manually_created,
                "is_approved": m.approved_at is not None,
                "duplicate_of_id": m.duplicate_of_id,
            } for m in members],
        })

    return {"clusters": out, "count": len(out), "total": total}


@router.post("/api/events/{event_id}/absorb", dependencies=[Depends(require_admin)])
async def absorb_duplicates(
    event_id: int,
    body: FoldBody,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Fold `body.duplicate_ids` into `event_id`, which survives.

    The survivor is the path parameter because that is the direction the UI acts in: the
    admin presses "keep this one" on the row they want, and everything else in the cluster
    folds into it. A "mark this a duplicate" endpoint would put the id of the *loser* in
    the path and make the caller name the winner in the body, which is the same write with
    the reasoning inverted — and inverting it is how the wrong row gets hidden.

    Folding hides rows; it never deletes them. plan_upsert's reconcile guard
    (scraper_must_not_delete) keeps the scraper from deleting them either, which is what
    makes this reversible at all.
    """
    survivor = await session.get(Event, event_id)
    if not survivor:
        raise HTTPException(status_code=404, detail="Event not found")

    requested = list(dict.fromkeys(body.duplicate_ids))
    rows = (await session.execute(
        select(Event).where(Event.id.in_(requested + [event_id]))
    )).scalars().all()
    by_id = {r.id: r for r in rows}

    missing = [i for i in requested if i not in by_id]
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"Event(s) not found: {', '.join(str(i) for i in missing)}",
        )

    try:
        ids = validate_fold(
            event_id,
            requested,
            already_folded={i: by_id[i].duplicate_of_id for i in by_id},
        )
    except FoldError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Refuse to fold a row that already survives other rows: those rows point at it, and
    # hiding it would leave them hidden behind something hidden — the chain case, arriving
    # from the other direction. validate_fold cannot see this; it needs a query.
    dependents = (await session.execute(
        select(Event.duplicate_of_id, func.count())
        .where(Event.duplicate_of_id.in_(ids))
        .group_by(Event.duplicate_of_id)
    )).all()
    if dependents:
        detail = "; ".join(
            f"event {target} already has {n} duplicate(s) folded into it"
            for target, n in dependents
        )
        raise HTTPException(
            status_code=400,
            detail=f"Unfold those first. {detail}",
        )

    # Warn rather than refuse on a cross-venue or cross-date fold. A rescheduled or
    # relocated show is a real duplicate, so blocking it would make the tool useless for
    # exactly the messy case that needs a human; but it is unusual enough to be worth a
    # line in the log if it later looks like a mistake.
    odd = [
        i for i in ids
        if by_id[i].venue_id != survivor.venue_id or by_id[i].date != survivor.date
    ]
    if odd:
        logger.info(
            f"[admin] folding across venue/date into event {event_id}: "
            f"{', '.join(str(i) for i in odd)}"
        )

    for i in ids:
        by_id[i].duplicate_of_id = event_id
    await session.commit()
    return {"ok": True, "survivor_id": event_id, "folded": ids}


@router.post("/api/events/{event_id}/unfold", dependencies=[Depends(require_admin)])
async def unfold_duplicate(
    event_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Clear `event_id`'s duplicate flag, returning it to the public calendar."""
    event = await session.get(Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    was = event.duplicate_of_id
    event.duplicate_of_id = None
    await session.commit()
    return {"ok": True, "id": event_id, "was_duplicate_of": was}


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
