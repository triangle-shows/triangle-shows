"""
Scrape orchestrator: runs all venue scrapers, matches each run against the events already
stored for that venue, and upserts events + scrape logs into the database.

Matching is the interesting part. Rows are identified by the venue's own event ID first
and only then by a hash of the title, because titles get edited (support acts announced,
events renamed) and a title-only identity turns every edit into a second listing. Rows the
source has stopped listing are removed, subject to the safety valves below.

Role: Triggered by POST /api/scrape (called by the scheduler every 6 hours or
by Cloud Scheduler). Sits between the individual scrapers and the database —
it owns the fan-out, error isolation, and upsert logic.
Requires: TICKETMASTER_API_KEY (via app.config.settings), async PostgreSQL
session, and all scraper modules in app.scrapers/.
"""

# --- Imports ---
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.classifier import classification_updates, reclassify_floor, ALWAYS_LIVE_VENUE_SLUGS
from app.models import Venue, Event, ScrapeLog, SeriesOverride, MANUAL_SCRAPER_TYPE
from app.redaction import describe_exception
from app.scrapers.base import BaseScraper, ScrapedEvent
from app.scrapers.ticketmaster import TicketmasterScraper

logger = logging.getLogger(__name__)


# --- Reconcile safety valves ---
#
# Reconciling deletes rows, so it has to stay skeptical of its own input. A scraper that
# half-succeeds — a selector that stops matching, a paginated feed that returns page one —
# looks exactly like a venue that cancelled most of its calendar. These two constants
# decide when a run is too lossy to trust.

# Below this many orphans, reconcile always proceeds: small absolute numbers are normal
# churn (a cancelled show, a series wrapping up) and never worth second-guessing.
RECONCILE_ALWAYS_ALLOW = 4

# Above that floor, refuse to delete if orphans exceed this share of the venue's rows in
# the scraped date window. Losing half a venue's calendar in one run means the scraper
# broke, not that the venue emptied out.
RECONCILE_MAX_ORPHAN_FRACTION = 0.5


# --- Upsert planning ---


@dataclass
class UpsertPlan:
    """What a scrape run should do to the rows already stored for a venue.

    Pure data — computed by plan_upsert() without touching the session, so the matching
    rules can be tested without a database.
    """

    updates: list[tuple[ScrapedEvent, Any]] = field(default_factory=list)   # (scraped, row to update)
    inserts: list[ScrapedEvent] = field(default_factory=list)               # no row matched
    superseded: list[Any] = field(default_factory=list)                     # older rows folded into a match
    expired: list[Any] = field(default_factory=list)                        # rows the source no longer lists
    reconcile_skipped: bool = False                                         # True when the safety valve tripped


def dedupe_scraped(scraped_events: list[ScrapedEvent]) -> list[ScrapedEvent]:
    """Collapse repeats within a single scrape run, keeping the first of each.

    Sites list the same event twice (a featured section plus the main listing), and
    without this the repeats collide on Event.hash inside one INSERT batch.
    """
    out: list[ScrapedEvent] = []
    seen_ext: set[tuple[str, date]] = set()
    seen_hash: set[str] = set()

    for se in scraped_events:
        ext_key = (se.external_id, se.date) if se.external_id else None
        if ext_key is not None and ext_key in seen_ext:
            continue
        if se.hash in seen_hash:
            continue
        if ext_key is not None:
            seen_ext.add(ext_key)
        seen_hash.add(se.hash)
        out.append(se)

    return out


