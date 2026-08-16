# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression: ``web/routes/super_admin.py`` imports no tenant-data repositories.

Per ADR-0064 §1 the super-admin route surface deliberately does
**not** read tenant-data tables. The CLI ``inspect-tenant`` command
is the only sanctioned super-admin → tenant-data pathway. This
test walks the AST of ``web/routes/super_admin.py`` and asserts no
forbidden import appears.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = (Path(__file__).resolve().parents[2] / "web" / "routes" / "super_admin.py").read_text(
    encoding="utf-8"
)


# Allow-listed: the super-admin surface may legitimately depend on
# these (UserRepository for super-admin user management,
# TenantRepository for tenant CRUD, the audit repository for the
# super_admin_audit table).
_ALLOWED_REPO_MODULES: frozenset[str] = frozenset(
    {
        "core.repositories.user_repository",
        "core.repositories.tenant_repository",
        "core.repositories.super_admin_audit_repository",
    }
)

# Forbidden modules under ``core.repositories.*``. Adding a new
# tenant-data repository to the project should NOT change this
# regression unless ADR-0064 §1 is amended.
_FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "core.repositories.investment_",
    "core.repositories.saa_",
    "core.repositories.limit_",
    "core.repositories.portfolio_",
    "core.repositories.benchmark_",
    "core.repositories.region_",
    "core.repositories.sector_",
    "core.repositories.country_",
    "core.repositories.data_upload_",
    "core.repositories.asset_class_",
)


def test_super_admin_routes_import_no_tenant_data_repositories() -> None:
    tree = ast.parse(_SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for prefix in _FORBIDDEN_PREFIXES:
                assert not mod.startswith(prefix), (
                    f"super_admin.py imports forbidden tenant-data "
                    f"module {mod!r}. Tenant-data access from "
                    "super-admin routes violates ADR-0064 §1."
                )
            if mod.startswith("core.repositories.") and mod not in _ALLOWED_REPO_MODULES:
                # New repository under core.repositories that isn't
                # in either list — fail loudly so the maintainer
                # consciously categorises it.
                raise AssertionError(
                    f"super_admin.py imports {mod!r} which is neither "
                    "in the allow-list nor in the forbidden prefixes. "
                    "Categorise it explicitly: tenant-data repositories "
                    "are forbidden per ADR-0064 §1; super-admin-scoped "
                    "ones should be added to _ALLOWED_REPO_MODULES."
                )
