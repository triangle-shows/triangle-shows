"""Pytest configuration for the backend suite: make `app` importable.

The tests import the code under test as `app.scrapers.…`, but `app` is a plain
directory inside `backend/` rather than an installed package, so it is only
importable when `backend/` is on `sys.path`. Every test module used to arrange that
for itself with a three-line preamble before its real imports.

That worked, but it made the suite quietly invocation-dependent. `python -m pytest`
puts the current directory on `sys.path` as a side effect of `-m`, so from `backend/`
the preamble was redundant and its absence went unnoticed; a bare `pytest tests`
adds no such entry, so a module that omitted the preamble failed at collection with
`ModuleNotFoundError: No module named 'app'`. CI uses the `python -m` form, which is
why it never surfaced there.

pytest imports the conftest.py files covering a test's directory before importing the
test modules themselves, so putting the insert here runs early enough for the whole
suite and stays correct however pytest is invoked -- and applies to new test modules
without them needing to remember anything.
"""

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
