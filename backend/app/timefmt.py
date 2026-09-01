"""Render a time as a readable 12-hour clock, identically on every platform.

Role: shared by the JSON event API (app.api.events) and the iCal feed (app.api.feeds),
the two places a stored time is shown to a person.

Why this is not a strftime call. The obvious spelling, ``strftime('%-I:%M %p')``, uses
``%-I`` for "hour with no leading zero" — a glibc extension. The Windows C runtime does
not implement it and raises ``ValueError: Invalid format string``, so
``GET /feeds/events.ics`` answered 500 on any Windows machine while working on Cloud Run
and in CI. A local developer could not exercise the feed at all, and nothing in the test
suite covered it because the tests never rendered one.

``strftime('%I:%M %p').lstrip('0')`` is the portable workaround, and app.api.events was
already using it. It has a subtler problem: ``%p`` is locale-dependent, so the output of a
public JSON API and a calendar feed would depend on the server's locale. Formatting from
the integer fields sidesteps both — no platform-specific directives, no locale, and the
midnight and noon cases are stated outright rather than inherited.

Requires: nothing outside the standard library.
"""

# --- Imports ---

from datetime import time
from typing import Optional

# --- Formatting ---


def format_time_12h(value: Optional[time]) -> Optional[str]:
    """Format `value` as ``h:mm AM/PM``, or None for None.

    Passes None through so call sites can format an optional column without guarding
    first — doors_time and show_time are both nullable and frequently absent.

    Midnight is 12 AM and noon is 12 PM, which is what the ``% 12 or 12`` does: hour 0 and
    hour 12 both map to a displayed 12, and the meridiem comes from the original 24-hour
    value rather than the displayed one.
    """
    if value is None:
        return None
    hour = value.hour % 12 or 12
    meridiem = "AM" if value.hour < 12 else "PM"
    return f"{hour}:{value.minute:02d} {meridiem}"
