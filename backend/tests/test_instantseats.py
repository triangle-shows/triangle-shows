"""
Unit tests for app.scrapers.instantseats — the clickgobuynow list-view parser.

Three things about this source need holding in place, and none of them is visible from
the happy path:

The listing states no year. It does state a weekday, and that pins the year exactly,
because a given month and day falls on a different weekday in consecutive years. The
tests below fix `today` rather than using the real one, so they keep meaning after the
calendar rolls over.

A sold-out show loses its "Buy Tickets" anchor, and with it the only link the obvious
implementation would have read the event ID from. The "Event Info" anchor survives, and
the ID has to come from whichever is present — otherwise a show that sells out changes
identity mid-season and re-inserts as a second row.

An early and a late set are two listings sharing a date and a title, so they collide on
ScrapedEvent.hash and one of them is dropped before it ever reaches the database.

Run from the backend/ directory:  pytest tests/test_instantseats.py
Pure parsing — no DB, no network. The scraper's own HTTP call is not exercised.
"""

from datetime import date, time

from bs4 import BeautifulSoup

from app.scrapers.instantseats import _WEEKDAY_LINE, InstantSeatsScraper

# A Monday, and the date the fixtures below are written against.
TODAY = date(2026, 9, 21)

BUY = "https://www.instantseats.com/index.cfm?fuseaction=buy.event&amp;eventID={eid}"
INFO = "https://www.instantseats.com/index.cfm?fuseaction=home.event&eventID={eid}"
PAGE = "https://clickgobuynow.com/durham/"


def _scraper() -> InstantSeatsScraper:
    return InstantSeatsScraper("sharp-nine", {"url": PAGE})


def _row(
    title="Ernest Turner Trio",
    month="Oct",
    day="6",
    price="$15 - $30",
    when="Tuesday 10/6",
    at="7:00 pm",
    eid="7338BDA6-F4C3-FFE7-A3A2ABC9C9235B8B",
    sold_out=False,
    image="https://www.instantseats.com//photos/520/1691673935253_mobileLarge.jpg",
    date_box=True,
) -> str:
    """One event row, shaped like the live list view."""
    date_html = (
        f'<div class="event-date"><p class="event-month">{month}</p>'
        f'<p class="event-day">{day}</p></div>'
        if date_box
        else ""
    )
    img_html = f'<div class="photofit"><img src="{image}" alt="Image" /></div>' if image else ""
    # Sold out replaces the Buy Tickets anchor with a badge. Event Info stays either way.
    buy_html = (
        '<span class="alert button">SOLD OUT</span>'
        if sold_out
        else f'<a href="{BUY.format(eid=eid)}" class="button">Buy Tickets</a>'
    )
    details = "<br>".join(part for part in (price, when, at) if part)
    return f"""
    <div class="row">
      <div class="small-12-centered columns">
        <div class="small-1 columns show-for-medium">{date_html}</div>
        <div class="small-11 medium columns">
          {img_html}
          <h6 class="title">{title}</h6>
          <div class="details">
            <p>{details}</p>
            <div class="button-group">
              {buy_html}
              <a class="secondary button" href="{INFO.format(eid=eid)}">Event Info</a>
            </div>
          </div>
        </div>
      </div>
    </div>
    """


def _parse(html: str, today: date = TODAY):
    """Run the row parser over every event in a fragment, as scrape() does."""
    soup = BeautifulSoup(f"<div>{html}</div>", "lxml")
    scraper = _scraper()
    out = []
    for title_el in soup.select("h6.title"):
        row = title_el.find_parent("div", class_="row")
        parsed = scraper._parse_row(row, title_el, PAGE, today)
        if parsed:
            out.append(parsed)
    return out


def _parse_one(html: str, today: date = TODAY):
    events = _parse(html, today)
    assert len(events) == 1, f"expected one event, got {len(events)}"
    return events[0]


