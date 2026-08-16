# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Shared fixtures for ``services/tenant_users`` tests.

Re-exports the standard DB fixtures so the tenant-user service tests run
against the same live compose Postgres as the rest of the service-layer
suite — under the ``portfoliflow_app`` role, which is the point: the
guards these tests exercise sit on top of RLS.
"""

from __future__ import annotations

from tests._db_fixtures import (  # noqa: F401 — fixture re-exports for pytest collection
    app_engine,
    reset_schema,
    seed_tenant,
    superuser_engine,
)
