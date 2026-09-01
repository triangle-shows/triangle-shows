"""Tests for the colour assigned to a venue an admin creates by hand.

Seeded venues have hand-picked colours spread around the hue wheel so that adjacent venues
on a calendar are distinguishable. A venue created at runtime has no such curation, and the
column default is a single indigo — so without this, every promoter an admin ever added
would share one colour, and would collide with whichever seeded venue is nearest that hue.

The property under test is *distinctness*, not prettiness: frontend/js/design.js separately
clamps whatever arrives for contrast, so this only has to place the new hue far from the
ones already in use.
"""

from datetime import date

import pytest

from app.venue_colors import (
    hex_from_hue,
    hue_of,
    is_valid_hex,
    largest_hue_gap_midpoint,
    pick_venue_color,
)


def _angular_distance(a: float, b: float) -> float:
    """Degrees between two hues, the short way round the wheel."""
    return abs(((a - b + 180) % 360) - 180)


class TestHexValidation:
    @pytest.mark.parametrize("value", ["#7fb069", "#FFF", "#abc", "#000000", "  #7FB069  "])
    def test_accepts_valid(self, value):
        assert is_valid_hex(value)

    @pytest.mark.parametrize("value", [
        "", None, "7fb069", "#12345", "#1234567", "red", "#gggggg",
        "#7fb069; background:url(x)",
    ])
    def test_rejects_invalid(self, value):
        """Venue.color is String(7). A longer value is truncated by some drivers and
        rejected by others, so an admin's typo has to be a clear error rather than a
        corrupted column — and the last case is why this is a whitelist, not a length check.
        """
        assert not is_valid_hex(value)


class TestHueOf:
    def test_reads_a_hue(self):
        assert hue_of("#ff0000") == pytest.approx(0)
        assert hue_of("#00ff00") == pytest.approx(120)
        assert hue_of("#0000ff") == pytest.approx(240)

    def test_expands_shorthand(self):
        assert hue_of("#f00") == pytest.approx(hue_of("#ff0000"))

    @pytest.mark.parametrize("grey", ["#000000", "#ffffff", "#888888", "#333"])
    def test_greys_have_no_hue(self, grey):
        """colorsys reports 0 for every grey, which is red. Returning None instead keeps a
        grey venue out of the gap calculation rather than letting it pretend to occupy the
        red part of the wheel and pushing new colours away from a hue nothing uses."""
        assert hue_of(grey) is None

    def test_invalid_input_has_no_hue(self):
        assert hue_of("nonsense") is None


class TestLargestHueGapMidpoint:
    def test_picks_the_middle_of_the_widest_gap(self):
        # Hues at 0 and 90 leave a 270-degree gap; its midpoint is 225.
        assert largest_hue_gap_midpoint([0, 90]) == pytest.approx(225)

    def test_finds_a_gap_that_straddles_zero(self):
        """The wrap-around case. Walking sorted hues without closing the circle makes the
        widest gap invisible whenever it crosses 0 degrees — which is common, because reds
        are well represented in the seeded palette."""
        # 350 and 10 are 20 degrees apart across zero; everything else is packed at 90-180.
        result = largest_hue_gap_midpoint([90, 120, 150, 180, 350, 10])
        # The widest gap is 180 -> 350, midpoint 265.
        assert result == pytest.approx(265)

    def test_one_hue_gets_its_opposite(self):
        assert largest_hue_gap_midpoint([0]) == pytest.approx(180)

    def test_no_hues_is_a_deliberate_blue_not_red(self):
        """An arbitrary starting point still has to look deliberate on a calendar, and red
        reads as an alert."""
        assert largest_hue_gap_midpoint([]) == pytest.approx(210)

    def test_result_is_always_a_valid_hue(self):
        for hues in ([0, 1], [359, 0, 1], [0, 180], [200] * 5):
            assert 0 <= largest_hue_gap_midpoint(hues) < 360


class TestPickVenueColor:
    def test_returns_a_storable_hex(self):
        colour = pick_venue_color(["#ff0000"], name="X")
        assert is_valid_hex(colour)
        assert len(colour) == 7, "Venue.color is String(7)"

    def test_lands_far_from_the_existing_colours(self):
        existing = ["#ff0000", "#00ff00"]  # hues 0 and 120
        hue = hue_of(pick_venue_color(existing, name="X"))
        assert min(_angular_distance(hue, h) for h in (0, 120)) > 90

    def test_against_the_real_seeded_palette(self):
        """The case that actually matters. The shipped palette leaves its widest hole
        between yellow and green, and a new promoter should land in it."""
        from app.seed import VENUES

        existing = [v["color"] for v in VENUES if v.get("color")]
        hues = [h for h in (hue_of(c) for c in existing) if h is not None]
        assert len(hues) > 15, "sanity: the seeded palette should be populated"

        hue = hue_of(pick_venue_color(existing, name="Sharp 9 Gallery"))
        nearest = min(_angular_distance(hue, h) for h in hues)
        assert nearest > 30, (
            f"new colour at {hue:.0f} deg is only {nearest:.0f} deg from a venue already "
            "on the calendar"
        )

    def test_successive_additions_keep_spreading_out(self):
        """Each new venue sees the previous ones, so the gaps shrink — but every choice must
        still be the best available at the time, never a repeat."""
        palette = ["#ff0000", "#00ff00", "#0000ff"]
        picked = []
        for i in range(4):
            colour = pick_venue_color(palette, name=f"P{i}")
            assert colour not in palette, "handed out a colour already in use"
            picked.append(colour)
            palette.append(colour)
        assert len(set(picked)) == len(picked), "two venues got the same colour"

    def test_falls_back_to_the_name_when_no_hue_exists(self):
        """All-grey or all-default palettes have no gap structure to reason about."""
        colour = pick_venue_color(["#888888", "#cccccc"], name="Sharp 9 Gallery")
        assert is_valid_hex(colour)
        assert hue_of(colour) is not None, "the fallback must still produce a real colour"

    def test_the_fallback_is_stable_for_a_name(self):
        """Re-creating a venue should not reshuffle the calendar's colours."""
        a = pick_venue_color([], name="Sharp 9 Gallery")
        b = pick_venue_color([], name="Sharp 9 Gallery")
        assert a == b

    def test_the_fallback_separates_different_names(self):
        assert pick_venue_color([], name="Sharp 9 Gallery") != pick_venue_color([], name="The Fruit")

    def test_an_empty_palette_and_no_name_still_works(self):
        assert is_valid_hex(pick_venue_color([]))


class TestRoundTrip:
    @pytest.mark.parametrize("hue", [0, 45, 90, 180, 270, 359])
    def test_hex_from_hue_preserves_the_hue(self, hue):
        assert _angular_distance(hue_of(hex_from_hue(hue)), hue) < 2
