"""pytest configuration — isolate tests from prod data.

Sets FIO_DB_PATH to a per-session temp file so `services.db` writes
nowhere near `data/fio.db`. Imports are deferred so the env var is
in effect before any module touches the filesystem.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Ensure project root is importable as `services.*`, `routes.*`
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Redirect SQLite + audit log + caches to a tmp dir
_TMP = Path(tempfile.mkdtemp(prefix="fio-tests-"))
os.environ.setdefault("FIO_DB_PATH", str(_TMP / "fio.db"))
os.environ.setdefault("FIO_DATA_DIR", str(_TMP))
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")


import pytest


@pytest.fixture(scope="session", autouse=True)
def _init_test_db():
    """Create the full schema in the isolated FIO_DB_PATH database once per
    session. Without this, tests that INSERT (card_audit imports, etc.) hit a
    table that either doesn't exist or is missing the latest migrated columns.
    Now that config.DB_PATH honours FIO_DB_PATH, this targets the temp DB, not
    the operator's data/fio.db."""
    from services import db
    db.init_db()
    yield
