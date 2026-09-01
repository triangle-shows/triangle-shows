"""Tests for hand-added events and the venues they hang off.

An admin creating rows by hand is the one write path where a mistake reaches the public
calendar with no scraper in between to correct it on the next run. Most of the protection
comes from reuse rather than from new validation — a manual event is built as a
ScrapedEvent, so it inherits the title cleaning, URL validation and dedup hash that
scraped events already get — and the assertions below are mostly about that reuse staying
intact.

The load-bearing one is TestManualVenuesAreNeverScraped. A manual venue has no scraper by
design, so if scrape_all ever stops excluding it, every promoter an admin has created
writes a failed ScrapeLog on every cycle — four times a day, forever — and the scrape looks
broken in the logs while working perfectly.
"""

import inspect
from datetime import date, time

import pytest

from app.models import MANUAL_SCRAPER_TYPE, Venue
from app.scrapers.base import ScrapedEvent
from app.scrapers.manager import ScrapeManager, event_from_scraped


# --- The guardrail that matters most ---


class _CapturingSession:
    """Records the statements handed to execute(), and returns nothing.

    Enough to watch which venues scrape_all asks for, without a database.
    """

    def __init__(self):
        self.statements = []

    async def execute(self, statement, *args, **kwargs):
        self.statements.append(statement)

        class _Result:
            def scalars(self_inner):
                return self_inner

            def all(self_inner):
                return []

            def unique(self_inner):
                return self_inner

        return _Result()

    async def commit(self):
        return None

    async def flush(self):
        return None


def _rendered_venue_query(method: str, **kwargs) -> str:
    """The SQL a bulk-scrape entry point issues, with bind values inlined.

    literal_binds matters. Plain str() on a SQLAlchemy statement renders values as bind
    parameters *named after their column* — `venues.scraper_type != :scraper_type_1` — so an
    assertion looking for "scraper_type" passes on a query that filters the column to
    anything at all. The first version of these tests did exactly that and stayed green
    against a query with the manual exclusion removed. Inlining the values is what makes
    them test the rule rather than the column name.
    """
    import asyncio

    session = _CapturingSession()
    manager = ScrapeManager(session)

    # reclassify_all runs at the end and needs more of a session than the stub provides;
    # the venue query is the subject here.
    async def _noop():
        return None

    manager.reclassify_all = _noop
    asyncio.run(getattr(manager, method)(**kwargs))
    assert session.statements, f"{method} issued no query"
    return str(
        session.statements[0].compile(compile_kwargs={"literal_binds": True})
    )


class TestManualVenuesAreNeverScraped:
    @staticmethod
    def _venue_query_sql(scraper_types=None) -> str:
        return _rendered_venue_query("scrape_all", scraper_types=scraper_types)

    def test_the_venue_query_filters_out_manual_venues(self):
        assert f"!= '{MANUAL_SCRAPER_TYPE}'" in self._venue_query_sql()

    def test_still_filtered_when_scraper_types_is_given(self):
        """A regression shape worth naming: an if/else that replaces the base query rather
        than narrowing it would drop the exclusion whenever a type filter was supplied."""
        sql = self._venue_query_sql(scraper_types=["ticketmaster"])
        assert f"!= '{MANUAL_SCRAPER_TYPE}'" in sql, (
            "the manual exclusion was replaced by the scraper_types filter, not added to it"
        )
        assert "'ticketmaster'" in sql, "the caller's own filter was lost"

    def test_asking_for_manual_explicitly_is_still_refused(self):
        """There is nothing to scrape, so naming it is a mistake rather than an override."""
        sql = self._venue_query_sql(scraper_types=[MANUAL_SCRAPER_TYPE])
        assert f"!= '{MANUAL_SCRAPER_TYPE}'" in sql, (
            "the exclusion should survive even an explicit request"
        )

    def test_get_scraper_returns_none_for_a_manual_venue(self):
        """Belt and braces: scrape_all filters these out, but a manual venue arriving by
        another path should be a clear no-op rather than look like a broken scraper_type."""
        manager = ScrapeManager(_CapturingSession())
        venue = Venue(
            name="Sharp 9 Gallery", slug="sharp-9", city="Durham",
            size_category="small", scraper_type=MANUAL_SCRAPER_TYPE, color="#7fb069",
        )
        assert manager._get_scraper(venue) is None

    def test_a_real_scraper_type_is_unaffected(self):
        """Sanity: the manual short-circuit must not swallow every venue."""
        manager = ScrapeManager(_CapturingSession())
        venue = Venue(
            name="DPAC", slug="dpac", city="Durham", size_category="large",
            scraper_type="ticketmaster", ticketmaster_venue_id="KovZpZA",
            color="#e88873",
        )
        assert manager._get_scraper(venue) is not None


