"""Scrub credential-bearing query parameters out of text before it leaves the process.

Role: shared by every sink a request URL can escape through — :func:`redact_handler`,
which ``app.main.configure_logging`` applies to every log handler in the process
(covering messages, exception tracebacks, and the handlers owned by whatever ASGI
server is running, which log unhandled-request tracebacks on loggers that set
``propagate=False`` and so are invisible to any root handler), and the non-logging
sinks: the ``scrape_logs.error_message`` column and the per-venue result dict in
``app.scrapers.manager.scrape_venue``, plus the ``detail`` of the 500 that
``app.main.trigger_scrape`` raises — ``POST /api/scrape`` is unauthenticated unless
``SCRAPE_ALLOWED_SERVICE_ACCOUNTS`` is set.

Why this exists: the Ticketmaster Discovery API authenticates with a query parameter
rather than a header, so the scraper's request URL *is* a credential. ``httpx`` logs
that URL in full at INFO on every request, and ``httpx.HTTPStatusError`` embeds it in
its ``str()`` — which the scrape manager interpolates into a log line, persists to the
database, and returns to the caller. Redacting in one place would have closed one
route out of four.

Call :func:`describe_exception` rather than ``redact_credentials(str(exc))`` when the
text being emitted *is* an exception. It applies the same scrubbing and additionally
names the exception class, without which the majority of scrape failures describe
themselves as the empty string.

This is the backstop, not the primary defense. ``configure_logging`` also pins the
``httpx`` logger to WARNING so the high-volume emitter is switched off outright: a
denylist of parameter names should not be the only thing standing between a live
credential and a log store.

Requires: nothing outside the standard library — deliberately import-light so any
layer can call it without a dependency cycle.
"""

import logging
import re

# --- Redaction ---

REDACTED = "<redacted>"

# Parameter names whose values are credentials. Matched as a *suffix* behind an optional
# `[\w-]*` prefix, so `csrf_token`, `x-api-key` and `client_secret` are all caught rather
# than requiring an exact spelling. `sig` is the one exact-only entry: as a suffix it
# would fire on innocuous names, and the lookbehind is what keeps it from matching inside
# a longer word.
#
# This is a denylist, and a denylist is never complete. It covers what this codebase
# actually sends; when a new scraper authenticates with an unlisted parameter name, add
# it here and to the parametrized test rather than relying on the entry that happens to
# be closest.
_CREDENTIAL_QUERY_RE = re.compile(
    r"(?i)"
    r"(?<![\w-])"
    r"((?:[\w-]*(?:api[-_]?key|access[-_]?token|token|secret|password|signature)|sig)=)"
    # A value runs to the next parameter separator or to whatever delimiter the
    # surrounding text uses — a quote in an exception message, an angle bracket in
    # rendered markup, or the end of the string.
    r"([^&\s\"'<>]+)"
)


def redact_credentials(text):
    """Replace credential query-parameter values in `text`, leaving everything else intact.

    Non-string input is returned unchanged so call sites can pass an exception object or
    ``None`` without guarding first.

    Idempotent by construction: the placeholder opens with ``<``, which the value pattern
    excludes, so a second pass finds nothing left to match. That matters because the
    formatter and the manager both apply this, and a failed Ticketmaster scrape goes
    through both.

    Only the *value* is removed — the parameter name and every non-credential parameter
    survive, so a redacted URL still says which venue was being fetched and with what
    paging. A log line that has been scrubbed into uselessness gets replaced by one that
    isn't.
    """
    if not isinstance(text, str):
        return text
    # Group 1 of the pattern always ends in a literal `=`, so text without one cannot
    # match — a provable short-circuit, not a heuristic. Worth the line because the
    # pattern is deliberately anchorless (a leading `(?i)`, an alternation, and a
    # lookbehind leave the engine no literal prefix to seek on), so it costs roughly
    # 50ns per character and retries at every position. This now runs over every line
    # every handler in the process renders, tracebacks included; the miss case is the
    # overwhelmingly common one.
    if "=" not in text:
        return text
    return _CREDENTIAL_QUERY_RE.sub(rf"\g<1>{REDACTED}", text)


def describe_exception(exc):
    """Render `exc` as a one-line, credential-free description that is never empty.

    ``str(exception)`` returns the exception's *message*, and a large class of exceptions
    carry none — every httpx transport error (``ReadTimeout``, ``ConnectTimeout``,
    ``ConnectError``, ``RemoteProtocolError``) plus the bare ``IndexError`` /
    ``AttributeError`` / ``KeyError`` shapes a parser throws. All stringify to ``""``.
    Those are also the *most common* ways a scrape fails, so the sinks that reported
    ``str(e)`` reported nothing precisely when a venue went down or its markup changed
    (issue #15: ``{"venue":"the-pinhook","status":"failed","error":""}``).

    Prefixing the class name fixes that, and is worth doing even when a message exists:
    ``ValueError: No scraper available for x`` says more than the message alone, and a
    uniform ``Type: message`` shape is what makes a log store greppable by failure kind.
    The name is omitted-from-nothing — ``type(exc).__name__`` is always populated — so
    the result cannot be blank however little the exception carries.

    Redaction is folded in rather than left to the caller. Every sink that wants an
    exception description also needs it scrubbed, and the two-call version is one a
    future call site can half-remember; see the module docstring for what leaks when it
    does. Idempotent, because :func:`redact_credentials` is.

    Traceback text is deliberately *not* included. This return value goes to a database
    column, an API response body and a log line; only the log line should carry a
    traceback, and it gets one from ``exc_info=True`` at the call site, where the
    formatter can scrub it.
    """
    message = redact_credentials(str(exc))
    name = type(exc).__name__
    return f"{name}: {message}" if message else name


# --- Logging installation point ---


class RedactingFormatter(logging.Formatter):
    """Scrubs credentials from whatever another formatter rendered, keeping its layout.

    Deliberately a formatter rather than a ``logging.Filter``. A filter sees
    ``record.msg`` and ``record.args`` before they are combined, and never sees the
    rendered exception traceback at all — so an ``exc_info=True`` call site would walk
    straight past it carrying the URL in the traceback text. Formatting is the single
    point where message, arguments, and traceback have all become one string.

    Deliberately *wraps* an inner formatter rather than rendering from its own format
    string. Most handlers this runs against belong to somebody else — uvicorn installs
    its own ``DefaultFormatter``/``AccessFormatter``, and a deployment may install a
    structured/JSON handler on the root logger — and replacing their formatter outright
    would silently reshape those log lines (the access log in particular renders from
    record *args*, not from a plain ``%(message)s``). Wrapping keeps every handler's
    output byte-identical apart from the credential values, which means the same
    mechanism is safe to apply to every handler in the process without first deciding
    who owns it. Attach to *handlers*, not to loggers: a formatter belongs to a handler
    by design, and the handler is what every propagated record passes through.
    """

    def __init__(self, inner: logging.Formatter) -> None:
        super().__init__()
        self._inner = inner

    def format(self, record: logging.LogRecord) -> str:
        return redact_credentials(self._inner.format(record))


def redact_handler(handler: logging.Handler) -> None:
    """Ensure `handler` scrubs credentials, leaving its existing format alone.

    Idempotent: a handler already carrying a :class:`RedactingFormatter` is left as-is,
    so repeated ``configure_logging()`` calls (import, then a test) don't nest wrappers.
    """
    existing = handler.formatter
    if isinstance(existing, RedactingFormatter):
        return
    handler.setFormatter(RedactingFormatter(existing or logging.Formatter()))
