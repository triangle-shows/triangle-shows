#!/usr/bin/env python3
"""
Moves RHP events that were filed a year early onto their real date. Dry-run unless told otherwise.

Role: One-off cleanup utility. Not part of the runtime scrape or serving path.

Why this is needed. The RHP listing pages (Cat's Cradle, Cat's Cradle Back Room, Lincoln
Theatre, Local 506) render dates as "Fri, Jan 08" — no year, no datetime attribute — and
rhp_events.py used to assume the current year. Every show from January onward was stored
a year early, in the past, where the calendar never showed it. The scraper now infers the
year from the weekday, but that does nothing for rows already stored:

  * reconcile is bounded to `today <= date <= horizon`, so it never deletes a past row
  * RHP rows have no external_id, so they are matched on the title hash alone — and the
    hash includes the date, so the corrected scrape sees a new event and inserts a second
    row rather than moving the old one

How a misfiled row is recognised. A venue lists a show before it happens and drops it
once it has, so a genuine row is always created on or before its date. A row created more
than a week *after* its date can only be one whose year was wrong.

What happens to each one. Its date moves forward a year and its hash is recomputed for the
new date, exactly as ScrapedEvent.hash computes it, so the next scrape matches it rather
than inserting beside it. If the corrected scrape has already inserted that row — a row
with the recomputed hash exists — the misfiled one is deleted instead, since the hash
column is unique and the newer row carries the fresher listing.

Run it AFTER the scraper fix is deployed. Before then, the next scrape re-files these
shows a year early all over again.

    Usage:
        # look, change nothing
        python tools/fix_rhp_years.py

        # actually move and delete, after reading the dry-run output
        python tools/fix_rhp_years.py --apply

    Requires DATABASE_URL in the environment, or --database-url. To point at production:

        export DATABASE_URL="$(gcloud secrets versions access latest \
          --secret=triangle-shows-db-url --project=triangle-shows)"

    Exits 0 when it completes (whether or not anything was found), 1 on a usage or
    connection error, 2 if --apply hit a database error partway.
"""

# --- Imports ---

import argparse
import hashlib
import os
import re
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dedupe_past_events import to_psycopg_url  # noqa: E402  (also exits cleanly without psycopg2)

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402


# --- Rules ---

# How far past its date a row has to have been created before it counts as misfiled. A
# show is normally listed weeks ahead; a week of slack absorbs UTC created_at against a
# local date and a venue that keeps tonight's show up until the morning after.
MIN_DAYS_CREATED_AFTER = 7

FIELDS = (
    "id", "venue_id", "slug", "name", "date", "hash", "created_at",
    "is_manual_override", "is_manually_created", "duplicate_of_id",
)


def title_hash(venue_slug, day, name):
    """ScrapedEvent.hash, reproduced so this tool needs nothing from backend/.

    backend/tests/test_fix_rhp_years.py holds the two in step.
    """
    name = re.sub(r'\b(box|boxes)\b', '', name, flags=re.IGNORECASE)
    normalized = re.sub(r'[^a-z0-9]', '', name.lower().strip())
    raw = f"{venue_slug}|{day.isoformat()}|{normalized}"
    return hashlib.sha256(raw.encode()).hexdigest()


def is_misfiled(row):
    """True when the row was created too long after its own date to be real."""
    return (row["created_at"].date() - row["date"]).days > MIN_DAYS_CREATED_AFTER


def next_year(day):
    try:
        return day.replace(year=day.year + 1)
    except ValueError:
        return None  # 29 February; the old parser could never produce one


