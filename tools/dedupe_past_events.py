#!/usr/bin/env python3
"""
Merges duplicate event rows left behind in past dates. Dry-run unless told otherwise.

Role: One-off cleanup utility. Not part of the runtime scrape or serving path, and not
something to schedule — the matching rule is deliberately fuzzy, so every run is meant to
be read by a person before anything is deleted.

Why this is needed. Before PR #51, event identity was a hash of the title, so any title
edit — a support act announced, a "[CANCELLED]" prefix added, a series name prepended —
looked like a brand new event and inserted a second row. #51 fixed the cause by matching
on the venue's own event ID first, and it reconciles away rows a source has stopped
listing. Neither of those cleans up what already exists in past dates:

  * reconcile is bounded to `today <= date <= horizon` on purpose, because venues drop
    events from their listings the moment they happen — reconciling the past would delete
    the archive on every run
  * merging only happens for events present in the current scrape, and a past event is no
    longer in any venue's listing, so nothing ever matches it
  * the display-time guard in api/events.py keys on the normalized name, which is exactly
    what a title edit changes, so it collapses almost none of these

So they are permanent debris until something like this removes them.

    Usage:
        # look, change nothing (default: 2026-07-01 .. yesterday)
        python tools/dedupe_past_events.py

        # a different window
        python tools/dedupe_past_events.py --start 2026-06-01 --end 2026-06-30

        # actually merge, after reading the dry-run output
        python tools/dedupe_past_events.py --apply

    Requires DATABASE_URL in the environment, or --database-url. To point at production:

        export DATABASE_URL="$(gcloud secrets versions access latest \
          --secret=triangle-shows-db-url --project=triangle-shows)"

    Exits 0 when it completes (whether or not anything was found), 1 on a usage or
    connection error, 2 if --apply hit a database error partway.
"""

# --- Imports ---

import argparse
import os
import re
import sys
from collections import defaultdict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from datetime import date, datetime, timedelta

try:
    import psycopg2
    import psycopg2.extras
except ImportError:  # pragma: no cover - dependency is in backend/requirements.txt
    print("psycopg2 is required: pip install -r backend/requirements.txt", file=sys.stderr)
    raise SystemExit(1)


# --- Matching rules ---

# A run is only ever considered inside one (venue, date) pair, so two different bookings
# would have to be at the same venue on the same night to collide at all.
#
# Names are compared after stripping bracketed status markers and reducing to lowercase
# alphanumerics, then matched by containment rather than equality: the whole point is that
# one title is an edited version of the other, usually by addition.
#
#   'Slow Joy'                         -> slowjoy
#   'Slow Joy with Bike Routes'        -> slowjoywithbikeroutes          (contains slowjoy)
#   '[CANCELLED] The Philharmonik'     -> thephilharmonik                (marker stripped)
#
# Containment is looser than a prefix test, and it has to be: a "[CANCELLED]" marker sits
# at the front, so prefix matching would miss precisely the case that motivated this.
# Only *status* markers are stripped, from an explicit list. Stripping every bracketed
# token was the first attempt and it was wrong: it turned '[LATE] Sub Rosa' and 'Sub Rosa'
# into the same string, and those are the early and late shows on one night at a jazz club
# — two real performances. Deleting one of them would lose a listing, which is a worse
# error than leaving a duplicate in place.
_STATUS_MARKER = re.compile(
    r"[\[\(]\s*(?:"
    r"cancell?ed|canceled|postponed|rescheduled|moved|new\s*date|"
    r"sold\s*out|low\s*tix|low\s*tickets|free|"
    r"\d{1,2}\s*\+|all\s*ages"
    r")\s*[\]\)]",
    re.IGNORECASE,
)

# Session markers do the opposite job: they *distinguish* two events rather than being
# noise on one. Two rows whose session markers differ are never merged, however similar
# their titles. Compared as sets, so 'Late Night Drive Home' and 'Late Night Drive Home
# w/ Support' still match — both carry the same marker.
_SESSION_MARKER = re.compile(
    r"\b(?:early|late|matinee|first\s*set|second\s*set|1st\s*set|2nd\s*set)\b",
    re.IGNORECASE,
)

# Markers meaning "this show is not happening as listed". A row carrying one of these is
# the newer information about the show, so it wins the survivor choice — otherwise a
# cancelled show could keep the row that still says it is on sale.
_CANCELLED_MARKER = re.compile(
    r"\b(?:cancell?ed|canceled|postponed|rescheduled)\b", re.IGNORECASE
)

