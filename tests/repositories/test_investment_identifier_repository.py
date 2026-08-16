# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InvestmentIdentifierRepository tests against the live compose Postgres.

The ``investment_identifiers`` table is tenant-scoped (RLS-policed, per
ADR-0090 / ADR-0035). It records security identifiers (ISIN / ticker /
FIGI / CUSIP / internal) as the deterministic join-key to external
market-data worlds.

Coverage
--------
* II-01: ``add`` roundtrip; ``value`` is stored trimmed + upper-cased;
  ``list_for_investment`` and ``get_by_scheme_value`` (normalised
  lookup) read it back; ``delete`` reports True/False on rowcount.
* II-02: an empty-after-trim value is rejected with the typed
  :class:`ValidationError` before it reaches the database.
* II-03: a duplicate ``(investment_id, scheme, value)`` is rejected by
  the UNIQUE constraint.
* II-04: the same ``(scheme, value)`` on a second investment in the
  same tenant is rejected for ``scheme='isin'`` (partial tenant-unique)
  but allowed for ``scheme='internal'`` (free namespace, exempt).
* II-05: a second ``is_primary=TRUE`` row for the same investment is
  rejected by the partial one-primary-per-investment index.
* II-06: an identifier written under tenant A is invisible from a
  session under tenant B (RLS smoke).
