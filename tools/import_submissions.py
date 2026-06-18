#!/usr/bin/env python3
"""Import approved event submissions from Google Sheet into the triangle-shows DB.

Usage:
    python tools/import_submissions.py [--dry-run]

Setup (one-time):
    1. Create a GCP service account (IAM → Service Accounts → Create).
       No roles needed — Sheets API auth is done via OAuth scope, not IAM.
    2. Enable the Google Sheets API for the project (APIs & Services → Enable).
    3. Download the JSON key for the service account; save as:
           tools/service_account.json    ← gitignored
    4. Share your Google Sheet with the service account's email address (viewer
       access is enough for reading; editor needed to write "imported" status back).
    5. Set environment variables (or add to a .env file in the repo root):
           SHEET_ID=<the long ID from your Sheet URL>
           DATABASE_URL=<your Neon connection string>

Expected Sheet columns (left to right).  The first row must be a header row.
Google Forms will auto-create columns A–I; add J and K manually:

    A  Timestamp          (auto-populated by Forms)
    B  Artist / Band name (required)
    C  Event name         (optional — for named events like "Haw River Jamboree")
    D  Venue              (must match a venue name in the DB exactly, or close)
    E  Date               (YYYY-MM-DD or MM/DD/YYYY)
    F  Show time          (e.g. "8:00 PM", "8pm", "20:00" — optional)
    G  Ticket URL         (optional)
    H  Notes              (optional — shown as event description)
    I  Submitter email    (optional)
    J  Status             (set to "approved" to import; script writes "imported")
    K  Admin notes        (optional — for your own notes, never imported)

Install dependencies:
    pip install google-auth google-api-python-client psycopg2-binary python-dotenv
"""

import argparse
import hashlib
import os
import re
import sys
from datetime import date, datetime, time
from pathlib import Path

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary")
    sys.exit(1)

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
except ImportError:
    print("ERROR: google-api-python-client not installed.")
    print("Run: pip install google-auth google-api-python-client")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass  # python-dotenv is optional


# ── Config ────────────────────────────────────────────────────────────────────