# --- Reuse of the scraper's own construction ---


class TestEventFromScraped:
    def _scraped(self, **kw) -> ScrapedEvent:
        base = dict(name="Sub Rosa", date=date(2026, 9, 30), venue_slug="motorco",
                    source="manual")
        base.update(kw)
        return ScrapedEvent(**base)

    def test_copies_every_scraped_field(self):
        """The point of extracting this helper was that the admin path and the scrape path
        cannot drift. If ScrapedEvent gains a field that Event also has, this catches the
        mapping not being updated."""
        se = self._scraped(
            artist="Sub Rosa", support_artists="Openers", doors_time=time(19, 0),
            show_time=time(20, 0), ticket_url="https://example.test/t",
            price_min=10.0, price_max=15.0, genre="rock", subgenre="indie",
            age_restriction="18+", description="A show.", external_id="abc",
            source_url="https://example.test/e",
        )
        event = event_from_scraped(se, venue_id=7)

        assert event.venue_id == 7
        for field in (
            "name", "artist", "support_artists", "date", "doors_time", "show_time",
            "ticket_url", "price_min", "price_max", "genre", "subgenre", "status",
            "age_restriction", "description", "source", "source_url", "external_id",
            "hash",
        ):
            assert getattr(event, field) == getattr(se, field), f"{field} was not copied"

    def test_does_not_set_is_manually_created(self):
        """Nothing in the scraper path may write that flag — it is what stops reconcile
        deleting a hand-added row, and a scraper writing it would make it meaningless.
        The admin endpoint sets it explicitly instead."""
        event = event_from_scraped(self._scraped(), venue_id=1)
        assert not getattr(event, "is_manually_created", False)

    def test_the_hash_matches_what_a_scraper_would_compute(self):
        """Deliberate, and the reason a manual event goes through ScrapedEvent at all: a
        venue that later lists a show an admin already added matches the admin's row by
        hash and enriches it, instead of inserting a second listing."""
        manual = self._scraped(source="manual")
        scraped = self._scraped(source="motorco")
        assert manual.hash == scraped.hash

    def test_a_hostile_ticket_url_is_dropped_by_the_dataclass(self):
        """Not new validation — ScrapedEvent.__post_init__ already does this for scraped
        data, and routing manual input through it is what makes the admin form inherit it.
        """
        event = event_from_scraped(
            self._scraped(ticket_url="javascript:alert(1)"), venue_id=1
        )
        assert event.ticket_url is None


# --- The endpoint's own rules ---


class TestManualEventEndpointRules:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        from app.api import admin

        return inspect.getsource(admin.create_manual_event)

    def test_both_manual_flags_are_set(self, source):
        """They mean different things and both are needed. is_manually_created stops
        reconcile deleting the row; is_manual_override stops reclassify_all overwriting the
        live-music verdict — without which an event added as "Comedy Showcase" would be
        auto-flagged non-live and vanish from the calendar it was added to."""
        assert "is_manually_created = True" in source
        assert "is_manual_override = True" in source

    def test_it_is_approved_on_creation(self, source):
        """Adding it by hand *is* the review, so it should not land in the queue asking the
        admin to confirm a decision they just made."""
        assert "approved_at" in source

    def test_a_clashing_hash_is_a_409_not_a_500(self, source):
        """Event.hash is unique. Letting the IntegrityError escape would turn "you already
        added this" into an opaque server error."""
        assert "status_code=409" in source
        assert "already on the calendar" in source

    def test_the_date_is_bounded(self, source):
        assert "MANUAL_EVENT_MIN_DATE" in source
        assert "MANUAL_EVENT_MAX_YEARS_AHEAD" in source

    def test_the_bounds_are_sane(self):
        from app.api.admin import MANUAL_EVENT_MAX_YEARS_AHEAD, MANUAL_EVENT_MIN_DATE

        assert MANUAL_EVENT_MIN_DATE < date.today(), "the archive must be reachable"
        assert 1 <= MANUAL_EVENT_MAX_YEARS_AHEAD <= 5, (
            "wide enough for a real announcement, narrow enough that a mistyped year is "
            "rejected rather than creating a row no view shows"
        )

    def test_prices_are_checked_against_each_other(self, source):
        assert "price_max < body.price_min" in source


