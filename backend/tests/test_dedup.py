"""
Tests for duplicate prevention: title normalization, run-level dedup, and upsert planning.

These cover the two ways the same show used to end up in the database twice — a venue
editing an event's title, and one scraper reporting a title in two encodings — plus the
reconcile pass that removes listings a source has dropped.

Every case here is drawn from a real pair found in the production data.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

import pytest

from app.scrapers.base import ScrapedEvent, clean_title
from app.scrapers.manager import (
    RECONCILE_ALWAYS_ALLOW,
    dedupe_scraped,
    plan_upsert,
)


TODAY = date(2026, 9, 1)


# --- Test helpers ---


@dataclass
class Row:
    """Stand-in for an Event row. plan_upsert only reads these attributes."""

    id: int
    date: date
    hash: str
    external_id: Optional[str] = None
    updated_at: Optional[datetime] = None
    # Both default to "ordinary scraped row" so every existing construction site keeps
    # working; only the manual-event and duplicate tests below set them.
    is_manually_created: bool = False
    duplicate_of_id: Optional[int] = None


def scraped(name: str, on: date = TODAY, external_id: Optional[str] = None) -> ScrapedEvent:
    return ScrapedEvent(
        name=name,
        date=on,
        venue_slug="the-pinhook",
        source="venuepilot",
        external_id=external_id,
    )


def row_for(se: ScrapedEvent, id: int, seen: Optional[datetime] = None) -> Row:
    """A stored row matching a scraped event exactly — the already-up-to-date case."""
    return Row(
        id=id,
        date=se.date,
        hash=se.hash,
        external_id=se.external_id,
        updated_at=seen or datetime(2026, 8, 27),
    )


# --- clean_title ---


class TestCleanTitle:
    def test_decodes_html_entities(self):
        assert clean_title("JoVia Armstrong &#038; alana amore colvin") == (
            "JoVia Armstrong & alana amore colvin"
        )

    def test_decodes_percent_escapes(self):
        assert clean_title("JoVia Armstrong %26 alana amore colvin") == (
            "JoVia Armstrong & alana amore colvin"
        )

    def test_both_encodings_converge(self):
        """The Shadowbox bug: one event, two pages, two encodings, two database rows."""
        entity = clean_title("Ben Copperhead &#038; David Menestres")
        percent = clean_title("Ben Copperhead %26 David Menestres")
        assert entity == percent

    def test_decodes_double_escaped_entities(self):
        assert clean_title("Cool Boy&amp;#039;s Tapes") == "Cool Boy's Tapes"

    def test_decodes_multibyte_percent_escapes(self):
        """Decoding the whole string, not each escape, is what makes this come out right."""
        assert clean_title("Cool Boy%E2%80%99s Tapes") == "Cool Boy’s Tapes"

    def test_collapses_whitespace(self):
        assert clean_title("Alex Cameron /  Josh De Costa") == "Alex Cameron / Josh De Costa"

    def test_leaves_a_bare_percent_alone(self):
        assert clean_title("Get 50% Off Before Doors") == "Get 50% Off Before Doors"

    def test_passes_through_empty_values(self):
        assert clean_title(None) is None
        assert clean_title("") == ""

    def test_applied_on_construction(self):
        assert scraped("A &#038; B").name == "A & B"

    def test_encodings_hash_identically(self):
        assert scraped("A &#038; B").hash == scraped("A %26 B").hash


# --- dedupe_scraped ---


class TestDedupeScraped:
    def test_collapses_repeated_hash(self):
        events = [scraped("Acopia"), scraped("Acopia")]
        assert len(dedupe_scraped(events)) == 1

    def test_collapses_repeated_external_id_on_one_date(self):
        """Two sections of one page describing the same show with different titles."""
        events = [scraped("Acopia", external_id="188044"),
                  scraped("Acopia / Lilly Flower", external_id="188044")]
        assert [e.name for e in dedupe_scraped(events)] == ["Acopia"]

    def test_keeps_same_external_id_on_different_dates(self):
        """A recurring series reuses its ID; each night is still its own event."""
        events = [scraped("Open Mic", TODAY, external_id="900"),
                  scraped("Open Mic", TODAY + timedelta(days=7), external_id="900")]
        assert len(dedupe_scraped(events)) == 2

    def test_keeps_distinct_events(self):
        events = [scraped("Acopia"), scraped("Anna Graves")]
        assert len(dedupe_scraped(events)) == 2


# --- plan_upsert: matching ---


class TestMatching:
    def test_unchanged_event_is_updated_not_reinserted(self):
        se = scraped("Acopia", external_id="188044")
        plan = plan_upsert([row_for(se, id=1)], [se], TODAY)
        assert plan.inserts == []
        assert [(s.name, r.id) for s, r in plan.updates] == [("Acopia", 1)]

    def test_new_event_is_inserted(self):
        plan = plan_upsert([], [scraped("Acopia", external_id="188044")], TODAY)
        assert [se.name for se in plan.inserts] == ["Acopia"]
        assert plan.updates == []

    def test_support_act_added_to_title_updates_in_place(self):
        """The Pinhook's most common case, and the one hash-only matching got wrong."""
        stored = row_for(scraped("Acopia", external_id="188044"), id=1)
        se = scraped("Acopia / Lilly Flower", external_id="188044")

        plan = plan_upsert([stored], [se], TODAY)

        assert plan.inserts == [], "a retitled event is the same event"
        assert [r.id for _, r in plan.updates] == [1]
        assert plan.expired == []

    def test_full_rename_updates_in_place(self):
        """Stanczyks: 'Duke Music Collective' became 'Private Event' under one ID."""
        stored = row_for(scraped("Duke Music Collective", external_id="192587"), id=1)
        se = scraped("Private Event", external_id="192587")

        plan = plan_upsert([stored], [se], TODAY)

        assert [r.id for _, r in plan.updates] == [1]
        assert plan.inserts == []

    def test_matches_on_hash_when_no_external_id(self):
        """Scrapers like webflow_cms and mec expose no venue-side ID."""
        se = scraped("Electro Lust w/ Domocile")
        stored = Row(id=1, date=se.date, hash=se.hash, updated_at=datetime(2026, 8, 27))

        plan = plan_upsert([stored], [se], TODAY)

        assert [r.id for _, r in plan.updates] == [1]

    def test_same_external_id_different_date_does_not_match(self):
        """Merging a recurring series onto one row would delete every other night."""
        stored = row_for(scraped("Open Mic", TODAY, external_id="900"), id=1)
        se = scraped("Open Mic", TODAY + timedelta(days=7), external_id="900")

        plan = plan_upsert([stored], [se], TODAY)

        assert [s.date for s in plan.inserts] == [TODAY + timedelta(days=7)]
        assert plan.updates == []

    def test_different_external_ids_stay_separate(self):
        """Rubies lists two shows one night under two IDs; they are not duplicates."""
        first = row_for(scraped("Adulting - Shallow Cuts", external_id="161232"), id=1)
        second = row_for(scraped("Shallow Cuts", external_id="160887"), id=2)
        events = [scraped("Adulting - Shallow Cuts", external_id="161232"),
                  scraped("Shallow Cuts", external_id="160887")]

        plan = plan_upsert([first, second], events, TODAY)

        assert len(plan.updates) == 2
        assert plan.superseded == []
        assert plan.expired == []

    def test_one_row_is_not_claimed_by_two_scraped_events(self):
        stored = row_for(scraped("Acopia", external_id="188044"), id=1)
        events = [scraped("Acopia", external_id="188044"),
                  scraped("Acopia", external_id="999")]

        plan = plan_upsert([stored], events, TODAY)

        assert len(plan.updates) == 1
        assert len(plan.inserts) == 1


