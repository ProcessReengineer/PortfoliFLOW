# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tenant resolution — mapping a request to a tenant id.

Per ADR-0063 §1. The package owns the :class:`TenantResolver`
abstraction and ships the production :class:`SubdomainTenantResolver`
plus a test-only :class:`ExplicitHostHeaderResolver`.

The resolver runs **before** any tenant context exists, so its DB
reads must go via the audit engine (the same RLS-bypass surface
ADR-0036 §8 uses for ``login_audit`` writes). The sanctioned audit-
engine usage is enforced by
``tests/regression/test_audit_engine_only_writes_login_audit.py``.
"""

from services.tenant_resolution.resolver import (
    ExplicitHostHeaderResolver,
    SubdomainTenantResolver,
    TenantResolutionError,
    TenantResolver,
    UnknownSubdomainError,
)

__all__ = [
    "ExplicitHostHeaderResolver",
    "SubdomainTenantResolver",
    "TenantResolutionError",
    "TenantResolver",
    "UnknownSubdomainError",
]
