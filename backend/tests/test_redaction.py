"""Regression tests for credential leakage out of the application.

The Ticketmaster Discovery API authenticates with a query parameter (``?apikey=``),
so every request URL the scraper builds carries a live credential. That URL escapes
the process by four independent routes, and a fix that closes one leaves the others
open:

1. ``httpx`` logs every request at INFO with the full query string, so a *healthy*
   scrape writes the key to the log store once per Ticketmaster venue per cycle.
2. ``httpx.HTTPStatusError`` embeds the request URL in its ``str()``, and
   ``manager.scrape_venue`` interpolates that into ``logger.error`` — so a *failed*
   scrape logs the key even with httpx itself silenced.
3. That same exception string is persisted to ``scrape_logs.error_message`` and
   returned in the per-venue result dict, which ``POST /api/scrape`` hands back.
4. An exception escaping a route is re-raised by Starlette's ``ServerErrorMiddleware``
   and logged by the ASGI server on a logger it configures with ``propagate=False``
   and its own handler, which no root handler ever sees.

Route 3 is why redaction cannot live in the logging layer alone: that sink is not a
log. Route 4 is why the logging half has to sweep every handler in the process rather
than root's. The shared helper is the contract; the formatter and the manager are its
installation points.
"""

import asyncio
import contextlib
import io
import logging
import threading

import httpx
import pytest

from app.redaction import RedactingFormatter, redact_credentials, redact_handler


# Shaped exactly like the URL the Ticketmaster scraper builds
# (app/scrapers/ticketmaster.py), with a placeholder standing in for the live key.
FAKE_KEY = "s3cr3tKEYvalue0123456789abcdefgh"
TM_URL = (
    "https://app.ticketmaster.com/discovery/v2/events.json"
    f"?apikey={FAKE_KEY}&venueId=KovZpZAdEEvA&size=200&page=0&sort=date%2Casc"
)


@pytest.fixture
def preserved_logging():
    """Snapshot and restore global logging state around a test that reconfigures it.

    ``app.main.configure_logging`` mutates process-wide state — root's level and
    handlers, and the level, handlers and formatter of every pre-existing logger.
    Without this, a test that trips it takes the rest of the session down with it.
    """
    root = logging.getLogger()
    saved_root_level = root.level
    saved_root_handlers = root.handlers[:]
    saved_formatters = [(handler, handler.formatter) for handler in root.handlers]
    saved_loggers = [
        (logger, logger.level, logger.disabled, logger.handlers[:], logger.propagate)
        for logger in logging.Logger.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    ]
    try:
        yield
    finally:
        root.setLevel(saved_root_level)
        root.handlers[:] = saved_root_handlers
        # configure_logging() swaps formatters in place on handlers it did not create,
        # so restoring the handler list alone would leave the new formatter installed.
        for handler, formatter in saved_formatters:
            handler.setFormatter(formatter)
        for logger, level, disabled, handlers, propagate in saved_loggers:
            logger.setLevel(level)
            logger.disabled = disabled
            logger.handlers[:] = handlers
            logger.propagate = propagate


def _tm_error() -> httpx.HTTPStatusError:
    """The exact exception httpx raises on a Ticketmaster 401, key and all."""
    request = httpx.Request("GET", TM_URL)
    return httpx.HTTPStatusError(
        f"Client error '401 Unauthorized' for url '{TM_URL}'",
        request=request,
        response=httpx.Response(401, request=request),
    )


# --- The shared helper ---


@pytest.mark.parametrize(
    "param",
    ["apikey", "api_key", "access_token", "token", "secret", "password", "signature", "sig"],
)
def test_credential_query_params_are_redacted(param):
    """Every parameter name we treat as a credential loses its value."""
    redacted = redact_credentials(f"https://example.test/x?{param}={FAKE_KEY}&venueId=1")
    assert FAKE_KEY not in redacted
    assert f"{param}=" in redacted, "the parameter name is kept — only the value is scrubbed"
    assert "venueId=1" in redacted


@pytest.mark.parametrize("param", ["APIKEY", "ApiKey", "Api_Key", "Access_Token"])
def test_redaction_is_case_insensitive(param):
    """Query-parameter casing varies across APIs; a case-sensitive match would miss them."""
    assert FAKE_KEY not in redact_credentials(f"https://example.test/x?{param}={FAKE_KEY}")


def test_non_credential_params_survive():
    """Redaction must not destroy the diagnostic value of a logged URL."""
    redacted = redact_credentials(TM_URL)
    assert FAKE_KEY not in redacted
    for kept in ("venueId=KovZpZAdEEvA", "size=200", "page=0", "sort=date%2Casc"):
        assert kept in redacted, f"{kept} is diagnostic, not secret, and should survive"
    assert "app.ticketmaster.com/discovery/v2/events.json" in redacted


