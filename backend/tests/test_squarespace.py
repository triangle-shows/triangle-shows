"""
Tests for the Squarespace scraper's description handling.

Two regressions covered here:

- #35: an event whose body has no `div.sqs-html-content` block raised IndexError,
  which the broad except in _parse_event swallowed — dropping the whole event.
  Every Boom Club listing has an empty body, so the venue sat at zero events in the
  database while its scrape logs reported success.
- #13: `excerpt` content (Boom Club fills this in with real summaries) was read with
  the same "look for a Post-Body wrapper, skip its first child" logic that only
  applies to `body`. Excerpt has no such wrapper and never repeats the title, so real
  description text was silently discarded — not shown as raw HTML, but lost outright.

The markup fixtures below are trimmed copies of what the two live feeds return.
"""

from datetime import date

from app.scrapers.squarespace import SquarespaceScraper


# --- Fixtures drawn from the live feeds ---

# Boom Club: the venue never filled in a body, so Squarespace emits layout
# scaffolding marked `empty` with no sqs-html-content block anywhere.
EMPTY_BODY = (
    '<div class="sqs-layout sqs-grid-12 columns-12 empty" data-layout-label="Post Body"'
    ' data-type="item" id="item-6a626f7c24c2d308343e7804">'
    '<div class="row sqs-row"><div class="col sqs-col-12 span-12"></div></div></div>'
)

# Neptune's Parlour: the Post Body editor's first <p> always repeats the event's own
# title verbatim (confirmed against the live feed) — real content follows after it.
POPULATED_BODY = (
    '<div class="sqs-layout"><div class="row sqs-row"><div class="col">'
    '<div class="sqs-block html-block"><div class="sqs-block-content">'
    '<div class="sqs-html-content">'
    '<p class="" style="white-space:pre-wrap;">The Regulars</p>'
    "<p>Doors at 8pm.</p>"
    "<p>$12 advance</p>"
    "</div></div></div></div></div></div>"
)

# Boom Club: `excerpt` is a genuinely separate summary field — bare <p> tags, no
# Post-Body wrapper, and never a repeat of the title (confirmed against the live feed,
# where `body` for these same events looks like an unrelated, mostly-empty layout).
EXCERPT_CONTENT = (
    '<p style="white-space:pre-wrap;" data-rte-preserve-empty="true">'
    "Modular virtuoso Matthew Cha presents a live, semi-improvised suite.</p>"
)

START_MS = 1788000000000  # milliseconds, as Squarespace sends them


def _scraper() -> SquarespaceScraper:
    return SquarespaceScraper("boom-club", {"url": "https://example.org/events?format=json"})


def _item(**overrides) -> dict:
    item = {"title": "MICROMACHINES", "startDate": START_MS, "body": EMPTY_BODY}
    item.update(overrides)
    return item


# --- _select_items ---

class TestSelectItems:
    def test_reads_the_items_key(self):
        assert SquarespaceScraper._select_items({"items": [1, 2]}) == [1, 2]

    def test_reads_the_upcoming_key(self):
        """Boom Club's template has no 'items' key at all."""
        assert SquarespaceScraper._select_items({"upcoming": [1]}) == [1]

    def test_reads_the_events_key(self):
        assert SquarespaceScraper._select_items({"events": [1]}) == [1]

    def test_an_empty_items_key_does_not_mask_upcoming(self):
        """The reason for `or` over a get() default — a default only fires when the
        key is absent, so "items": [] used to win and hide the real events."""
        data = {"items": [], "upcoming": [{"title": "MICROMACHINES"}]}
        assert SquarespaceScraper._select_items(data) == [{"title": "MICROMACHINES"}]

    def test_items_wins_when_both_are_populated(self):
        assert SquarespaceScraper._select_items({"items": [1], "upcoming": [2]}) == [1]

    def test_no_known_key_yields_an_empty_list(self):
        assert SquarespaceScraper._select_items({"past": [1]}) == []

    def test_all_keys_empty_yields_an_empty_list(self):
        assert SquarespaceScraper._select_items({"items": [], "upcoming": []}) == []


# --- _extract_description ---

class TestExtractDescription:
    def test_empty_body_yields_none(self):
        assert _scraper()._extract_description(_item(), "MICROMACHINES") is None

    def test_missing_body_and_excerpt_yields_none(self):
        assert _scraper()._extract_description({"title": "X"}, "X") is None

    def test_populated_body_is_read_as_plain_text(self):
        out = _scraper()._extract_description(_item(body=POPULATED_BODY), "The Regulars")
        assert "Doors at 8pm." in out
        assert "$12 advance" in out

    def test_leading_title_paragraph_is_dropped_from_body(self):
        """body's Post-Body editor repeats the title as its first paragraph (#17)."""
        out = _scraper()._extract_description(_item(body=POPULATED_BODY), "The Regulars")
        assert "The Regulars" not in out

    def test_html_is_stripped(self):
        """#17 — raw markup must not reach the database."""
        out = _scraper()._extract_description(_item(body=POPULATED_BODY), "The Regulars")
        assert "<p>" not in out and "div" not in out

    def test_excerpt_is_preferred_over_body(self):
        item = _item(excerpt=EXCERPT_CONTENT, body=POPULATED_BODY)
        out = _scraper()._extract_description(item, "The Regulars")
        assert "Matthew Cha" in out

    def test_excerpt_with_no_wrapper_is_still_read(self):
        """The #13 regression: excerpt has no Post-Body div to find, and previously
        that meant it was read as having nothing in it."""
        item = _item(excerpt=EXCERPT_CONTENT)
        out = _scraper()._extract_description(item, "Matthew Cha \"Technojazz\" Release")
        assert out is not None
        assert "Modular virtuoso Matthew Cha" in out

    def test_excerpt_is_never_checked_against_the_title(self):
        """Only body's known title-repeat paragraph is dropped. Excerpt is a genuinely
        separate field and must survive even a coincidental title match."""
        item = _item(excerpt="<p>The Regulars</p><p>A special guest tonight.</p>")
        out = _scraper()._extract_description(item, "The Regulars")
        assert "The Regulars" in out
        assert "A special guest tonight." in out


# --- _parse_event: the actual #35 regression ---

class TestParseEventSurvivesEmptyDescription:
    def test_event_with_empty_body_is_still_returned(self):
        """The regression: this used to return None and the event vanished."""
        parsed = _scraper()._parse_event(_item())

        assert parsed is not None, "an empty description must not discard the event"
        assert parsed.name == "MICROMACHINES"
        assert parsed.description is None

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

    def test_excerpt_content_survives_end_to_end(self):
        """The #13 regression, through the full parse path rather than just the
        extraction helper: Boom Club's real excerpts must reach the stored event."""
        item = _item(
            title='Matthew Cha "Technojazz" Release',
            excerpt=EXCERPT_CONTENT,
            body=EMPTY_BODY,
        )
        parsed = _scraper()._parse_event(item)

        assert parsed is not None
        assert parsed.description is not None
        assert "Modular virtuoso Matthew Cha" in parsed.description
        assert "<p>" not in parsed.description
