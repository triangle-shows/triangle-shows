"""
Live-music classifier — decides whether an event is live music or something else
(karaoke, trivia, theme nights, comedy, film screenings, ...). This module is the
single source of truth for the detection criteria used to declutter the public
calendar.

Role: Called by ScrapeManager.reclassify_all() after every scrape to (re)compute
each event's `is_live_music` flag and a human-readable `classification_reason`.
Events an admin has manually overridden (is_manual_override=True) are skipped by
the caller, so nothing here can clobber a manual decision. Pure Python — no DB or
network access — so it is cheap to run and trivial to unit-test.

Detection criteria, in priority order (highest wins):
  1. Recurrence — a name that repeats on >= RECURRENCE_THRESHOLD separate occasions
     at the same venue is treated as a recurring series (weekly karaoke, trivia, DJ
     nights, etc.) and flagged non-live. This is the strongest signal per the
     product decision, so it wins over keywords and genre. Dates within
     RUN_GAP_DAYS of each other count as one occasion, so a multi-night residency
     stays live music instead of reading as a series.
  2. Keywords — the event name matches a known non-music keyword, OR a
     "<word> night" phrase (any word except days-of-week / NIGHT_EXCEPTIONS, so
     "Emo Night" and "Hyperpop Night" match but "Saturday Night" does not), OR a
     "<theme> party" phrase whose leading word is in the PARTY_THEMES allowlist
     (see PARTY_THEMES for why "party" is allowlisted rather than matched bare).
  3. Genre — where the source provides one (currently only the Ticketmaster
     scraper), a non-music genre (Comedy, Theatre, Film, Sports, ...) flags it.
Anything matching none of these is assumed to be live music.

To tune detection, edit the lists below — they are intentionally readable and are
surfaced verbatim to the admin UI via criteria_summary().
"""

# --- Imports ---
import re
from collections import defaultdict
from datetime import date, timedelta
from typing import Iterable, Optional


# --- Tunable criteria ---

# An event whose (venue, name) repeats on at least this many separate occasions at
# the same venue is treated as a recurring, non-live series.
RECURRENCE_THRESHOLD = 3

# Dates within this many days of each other count as ONE occasion, not several.
#
# Without this, recurrence cannot tell a weekly karaoke night from a band booked for
# a three-night run — both produce three distinct dates at one venue. A residency is
# exactly what someone opens the calendar to find, so counting raw dates hid the
# site's best listings. Grouping nearby dates collapses a run to a single occasion
# while leaving a weekly series at one occasion per week.
#
# 2 days keeps a Fri/Sat/Sun run together, and a Fri/Sun booking with Saturday off,
# while still splitting anything on a weekly-or-sparser cadence.
RUN_GAP_DAYS = 2

# How far into the past reclassification and admin moderation reach. The calendar can
# be scrolled back into recent past dates, and those events carry is_live_music too, so
# they need to respond to live/non-live changes rather than staying frozen at whatever
# they were last set to. Older events are settled and left alone.
#
# Note this also widens the recurrence window: instances just behind today now count
# toward RECURRENCE_THRESHOLD, so a series is recognised sooner than it would be from
# future dates alone.
RECLASSIFY_PAST_DAYS = 30


def reclassify_floor(today: Optional[date] = None) -> date:
    """Earliest event date that reclassification and admin moderation still touch."""
    return (today or date.today()) - timedelta(days=RECLASSIFY_PAST_DAYS)

# Venues assumed to be entirely live music — exempt from non-live flagging.
# DPAC hosts touring concerts/Broadway/comedy, but per product decision it is
# treated as all live music and excluded from the filter pass entirely.
ALWAYS_LIVE_VENUE_SLUGS = {"dpac"}

# Whole-word matches anywhere in the event name flag it as non-live music.
# Word-boundary matched, case-insensitive (so "class" does not match "classic").
# Note: "<theme> night" and "<theme> party" are handled by their own broader
# patterns below, not this list.
NON_MUSIC_KEYWORDS = [
    "karaoke",
    "trivia",
    "bingo",
    "quizzo",
    "quiz",
    "open mic",
    "open-mic",
    "open decks",
    "comedy",
    "stand-up",
    "standup",
    "improv",
    "drag",
    "burlesque",
    "silent disco",
    "screening",
    "film screening",
    "movie",
    "market",
    "pop-up",
    "popup",
    "yoga",
    "brunch",
    "book club",
    "poetry",
    "spoken word",
    "workshop",
    "meetup",
    "networking",
    "speed dating",
    "sing-along",
    "singalong",
]

# "<word> night" (or "nights") is treated as a themed DJ/dance/recurring night —
# a strong non-live signal — for ANY preceding word EXCEPT these. Days of the week
# and a few common phrases ("Last Night", "One Night Only", "A Hard Day's Night")
# are excluded so real show titles aren't caught. Single-letter matches (e.g. the
# "s" left by a possessive like "Day's Night") are ignored in classify_one().
NIGHT_EXCEPTIONS = {
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "last",
    "one",
    "good",
    "hard",
}