def plan(rows, existing_hashes):
    """Decide what to do with each misfiled row. Returns a list of (action, row, target, why).

    action is "move" (shift the date, rewrite the hash), "delete" (the corrected row is
    already there) or "skip" (needs a person). `existing_hashes` maps hash -> id for every
    row the target hashes could collide with.

    Latest date first, and in that order when applied. A show that recurs every year can
    have two misfiled rows a year apart, and the earlier one's target is the later one's
    current hash: moving the later one first frees that hash, so the earlier one moves
    too instead of being deleted as if the corrected row already existed.
    """
    hashes = dict(existing_hashes)
    actions = []
    for row in sorted(rows, key=lambda r: (r["date"], r["id"]), reverse=True):
        if not is_misfiled(row):
            continue
        target = next_year(row["date"])
        if target is None:
            actions.append(("skip", row, None, "no such date next year"))
            continue
        if row["is_manually_created"]:
            # Added by hand, so its date was typed, not inferred. Never second-guessed here.
            continue
        if title_hash(row["slug"], row["date"], row["name"]) != row["hash"]:
            # The stored hash is not what this name would scrape to, so the title was
            # edited after the fact and the next scrape would not match the rewritten hash.
            actions.append(("skip", row, target, "title edited since scrape"))
            continue

        new_hash = title_hash(row["slug"], target, row["name"])
        twin = hashes.get(new_hash)
        if twin is None:
            actions.append(("move", row, target, None))
            hashes.pop(row["hash"], None)
            hashes[new_hash] = row["id"]
        elif row["is_manual_override"] or row["duplicate_of_id"] is not None:
            # An admin has judged the misfiled row; deleting it would lose that decision.
            actions.append(("skip", row, target, f"admin-flagged, corrected row id={twin} exists"))
        else:
            actions.append(("delete", row, target, f"corrected row id={twin} exists"))
    return actions


# --- Database ---

def fetch_candidates(conn, today):
    columns = ", ".join(
        "v.slug" if f == "slug" else f"e.{f}" for f in FIELDS
    )
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT {columns} FROM events e JOIN venues v ON v.id = e.venue_id "
            "WHERE v.scraper_type = 'rhp_events' AND e.date < %s ORDER BY e.date, e.id",
            (today,),
        )
        return cur.fetchall()


def fetch_hashes(conn, hashes):
    if not hashes:
        return {}
    with conn.cursor() as cur:
        cur.execute("SELECT hash, id FROM events WHERE hash = ANY(%s)", (list(hashes),))
        return dict(cur.fetchall())


def apply_action(conn, action, row, target):
    with conn.cursor() as cur:
        if action == "move":
            cur.execute(
                "UPDATE events SET date = %s, hash = %s, updated_at = %s WHERE id = %s",
                (target, title_hash(row["slug"], target, row["name"]), datetime.utcnow(), row["id"]),
            )
        elif action == "delete":
            cur.execute("DELETE FROM events WHERE id = %s", (row["id"],))


# --- Reporting ---

def main():
    parser = argparse.ArgumentParser(
        description="Move RHP events filed a year early onto their real date. Dry-run by default."
    )
    parser.add_argument("--apply", action="store_true", help="actually move and delete")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    args = parser.parse_args()

    if not args.database_url:
        print("No database URL. Set DATABASE_URL or pass --database-url.", file=sys.stderr)
        return 1

    mode = "APPLY (rows will be moved and deleted)" if args.apply else "DRY RUN (nothing will change)"
    print(f"Mode : {mode}")
    print(f"Rule : RHP venue, date in the past, created more than "
          f"{MIN_DAYS_CREATED_AFTER} days after that date")
    print()

    try:
        conn = psycopg2.connect(to_psycopg_url(args.database_url))
    except Exception as exc:
        print(f"Could not connect: {exc}", file=sys.stderr)
        return 1

    try:
        rows = fetch_candidates(conn, date.today())
        # Every hash a move could land on, plus the candidates' own: those are what a
        # later move frees for an earlier one (see plan()).
        targets = {r["hash"] for r in rows} | {
            title_hash(r["slug"], t, r["name"])
            for r in rows if is_misfiled(r) and (t := next_year(r["date"]))
        }
        actions = plan(rows, fetch_hashes(conn, targets))
        print(f"Loaded {len(rows)} past RHP events; {len(actions)} look misfiled.")
        print()

        counts = {"move": 0, "delete": 0, "skip": 0}
        for action, row, target, why in actions:
            line = (f"  {action.upper():<6} id={row['id']:<6} {row['slug']:<22} "
                    f"{row['date']} -> {target or '?'}  {(row['name'] or '')[:50]}")
            print(line + (f"  ({why})" if why else ""))
            if args.apply and action != "skip":
                try:
                    apply_action(conn, action, row, target)
                    conn.commit()
                except Exception as exc:
                    conn.rollback()
                    print(f"  ERROR on id={row['id']}, rolled back, stopping: {exc}", file=sys.stderr)
                    return 2
            counts[action] += 1

        print()
        print("--- summary ---")
        verb = "" if args.apply else "would be "
        print(f"{verb}moved   : {counts['move']}")
        print(f"{verb}deleted : {counts['delete']}")
        print(f"skipped         : {counts['skip']}")
        if not args.apply:
            print()
            print("Nothing was changed. Re-run with --apply once the list above looks right.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
