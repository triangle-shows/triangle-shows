"""Unit tests for EventResponse's ticket_url/image_url normalization.

Pure Pydantic-model tests — no database. This is the second, independent gate on
these two fields: app.scrapers.base.ScrapedEvent.__post_init__ already normalizes
them at ingestion (tests/test_scraped_event_url_validation.py), but EventResponse is
what the events list endpoint actually serves, regardless of how a row reached the
database — a row written before the ingestion-time gate existed, or by any future
path that does not go through ScrapedEvent. Both gates must independently null out a
malformed value rather than raise, so one bad field never fails a whole event.
"""

from datetime import date

from app.schemas import EventResponse


def _min_fields(**overrides):
    fields = dict(
        id=1,
        venue_id=1,
        name="Test Show",
        date=date(2026, 1, 1),
        status="on_sale",
        source="manual",
    )
    fields.update(overrides)
    return fields


def test_well_formed_https_ticket_url_passes_through_unchanged():
    ev = EventResponse(**_min_fields(ticket_url="https://tickets.example.com/42?ref=abc&utm_source=site"))
    assert ev.ticket_url == "https://tickets.example.com/42?ref=abc&utm_source=site"


def test_well_formed_http_image_url_passes_through_unchanged():
    ev = EventResponse(**_min_fields(image_url="http://cdn.example.com/poster.jpg"))
    assert ev.image_url == "http://cdn.example.com/poster.jpg"


def test_javascript_scheme_ticket_url_becomes_none():
    ev = EventResponse(**_min_fields(ticket_url="javascript:alert(document.cookie)"))
    assert ev.ticket_url is None


def test_relative_image_url_becomes_none():
    ev = EventResponse(**_min_fields(image_url="/images/poster.jpg"))
    assert ev.image_url is None


def test_attribute_breakout_payload_that_still_starts_with_https_is_not_this_layers_job():
    # The prefix check alone cannot stop a `"` later in the string — that is the
    # frontend _h() escaping's job, in frontend/js/modal.js. A value that starts with
    # a real https:// scheme is a well-formed absolute URL as far as this validator is
    # concerned and passes through unchanged. Documented here so a future reader does
    # not mistake this gate for the whole fix.
    payload = 'https://evil.example/show?ref=1" onmouseover="alert(1)'
    ev = EventResponse(**_min_fields(ticket_url=payload))
    assert ev.ticket_url == payload


def test_non_string_becomes_none():
    ev = EventResponse(**_min_fields(ticket_url=12345))
    assert ev.ticket_url is None


def test_none_stays_none():
    ev = EventResponse(**_min_fields())
    assert ev.ticket_url is None
    assert ev.image_url is None
