"""
Tests for the observability gaps that let a broken release look healthy.

Background. The 2026-08-27 release shipped the live-music filter classifying nothing, and
it took manual API probing to notice, because every signal that would have shown it was
either absent or unreadable:

  * the reclassify pass reported itself only to stdout, and the work ran in a detached
    asyncio task that Cloud Run starves of CPU once startup finishes, so nothing was
    flushed — the whole scrape produced zero log lines
  * _startup_scrape() caught every exception into a one-line WARNING with no traceback
  * POST /api/scrape returned per-venue counts and said nothing about classification

These assertions cover the parts reachable without a database. The route and manager
contracts are what a caller depends on; the Cloud Run CPU behavior is not testable here
and is addressed in cloudbuild.yaml instead.
"""

from fastapi.routing import APIRoute

import pytest

from app.main import app
from app.scrapers.manager import ScrapeManager


def _route(path: str) -> APIRoute:
    for r in app.routes:
        if isinstance(r, APIRoute) and r.path == path:
            return r
    raise AssertionError(f"no route for {path}")


class TestHealthAcceptsHead:
    """UptimeRobot sends HEAD. A GET-only route answers 404, so the monitor was recording
    a failure on every check against a healthy service — verified against production logs.
    """

    def test_head_is_accepted(self):
        assert "HEAD" in _route("/api/health").methods

    def test_get_still_works(self):
        assert "GET" in _route("/api/health").methods

    def test_no_other_method_was_opened_up(self):
        """Widening to HEAD must not have widened to writes."""
        assert _route("/api/health").methods <= {"GET", "HEAD"}


class TestScrapeReportsReclassification:
    def test_a_fresh_manager_has_no_reclassify_result(self):
        """None rather than an empty dict, so "did not run" is distinguishable from
        "ran and changed nothing" — the exact distinction that was unavailable while
        diagnosing the release."""
        assert ScrapeManager(session=None).last_reclassify is None

    def test_the_attribute_exists_before_any_scrape(self):
        """The endpoint reads it unconditionally; if it were only set inside scrape_all,
        an early failure would turn a useful response into an AttributeError."""
        assert hasattr(ScrapeManager(session=None), "last_reclassify")


class TestStartupScrapeLogsFailuresLoudly:
    """The handler swallows every failure in the scrape and reclassify path, so what it
    logs is the only evidence that anything went wrong."""

    @pytest.fixture
    def source(self):
        import inspect

        from app import main

        return inspect.getsource(main._startup_scrape)

    def test_failures_are_logged_at_error(self, source):
        assert "logger.error" in source
        assert "logger.warning" not in source

    def test_the_traceback_is_included(self, source):
        assert "exc_info=True" in source

    def test_the_reclassify_result_is_logged(self, source):
        assert "last_reclassify" in source
