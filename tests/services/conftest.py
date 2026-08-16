# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Service test fixtures.

The sub-stream 3b SAA service tests exercise the live compose
Postgres just like the repository tests. Fixtures are re-exported
from the shared ``tests._db_fixtures`` module so both packages
share one source of truth without spreading autouse fixtures into
unrelated non-DB tests.
"""

from __future__ import annotations

from tests._db_fixtures import (  # noqa: F401 — fixture re-exports for pytest collection
    app_engine,
    reset_schema,
    seed_tenant,
    superuser_engine,
)