class TestEmptyManualVenuesAreHidden:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        from app.api import venues

        return inspect.getsource(venues.list_venues)

    def test_manual_venues_with_no_upcoming_events_are_skipped(self, source):
        assert "MANUAL_SCRAPER_TYPE" in source
        assert "count == 0" in source

    def test_scraped_venues_are_never_skipped(self, source):
        """A scraped venue with an empty calendar is still a real place having a quiet
        month, and it fills up again on the next scrape. Only the hand-added kind is a
        filter that would select nothing forever."""
        assert "venue.scraper_type == MANUAL_SCRAPER_TYPE" in source, (
            "the skip must be conditional on the venue being hand-added"
        )

    def test_the_count_matches_the_public_calendar(self, source):
        """Counting rows the calendar would not show — past events, or duplicates an admin
        hid — would make the sidebar promise events that are not there."""
        assert "duplicate_of_id" in source
        assert "date.today()" in source


class TestHiddenClassActuallyHides:
    """`.hidden { display:none }` is a single class, so any id rule that also sets
    `display` outranks it and the element never hides.

    This shipped broken once: `#newVenue { display:grid }` (specificity 1-0-0) beat
    `.hidden` (0-1-0), so the new-promoter fields were permanently visible however the
    class was toggled. Nothing failed and no error appeared — the panel was simply always
    open, which reads as a layout choice rather than a bug.

    Structural rather than behavioural because the admin page is a string of CSS and JS
    with no stylesheet for a test to query, and this failure mode is invisible in the DOM:
    the class *is* applied, it just does not win.
    """

    @pytest.fixture(scope="class")
    def parts(self):
        import re

        from app.admin_ui import ADMIN_HTML

        css = re.search(r"<style>(.*?)</style>", ADMIN_HTML, re.S).group(1)
        js = re.search(r"<script>(.*?)</script>", ADMIN_HTML, re.S).group(1)
        return ADMIN_HTML, css, js

    def test_every_toggled_id_can_actually_be_hidden(self, parts):
        import re

        html, css, js = parts
        toggled = set(re.findall(r"\$\('#(\w+)'\)\.classList\.toggle\('hidden'", js))
        toggled |= set(re.findall(r'id="(\w+)"[^>]*class="[^"]*hidden', html))
        assert toggled, "found nothing toggled with .hidden — has the mechanism changed?"

        broken = []
        for element_id in sorted(toggled):
            # The element's *own* rule: '#id' followed only by whitespace then '{'. A
            # descendant rule like '#add .actions { display:flex }' styles the child and is
            # irrelevant here, which is why this is not a substring search.
            own = re.findall(r"#" + element_id + r"\s*\{([^}]*)\}", css)
            if not any(re.search(r"\bdisplay\s*:", rule) for rule in own):
                continue  # display unset, so .hidden wins on its own
            if re.search(
                r"#" + element_id + r"\.hidden\s*\{[^}]*display\s*:\s*none", css
            ):
                continue  # specificity matched deliberately
            broken.append(element_id)

        assert not broken, (
            "these ids set `display` in an id rule and are toggled with .hidden, so the "
            f"class cannot hide them: {broken}. Add `#<id>.hidden {{ display:none; }}`."
        )