class TestFieldExtraction:
    def test_reads_every_field_from_a_row(self):
        ev = _parse_one(_row())
        assert ev.name == "Ernest Turner Trio"
        assert ev.artist == "Ernest Turner Trio"
        assert ev.date == date(2026, 10, 6)
        assert ev.show_time == time(19, 0)
        assert (ev.price_min, ev.price_max) == (15.0, 30.0)
        assert ev.venue_slug == "sharp-nine"
        assert ev.source == "instantseats"
        assert ev.external_id == "7338BDA6-F4C3-FFE7-A3A2ABC9C9235B8B"
        assert ev.status == "on_sale"
        assert ev.image_url.endswith("1691673935253_mobileLarge.jpg")
        assert "buy.event" in ev.ticket_url
        assert "home.event" in ev.source_url

    def test_a_single_price_becomes_both_ends_of_the_range(self):
        ev = _parse_one(_row(price="$40"))
        assert (ev.price_min, ev.price_max) == (40.0, 40.0)

    def test_an_afternoon_set_keeps_its_time(self):
        ev = _parse_one(_row(when="Sunday 10/11", month="Oct", day="11", at="3:00 pm"))
        assert ev.show_time == time(15, 0)

    def test_details_are_read_by_shape_not_by_position(self):
        """A listing with no price must not shift the date and time lines up by one."""
        ev = _parse_one(_row(price=""))
        assert ev.date == date(2026, 10, 6)
        assert ev.show_time == time(19, 0)
        assert ev.price_min is None

    def test_a_row_without_a_date_box_is_skipped(self):
        assert _parse(_row(date_box=False)) == []

    def test_a_row_without_an_image_still_parses(self):
        ev = _parse_one(_row(image=""))
        assert ev.image_url is None
        assert ev.name == "Ernest Turner Trio"


class TestSoldOut:
    def test_sold_out_sets_the_status(self):
        ev = _parse_one(_row(sold_out=True))
        assert ev.status == "sold_out"

    def test_the_event_id_survives_losing_the_buy_anchor(self):
        """The whole reason the ID is read from either anchor.

        A sold-out show keeps no Buy Tickets link. If the ID came only from there it
        would go None, plan_upsert would fall back to matching on the title hash, and
        this venue's repeated artist names make that match the wrong row.
        """
        ev = _parse_one(_row(sold_out=True, eid="4823BA51-98EE-6FC7-2CED420D599254B3"))
        assert ev.external_id == "4823BA51-98EE-6FC7-2CED420D599254B3"

    def test_a_sold_out_show_still_links_somewhere(self):
        """frontend/js/modal.js renders its button from ticket_url alone, so without the
        fallback a sold-out show would show a Sold Out badge and no way through to the
        venue at all."""
        ev = _parse_one(_row(sold_out=True))
        assert ev.ticket_url is not None
        assert "home.event" in ev.ticket_url


class TestYearInference:
    def test_uses_the_weekday_to_pick_the_year(self):
        """24 September is a Thursday in 2026 and a Friday in 2027."""
        ev = _parse_one(_row(month="Sep", day="24", when="Thursday 9/24"))
        assert ev.date == date(2026, 9, 24)

    def test_rolls_into_next_year_for_a_january_listing(self):
        """Scraped in December, a January date belongs to the year after.

        15 January is a Thursday in 2026 and a Friday in 2027, so the weekday alone
        settles it — no date-distance rule required.
        """
        ev = _parse_one(
            _row(month="Jan", day="15", when="Friday 1/15"), today=date(2026, 12, 20)
        )
        assert ev.date == date(2027, 1, 15)

    def test_falls_back_to_the_date_rule_when_no_weekday_matches(self):
        """A wrong weekday must not drop a real event."""
        ev = _parse_one(_row(month="Oct", day="6", when="Monday 10/6"))
        assert ev.date == date(2026, 10, 6)

    def test_ignores_a_weekday_written_against_a_different_date(self):
        """The details line repeats the date box. When the two disagree the weekday is
        not evidence about the date box's date, so it is discarded rather than used to
        pin the year off the wrong day."""
        ev = _parse_one(_row(month="Oct", day="6", when="Friday 10/16"))
        assert ev.date == date(2026, 10, 6)

    def test_a_date_just_past_stays_in_the_current_year(self):
        ev = _parse_one(_row(month="Sep", day="18", when="", at="7:00 pm"))
        assert ev.date == date(2026, 9, 18)

    def test_a_date_long_past_reads_as_next_year(self):
        ev = _parse_one(_row(month="Feb", day="20", when="", at="7:00 pm"))
        assert ev.date == date(2027, 2, 20)

    def test_29_february_resolves_to_the_leap_year(self):
        """2027 is not a leap year, so the only candidate is 2028."""
        m = _WEEKDAY_LINE.search("Tuesday 2/29")
        assert InstantSeatsScraper._parse_date("Feb", "29", m, date(2027, 6, 1)) == date(
            2028, 2, 29
        )


