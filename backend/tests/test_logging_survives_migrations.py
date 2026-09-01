"""
Tests that running Alembic in-process does not dismantle the app's logging.

main.py applies migrations from inside the lifespan handler, which executes
alembic/env.py in this same process. env.py configures logging from alembic.ini, and
`logging.config.fileConfig` defaults to ``disable_existing_loggers=True`` — so that call
used to disable every ``app.*`` logger and replace root's handlers. The observable
result in production was that everything logged before migrations reached Cloud Logging
and nothing logged after them ever did: no "Migrations applied", no per-request lines,
and — across a period containing hundreds of HTTP 500s — not one entry at ERROR
severity.

Two properties are pinned here, because the same call broke two unrelated things:

* ``app.*`` loggers stay enabled and keep their level, so anything logged after
  migrations is still emitted at all (issue #79).
* Root's handlers stay wrapped by ``redact_handler``, so the Ticketmaster credential
  redaction added in #77 keeps applying. ``fileConfig`` installs alembic.ini's own
  ``StreamHandler`` on root; an unwrapped handler is a reopened sink. #77's docstring
  names this exact residue — "handlers installed *after* this runs are not covered" —
  and Alembic is a dependency that installs one lazily.

The hostile call is reproduced against the real ``backend/alembic.ini`` rather than a
fixture, so a change to that file's logger sections is caught here.
"""

import logging
import logging.config
from pathlib import Path

import pytest

from app.main import configure_logging
from app.redaction import RedactingFormatter


ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def _root_handlers_are_wrapped() -> bool:
    """True when every handler on root carries #77's redaction wrapper.

    redact_handler wraps by swapping in a RedactingFormatter and returns None, so the
    presence of that formatter is the only readable signal — which is also exactly what
    redact_handler itself checks to stay idempotent.
    """
    root = logging.getLogger()
    if not root.handlers:
        return False
    return all(isinstance(h.formatter, RedactingFormatter) for h in root.handlers)


@pytest.fixture
def restore_logging():
    """Snapshot and restore global logging state, so these tests cannot leak."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_disabled = {
        name: getattr(obj, "disabled", None)
        for name, obj in logging.root.manager.loggerDict.items()
        if isinstance(obj, logging.Logger)
    }
    yield
    root.handlers = saved_handlers
    root.setLevel(saved_level)
    for name, was_disabled in saved_disabled.items():
        obj = logging.root.manager.loggerDict.get(name)
        if isinstance(obj, logging.Logger) and was_disabled is not None:
            obj.disabled = was_disabled


class TestAlembicIniIsStillHostile:
    """Guards the premise. If alembic.ini stops disabling loggers on its own, the
    assertions below would pass for the wrong reason and this file would be testing
    nothing."""

    def test_ini_exists(self):
        assert ALEMBIC_INI.is_file(), f"expected {ALEMBIC_INI}"

    def test_raw_fileconfig_still_disables_app_loggers(self, restore_logging):
        """The unguarded call, as env.py used to make it."""
        configure_logging()
        app_logger = logging.getLogger("app.main")
        assert not app_logger.disabled

        logging.config.fileConfig(str(ALEMBIC_INI))

        assert app_logger.disabled, (
            "alembic.ini no longer disables existing loggers — the guard in "
            "alembic/env.py may now be unnecessary, but check before removing it"
        )


class TestLoggingSurvivesAnInProcessMigration:
    def test_app_loggers_stay_enabled_and_leveled(self, restore_logging):
        """#79: the lines after 'Migrations applied' have to be emitted at all."""
        configure_logging()
        logging.config.fileConfig(str(ALEMBIC_INI), disable_existing_loggers=False)
        configure_logging()  # what main.py now does after _run_migrations()

        app_logger = logging.getLogger("app.main")
        assert not app_logger.disabled
        assert app_logger.getEffectiveLevel() <= logging.INFO, (
            "alembic.ini pins [logger_root] to WARN; app loggers inherit from root, so "
            "re-enabling them is not enough on its own"
        )

    def test_root_handlers_are_re_wrapped_for_redaction(self, restore_logging):
        """#77: alembic.ini installs its own StreamHandler on root. Unwrapped, that is a
        credential sink that reopened silently after startup."""
        configure_logging()
        assert _root_handlers_are_wrapped()

        logging.config.fileConfig(str(ALEMBIC_INI), disable_existing_loggers=False)
        configure_logging()

        assert _root_handlers_are_wrapped(), (
            "root carries a handler that redact_handler has not wrapped"
        )

    def test_a_record_logged_after_migrations_reaches_a_handler(self, restore_logging):
        """End to end rather than by attribute: the point is that the record arrives.

        Deliberately not caplog. fileConfig replaces root's handlers, which removes the
        one caplog installed — so caplog reports nothing even when the record is being
        emitted perfectly well to stderr. That false negative is worth naming: it is the
        same shape as the original bug, a sink disappearing rather than a record going
        missing.
        """
        configure_logging()
        logging.config.fileConfig(str(ALEMBIC_INI), disable_existing_loggers=False)
        configure_logging()

        seen = []

        class Capture(logging.Handler):
            def emit(self, record):
                seen.append(self.format(record))

        sink = Capture()
        logging.getLogger().addHandler(sink)
        try:
            logging.getLogger("app.main").info("Migrations applied")
        finally:
            logging.getLogger().removeHandler(sink)

        assert any("Migrations applied" in line for line in seen)