def test_value_at_end_of_string_is_redacted():
    """No trailing ampersand to anchor on — the common shape when the key is the last param."""
    assert FAKE_KEY not in redact_credentials(f"https://example.test/x?venueId=1&apikey={FAKE_KEY}")


def test_redaction_stops_at_the_end_of_the_value_not_the_end_of_the_line():
    """A credential in the *last* query parameter must not swallow the text after the URL.

    This is httpx's exact log shape, and the case that separates a correct value pattern
    from a lazy one: with the key last, a pattern that only stops at `&` runs to the end
    of the record and eats the HTTP status — redacting far more than the secret. Nothing
    leaks either way, so only an assertion on the *surviving* text catches it.
    """
    line = (
        f"HTTP Request: GET https://app.ticketmaster.com/x?venueId=KovZpZAdEEvA&apikey={FAKE_KEY} "
        '"HTTP/1.1 200 OK"'
    )
    redacted = redact_credentials(line)

    assert FAKE_KEY not in redacted
    assert '"HTTP/1.1 200 OK"' in redacted, "redaction ran past the value and ate the status"
    assert "venueId=KovZpZAdEEvA" in redacted


def test_redaction_stops_at_a_closing_quote():
    """httpx.HTTPStatusError renders the URL inside single quotes; the closing quote is
    the only boundary when the credential is the final parameter."""
    line = f"Client error '401 Unauthorized' for url 'https://x.test/y?apikey={FAKE_KEY}'"
    redacted = redact_credentials(line)

    assert FAKE_KEY not in redacted
    assert redacted.endswith("'"), "the closing quote was consumed as part of the value"


def test_redaction_is_idempotent():
    """Applied at more than one layer, so double-application must not corrupt the text."""
    once = redact_credentials(TM_URL)
    assert redact_credentials(once) == once


def test_non_string_input_is_returned_unchanged():
    """Call sites pass exception objects and None; the helper must not raise on them."""
    assert redact_credentials(None) is None
    assert redact_credentials(42) == 42


# --- The logging installation point ---


def _capture(formatter, record):
    """Format one record through a handler carrying `formatter`, returning the output."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    handler.handle(record)
    return stream.getvalue()


def test_formatter_redacts_the_message():
    record = logging.LogRecord(
        name="httpx", level=logging.INFO, pathname=__file__, lineno=1,
        msg='HTTP Request: GET %s "HTTP/1.1 200 OK"', args=(TM_URL,), exc_info=None,
    )
    out = _capture(RedactingFormatter(logging.Formatter("%(message)s")), record)
    assert FAKE_KEY not in out
    assert "venueId=KovZpZAdEEvA" in out


def test_formatter_redacts_the_exception_traceback():
    """The manager logs failures by interpolation, but an exc_info=True call site puts the
    URL in the traceback text instead — where a message-only filter never looks. That is
    not hypothetical here: app.main.trigger_scrape logs its catch-all with exc_info=True.
    """
    try:
        raise _tm_error()
    except httpx.HTTPStatusError:
        import sys

        record = logging.LogRecord(
            name="app.scrapers.manager", level=logging.ERROR, pathname=__file__,
            lineno=1, msg="scrape failed", args=(), exc_info=sys.exc_info(),
        )

    out = _capture(RedactingFormatter(logging.Formatter("%(message)s")), record)
    assert "Traceback" in out, "the test is only meaningful if the traceback was rendered"
    assert FAKE_KEY not in out


def test_configure_logging_installs_the_formatter_on_every_root_handler(preserved_logging):
    """A handler without the formatter is an open sink; there is usually only one, but
    the assertion is over all of them so an added handler cannot silently bypass this."""
    from app.main import configure_logging

    configure_logging()
    root = logging.getLogger()
    assert root.handlers, "expected basicConfig to have installed at least one handler"
    for handler in root.handlers:
        assert isinstance(handler.formatter, RedactingFormatter), (
            f"{handler!r} would emit un-redacted output"
        )


def test_configure_logging_silences_httpx_request_logging(preserved_logging):
    """The primary emitter is switched off outright rather than left to the formatter.

    No regex should be the only thing between a live credential and a log store: httpx
    logs the full URL on every successful request, so the volume alone makes it the one
    source worth removing alongside redacting. Our own scrapers already log a
    per-request line ('[TM] Fetching page 0 for red-hat') carrying the venue, which is
    the part with diagnostic value.
    """
    from app.main import configure_logging

    logging.getLogger("httpx").setLevel(logging.NOTSET)
    configure_logging()

    # Assert on the logger's *own* level rather than isEnabledFor(INFO). An effective
    # level falls back to root's, and pytest installs its own root handlers before the
    # app is imported — which makes logging.basicConfig() a no-op, because the standard
    # library only applies its `level` argument when root has no handlers yet. Root
    # therefore sits at pytest's WARNING for the whole session, and isEnabledFor(INFO)
    # would report httpx silenced whether or not this pin exists. Equality (not <=) also
    # pins the level from above: a genuine httpx failure must still reach the log.
    assert logging.getLogger("httpx").level == logging.WARNING


def test_redact_handler_scrubs_without_changing_the_existing_format():
    """redact_handler *wraps* the handler's formatter rather than replacing it, so a
    handler owned by someone else (uvicorn installs its own DefaultFormatter and an
    AccessFormatter that renders from record args, not %(message)s) keeps its layout."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("PREFIX %(levelname)s :: %(message)s"))
    redact_handler(handler)

    logger = logging.getLogger("test_redact_handler_format")
    logger.propagate = False
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.info("fetching %s", TM_URL)

    out = buf.getvalue()
    assert out.startswith("PREFIX INFO :: "), "the wrapped formatter's layout was lost"
    assert FAKE_KEY not in out
    assert "venueId=KovZpZAdEEvA" in out, "only the credential value should be removed"