_NON_ALNUM = re.compile(r"[^a-z0-9]")

# Below this many characters a normalized name is too generic to trust containment on:
# 'dj' (2) would swallow every DJ night at a venue. Pairs under the floor are reported as
# skipped rather than silently dropped, so they can be looked at by hand.
#
# Six, not eight: 'Slow Joy' normalizes to 'slowjoy', which is 7 characters, and that is a
# real duplicate in the production data this was written against. A floor of 8 silently
# excluded it — found by testing the rule against the actual rows rather than by reasoning
# about it. Six still blocks the short generic names that motivated having a floor.
MIN_NORMALIZED_LENGTH = 6

# Fields copied from a discarded row onto the survivor when the survivor's value is NULL.
# `name`, `hash`, `date` and `venue_id` are deliberately absent: the hash is derived from
# the name, and rewriting either would make the surviving row unmatchable by the scraper
# on a later run.
MERGEABLE_FIELDS = (
    "external_id", "artist", "support_artists", "doors_time", "show_time",
    "ticket_url", "price_min", "price_max", "image_url", "genre", "subgenre",
    "age_restriction", "description", "source_url",
)

BASE_FIELDS = (
    "id", "venue_id", "name", "date", "status",
    "is_live_music", "is_manual_override", "updated_at",
)


def normalize(name):
    """Reduce a title to the form used for comparison."""
    without_markers = _STATUS_MARKER.sub(" ", name or "")
    return _NON_ALNUM.sub("", without_markers.lower())


def sessions(name):
    """Session markers in a title, as a set — 'early', 'late', '2nd set' and so on."""
    return frozenset(m.group(0).lower().replace(" ", "") for m in _SESSION_MARKER.finditer(name or ""))


def start_time(row):
    """When the show actually starts, preferring show_time over doors."""
    return row.get("show_time") or row.get("doors_time")


def different_performances(a, b):
    """True when two similarly-named rows look like genuinely separate performances.

    The label alone is not enough to decide, and believing it was a mistake. Kings lists
    the same show twice, once as 'Sub Rosa' and once as '[LATE] Sub Rosa', with identical
    doors, start time and price — and both rows were inserted by a single scrape batch
    nine microseconds apart. Treating the label as decisive refused to merge a pair that
    is plainly one event.

    So the clock decides. A differing session label matters only when the two rows
    disagree about when the show starts; a real early-and-late double bill has two start
    times. When either time is missing there is nothing to compare, and the same act at
    the same venue on one night is far more often one relabeled listing than two shows —
    so the rows are treated as mergeable and the dry run puts them in front of a person.
    """
    if sessions(a["name"]) == sessions(b["name"]):
        return False

    ta, tb = start_time(a), start_time(b)
    if ta is None or tb is None:
        return False

    return ta != tb


def is_cancelled(row):
    """True when this row says the show is not happening as originally listed."""
    if _CANCELLED_MARKER.search(row.get("name") or ""):
        return True
    status = (row.get("status") or "").lower().replace("_", "").replace("-", "")
    return status in {"cancelled", "canceled", "postponed"}


def rank(row):
    """Sort key for choosing which row survives — highest wins.

    Mirrors _completeness() in backend/app/api/events.py, with one addition: a longer
    title breaks a metadata tie, because the longer title is normally the fuller listing
    ('The Briefs w/ Paint Fumes' over 'The Briefs'). Every component is total and the id
    settles the last tie, so the choice never depends on row order.
    """
    return (
        bool(row["is_manual_override"]),          # an admin decided about this row
        is_cancelled(row),                        # a cancellation is the newer truth
        bool(row["is_live_music"]),               # prefer the visible copy
        (bool(row["image_url"]) + bool(row["ticket_url"]) + (row["price_min"] is not None)),
        len(row["name"] or ""),                   # fuller listing
        row["updated_at"] or datetime.min,        # most recently scraped
        row["id"],
    )


