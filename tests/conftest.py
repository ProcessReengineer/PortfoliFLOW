# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Suite-wide hooks: the test database, its migration and the run lock.

Importing this module points ``DATABASE_URL`` and ``DATABASE_URL_SUPERUSER``
at the RAM-held test database of ``compose.test.yml``, never the development
database (``tests/_test_database.py``, ADR-0135). It runs before any test
module is imported, so every module sees only the test database.

A listener on every SQLAlchemy pool calls :meth:`tests._run_lock.RunLock.ensure`
whenever a connection is opened. The first call takes the run lock; while
another run holds it, the call raises, the connection is never handed out,
and pytest stops after the current test with the refusal as its last line.
Once the lock is held, the same listener migrates the test database to
Alembic ``head`` (once per run); a failed migration stops the run the same way.

Hooks only, no fixtures: a database-free run never opens a connection, so it
never takes the lock and pays nothing for it. Rationale and scope:
``tests/_run_lock.py`` and ``docs/testing.md`` ("The database").
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.pool import Pool

from tests._run_lock import RunLock, RunLockRefused, database_url
from tests._test_database import HeadMigration, TestDatabaseError, select_test_database

_ROOT = Path(__file__).resolve().parents[1]

try:
    select_test_database(_ROOT)
except TestDatabaseError as error:
    raise pytest.UsageError(str(error)) from error

_LOCK = pytest.StashKey[RunLock]()
_LISTENER = pytest.StashKey[Callable[[Any, Any], None]]()
_SESSION = pytest.StashKey[pytest.Session]()


def pytest_configure(config: pytest.Config) -> None:
    lock = RunLock(database_url(config.rootpath))
    migration = HeadMigration(config.rootpath)

    def _on_connect(dbapi_connection: Any, connection_record: Any) -> None:
        try:
            lock.ensure()
            migration.ensure()
        except (RunLockRefused, TestDatabaseError) as refusal:
            session = config.stash.get(_SESSION, None)
            if session is not None:
                session.shouldfail = str(refusal)
            raise

    event.listen(Pool, "connect", _on_connect)
    config.stash[_LOCK] = lock
    config.stash[_LISTENER] = _on_connect


def pytest_sessionstart(session: pytest.Session) -> None:
    session.config.stash[_SESSION] = session


def pytest_unconfigure(config: pytest.Config) -> None:
    listener = config.stash.get(_LISTENER, None)
    if listener is not None:
        event.remove(Pool, "connect", listener)
    lock = config.stash.get(_LOCK, None)
    if lock is not None:
        lock.release()
