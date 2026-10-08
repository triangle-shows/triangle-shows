"""
The show-proposal form at /new-shows-form: a page and the JSON behind it.

Role: Lets people on a Cloudflare Access policy propose a show — at a listed venue or a
new one — for an admin to approve on /admin. Nothing submitted here reaches the public
calendar on its own: a submission is a ShowSubmission row, which no public read path
looks at, until an admin approves it (app.api.admin.approve_submission).

Access is a separate Cloudflare Access application from /admin, with its own AUD tag
(CF_ACCESS_SUBMIT_AUD), verified at the origin by app.main.enforce_submit_access. Unlike
/admin there is no password fallback, so in production the form fails closed until the
gate is configured; elsewhere it is open, for local dev.

Everything lives under one path prefix so that single path check covers the page and
every endpoint beneath it. Must be registered before the "/" static mount in main.py.
Requires: async PostgreSQL session; validation rules from app.api.admin.
"""
import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app import tokens
from app.api.admin import (
    MAX_PENDING_PER_SUBMITTER,
    SubmissionBody,
    _slugify,
    clean_submission,
    resolve_submission_venue,
)
from app.config import settings
from app.database import get_session
from app.models import Event, ShowSubmission, SubmissionStatus, Venue
from app.scrapers.base import ScrapedEvent
from app.submit_ui import SUBMIT_HTML

logger = logging.getLogger(__name__)

SUBMIT_PREFIX = "/new-shows-form"

# Stands in for an Access identity when the gate is off outside production, so local
# runs can exercise the whole flow and the rows still say where they came from.
LOCAL_SUBMITTER = "local-dev@localhost"

# Arbitrary key for the Postgres advisory lock that serializes new submissions. Any
# constant works as long as nothing else in the app takes the same one.
SUBMISSION_LOCK_KEY = 0x5B_5B_01

router = APIRouter(prefix=SUBMIT_PREFIX, tags=["submissions"])


def is_submit_path(path: str) -> bool:
    """Whether the origin gate for the form applies to `path`.

    The prefix itself or anything beneath it — not a bare startswith, which would also
    catch an unrelated /new-shows-formerly.
    """
    return path == SUBMIT_PREFIX or path.startswith(SUBMIT_PREFIX + "/")


def show_key(name: str, on: date, venue_slug: str) -> str:
    """The calendar's identity for a show, as a scraper or a hand-add would compute it.

    Used for both "already" checks, so a pending submission counts as the same show as
    another exactly when approving both would collide on the same Event hash: "Sub-Rosa"
    and "Sub Rosa" are one show, as are names differing only in case or HTML encoding.
    """
    return ScrapedEvent(name=name, date=on, venue_slug=venue_slug, source="manual").hash


def submission_venue_slug(s: ShowSubmission) -> str:
    """The slug a submission's venue has, or will have once approval creates it."""
    return s.venue.slug if s.venue else _slugify(s.new_venue_name or "")


async def require_submitter(request: Request) -> str:
    """Who is submitting: their Cloudflare Access identity, or a 403.

    With the gate configured, enforce_submit_access has already verified the token and
    rejected the request if it failed, so the identity is present whenever a handler
    runs. The check is repeated anyway because the alternative — trusting that the
    middleware is registered — is how a gate silently stops gating.

    Without the gate, production refuses: there is no other way to identify a
    submitter, and an open form on the public origin would be a spam inlet into the
    admin's queue. Outside production it is open, for local dev.
    """
    if tokens.submit_access_configured():
        email = getattr(request.state, "submit_email", None)
        if email:
            return email
        raise HTTPException(status_code=403, detail="Not signed in.")
    if settings.APP_ENV == "production":
        raise HTTPException(
            status_code=403, detail="The show form is not available right now."
        )
    return LOCAL_SUBMITTER


@router.get("", response_class=HTMLResponse)
async def submit_page(who: str = Depends(require_submitter)):
    return HTMLResponse(SUBMIT_HTML, headers={"Cache-Control": "no-store"})


