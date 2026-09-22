"""Tests for the Duke Bedework scraper.

Duke is the first source where one feed serves several venues, and the first where the
set of venues cannot be known in advance: there is no locations endpoint, and the feed
only ever shows the next 40 events, so any fixed list of rooms is a guess. The catch-all
row exists for that reason and is what these assert hardest — a concert in a room nobody
had seen before must still reach the calendar.

The other load-bearing rules:

  Dates come from the feed's *local* stamp, not its UTC one. A 9pm carillon recital is
  17:00 local and 21:00Z; reading the UTC value would file half the year's late shows on
  the following day. frontend/js/lineup.js documents the same trap for stored favourites.

  external_id is guid + recurrenceId, because the guid alone is shared by every instance
  of a recurring series — eighteen carillon recitals in one sample carried one guid.
  plan_upsert matches on (external_id, date), so a guid-only id would collapse a series
  to a single event the first time two instances landed in the same window.

No network. `_fetch_feed` is patched with a fixture cut down from a real response.
"""

import asyncio
from datetime import date, time

import pytest

from app.scrapers import duke_bedework as duke
from app.scrapers.duke_bedework import (
    DukeBedeworkScraper,
    external_id,
    location_line,
    location_name,
    parse_start,
)


CHAPEL = "18832edc-1b27e154-011b-281ad92b-00000024"
BALDWIN = "18832edc-1b23ba5b-011b-27d4ee03-0000000e"
TOBACCO = "8a087089-37a82cb0-0137-c28c6651-0000224e"


def _raw(summary, uid, name, local, utc, guid, rid=None, **extra):
    return {
        "summary": summary,
        "guid": guid,
        "recurrenceId": rid,
        "location": {"uid": uid, "address": name},
        "start": {"unformatted": local, "utcdate": utc, "allday": "false"},
        **extra,
    }


FEED = [
    _raw("Weekday Carillon Recital", CHAPEL, "Duke Chapel",
         "20260922T170000", "20260922T210000Z", "CAL-carillon", "20260922T210000Z"),
    _raw("Weekday Carillon Recital", CHAPEL, "Duke Chapel",
         "20260923T170000", "20260923T210000Z", "CAL-carillon", "20260923T210000Z"),
    _raw("Duke Symphony Orchestra", BALDWIN, "Baldwin Auditorium",
         "20260926T193000", "20260926T233000Z", "CAL-dso"),
    _raw("Duke Arts at American Tobacco: Ken Pomeroy", TOBACCO, "American Tobacco Campus - Lawn",
         "20260923T183000", "20260923T223000Z", "CAL-pomeroy",
         link="https://example.com/tickets", description="  An outdoor show.  "),
]


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def feed(monkeypatch):
    """Serve the fixture, and make sure no test inherits another's cached fetch.

    Yields a setter, because two tests below need a different feed and the real cache
    is not consulted once _fetch_feed itself is replaced.
    """
    duke._reset_feed_cache()
    current = [list(FEED)]

    async def fake(url):
        return list(current[0])

    monkeypatch.setattr(duke, "_fetch_feed", fake)
    yield lambda events: current.__setitem__(0, list(events))
    duke._reset_feed_cache()


# --- The catch-all, which is the whole reason the design looks like this -----


def test_the_catch_all_takes_everything():
    events = run(DukeBedeworkScraper("duke-university", {"catch_all": True}).scrape())
    assert len(events) == len(FEED)
    assert {e.venue_slug for e in events} == {"duke-university"}


def test_a_room_nobody_has_seen_before_still_reaches_the_calendar(feed):
    """The failure the catch-all exists to prevent. Duke publishes no list of its
    locations, so a fixed set of rooms would drop this silently."""
    surprise = _raw("A Show Somewhere New", "uid-nobody-has-seen", "Rubenstein Arts Center",
                    "20260927T200000", "20260928T000000Z", "CAL-new")
    feed(FEED + [surprise])

    events = run(DukeBedeworkScraper("duke-university", {"catch_all": True}).scrape())
    assert "A Show Somewhere New" in {e.name for e in events}


def test_the_catch_all_leaves_claimed_rooms_alone():
    """Once a room has a venue row, the catch-all must stop taking it, or the event
    would be filed at two venues at once."""
    events = run(DukeBedeworkScraper(
        "duke-university", {"catch_all": True, "exclude_uids": [CHAPEL]}
    ).scrape())
    assert len(events) == 2
    assert "Weekday Carillon Recital" not in {e.name for e in events}


def test_a_named_row_takes_only_its_own_rooms():
    events = run(DukeBedeworkScraper("duke-chapel", {"location_uids": [CHAPEL]}).scrape())
    assert len(events) == 2
    assert {e.name for e in events} == {"Weekday Carillon Recital"}


def test_named_rows_and_the_catch_all_partition_the_feed():
    """Together they must cover the feed exactly — nothing dropped, nothing twice."""
    chapel = run(DukeBedeworkScraper("duke-chapel", {"location_uids": [CHAPEL]}).scrape())
    rest = run(DukeBedeworkScraper(
        "duke-university", {"catch_all": True, "exclude_uids": [CHAPEL]}
    ).scrape())
    ids = [e.external_id for e in chapel] + [e.external_id for e in rest]
    assert len(ids) == len(FEED)
    assert len(set(ids)) == len(ids)


def test_a_row_that_claims_nothing_is_a_configuration_error():
    """Silently scraping zero events would look like a venue with no shows on."""
    with pytest.raises(ValueError, match="claim nothing"):
        run(DukeBedeworkScraper("duke-chapel", {}).scrape())


