# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Suite-wide hooks: the run lock for the shared test database.

A listener on every SQLAlchemy pool calls :meth:`tests._run_lock.RunLock.ensure`
whenever a connection is opened. The first call takes the run lock; while
another run holds it, the call raises, the connection is never handed out,
and pytest stops after the current test with the refusal as its last line.

Hooks only, no fixtures: a database-free run never opens a connection, so it
never takes the lock and pays nothing for it. Rationale and scope:
``tests/_run_lock.py`` and ``docs/testing.md`` ("The database").
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.pool import Pool

from tests._run_lock import RunLock, RunLockRefused, database_url

_LOCK = pytest.StashKey[RunLock]()
_LISTENER = pytest.StashKey[Callable[[Any, Any], None]]()
_SESSION = pytest.StashKey[pytest.Session]()


def pytest_configure(config: pytest.Config) -> None:
    lock = RunLock(database_url(config.rootpath))

    def _on_connect(dbapi_connection: Any, connection_record: Any) -> None:
        try:
            lock.ensure()
        except RunLockRefused as refusal:
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
