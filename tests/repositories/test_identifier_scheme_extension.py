# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Provider-native scheme behaviour on ``investment_identifiers`` (ADR-0096).

These live-DB tests prove the b023 CHECK swap's carry-over claim (ADR-0096 §1)
against the compose Postgres at head: a ``preqin`` row inserts successfully,
and the ADR-0090 partial tenant-unique index still applies to it — the same
provider fund ID cannot map to two investments in one tenant, exactly as an
ISIN cannot. A private-equity investment is used deliberately: the illiquid
book is precisely the one provider-native schemes exist to reach.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    InvestmentIdentifierRepository,
    InvestmentRepository,
    UserRepository,
    tenant_context,
)


async def _seed_two_investments(app_engine: AsyncEngine, tenant_id, *, email: str):
    """Create one user, one asset class, and two private-equity investments."""
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        ac = await AssetClassRepository(session).create(
            code="pe_class", display_name="Private Equity"
        )
        inv_repo = InvestmentRepository(session)
        investments = []
        for name in ("Fund A", "Fund B"):
            inv = await inv_repo.create(
                name=name,
                investment_type="private_equity",
                asset_class_id=ac.id,
                currency="EUR",
                created_by=actor.id,
            )
            investments.append(inv)
    return actor, investments


async def test_preqin_row_inserts_after_b023(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("preqin-insert")
    actor, (inv_a, _inv_b) = await _seed_two_investments(
        app_engine, tenant_id, email="preqin-insert@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        created = await InvestmentIdentifierRepository(session).add(
            investment_id=inv_a.id,
            scheme="preqin",
            # Messy input to show the uniform normalisation still applies.
            value="  preqin-12345  ",
            created_by=actor.id,
            is_primary=True,
            source="manual",
        )

    assert created.scheme == "preqin"
    assert created.value == "PREQIN-12345"  # trimmed + upper-cased
    assert created.is_primary is True
    assert created.source == "manual"

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await InvestmentIdentifierRepository(session).list_for_investment(inv_a.id)
    assert [(r.scheme, r.value) for r in rows] == [("preqin", "PREQIN-12345")]


async def test_preqin_tenant_unique_rejects_second_investment(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("preqin-unique")
    actor, (inv_a, inv_b) = await _seed_two_investments(
        app_engine, tenant_id, email="preqin-unique@example.com"
    )

    # The same Preqin fund ID on investment A.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await InvestmentIdentifierRepository(session).add(
            investment_id=inv_a.id,
            scheme="preqin",
            value="PQ-777",
            created_by=actor.id,
        )

    # The same (scheme, value) on investment B in the same tenant is rejected
    # by the partial UNIQUE (tenant_id, scheme, value) WHERE scheme <> 'internal'
    # index — a provider fund ID is an external identity like an ISIN
    # (ADR-0096 §1 carry-over).
    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            await InvestmentIdentifierRepository(session).add(
                investment_id=inv_b.id,
                scheme="preqin",
                value="PQ-777",
                created_by=actor.id,
            )