* II-07: ``set_primary`` promotes a non-primary row (rowcount → True),
  reports False for an unknown id, and cannot create a second primary
  for the same investment (partial unique index). Added for the
  Excel-import primary-promotion path (ADR-0090 §"Identifiers enter
  through both import paths").
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import ValidationError
from core.repositories import (
    AssetClassRepository,
    InvestmentIdentifierRepository,
    InvestmentRepository,
    UserRepository,
    tenant_context,
)


async def _seed_investments(
    app_engine: AsyncEngine,
    tenant_id,
    *,
    email: str,
    names: tuple[str, ...] = ("Fund One",),
):
    """Create one user, one asset class, and N investments for setup.

    Returns ``(actor, [investment, ...])`` in the order of ``names`` so
    callers can drive the identifier methods against explicit ids.
    """
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        ac = await AssetClassRepository(session).create(
            code="default_class", display_name="Default Class"
        )
        inv_repo = InvestmentRepository(session)
        investments = []
        for name in names:
            inv = await inv_repo.create(
                name=name,
                investment_type="listed_equity",
                asset_class_id=ac.id,
                currency="EUR",
                created_by=actor.id,
            )
            investments.append(inv)
    return actor, investments


# ---------------------------------------------------------------------------
# II-01: add roundtrip + normalisation + list + lookup + delete
# ---------------------------------------------------------------------------


async def test_ii01_add_roundtrip_normalisation_and_delete(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("II-01")
    actor, (inv,) = await _seed_investments(app_engine, tenant_id, email="ii01@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        created = await InvestmentIdentifierRepository(session).add(
            investment_id=inv.id,
            scheme="isin",
            # Deliberately messy input: leading/trailing space, lowercase.
            value="  de000basf111  ",
            created_by=actor.id,
            is_primary=True,
            source="excel",
        )

    # Normalised on write: trimmed + upper-cased.
    assert created.value == "DE000BASF111"
    assert created.scheme == "isin"
    assert created.is_primary is True
    assert created.source == "excel"
    assert created.investment_id == inv.id

    async with tenant_context(app_engine, tenant_id) as session:
        repo = InvestmentIdentifierRepository(session)
        rows = await repo.list_for_investment(inv.id)
        assert [r.value for r in rows] == ["DE000BASF111"]

        # Lookup normalises its argument the same way, so a lowercase /
        # padded query still matches the stored row.
        hit = await repo.get_by_scheme_value("isin", "de000basf111 ")
        assert hit is not None
        assert hit.id == created.id
        # A miss returns None, not a raise.
        assert await repo.get_by_scheme_value("isin", "US0000000000") is None

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        first = await InvestmentIdentifierRepository(session).delete(created.id)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        second = await InvestmentIdentifierRepository(session).delete(created.id)

    assert first is True
    assert second is False

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await InvestmentIdentifierRepository(session).list_for_investment(inv.id)
    assert rows == []


# ---------------------------------------------------------------------------
# II-02: empty-after-trim value rejected with ValidationError
# ---------------------------------------------------------------------------


async def test_ii02_empty_value_rejected(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("II-02")
    actor, (inv,) = await _seed_investments(app_engine, tenant_id, email="ii02@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentIdentifierRepository(session)
        with pytest.raises(ValidationError):
            await repo.add(
                investment_id=inv.id,
                scheme="ticker",
                value="   ",
                created_by=actor.id,
            )


# ---------------------------------------------------------------------------
# II-03: duplicate (investment_id, scheme, value) rejected
# ---------------------------------------------------------------------------


async def test_ii03_duplicate_identifier_rejected(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("II-03")
    actor, (inv,) = await _seed_investments(app_engine, tenant_id, email="ii03@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await InvestmentIdentifierRepository(session).add(
            investment_id=inv.id,
            scheme="ticker",
            value="BAS",
            created_by=actor.id,
        )

    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            await InvestmentIdentifierRepository(session).add(
                investment_id=inv.id,
                scheme="ticker",
                # Same value after normalisation ("bas" -> "BAS").
                value="bas",
                created_by=actor.id,
            )


# ---------------------------------------------------------------------------
# II-04: per-tenant scheme/value uniqueness — isin unique, internal free
# ---------------------------------------------------------------------------


async def test_ii04_tenant_scheme_value_uniqueness(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("II-04")
    actor, (inv_a, inv_b) = await _seed_investments(
        app_engine,
        tenant_id,
        email="ii04@example.com",
        names=("Fund A", "Fund B"),
    )

    # ISIN X on investment A.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await InvestmentIdentifierRepository(session).add(
            investment_id=inv_a.id,
            scheme="isin",
            value="US0378331005",
            created_by=actor.id,
        )

    # The same ISIN on investment B in the same tenant is rejected: a
    # real-world identifier maps to at most one investment per tenant.
    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            await InvestmentIdentifierRepository(session).add(
                investment_id=inv_b.id,
                scheme="isin",
                value="US0378331005",
                created_by=actor.id,
            )

    # The 'internal' scheme is a free namespace: the same internal value
    # may be attached to two different investments in the same tenant.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentIdentifierRepository(session)
        await repo.add(
            investment_id=inv_a.id,
            scheme="internal",
            value="LEGACY-42",
            created_by=actor.id,
        )
        await repo.add(
            investment_id=inv_b.id,
            scheme="internal",
            value="LEGACY-42",
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = InvestmentIdentifierRepository(session)
        a_internal = [
            r.value for r in await repo.list_for_investment(inv_a.id) if r.scheme == "internal"
        ]
        b_internal = [
            r.value for r in await repo.list_for_investment(inv_b.id) if r.scheme == "internal"
        ]
    assert a_internal == ["LEGACY-42"]
    assert b_internal == ["LEGACY-42"]


# ---------------------------------------------------------------------------
# II-05: at most one primary identifier per investment
# ---------------------------------------------------------------------------


async def test_ii05_second_primary_rejected(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("II-05")
    actor, (inv,) = await _seed_investments(app_engine, tenant_id, email="ii05@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await InvestmentIdentifierRepository(session).add(
            investment_id=inv.id,
            scheme="isin",
            value="US0378331005",
            created_by=actor.id,
            is_primary=True,
        )

    # A second primary — distinct on (scheme, value) so only the
    # one-primary-per-investment index can fire — is rejected.
    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            await InvestmentIdentifierRepository(session).add(
                investment_id=inv.id,
                scheme="ticker",
                value="AAPL",
                created_by=actor.id,
                is_primary=True,
            )

    # A second *non-primary* identifier is fine.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        secondary = await InvestmentIdentifierRepository(session).add(
            investment_id=inv.id,
            scheme="ticker",
            value="AAPL",
            created_by=actor.id,
            is_primary=False,
        )
    assert secondary.is_primary is False


# ---------------------------------------------------------------------------
# II-06: cross-tenant invisibility (RLS smoke)
# ---------------------------------------------------------------------------


async def test_ii06_cross_tenant_invisibility(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant(name="A")
    tenant_b = await seed_tenant(name="B")

    actor_a, (inv_a,) = await _seed_investments(app_engine, tenant_a, email="a@example.com")
    _actor_b, (_inv_b,) = await _seed_investments(app_engine, tenant_b, email="b@example.com")

    async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
        await InvestmentIdentifierRepository(session).add(
            investment_id=inv_a.id,
            scheme="isin",
            value="US0378331005",
            created_by=actor_a.id,
        )

    async with tenant_context(app_engine, tenant_b) as session:
        repo = InvestmentIdentifierRepository(session)
        # Tenant B cannot see tenant A's identifier by investment id …
        assert await repo.list_for_investment(inv_a.id) == []
        # … nor by (scheme, value) lookup …
        assert await repo.get_by_scheme_value("isin", "US0378331005") is None
        # … and the raw row count under B's context is zero.
        count = await session.execute(text("SELECT count(*) FROM investment_identifiers"))
        assert count.scalar_one() == 0


# ---------------------------------------------------------------------------
# II-07: set_primary promotes a row; rowcount semantics; one-primary index
# ---------------------------------------------------------------------------


async def test_ii07_set_primary_promotes_and_guards(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("II-07")
    actor, (inv,) = await _seed_investments(app_engine, tenant_id, email="ii07@example.com")

    # Two non-primary rows on the same investment.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentIdentifierRepository(session)
        isin = await repo.add(
            investment_id=inv.id,
            scheme="isin",
            value="US0378331005",
            created_by=actor.id,
            is_primary=False,
        )
        await repo.add(
            investment_id=inv.id,
            scheme="ticker",
            value="AAPL",
            created_by=actor.id,
            is_primary=False,
        )

    # Promote the ISIN row → rowcount 1 → True.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        promoted = await InvestmentIdentifierRepository(session).set_primary(isin.id)
    assert promoted is True

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await InvestmentIdentifierRepository(session).list_for_investment(inv.id)
    primaries = {r.scheme for r in rows if r.is_primary}
    assert primaries == {"isin"}

    # An unknown id updates no row → False.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        missing = await InvestmentIdentifierRepository(session).set_primary(uuid.uuid4())
    assert missing is False

    # Promoting a second row while one is already primary is rejected by
    # the partial one-primary-per-investment index.
    ticker_id = next(r.id for r in rows if r.scheme == "ticker")
    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            await InvestmentIdentifierRepository(session).set_primary(ticker_id)