def test_redact_handler_is_idempotent():
    """configure_logging() runs at import and again in any test that calls it, so a
    second pass must not nest wrappers."""
    handler = logging.StreamHandler(io.StringIO())
    handler.setFormatter(logging.Formatter("%(message)s"))
    redact_handler(handler)
    first = handler.formatter
    redact_handler(handler)
    assert handler.formatter is first


# --- The ASGI server sink: a non-propagating logger no root handler can reach ---


@pytest.mark.parametrize(
    "server_logger, child_logger",
    [
        ("uvicorn", "uvicorn.error"),
        # gunicorn is named nowhere in app/**, which is the point: configure_logging
        # sweeps every handler in the process rather than a list of dependency names,
        # so swapping the server (`gunicorn -k UvicornWorker` is a deployment change
        # touching no application code) cannot silently reopen the sink. A test that
        # only ever reached for "uvicorn" would share the production code's blind spot
        # instead of checking it.
        ("gunicorn", "gunicorn.error"),
    ],
)
def test_configure_logging_closes_any_servers_traceback_sink(
    preserved_logging, server_logger, child_logger
):
    """Starlette's ServerErrorMiddleware ALWAYS re-raises after invoking the
    bare-Exception handler, so the ASGI server logs the traceback itself on its own
    error logger. That logger's parent is configured with propagate=False and its own
    handler, so nothing it writes ever passes a root handler. Without the sweep, an
    httpx.HTTPStatusError escaping any route writes a live ?apikey= in full.
    """
    from app.main import configure_logging

    server = logging.getLogger(server_logger)
    # Restored by hand rather than leaning on preserved_logging alone: that fixture
    # snapshots the loggers existing when it runs, and `gunicorn` is created by this
    # test, so it would otherwise leak its handler into the rest of the session.
    original_handlers = server.handlers
    original_propagate = server.propagate

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    server.handlers = [handler]
    server.propagate = False

    try:
        configure_logging()

        try:
            raise _tm_error()
        except httpx.HTTPStatusError as exc:
            logging.getLogger(child_logger).error(
                "Exception in ASGI application\n", exc_info=exc
            )

        out = buf.getvalue()
    finally:
        server.handlers = original_handlers
        server.propagate = original_propagate

    assert "Exception in ASGI application" in out, "the test did not exercise the handler"
    assert FAKE_KEY not in out, f"{server_logger}'s own handler leaked the credential"
    assert "venueId=KovZpZAdEEvA" in out, "the diagnosis itself must survive"


