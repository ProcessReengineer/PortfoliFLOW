# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The test suite's own database: chosen at start-up, migrated on first use.

Database-backed tests run against the RAM-held test container defined in
``compose.test.yml``, never against the development database in ``.env``
(ADR-0135). Two pieces make that hold for every test module:

* :func:`select_test_database` runs when ``tests/conftest.py`` is imported,
  before any test module is. It writes the test URLs into ``DATABASE_URL``
  and ``DATABASE_URL_SUPERUSER`` in the process environment. Every
  ``load_dotenv`` call in the suite leaves an existing variable alone, and
  ``WebSettings`` prefers the environment over ``.env``, so the application,
  the fixtures, the run lock and every subprocess (Alembic, the CLI) see the
  test database and nothing else.
* :class:`HeadMigration` brings that database to Alembic ``head`` the first
  time a run connects. The container keeps its data in tmpfs, so after every
  start it is an empty cluster with only the application role in it.

The test URLs come from ``TEST_DATABASE_URL`` and
``TEST_DATABASE_URL_SUPERUSER`` (process environment first, then ``.env``),
else from the defaults below, which match ``compose.test.yml``. The defaults
apply only on a machine set up for database work, one where ``DATABASE_URL``
or ``DATABASE_URL_SUPERUSER`` is set. Where neither is (a bare checkout, the
fast CI tier), nothing is set and the database-bound modules skip, as before.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy.engine import make_url

#: Application-role URL of the ``compose.test.yml`` container.
DEFAULT_TEST_DATABASE_URL = (
    "postgresql+asyncpg://portfoliflow_app:app_test@localhost:5433/portfoliflow_test"
)
#: Superuser URL of the ``compose.test.yml`` container.
DEFAULT_TEST_DATABASE_URL_SUPERUSER = (
    "postgresql+asyncpg://postgres:postgres_test@localhost:5433/portfoliflow_test"
)

_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


class TestDatabaseError(RuntimeError):
    """The test database is misconfigured or could not be brought to head."""

    __test__ = False  # not a test class, despite the name


def _database_identity(url: str) -> tuple[str, int, str]:
    parsed = make_url(url)
    host = parsed.host or "localhost"
    return (
        "loopback" if host in _LOOPBACK else host,
        parsed.port or 5432,
        parsed.database or "",
    )


def select_test_database(rootdir: Path) -> tuple[str, str] | None:
    """Point ``DATABASE_URL`` and ``DATABASE_URL_SUPERUSER`` at the test database.

    Args:
        rootdir: The repository root, where ``.env`` lives.

    Returns:
        The application-role URL and the superuser URL now in the environment,
        or ``None`` when no database is configured at all (nothing is set).

    Raises:
        TestDatabaseError: Only one of the two ``TEST_DATABASE_URL*`` variables
            is set, or the test database is the development database named
            by ``.env``.
    """
    file_values = dotenv_values(rootdir / ".env")

    def _setting(name: str) -> str | None:
        return os.environ.get(name) or file_values.get(name) or None

    app_url = _setting("TEST_DATABASE_URL")
    superuser_url = _setting("TEST_DATABASE_URL_SUPERUSER")
    if bool(app_url) != bool(superuser_url):
        raise TestDatabaseError(
            "Set both TEST_DATABASE_URL and TEST_DATABASE_URL_SUPERUSER, or neither "
            "(the defaults match compose.test.yml)."
        )
    if not app_url or not superuser_url:
        if not (_setting("DATABASE_URL") or _setting("DATABASE_URL_SUPERUSER")):
            return None
        app_url, superuser_url = DEFAULT_TEST_DATABASE_URL, DEFAULT_TEST_DATABASE_URL_SUPERUSER

    test_identities = {_database_identity(app_url), _database_identity(superuser_url)}
    for name in ("DATABASE_URL", "DATABASE_URL_SUPERUSER"):
        dev_url = file_values.get(name)
        if dev_url and _database_identity(dev_url) in test_identities:
            raise TestDatabaseError(
                f"The test database is the development database ({name} in .env). "
                "Tests run against the RAM-held container in compose.test.yml "
                "(ADR-0135); point TEST_DATABASE_URL and TEST_DATABASE_URL_SUPERUSER "
                "at it, or leave them unset."
            )

    os.environ["DATABASE_URL"] = app_url
    os.environ["DATABASE_URL_SUPERUSER"] = superuser_url
    return app_url, superuser_url


class HeadMigration:
    """Runs ``alembic upgrade head`` once per test run, on first use."""

    def __init__(self, rootdir: Path) -> None:
        self._rootdir = rootdir
        self._mutex = threading.Lock()
        self._done = False
        self._failure: str | None = None

    def ensure(self) -> None:
        """Migrate the test database to head unless this run already has.

        Raises:
            TestDatabaseError: The migration failed; every later call raises
                the same error without trying again.
        """
        if self._done:
            return
        with self._mutex:
            if self._done:
                return
            if self._failure is not None:
                raise TestDatabaseError(self._failure)
            result = subprocess.run(
                [sys.executable, "-m", "alembic", "-c", "db/alembic.ini", "upgrade", "head"],
                cwd=self._rootdir,
                capture_output=True,
                text=True,
                check=False,
                env=dict(os.environ),
            )
            if result.returncode != 0:
                self._failure = (
                    "Could not migrate the test database to head "
                    f"(alembic exit {result.returncode}):\n{result.stderr[-2000:]}"
                )
                raise TestDatabaseError(self._failure)
            self._done = True
