# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Shared fixtures for the ``services/transactions`` tests.

Re-exports the standard DB fixtures so the trade-ticket service tests run
against the same live compose Postgres as the rest of the service-layer
suite — under the ``portfoliflow_app`` role, which is the point: the
propose-time guards sit on top of RLS and read real ledgers.
"""

from __future__ import annotations

from tests._db_fixtures import (  # noqa: F401 — fixture re-exports for pytest collection
    app_engine,
    reset_schema,
    seed_tenant,
    superuser_engine,
)