# "<theme> party" (or "parties") — listening/release/album parties, etc. This is an
# allowlist rather than "any word" because band names contain "party" (e.g. the
# band Bloc Party), so a bare "party" match would produce false positives.
PARTY_THEMES = [
    "listening",
    "release",
    "album",
    "single",
    "launch",
    "block",
    "dance",
    "watch",
    "viewing",
    "after",
    "silent",
]

# Substrings of a source-provided genre/subgenre that indicate non-music.
# Kept conservative: "Dance/Electronic" is a legitimate live-music genre, so
# "dance" is NOT listed here (only "dance party"/"dance night" via keywords).
NON_MUSIC_GENRES = [
    "comedy",
    "theatre",
    "theater",
    "film",
    "sports",
    "family",
    "magic",
    "illusion",
    "spoken word",
]


# --- Compiled matchers (built once at import) ---

_KEYWORD_PATTERNS = [
    (kw, re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE))
    for kw in NON_MUSIC_KEYWORDS
]

# Broad "<word> night(s)"; the word is filtered against NIGHT_EXCEPTIONS in code.
_NIGHT_PATTERN = re.compile(r"\b(\w+)\s+nights?\b", re.IGNORECASE)

# "<theme> party" / "<theme> parties" for an allowlisted set of themes.
_PARTY_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in PARTY_THEMES) + r")\s+part(?:y|ies)\b",
    re.IGNORECASE,
)


# --- Helpers ---

def _normalize_for_grouping(name: str) -> str:
    """Collapse a name to a comparison key for recurrence grouping.

    Lowercases and strips punctuation, whitespace, AND digits so that dated
    variants of the same series collapse together (e.g. "Trivia Night 7/8" and
    "Trivia Night 7/15" both become "trivianight").
    """
    return re.sub(r"[^a-z]", "", (name or "").lower())


def _count_occasions(dates: Iterable[date], gap_days: int = RUN_GAP_DAYS) -> int:
    """Collapse dates into separate occasions, merging any that form one run.

    Sorts the distinct dates and starts a new occasion whenever the gap from the
    previous date exceeds `gap_days`. A three-night residency returns 1; a weekly
    series returns one per week. See RUN_GAP_DAYS for why this matters.
    """
    ordered = sorted(set(dates))
    if not ordered:
        return 0
    occasions = 1
    for previous, current in zip(ordered, ordered[1:]):
        if (current - previous).days > gap_days:
            occasions += 1
    return occasions


def _match_non_music_genre(genre: Optional[str], subgenre: Optional[str]) -> Optional[str]:
    """Return the matched non-music genre token, or None."""
    haystack = " ".join(filter(None, [genre, subgenre])).lower()
    if not haystack:
        return None
    for token in NON_MUSIC_GENRES:
        if token in haystack:
            return token
    return None


# --- Public API ---

def normalize_series_name(name: str) -> str:
    """Public series key for a name — the value stored in SeriesOverride.normalized_name.

    Must stay identical to the grouping used by recurrence detection so an admin's
    series override matches the same events the detector groups together.
    """
    return _normalize_for_grouping(name)


