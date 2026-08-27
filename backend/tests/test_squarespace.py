"""
Tests for the Squarespace scraper's description handling.

Regression cover for #35: an event whose body has no `div.sqs-html-content` block
raised IndexError, which the broad except in _parse_event swallowed — dropping the
whole event. Every Boom Club listing has an empty body, so the venue sat at zero
events in the database while its scrape logs reported success.

The markup fixtures below are trimmed copies of what the two live feeds return.
"""

import sys
from datetime import date
from pathlib import Path

# Make `app` importable when running pytest from backend/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.scrapers.squarespace import SquarespaceScraper  # noqa: E402


# --- Fixtures drawn from the live feeds ---

# Boom Club: the venue never filled in a body, so Squarespace emits layout
# scaffolding marked `empty` with no sqs-html-content block anywhere.
EMPTY_BODY = (
    '<div class="sqs-layout sqs-grid-12 columns-12 empty" data-layout-label="Post Body"'
    ' data-type="item" id="item-6a626f7c24c2d308343e7804">'
    '<div class="row sqs-row"><div class="col sqs-col-12 span-12"></div></div></div>'
)

# Neptune's Parlour: a populated body. First child repeats the title, so it is skipped.
POPULATED_BODY = (
    '<div class="sqs-layout"><div class="row sqs-row"><div class="col">'
    '<div class="sqs-block html-block"><div class="sqs-block-content">'
    '<div class="sqs-html-content">'
    "<h2>The Regulars</h2>"
    "<p>Doors at 8pm.</p>"
    "<p>$12 advance</p>"
    "</div></div></div></div></div></div>"
)

START_MS = 1788000000000  # milliseconds, as Squarespace sends them


def _scraper() -> SquarespaceScraper:
    return SquarespaceScraper("boom-club", {"url": "https://example.org/events?format=json"})


def _item(**overrides) -> dict:
    item = {"title": "MICROMACHINES", "startDate": START_MS, "body": EMPTY_BODY}
    item.update(overrides)
    return item


# --- _extract_description ---

class TestExtractDescription:
    def test_empty_body_yields_empty_string(self):
        assert _scraper()._extract_description(_item()) == ''

    def test_missing_body_and_excerpt_yields_empty_string(self):
        assert _scraper()._extract_description({"title": "X"}) == ''

    def test_populated_body_is_read_as_plain_text(self):
        out = _scraper()._extract_description(_item(body=POPULATED_BODY))
        assert "Doors at 8pm." in out
        assert "$12 advance" in out

    def test_leading_title_child_is_skipped(self):
        """The first child of sqs-html-content repeats the title (#17)."""
        out = _scraper()._extract_description(_item(body=POPULATED_BODY))
        assert "The Regulars" not in out

    def test_html_is_stripped(self):
        """#17 — raw markup must not reach the database."""
        out = _scraper()._extract_description(_item(body=POPULATED_BODY))
        assert "<p>" not in out and "div" not in out

    def test_excerpt_is_preferred_over_body(self):
        item = _item(excerpt=POPULATED_BODY, body=EMPTY_BODY)
        assert "Doors at 8pm." in _scraper()._extract_description(item)


# --- _parse_event: the actual #35 regression ---

class TestParseEventSurvivesEmptyDescription:
    def test_event_with_empty_body_is_still_returned(self):
        """The regression: this used to return None and the event vanished."""
        parsed = _scraper()._parse_event(_item())

        assert parsed is not None, "an empty description must not discard the event"
        assert parsed.name == "MICROMACHINES"
        assert parsed.description == ''

    def test_event_with_empty_body_keeps_its_required_fields(self):
        parsed = _scraper()._parse_event(_item())

        assert isinstance(parsed.date, date)
        assert parsed.venue_slug == "boom-club"
        assert parsed.source == "squarespace"

    def test_event_with_a_description_still_parses(self):
        parsed = _scraper()._parse_event(_item(body=POPULATED_BODY))

        assert parsed is not None
        assert "Doors at 8pm." in parsed.description

    def test_price_is_still_read_from_a_populated_description(self):
        """Description feeds price parsing, so the two must stay wired together."""
        parsed = _scraper()._parse_event(_item(body=POPULATED_BODY))
        assert parsed.price_min == 12.0

    def test_missing_title_is_still_rejected(self):
        """Required fields must keep failing closed — only description got lenient."""
        assert _scraper()._parse_event(_item(title="")) is None

    def test_missing_start_date_is_still_rejected(self):
        item = _item()
        del item["startDate"]
        assert _scraper()._parse_event(item) is None

    def test_excluded_titles_are_still_skipped(self):
        scraper = SquarespaceScraper("boom-club", {
            "url": "https://example.org/events?format=json",
            "exclude_titles": ["micromachines"],
        })
        assert scraper._parse_event(_item()) is None