class TestScrapeIndieAlsoSkipsManualVenues:
    """scrape_indie is a second entry point, and it used to build its own venue query.

    That query filtered only on `scraper_type != "ticketmaster"`, so it did *not* pick up
    the manual-venue exclusion — every promoter an admin created would be scraped there,
    fail for want of a scraper, and write a failed ScrapeLog. It is a scheduled job, so
    that is three times a day, and it was only dormant in production because
    ENABLE_SCHEDULER is false. It now delegates to scrape_all.
    """

    @staticmethod
    def _sql(method: str) -> str:
        return _rendered_venue_query(method)

    def test_scrape_indie_excludes_manual_venues(self):
        assert f"!= '{MANUAL_SCRAPER_TYPE}'" in self._sql("scrape_indie"), (
            "scrape_indie does not exclude manual venues — it has probably gone back to "
            "building its own query"
        )

    def test_scrape_indie_still_excludes_ticketmaster(self):
        """The exclusion it existed for in the first place must survive the refactor."""
        assert "'ticketmaster'" in self._sql("scrape_indie")

    def test_scrape_ticketmaster_excludes_manual_venues(self):
        assert f"!= '{MANUAL_SCRAPER_TYPE}'" in self._sql("scrape_ticketmaster")

    def test_there_is_only_one_venue_query_in_the_class(self):
        """The structural fix. Two entry points building their own `select(Venue)` is how
        the exclusion went missing from one of them; any future venue rule should apply
        everywhere by construction rather than by being remembered twice.

        Parsed rather than grepped. A substring count also matches the docstring in
        scrape_indie, which quotes the query it used to build — so the first version of this
        test failed on its own explanatory prose. Counting AST call nodes measures the code.
        """
        import ast
        import inspect
        import textwrap

        from app.scrapers import manager as manager_module

        tree = ast.parse(textwrap.dedent(inspect.getsource(manager_module.ScrapeManager)))
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "select"
            and any(isinstance(a, ast.Name) and a.id == "Venue" for a in node.args)
        ]
        assert len(calls) == 1, (
            f"{len(calls)} venue-selection queries in ScrapeManager (expected 1) — the "
            "manual exclusion now has to be repeated in each, which is the bug this "
            "consolidation fixed"
        )


class TestDeleteRules:
    """Deleting is the only admin control that destroys rather than hides, and hand-added
    rows are the only ones with no other way out — reconcile deliberately will not touch
    them, and at a hand-added venue they are never even considered, because scrape_all
    skips that venue. Everything protecting them from the scraper is what makes delete
    necessary.
    """

    @pytest.fixture(scope="class")
    def event_source(self) -> str:
        from app.api import admin

        return inspect.getsource(admin.delete_manual_event)

    @pytest.fixture(scope="class")
    def venue_source(self) -> str:
        from app.api import admin

        return inspect.getsource(admin.delete_manual_venue)

    def test_only_hand_added_events_can_be_deleted(self, event_source):
        """A scraped event returns on the next scrape, so a general delete would look
        broken while being the one control able to destroy an archive row."""
        assert "if not event.is_manually_created" in event_source
        assert "status_code=400" in event_source

    def test_the_refusal_says_what_to_do_instead(self, event_source):
        assert "mark it non-live" in event_source or "non-live" in event_source

    def test_deleting_a_survivor_reports_the_rows_it_un_hides(self, event_source):
        """duplicate_of_id is ON DELETE SET NULL, so deleting a survivor puts everything
        folded into it back on the public calendar. A delete that silently *adds* listings
        is surprising enough to report."""
        assert "restored_duplicates" in event_source
        assert "duplicate_of_id == event_id" in event_source

    def test_only_hand_added_venues_can_be_deleted(self, venue_source):
        """seed_venues() would recreate a seeded venue on the next boot with a new id,
        while its events stayed cascade-deleted — the venue appears to come back and the
        data is gone, which looks like the delete failed."""
        assert "venue.scraper_type != MANUAL_SCRAPER_TYPE" in venue_source

    def test_a_venue_with_events_is_refused_with_a_count(self, venue_source):
        """Venue.events cascades with delete-orphan, so one click on a venue holding a run
        of shows would take all of them with nothing naming what was lost."""
        assert "status_code=409" in venue_source
        assert "event_count" in venue_source

    def test_the_venue_count_covers_past_events_too(self, venue_source):
        """A past event is still a row, and cascading it away silently is the thing being
        prevented — so this count must not reuse the upcoming-only helper."""
        assert "Event.venue_id == venue_id" in venue_source
        assert "date.today()" not in venue_source, (
            "the guard counts only upcoming events, so a venue whose shows have all passed "
            "would be deletable and would cascade them away"
        )

    def test_both_deletes_are_registered_as_DELETE(self):
        from app.main import app

        paths = {
            r.path for r in app.routes
            if getattr(r, "path", "").startswith("/admin/api") and "DELETE" in r.methods
        }
        assert "/admin/api/events/{event_id}" in paths
        assert "/admin/api/venues/{venue_id}" in paths


