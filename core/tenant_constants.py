# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tenant-related constants used across the persistence layer.

Two hardcoded UUIDs anchor the multi-tenant substrate:

- :data:`PRIMARY_TENANT_ID` — the production tenant for the
  PortfoliFLOW deployment (Minathena Capital). This is the same
  UUID previously named ``SENTINEL_TENANT_ID``. ADR-0063 §7 renamed
  it to sharpen the semantic ("not a placeholder, the actual
  production tenant"); :data:`SENTINEL_TENANT_ID` is retained as a
  transitional alias and removed in a follow-up after call-sites
  are migrated.

- :data:`SYSTEM_TENANT_ID` — the platform-operations tenant that
  hosts super-admin user accounts. Structurally not a data-bearing
  tenant; a regression test asserts it holds zero rows in every
  domain table. The schema-level CHECK on ``users`` binds
  ``is_super_admin = TRUE`` to this tenant id.

See ADR-0063 §3 and ADR-0064 in full for the rationale.
"""

from __future__ import annotations

from uuid import UUID


# The platform-operations tenant. Hosts super-admin user accounts
# and nothing else. A schema-level CHECK on users binds
# is_super_admin = TRUE to this tenant id. See ADR-0063 §3 and
# ADR-0064 in full.
SYSTEM_TENANT_ID: UUID = UUID("00000000-0000-0000-0000-000000000000")

# The structurally-anchored primary tenant (Minathena Capital).
# Same UUID as the previous ``SENTINEL_TENANT_ID``; the rename
# sharpens the semantic from "Phase-2 single-tenant placeholder" to
# "the production tenant".
PRIMARY_TENANT_ID: UUID = UUID("00000000-0000-0000-0000-000000000001")

# Transitional alias for PRIMARY_TENANT_ID. New code uses
# PRIMARY_TENANT_ID. Removed in a follow-up commit after the
# transitional period.
SENTINEL_TENANT_ID: UUID = PRIMARY_TENANT_ID