@router.get("/api/venues")
async def submit_venues(
    who: str = Depends(require_submitter),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Every venue, for the picker, and the cities in use, for a new venue's city field.

    All of them, promoters with nothing upcoming included: the public list hides those,
    but a submitter adding the next show for one is exactly when it should be offered.
    """
    venues = (await session.execute(
        select(Venue).order_by(Venue.city, Venue.name)
    )).scalars().all()
    return {
        "venues": [{"id": v.id, "name": v.name, "city": v.city} for v in venues],
        "cities": sorted({v.city for v in venues}),
    }


@router.get("/api/submissions")
async def my_submissions(
    who: str = Depends(require_submitter),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The signed-in person's own recent submissions and what became of each."""
    rows = (await session.execute(
        select(ShowSubmission)
        .options(joinedload(ShowSubmission.venue))
        .where(ShowSubmission.submitted_by == who)
        .order_by(ShowSubmission.submitted_at.desc(), ShowSubmission.id.desc())
        .limit(50)
    )).unique().scalars().all()
    return {
        "you": who,
        "submissions": [{
            "id": s.id,
            "status": s.status,
            "name": s.name,
            "date": s.date.isoformat(),
            "venue": s.venue.name if s.venue else s.new_venue_name,
            "submitted_at": s.submitted_at.isoformat() if s.submitted_at else None,
        } for s in rows],
    }


@router.post("/api/submissions")
async def create_submission(
    body: SubmissionBody,
    who: str = Depends(require_submitter),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Propose a show. It waits for an admin; nothing here touches the calendar."""
    values = clean_submission(body, earliest=date.today())
    venue = await resolve_submission_venue(session, values)

    # Held until this request's transaction ends, so the count-then-insert below and the
    # duplicate checks cannot interleave with another submission's: without it, parallel
    # requests from someone with 19 pending would each count 19 and all be saved, and two
    # people sending the same show at once would both pass the duplicate check. One lock
    # for everyone rather than one per submitter, because the second case crosses
    # submitters; the form sees a handful of submissions a day, so nobody waits on it.
    await session.execute(select(func.pg_advisory_xact_lock(SUBMISSION_LOCK_KEY)))

    pending = (await session.execute(
        select(func.count()).select_from(ShowSubmission).where(
            ShowSubmission.submitted_by == who,
            ShowSubmission.status == SubmissionStatus.pending.value,
        )
    )).scalar_one()
    if pending >= MAX_PENDING_PER_SUBMITTER:
        raise HTTPException(
            status_code=429,
            detail=(
                f"You have {pending} shows waiting for review already. Once some of "
                "those are looked at you can send more."
            ),
        )

    # Two "already" checks. Both save the submitter a wait for an answer that is already
    # known, and save the admin a duplicate to reject.
    venue_slug = venue.slug if venue is not None else _slugify(values["new_venue_name"])
    key = show_key(values["name"], values["date"], venue_slug)

    # Only a listed venue can have anything on the calendar yet.
    if venue is not None:
        existing = (await session.execute(
            select(Event).where(Event.hash == key)
        )).scalar_one_or_none()
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f'"{existing.name}" on {existing.date.isoformat()} is already on the calendar.',
            )

    # Anything can already be in the queue, at a listed venue or a proposed one. The hash
    # cannot be matched in SQL — submissions do not store one — so every pending row on
    # that date is compared here; there are only ever a few.
    same_day = (await session.execute(
        select(ShowSubmission)
        .options(joinedload(ShowSubmission.venue))
        .where(
            ShowSubmission.date == values["date"],
            ShowSubmission.status == SubmissionStatus.pending.value,
        )
    )).unique().scalars().all()
    if any(
        show_key(s.name, s.date, submission_venue_slug(s)) == key for s in same_day
    ):
        raise HTTPException(
            status_code=409,
            detail="Someone has already sent this show in. It's waiting for review.",
        )

    submission = ShowSubmission(submitted_by=who, **values)
    session.add(submission)
    await session.commit()

    logger.info(
        f"[submit] {who} proposed submission {submission.id} {submission.name!r} "
        f"on {submission.date} at "
        f"{venue.slug if venue else 'new venue ' + repr(submission.new_venue_name)}"
    )
    return {"ok": True, "id": submission.id, "name": submission.name}
