# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Service-level tests for the identifier CRUD surface (ADR-0096).

Exercises the four :class:`InvestmentService` identifier methods against the
live compose Postgres, with the identifier repository wired into the service
(the CRUD-surface construction). Coverage:

* add-manual stamps ``source='manual'`` + ``created_by`` and normalises the
  value; a scheme outside the closed set is rejected before the DB;
* set-primary demotes the current primary then promotes the target, leaving
  exactly one primary;
* delete of the primary leaves the investment with none, and the market-linked
  predicate turns false for a listed investment as a direct consequence
  (ADR-0096 §3 — eligibility lapses, no auto-promotion).
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import ValidationError
from core.repositories import (
    AssetClassRepository,
    InvestmentCashflowRepository,
    InvestmentIdentifierRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    UserRepository,
    tenant_context,
)
from services.investments import InvestmentService
from services.investments.market_linked import is_market_linked


def _service(session) -> InvestmentService:
    return InvestmentService(
        investments=InvestmentRepository(session),
        navs=InvestmentNavRepository(session),
        cashflows=InvestmentCashflowRepository(session),
        identifiers=InvestmentIdentifierRepository(session),
    )


async def _seed_investment(
    app_engine: AsyncEngine,
    tenant_id,
    *,
    email: str,
    investment_type: str = "listed_equity",
):
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        ac = await AssetClassRepository(session).create(code="ac", display_name="AC")
        inv = await InvestmentRepository(session).create(
            name="Fund",
            investment_type=investment_type,
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
    return actor, inv


# ---------------------------------------------------------------------------
# add_identifier_manual
# ---------------------------------------------------------------------------


async def test_add_manual_stamps_provenance_and_normalises(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("id-add")
    actor, inv = await _seed_investment(app_engine, tenant_id, email="id-add@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        created = await _service(session).add_identifier_manual(
            investment_id=inv.id,
            scheme="preqin",
            value="  pq-1 ",
            user_id=actor.id,
        )

    assert created.source == "manual"
    assert created.created_by == actor.id
    assert created.value == "PQ-1"  # trimmed + upper-cased
    assert created.is_primary is False  # never primary on creation


async def test_add_manual_rejects_unknown_scheme(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("id-badscheme")
    actor, inv = await _seed_investment(app_engine, tenant_id, email="id-badscheme@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        with pytest.raises(ValidationError):
            await _service(session).add_identifier_manual(
                investment_id=inv.id,
                scheme="sedol",  # not in the closed set
                value="0263494",
                user_id=actor.id,
            )


# ---------------------------------------------------------------------------
# set_primary_identifier
# ---------------------------------------------------------------------------


async def test_set_primary_demotes_then_promotes(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("id-setprimary")
    actor, inv = await _seed_investment(app_engine, tenant_id, email="id-setprimary@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _service(session)
        ticker = await svc.add_identifier_manual(
            investment_id=inv.id, scheme="ticker", value="ACME", user_id=actor.id
        )
        isin = await svc.add_identifier_manual(
            investment_id=inv.id,
            scheme="isin",
            value="US0378331005",
            user_id=actor.id,
        )

    # Promote the ticker.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        assert (
            await _service(session).set_primary_identifier(
                investment_id=inv.id, identifier_id=ticker.id, user_id=actor.id
            )
            is True
        )

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await _service(session).list_identifiers(inv.id)
    primaries = {r.scheme for r in rows if r.is_primary}
    assert primaries == {"ticker"}

    # Re-prime to the ISIN: ticker must be demoted, ISIN promoted, one primary.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        assert (
            await _service(session).set_primary_identifier(
                investment_id=inv.id, identifier_id=isin.id, user_id=actor.id
            )
            is True
        )

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await _service(session).list_identifiers(inv.id)
    by_scheme = {r.scheme: r.is_primary for r in rows}
    assert by_scheme == {"ticker": False, "isin": True}
    assert sum(1 for r in rows if r.is_primary) == 1


async def test_set_primary_unknown_id_is_noop_false(app_engine: AsyncEngine, seed_tenant) -> None:
    from uuid import uuid4

    tenant_id = await seed_tenant("id-setprimary-miss")
    actor, inv = await _seed_investment(
        app_engine, tenant_id, email="id-setprimary-miss@example.com"
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        assert (
            await _service(session).set_primary_identifier(
                investment_id=inv.id, identifier_id=uuid4(), user_id=actor.id
            )
            is False
        )


# ---------------------------------------------------------------------------
# delete_identifier — deleting the primary lapses live-eligibility
# ---------------------------------------------------------------------------


async def test_delete_primary_leaves_none_and_market_linked_turns_false(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("id-delprimary")
    actor, inv = await _seed_investment(
        app_engine,
        tenant_id,
        email="id-delprimary@example.com",
        investment_type="listed_equity",
    )

    # Add a market-usable identifier and make it primary.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _service(session)
        ident = await svc.add_identifier_manual(
            investment_id=inv.id, scheme="ticker", value="ACME", user_id=actor.id
        )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _service(session).set_primary_identifier(
            investment_id=inv.id, identifier_id=ident.id, user_id=actor.id
        )

    # A listed, unitised investment with a primary market-usable identifier is
    # eligible; 'unitised' isolates the identifier gate under test (ADR-0097 §9).
    async with tenant_context(app_engine, tenant_id) as session:
        rows = await _service(session).list_identifiers(inv.id)
    assert is_market_linked("listed_equity", rows, "unitised") is True

    # Delete the primary — allowed; leaves the investment with no identifiers.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        assert (
            await _service(session).delete_identifier(
                investment_id=inv.id, identifier_id=ident.id, user_id=actor.id
            )
            is True
        )

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await _service(session).list_identifiers(inv.id)
    assert rows == []
    # Eligibility lapses as a direct consequence — no auto-promotion.
    assert is_market_linked("listed_equity", rows, "unitised") is False


async def test_delete_identifier_wrong_investment_is_false(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    from uuid import uuid4

    tenant_id = await seed_tenant("id-del-miss")
    actor, inv = await _seed_investment(app_engine, tenant_id, email="id-del-miss@example.com")
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        assert (
            await _service(session).delete_identifier(
                investment_id=inv.id, identifier_id=uuid4(), user_id=actor.id
            )
            is False
        )