def scraper_must_not_delete(row: Any) -> bool:
    """True for a row whose existence is a human decision, so reconcile must leave it.

    Reconcile deletes any row inside the scraped window the scrape did not return, which
    is right for a listing a venue dropped and wrong for anything a person put there
    deliberately. This is the one place that distinction lives; add to it rather than
    growing another predicate somewhere else.

    Two flags qualify:

    `is_manually_created` — a hand-added event, which a scraper by definition never
    returns, so without this every manual event at a scraped venue would be deleted on the
    next run of that venue's scraper.

    `duplicate_of_id` — a row an admin folded into another (issue #63). It is hidden rather
    than gone precisely so the judgement stays reversible and auditable, so deleting it
    destroys the thing the column exists for. Easy to miss, because the row is already
    hidden from the calendar: nothing looks wrong until someone tries to unmark it and
    finds it gone.

    The flags rather than `source`: _apply_scraped() writes `row.source = se.source`
    whenever a scraped event matches a stored row, so a source-based test stops protecting
    a row the first time the venue lists the same show — and the row is deleted on the run
    after that, having looked protected the whole time. Nothing in the scraper path writes
    these flags, which is the property that makes them trustworthy here.

    Both deletion paths in plan_upsert have to consult this, not just the orphan filter.
    The loser of the candidate sort is `superseded`, and superseded rows are deleted in the
    same pass — see the call sites.

    getattr, not attribute access: plan_upsert is deliberately callable with lightweight
    stand-ins (see tests/test_dedup.py), and this should not force every one of them to
    declare every flag.
    """
    return bool(
        getattr(row, "is_manually_created", False)
        or getattr(row, "duplicate_of_id", None) is not None
    )


def plan_upsert(existing: list[Any], scraped_events: list[ScrapedEvent], today: date) -> UpsertPlan:
    """Match a scrape run against the rows already stored for one venue.

    Rows are matched on (external_id, date) first and only then on the title hash. That
    order is the whole point: the hash is built from the title, so when a venue edits an
    event — adding a support act, renaming it, prefixing a series — the hash changes and
    a hash-only match sees a brand-new event. The venue's own ID does not change, so it
    identifies the show across a rename where the title cannot.

    Rows for the same show that were created by earlier renames are `superseded` and get
    folded into the survivor. Rows inside the scraped date window that the source stopped
    listing are `expired`.

    Only attribute access is used on `existing` rows (.id, .date, .hash, .external_id,
    .updated_at), so tests can pass stand-ins instead of ORM objects.
    """
    plan = UpsertPlan()
    if not scraped_events:
        return plan

    by_ext: dict[tuple[str, date], list[Any]] = defaultdict(list)
    by_hash: dict[str, Any] = {}
    for row in existing:
        if row.external_id:
            by_ext[(row.external_id, row.date)].append(row)
        by_hash[row.hash] = row

    claimed: set[int] = set()

    for se in scraped_events:
        candidates: list[Any] = []
        if se.external_id:
            candidates.extend(by_ext.get((se.external_id, se.date), []))
        hash_row = by_hash.get(se.hash)
        if hash_row is not None:
            candidates.append(hash_row)

        # An external_id match and a hash match are often the same row; and a row already
        # claimed by an earlier scraped event must not be claimed twice.
        unique: list[Any] = []
        picked: set[int] = set()
        for row in candidates:
            if row.id in claimed or row.id in picked:
                continue
            picked.add(row.id)
            unique.append(row)

        if not unique:
            plan.inserts.append(se)
            continue

        # Rank, most significant key first: not-a-duplicate, then hand-added, then the row
        # whose title already matches what the source says now, then the most recently seen
        # row. Every key is total, so the choice is stable.
        #
        # "Not a duplicate" outranks "hand-added" rather than the other way round. A row an
        # admin flagged as a duplicate has already been judged not to be the canonical one,
        # so it must never be chosen as the survivor — and that judgement holds even for a
        # row that was *also* added by hand, which is the combination the manual key alone
        # would get backwards.
        #
        # The manual key still leads over the hash and recency keys: a venue that later
        # lists a show an admin had already added by hand should enrich the curated row via
        # _apply_scraped, not discard it in favour of the scraped one.
        unique.sort(
            key=lambda r: (
                getattr(r, "duplicate_of_id", None) is None,
                getattr(r, "is_manually_created", False),
                r.hash == se.hash,
                r.updated_at or datetime.min,
            ),
            reverse=True,
        )
        keep, rest = unique[0], unique[1:]
        claimed.add(keep.id)
        plan.updates.append((se, keep))
        for row in rest:
            claimed.add(row.id)
            # Losing the sort means "not the canonical row", which is not the same as
            # "delete me" — superseded rows are deleted further down. A human-owned row
            # that loses is simply left alone: it stays claimed, so the orphan filter
            # below skips it too, and it survives the run untouched.
            #
            # This is the second deletion path, and it is reachable in two ways the sort
            # key cannot fix by itself. One scraped event can match both a survivor and a
            # duplicate an admin folded into it (via external_id and hash respectively),
            # which would delete the audit trail; and two hand-added rows can match the
            # same scraped event, which would delete one of them.
            if not scraper_must_not_delete(row):
                plan.superseded.append(row)

    # --- Reconcile ---
    # Only rows inside the window the scraper actually covered are candidates. A scraper
    # that returns three months of listings says nothing about a show ten months out, and
    # deleting past events would wipe the archive every run — venues drop them from their
    # own listings as soon as they happen.
    horizon = max(se.date for se in scraped_events)
    in_window = [r for r in existing if today <= r.date <= horizon]
    orphans = [
        r for r in in_window if r.id not in claimed and not scraper_must_not_delete(r)
    ]

    if orphans:
        too_lossy = (
            len(orphans) > RECONCILE_ALWAYS_ALLOW
            and len(orphans) > len(in_window) * RECONCILE_MAX_ORPHAN_FRACTION
        )
        if too_lossy:
            plan.reconcile_skipped = True
        else:
            plan.expired = orphans

    return plan