def test_configure_logging_survives_concurrent_logger_creation(preserved_logging):
    """configure_logging sweeps every logger in the process, and the registry it reads
    is a live dict that logging.getLogger() writes into. Walking it lazily meant any
    thread creating a logger mid-sweep could raise "dictionary changed size during
    iteration" — out of configure_logging, which runs at import, so the failure mode is
    the app not booting at all.

    Stress rather than a fixed scenario, because the window is a thread switch: this
    can only ever miss the bug, never report one that isn't there, so a pass is always
    legitimate. The round count is set where the unsnapshotted version failed on every
    one of eight consecutive runs while the whole module still finished in about a
    second.
    """
    from app.main import configure_logging

    registry = logging.root.manager.loggerDict
    # logging.Manager.getLogger mutates the registry while holding the module lock, and
    # Manager._clear_cache iterates it lazily under that same lock on exactly that
    # assumption — so a churn thread that skipped the lock would provoke a RuntimeError
    # inside the standard library instead of inside the sweep, and this test would fail
    # for a reason that has nothing to do with the code under test. Falling back to a
    # no-op context manager keeps the test honest rather than erroring if a future
    # release renames the private lock: the sweep is still exercised concurrently.
    registry_lock = getattr(logging, "_lock", None) or contextlib.nullcontext()
    names = [f"_race_probe_{n}" for n in range(500)]
    stop = threading.Event()
    failure: list[BaseException] = []

    def churn() -> None:
        # Adds and removes within a fixed name pool rather than creating new loggers
        # without bound: the registry's *size* has to change to provoke the bug, but it
        # must not grow, or the sweep under test gets slower every round and the whole
        # suite crawls. PlaceHolder is what getLogger itself stores for intermediate
        # names, so this is the shape the real registry churns through.
        index = 0
        while not stop.is_set():
            name = names[index % len(names)]
            with registry_lock:
                if name in registry:
                    registry.pop(name, None)
                else:
                    registry[name] = logging.PlaceHolder(logging.getLogger())
            index += 1

    churner = threading.Thread(target=churn, daemon=True)
    churner.start()
    try:
        for _ in range(3000):
            try:
                configure_logging()
            except RuntimeError as exc:  # pragma: no cover - the bug this pins
                failure.append(exc)
                break
    finally:
        stop.set()
        churner.join(timeout=5)
        for name in names:
            registry.pop(name, None)

    assert not failure, f"configure_logging raced with logger creation: {failure[0]!r}"


# --- The non-logging sinks ---


class _StubSession:
    """The narrow slice of AsyncSession that scrape_venue's failure path touches.

    Enough to reach the `except` block without a database, so this runs in CI's unit
    test step — which executes before `alembic upgrade head`, and therefore against a
    schema that does not exist yet.
    """

    def __init__(self):
        self.added = []

    async def refresh(self, obj):
        return None

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None

    async def rollback(self):
        return None

    async def commit(self):
        return None


class _StubVenue:
    slug = "red-hat"
    id = 1
    scraper_type = "ticketmaster"
    ticketmaster_venue_id = "KovZpZAdEEvA"
    scraper_config = None


def test_a_failed_scrape_neither_persists_nor_returns_the_credential():
    """manager.scrape_venue's `except` block feeds three sinks off one exception string:
    a log line, scrape_logs.error_message, and the per-venue dict that POST /api/scrape
    returns. Redaction happens once, above all three."""
    from app.scrapers.manager import ScrapeManager

    class ExplodingScraper:
        async def scrape(self):
            raise _tm_error()

    session = _StubSession()
    manager = ScrapeManager(session)
    manager._get_scraper = lambda venue: ExplodingScraper()

    result = asyncio.run(manager.scrape_venue(_StubVenue()))

    assert result["status"] == "failed"
    assert FAKE_KEY not in result["error"], "the response body leaked the credential"
    assert "401 Unauthorized" in result["error"], "the diagnosis itself must survive"

    logs = [row for row in session.added if hasattr(row, "error_message")]
    assert logs, "expected the failure to be recorded on a ScrapeLog"
    assert all(FAKE_KEY not in (row.error_message or "") for row in logs), (
        "the credential was persisted to scrape_logs.error_message"
    )
    assert any("401 Unauthorized" in (row.error_message or "") for row in logs)


def test_trigger_scrape_catch_all_redacts_the_credential(monkeypatch):
    """POST /api/scrape has its own `except Exception`, separate from scrape_venue's —
    it fires on failures *outside* scrape_venue (session construction, a scraper import)
    that never pass through the manager's redaction, and it puts str(e) straight into
    the response body. Forcing the session factory itself to raise reaches exactly that
    branch.

    The endpoint is unauthenticated unless SCRAPE_ALLOWED_SERVICE_ACCOUNTS names an
    account, so on a default deployment this hands the body to any caller.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    def _raising_session_factory():
        raise _tm_error()

    # trigger_scrape imports async_session inside the function body, so the name is
    # resolved from the module at call time and patching it here takes effect.
    monkeypatch.setattr("app.database.async_session", _raising_session_factory)

    response = TestClient(app).post("/api/scrape")

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert FAKE_KEY not in detail, "the unauthenticated endpoint leaked the credential"
    assert "401 Unauthorized" in detail, "the diagnosis itself must survive"
