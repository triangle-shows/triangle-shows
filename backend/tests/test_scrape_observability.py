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

import asyncio
import logging

from fastapi.routing import APIRoute

import httpx
import pytest

from tests.helpers import StubSession, StubVenue, scrape_logs

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


class TestFailedScrapesSayWhatWentWrong:
    """A failed scrape must not describe itself as the empty string.

    Issue #15: ``{"venue":"the-pinhook","status":"failed","error":""}``. The three sinks
    in ``scrape_venue``'s except block — the log line, ``scrape_logs.error_message`` and
    the dict returned to ``POST /api/scrape`` — all reported ``str(e)``, and ``str()`` is
    empty for every httpx transport error and for the bare ``IndexError`` /
    ``AttributeError`` a parser throws. Those are the two most common ways a scrape fails,
    so the reporting went blank exactly when it was needed.

    #79 was masking this: the log line never arrived at all. Fixing that (PR #88) makes
    the line arrive, still empty after the colon — which is why this needed its own fix.
    """

    @staticmethod
    def _failed_scrape(exc: BaseException) -> tuple[dict, list]:
        """Run scrape_venue against a scraper that raises `exc`; return (result, logs)."""
        class ExplodingScraper:
            async def scrape(self):
                raise exc

        session = StubSession()
        manager = ScrapeManager(session)
        manager._get_scraper = lambda venue: ExplodingScraper()

        result = asyncio.run(manager.scrape_venue(StubVenue()))
        return result, scrape_logs(session)

    # The real #15 shapes. httpx raises these with no message on a bare timeout, and a
    # parser raises the builtins with no message when markup changes underneath it.
    SILENT_FAILURES = [
        httpx.ConnectTimeout(""),
        httpx.ReadTimeout(""),
        httpx.ConnectError(""),
        httpx.RemoteProtocolError(""),
        IndexError(),
        AttributeError(),
    ]

    @pytest.mark.parametrize("exc", SILENT_FAILURES, ids=lambda e: type(e).__name__)
    def test_the_response_body_names_the_failure(self, exc):
        result, _ = self._failed_scrape(exc)
        assert result["status"] == "failed"
        assert result["error"] == type(exc).__name__

    @pytest.mark.parametrize("exc", SILENT_FAILURES, ids=lambda e: type(e).__name__)
    def test_the_scrape_log_row_names_the_failure(self, exc):
        """The row is the only durable record — the one you read when asking why a venue
        stopped updating three days ago."""
        _, logs = self._failed_scrape(exc)
        assert logs, "expected the failure to be recorded on a ScrapeLog"
        assert all(row.error_message == type(exc).__name__ for row in logs)

    @pytest.mark.parametrize("exc", SILENT_FAILURES, ids=lambda e: type(e).__name__)
    def test_nothing_reports_an_empty_error(self, exc):
        """The literal regression. Asserted separately from the shape above so that a
        future change to the format still cannot reintroduce a blank."""
        result, logs = self._failed_scrape(exc)
        assert result["error"].strip() != ""
        assert all((row.error_message or "").strip() != "" for row in logs)

    def test_an_exception_with_a_message_keeps_it(self):
        """Naming the class must not come at the cost of the message when there is one."""
        result, _ = self._failed_scrape(ValueError("nav.eventlist returned no rows"))
        assert result["error"] == "ValueError: nav.eventlist returned no rows"

    def test_the_traceback_is_logged_but_not_persisted(self, caplog):
        """A traceback is what separates "the venue is down" from "our parser broke", so
        the log line needs one. It must not reach the database column or the response
        body, which are single-line fields read by people and by the admin UI.
        """
        with caplog.at_level(logging.ERROR, logger="app.scrapers.manager"):
            result, logs = self._failed_scrape(AttributeError())

        records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert records, "the failure was not logged at ERROR"
        assert any(r.exc_info for r in records), (
            "no traceback attached — exc_info=True is what makes a parser break diagnosable"
        )
        assert "Traceback" not in result["error"]
        assert all("Traceback" not in (row.error_message or "") for row in logs)