def event_from_scraped(se: ScrapedEvent, venue_id: int) -> Event:
    """Build a new Event row from a ScrapedEvent.

    Extracted so the admin's manual-add endpoint constructs rows the same way a scrape
    does, rather than maintaining a parallel field list that drifts. That matters more
    than it looks: ScrapedEvent.__post_init__ cleans the title and validates the ticket
    and image URLs, and ScrapedEvent.hash is the dedup key. A hand-added event routed
    through the same dataclass therefore gets the same normalization, and — because the
    hash is computed identically — a venue that later lists a show an admin already added
    matches the admin's row instead of inserting a second one.

    Does not set is_manually_created; the caller does. Nothing in the scraper path should
    ever write that flag (see scraper_must_not_delete).
    """
    return Event(
        external_id=se.external_id,
        venue_id=venue_id,
        name=se.name,
        artist=se.artist,
        support_artists=se.support_artists,
        date=se.date,
        doors_time=se.doors_time,
        show_time=se.show_time,
        ticket_url=se.ticket_url,
        price_min=se.price_min,
        price_max=se.price_max,
        image_url=se.image_url,
        genre=se.genre,
        subgenre=se.subgenre,
        status=se.status,
        age_restriction=se.age_restriction,
        description=se.description,
        source=se.source,
        source_url=se.source_url,
        hash=se.hash,
    )


# --- ScrapeManager ---

