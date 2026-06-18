#!/usr/bin/env python3
"""
Trigger the triangle-shows scrape API and write structured results to a log file.

Usage:
    python tools/run_scrape.py                          # scrape all venues
    python tools/run_scrape.py --type rhp_events        # scrape one type
    python tools/run_scrape.py --url http://localhost:8000  # local dev
"""

import argparse
import json
import sys
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

BASE_URL = "https://triangle-shows.net"
LOG_FILE = Path(__file__).parent / "scrape_results.log"


def main():
    parser = argparse.ArgumentParser(description="Trigger scrape and log results")
    parser.add_argument("--type", dest="scraper_type", help="Scraper type to run (e.g. rhp_events)")
    parser.add_argument("--url", default=BASE_URL, help=f"Base URL (default: {BASE_URL})")
    args = parser.parse_args()

    endpoint = f"{args.url.rstrip('/')}/api/scrape"
    if args.scraper_type:
        endpoint += f"?scraper_type={args.scraper_type}"

    started = datetime.now()
    label = f"[{args.scraper_type or 'ALL'}]"

    print(f"{label} POST {endpoint}")
    print(f"{label} Started at {started.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{label} Writing results to {LOG_FILE}")

    try:
        req = urllib.request.Request(endpoint, method="POST", headers={"Content-Length": "0"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
            status_code = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        status_code = e.code
    except urllib.error.URLError as e:
        print(f"ERROR: Could not reach {endpoint}: {e.reason}", file=sys.stderr)
        sys.exit(1)

    elapsed = (datetime.now() - started).total_seconds()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = None

    lines = []
    lines.append("=" * 70)
    lines.append(f"Scrape run — {started.strftime('%Y-%m-%d %H:%M:%S')}  ({elapsed:.1f}s)")
    lines.append(f"Endpoint : {endpoint}")
    lines.append(f"HTTP     : {status_code}")
    lines.append("=" * 70)

    if data is None:
        lines.append(f"Non-JSON response ({status_code}):")
        lines.append(raw.decode(errors="replace"))
    elif "detail" in data:
        # FastAPI error (500 with our new handler)
        lines.append(f"ERROR: {data['detail']}")
    elif "results" in data:
        results = data["results"]
        successes = [r for r in results if r.get("status") == "success"]
        failures  = [r for r in results if r.get("status") != "success"]

        lines.append(f"Venues  : {len(results)}  ({len(successes)} ok, {len(failures)} failed)")
        total_found   = sum(r.get("found",   0) for r in successes)
        total_created = sum(r.get("created", 0) for r in successes)
        total_updated = sum(r.get("updated", 0) for r in successes)
        lines.append(f"Events  : {total_found} found  |  {total_created} created  |  {total_updated} updated")
        lines.append("")

        # Successes
        if successes:
            lines.append("  OK venues:")
            for r in successes:
                lines.append(
                    f"    OK  {r['venue']:<30}  "
                    f"found={r.get('found',0):>3}  "
                    f"created={r.get('created',0):>3}  "
                    f"updated={r.get('updated',0):>3}"
                )

        # Failures
        if failures:
            lines.append("")
            lines.append("  FAILED venues:")
            for r in failures:
                lines.append(f"    ERR {r['venue']:<30}  {r.get('error', 'unknown error')}")
    else:
        lines.append(json.dumps(data, indent=2))

    lines.append("")

    output = "\n".join(lines)
    print(output)

    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(output + "\n")

    print(f"{label} Done in {elapsed:.1f}s — appended to {LOG_FILE.name}")

    if data and "results" in data:
        failures = [r for r in data["results"] if r.get("status") != "success"]
        if failures:
            sys.exit(1)


if __name__ == "__main__":
    main()