SHEET_ID     = os.environ.get("SHEET_ID", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
CREDS_FILE   = Path(__file__).parent / "service_account.json"
SCOPES       = ["https://www.googleapis.com/auth/spreadsheets"]

# Column indices (0-based). Update these if you add/remove form fields.
COL_TIMESTAMP   = 0
COL_ARTIST      = 1
COL_EVENT_NAME  = 2
COL_VENUE       = 3
COL_DATE        = 4
COL_SHOW_TIME   = 5
COL_TICKET_URL  = 6
COL_NOTES       = 7
COL_EMAIL       = 8
COL_STATUS      = 9   # set to "approved" to import; written back as "imported"
COL_ADMIN_NOTES = 10  # never read; for your own notes


# ── Helpers ───────────────────────────────────────────────────────────────────

def compute_hash(venue_slug: str, date_iso: str, name: str) -> str:
    """Replicate the hash logic from backend/app/scrapers/base.py exactly."""
    name = re.sub(r'\b(box|boxes)\b', '', name, flags=re.IGNORECASE)
    normalized = re.sub(r'[^a-z0-9]', '', name.lower().strip())
    raw = f"{venue_slug}|{date_iso}|{normalized}"
    return hashlib.sha256(raw.encode()).hexdigest()


def parse_date(text: str) -> date | None:
    text = text.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def parse_time(text: str) -> time | None:
    if not text:
        return None
    text = text.strip().lower().replace('.', '')
    m = re.search(r'(\d{1,2}):(\d{2})\s*(am|pm)', text)
    if m:
        h, mn, ampm = int(m.group(1)), int(m.group(2)), m.group(3)
        if ampm == 'pm' and h != 12: h += 12
        elif ampm == 'am' and h == 12: h = 0
        return time(h, mn)
    m = re.search(r'(\d{1,2})\s*(am|pm)', text)
    if m:
        h, ampm = int(m.group(1)), m.group(2)
        if ampm == 'pm' and h != 12: h += 12
        elif ampm == 'am' and h == 12: h = 0
        return time(h, 0)
    m = re.search(r'(\d{1,2}):(\d{2})', text)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mn <= 59:
            return time(h, mn)
    return None


def get_cell(row: list, col: int) -> str:
    return row[col].strip() if col < len(row) else ""


def fuzzy_venue_match(name: str, venue_by_name: dict) -> dict | None:
    """Case-insensitive exact match, then prefix match."""
    key = name.lower()
    if key in venue_by_name:
        return venue_by_name[key]
    # Try prefix / substring match as fallback
    for k, v in venue_by_name.items():
        if key in k or k in key:
            return v
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Import approved event submissions from Google Sheet"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be imported without writing to DB or Sheet"
    )
    args = parser.parse_args()
    dry_run = args.dry_run

    if not SHEET_ID:
        print("ERROR: SHEET_ID env var not set.")
        sys.exit(1)
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL env var not set.")
        sys.exit(1)
    if not CREDS_FILE.exists():
        print(f"ERROR: Service account credentials not found at {CREDS_FILE}")
        print("       Download a JSON key from GCP → IAM → Service Accounts.")
        sys.exit(1)

    prefix = "[DRY RUN] " if dry_run else ""
    print(f"{prefix}Connecting to Google Sheets...")

    creds   = service_account.Credentials.from_service_account_file(str(CREDS_FILE), scopes=SCOPES)
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    sheet   = service.spreadsheets()

    result = sheet.values().get(spreadsheetId=SHEET_ID, range="Sheet1").execute()
    rows   = result.get("values", [])

    if not rows:
        print("Sheet is empty.")
        return

    data_rows = rows[1:]  # skip header
    approved  = [
        (i + 2, row)  # row_num is 1-indexed and skips header, so offset by 2
        for i, row in enumerate(data_rows)
        if get_cell(row, COL_STATUS).lower() == "approved"
    ]

    print(f"Found {len(data_rows)} submission(s) total, {len(approved)} approved.")
    if not approved:
        print("Nothing to import.")
        return

    print(f"{prefix}Connecting to database...")
    conn = psycopg2.connect(DATABASE_URL)
    cur  = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("SELECT id, slug, name FROM venues")
    venues_raw   = cur.fetchall()
    venue_by_name = {v["name"].lower(): v for v in venues_raw}

    imported        = 0
    skipped         = 0
    errors          = []
    to_mark_done    = []

    for row_num, row in approved:
        artist     = get_cell(row, COL_ARTIST)
        event_name = get_cell(row, COL_EVENT_NAME) or artist
        venue_str  = get_cell(row, COL_VENUE)
        date_str   = get_cell(row, COL_DATE)
        time_str   = get_cell(row, COL_SHOW_TIME)
        ticket_url = get_cell(row, COL_TICKET_URL) or None
        notes      = get_cell(row, COL_NOTES) or None

        if not artist:
            errors.append(f"Row {row_num}: No artist name — skipped.")
            skipped += 1
            continue

        venue = fuzzy_venue_match(venue_str, venue_by_name)
        if not venue:
            errors.append(
                f"Row {row_num}: Unknown venue '{venue_str}' — skipped. "
                "Check spelling or add to seed.py first."
            )
            skipped += 1
            continue

        event_date = parse_date(date_str)
        if not event_date:
            errors.append(f"Row {row_num}: Could not parse date '{date_str}' — skipped.")
            skipped += 1
            continue

        event_time = parse_time(time_str)
        event_hash = compute_hash(venue["slug"], event_date.isoformat(), artist or event_name)

        cur.execute("SELECT id FROM events WHERE hash = %s", (event_hash,))
        existing = cur.fetchone()
        if existing:
            print(f"  Row {row_num}: '{artist}' @ {venue['slug']} on {event_date} already exists (id={existing['id']}) — marking done.")
            to_mark_done.append(row_num)
            skipped += 1
            continue

        label = f"'{artist}' @ {venue['slug']} on {event_date}" + (f" {time_str}" if time_str else "")
        print(f"  Row {row_num}: {prefix}Importing {label}")

        if not dry_run:
            cur.execute(
                """
                INSERT INTO events (
                    venue_id, name, artist, date, show_time,
                    ticket_url, status, source, source_url,
                    hash, description, created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, 'on_sale', 'user_submission', %s,
                    %s, %s, NOW(), NOW()
                )
                """,
                (
                    venue["id"],
                    event_name,
                    artist or None,
                    event_date,
                    event_time,
                    ticket_url,
                    ticket_url,   # source_url = ticket URL as best proxy
                    event_hash,
                    notes,
                ),
            )
            to_mark_done.append(row_num)

        imported += 1

    if not dry_run:
        conn.commit()
        print(f"\nCommitted {imported} event(s) to database.")

        if to_mark_done:
            print(f"Writing 'imported' status back to Sheet for {len(to_mark_done)} row(s)...")
            for row_num in to_mark_done:
                sheet.values().update(
                    spreadsheetId=SHEET_ID,
                    range=f"Sheet1!J{row_num}",
                    valueInputOption="RAW",
                    body={"values": [["imported"]]},
                ).execute()
            print("Done.")

    cur.close()
    conn.close()

    print(f"\nSummary: imported={imported}, skipped={skipped}")
    if errors:
        print("\nErrors:")
        for e in errors:
            print(f"  {e}")


if __name__ == "__main__":
    main()
