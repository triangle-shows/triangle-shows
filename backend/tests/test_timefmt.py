"""Tests for format_time_12h, the shared 12-hour clock renderer.

Motivated by a real crash rather than by tidiness. ``GET /feeds/events.ics`` answered 500
on any Windows machine, because ``feeds.py`` formatted times with ``strftime('%-I:%M %p')``
and ``%-I`` is a glibc extension the Windows C runtime rejects with ``ValueError: Invalid
format string``. It worked on Cloud Run and in CI, so nothing caught it — and no test
rendered a feed at all, which is why the platform gap went unnoticed.

The parity class below is the point of the file: ``app.api.events`` was already producing
this format by a different route, and that route is a public API contract. The helper had
to match it exactly, not merely closely.
"""

from datetime import time

import pytest

from app.timefmt import format_time_12h


class TestFormatTime12h:
    @pytest.mark.parametrize("value,expected", [
        (time(0, 0), "12:00 AM"),      # midnight is 12 AM, not 0 AM
        (time(0, 30), "12:30 AM"),
        (time(1, 5), "1:05 AM"),       # no leading zero on the hour...
        (time(9, 5), "9:05 AM"),       # ...but minutes keep theirs
        (time(11, 59), "11:59 AM"),
        (time(12, 0), "12:00 PM"),     # noon is 12 PM, and PM not AM
        (time(12, 1), "12:01 PM"),
        (time(13, 45), "1:45 PM"),
        (time(19, 0), "7:00 PM"),      # a typical doors time
        (time(22, 30), "10:30 PM"),    # a typical late show
        (time(23, 59), "11:59 PM"),
    ])
    def test_formats(self, value, expected):
        assert format_time_12h(value) == expected

    def test_none_passes_through(self):
        """doors_time and show_time are both nullable and often absent, so every call site
        would otherwise need the same guard."""
        assert format_time_12h(None) is None

    def test_seconds_are_ignored(self):
        assert format_time_12h(time(20, 15, 42)) == "8:15 PM"

    def test_no_platform_specific_directive_is_used(self):
        """The regression guard. `%-I` is a glibc extension: it works on Linux, and raises
        ValueError on Windows. Reintroducing it would 500 the iCal feed on every Windows
        dev machine while CI stayed green, which is exactly how it survived the first time.
        """
        import inspect

        from app import timefmt

        source = inspect.getsource(timefmt.format_time_12h)
        assert "strftime" not in source, (
            "format_time_12h should build the string from the integer fields; strftime "
            "reintroduces both the platform and the locale dependency"
        )


class TestParityWithThePreviousApiOutput:
    """`/api/events` already emitted this format via `strftime('%I:%M %p').lstrip('0')`.

    That is a public response field, so the shared helper has to reproduce it exactly —
    a formatting change here would silently alter the API for every consumer. Asserted
    against the old expression rather than against hand-written strings, so this compares
    the two implementations rather than my reading of one of them.
    """

    @pytest.mark.parametrize("value", [
        time(0, 0), time(0, 30), time(1, 5), time(9, 5), time(11, 59),
        time(12, 0), time(12, 1), time(13, 45), time(19, 0), time(22, 30), time(23, 59),
    ])
    def test_matches_the_expression_it_replaced(self, value):
        previous = value.strftime("%I:%M %p").lstrip("0")
        assert format_time_12h(value) == previous

    def test_the_locale_dependency_is_gone(self):
        """The one deliberate difference from the old expression: `%p` is locale-dependent,
        so a server with a non-English locale could have changed the output of a public API
        and a calendar feed. The meridiem is now fixed."""
        assert format_time_12h(time(9, 0)).endswith("AM")
        assert format_time_12h(time(21, 0)).endswith("PM")
