"""
Tests for tools/fix_rhp_years.py — which rows count as misfiled, and what happens to them.

The tool rewrites dates and deletes rows in the production database, so its rules are
pinned here. Fixtures follow rows observed in production on 2026-10-02: "Crank It Loud
Presents Emo Night Karaoke" stored on 2026-01-08 for a show listed as "Fri, Jan 08", which
is 8 January 2027.

Only the pure functions are exercised. Nothing here connects to a database.
"""

import importlib.util
import pathlib
import sys
from datetime import date, datetime

from app.scrapers.base import ScrapedEvent


def _load_tool():
    tools = pathlib.Path(__file__).resolve().parent.parent.parent / "tools"
    sys.path.insert(0, str(tools))  # the tool imports its sibling dedupe_past_events
    spec = importlib.util.spec_from_file_location("fix_rhp_years", tools / "fix_rhp_years.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = _load_tool()

SLUG = "cats-cradle"
EMO = "Crank It Loud Presents Emo Night Karaoke"


def row(id, name=EMO, day="2026-01-08", created="2026-09-15", **kw):
    d = date.fromisoformat(day)
    base = {
        "id": id,
        "venue_id": 1,
        "slug": SLUG,
        "name": name,
        "date": d,
        "hash": tool.title_hash(SLUG, d, name),
        "created_at": datetime.fromisoformat(created),
        "is_manual_override": False,
        "is_manually_created": False,
        "duplicate_of_id": None,
    }
    base.update(kw)
    return base


def test_the_hash_matches_the_scrapers():
    """If these drift, a moved row stops matching the next scrape and is inserted twice."""
    for name in (EMO, "Piano Box Series", "Dinosaur Jr. – Spring Tour 2027"):
        se = ScrapedEvent(name=name, venue_slug=SLUG, date=date(2027, 1, 8), source="rhp_events")
        assert tool.title_hash(SLUG, se.date, se.name) == se.hash


class TestWhichRowsAreMisfiled:
    def test_created_months_after_its_date(self):
        assert tool.is_misfiled(row(1))

    def test_a_genuine_past_show_was_created_before_it_happened(self):
        assert not tool.is_misfiled(row(1, day="2026-03-10", created="2026-01-20"))

    def test_a_show_still_listed_the_morning_after_is_not_misfiled(self):
        assert not tool.is_misfiled(row(1, day="2026-03-10", created="2026-03-11"))


class TestPlan:
    def test_moves_a_row_the_corrected_scrape_has_not_reached(self):
        [(action, _, target, _)] = tool.plan([row(1)], {})
        assert (action, target) == ("move", date(2027, 1, 8))

    def test_deletes_a_row_the_corrected_scrape_already_replaced(self):
        twin = tool.title_hash(SLUG, date(2027, 1, 8), EMO)
        [(action, _, _, why)] = tool.plan([row(1)], {twin: 99})
        assert action == "delete"
        assert "id=99" in why

    def test_genuine_past_rows_are_left_alone(self):
        assert tool.plan([row(1, day="2026-03-10", created="2026-01-20")], {}) == []

    def test_hand_added_rows_are_never_touched(self):
        assert tool.plan([row(1, is_manually_created=True)], {}) == []

    def test_an_admin_flagged_row_is_not_deleted(self):
        twin = tool.title_hash(SLUG, date(2027, 1, 8), EMO)
        [(action, *_)] = tool.plan([row(1, is_manual_override=True)], {twin: 99})
        assert action == "skip"

    def test_a_retitled_row_is_skipped(self):
        """Its hash is not what its name scrapes to, so a rewritten hash would not match."""
        [(action, *_)] = tool.plan([row(1, hash="0" * 64)], {})
        assert action == "skip"

    def test_an_annual_show_misfiled_twice_moves_both(self):
        """2025's misfiled copy targets exactly the hash 2026's copy holds now. Handled
        latest-first, the 2026 copy moves out of the way and the 2025 one follows it,
        rather than being deleted as though its corrected row already existed."""
        older = row(1, day="2025-01-08", created="2025-09-15")
        newer = row(2, day="2026-01-08", created="2026-09-15")
        existing = {older["hash"]: 1, newer["hash"]: 2}
        plan = tool.plan([older, newer], existing)
        assert [(a, r["id"], t) for a, r, t, _ in plan] == [
            ("move", 2, date(2027, 1, 8)),
            ("move", 1, date(2026, 1, 8)),
        ]
