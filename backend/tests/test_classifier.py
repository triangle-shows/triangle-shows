"""
Unit tests for app.classifier — the live-music detection logic.

Covers the three signals (keyword, themed-night phrase, genre), the "night"
false-positive guard, recurrence grouping, the manual-override skip that
protects admin decisions from being overwritten on re-scrape, and series-level
overrides (including keeping a recurring series as live music).

Run from the backend/ directory:  pytest tests/test_classifier.py
The classifier is pure stdlib, so no DB or fixtures are needed.

NOT COVERED — known gaps, all requiring a database session:

  * ScrapeManager.reclassify_all() clearing Event.approved_at when a verdict
    changes. This is the subtlest contract in the feature: the condition checks
    `is_live_music` specifically, NOT the broader `changed` flag used on the
    surrounding lines, because a reworded classification_reason is not a
    different answer. "Simplifying" it to match its neighbours would silently
    empty the admin review queue on every scrape — no error, just an
    inexplicably empty page. Verified by hand against real data (22 approvals
    cleared on a flip, 22 preserved across a no-op reclassify) but nothing here
    would catch a regression.
  * The series-scoped approve/unapprove endpoints, which group by
    normalize_series_name and must match what the dashboard shows collapsed.
  * The admin list endpoint's honest-count fields — total, approved_hidden and
    series_size — each of which exists specifically to stop the UI understating
    what it is showing or what a series action affects.

Closing these needs pytest-asyncio (not currently in requirements-dev.txt) and a
session fixture against a throwaway database. The CI workflow already stands up a
Postgres service container, so that is the natural place to hang it.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from app.classifier import (
    classify_one,
    find_recurring_event_ids,
    classify_batch,
    classification_updates,
    normalize_series_name,
    criteria_summary,
    reclassify_floor,
    RECLASSIFY_PAST_DAYS,
    RECURRENCE_THRESHOLD,
    RUN_GAP_DAYS,
)


@dataclass
class FakeEvent:
    """Lightweight stand-in for an ORM Event with just the attributes the
    classifier reads."""
    id: int
    venue_id: int
    name: str
    date: date
    genre: Optional[str] = None
    subgenre: Optional[str] = None
    is_manual_override: bool = False


D0 = date(2026, 8, 1)


def _weekly(n: int, start: date = D0) -> list[date]:
    return [start + timedelta(days=7 * i) for i in range(n)]


def _consecutive(n: int, start: date = D0) -> list[date]:
    """n back-to-back nights — a residency, not a series."""
    return [start + timedelta(days=i) for i in range(n)]


# --- classify_one: keywords ---

def test_keyword_karaoke_flagged():
    is_live, reason = classify_one("Tuesday Karaoke")
    assert is_live is False
    assert reason == "keyword: karaoke"


def test_keyword_trivia_flagged():
    is_live, reason = classify_one("Pub Trivia")
    assert is_live is False
    assert "trivia" in reason


def test_keyword_comedy_flagged():
    is_live, _ = classify_one("Stand-Up Comedy Showcase")
    assert is_live is False


def test_keyword_matches_are_word_bounded():
    # "class" must not match inside "classic"
    is_live, reason = classify_one("Classic Rock Tribute")
    assert is_live is True
    assert reason is None


def test_plain_band_name_is_live():
    is_live, reason = classify_one("The Mountain Goats")
    assert is_live is True
    assert reason is None


# --- classify_one: the "night" guard ---

def test_themed_night_flagged():
    is_live, reason = classify_one("Emo Night")
    assert is_live is False
    assert reason == "keyword: emo night"


def test_decade_night_flagged():
    is_live, _ = classify_one("80s Night Dance Party")
    assert is_live is False


def test_bare_night_not_flagged():
    # Day-of-week / song-title uses of "night" must stay live music.
    for title in ("Saturday Night Fever Tribute", "Last Night", "A Hard Day's Night",
                  "One Night Only"):
        is_live, reason = classify_one(title)
        assert is_live is True, f"{title!r} was wrongly flagged ({reason})"


def test_broad_night_flags_arbitrary_theme_words():
    # Broadened "<word> night" — any non-excepted word before "night" is flagged.
    cases = {
        "Monday Jazz Night!": "jazz night",                      # word before night = jazz
        "CLUB XCX : CHARLI XCX + HYPERPOP NIGHT": "hyperpop night",
        "Funk Night": "funk night",
        "Reggae Nights": "reggae night",                          # plural
    }
    for title, expected in cases.items():
        is_live, reason = classify_one(title)
        assert is_live is False, f"{title!r} should be flagged"
        assert reason == f"keyword: {expected}", f"{title!r} -> {reason}"


# --- classify_one: "<theme> party" ---

def test_party_themes_flagged():
    for title, theme in (("Album Listening Party", "listening"),
                         ("New Release Party", "release"),
                         ("Block Party", "block")):
        is_live, reason = classify_one(title)
        assert is_live is False, f"{title!r} should be flagged"
        assert reason == f"keyword: {theme} party"


def test_band_named_party_not_flagged():
    # "Bloc Party" (the band) must stay live — bare "party" is never matched.
    is_live, reason = classify_one("Bloc Party")
    assert is_live is True
    assert reason is None


# --- classify_one: genre ---

def test_genre_comedy_flagged():
    is_live, reason = classify_one("An Evening With Someone", genre="Comedy")
    assert is_live is False
    assert reason == "genre: comedy"


def test_genre_theatre_flagged_via_substring():
    is_live, _ = classify_one("The Nutcracker", genre="Children's Theatre")
    assert is_live is False


def test_music_genre_stays_live():
    is_live, reason = classify_one("Some Band", genre="Rock", subgenre="Indie Rock")
    assert is_live is True
    assert reason is None


def test_dance_electronic_genre_is_live():
    # "Dance/Electronic" is real live music — must not be caught as non-music.
    is_live, _ = classify_one("Big DJ Live Set", genre="Dance/Electronic")
    assert is_live is True


# --- recurrence ---

def test_recurring_series_detected():
    events = [FakeEvent(i, 1, "The Regulars", d) for i, d in enumerate(_weekly(4))]
    recurring = find_recurring_event_ids(events)
    assert set(recurring) == {0, 1, 2, 3}
    assert all(count == 4 for count in recurring.values())


def test_below_threshold_not_recurring():
    events = [FakeEvent(i, 1, "The Regulars", d) for i, d in enumerate(_weekly(2))]
    assert find_recurring_event_ids(events) == {}


def test_recurrence_is_per_venue():
    # Same name at two venues, twice each -> neither hits the threshold of 3.
    events = [
        FakeEvent(1, 1, "House Band", D0),
        FakeEvent(2, 1, "House Band", D0 + timedelta(days=7)),
        FakeEvent(3, 2, "House Band", D0),
        FakeEvent(4, 2, "House Band", D0 + timedelta(days=7)),
    ]
    assert find_recurring_event_ids(events) == {}


def test_recurrence_groups_dated_name_variants():
    # Digit-stripping normalization collapses "... 8/1", "... 8/8", "... 8/15".
    events = [
        FakeEvent(1, 1, "Open Turntables 8/1", D0),
        FakeEvent(2, 1, "Open Turntables 8/8", D0 + timedelta(days=7)),
        FakeEvent(3, 1, "Open Turntables 8/15", D0 + timedelta(days=14)),
    ]
    assert set(find_recurring_event_ids(events)) == {1, 2, 3}


# --- recurrence: a run is not a series (review item #1) ---

def test_multi_night_residency_is_not_a_series():
    """A band booked three consecutive nights is the marquee listing, not karaoke."""
    events = [FakeEvent(i, 1, "Hiss Golden Messenger", d)
              for i, d in enumerate(_consecutive(3))]
    assert find_recurring_event_ids(events) == {}


def test_long_residency_is_not_a_series():
    """DPAC-style eight-night Broadway runs must survive even without the exemption."""
    events = [FakeEvent(i, 1, "Death Becomes Her", d)
              for i, d in enumerate(_consecutive(8))]
    assert find_recurring_event_ids(events) == {}


def test_a_gap_within_the_run_window_stays_one_occasion():
    """Fri and Sun with Saturday off is still one booking."""
    dates = [D0, D0 + timedelta(days=RUN_GAP_DAYS), D0 + timedelta(days=2 * RUN_GAP_DAYS)]
    events = [FakeEvent(i, 1, "Weekend Stand", d) for i, d in enumerate(dates)]
    assert find_recurring_event_ids(events) == {}


def test_weekly_series_is_still_detected():
    """The behavior the feature exists for must be unaffected by the run rule."""
    events = [FakeEvent(i, 1, "Tuesday Karaoke", d)
              for i, d in enumerate(_weekly(RECURRENCE_THRESHOLD))]
    assert len(find_recurring_event_ids(events)) == RECURRENCE_THRESHOLD


def test_repeating_two_night_run_is_a_series():
    """Fri+Sat every week is a series — three occasions, six dates."""
    dates = []
    for week in range(3):
        dates.append(D0 + timedelta(days=7 * week))
        dates.append(D0 + timedelta(days=7 * week + 1))
    events = [FakeEvent(i, 1, "Emo Weekend", d) for i, d in enumerate(dates)]

    recurring = find_recurring_event_ids(events)

    assert len(recurring) == 6
    assert all(count == 6 for count in recurring.values()), "count reports dates, not occasions"


def test_monthly_series_is_detected():
    """Sparse cadences are series too — the run rule must not swallow them."""
    dates = [D0, D0 + timedelta(days=30), D0 + timedelta(days=60)]
    events = [FakeEvent(i, 1, "First Friday Market", d) for i, d in enumerate(dates)]
    assert len(find_recurring_event_ids(events)) == 3


# --- recurrence: unnamed events are not a series (review item #3) ---

def test_empty_names_do_not_form_a_series():
    """A scraper emitting nameless records must not hide them as a phantom series."""
    events = [FakeEvent(i, 1, "", d) for i, d in enumerate(_weekly(4))]
    assert find_recurring_event_ids(events) == {}


def test_digit_only_names_do_not_form_a_series():
    """These normalize to '' and are unrelated events, not one series."""
    events = [
        FakeEvent(1, 1, "2/14", D0),
        FakeEvent(2, 1, "$5", D0 + timedelta(days=7)),
        FakeEvent(3, 1, "7/8 - 9", D0 + timedelta(days=14)),
    ]
    assert find_recurring_event_ids(events) == {}


def test_unnamed_events_do_not_drag_in_named_ones():
    """The empty-key group must stay separate from a real series at the same venue."""
    events = [FakeEvent(i, 1, "", d) for i, d in enumerate(_weekly(3))]
    events += [FakeEvent(10 + i, 1, "Tuesday Karaoke", d)
               for i, d in enumerate(_weekly(3, start=D0 + timedelta(days=3)))]

    recurring = find_recurring_event_ids(events)

    assert set(recurring) == {10, 11, 12}


# --- classify_batch: signal priority ---

def test_recurrence_overrides_a_keyword_miss():
    # No keyword in the name, but it recurs weekly -> non-live via recurrence.
    events = [FakeEvent(i, 1, "The Regulars", d) for i, d in enumerate(_weekly(3))]
    result = classify_batch(events)
    for eid in (0, 1, 2):
        is_live, reason = result[eid]
        assert is_live is False
        assert reason == "recurring: 3 dates"


def test_batch_keeps_one_off_show_live():
    events = [FakeEvent(1, 1, "Waxahatchee", D0)]
    is_live, reason = classify_batch(events)[1]
    assert is_live is True
    assert reason is None


# --- override durability (the pure function behind reclassify_all) ---

def test_manual_override_is_excluded_from_updates():
    events = [
        FakeEvent(1, 1, "Karaoke", D0, is_manual_override=True),   # admin forced a value
        FakeEvent(2, 1, "Trivia", D0, is_manual_override=False),
    ]
    updates = classification_updates(events)
    assert 1 not in updates          # overridden row is left untouched
    assert updates[2] == (False, "keyword: trivia")


# --- series-level overrides ---

def _series_key(venue_id: int, name: str) -> tuple:
    return (venue_id, normalize_series_name(name))


def test_series_override_keeps_recurring_series_live():
    # A weekly jazz jam that recurrence would flag non-live, but the admin has
    # marked the whole series as live music.
    events = [FakeEvent(i, 1, "Wednesday Night Jazz Jam", d)
              for i, d in enumerate(_weekly(4))]
    overrides = {_series_key(1, "Wednesday Night Jazz Jam"): (True, "house jazz band")}
    updates = classification_updates(events, series_overrides=overrides)
    for eid in range(4):
        assert updates[eid] == (True, "series override: live music")


def test_series_override_can_force_non_live():
    # A one-off name recurrence wouldn't catch, but the admin wants it hidden.
    events = [FakeEvent(1, 2, "Vinyl Social", D0)]
    overrides = {_series_key(2, "Vinyl Social"): (False, "just records, no band")}
    updates = classification_updates(events, series_overrides=overrides)
    assert updates[1] == (False, "series override: non-live")


def test_series_override_is_venue_scoped():
    # Same series name at a different venue is unaffected by the override.
    events = [
        FakeEvent(1, 1, "Open Turntables", D0),  # overridden venue
        FakeEvent(2, 2, "Open Turntables", D0),  # different venue
    ]
    overrides = {_series_key(1, "Open Turntables"): (True, None)}
    updates = classification_updates(events, series_overrides=overrides)
    assert updates[1] == (True, "series override: live music")
    # Venue 2 falls through to automatic classification (a single date -> live).
    assert updates[2] == (True, None)


def test_per_event_override_beats_series_override():
    # The specific instance is manually overridden, so it is left untouched even
    # though a series override also matches it.
    events = [FakeEvent(1, 1, "Karaoke", D0, is_manual_override=True)]
    overrides = {_series_key(1, "Karaoke"): (True, None)}
    updates = classification_updates(events, series_overrides=overrides)
    assert 1 not in updates  # reclassify leaves the hand-set value in place


def test_series_key_matches_dated_name_variants():
    # The admin keys off one instance; other dated instances of the series match.
    assert (normalize_series_name("Trivia Night 8/1")
            == normalize_series_name("Trivia Night 8/8"))


# --- always-live venue exemption (e.g. DPAC) ---

def test_exempt_venue_forced_live_over_automatic_classification():
    # Venue 9 is exempt, so a recurring, keyword-y name there stays live music instead of
    # being flagged as a series.
    events = [FakeEvent(i, 9, "Karaoke Night", d) for i, d in enumerate(_weekly(4))]
    updates = classification_updates(events, exempt_venue_ids={9})
    for eid in range(4):
        assert updates[eid] == (True, "venue: always live music")


def test_series_override_beats_the_venue_exemption():
    """Review item #6. This assertion is the reverse of what it was.

    The exemption used to return before series overrides were consulted, which made an
    exempt venue's series impossible to mark non-live through the series UI — on every
    future instance, permanently. DPAC is the only exempt venue and it hosts Broadway and
    comedy alongside music, so that is precisely the case the series UI exists for. The
    exemption is now a default that a deliberate series decision can override.
    """
    events = [FakeEvent(i, 9, "Karaoke Night", d) for i, d in enumerate(_weekly(4))]
    overrides = {_series_key(9, "Karaoke Night"): (False, None)}

    updates = classification_updates(
        events, series_overrides=overrides, exempt_venue_ids={9}
    )

    for eid in range(4):
        assert updates[eid] == (False, "series override: non-live")


def test_a_series_override_can_also_keep_an_exempt_venues_series_live():
    """The override wins in both directions — it is not a special case for hiding."""
    events = [FakeEvent(i, 9, "Karaoke Night", d) for i, d in enumerate(_weekly(4))]
    overrides = {_series_key(9, "Karaoke Night"): (True, None)}

    updates = classification_updates(
        events, series_overrides=overrides, exempt_venue_ids={9}
    )

    for eid in range(4):
        assert updates[eid] == (True, "series override: live music")


def test_an_unrelated_series_at_an_exempt_venue_still_gets_the_exemption():
    """Only the overridden series changes; the rest of the venue keeps its default."""
    events = [FakeEvent(i, 9, "Karaoke Night", d) for i, d in enumerate(_weekly(4))]
    events += [FakeEvent(100 + i, 9, "Trivia Night", d) for i, d in enumerate(_weekly(4))]
    overrides = {_series_key(9, "Karaoke Night"): (False, None)}

    updates = classification_updates(
        events, series_overrides=overrides, exempt_venue_ids={9}
    )

    assert updates[0] == (False, "series override: non-live")
    assert updates[100] == (True, "venue: always live music")


def test_exempt_venue_still_respects_per_event_override():
    # A hand-set instance at an exempt venue is still left untouched.
    events = [FakeEvent(1, 9, "Comedy", D0, is_manual_override=True)]
    updates = classification_updates(events, exempt_venue_ids={9})
    assert 1 not in updates


# --- criteria surface ---

def test_criteria_summary_shape():
    summary = criteria_summary()
    assert summary["recurrence_threshold"] == 3
    assert summary["run_gap_days"] == RUN_GAP_DAYS
    assert "occasion" in summary["recurrence_rule"]
    assert "karaoke" in summary["keywords"]
    assert "listening" in summary["party_themes"]
    assert "allowlist" in summary["party_rule"]  # explains why "party" isn't matched bare
    assert "saturday" in summary["night_exceptions"]
    assert "dpac" in summary["always_live_venues"]
    assert "comedy" in summary["non_music_genres"]


# --- reclassify_floor ---

def test_reclassify_floor_reaches_into_the_past():
    """The window must extend behind today, not just forward.

    The calendar can be scrolled back into recent dates, and those events carry
    is_live_music too — if the floor were today, past events would freeze at whatever
    they were last set to and stop responding to series rules.
    """
    today = date(2026, 8, 27)
    assert reclassify_floor(today) < today
    assert reclassify_floor(today) == today - timedelta(days=RECLASSIFY_PAST_DAYS)


def test_reclassify_floor_window_is_about_a_month():
    """Guards the constant itself. Widening it changes the blast radius of every
    series action and of the admin queue, so a change here should be deliberate."""
    assert RECLASSIFY_PAST_DAYS == 30


def test_reclassify_floor_defaults_to_today():
    """Called with no argument in production code paths, so the default must work."""
    assert reclassify_floor() == date.today() - timedelta(days=RECLASSIFY_PAST_DAYS)
