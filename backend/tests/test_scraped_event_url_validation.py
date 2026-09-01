"""Unit tests for ScrapedEvent's ticket_url/image_url normalization.

Pure-function tests — no database, no HTTP. Both fields are scraped off third-party
venue pages and are rendered directly into HTML attributes by the web client
(frontend/js/modal.js) and into the iCal feed (app/api/feeds.py).
ScrapedEvent.__post_init__ normalizes anything that isn't an absolute http(s) URL to
None before it ever reaches the database.

The constraint under test throughout: a malformed value nulls the field out, it never
raises and never drops the event — see test_malformed_url_does_not_prevent_event_construction.
"""

from datetime import date

from app.scrapers.base import ScrapedEvent


def _event(**overrides) -> ScrapedEvent:
    fields = dict(name="Test Show", date=date(2026, 1, 1), venue_slug="test-venue", source="manual")
    fields.update(overrides)
    return ScrapedEvent(**fields)


def test_well_formed_https_ticket_url_passes_through_unchanged():
    ev = _event(ticket_url="https://tickets.example.com/42?ref=abc&utm_source=site")
    assert ev.ticket_url == "https://tickets.example.com/42?ref=abc&utm_source=site"


def test_well_formed_http_image_url_passes_through_unchanged():
    ev = _event(image_url="http://cdn.example.com/poster.jpg")
    assert ev.image_url == "http://cdn.example.com/poster.jpg"


def test_https_prefix_is_case_insensitive():
    ev = _event(ticket_url="HTTPS://tickets.example.com/42")
    assert ev.ticket_url == "HTTPS://tickets.example.com/42"


def test_surrounding_whitespace_is_trimmed():
    ev = _event(ticket_url="  https://tickets.example.com/42  ")
    assert ev.ticket_url == "https://tickets.example.com/42"


def test_javascript_scheme_becomes_none():
    ev = _event(ticket_url="javascript:alert(document.cookie)")
    assert ev.ticket_url is None


def test_data_scheme_becomes_none():
    ev = _event(image_url="data:text/html,<script>alert(1)</script>")
    assert ev.image_url is None


def test_relative_path_becomes_none():
    ev = _event(image_url="/images/poster.jpg")
    assert ev.image_url is None


def test_scheme_relative_url_becomes_none():
    ev = _event(image_url="//evil.example/poster.jpg")
    assert ev.image_url is None


def test_blank_and_whitespace_only_become_none():
    assert _event(ticket_url="").ticket_url is None
    assert _event(ticket_url="   ").ticket_url is None


def test_non_string_becomes_none_rather_than_raising():
    # A JSON-LD feed can hand a scraper a dict/list where a URL string belongs.
    ev = _event(ticket_url=12345, image_url={"url": "https://x"})
    assert ev.ticket_url is None
    assert ev.image_url is None


def test_none_stays_none():
    ev = _event()
    assert ev.ticket_url is None
    assert ev.image_url is None


def test_malformed_url_does_not_prevent_event_construction():
    # The core constraint: a bad field is dropped, never the event.
    ev = _event(name="Still Shows Up", ticket_url="not a url at all", image_url="also not a url")
    assert ev.name == "Still Shows Up"
    assert ev.ticket_url is None
    assert ev.image_url is None
