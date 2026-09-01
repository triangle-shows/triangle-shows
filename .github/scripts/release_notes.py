"""Build release notes for a main -> prod promotion, one line per component PR.

Role: run by .github/workflows/release-notes.yml when a promotion PR is opened against
`prod`, and available on demand (workflow_dispatch, or locally) to preview what a
promotion would carry. Writes markdown to stdout.

Why this exists. A promotion here is a single PR carrying every commit merged to `main`
since the last one — 29 commits across 10 PRs at the time of writing. `git log --oneline`
over that range mixes merge commits with the individual commits inside each PR, so a
hand-written summary of it describes what the author *remembers* doing. The first run of
this script surfaced four merged security fixes from a fork that nobody had thought to
mention, which is the whole argument for deriving the list instead.

PRs are matched to the range by **merge commit SHA**, not by grepping `#\\d+` out of commit
subjects. That is what makes it correct for squash-merged PRs, which leave no "Merge pull
request #N" line to grep, and immune to a commit message that merely mentions an issue
number.

Everything comes from the GitHub API via `gh`, so there is no checkout, no fetch-depth
trap, and it behaves the same locally as in Actions.

Requires: the `gh` CLI, authenticated. In Actions that is GH_TOKEN=${{ github.token }}.
"""

# --- Imports ---

import argparse
import json
import os
import subprocess
import sys
from typing import Any, Optional

# The compare API returns at most 250 commits and `gh pr list` is capped below. A
# promotion larger than this is not a release note problem, but say so rather than
# silently truncating — a quiet cap here would reintroduce exactly the "the summary
# omitted things" failure this script exists to prevent.
COMPARE_COMMIT_CAP = 250
PR_LOOKBACK = 100


# --- GitHub API ---


def gh_json(*args: str) -> Any:
    """Run `gh` and parse its JSON output, failing loudly."""
    result = subprocess.run(
        ["gh", *args], capture_output=True, text=True, encoding="utf-8"
    )
    if result.returncode != 0:
        sys.exit(f"gh {' '.join(args)} failed:\n{result.stderr.strip()}")
    return json.loads(result.stdout or "null")


def commits_in_range(repo: str, base: str, head: str) -> list[dict]:
    """Commits on `head` that are not on `base`.

    `base...head` (three dots) is deliberate: it compares against the merge base, so the
    result is what a promotion PR would actually contain. Note this repo's promotions
    always report status "diverged" with a non-zero `behind_by` — every past promotion left
    its own merge commit on `prod` — so divergence here is normal and not worth warning
    about.
    """
    data = gh_json("api", f"repos/{repo}/compare/{base}...{head}")
    commits = data.get("commits", [])
    if len(commits) >= COMPARE_COMMIT_CAP:
        print(
            f"> **Note:** the compare API returned {len(commits)} commits, at or near its "
            f"{COMPARE_COMMIT_CAP} cap. This list may be incomplete.\n",
            file=sys.stderr,
        )
    return commits


def merged_prs(repo: str, base: str) -> list[dict]:
    """Recently merged PRs targeting `base`, newest first."""
    return gh_json(
        "pr", "list", "--repo", repo, "--state", "merged", "--base", base,
        "--limit", str(PR_LOOKBACK),
        "--json", "number,title,mergedAt,mergeCommit,url,author",
    )


def pr_commit_shas(repo: str, number: int) -> set[str]:
    """Every commit inside a PR, so its contents can be told apart from a direct push."""
    data = gh_json("pr", "view", str(number), "--repo", repo, "--json", "commits")
    return {c["oid"] for c in (data or {}).get("commits", [])}


# --- Notes ---


def build(repo: str, base: str, head: str, pr_base: str) -> str:
    range_commits = commits_in_range(repo, base, head)
    range_shas = {c["sha"] for c in range_commits}
    if not range_shas:
        return f"`{head}` has nothing that `{base}` does not. Nothing to promote."

    shipping = [
        pr for pr in merged_prs(repo, pr_base)
        if (pr.get("mergeCommit") or {}).get("oid") in range_shas
    ]
    shipping.sort(key=lambda p: p["mergedAt"])

    lines = [
        f"**{len(range_commits)} commits across {len(shipping)} pull requests** "
        f"will ship when `{head}` reaches `{base}`.",
        "",
    ]

    if shipping:
        lines.append("## What's shipping")
        lines.append("")
        for pr in shipping:
            author = (pr.get("author") or {}).get("login")
            # Attribution only when it is not the usual maintainer path — a fork
            # contribution is the case worth noticing in a release note.
            by = f" _(@{author})_" if author and author != "ty-fi" else ""
            lines.append(f"- [**#{pr['number']}**]({pr['url']}) — {pr['title']}{by}")
        lines.append("")

    # Anything in the range that no merged PR accounts for: pushed straight to the branch,
    # or merged from a PR older than the lookback. Either way a PR-based summary would
    # omit it silently, which is the failure mode this whole script is guarding against.
    explained = set()
    for pr in shipping:
        explained.add((pr.get("mergeCommit") or {}).get("oid"))
        explained |= pr_commit_shas(repo, pr["number"])
    unexplained = [c for c in range_commits if c["sha"] not in explained]

    if unexplained:
        lines.append(f"## Not covered by any merged PR — {len(unexplained)}")
        lines.append("")
        lines.append(
            "_Pushed directly, or from a PR older than the lookback window. Listed because "
            "a summary built only from PRs would otherwise leave these out silently._"
        )
        lines.append("")
        for c in unexplained[:20]:
            subject = c["commit"]["message"].splitlines()[0]
            lines.append(f"- `{c['sha'][:8]}` {subject}")
        if len(unexplained) > 20:
            lines.append(f"- …and {len(unexplained) - 20} more")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# --- Entry point ---


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--base", default="prod", help="the branch being deployed to")
    parser.add_argument("--head", default="main", help="the branch being promoted")
    parser.add_argument(
        "--pr-base", default=None,
        help="branch the component PRs were merged into (defaults to --head)",
    )
    args = parser.parse_args()
    if not args.repo:
        sys.exit("--repo is required (or set GITHUB_REPOSITORY)")

    # Force UTF-8 out. Python picks the console encoding on Windows, which is cp1252 here,
    # and the em-dashes in PR titles come out as raw 0x97 — invalid UTF-8, so the notes
    # arrive in a PR body as mojibake. Linux runners default to UTF-8 and would never show
    # this, which is precisely why it needs pinning rather than leaving to the platform.
    sys.stdout.reconfigure(encoding="utf-8")

    notes = build(args.repo, args.base, args.head, args.pr_base or args.head)
    print(notes)

    # Also render into the Actions run summary, so a workflow_dispatch preview is readable
    # without opening the log.
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(notes)


if __name__ == "__main__":
    main()
