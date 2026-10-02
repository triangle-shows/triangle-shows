"""
Unit tests for app.scrapers.rhp_events — the year a listing's date falls in.

The RHP listing pages (Cat's Cradle, Lincoln Theatre, Local 506) render each date as
"Fri, Jan 08": no year, and no datetime attribute to fall back on. The scraper used to
assume the current year, which filed every show from January onward a year early — in
the past, where the calendar never showed it. Measured 2026-10-02, the four RHP venues
held no 2027 events at all, while "Dinosaur Jr. – Spring Tour 2027" sat on 2026-02-18.

The weekday is what fixes it: a given month and day falls on a different weekday in any
two consecutive years, so it pins the year exactly. The tests fix `today` rather than
using the real one, so they keep meaning after the calendar rolls over.

Run from the backend/ directory:  pytest tests/test_rhp_events.py
Pure parsing — no DB, no network. The scraper's own HTTP call is not exercised.
"""

from datetime import date

from bs4 import BeautifulSoup

from app.scrapers.rhp_events import RHPEventsScraper

# The date the live Cat's Cradle listing above was read on.
TODAY = date(2026, 10, 2)

parse = RHPEventsScraper._parse_date_text


def _card(date_text: str, datetime_attr: str = "") -> str:
    """One listing card, shaped like the live Cat's Cradle markup."""
    attr = f' datetime="{datetime_attr}"' if datetime_attr else ""
    return f"""
    <div class="rhp-event">
      <div class="eventDateListTop rhp-event__date--list">
        <div id="eventDate" class="mb-0 eventMonth singleEventDate text-uppercase"{attr}>
          {date_text}</div>
      </div>
      <a class="url" href="https://catscradle.com/event/emo-night/">
        <h2 class="rhp-event__title--list">Crank It Loud Presents Emo Night Karaoke</h2>
      </a>
      <div class="rhpVenueContent">Cat's Cradle</div>
    </div>
    """


def _parse_one(html: str, today: date = TODAY):
    scraper = RHPEventsScraper("cats-cradle", {"url": "https://catscradle.com/events/"})
    wrapper = BeautifulSoup(html, "lxml").select_one(".rhp-event")
    return scraper._parse_event(wrapper, "Cat's Cradle", today)


class TestYearInference:
    def test_a_january_show_lands_in_next_year(self):
        """The case that was broken: read in October, "Fri, Jan 08" is 2027."""
        ev = _parse_one(_card("Fri, Jan 08"))
        assert ev is not None
        assert ev.date == date(2027, 1, 8)

    def test_a_show_later_this_year_stays_in_this_year(self):
        """28 October is a Wednesday in 2026."""
        assert parse("Wed, Oct 28", TODAY) == date(2026, 10, 28)

    def test_the_weekday_overrides_the_date_distance_rule(self):
        """5 March is a Thursday in 2026 and a Friday in 2027. Read just after it, the
        week-past rule alone would keep 2026; the weekday says the listing means 2027."""
        assert parse("Fri, Mar 05", date(2026, 3, 6)) == date(2027, 3, 5)

    def test_a_wrong_weekday_falls_back_to_the_date_rule(self):
        """A mistyped weekday must not drop a real event."""
        assert parse("Mon, Jan 08", TODAY) == date(2027, 1, 8)

    def test_without_a_weekday_a_long_past_date_reads_as_next_year(self):
        assert parse("Feb 18", TODAY) == date(2027, 2, 18)

    def test_without_a_weekday_a_date_just_past_stays_this_year(self):
        assert parse("Sep 28", TODAY) == date(2026, 9, 28)

    def test_full_month_and_weekday_names(self):
        assert parse("Friday, January 8", TODAY) == date(2027, 1, 8)

    def test_29_february_resolves_to_the_leap_year(self):
        """strptime's default year is 1900, a common year, so the old parse dropped this
        date outright. 2028 is the only candidate leap year."""
        assert parse("Tue, Feb 29", date(2027, 6, 1)) == date(2028, 2, 29)


class TestExplicitDates:
    def test_a_stated_year_is_taken_as_given(self):
        """Inference applies only to year-less text; a stated year is never moved."""
        assert parse("Fri, Jan 8, 2027", TODAY) == date(2027, 1, 8)
        assert parse("January 15, 2025", TODAY) == date(2025, 1, 15)

    def test_a_datetime_attribute_wins_over_the_text(self):
        ev = _parse_one(_card("Fri, Jan 08", datetime_attr="2027-01-08T20:00:00"))
        assert ev.date == date(2027, 1, 8)

    def test_unparseable_text_skips_the_card(self):
        assert _parse_one(_card("TBA")) is None