# --- Dates and identity -------------------------------------------------------


def test_the_date_and_time_come_from_the_local_stamp():
    """17:00 local / 21:00Z. Reading the UTC value would give 21:00 on the same day here,
    and the following day for any show after 8pm."""
    events = run(DukeBedeworkScraper("duke-university", {"catch_all": True}).scrape())
    recital = next(e for e in events if e.name == "Weekday Carillon Recital")
    assert recital.date == date(2026, 9, 22)
    assert recital.show_time == time(17, 0)


def test_a_late_show_stays_on_its_own_day():
    """The 7:30pm Baldwin concert is 23:30Z — the case that moves if UTC is read."""
    events = run(DukeBedeworkScraper("duke-university", {"catch_all": True}).scrape())
    dso = next(e for e in events if e.name == "Duke Symphony Orchestra")
    assert dso.date == date(2026, 9, 26)
    assert dso.show_time == time(19, 30)


def test_recurring_instances_get_distinct_ids():
    """One guid, many nights. Without the recurrence id they would collapse into one."""
    events = run(DukeBedeworkScraper("duke-university", {"catch_all": True}).scrape())
    recitals = [e for e in events if e.name == "Weekday Carillon Recital"]
    assert len(recitals) == 2
    assert len({e.external_id for e in recitals}) == 2
    assert all("CAL-carillon_" in e.external_id for e in recitals)


def test_a_one_off_keeps_its_bare_guid():
    assert external_id({"guid": "CAL-dso", "recurrenceId": None}) == "CAL-dso"
    assert external_id({"guid": "", "recurrenceId": "x"}) is None


def test_an_all_day_event_has_a_date_and_no_time():
    raw = {"start": {"unformatted": "20260922T000000", "allday": "true"}}
    assert parse_start(raw) == (date(2026, 9, 22), None)


def test_an_unparseable_start_is_dropped_rather_than_guessed(feed):
    assert parse_start({"start": {"unformatted": "soon"}}) is None
    feed([_raw("Broken", CHAPEL, "Duke Chapel", "soon", "", "CAL-x")])
    assert run(DukeBedeworkScraper("duke-university", {"catch_all": True}).scrape()) == []


# --- Other fields -------------------------------------------------------------


def test_the_calendars_own_page_is_always_the_source_url():
    """`link` is set on about one event in eight, so it cannot be the only link."""
    events = run(DukeBedeworkScraper("duke-university", {"catch_all": True}).scrape())
    pomeroy = next(e for e in events if "Pomeroy" in e.name)
    plain = next(e for e in events if e.name == "Duke Symphony Orchestra")

    assert pomeroy.ticket_url == "https://example.com/tickets"
    assert plain.ticket_url is None
    assert plain.source_url == "https://calendar.duke.edu/show?fq=id:CAL-dso"


def test_text_is_trimmed():
    events = run(DukeBedeworkScraper("duke-university", {"catch_all": True}).scrape())
    pomeroy = next(e for e in events if "Pomeroy" in e.name)
    assert pomeroy.description.endswith("An outdoor show.")


# --- The room, which the venue row cannot carry -------------------------------
#
# Every Duke event is filed under one "Duke University" venue, so the calendar tile says
# the same thing whether the show is in Duke Chapel or on the lawn at American Tobacco.
# The description is the only place a visitor can be told which.


def test_the_room_leads_the_description():
    events = run(DukeBedeworkScraper("duke-university", {"catch_all": True}).scrape())
    pomeroy = next(e for e in events if "Pomeroy" in e.name)
    assert pomeroy.description == (
        "American Tobacco Campus - Lawn\n\nAn outdoor show."
    )


def test_an_event_with_no_blurb_gets_the_room_alone():
    """Most of this feed has no description at all, so this is the common case."""
    events = run(DukeBedeworkScraper("duke-university", {"catch_all": True}).scrape())
    dso = next(e for e in events if e.name == "Duke Symphony Orchestra")
    assert dso.description == "Baldwin Auditorium"


def test_the_building_comes_with_the_room_when_the_feed_names_one(feed):
    raw = _raw("Evensong", "uid-goodson", "Goodson Chapel",
               "20260924T170000", "20260924T210000Z", "CAL-evensong")
    raw["location"]["subaddress"] = "Westbrook Building"
    feed([raw])

    events = run(DukeBedeworkScraper("duke-university", {"catch_all": True}).scrape())
    assert events[0].description == "Goodson Chapel, Westbrook Building"


def test_the_feeds_literal_None_is_not_a_room(feed):
    """Duke writes the string "None", not a null, when an event has no location. Two of
    forty carried it in one sample, and unfiltered each would open with the word None."""
    raw = _raw("Somewhere Unstated", "00f1fcdb-uid", "None",
               "20260925T190000", "20260925T230000Z", "CAL-nowhere",
               description="A show with no room given.")
    feed([raw])

    events = run(DukeBedeworkScraper("duke-university", {"catch_all": True}).scrape())
    assert events[0].description == "A show with no room given."
    assert location_name(raw) is None
    assert location_line(raw) is None


def test_an_event_with_neither_room_nor_blurb_has_no_description(feed):
    feed([_raw("Bare", "uid-x", "None", "20260925T190000", "20260925T230000Z", "CAL-bare")])
    events = run(DukeBedeworkScraper("duke-university", {"catch_all": True}).scrape())
    assert events[0].description is None
