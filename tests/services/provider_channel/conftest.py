# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Keep the provider-channel tests database-free (ADR-0129 Stage A).

This file adds no fixture. It exists to *remove* one.

``tests/services/conftest.py`` re-exports ``reset_schema`` from
``tests._db_fixtures``, and that fixture is ``autouse=True``: every test
collected anywhere under ``tests/services/`` otherwise opens the compose
Postgres and truncates every domain table before and after it runs. The
package under test here reaches no database at all — that is the whole point
of :mod:`services.provider_channel`, and ``test_contract.py`` pins it — so
paying a TRUNCATE per assertion would couple a pure suite to a live server
for nothing.

The no-op below shadows the inherited fixture by name, which is pytest's
ordinary mechanism for exactly this. Nothing else is overridden; if a future
prompt adds a DB-backed test in this directory, delete this file rather than
working around it.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def reset_schema() -> None:
    """Shadow the inherited autouse DB fixture; these tests need no database."""
    return None