def find_groups(rows, min_length=MIN_NORMALIZED_LENGTH):
    """Cluster rows into duplicate groups. Returns (groups, skipped_short).

    Clusters transitively within a (venue, date) bucket, so the three-way Briefs case
    ('The Briefs' / 'The Briefs w/ Paint Fumes' / 'The Briefs w/ The Sleevens, Paint
    Fumes') collapses into one group rather than two overlapping pairs.
    """
    buckets = defaultdict(list)
    for row in rows:
        buckets[(row["venue_id"], row["date"])].append(row)

    groups = []
    skipped_short = []

    for key in sorted(buckets, key=lambda k: (str(k[1]), k[0])):
        bucket = buckets[key]
        if len(bucket) < 2:
            continue

        # Shortest name first, so the base title anchors its cluster and longer edited
        # variants join it rather than each starting one of their own.
        clusters = []
        for row in sorted(bucket, key=lambda r: len(normalize(r["name"]))):
            a = normalize(row["name"])
            target = None

            for cluster in clusters:
                for other in cluster:
                    b = normalize(other["name"])
                    if not a or not b:
                        continue
                    if not (a == b or a in b or b in a):
                        continue
                    # A real early-and-late double bill has two start times.
                    if different_performances(row, other):
                        continue
                    if min(len(a), len(b)) < min_length:
                        skipped_short.append((row, other))
                        continue
                    target = cluster
                    break
                if target is not None:
                    break

            if target is None:
                clusters.append([row])
            else:
                target.append(row)

        groups.extend(c for c in clusters if len(c) > 1)

    return groups, skipped_short


# --- Database ---

def to_psycopg_url(url):
    """Translate the app's asyncpg URL into one psycopg2 accepts.

    Two differences, both of which psycopg2 rejects outright rather than ignoring:

      * the driver suffix, `postgresql+asyncpg://`
      * the TLS parameter — asyncpg spells it `ssl=require`, psycopg2 `sslmode=require`,
        and Neon's connection string uses the former. Without this the tool fails with
        'invalid URI query parameter: "ssl"'.

    An explicit sslmode already in the URL wins, on the assumption it was put there
    deliberately.
    """
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg2://", "postgres+asyncpg://"):
        url = url.replace(prefix, "postgresql://")

    parsed = urlsplit(url)
    if not parsed.query:
        return url

    params = parse_qsl(parsed.query, keep_blank_values=True)
    has_sslmode = any(k == "sslmode" for k, _ in params)

    translated = []
    for key, value in params:
        if key == "ssl" and not has_sslmode:
            # asyncpg accepts booleans here; psycopg2 wants a named mode.
            mode = {"true": "require", "on": "require", "1": "require",
                    "false": "disable", "off": "disable", "0": "disable"}.get(
                        value.lower(), value)
            translated.append(("sslmode", mode))
        elif key == "ssl":
            continue  # an explicit sslmode is already present; drop the duplicate
        else:
            translated.append((key, value))

    return urlunsplit(parsed._replace(query=urlencode(translated)))


def fetch_rows(conn, start, end):
    columns = ", ".join(BASE_FIELDS + MERGEABLE_FIELDS)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT {columns} FROM events WHERE date >= %s AND date <= %s ORDER BY date, venue_id, id",
            (start, end),
        )
        return cur.fetchall()


def apply_group(conn, survivor, losers):
    """Copy missing fields onto the survivor, then delete the discarded rows."""
    fills = {}
    for field in MERGEABLE_FIELDS:
        if survivor.get(field) not in (None, ""):
            continue
        for loser in losers:
            value = loser.get(field)
            if value not in (None, ""):
                fills[field] = value
                break

    with conn.cursor() as cur:
        if fills:
            assignments = ", ".join(f"{k} = %s" for k in fills)
            cur.execute(
                f"UPDATE events SET {assignments}, updated_at = %s WHERE id = %s",
                (*fills.values(), datetime.utcnow(), survivor["id"]),
            )
        cur.execute(
            "DELETE FROM events WHERE id = ANY(%s)", ([r["id"] for r in losers],)
        )
    return fills


# --- Reporting ---

def describe(row, marker):
    bits = [f"{marker} id={row['id']:<6}", f"{(row['name'] or '')[:58]:<58}"]
    flags = []
    if row["is_manual_override"]:
        flags.append("manual-override")
    if not row["is_live_music"]:
        flags.append("non-live")
    if row.get("status") and row["status"] != "onsale":
        flags.append(str(row["status"]))
    filled = sum(1 for f in MERGEABLE_FIELDS if row.get(f) not in (None, ""))
    flags.append(f"{filled}/{len(MERGEABLE_FIELDS)} fields")
    return "  " + " ".join(bits) + "  [" + ", ".join(flags) + "]"


