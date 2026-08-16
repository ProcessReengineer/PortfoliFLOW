# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""TenantRepository — read the active tenant's own row.

Per ADR-0035 the ``tenants`` table is policed by a self-visibility RLS
policy (``id = current_setting('app.tenant_id')::uuid``), so a
tenant-scoped session sees exactly one row: its own. This repository
exposes narrow reads used by presentation and calculation surfaces — the
ADR-0068 Front Office welcome header needs the human tenant name for the
active context to render ``… — {tenant} portfolio``, and the ADR-0099
conversion boundary needs the tenant's functional currency to know what
every aggregate figure is denominated in.

Writes to ``tenants`` go through the superuser / audit path
(``cli/bootstrap.py``, ``services/super_admin``), not this repository:
the ``portfoliflow_app`` role cannot create or rename tenants.
"""

from __future__ import annotations

from sqlalchemy import select

from core.models.tenant import Tenant
from core.repositories.base import BaseRepository


class TenantRepository(BaseRepository):
    """Read-only access to the active tenant's own row."""

    async def get_current_name(self) -> str | None:
        """Return the active tenant's display name, or ``None``.

        Under the ``tenant_self_visibility`` RLS policy the only visible
        ``tenants`` row is the one matching ``app.tenant_id``, so this
        reads that single row's ``name``. Returns ``None`` when no row is
        visible (an unscoped or misconfigured session) rather than
        raising — callers treat a missing name as "omit", not an error.
        """
        result = await self._session.execute(select(Tenant.name).limit(1))
        return result.scalar_one_or_none()

    async def get_current_functional_currency(self) -> str | None:
        """Return the active tenant's functional currency, or ``None``.

        The functional currency (ADR-0099 §1) is the currency every
        aggregate figure the system reports is expressed in. It is distinct
        from an investment's *position* currency
        (``investments.currency``) and from the *reference* currency of the
        FX dataset (``fx_rates.reference_currency``).

        Reads the single row the ``tenant_self_visibility`` RLS policy
        exposes, exactly as :meth:`get_current_name` does. Returns ``None``
        when no row is visible (an unscoped or misconfigured session)
        rather than raising — the column itself is ``NOT NULL DEFAULT
        'EUR'``, so ``None`` means "no tenant", never "no currency".
        """
        result = await self._session.execute(select(Tenant.functional_currency).limit(1))
        return result.scalar_one_or_none()