class TestNothingScheduledDeletesEvents:
    """Past events are kept, and no scheduled job may delete any.

    There used to be a nightly job removing every event more than 7 days old. It never ran
    anywhere — ENABLE_SCHEDULER is false in cloudbuild.yaml, backend/.env.example,
    docs/SELF-HOSTING.md and config.py's default — which is the only reason production still
    holds events back to January. Removed rather than commented out, because commented-out
    code gets re-enabled by someone reading the comment as a to-do.

    Three parts of the app already assume past events persist: plan_upsert bounds reconcile
    to `today <= date <= horizon` so a scrape cannot touch the archive; the admin queue and
    reclassify_all() work from reclassify_floor(), which is 30 days back; and the public
    calendar scrolls backwards with no floor on /api/events. A 7-day cleanup contradicted
    all three, and additionally bypassed scraper_must_not_delete by issuing a bulk DELETE —
    so it would have silently undone the hand-added and flagged-duplicate protections a week
    after the fact.

    These assertions are about the *module*, not one function, because the point is that no
    scheduled job deletes events — not that one particular job was fixed.
    """

    @pytest.fixture(scope="class")
    def scheduler_module(self):
        from app import scheduler

        return scheduler

    @pytest.fixture(scope="class")
    def source(self, scheduler_module) -> str:
        return inspect.getsource(scheduler_module)

    def test_the_cleanup_job_is_gone(self, scheduler_module):
        assert not hasattr(scheduler_module, "cleanup_past_events_job"), (
            "the past-event cleanup job is back; past events are kept deliberately"
        )

    def test_no_scheduled_job_issues_a_delete(self, source):
        """Checks the code, not just that one name is absent. Any new job that deletes rows
        has to trip this and be justified."""
        import ast
        import textwrap

        tree = ast.parse(textwrap.dedent(source))
        deletes = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "delete"
        ]
        assert not deletes, "a scheduled job now issues a delete()"
        assert "session.delete" not in source, "a scheduled job now deletes via the ORM"

    def test_the_module_cannot_delete_without_a_new_import(self, scheduler_module):
        """The structural version of the same rule: with no `delete` and no Event model in
        scope, adding a deletion here requires adding an import, which is visible in review.
        """
        assert not hasattr(scheduler_module, "delete")
        assert not hasattr(scheduler_module, "Event")

    def test_the_scrape_jobs_still_exist(self, scheduler_module):
        """Removing the cleanup must not have removed the jobs the scheduler is for."""
        assert hasattr(scheduler_module, "scrape_ticketmaster_job")
        assert hasattr(scheduler_module, "scrape_indie_job")

    def test_configure_scheduler_registers_only_the_two_scrapes(self, scheduler_module):
        """Registered by id, so a third job appearing is a deliberate decision rather than
        something that slips in."""
        registered = set()

        class _Recorder:
            def add_job(self, func, trigger, id=None, **kwargs):
                registered.add(id)

        original = scheduler_module.scheduler
        scheduler_module.scheduler = _Recorder()
        try:
            scheduler_module.configure_scheduler()
        finally:
            scheduler_module.scheduler = original

        assert registered == {"scrape_ticketmaster", "scrape_indie"}, (
            f"unexpected scheduled jobs: {sorted(registered)}"
        )