def main():
    parser = argparse.ArgumentParser(
        description="Merge duplicate event rows in past dates. Dry-run by default."
    )
    parser.add_argument(
        "--start", default="2026-07-01",
        help="inclusive start date (YYYY-MM-DD). Default 2026-07-01 — July and August are "
             "what the calendar's default view still shows.",
    )
    parser.add_argument(
        "--end", default=None,
        help="inclusive end date (YYYY-MM-DD). Defaults to yesterday: the window has to "
             "end in the past, because upcoming events are the scraper's to dedupe.",
    )
    parser.add_argument("--apply", action="store_true", help="actually merge and delete")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument(
        "--min-length", type=int, default=MIN_NORMALIZED_LENGTH,
        help=f"shortest normalized name to trust containment on (default {MIN_NORMALIZED_LENGTH})",
    )
    parser.add_argument(
        "--allow-future", action="store_true",
        help="permit a window that reaches today or later (refused by default)",
    )
    args = parser.parse_args()

    if not args.database_url:
        print("No database URL. Set DATABASE_URL or pass --database-url.", file=sys.stderr)
        return 1

    try:
        start = date.fromisoformat(args.start)
        # Yesterday, not today: an event happening today may still be in a venue's listing,
        # so the scraper can still match and reconcile it.
        end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
    except ValueError as exc:
        print(f"Bad date: {exc}", file=sys.stderr)
        return 1
    if start > end:
        print("--start is after --end", file=sys.stderr)
        return 1

    # Upcoming events are the scraper's business: it matches them on external_id and
    # reconciles them every run, so touching them here would fight the live system and
    # could delete a listing that is about to be re-matched.
    if end >= date.today() and not args.allow_future:
        print(
            f"Window ends {end}, which is not in the past. The scraper already dedupes "
            f"upcoming events on every run — pass --allow-future only if you mean it.",
            file=sys.stderr,
        )
        return 1

    mode = "APPLY (rows will be merged and deleted)" if args.apply else "DRY RUN (nothing will change)"
    print(f"Window : {start} .. {end}")
    print(f"Mode   : {mode}")
    print(f"Rule   : same venue + same date, one normalized name contains the other,")
    print(f"         shortest normalized name >= {args.min_length} chars")
    print()

    try:
        conn = psycopg2.connect(to_psycopg_url(args.database_url))
    except Exception as exc:
        print(f"Could not connect: {exc}", file=sys.stderr)
        return 1

    try:
        rows = fetch_rows(conn, start, end)
        print(f"Loaded {len(rows)} events in range.")
        groups, skipped = find_groups(rows, min_length=args.min_length)
        print(f"Found {len(groups)} duplicate group(s).")
        if skipped:
            print(f"Skipped {len(skipped)} pair(s) whose names were too short to trust.")
        print()

        total_deleted = 0
        total_filled = 0

        for n, group in enumerate(sorted(groups, key=lambda g: (str(g[0]['date']), g[0]['venue_id'])), 1):
            ordered = sorted(group, key=rank, reverse=True)
            survivor, losers = ordered[0], ordered[1:]

            print(f"[{n}/{len(groups)}] {survivor['date']}  venue_id={survivor['venue_id']}")
            print(describe(survivor, "KEEP  "))
            for loser in losers:
                print(describe(loser, "DELETE"))

            if args.apply:
                try:
                    fills = apply_group(conn, survivor, losers)
                    conn.commit()
                except Exception as exc:
                    conn.rollback()
                    print(f"  ERROR on this group, rolled back, stopping: {exc}", file=sys.stderr)
                    return 2
                total_deleted += len(losers)
                total_filled += len(fills)
                if fills:
                    print(f"  filled from discarded rows: {', '.join(sorted(fills))}")
                print(f"  merged ({total_deleted} row(s) deleted so far)")
            else:
                would = [
                    f for f in MERGEABLE_FIELDS
                    if survivor.get(f) in (None, "")
                    and any(l.get(f) not in (None, "") for l in losers)
                ]
                if would:
                    print(f"  would fill from discarded rows: {', '.join(sorted(would))}")
                total_deleted += len(losers)
            print()

        print("--- summary ---")
        print(f"groups            : {len(groups)}")
        if args.apply:
            print(f"rows deleted      : {total_deleted}")
            print(f"fields backfilled : {total_filled}")
        else:
            print(f"rows that would be deleted : {total_deleted}")
            print()
            print("Nothing was changed. Re-run with --apply once the list above looks right.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
