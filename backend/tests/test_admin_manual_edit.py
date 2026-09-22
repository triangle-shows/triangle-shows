"""Tests for editing hand-added venues and events.

Editing is the one admin write that can be quietly undone by something else. Two
different mechanisms do it, and the endpoints exist in the shape they do because of
them:

  seed_venues() runs on every boot and does `setattr(existing, key, value)` for every
  field of every venue whose slug is listed in VENUES. An edit to a seeded venue lasts
  until the next deploy and then reverts, which looks like the save button is broken.

  A scrape writes its values back over a matched row -- `row.name = se.name` and the
  rest -- so an edit to a scraped event lasts until the next cycle.

Both refusals are asserted below, because losing either one produces a bug that nobody
sees at the time and nobody can reproduce afterwards.

The third load-bearing rule is that the slug never moves. Event.hash is
`venue_slug | date | name`, stored and unique, and plan_upsert falls back to it when a
source has no external_id. A renamed slug strands every existing hash.

No database. The handlers take a session, so a stand-in that answers get() and execute()
is enough to exercise every branch, the same approach test_manual_events.py takes.
"""

import asyncio
from datetime import date, time

import pytest
from fastapi import HTTPException

from app.api.admin import (
    ManualEventEditBody,
    ManualVenueEditBody,
    edit_manual_event,
    edit_manual_venue,
)
from app.models import MANUAL_SCRAPER_TYPE, Event, Venue


# --- Stand-ins ---------------------------------------------------------------


class _Session:
    """Answers get() from a dict and execute() with a queued scalar result."""

    def __init__(self, rows=None, scalar_results=None):
        self.rows = rows or {}
        self.scalar_results = list(scalar_results or [])
        self.committed = False

    async def get(self, model, pk):
        return self.rows.get((model.__name__, pk))

    async def execute(self, statement, *a, **kw):
        value = self.scalar_results.pop(0) if self.scalar_results else None

        class _Result:
            def scalar_one_or_none(self_inner):
                return value

        return _Result()

    async def commit(self):
        self.committed = True


def _venue(**kw):
    v = Venue()
    v.id = kw.get("id", 1)
    v.name = kw.get("name", "The Fruit")
    v.slug = kw.get("slug", "the-fruit")
    v.city = kw.get("city", "Durham")
    v.size_category = kw.get("size_category", "small")
    v.website = kw.get("website")
    v.color = kw.get("color", "#7fb069")
    v.scraper_type = kw.get("scraper_type", MANUAL_SCRAPER_TYPE)
    return v


def _event(**kw):
    e = Event()
    e.id = kw.get("id", 10)
    e.venue_id = kw.get("venue_id", 1)
    e.name = kw.get("name", "Some Show")
    e.date = kw.get("date", date(2026, 10, 1))
    e.hash = kw.get("hash", "oldhash")
    e.is_manually_created = kw.get("is_manually_created", True)
    e.artist = kw.get("artist")
    e.show_time = kw.get("show_time")
    e.doors_time = kw.get("doors_time")
    e.ticket_url = kw.get("ticket_url")
    e.price_min = kw.get("price_min")
    e.price_max = kw.get("price_max")
    e.description = kw.get("description")
    e.genre = kw.get("genre")
    e.age_restriction = kw.get("age_restriction")
    return e


def run(coro):
    return asyncio.run(coro)


# --- Venues ------------------------------------------------------------------


def test_a_seeded_venue_cannot_be_edited():
    """The refusal that stops an edit silently reverting on the next deploy."""
    venue = _venue(scraper_type="ticketmaster", name="Cat's Cradle")
    session = _Session({("Venue", 1): venue})

    with pytest.raises(HTTPException) as exc:
        run(edit_manual_venue(1, ManualVenueEditBody(city="Carrboro"), session=session))

    assert exc.value.status_code == 400
    assert "seed" in exc.value.detail.lower()
    assert venue.city == "Durham", "the refusal has to happen before the write"
    assert not session.committed


def test_renaming_a_manual_venue_leaves_the_slug_alone():
    """Event.hash is venue_slug|date|name, so a moved slug strands every stored hash."""
    venue = _venue(name="The Fruit", slug="the-fruit")
    session = _Session({("Venue", 1): venue})

    out = run(edit_manual_venue(1, ManualVenueEditBody(name="The Fruit Warehouse"),
                                session=session))

    assert venue.name == "The Fruit Warehouse"
    assert venue.slug == "the-fruit", "the slug is immutable by design"
    assert out["name"] == "The Fruit Warehouse"
    assert session.committed


def test_renaming_onto_another_venues_name_is_refused():
    session = _Session({("Venue", 1): _venue()}, scalar_results=[_venue(id=2, name="Kings")])

    with pytest.raises(HTTPException) as exc:
        run(edit_manual_venue(1, ManualVenueEditBody(name="Kings"), session=session))

    assert exc.value.status_code == 409
    assert not session.committed


