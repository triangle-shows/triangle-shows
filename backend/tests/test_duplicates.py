"""Tests for admin duplicate flagging (issue #63).

The design being protected here is the division of labour in app/duplicates.py: candidate
clusters are chosen by *fact* (same venue, same date, two or more unfolded rows), title
similarity only *orders* the queue, and a human makes every verdict. That split exists
because two automated title rules had already been wrong in opposite directions — one
merged genuine early/late double bills, the next refused to merge "[LATE] Sub Rosa" with
"Sub Rosa", which are one show.

So the assertions come in two flavours, and the distinction matters when one fails:

  * **Ordering** assertions may be tuned freely. Getting an order wrong costs a scroll.
  * **Grouping and validation** assertions are load-bearing. Getting those wrong hides a
    show visitors should see, or leaves a row hidden behind a row that is itself hidden.
"""

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Optional

import pytest

from app.duplicates import (
    FoldError,
    build_clusters,
    cluster_score,
    normalize_for_similarity,
    title_similarity,
    validate_fold,
)

TODAY = date(2026, 9, 10)


@dataclass
class Row:
    """Stand-in for an Event row. build_clusters only reads these attributes."""

    id: int
    name: str
    venue_id: int = 1
    date: date = TODAY
    show_time: Optional[time] = None
    doors_time: Optional[time] = None
    duplicate_of_id: Optional[int] = None
    approved_at: Optional[datetime] = None


# --- Similarity: ordering only ---


class TestNormalizeForSimilarity:
    """Deliberately aggressive. That aggression was wrong when a rule like this *decided*
    whether two rows were one show; it is fine when it only picks a sort order."""

    @pytest.mark.parametrize("title,expected", [
        ("[LATE] Sub Rosa", "sub rosa"),
        ("Sub Rosa (Early Show)", "sub rosa"),
        ("DPAC: Hamilton [Box Seats]", "dpac hamilton"),
        ("An Evening with X - LATE SHOW", "an evening with x"),
        ("  Spaced   Out  ", "spaced out"),
    ])
    def test_strips_session_noise_and_punctuation(self, title, expected):
        assert normalize_for_similarity(title) == expected

    def test_a_title_of_only_noise_normalizes_to_nothing(self):
        assert normalize_for_similarity("[LATE]") == ""

    def test_none_and_empty_are_safe(self):
        assert normalize_for_similarity(None) == ""
        assert normalize_for_similarity("") == ""


class TestTitleSimilarity:
    def test_the_two_cases_from_issue_63_score_top(self):
        """Both real examples the old rules got wrong, in opposite directions. These are
        the pairs that must float to the top of the queue."""
        assert title_similarity("Sub Rosa", "[LATE] Sub Rosa") == 1.0
        assert title_similarity("DPAC: Hamilton", "DPAC: Hamilton [Box Seats]") == 1.0

    def test_unrelated_titles_score_low(self):
        assert title_similarity("Sub Rosa", "Completely Different Band") < 0.4

    def test_two_titles_that_normalize_to_nothing_do_not_match(self):
        """Without the empty guard both normalize to "" and score a perfect 1.0, dragging
        unrelated rows to the top of the queue on the strength of having no title left."""
        assert title_similarity("[LATE]", "[EARLY]") == 0.0

    def test_one_empty_side_does_not_match_everything(self):
        assert title_similarity("[LATE]", "Sub Rosa") == 0.0


class TestClusterScore:
    def test_uses_the_strongest_pair_not_the_average(self):
        """A cluster of three where two are obviously one show deserves attention even if
        the third is unrelated; averaging would bury it below weaker but tidier clusters."""
        titles = ["Sub Rosa", "[LATE] Sub Rosa", "Some Unrelated DJ Set"]
        assert cluster_score(titles) == 1.0

    def test_a_single_title_has_no_pair(self):
        assert cluster_score(["Sub Rosa"]) == 0.0

    def test_empty(self):
        assert cluster_score([]) == 0.0


# --- Grouping: load-bearing ---