class TestSameNightSets:
    """A jazz club plays an early and a late set: two listings, one date, one title."""

    _EARLY = dict(title="Pasquale Grasso", month="Oct", day="16", when="Friday 10/16",
                  at="7:00 pm", price="$40", eid="4823BA51-98EE-6FC7-2CED420D599254B3",
                  sold_out=True)
    _LATE = dict(title="Pasquale Grasso", month="Oct", day="16", when="Friday 10/16",
                 at="9:00 pm", price="$40", eid="4590885C-B6AA-0FC2-09AE8FE811C29EC5")

    def _both(self):
        events = _parse(_row(**self._EARLY) + _row(**self._LATE))
        assert len(events) == 2
        return InstantSeatsScraper._disambiguate_same_night_sets(events)

    def test_both_sets_survive_with_distinct_hashes(self):
        """Without the rename they hash alike, and dedupe_scraped in the manager drops
        the second before the database ever sees it."""
        early, late = self._both()
        assert early.hash != late.hash

    def test_the_titles_say_which_set_they_are(self):
        early, late = self._both()
        assert early.name == "Pasquale Grasso (7:00 pm)"
        assert late.name == "Pasquale Grasso (9:00 pm)"

    def test_each_set_keeps_its_own_ticket_identity(self):
        early, late = self._both()
        assert early.external_id != late.external_id
        assert early.status == "sold_out"
        assert late.status == "on_sale"

    def test_a_lone_show_keeps_the_title_the_venue_gave_it(self):
        events = InstantSeatsScraper._disambiguate_same_night_sets(_parse(_row()))
        assert events[0].name == "Ernest Turner Trio"

    def test_two_shows_at_the_same_stated_time_are_left_alone(self):
        """Appending an identical time to both would not separate them, so the rename is
        skipped and the manager's dedup handles it rather than the titles gaining a
        suffix that distinguishes nothing."""
        same = dict(self._LATE)
        same["at"] = "7:00 pm"
        events = _parse(_row(**self._EARLY) + _row(**same))
        out = InstantSeatsScraper._disambiguate_same_night_sets(events)
        assert [ev.name for ev in out] == ["Pasquale Grasso", "Pasquale Grasso"]

    def test_same_title_on_different_nights_is_not_touched(self):
        """A residency is not a double bill."""
        first = dict(title="Sean Mason Trio", month="Oct", day="30", when="Friday 10/30",
                     at="7:00 pm", eid="6D1433B9-F727-939E-5A975BC1AB6F0043")
        second = dict(title="Sean Mason Trio", month="Oct", day="31", when="Saturday 10/31",
                      at="7:00 pm", eid="6D1E84BB-D6A4-A589-A0D21AEC70330A83")
        out = InstantSeatsScraper._disambiguate_same_night_sets(
            _parse(_row(**first) + _row(**second))
        )
        assert [ev.name for ev in out] == ["Sean Mason Trio", "Sean Mason Trio"]
        assert out[0].hash != out[1].hash
