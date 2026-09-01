"""
Unit tests for app.scrapers.motorco — the FullCalendar JS extraction.

Covers the title field specifically: WordPress `esc_js` backslash-escapes any
apostrophe inside the single-quoted `title:` value, and the previous
stop-at-the-first-quote pattern terminated there, storing a truncated title with
a trailing backslash. A fetch of the live calendar showed 61 of 625 titles
carrying such an escape.

Also covers the per-event `backgroundImage` key, which the extraction reads out
of the same `{...}` block as the title/start/url it sits alongside.

Run from the backend/ directory:  pytest tests/test_motorco.py
The extraction is pure stdlib regex, so no DB, no network and no fixtures are
needed — the scraper's own HTTP call is not exercised here.
"""

from datetime import date, timedelta

from app.scrapers.motorco import _EVENT_PATTERN, MotorcoScraper


def _scraper() -> MotorcoScraper:
    return MotorcoScraper("motorco", {"url": "https://motorcomusic.com/calendar/"})


def _event_object(title: str, day: str) -> str:
    """One JS event object, shaped like the live FullCalendar init array."""
    return (
        "{"
        f"title: '{title}',"
        f"start: '{day} 20:00',"
        "url: 'https://motorcomusic.com/event/example/',"
        "classNames: 'tcec-event-'"
        "}"
    )


# `\\'` in this Python string is one backslash + one apostrophe in the JS source —
# exactly what `esc_js` emits for a title containing an apostrophe.
_ESCAPED = "Wednesday : It\\'s Fine"
_PLAIN = "Hermanos Gutierrez"


def test_escaped_apostrophe_title_is_captured_whole():
    """The old pattern stopped at the escaped quote, yielding 'Wednesday : It\\'."""
    m = _EVENT_PATTERN.search(_event_object(_ESCAPED, "2026-09-12"))
    assert m is not None
    assert m.group("title") == _ESCAPED


def test_plain_title_still_captured():
    m = _EVENT_PATTERN.search(_event_object(_PLAIN, "2026-09-12"))
    assert m is not None
    assert m.group("title") == _PLAIN


def test_start_and_url_still_captured():
    m = _EVENT_PATTERN.search(_event_object(_ESCAPED, "2026-09-12"))
    assert m is not None
    assert m.group("start") == "2026-09-12 20:00"
    assert m.group("url") == "https://motorcomusic.com/event/example/"


def test_both_titles_survive_in_a_mixed_array():
    """Events were never dropped — each escaped title was silently truncated."""
    html = "events: [%s,%s]," % (
        _event_object(_ESCAPED, "2026-09-12"),
        _event_object(_PLAIN, "2026-09-20"),
    )
    titles = [m.group("title") for m in _EVENT_PATTERN.finditer(html)]
    assert titles == [_ESCAPED, _PLAIN]


def test_parse_event_collapses_the_escape_to_a_real_apostrophe():
    future = (date.today() + timedelta(days=45)).strftime("%Y-%m-%d %H:%M")
    parsed = _scraper()._parse_event(
        _ESCAPED, future, "https://motorcomusic.com/event/example/", date.today()
    )
    assert parsed is not None
    assert parsed.name == "Wednesday : It's Fine"
    assert parsed.artist == "Wednesday : It's Fine"
    assert "\\" not in parsed.name


def test_parse_event_leaves_a_plain_title_alone():
    future = (date.today() + timedelta(days=45)).strftime("%Y-%m-%d %H:%M")
    parsed = _scraper()._parse_event(
        _PLAIN, future, "https://motorcomusic.com/event/example/", date.today()
    )
    assert parsed is not None
    assert parsed.name == _PLAIN


# --- The per-event poster ---
# Each event object also carries a `backgroundImage` key holding the poster the
# calendar tile paints behind the show. A fetch of the live calendar found it on
# all 640 event objects on the page, but a WordPress admin can always post an
# event with no featured image, so the absent case has to degrade rather than
# drop the event.

_POSTER = "https://motorcomusic.com/wp-content/uploads/2026/07/example.jpg"
_OTHER_POSTER = "https://motorcomusic.com/wp-content/uploads/2026/07/other.jpg"


def _event_object_with_poster(title: str, day: str, poster: str) -> str:
    """One JS event object carrying `end` and `backgroundImage`, as the live page does."""
    return (
        "{"
        f"title: '{title}',"
        f"start: '{day} 20:00',"
        f"end: '{day} 21:00',"
        "url: 'https://motorcomusic.com/event/example/',"
        "classNames: 'tcec-event-',"
        f"backgroundImage: '{poster}'"
        "}"
    )


def _future_day(offset: int = 45) -> str:
    """The scraper drops past events, so fixtures must be anchored ahead of today."""
    return (date.today() + timedelta(days=offset)).isoformat()


def test_backgroundimage_becomes_the_event_image():
    day = _future_day()
    js = "events: [%s]," % _event_object_with_poster(_PLAIN, day, _POSTER)
    events = _scraper()._extract_events(js, date.today())
    assert len(events) == 1
    assert events[0].image_url == _POSTER


def test_an_event_object_without_backgroundimage_is_kept_with_no_image():
    day = _future_day()
    js = "events: [%s]," % _event_object(_PLAIN, day)
    events = _scraper()._extract_events(js, date.today())
    assert len(events) == 1
    assert events[0].image_url is None


def test_a_poster_is_never_borrowed_from_a_neighbouring_event_object():
    """Each poster must come from its own `{...}` block, not the next one along."""
    js = "events: [%s,%s]," % (
        _event_object(_ESCAPED, _future_day(45)),
        _event_object_with_poster(_PLAIN, _future_day(46), _POSTER),
    )
    events = _scraper()._extract_events(js, date.today())
    by_name = {e.name: e for e in events}
    assert by_name["Wednesday : It's Fine"].image_url is None
    assert by_name[_PLAIN].image_url == _POSTER


def test_each_event_keeps_its_own_poster_across_a_mixed_array():
    js = "events: [%s,%s]," % (
        _event_object_with_poster(_PLAIN, _future_day(45), _POSTER),
        _event_object_with_poster(_ESCAPED, _future_day(46), _OTHER_POSTER),
    )
    events = _scraper()._extract_events(js, date.today())
    by_name = {e.name: e for e in events}
    assert by_name[_PLAIN].image_url == _POSTER
    assert by_name["Wednesday : It's Fine"].image_url == _OTHER_POSTER


def test_the_other_fields_survive_alongside_a_poster():
    day = _future_day()
    js = "events: [%s]," % _event_object_with_poster(_ESCAPED, day, _POSTER)
    events = _scraper()._extract_events(js, date.today())
    assert len(events) == 1
    event = events[0]
    assert event.name == "Wednesday : It's Fine"
    assert event.artist == "Wednesday : It's Fine"
    assert event.date.isoformat() == day
    assert event.show_time is not None
    assert (event.show_time.hour, event.show_time.minute) == (20, 0)
    assert event.ticket_url == "https://motorcomusic.com/event/example/"
    assert event.source_url == "https://motorcomusic.com/event/example/"
