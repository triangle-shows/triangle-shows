"""
Tests for two review items in app.api.events — #4 (dedup ranking) and #5 (list filtering).

#4 is a pure function, so it is tested directly. #5 builds a SQL WHERE clause and would
need a database to exercise end to end; what is asserted here is the parameter contract,
with the gap stated plainly rather than papered over. See the note on TestListFiltering.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from fastapi.testclient import TestClient

import pytest

from app.api.events import _completeness, _list_conditions
from app.main import app


@dataclass
class Row:
    """Stand-in for an ORM Event with only the attributes _completeness reads."""

    id: int
    is_live_music: bool = True
    is_manual_override: bool = False
    image_url: Optional[str] = None
    ticket_url: Optional[str] = None
    price_min: Optional[float] = None
    updated_at: Optional[datetime] = None


def _winner(a: Row, b: Row) -> Row:
    """Whichever row the display-time dedup would keep, order-independently."""
    forward = a if _completeness(a) > _completeness(b) else b
    backward = b if _completeness(b) > _completeness(a) else a
    assert forward is backward, "ranking depends on comparison order — not a total order"
    return forward


# --- #4: dedup must not discard the visible copy ---

class TestDedupRanking:
    def test_a_live_row_beats_a_non_live_row_with_richer_metadata(self):
        """The regression. Ranking on field counts alone let the richer row win, and if
        that row was classified non-live the show vanished from the default calendar."""
        live_but_sparse = Row(id=1, is_live_music=True)
        non_live_but_rich = Row(
            id=2, is_live_music=False, image_url="x", ticket_url="y", price_min=10.0
        )

        assert _winner(live_but_sparse, non_live_but_rich).id == 1

    def test_an_admin_override_beats_an_automatic_live_verdict(self):
        """An admin looked at this row and decided; that outranks anything computed —
        including a computed verdict that happens to be more permissive."""
        hand_set_hidden = Row(id=1, is_live_music=False, is_manual_override=True)
        auto_live = Row(id=2, is_live_music=True, image_url="x", ticket_url="y")

        assert _winner(hand_set_hidden, auto_live).id == 1

    def test_metadata_still_decides_between_two_equal_verdicts(self):
        """The original behavior survives as a tiebreaker rather than being replaced."""
        sparse = Row(id=1, is_live_music=True)
        rich = Row(id=2, is_live_music=True, image_url="x", ticket_url="y", price_min=5.0)

        assert _winner(sparse, rich).id == 2

    def test_recency_breaks_a_tie_on_metadata(self):
        older = Row(id=1, image_url="x", updated_at=datetime(2026, 1, 1))
        newer = Row(id=2, image_url="y", updated_at=datetime(2026, 6, 1))

        assert _winner(older, newer).id == 2

    def test_id_breaks_a_total_tie(self):
        """Without a final total key the winner would depend on row order from the
        database, which is how the earlier version picked a surviving venue at random."""
        assert _winner(Row(id=1), Row(id=2)).id == 2

    def test_a_missing_updated_at_does_not_raise(self):
        """Rows inserted but never re-scraped have updated_at unset."""
        assert _winner(Row(id=1, updated_at=None), Row(id=2, updated_at=None)).id == 2

    def test_is_manual_override_absent_is_treated_as_false(self):
        """_completeness reads it with getattr, so a stand-in or a partially loaded row
        lacking the attribute must rank as not-overridden rather than blow up."""

        class NoOverrideAttr:
            id = 1
            is_live_music = True
            image_url = ticket_url = None
            price_min = None
            updated_at = None

        assert _completeness(NoOverrideAttr())[0] is False


# --- #5: the list endpoint gains the opt-in the other consumers already have ---

class TestListConditions:
    """The clauses themselves, via _list_conditions.

    An earlier version of this file only checked the OpenAPI parameter contract below.
    Mutation-testing showed that was not enough: deleting the line that appends the
    live-music clause left every assertion passing, because a declared-but-unused query
    parameter still appears in the schema. That is exactly the bug worth catching, so the
    clause building was split out of the endpoint to make it reachable without Postgres.
    """

    @staticmethod
    def _sql(conditions) -> str:
        return " ".join(str(c) for c in conditions)

    def test_live_music_is_filtered_by_default(self):
        assert "is_live_music" in self._sql(_list_conditions())

    def test_opting_in_removes_the_filter(self):
        assert "is_live_music" not in self._sql(_list_conditions(include_non_music=True))

    def test_the_filter_is_added_alongside_other_filters(self):
        """Regression shape: an early return or an if/elif could drop it whenever another
        filter was supplied."""
        sql = self._sql(_list_conditions(search="band", genre="rock", status="onsale"))
        assert "is_live_music" in sql

    def test_other_filters_are_unaffected_by_the_opt_in(self):
        """Opting in drops only the live-music clause, not the caller's own filters."""
        opted_in = self._sql(_list_conditions(search="band", include_non_music=True))
        assert "events.name" in opted_in and "events.artist" in opted_in
        assert "is_live_music" not in opted_in

    def test_a_malformed_date_is_ignored_rather_than_raising(self):
        """Pre-existing behavior, preserved through the extraction."""
        conditions = _list_conditions(start="not-a-date", include_non_music=True)
        assert conditions == []


class TestListFiltering:
    """The parameter contract, as it appears to callers."""

    @pytest.fixture
    def schema(self):
        return TestClient(app).get("/openapi.json").json()

    def _params(self, schema, path):
        return {p["name"]: p for p in schema["paths"][path]["get"]["parameters"]}

    def test_the_list_endpoint_offers_the_opt_in(self, schema):
        assert "include_non_music" in self._params(schema, "/api/events")

    def test_it_defaults_to_live_music_only(self, schema):
        """Default off is the point: before this, the list endpoint was the one consumer
        that served the events the feature exists to hide."""
        param = self._params(schema, "/api/events")["include_non_music"]
        assert param["schema"]["default"] is False

    def test_it_matches_the_ical_feed_by_name_and_default(self, schema):
        """Review item #5 is about consistency across consumers, so the two endpoints
        agreeing is the property worth pinning — a rename of one would break this."""
        events = self._params(schema, "/api/events")["include_non_music"]
        feed = self._params(schema, "/feeds/events.ics")["include_non_music"]

        assert events["schema"]["default"] == feed["schema"]["default"] is False

    def test_the_fullcalendar_endpoint_deliberately_has_no_such_parameter(self, schema):
        """Left returning everything on purpose: filters.js toggles visibility in the
        browser without refetching, so filtering server-side would empty the calendar the
        moment someone switched the toggle on. Asserted so the omission reads as a decision
        rather than as an oversight."""
        params = self._params(schema, "/api/events/fullcalendar")
        assert "include_non_music" not in params