# --- plan_upsert: merging existing duplicates ---


class TestMerging:
    def test_existing_duplicate_pair_is_merged(self):
        """Self-healing: the pairs already in the database collapse on the next scrape."""
        old = row_for(scraped("Acopia", external_id="188044"), id=1,
                      seen=datetime(2026, 8, 18))
        new = row_for(scraped("Acopia / Lilly Flower", external_id="188044"), id=2,
                      seen=datetime(2026, 8, 27))
        se = scraped("Acopia / Lilly Flower", external_id="188044")

        plan = plan_upsert([old, new], [se], TODAY)

        assert [r.id for _, r in plan.updates] == [2], "keep the row the source still matches"
        assert [r.id for r in plan.superseded] == [1]
        assert plan.expired == [], "a merged row is not also an orphan"

    def test_survivor_is_chosen_by_title_not_recency(self):
        """A stale row touched more recently must not win over the one that matches."""
        matching = row_for(scraped("Acopia / Lilly Flower", external_id="188044"), id=1,
                           seen=datetime(2026, 8, 18))
        other = row_for(scraped("Acopia", external_id="188044"), id=2,
                        seen=datetime(2026, 8, 27))
        se = scraped("Acopia / Lilly Flower", external_id="188044")

        plan = plan_upsert([matching, other], [se], TODAY)

        assert [r.id for _, r in plan.updates] == [1]
        assert [r.id for r in plan.superseded] == [2]

    def test_survivor_is_stable_regardless_of_row_order(self):
        old = row_for(scraped("Acopia", external_id="188044"), id=1,
                      seen=datetime(2026, 8, 18))
        new = row_for(scraped("Acopia / Lilly Flower", external_id="188044"), id=2,
                      seen=datetime(2026, 8, 27))
        se = scraped("Acopia / Lilly Flower", external_id="188044")

        forward = plan_upsert([old, new], [se], TODAY)
        reverse = plan_upsert([new, old], [se], TODAY)

        assert [r.id for _, r in forward.updates] == [r.id for _, r in reverse.updates]
        assert [r.id for r in forward.superseded] == [r.id for r in reverse.superseded]