class ScrapeManager:
    """Orchestrates scraping for all venues."""

    def __init__(self, session: AsyncSession):
        self.session = session
        # Result of the reclassify pass at the end of the last bulk scrape, so a caller
        # can report what the classifier actually did. Without this, the only record was
        # a log line — and Cloud Run drops stdout from work that runs outside a request,
        # which is exactly how a reclassify pass silently doing nothing went unnoticed.
        self.last_reclassify: Optional[dict] = None

    def _get_scraper(self, venue: Venue) -> Optional[BaseScraper]:
        """Instantiate the correct scraper for a venue, or None if there is none.

        A manual venue (a promoter, a one-off series) has no source to scrape, so None is
        the correct and expected answer for it — not a misconfiguration. scrape_all filters
        these out before reaching here; this branch exists so that a manual venue arriving
        by some other path fails as a clear no-op rather than looking like a broken
        scraper_type.
        """
        if venue.scraper_type == MANUAL_SCRAPER_TYPE:
            return None
        if venue.scraper_type == "ticketmaster":
            if not venue.ticketmaster_venue_id:
                logger.warning(f"No TM venue ID for {venue.slug}")
                return None
            return TicketmasterScraper(
                venue_slug=venue.slug,
                venue_tm_id=venue.ticketmaster_venue_id,
                api_key=settings.TICKETMASTER_API_KEY,
                config=venue.scraper_config,
            )
        # Remaining scraper types are imported lazily to avoid circular imports
        # and to keep startup time fast when only a subset of scrapers are used.
        elif venue.scraper_type == "rhp_events":
            from app.scrapers.rhp_events import RHPEventsScraper
            return RHPEventsScraper(venue.slug, venue.scraper_config)
        elif venue.scraper_type == "tribe_events":
            from app.scrapers.tribe_events import TribeEventsScraper
            return TribeEventsScraper(venue.slug, venue.scraper_config)
        elif venue.scraper_type == "squarespace":
            from app.scrapers.squarespace import SquarespaceScraper
            return SquarespaceScraper(venue.slug, venue.scraper_config)
        elif venue.scraper_type == "eventprime":
            from app.scrapers.eventprime import EventPrimeScraper
            return EventPrimeScraper(venue.slug, venue.scraper_config)
        elif venue.scraper_type == "motorco":
            from app.scrapers.motorco import MotorcoScraper
            return MotorcoScraper(venue.slug, venue.scraper_config)
        elif venue.scraper_type == "carolina_theatre":
            from app.scrapers.carolina_theatre import CarolinaTheatreScraper
            return CarolinaTheatreScraper(venue.slug, venue.scraper_config)
        elif venue.scraper_type == "venuepilot":
            from app.scrapers.venuepilot import VenuePilotScraper
            return VenuePilotScraper(venue.slug, venue.scraper_config)
        elif venue.scraper_type == "koka_booth":
            from app.scrapers.koka_booth import KokaBoothScraper
            return KokaBoothScraper(venue.slug, venue.scraper_config)
        elif venue.scraper_type == "mec":
            from app.scrapers.mec import MECScraper
            return MECScraper(venue.slug, venue.scraper_config)
        elif venue.scraper_type == "webflow_cms":
            from app.scrapers.webflow_cms import WebflowCMSScraper
            return WebflowCMSScraper(venue.slug, venue.scraper_config)
        elif venue.scraper_type == "tickpick_organizer":
            from app.scrapers.tickpick_organizer import TickPickOrganizerScraper
            return TickPickOrganizerScraper(venue.slug, venue.scraper_config)
        else:
            logger.warning(f"Unknown scraper type: {venue.scraper_type}")
            return None

    # --- Per-venue scrape logic ---

    async def scrape_venue(self, venue: Venue) -> dict:
        """Scrape a single venue and upsert events."""
        # Refresh venue before accessing any attributes. If a previous scrape_venue call
        # ended in a rollback, all ORM objects in the session are expired; accessing an
        # expired attribute on an AsyncSession triggers a sync lazy-load → greenlet error.
        await self.session.refresh(venue)
        venue_slug = venue.slug
        venue_id = venue.id
        scraper_type = venue.scraper_type

        # Create a ScrapeLog row up front so we have a record even if the scrape crashes.
        log = ScrapeLog(
            venue_id=venue_id,
            scraper_type=scraper_type,
            started_at=datetime.utcnow(),
        )
        try:
            self.session.add(log)
            await self.session.flush()

            scraper = self._get_scraper(venue)
            if not scraper:
                raise ValueError(f"No scraper available for {venue_slug}")

            scraped_events = await scraper.scrape()
            counts = await self._upsert_events(venue_id, venue_slug, scraped_events)

            log.status = "success"
            log.events_found = len(scraped_events)
            log.events_created = counts["created"]
            log.events_updated = counts["updated"]
            log.finished_at = datetime.utcnow()
            log.duration_seconds = (log.finished_at - log.started_at).total_seconds()
            await self.session.commit()

            # merged/expired counts have no ScrapeLog column yet, so they live in the log
            # line and the API response. Individual removals are logged in _upsert_events.
            logger.info(
                f"[{venue_slug}] Scrape complete: {len(scraped_events)} found, "
                f"{counts['created']} created, {counts['updated']} updated, "
                f"{counts['merged']} merged, {counts['expired']} expired"
            )
            return {
                "venue": venue_slug,
                "status": "success",
                "found": len(scraped_events),
                **counts,
            }

        except Exception as e:
            # httpx.HTTPStatusError stringifies to include the full request URL, and the
            # Ticketmaster scraper authenticates with an ?apikey= query parameter — so the
            # raw exception text is a live credential. It reaches three sinks from here:
            # this log line, the scrape_logs row below, and the dict returned to
            # POST /api/scrape. describe_exception redacts once, at the top.
            #
            # It also names the exception class, which is the whole of what those three
            # sinks reported for a timeout: every httpx transport error has an empty
            # str(), so `error` came back as "" exactly when a venue was unreachable
            # (issue #15). "ReadTimeout" is not a diagnosis, but it separates "the venue
            # is down" from "our parser broke", which is the first question asked.
            message = describe_exception(e)
            # exc_info because the class name is not enough for the parser-broke case:
            # `AttributeError` needs the frame that raised it. Safe to attach here —
            # RedactingFormatter is a formatter rather than a filter specifically so
            # that rendered tracebacks are scrubbed too (see app/redaction.py).
            logger.error(f"[{venue_slug}] Scrape failed: {message}", exc_info=True)
            try:
                # Roll back the failed transaction before writing the error log,
                # otherwise the commit below will also fail.
                await self.session.rollback()
                log.status = "failed"
                log.error_message = message[:2000]  # cap length to fit DB column
                log.finished_at = datetime.utcnow()
                log.duration_seconds = (log.finished_at - log.started_at).total_seconds()
                self.session.add(log)
                await self.session.commit()
            except Exception as log_err:
                logger.warning(
                    f"[{venue_slug}] Could not write error log: {describe_exception(log_err)}",
                    exc_info=True,
                )
            return {"venue": venue_slug, "status": "failed", "error": message}

    # --- Upsert helpers ---

    @staticmethod
    def _apply_scraped(row: Event, se: ScrapedEvent) -> None:
        """Copy a freshly scraped event onto the row that represents it.

        The title and its hash are written back, because a row matched on external_id may
        be carrying a title the venue has since edited. Optional fields fall back to the
        stored value so a source that briefly omits a field does not blank out good data.
        """
        row.name = se.name
        row.artist = se.artist or row.artist
        row.hash = se.hash
        row.external_id = se.external_id or row.external_id
        row.source = se.source
        row.source_url = se.source_url or row.source_url
        row.price_min = se.price_min
        row.price_max = se.price_max
        row.status = se.status
        row.image_url = se.image_url or row.image_url
        row.ticket_url = se.ticket_url or row.ticket_url
        row.doors_time = se.doors_time or row.doors_time
        row.show_time = se.show_time or row.show_time
        row.support_artists = se.support_artists or row.support_artists
        row.genre = se.genre or row.genre
        row.subgenre = se.subgenre or row.subgenre
        row.age_restriction = se.age_restriction or row.age_restriction
        row.description = se.description or row.description
        row.updated_at = datetime.utcnow()

    async def _upsert_events(self, venue_id: int, venue_slug: str, scraped_events: list[ScrapedEvent]) -> dict:
        """Reconcile a scrape run against stored events. Returns per-outcome counts."""
        if not scraped_events:
            return {"created": 0, "updated": 0, "merged": 0, "expired": 0}

        scraped_events = dedupe_scraped(scraped_events)

        # Load the venue's whole calendar in one query. Matching on external_id and
        # spotting rows the source dropped both need the full picture, and no venue
        # holds more than a couple hundred rows.
        result = await self.session.execute(
            select(Event).where(Event.venue_id == venue_id)
        )
        existing = list(result.scalars().all())

        plan = plan_upsert(existing, scraped_events, datetime.utcnow().date())

        if plan.reconcile_skipped:
            logger.warning(
                f"[{venue_slug}] Reconcile skipped: {len(scraped_events)} events scraped would "
                f"orphan too large a share of the stored calendar. Leaving rows in place — "
                f"this usually means the scraper is partially broken, not that the venue emptied."
            )

        # Deletes go first, and get their own flush. Event.hash is globally unique, and a
        # surviving row is often about to take the hash a superseded row still holds.
        removals = (
            [(r, "superseded by another listing for the same show") for r in plan.superseded]
            + [(r, "no longer listed by the source") for r in plan.expired]
        )
        for row, why in removals:
            logger.info(f"[{venue_slug}] Removing event {row.id} ({row.date} {row.name!r}) — {why}")
            await self.session.delete(row)
        if removals:
            await self.session.flush()

        for se, row in plan.updates:
            self._apply_scraped(row, se)

        for se in plan.inserts:
            self.session.add(event_from_scraped(se, venue_id))

        await self.session.flush()
        return {
            "created": len(plan.inserts),
            "updated": len(plan.updates),
            "merged": len(plan.superseded),
            "expired": len(plan.expired),
        }

    # --- Classification ---

    async def reclassify_all(self) -> dict:
        """Recompute is_live_music for upcoming events and the recent past.

        Runs after every bulk scrape. Recurrence is a cross-event signal, so this
        always considers the full set of events in range regardless of which venues
        were just scraped. Admin decisions are respected: per-event manual overrides
        (is_manual_override=True) are left untouched, and series-level overrides are
        applied to every matching event — including instances scraped later.

        The range reaches RECLASSIFY_PAST_DAYS behind today, not just forward: the
        calendar can be scrolled back into recent dates, and those events need to
        respond to live/non-live changes too. Returns {events, changed} counts.
        """
        result = await self.session.execute(
            select(Event).where(Event.date >= reclassify_floor())
        )
        events = result.scalars().all()

        # Load series-level overrides, keyed to match classifier.normalize_series_name.
        so_result = await self.session.execute(select(SeriesOverride))
        series_overrides = {
            (so.venue_id, so.normalized_name): (so.is_live_music, so.note)
            for so in so_result.scalars().all()
        }

        # Resolve always-live venues (e.g. DPAC) to their ids for the exemption.
        venue_rows = await self.session.execute(select(Venue.id, Venue.slug))
        exempt_venue_ids = {
            vid for vid, slug in venue_rows.all() if slug in ALWAYS_LIVE_VENUE_SLUGS
        }

        # classification_updates omits manually-overridden rows (those flags survive),
        # forces always-live venues, and applies series overrides above auto.
        updates = classification_updates(
            events,
            series_overrides=series_overrides,
            exempt_venue_ids=exempt_venue_ids,
        )

        changed = 0
        unapproved = 0
        for ev in events:
            if ev.id not in updates:
                continue
            is_live, reason = updates[ev.id]
            if ev.is_live_music != is_live or ev.classification_reason != reason:
                # An approval records agreement with a specific verdict. If the verdict
                # itself moves, that approval is stale — clear it so the event returns
                # to the review queue instead of staying hidden behind an old decision.
                # Only a change of is_live_music counts; a reworded reason is not a
                # different answer and should not resurface work already reviewed.
                if ev.is_live_music != is_live and ev.approved_at is not None:
                    ev.approved_at = None
                    unapproved += 1
                ev.is_live_music = is_live
                ev.classification_reason = reason
                changed += 1

        await self.session.commit()
        logger.info(
            f"Reclassified {len(events)} events in range; {changed} changed, "
            f"{unapproved} approvals cleared (manual overrides preserved)"
        )
        return {"events": len(events), "changed": changed, "unapproved": unapproved}

    # --- Bulk scrape entry points ---

    async def scrape_all(self, scraper_types: Optional[list[str]] = None) -> list[dict]:
        """Scrape all venues (or those matching given scraper_types).

        Venues whose scraper_type is MANUAL_SCRAPER_TYPE are excluded unconditionally, and
        this is a guardrail rather than an optimisation. There is no scraper for them by
        design, so _get_scraper returns None, scrape_venue raises "No scraper available",
        and every one of them would write a failed ScrapeLog row on every cycle — four
        times a day, forever, for each promoter an admin adds. The scrape would still look
        broken in the logs while working perfectly.

        Excluded even when scraper_types names them explicitly: there is nothing to scrape,
        so an explicit request is a mistake rather than an override.
        """
        query = select(Venue).where(Venue.scraper_type != MANUAL_SCRAPER_TYPE)
        if scraper_types:
            query = query.where(Venue.scraper_type.in_(scraper_types))
        result = await self.session.execute(query)
        venues = result.scalars().all()

        results = []
        # Venues are scraped sequentially to keep the session state simple and avoid
        # concurrent writes on the same async session (AsyncSession is not thread-safe).
        for venue in venues:
            r = await self.scrape_venue(venue)
            results.append(r)

        # Recompute live-music flags across all upcoming events now that new data is in.
        self.last_reclassify = await self.reclassify_all()

        return results

    async def scrape_ticketmaster(self) -> list[dict]:
        """Scrape only Ticketmaster venues."""
        return await self.scrape_all(scraper_types=["ticketmaster"])

    async def scrape_indie(self) -> list[dict]:
        """Scrape only non-Ticketmaster venues."""
        query = select(Venue).where(Venue.scraper_type != "ticketmaster")
        result = await self.session.execute(query)
        venues = result.scalars().all()

        results = []
        for venue in venues:
            r = await self.scrape_venue(venue)
            results.append(r)

        # Recompute live-music flags across all upcoming events now that new data is in.
        self.last_reclassify = await self.reclassify_all()

        return results
