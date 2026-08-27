#!/usr/bin/env python3
"""
Polls /api/health until the deployed git SHA matches the local HEAD commit.

Role: Developer utility — run manually after pushing to confirm a Cloud Run deploy
has gone live. Not part of the runtime scrape or serving path.
Requires: git available on PATH; network access to the deployed app's /api/health
endpoint (which returns a JSON body with a "version" field set to the deployed SHA).

Usage:
    python tools/wait_for_deploy.py
    python tools/wait_for_deploy.py --url http://localhost:8000
    python tools/wait_for_deploy.py --interval 15
    python tools/wait_for_deploy.py --timeout 300   # give up sooner

Exits 0 when the deployed SHA matches local HEAD, 1 if the timeout passes first.
"""

# --- Imports ---

import argparse
import json
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime

# --- Constants ---

BASE_URL = "https://triangle-shows.net"
DEFAULT_INTERVAL = 20  # seconds between health-check polls
DEFAULT_TIMEOUT = 900  # give up after 15 minutes rather than polling forever

# Cloudflare fronts triangle-shows.net and rejects urllib's default
# "Python-urllib/x.y" agent with a 403. Any identifiable agent gets through.
USER_AGENT = "triangle-shows-deploy-watcher/1.0 (+https://github.com/ty-fi/triangle-shows)"


# --- Helpers ---

def get_local_sha():
    """Return the full SHA of the local HEAD commit."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True
        )
        return result.stdout.strip()
    except Exception as e:
        print(f"ERROR: Could not get local git SHA: {e}", file=sys.stderr)
        sys.exit(1)


def get_deployed_version(url):
    """Fetch the 'version' field from /api/health, returning an error string on failure."""
    try:
        req = urllib.request.Request(
            f"{url}/api/health", headers={"User-Agent": USER_AGENT}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            # The health endpoint embeds the deployed git SHA as "version"
            return data.get("version", "unknown")
    except urllib.error.HTTPError as e:
        # Must precede URLError — HTTPError subclasses it, and the status code matters.
        # A 503 from the stale-data check still carries the deployed SHA in its body,
        # so read it rather than reporting the revision as unreachable.
        try:
            version = json.loads(e.read()).get("version")
            if version:
                return version
        except Exception:
            pass
        return f"(http {e.code} {e.reason})"
    except urllib.error.URLError as e:
        # App is still coming up or unreachable — not a fatal error, just keep polling
        return f"(unreachable: {e.reason})"
    except Exception as e:
        return f"(error: {e})"


# --- Main ---

def main():
    parser = argparse.ArgumentParser(description="Poll until deployed version matches local HEAD")
    parser.add_argument("--url", default=BASE_URL, help=f"Base URL (default: {BASE_URL})")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help=f"Poll interval in seconds (default: {DEFAULT_INTERVAL})")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"Give up after this many seconds (default: {DEFAULT_TIMEOUT})")
    args = parser.parse_args()

    local_sha = get_local_sha()
    short = local_sha[:7]  # abbreviated SHA for display

    # flush on every print: stdout is block-buffered when redirected to a file,
    # so without this a backgrounded run shows no output until it exits.
    print(f"Waiting for deploy of {short} to {args.url}", flush=True)
    print(f"Polling every {args.interval}s, giving up after {args.timeout}s — Ctrl+C to cancel\n", flush=True)

    start = datetime.now()

    while True:
        deployed = get_deployed_version(args.url)
        elapsed = (datetime.now() - start).total_seconds()
        ts = datetime.now().strftime("%H:%M:%S")

        # Compare with startswith in both directions to handle full vs. short SHA mismatches
        if deployed.startswith(local_sha) or local_sha.startswith(deployed):
            print(f"[{ts}] DEPLOYED  version={deployed[:7]}  ({elapsed:.0f}s)", flush=True)
            return 0

        # Failures are returned parenthesized — show them whole instead of truncating
        # to 7 characters, which used to turn "(http 403 Forbidden)" into "(http 4".
        shown = deployed if deployed.startswith("(") else deployed[:7]
        print(f"[{ts}] waiting   deployed={shown}  want={short}  ({elapsed:.0f}s)", flush=True)

        if elapsed + args.interval > args.timeout:
            print(f"\nGave up after {elapsed:.0f}s. Last status: {deployed}", file=sys.stderr, flush=True)
            print("The build may have failed, or the health endpoint may be unreachable.", file=sys.stderr, flush=True)
            return 1

        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nCancelled.", flush=True)
            return 0


if __name__ == "__main__":
    sys.exit(main())
