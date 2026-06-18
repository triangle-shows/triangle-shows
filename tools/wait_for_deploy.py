#!/usr/bin/env python3
"""
Poll /api/health until the deployed version matches the local git HEAD.

Usage:
    python tools/wait_for_deploy.py
    python tools/wait_for_deploy.py --url http://localhost:8000
    python tools/wait_for_deploy.py --interval 15
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime

BASE_URL = "https://triangle-shows.net"
DEFAULT_INTERVAL = 20


def get_local_sha():
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
    try:
        req = urllib.request.Request(f"{url}/api/health")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return data.get("version", "unknown")
    except urllib.error.URLError as e:
        return f"(unreachable: {e.reason})"
    except Exception as e:
        return f"(error: {e})"


def main():
    parser = argparse.ArgumentParser(description="Poll until deployed version matches local HEAD")
    parser.add_argument("--url", default=BASE_URL, help=f"Base URL (default: {BASE_URL})")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help=f"Poll interval in seconds (default: {DEFAULT_INTERVAL})")
    args = parser.parse_args()

    local_sha = get_local_sha()
    short = local_sha[:7]

    print(f"Waiting for deploy of {short} to {args.url}")
    print(f"Polling every {args.interval}s — Ctrl+C to cancel\n")

    start = datetime.now()
    attempt = 0

    while True:
        attempt += 1
        deployed = get_deployed_version(args.url)
        elapsed = (datetime.now() - start).total_seconds()
        ts = datetime.now().strftime("%H:%M:%S")

        if deployed.startswith(local_sha) or local_sha.startswith(deployed):
            print(f"[{ts}] DEPLOYED  version={deployed[:7]}  ({elapsed:.0f}s)")
            break
        else:
            print(f"[{ts}] waiting   deployed={deployed[:7]}  want={short}  ({elapsed:.0f}s)")

        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nCancelled.")
            sys.exit(0)


if __name__ == "__main__":
    main()