# --- plan_upsert: reconcile ---


class TestReconcile:
    def test_dropped_listing_expires(self):
        """Lincoln Theatre's cancelled shows sat on the calendar because nothing removed them."""
        gone = row_for(scraped("Crucial Fiya", external_id="500"), id=1)
        kept = scraped("Anna Graves", TODAY, external_id="187015")

        plan = plan_upsert([gone, row_for(kept, id=2)], [kept], TODAY)

        assert [r.id for r in plan.expired] == [1]
        assert plan.reconcile_skipped is False

    def test_past_events_are_never_expired(self):
        """Venues drop past shows from their listings; the archive must survive that."""
        archived = Row(id=1, date=TODAY - timedelta(days=30), hash="old", updated_at=None)
        se = scraped("Anna Graves", TODAY, external_id="187015")

        plan = plan_upsert([archived, row_for(se, id=2)], [se], TODAY)

        assert plan.expired == []

    def test_events_beyond_the_scraped_horizon_are_never_expired(self):
        """A scraper covering three months says nothing about a show ten months out."""
        far_future = Row(id=1, date=TODAY + timedelta(days=300), hash="far", updated_at=None)
        se = scraped("Anna Graves", TODAY + timedelta(days=7), external_id="187015")

        plan = plan_upsert([far_future, row_for(se, id=2)], [se], TODAY)

        assert plan.expired == []

    def test_small_orphan_counts_always_expire(self):
        """A couple of cancellations is normal churn, even against a short calendar."""
        se = scraped("Anna Graves", TODAY + timedelta(days=10), external_id="187015")
        orphans = [
            Row(id=i, date=TODAY + timedelta(days=i), hash=f"h{i}", updated_at=None)
            for i in range(1, RECONCILE_ALWAYS_ALLOW + 1)
        ]

        plan = plan_upsert(orphans + [row_for(se, id=99)], [se], TODAY)

        assert len(plan.expired) == RECONCILE_ALWAYS_ALLOW
        assert plan.reconcile_skipped is False

    def test_a_half_broken_scraper_deletes_nothing(self):
        """One event returned against a full calendar means the scraper broke."""
        se = scraped("Anna Graves", TODAY + timedelta(days=30), external_id="187015")
        orphans = [
            Row(id=i, date=TODAY + timedelta(days=i), hash=f"h{i}", updated_at=None)
            for i in range(1, 21)
        ]

        plan = plan_upsert(orphans + [row_for(se, id=99)], [se], TODAY)

        assert plan.expired == []
        assert plan.reconcile_skipped is True

    def test_an_empty_run_changes_nothing(self):
        """A scraper returning nothing must never be read as 'the venue has no shows'."""
        stored = [Row(id=i, date=TODAY + timedelta(days=i), hash=f"h{i}") for i in range(1, 6)]

        plan = plan_upsert(stored, [], TODAY)

        assert plan.expired == []
        assert plan.superseded == []
        assert plan.inserts == []
        assert plan.updates == []