class TestBuildClusters:
    def test_groups_by_venue_and_date(self):
        rows = [
            Row(1, "Sub Rosa"),
            Row(2, "[LATE] Sub Rosa"),
            Row(3, "Other Show", venue_id=2),
            Row(4, "Other Show Late", venue_id=2),
        ]
        clusters = build_clusters(rows)
        assert len(clusters) == 2
        assert {c["venue_id"] for c in clusters} == {1, 2}

    def test_a_different_date_is_a_different_cluster(self):
        rows = [
            Row(1, "Sub Rosa"),
            Row(2, "Sub Rosa", date=TODAY.replace(day=11)),
        ]
        assert build_clusters(rows) == []

    def test_a_resolved_cluster_disappears_with_nothing_recording_it(self):
        """The self-resolving property. Folding row 2 into row 1 leaves one unfolded row,
        so the cluster stops being a candidate — no "reviewed" flag needed anywhere.
        """
        rows = [Row(1, "Sub Rosa"), Row(2, "[LATE] Sub Rosa", duplicate_of_id=1)]
        assert build_clusters(rows) == []

    def test_folded_rows_are_still_reported_inside_a_live_cluster(self):
        """A cluster that still has two unfolded rows must show what was already folded
        in, or the admin cannot see or undo the earlier decision."""
        rows = [
            Row(1, "Sub Rosa"),
            Row(2, "Sub Rosa Support"),
            Row(3, "[LATE] Sub Rosa", duplicate_of_id=1),
        ]
        clusters = build_clusters(rows)
        assert len(clusters) == 1
        assert {r.id for r in clusters[0]["members"]} == {1, 2, 3}

    def test_a_lone_row_is_never_a_cluster(self):
        assert build_clusters([Row(1, "Sub Rosa")]) == []

    def test_score_is_computed_over_unfolded_rows_only(self):
        """A folded row's title must not prop up the score of a cluster whose remaining
        rows are unalike — that would rank an already-handled night above a fresh one."""
        rows = [
            Row(1, "Sub Rosa"),
            Row(2, "Totally Unrelated Comedy"),
            Row(3, "[LATE] Sub Rosa", duplicate_of_id=1),
        ]
        assert build_clusters(rows)[0]["score"] < 0.5

    def test_likely_duplicates_sort_above_unlikely_ones(self):
        rows = [
            Row(1, "Jazz Jam", venue_id=2),
            Row(2, "Standup Showcase", venue_id=2),
            Row(3, "Sub Rosa", venue_id=1),
            Row(4, "[LATE] Sub Rosa", venue_id=1),
        ]
        clusters = build_clusters(rows)
        assert clusters[0]["venue_id"] == 1, "the near-identical pair should lead"

    def test_members_are_ordered_by_time_with_untimed_rows_last(self):
        """Time is what actually distinguishes an early show from a late one, so it is the
        field the admin reads next to two near-identical titles."""
        rows = [
            Row(1, "No Time"),
            Row(2, "Late", show_time=time(22, 30)),
            Row(3, "Early", show_time=time(19, 0)),
        ]
        members = build_clusters(rows)[0]["members"]
        assert [m.id for m in members] == [3, 2, 1]

    def test_doors_time_is_used_when_there_is_no_show_time(self):
        """Found by looking at the rendered queue. The UI's time label falls back to
        doors_time, so sorting on show_time alone put a row reading "doors 19:00" below one
        reading "show 22:00" — the order contradicting the label, in the very column the
        admin uses to tell two near-identical listings apart."""
        rows = [
            Row(1, "Late Set", show_time=time(22, 0)),
            Row(2, "Early Set", doors_time=time(19, 0)),
        ]
        members = build_clusters(rows)[0]["members"]
        assert [m.id for m in members] == [2, 1]

    def test_show_time_still_wins_over_doors_time_on_the_same_row(self):
        """Doors is only a fallback: a row with both sorts by when the music starts."""
        rows = [
            Row(1, "Doors Early Show Late", doors_time=time(18, 0), show_time=time(23, 0)),
            Row(2, "Straightforward", show_time=time(20, 0)),
        ]
        members = build_clusters(rows)[0]["members"]
        assert [m.id for m in members] == [2, 1]

    def test_all_untimed_rows_still_sort_without_raising(self):
        """The sentinel used for a missing time has to compare consistently against itself,
        or sorted() raises on a cluster where no row has a time — the common case."""
        rows = [Row(2, "B"), Row(1, "A"), Row(3, "C")]
        members = build_clusters(rows)[0]["members"]
        assert [m.id for m in members] == [1, 2, 3]


class TestApprovedOnlyAffectsOrdering:
    """`approved_at` orders the queue; it must never remove a cluster from it.

    Hiding on approval would let an approval granted for a *live-music* decision silently
    drop a duplicate nobody had assessed — the queue would go quiet for an unrelated
    reason. Ordering cannot do that, because the cluster stays reachable. See #90.
    """

    def test_an_approved_cluster_is_still_returned(self):
        rows = [
            Row(1, "Sub Rosa", approved_at=datetime(2026, 9, 1)),
            Row(2, "[LATE] Sub Rosa", approved_at=datetime(2026, 9, 1)),
        ]
        clusters = build_clusters(rows)
        assert len(clusters) == 1, "approving rows must not hide the cluster"
        assert clusters[0]["all_approved"] is True

    def test_an_approved_cluster_sorts_below_an_unreviewed_one_of_equal_score(self):
        rows = [
            Row(1, "Sub Rosa", venue_id=1, approved_at=datetime(2026, 9, 1)),
            Row(2, "[LATE] Sub Rosa", venue_id=1, approved_at=datetime(2026, 9, 1)),
            Row(3, "Sub Rosa", venue_id=2),
            Row(4, "[LATE] Sub Rosa", venue_id=2),
        ]
        clusters = build_clusters(rows)
        assert [c["venue_id"] for c in clusters] == [2, 1]

    def test_one_unapproved_row_keeps_the_cluster_unresolved(self):
        rows = [
            Row(1, "Sub Rosa", approved_at=datetime(2026, 9, 1)),
            Row(2, "[LATE] Sub Rosa"),
        ]
        assert build_clusters(rows)[0]["all_approved"] is False


