"""Pick a calendar colour for a venue an admin creates by hand.

Role: called by the admin venue-create endpoint. Pure functions, no database and no ORM
imports, so the choice is testable without Postgres.

The problem. Seeded venues have hand-chosen colours (app/seed.py) that are spread around
the wheel so adjacent venues on a calendar are distinguishable. A venue added at runtime
has no such curation, and the Event model's column default is a single indigo — so every
promoter an admin ever adds would share one colour and be mutually indistinguishable, and
would collide with whichever seeded venue is nearest that hue.

The approach. Put the new venue in the *largest gap* between the hues already in use. That
is the choice that maximises the distance to its nearest neighbour, which is exactly the
property "tell these apart on a calendar" needs. It degrades gracefully: with many venues
the gaps shrink, but the new colour is still as far from everything as any colour can be.

Deliberately not random. A random hue lands next to an existing one about as often as not,
and the failure is silent — two venues that look identical on a busy month. Deliberately
not sequential either, since deleting a venue would then shift every later assignment.

frontend/js/design.js separately clamps whatever colour arrives for contrast (it enforces
>= 6.5:1 against white and brightens for dark mode), so this only has to solve *distinctness*.
Saturation and lightness are therefore fixed, and only the hue varies: two colours at the
same S and L differing only in hue are as far apart as the eye is going to get.

Requires: nothing outside the standard library.
"""

# --- Imports ---

import colorsys
import hashlib
import re
from typing import Iterable, Optional

# --- Constants ---

# Fixed saturation and lightness, chosen to sit in the same family as the seeded palette
# (see app/seed.py) rather than to be maximally vivid. design.js will adjust for contrast,
# so these only need to be in the right neighbourhood.
_SATURATION = 0.45
_LIGHTNESS = 0.58

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


# --- Hex <-> hue ---


def is_valid_hex(value: str) -> bool:
    """Whether `value` is a hex colour this codebase can store.

    Venue.color is String(7), so a longer string would be silently truncated by some
    drivers and rejected by others; either way an admin's typo should be a clear error
    rather than a corrupted column.
    """
    return bool(value) and bool(_HEX_RE.match(value.strip()))


def _expand(hex_color: str) -> str:
    """Normalize to a 6-digit lowercase hex string with a leading '#'."""
    value = hex_color.strip().lstrip("#").lower()
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    return f"#{value}"


def hue_of(hex_color: str) -> Optional[float]:
    """The hue of `hex_color` in degrees (0-360), or None if it has none.

    Greys, black and white have no meaningful hue — colorsys reports 0 for all of them,
    which is red. Returning None instead keeps them out of the gap calculation rather than
    letting a grey venue pretend to occupy the red part of the wheel.
    """
    if not is_valid_hex(hex_color):
        return None
    value = _expand(hex_color)
    r, g, b = (int(value[i:i + 2], 16) / 255 for i in (1, 3, 5))
    if r == g == b:
        return None
    hue, _lightness, saturation = colorsys.rgb_to_hls(r, g, b)
    if saturation == 0:
        return None
    return hue * 360


def hex_from_hue(hue: float) -> str:
    """A hex colour at `hue` degrees, at the fixed saturation and lightness."""
    r, g, b = colorsys.hls_to_rgb((hue % 360) / 360, _LIGHTNESS, _SATURATION)
    return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))


# --- Assignment ---


def largest_hue_gap_midpoint(hues: Iterable[float]) -> float:
    """The hue furthest from every hue in `hues`.

    Walks the sorted hues as a circle, including the wrap-around gap from the last back to
    the first — without that the widest gap is invisible whenever it straddles 0 degrees,
    which is common because reds are well represented in the seeded palette.

    With no hues at all, returns 210 (a mid blue) rather than 0: an arbitrary starting
    point still has to look deliberate on the calendar, and red reads as an alert.
    """
    ordered = sorted(h % 360 for h in hues)
    if not ordered:
        return 210.0
    if len(ordered) == 1:
        return (ordered[0] + 180) % 360

    widest_start, widest_size = ordered[-1], (ordered[0] + 360) - ordered[-1]
    for first, second in zip(ordered, ordered[1:]):
        if second - first > widest_size:
            widest_start, widest_size = first, second - first
    return (widest_start + widest_size / 2) % 360


def pick_venue_color(existing_colors: Iterable[str], *, name: str = "") -> str:
    """Choose a colour for a new venue, given the colours already in use.

    `name` is only a fallback seed. When every existing colour is unusable — all greys, or
    a database whose colours were all left at the default — there is no gap structure to
    reason about, so the hue comes from a hash of the name instead. That is stable (the
    same venue always gets the same colour, so re-creating one does not reshuffle the
    calendar) and spread (different names land in different places), which is the best
    available when there is nothing to space away from.
    """
    hues = [h for h in (hue_of(c) for c in existing_colors) if h is not None]
    if hues:
        return hex_from_hue(largest_hue_gap_midpoint(hues))
    digest = hashlib.sha256((name or "venue").encode("utf-8")).digest()
    return hex_from_hue((digest[0] / 255) * 360)
