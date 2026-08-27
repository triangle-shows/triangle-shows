"""
Tests for tools/dedupe_past_events.py — the matching and survivor rules.

This tool deletes rows from the production database, and its matching rule is fuzzy by
necessity, so the rule is worth pinning down. Every fixture below is a real pair or triple
observed in production between 2026-08-01 and 2026-08-12, plus the near-miss cases the
containment rule has to *not* match.

Only the pure functions are exercised — clustering and ranking. Nothing here connects to a
database; the SQL path stays behind --apply and a dry run.
"""

import importlib.util
import pathlib
from datetime import date, datetime

import pytest


def _load_tool():
    path = pathlib.Path(__file__).resolve().parent.parent.parent / "tools" / "dedupe_past_events.py"
    spec = importlib.util.spec_from_file_location("dedupe_past_events", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


def row(id, name, venue_id=1, day="2026-08-03", **kw):
    base = {
        "id": id,
        "venue_id": venue_id,
        "name": name,
        "date": date.fromisoformat(day),
        "status": kw.pop("status", "onsale"),
        "is_live_music": kw.pop("is_live_music", True),
        "is_manual_override": kw.pop("is_manual_override", False),
        "updated_at": kw.pop("updated_at", None),
    }
    for field in tool.MERGEABLE_FIELDS:
        base[field] = None
    base.update(kw)
    return base


# --- normalize ---

class TestNormalize:
    def test_punctuation_and_case_are_dropped(self):
        assert tool.normalize("The Briefs w/ Paint Fumes") == "thebriefswpaintfumes"

    def test_a_bracketed_status_marker_is_stripped(self):
        """The case that rules out prefix matching: the marker sits at the front."""
        assert tool.normalize("[CANCELLED] The Philharmonik") == "thephilharmonik"

    def test_a_parenthesised_aside_is_kept(self):
        """Not stripped — only known status markers are. Deliberately conservative after
        blanket stripping merged an early show with a late one."""
        assert tool.normalize("Verse Versus Launch Party (subscribe!)") == (
            "verseversuslaunchpartysubscribe"
        )

    def test_an_aside_still_merges_through_containment(self):
        """The real pour-house pair. Keeping the aside costs nothing here, because the
        shorter name is still contained in the longer one."""
        rows = [
            row(3135, "Verse Versus Launch Party (subscribe to their newsletter)"),
            row(3287, "Verse Versus Launch Party"),
        ]
        groups, _ = tool.find_groups(rows)
        assert len(groups) == 1 and len(groups[0]) == 2

    def test_none_and_empty_are_safe(self):
        assert tool.normalize(None) == ""
        assert tool.normalize("") == ""


# --- clustering, against real production duplicates ---

class TestRealDuplicates:
    def test_a_support_act_added_to_the_title(self):
        rows = [row(2167, "Slow Joy"), row(2946, "Slow Joy with Bike Routes and Growing Pains")]
        groups, _ = tool.find_groups(rows)
        assert len(groups) == 1 and len(groups[0]) == 2

    def test_a_cancelled_marker_added_to_the_title(self):
        rows = [
            row(2753, "The Philharmonik & Wyatt Waddell"),
            row(3530, "[CANCELLED] The Philharmonik & Wyatt Waddell"),
        ]
        groups, _ = tool.find_groups(rows)
        assert len(groups) == 1 and len(groups[0]) == 2

    def test_a_subtitle_added_to_the_title(self):
        rows = [row(708, "Derek Hough"), row(159, "Derek Hough - Symphony of Dance: Encore")]
        groups, _ = tool.find_groups(rows)
        assert len(groups) == 1 and len(groups[0]) == 2

    def test_a_three_way_group_collapses_into_one(self):
        """Real pour-house case. Two overlapping pairs would double-count and pick two
        survivors, so the clustering has to be transitive."""
        rows = [
            row(2924, "The Briefs"),
            row(2931, "The Briefs w/ The Sleevens, Paint Fumes"),
            row(2928, "The Briefs w/ Paint Fumes"),
        ]
        groups, _ = tool.find_groups(rows)
        assert len(groups) == 1
        assert len(groups[0]) == 3


class TestEarlyAndLateShowsStaySeparate:
    """Real pair at venue 10 on 2026-07-07, and the reason the rule changed.

    The first version stripped every bracketed token, which turned '[LATE] Sub Rosa' and
    'Sub Rosa' into the same string — two performances on one night, one of which would
    have been deleted. Found by dry-running against production, not by reasoning.
    """

    def test_a_late_show_is_not_merged_with_the_early_one(self):
        rows = [row(3072, "Sub Rosa", venue_id=10, day="2026-07-07"),
                row(3073, "[LATE] Sub Rosa", venue_id=10, day="2026-07-07")]
        groups, _ = tool.find_groups(rows)
        assert groups == []

    def test_an_early_marker_also_separates(self):
        rows = [row(2763, "away with words", venue_id=10, day="2026-07-07"),
                row(3103, "[Early] away with words", venue_id=10, day="2026-07-07")]
        groups, _ = tool.find_groups(rows)
        assert groups == []

    def test_two_late_shows_still_merge(self):
        """Compared as sets, so a marker shared by both rows is not a distinction — and a
        band with 'Late' in its name is not accidentally protected from deduping."""
        rows = [row(1, "[LATE] Sub Rosa"), row(2, "[LATE] Sub Rosa w/ Guest")]
        groups, _ = tool.find_groups(rows)
        assert len(groups) == 1

    def test_a_band_name_containing_late_is_unaffected(self):
        rows = [row(1, "Late Night Drive Home"), row(2, "Late Night Drive Home w/ Support")]
        groups, _ = tool.find_groups(rows)
        assert len(groups) == 1

    def test_set_numbers_separate(self):
        rows = [row(1, "Branford Marsalis (1st Set)"), row(2, "Branford Marsalis (2nd Set)")]
        groups, _ = tool.find_groups(rows)
        assert groups == []


class TestStatusMarkersAreStillStripped:
    def test_only_known_status_markers_are_removed(self):
        assert tool.normalize("[CANCELLED] The Philharmonik") == "thephilharmonik"
        assert tool.normalize("(SOLD OUT) (18+) Bedroom Division") == "bedroomdivision"

    def test_an_unknown_bracketed_token_is_kept(self):
        """The conservative direction: an unrecognised marker stays part of the name, so
        two rows differing only by it are left alone rather than merged."""
        assert "tour" in tool.normalize("Some Band [Tour Debut]")


class TestCancellationWinsTheSurvivorChoice:
    """A cancelled row is the newer information about the show. If the on-sale row
    survived, the calendar would keep advertising a show that is not happening."""

    def test_a_cancelled_title_beats_an_on_sale_one(self):
        cancelled = row(3530, "[CANCELLED] The Philharmonik & Wyatt Waddell")
        on_sale = row(2753, "The Philharmonik & Wyatt Waddell", image_url="x", ticket_url="y")
        assert sorted([cancelled, on_sale], key=tool.rank, reverse=True)[0]["id"] == 3530

    def test_a_cancelled_status_column_also_counts(self):
        cancelled = row(1, "Short", status="cancelled")
        on_sale = row(2, "Much Longer Title Here", image_url="x")
        assert sorted([cancelled, on_sale], key=tool.rank, reverse=True)[0]["id"] == 1

    def test_an_admin_override_still_outranks_a_cancellation(self):
        hand_set = row(1, "Show", is_manual_override=True)
        cancelled = row(2, "[CANCELLED] Show")
        assert sorted([hand_set, cancelled], key=tool.rank, reverse=True)[0]["id"] == 1

    def test_is_cancelled_recognises_both_spellings(self):
        assert tool.is_cancelled(row(1, "[CANCELED] X")) is True
        assert tool.is_cancelled(row(1, "[CANCELLED] X")) is True
        assert tool.is_cancelled(row(1, "[POSTPONED] X")) is True
        assert tool.is_cancelled(row(1, "Ordinary Show")) is False


# --- what must NOT be merged ---

class TestNonDuplicates:
    def test_different_venues_never_group(self):
        """Two venues booking the same artist on one night is a moved or miscopied show,
        not a duplicate — the mistake #51 removed from the display-time guard."""
        rows = [row(1, "Slow Joy", venue_id=1), row(2, "Slow Joy", venue_id=2)]
        groups, _ = tool.find_groups(rows)
        assert groups == []

    def test_different_dates_never_group(self):
        rows = [row(1, "Slow Joy", day="2026-08-03"), row(2, "Slow Joy", day="2026-08-04")]
        groups, _ = tool.find_groups(rows)
        assert groups == []

    def test_unrelated_names_at_one_venue_and_date_do_not_group(self):
        rows = [
            row(3287, "Verse Versus Launch Party"),
            row(3338, "(Record Shop) Ariana Grande Listening Party"),
        ]
        groups, _ = tool.find_groups(rows)
        assert groups == []

    def test_a_short_generic_name_is_refused_and_reported(self):
        """'dj' is contained in half a venue's calendar. Below the length floor the pair is
        skipped and surfaced, rather than silently merged or silently dropped."""
        rows = [row(1, "DJ"), row(2, "DJ Shadow and Friends Live")]
        groups, skipped = tool.find_groups(rows)
        assert groups == []
        assert len(skipped) == 1

    def test_the_length_floor_is_adjustable(self):
        rows = [row(1, "DJ"), row(2, "DJ Shadow and Friends Live")]
        groups, _ = tool.find_groups(rows, min_length=2)
        assert len(groups) == 1


# --- survivor choice ---

class TestRank:
    def _survivor(self, rows):
        return sorted(rows, key=tool.rank, reverse=True)[0]

    def test_an_admin_override_wins(self):
        hand_set = row(1, "Slow Joy", is_manual_override=True, is_live_music=False)
        auto = row(2, "Slow Joy with Bike Routes", image_url="x", ticket_url="y")
        assert self._survivor([hand_set, auto])["id"] == 1

    def test_the_visible_copy_beats_the_hidden_one(self):
        hidden = row(1, "Slow Joy with Bike Routes", is_live_music=False, image_url="x")
        visible = row(2, "Slow Joy", is_live_music=True)
        assert self._survivor([hidden, visible])["id"] == 2

    def test_richer_metadata_wins_between_equal_verdicts(self):
        sparse = row(1, "Slow Joy")
        rich = row(2, "Slow Joy", image_url="x", ticket_url="y", price_min=10.0)
        assert self._survivor([sparse, rich])["id"] == 2

    def test_the_fuller_title_breaks_a_metadata_tie(self):
        """'The Briefs w/ Paint Fumes' is the more useful listing than 'The Briefs'."""
        short = row(1, "The Briefs")
        long = row(2, "The Briefs w/ The Sleevens, Paint Fumes")
        assert self._survivor([short, long])["id"] == 2

    def test_recency_breaks_a_tie_on_title_length(self):
        older = row(1, "Slow Joy", updated_at=datetime(2026, 1, 1))
        newer = row(2, "Slow Joy", updated_at=datetime(2026, 6, 1))
        assert self._survivor([older, newer])["id"] == 2

    def test_a_missing_updated_at_does_not_raise(self):
        assert self._survivor([row(1, "Slow Joy"), row(2, "Slow Joy")])["id"] == 2

    def test_the_order_rows_arrive_in_does_not_matter(self):
        a = row(1, "The Briefs")
        b = row(2, "The Briefs w/ Paint Fumes")
        assert self._survivor([a, b])["id"] == self._survivor([b, a])["id"]


# --- URL handling ---

class TestUrlRewrite:
    def test_the_asyncpg_driver_is_stripped(self):
        assert tool.to_psycopg_url(
            "postgresql+asyncpg://u:p@host/db"
        ) == "postgresql://u:p@host/db"

    def test_a_plain_url_is_unchanged(self):
        assert tool.to_psycopg_url("postgresql://u:p@host/db") == "postgresql://u:p@host/db"


# --- fields that must never be rewritten ---

class TestMergeableFields:
    @pytest.mark.parametrize("field", ["name", "hash", "date", "venue_id", "id"])
    def test_identity_fields_are_not_mergeable(self, field):
        """The hash is derived from the name, so rewriting either would leave the surviving
        row unmatchable by the scraper on a later run."""
        assert field not in tool.MERGEABLE_FIELDS

    def test_classification_fields_are_not_mergeable(self):
        """Copying a discarded row's verdict onto the survivor would overwrite the
        classifier's own answer for the row being kept."""
        for field in ("is_live_music", "is_manual_override", "classification_reason", "approved_at"):
            assert field not in tool.MERGEABLE_FIELDS