# --- Manually created events survive reconcile ---------------------------------------
#
# plan_upsert deletes any row inside the scraped window that the scrape did not return.
# A hand-added event never is returned, so without a guard every manual event at a
# scraped venue would be deleted on the next run.
#
# The guard reads events.is_manually_created rather than filtering on `source`, and the
# distinction is the point of test_survives_after_a_scrape_has_matched_it below: a
# source-based filter passes the obvious test and then fails silently in production the
# first time a venue lists a show that had already been added by hand, because
# _apply_scraped writes `row.source = se.source` on a match.


def manual_row(id: int, on: date = TODAY, name: str = "Hand-Added Benefit Show") -> Row:
    """A row an admin created. Its hash intentionally matches nothing a scraper produces."""
    return Row(
        id=id,
        date=on,
        hash=f"manual-{id}",
        external_id=None,
        updated_at=datetime(2026, 8, 30),
        is_manually_created=True,
    )


class TestManualEventsSurviveReconcile:
    def test_not_orphaned_when_the_scrape_does_not_mention_it(self):
        """The core case: a scrape of the same venue returns other shows, not this one."""
        manual = manual_row(1)
        other = scraped("Some Touring Band", on=TODAY + timedelta(days=2))

        plan = plan_upsert([manual, row_for(other, id=2)], [other], TODAY)

        assert manual not in plan.expired
        assert manual not in plan.superseded
        assert plan.expired == []

    def test_a_scraped_row_beside_it_is_still_reconciled(self):
        """The guard must protect manual rows without disabling reconcile generally."""
        manual = manual_row(1)
        dropped = row_for(scraped("Cancelled Show"), id=2)
        still_listed = scraped("Some Touring Band", on=TODAY + timedelta(days=2))

        plan = plan_upsert(
            [manual, dropped, row_for(still_listed, id=3)], [still_listed], TODAY
        )

        assert dropped in plan.expired, "an ordinary dropped listing should still expire"
        assert manual not in plan.expired

    def test_survives_after_a_scrape_has_matched_it(self):
        """The case a `source != "manual"` filter would fail.

        Sequence: a venue starts listing a show an admin had already added by hand, so the
        scrape matches the manual row and _apply_scraped copies the scraper's `source`
        onto it. The venue then drops the listing again. A source-based guard would have
        been stripped by the match and would delete the row here; the flag survives,
        because nothing in the scraper path writes it.
        """
        listed = scraped("Hand-Added Benefit Show")
        manual = manual_row(1)
        manual.hash = listed.hash  # the match that overwrites `source` in production

        # Run 1: the venue lists it. The manual row is matched, not deleted.
        first = plan_upsert([manual], [listed], TODAY)
        assert first.expired == []
        assert [row for _, row in first.updates] == [manual]

        # Run 2: the venue drops it again. The flag is untouched by run 1, so it holds.
        other = scraped("Unrelated Show", on=TODAY + timedelta(days=3))
        second = plan_upsert([manual, row_for(other, id=2)], [other], TODAY)
        assert second.expired == [], "manual row deleted after having been matched once"

    def test_preferred_over_a_scraped_duplicate_of_the_same_show(self):
        """When one scraped event matches both a manual row and a scraped row, the manual
        row must be the survivor — the loser is superseded, and superseded rows are
        deleted."""
        listed = scraped("Hand-Added Benefit Show")
        manual = manual_row(1)
        manual.external_id = "abc"
        listed_with_id = scraped("Hand-Added Benefit Show", external_id="abc")
        manual.hash = "manual-1"  # title differs, so only external_id matches
        scraped_dupe = Row(
            id=2,
            date=listed.date,
            hash=listed_with_id.hash,  # exact title match, would otherwise win the sort
            external_id="abc",
            updated_at=datetime(2026, 8, 31),  # and it is fresher
        )

        plan = plan_upsert([manual, scraped_dupe], [listed_with_id], TODAY)

        kept = [row for _, row in plan.updates]
        assert kept == [manual], "the hand-added row should be kept, not the scraped one"
        assert plan.superseded == [scraped_dupe]

    def test_manual_row_outside_the_window_is_untouched_as_before(self):
        """Sanity: the guard changes nothing about rows reconcile never considered."""
        past = manual_row(1, on=TODAY - timedelta(days=30))
        listed = scraped("Some Touring Band", on=TODAY + timedelta(days=2))

        plan = plan_upsert([past, row_for(listed, id=2)], [listed], TODAY)

        assert plan.expired == []


