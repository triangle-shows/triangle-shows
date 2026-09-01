"""Tests for .github/scripts/release_notes.py.

That script builds the body of a main -> prod promotion PR. A promotion carries every
commit merged to `main` since the last one — 29 across 10 PRs when it was written — so a
summary written by hand from `git log` describes what the author remembers rather than what
is shipping. Its first derived run surfaced four merged security fixes from a fork that
nobody had mentioned.

The assertion worth having is the matching rule: PRs are identified by **merge commit SHA**,
never by grepping `#\\d+` out of commit subjects. Subject-grepping looks equivalent and is
not — it silently misses squash-merged PRs, which leave no "Merge pull request #N" line, and
it falsely claims any commit whose message happens to mention an issue number.

Loaded by path because the script lives under .github/, outside the package the rest of the
suite imports.
"""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2] / ".github" / "scripts" / "release_notes.py"
)


@pytest.fixture(scope="module")
def notes():
    spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _commit(sha: str, subject: str) -> dict:
    return {"sha": sha, "commit": {"message": subject}}


def _pr(number: int, title: str, merge_sha: str, merged_at: str, author="ty-fi") -> dict:
    return {
        "number": number,
        "title": title,
        "mergedAt": merged_at,
        "mergeCommit": {"oid": merge_sha},
        "url": f"https://github.com/o/r/pull/{number}",
        "author": {"login": author},
    }


@pytest.fixture
def fake_api(notes, monkeypatch):
    """Stub the three GitHub calls, so `build()` is exercised without a network."""

    def install(range_commits, prs, pr_commits=None):
        pr_commits = pr_commits or {}
        monkeypatch.setattr(notes, "commits_in_range", lambda *a, **k: range_commits)
        monkeypatch.setattr(notes, "merged_prs", lambda *a, **k: prs)
        monkeypatch.setattr(
            notes, "pr_commit_shas", lambda repo, n: pr_commits.get(n, set())
        )

    return install


class TestMatchingRule:
    def test_a_pr_is_matched_by_its_merge_commit(self, notes, fake_api):
        fake_api(
            [_commit("aaa", "Merge pull request #42 from o/feature")],
            [_pr(42, "Add a thing", "aaa", "2026-09-01T10:00:00Z")],
        )
        out = notes.build("o/r", "prod", "main", "main")
        assert "**#42**" in out
        assert "Add a thing" in out

    def test_a_squash_merged_pr_is_still_matched(self, notes, fake_api):
        """The case subject-grepping gets wrong. A squash merge leaves an ordinary commit
        subject with no "Merge pull request #N" in it, so only the SHA identifies it."""
        fake_api(
            [_commit("bbb", "Add a thing")],  # no PR number anywhere in the subject
            [_pr(42, "Add a thing", "bbb", "2026-09-01T10:00:00Z")],
        )
        assert "**#42**" in notes.build("o/r", "prod", "main", "main")

    def test_a_commit_merely_mentioning_a_number_is_not_credited(self, notes, fake_api):
        """The other half of the same rule. "Fixes #42" in a message must not make #42
        appear in the release notes when its merge commit is not in the range."""
        fake_api(
            [_commit("ccc", "Refactor the parser. Fixes #42")],
            [_pr(42, "Something else entirely", "zzz", "2026-09-01T10:00:00Z")],
        )
        out = notes.build("o/r", "prod", "main", "main")
        assert "**#42**" not in out
        assert "Something else entirely" not in out

    def test_a_pr_outside_the_range_is_excluded(self, notes, fake_api):
        """An already-promoted PR must not reappear in the next release's notes."""
        fake_api(
            [_commit("aaa", "in range")],
            [
                _pr(1, "Already shipped", "old", "2026-08-01T10:00:00Z"),
                _pr(2, "Shipping now", "aaa", "2026-09-01T10:00:00Z"),
            ],
        )
        out = notes.build("o/r", "prod", "main", "main")
        assert "Already shipped" not in out
        assert "Shipping now" in out


class TestUnexplainedCommits:
    def test_a_direct_push_is_reported(self, notes, fake_api):
        """The failure this script exists to prevent, in miniature: a PR-only summary would
        omit a commit pushed straight to the branch, and omit it silently."""
        fake_api(
            [_commit("aaa", "Merge pull request #42 from o/f"), _commit("ddd", "hotfix typo")],
            [_pr(42, "A thing", "aaa", "2026-09-01T10:00:00Z")],
            {42: set()},
        )
        out = notes.build("o/r", "prod", "main", "main")
        assert "Not covered by any merged PR" in out
        assert "hotfix typo" in out
        assert "ddd"[:8] in out

    def test_commits_inside_a_pr_are_not_reported_as_direct_pushes(self, notes, fake_api):
        """Each PR's own commits are in the range too. Without asking for them, every
        component commit would be listed as an unexplained direct push."""
        fake_api(
            [
                _commit("aaa", "Merge pull request #42 from o/f"),
                _commit("eee", "first commit inside the PR"),
                _commit("fff", "second commit inside the PR"),
            ],
            [_pr(42, "A thing", "aaa", "2026-09-01T10:00:00Z")],
            {42: {"eee", "fff"}},
        )
        out = notes.build("o/r", "prod", "main", "main")
        assert "Not covered by any merged PR" not in out


class TestPresentation:
    def test_an_empty_range_says_so_rather_than_rendering_an_empty_list(self, notes, fake_api):
        fake_api([], [])
        out = notes.build("o/r", "prod", "main", "main")
        assert "Nothing to promote" in out
        assert "What's shipping" not in out

    def test_the_counts_are_stated(self, notes, fake_api):
        fake_api(
            [_commit("aaa", "m"), _commit("bbb", "x")],
            [_pr(1, "A", "aaa", "2026-09-01T10:00:00Z")],
            {1: {"bbb"}},
        )
        out = notes.build("o/r", "prod", "main", "main")
        assert "2 commits across 1 pull requests" in out

    def test_ordering_is_by_merge_time(self, notes, fake_api):
        """Reading order should match the order things landed, not the API's."""
        fake_api(
            [_commit("a1", "m"), _commit("a2", "m")],
            [
                _pr(9, "Later", "a2", "2026-09-02T10:00:00Z"),
                _pr(8, "Earlier", "a1", "2026-09-01T10:00:00Z"),
            ],
        )
        out = notes.build("o/r", "prod", "main", "main")
        assert out.index("Earlier") < out.index("Later")

    def test_a_fork_contributor_is_credited(self, notes, fake_api):
        """Attribution is the detail that makes an outside contribution visible in a release
        note — the four fork security fixes were the thing a hand-written summary missed."""
        fake_api(
            [_commit("aaa", "m")],
            [_pr(74, "A security fix", "aaa", "2026-09-01T10:00:00Z", author="someone")],
        )
        assert "_(@someone)_" in notes.build("o/r", "prod", "main", "main")

    def test_the_maintainer_is_not_credited_on_every_line(self, notes, fake_api):
        """Crediting the usual author on all ten lines is noise that hides the one line
        where attribution matters."""
        fake_api(
            [_commit("aaa", "m")],
            [_pr(88, "A routine fix", "aaa", "2026-09-01T10:00:00Z", author="ty-fi")],
        )
        assert "_(@" not in notes.build("o/r", "prod", "main", "main")