def classify_one(
    name: str,
    genre: Optional[str] = None,
    subgenre: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """Classify a single event by name + genre (no recurrence context).

    Returns (is_live_music, reason). `reason` is None when the event is treated as
    live music; otherwise it is a short human-readable explanation.
    """
    text = name or ""

    for kw, pattern in _KEYWORD_PATTERNS:
        if pattern.search(text):
            return False, f"keyword: {kw}"

    party = _PARTY_PATTERN.search(text)
    if party:
        return False, f"keyword: {party.group(1).lower()} party"

    # "<word> night" for any word that isn't a day-of-week / excepted phrase.
    # finditer (not search) so a leading excepted match doesn't mask a later
    # real one, e.g. "Saturday Night: Emo Night".
    for m in _NIGHT_PATTERN.finditer(text):
        word = m.group(1).lower()
        if len(word) >= 2 and word not in NIGHT_EXCEPTIONS:
            return False, f"keyword: {word} night"

    genre_hit = _match_non_music_genre(genre, subgenre)
    if genre_hit:
        return False, f"genre: {genre_hit}"

    return True, None


def find_recurring_event_ids(
    events: Iterable,
    threshold: int = RECURRENCE_THRESHOLD,
) -> dict:
    """Identify events that belong to a recurring series.

    events: iterable of objects with `id`, `venue_id`, `name`, and `date`.
    Returns {event_id: date_count} for every event whose (venue, name) key recurs on
    at least `threshold` separate occasions.

    The threshold is applied to occasions, not raw dates — consecutive dates are one
    run, not a series (see RUN_GAP_DAYS). The returned count is still the number of
    distinct dates, because that is what the admin UI and classification_reason
    report and it is the more useful number to read.
    """
    dates_by_key: dict = defaultdict(set)
    ids_by_key: dict = defaultdict(list)
    for ev in events:
        key = (ev.venue_id, _normalize_for_grouping(ev.name))
        dates_by_key[key].add(ev.date)
        ids_by_key[key].append(ev.id)

    recurring: dict = {}
    for key, dates in dates_by_key.items():
        _venue_id, normalized_name = key

        # An empty key means the name normalized away entirely — an event with no
        # name, or one named only in digits and punctuation ("2/14", "$5"). Those are
        # unrelated events sharing an empty key, not a series, and a broken scraper
        # emits them in bunches: three at one venue was enough to flag all three as
        # recurring and hide them. See the malformed Squarespace records in #35.
        if not normalized_name:
            continue

        if _count_occasions(dates) < threshold:
            continue

        for event_id in ids_by_key[key]:
            recurring[event_id] = len(dates)
    return recurring


def classify_batch(
    events: Iterable,
    threshold: int = RECURRENCE_THRESHOLD,
) -> dict:
    """Classify a batch of events, applying recurrence + keyword + genre signals.

    events: iterable of objects with `id`, `venue_id`, `name`, `date`, and
    optionally `genre`/`subgenre`. Returns {event_id: (is_live_music, reason)}.
    """
    events = list(events)
    recurring = find_recurring_event_ids(events, threshold)

    results: dict = {}
    for ev in events:
        count = recurring.get(ev.id)
        if count is not None:
            results[ev.id] = (False, f"recurring: {count} dates")
            continue
        results[ev.id] = classify_one(
            ev.name,
            getattr(ev, "genre", None),
            getattr(ev, "subgenre", None),
        )
    return results


def classification_updates(
    events: Iterable,
    series_overrides: Optional[dict] = None,
    exempt_venue_ids: Optional[set] = None,
    threshold: int = RECURRENCE_THRESHOLD,
) -> dict:
    """Compute the classification values to persist, honoring all override layers.

    events: iterable of objects with the classify_batch attributes plus
    `is_manual_override`.
    series_overrides: optional {(venue_id, normalized_name): (is_live_music, note)}
    mapping — a series-level rule that overrides automatic classification for every
    event whose (venue, name) key matches.
    exempt_venue_ids: optional set of venue ids assumed to be entirely live music
    (see ALWAYS_LIVE_VENUE_SLUGS, e.g. DPAC) — forced live unless a series override says
    otherwise, so the exemption is a default rather than an absolute.

    Returns {event_id: (is_live_music, reason)} for every event EXCEPT those with
    is_manual_override=True, which are omitted so a re-scrape never overwrites a
    decision an admin made on a single instance.

    Precedence, most specific first:
      1. per-event manual override  -> omitted here (caller leaves the row untouched)
      2. series override            -> forces the series value
      3. always-live venue exemption -> forced live music
      4. automatic classification   -> recurrence / keyword / genre
    This is the pure decision function behind ScrapeManager.reclassify_all().
    """
    events = list(events)
    series_overrides = series_overrides or {}
    exempt_venue_ids = exempt_venue_ids or set()
    classified = classify_batch(events, threshold)

    updates: dict = {}
    for ev in events:
        if getattr(ev, "is_manual_override", False):
            continue  # per-event override wins; leave the hand-set value in place

        # Series override is consulted before the venue exemption, so the exemption acts
        # as a default rather than as an absolute. The other order made an exempt venue's
        # series impossible to mark non-live through the series UI, on every future
        # instance, forever — and DPAC, the only exempt venue, hosts Broadway and comedy
        # alongside music, which is exactly the case the series UI is for.
        key = (ev.venue_id, normalize_series_name(ev.name))
        if key in series_overrides:
            is_live, _note = series_overrides[key]
            label = "live music" if is_live else "non-live"
            updates[ev.id] = (is_live, f"series override: {label}")
        elif ev.venue_id in exempt_venue_ids:
            updates[ev.id] = (True, "venue: always live music")
        else:
            updates[ev.id] = classified[ev.id]
    return updates


def criteria_summary() -> dict:
    """Machine-readable snapshot of the current detection criteria.

    Surfaced to the admin UI (GET /admin/api/rules) so the rules can be reviewed
    without reading the source.
    """
    return {
        "recurrence_threshold": RECURRENCE_THRESHOLD,
        "run_gap_days": RUN_GAP_DAYS,
        "recurrence_rule": (
            f"a name repeating on {RECURRENCE_THRESHOLD}+ separate occasions at one "
            f"venue is a series; dates within {RUN_GAP_DAYS} days of each other count "
            "as one occasion, so a multi-night run stays live music"
        ),
        "always_live_venues": sorted(ALWAYS_LIVE_VENUE_SLUGS),
        "keywords": list(NON_MUSIC_KEYWORDS),
        "night_rule": "'<word> night' is non-live unless <word> is a day of the "
                      "week or listed in night_exceptions",
        "night_exceptions": sorted(NIGHT_EXCEPTIONS),
        "party_rule": "'<theme> party' (or 'parties') is non-live only when the word "
                      "immediately before 'party' is one of party_themes. This is an "
                      "allowlist (not any word) on purpose: a bare 'party' match would "
                      "flag band names like 'Bloc Party', so only these themes count.",
        "party_themes": list(PARTY_THEMES),
        "non_music_genres": list(NON_MUSIC_GENRES),
    }