# --- Validation: load-bearing ---


class TestValidateFold:
    def test_a_normal_fold_returns_the_ids(self):
        assert validate_fold(1, [2, 3], already_folded={1: None, 2: None, 3: None}) == [2, 3]

    def test_duplicate_ids_in_the_request_are_collapsed(self):
        assert validate_fold(1, [2, 2, 3], already_folded={1: None}) == [2, 3]

    def test_self_reference_is_refused(self):
        """Also blocked by ck_events_duplicate_of_not_self in migration 0006. Checked here
        too so the admin gets a sentence rather than an IntegrityError."""
        with pytest.raises(FoldError, match="cannot be a duplicate of itself"):
            validate_fold(1, [1, 2], already_folded={1: None})

    def test_an_empty_fold_is_refused(self):
        """A no-op that reported success would read as "it worked" on a click that did
        nothing."""
        with pytest.raises(FoldError, match="at least one"):
            validate_fold(1, [], already_folded={1: None})

    def test_folding_into_a_row_that_is_itself_folded_is_refused(self):
        """The chain case. A -> B -> C leaves A hidden behind a row that is also hidden, so
        unfolding A restores it to invisibility with no way to see why."""
        with pytest.raises(FoldError, match="itself marked as a duplicate"):
            validate_fold(2, [3], already_folded={2: 1, 3: None})

    def test_the_refusal_names_the_row_to_use_instead(self):
        """Chains are refused rather than silently rewritten, because the intended survivor
        is genuinely ambiguous — so the message has to say what to do next."""
        with pytest.raises(FoldError, match=r"fold into event 1 instead"):
            validate_fold(2, [3], already_folded={2: 1, 3: None})


# --- The read paths, and the UI that drives all this ---


class TestEveryPublicReadPathExcludesFoldedRows:
    """Four public paths can return an event, and closing three leaves one open.

    Source-level because these need a database to exercise end to end, and the failure
    being guarded against is an omission — a path nobody remembered to filter — which a
    passing test on the other three would not reveal. `/api/events` is covered properly in
    test_events_api.py, where _list_conditions is reachable without Postgres.
    """

    @staticmethod
    def _source(fn) -> str:
        import inspect

        return inspect.getsource(fn)

    def test_the_fullcalendar_feed_filters_server_side(self):
        """Deliberately unlike is_live_music, which this endpoint does not filter because
        filters.js toggles it in the browser without refetching. There is no client toggle
        for duplicates, so a folded row must never reach the payload at all."""
        from app.api.events import get_fullcalendar_events

        assert "duplicate_of_id" in self._source(get_fullcalendar_events)

    def test_the_single_event_endpoint_filters(self):
        """Otherwise a folded row stays reachable by id and the flag is only advisory."""
        from app.api.events import get_event

        assert "duplicate_of_id" in self._source(get_event)

    def test_the_ical_feed_filters(self):
        """The one that matters most: a subscribed feed writes into someone's own calendar,
        where a duplicate is a second reminder for one show rather than mere clutter."""
        from app.api.feeds import get_ical_feed

        assert "duplicate_of_id" in self._source(get_ical_feed)


class TestAdminUiWiring:
    """admin_ui.py is ~600 lines of JS inside a Python string, so nothing type-checks it
    and a typo in an onclick handler fails silently in the browser — the button simply
    does nothing. These are cheap structural guards over that.
    """

    @pytest.fixture(scope="class")
    def html(self) -> str:
        from app.admin_ui import ADMIN_HTML

        return ADMIN_HTML

    @pytest.fixture(scope="class")
    def script(self, html) -> str:
        import re

        match = re.search(r"<script>(.*?)</script>", html, re.S)
        assert match, "the dashboard has no <script> block"
        return match.group(1)

    def test_every_onclick_handler_is_defined(self, html, script):
        import re

        referenced = set(re.findall(r'onclick="(\w+)\(', html))
        defined = set(re.findall(r"(?:async\s+)?function\s+(\w+)", script))
        # `event.stopPropagation()` is a DOM object, not a function of ours.
        missing = referenced - defined - {"event"}
        assert not missing, f"onclick handlers with no definition: {sorted(missing)}"

    def test_the_duplicate_actions_are_wired(self, script):
        assert "/admin/api/duplicates" in script
        assert "/absorb" in script
        assert "/unfold" in script

    def test_keep_this_one_acts_on_the_survivor(self, script):
        """The direction guard. absorb() takes the id of the row being *kept* first and the
        rows being hidden second, matching the endpoint. Inverting it would hide the row the
        admin clicked — the failure mode the "keep this one" wording exists to prevent."""
        assert "absorb(survivorId, duplicateIds)" in script
        assert "'/admin/api/events/' + survivorId + '/absorb'" in script

    def test_the_error_line_surfaces_the_servers_detail(self, script):
        """The fold endpoints refuse a chain with a sentence naming the row to use instead.
        Throwing a bare 'http 400' would discard exactly the part the admin needs."""
        assert "err.detail" in script
        assert "detail = (await r.json()).detail" in script