def test_fields_not_sent_are_left_alone():
    """PATCH, not PUT: absence means unchanged, which is what model_fields_set gives."""
    venue = _venue(city="Durham", website="https://example.com", color="#7fb069")
    session = _Session({("Venue", 1): venue})

    run(edit_manual_venue(1, ManualVenueEditBody(size_category="medium"), session=session))

    assert venue.size_category == "medium"
    assert venue.city == "Durham"
    assert venue.website == "https://example.com"
    assert venue.color == "#7fb069"


def test_a_field_sent_as_null_is_cleared():
    """The other half of the same rule: explicitly null means clear, not ignore."""
    venue = _venue(website="https://example.com")
    session = _Session({("Venue", 1): venue})

    run(edit_manual_venue(1, ManualVenueEditBody(website=None), session=session))

    assert venue.website is None


def test_an_empty_name_is_refused():
    venue = _venue()
    session = _Session({("Venue", 1): venue})

    with pytest.raises(HTTPException) as exc:
        run(edit_manual_venue(1, ManualVenueEditBody(name="   "), session=session))

    assert exc.value.status_code == 400
    assert venue.name == "The Fruit"


def test_a_bad_colour_is_refused():
    venue = _venue()
    session = _Session({("Venue", 1): venue})

    with pytest.raises(HTTPException) as exc:
        run(edit_manual_venue(1, ManualVenueEditBody(color="teal"), session=session))

    assert exc.value.status_code == 400
    assert venue.color == "#7fb069"


def test_a_missing_venue_is_404():
    with pytest.raises(HTTPException) as exc:
        run(edit_manual_venue(99, ManualVenueEditBody(city="Raleigh"), session=_Session()))
    assert exc.value.status_code == 404


# --- Events ------------------------------------------------------------------


def test_a_scraped_event_cannot_be_edited():
    """The refusal that stops an edit being overwritten by the next scrape."""
    event = _event(is_manually_created=False, name="Real Show")
    session = _Session({("Event", 10): event})

    with pytest.raises(HTTPException) as exc:
        run(edit_manual_event(10, ManualEventEditBody(name="Renamed"), session=session))

    assert exc.value.status_code == 400
    assert "scrape" in exc.value.detail.lower()
    assert event.name == "Real Show"
    assert not session.committed


def test_renaming_an_event_recomputes_the_hash():
    """Stale, it would let the same show be hand-added twice without complaint."""
    event = _event(name="Some Show", hash="oldhash")
    session = _Session({("Event", 10): event, ("Venue", 1): _venue()})

    run(edit_manual_event(10, ManualEventEditBody(name="Some Show With Support"),
                          session=session))

    assert event.name == "Some Show With Support"
    assert event.hash != "oldhash"
    assert session.committed


def test_an_edit_colliding_with_an_existing_event_is_refused():
    event = _event()
    session = _Session(
        {("Event", 10): event, ("Venue", 1): _venue()},
        scalar_results=[_event(id=11, name="Already There")],
    )

    with pytest.raises(HTTPException) as exc:
        run(edit_manual_event(10, ManualEventEditBody(name="Already There"), session=session))

    assert exc.value.status_code == 409
    assert not session.committed


def test_editing_only_the_time_leaves_the_hash_alone():
    """The hash covers venue, date and name — nothing else should disturb it."""
    event = _event(hash="stable")
    session = _Session({("Event", 10): event, ("Venue", 1): _venue()})

    run(edit_manual_event(10, ManualEventEditBody(show_time="20:00"), session=session))

    assert event.show_time == time(20, 0)
    assert event.hash == "stable"


def test_an_event_can_move_to_a_scraped_venue():
    """A show the scraper missed at a real venue is what hand-adding is for."""
    event = _event(venue_id=1)
    other = _venue(id=2, name="Cat's Cradle", slug="cats-cradle", scraper_type="ticketmaster")
    session = _Session({("Event", 10): event, ("Venue", 1): _venue(), ("Venue", 2): other})

    out = run(edit_manual_event(10, ManualEventEditBody(venue_id=2), session=session))

    assert event.venue_id == 2
    assert event.hash != "oldhash", "the venue is part of the hash"
    assert out["venue_name"] == "Cat's Cradle"


def test_prices_out_of_order_are_refused():
    event = _event(price_min=20.0)
    session = _Session({("Event", 10): event, ("Venue", 1): _venue()})

    with pytest.raises(HTTPException) as exc:
        run(edit_manual_event(10, ManualEventEditBody(price_max=5.0), session=session))

    assert exc.value.status_code == 400
    assert event.price_max is None


def test_a_date_beyond_the_horizon_is_refused():
    event = _event()
    session = _Session({("Event", 10): event, ("Venue", 1): _venue()})

    with pytest.raises(HTTPException) as exc:
        run(edit_manual_event(10, ManualEventEditBody(date="2099-01-01"), session=session))

    assert exc.value.status_code == 400
    assert event.date == date(2026, 10, 1)


def test_a_malformed_date_is_refused():
    event = _event()
    session = _Session({("Event", 10): event, ("Venue", 1): _venue()})

    with pytest.raises(HTTPException) as exc:
        run(edit_manual_event(10, ManualEventEditBody(date="next tuesday"), session=session))

    assert exc.value.status_code == 400