def duplicate_row(
    id: int, of: int, on: date = TODAY, name: str = "[LATE] Sub Rosa"
) -> Row:
    """A row an admin folded into row `of` as a duplicate (issue #63)."""
    return Row(
        id=id,
        date=on,
        hash=f"dupe-{id}",
        external_id=None,
        updated_at=datetime(2026, 8, 30),
        duplicate_of_id=of,
    )


class TestFlaggedDuplicatesSurviveReconcile:
    """A row an admin folded into another is hidden, not gone — that is the whole point of
    `duplicate_of_id` being a pointer rather than a delete.

    The failure mode is quiet, which is why these are worth having: the row is already
    hidden from the calendar, so a reconcile that deletes it looks like nothing at all
    until someone tries to unmark it and finds the row and the mapping both gone.
    """

    def test_not_orphaned_when_the_scrape_stops_returning_it(self):
        """The core case. A duplicate listing usually *is* dropped by the venue eventually
        — that is often why it was a duplicate — so this is the common path, not the edge.
        """
        dupe = duplicate_row(2, of=1)
        survivor = scraped("Sub Rosa")

        plan = plan_upsert([row_for(survivor, id=1), dupe], [survivor], TODAY)

        assert dupe not in plan.expired
        assert dupe not in plan.superseded
        assert plan.expired == []

    def test_an_ordinary_dropped_listing_beside_it_still_expires(self):
        """The guard must not switch reconcile off in general."""
        dupe = duplicate_row(2, of=1)
        dropped = row_for(scraped("Cancelled Show"), id=3)
        survivor = scraped("Sub Rosa")

        plan = plan_upsert(
            [row_for(survivor, id=1), dupe, dropped], [survivor], TODAY
        )

        assert dropped in plan.expired
        assert dupe not in plan.expired

    def test_never_chosen_as_the_survivor(self):
        """A flagged duplicate has already been judged not to be the canonical row, so it
        must lose the candidate sort even when every other key favours it — here it is both
        an exact title match and the fresher row."""
        listed = scraped("Sub Rosa", external_id="abc")
        canonical = Row(
            id=1,
            date=listed.date,
            hash="stale-title",           # title does NOT match what the source says now
            external_id="abc",
            updated_at=datetime(2026, 8, 1),  # and it is staler
        )
        dupe = Row(
            id=2,
            date=listed.date,
            hash=listed.hash,            # exact title match, would otherwise win
            external_id="abc",
            updated_at=datetime(2026, 8, 31),  # and fresher
            duplicate_of_id=1,
        )

        plan = plan_upsert([canonical, dupe], [listed], TODAY)

        kept = [row for _, row in plan.updates]
        assert kept == [canonical], "a flagged duplicate was chosen as the canonical row"

    def test_losing_the_sort_does_not_delete_it(self):
        """The second deletion path. Losing the sort means "not canonical", not "delete me"
        — but the loser is `superseded`, and superseded rows are deleted in the same pass.
        Reached whenever one scraped event matches both a survivor and a duplicate folded
        into it, which external_id makes routine."""
        listed = scraped("Sub Rosa", external_id="abc")
        canonical = Row(
            id=1,
            date=listed.date,
            hash=listed.hash,
            external_id="abc",
            updated_at=datetime(2026, 8, 31),
        )
        dupe = Row(
            id=2,
            date=listed.date,
            hash="dupe-2",
            external_id="abc",
            updated_at=datetime(2026, 8, 30),
            duplicate_of_id=1,
        )

        plan = plan_upsert([canonical, dupe], [listed], TODAY)

        assert [row for _, row in plan.updates] == [canonical]
        assert dupe not in plan.superseded, "the audit trail was deleted as superseded"
        assert dupe not in plan.expired, "and it must not fall through to the orphan pass"

    def test_a_row_that_is_both_hand_added_and_flagged_is_not_canonical(self):
        """The combination the manual key alone gets backwards.

        An admin adds a show by hand, later decides it duplicates another row and folds it
        in. Ranking on `is_manually_created` first would make that row win the sort and
        become the canonical listing again — undoing the admin's own second decision. The
        duplicate key has to outrank the manual key for this to come out right.
        """
        listed = scraped("Sub Rosa", external_id="abc")
        canonical = Row(
            id=1,
            date=listed.date,
            hash=listed.hash,
            external_id="abc",
            updated_at=datetime(2026, 8, 1),
        )
        both = Row(
            id=2,
            date=listed.date,
            hash="manual-2",
            external_id="abc",
            updated_at=datetime(2026, 8, 31),
            is_manually_created=True,
            duplicate_of_id=1,
        )

        plan = plan_upsert([canonical, both], [listed], TODAY)

        assert [row for _, row in plan.updates] == [canonical]
        assert both not in plan.superseded, "a hand-added row was deleted"
        assert both not in plan.expired

    def test_two_hand_added_rows_matching_one_event_both_survive(self):
        """Not about duplicates, but the same hole: before the superseded guard, two manual
        rows matching one scraped event meant one of them was deleted by the scraper."""
        listed = scraped("Hand-Added Benefit Show", external_id="abc")
        first = manual_row(1)
        first.external_id = "abc"
        second = manual_row(2)
        second.external_id = "abc"
        second.updated_at = datetime(2026, 8, 31)

        plan = plan_upsert([first, second], [listed], TODAY)

        assert len(plan.updates) == 1, "exactly one row should take the scraped detail"
        assert plan.superseded == [], "the other hand-added row was deleted"
        assert plan.expired == []

    def test_a_duplicate_the_scrape_still_lists_is_updated_in_place(self):
        """A venue that keeps listing the duplicate should keep the row current — it stays
        hidden either way, and letting it go stale would make unmarking it later show
        outdated detail."""
        dupe_listing = scraped("[LATE] Sub Rosa")
        dupe = duplicate_row(2, of=1)
        dupe.hash = dupe_listing.hash

        plan = plan_upsert([dupe], [dupe_listing], TODAY)

        assert [row for _, row in plan.updates] == [dupe]
        assert plan.inserts == [], "a new row would orphan the admin's mapping"
        assert plan.expired == []
