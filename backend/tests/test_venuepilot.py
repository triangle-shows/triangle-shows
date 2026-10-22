"""
Unit tests for app.scrapers.venuepilot — cleaning up the free-text `support` field.

VenuePilot's `support` field is whatever the venue typed, and Stanczyks and Rubies on
Five Points often start it with a joiner: "w/ Daniel Ouimet", "with Lamplight". Stored
verbatim, the joiner shows up as part of the support line. The scraper now drops a
leading lowercase "w/ " or "with ", which is how every observed value writes it.

Band names are written capitalized, so the strip is case-sensitive: "With Honor" is a
band, not a joiner, and is left alone. So are "w/o" and "w/out", and a bare joiner
with nothing after it.

`_parse_event` is called directly on a dict shaped like one GraphQL result, so no
network is involved.

Run from the backend/ directory:  pytest tests/test_venuepilot.py
"""

import pytest

from app.scrapers.venuepilot import VenuePilotScraper


def _scraper() -> VenuePilotScraper:
    return VenuePilotScraper("stanczyks", {"account_id": 1})


def _support(value):
    item = {"id": 1, "name": "Headliner", "date": "2026-11-05", "support": value}
    event = _scraper()._parse_event(item)
    assert event is not None
    return event.support_artists


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Values observed on the live feeds.
        ("w/ Daniel Ouimet", "Daniel Ouimet"),
        ("w/ Hannah Wilson", "Hannah Wilson"),
        ("w/ Gray Young, Delver", "Gray Young, Delver"),
        ("with Lamplight", "Lamplight"),
        ("with DJ VSPRTN", "DJ VSPRTN"),
        # No space after the slash, and extra whitespace around the remainder.
        ("w/Daniel Ouimet", "Daniel Ouimet"),
        ("  with   Lamplight  ", "Lamplight"),
    ],
)
def test_leading_joiner_is_dropped(raw, expected):
    assert _support(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        # A capitalized "With" is part of a name, not a joiner.
        ("With Honor", "With Honor"),
        ("Withered Hand", "Withered Hand"),
        ("without", "without"),
        ("w/o Daniel Ouimet", "w/o Daniel Ouimet"),
        ("w/out Daniel Ouimet", "w/out Daniel Ouimet"),
        # A bare joiner is kept rather than turned into an empty support line.
        ("with", "with"),
        ("w/", "w/"),
        # Unprefixed values are unchanged.
        ("Gray Young, Delver", "Gray Young, Delver"),
        ("", None),
        (None, None),
    ],
)
def test_other_values_are_left_alone(raw, expected):
    assert _support(raw) == expected
