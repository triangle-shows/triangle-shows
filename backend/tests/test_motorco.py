"""
Unit tests for app.scrapers.motorco — the FullCalendar JS extraction.

Covers the title field specifically: WordPress `esc_js` backslash-escapes any
apostrophe inside the single-quoted `title:` value, and the previous
stop-at-the-first-quote pattern terminated there, storing a truncated title with
a trailing backslash. A fetch of the live calendar showed 61 of 625 titles
carrying such an escape.

Run from the backend/ directory:  python -m pytest tests/test_motorco.py
The extraction is pure stdlib regex, so no DB, no network and no fixtures are
needed — the scraper's own HTTP call is not exercised here.
"""

import sys
from datetime import date, timedelta
from pathlib import Path

# Make `app` importable when running pytest from backend/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.scrapers.motorco import _EVENT_PATTERN, MotorcoScraper  # noqa: E402


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
