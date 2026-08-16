# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InvestmentCountryWeightsRepository tests against the live compose Postgres.

Coverage per ADR-0045 §2 and the ADR-0080 historisation contract:

* ``replace_snapshot_for_investment`` is date-scoped, idempotent and
  replaces in place within one snapshot.
* Cross-tenant isolation: tenant A cannot see tenant B's weights.
* ``weight_pct`` range CHECK rejects values outside ``[0, 100]``.
* Empty input clears the snapshot.
* Two snapshots coexist; ``list_latest_*`` returns the most recent,
  ``list_by_investments`` returns both, a date-scoped replace leaves
  the other snapshot intact, and ``as_of_cutoff`` selects the earlier.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    CountryWeightInput,
    InvestmentCountryWeightsRepository,
    InvestmentRepository,
    UserRepository,
    tenant_context,
)

# Two statement dates used across the historisation tests.
_D1 = date(2024, 3, 31)
_D2 = date(2024, 6, 30)


async def _seed_actor_and_investment(
    app_engine: AsyncEngine,
    tenant_id,
    *,
    email: str,
    investment_name: str = "Investment X",
):
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        ac = await AssetClassRepository(session).create(code="ac_for_cw", display_name="AC")
        inv = await InvestmentRepository(session).create(
            name=investment_name,
            investment_type="private_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
    return actor, inv


# ---------------------------------------------------------------------------
# CW-01: replace_snapshot is idempotent and replaces in place
# ---------------------------------------------------------------------------


async def test_cw01_replace_snapshot_replaces_in_place(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_actor_and_investment(app_engine, tenant_id, email="cw01@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentCountryWeightsRepository(session)
        # First generation.
        first = await repo.replace_snapshot_for_investment(
            inv.id,
            _D1,
            [
                CountryWeightInput(
                    country_iso_code="DE",
                    weight_pct=Decimal("60"),
                ),
                CountryWeightInput(
                    country_iso_code="US",
                    weight_pct=Decimal("40"),
                ),
            ],
            basis="reported",
            created_by=actor.id,
        )
    assert {w.country_iso_code for w in first} == {"DE", "US"}
    assert all(w.as_of_date == _D1 for w in first)
    assert all(w.basis == "reported" for w in first)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentCountryWeightsRepository(session)
        # Second generation replaces with a different mix; no
        # UNIQUE-violation because the prior generation was deleted.
        second = await repo.replace_snapshot_for_investment(
            inv.id,
            _D1,
            [
                CountryWeightInput(
                    country_iso_code="DE",
                    weight_pct=Decimal("100"),
                ),
            ],
            basis="reported",
            created_by=actor.id,
        )
    assert len(second) == 1
    assert second[0].country_iso_code == "DE"
    assert second[0].weight_pct == Decimal("100.0000")


# ---------------------------------------------------------------------------
# CW-02: list_for_investment returns sorted weights
# ---------------------------------------------------------------------------


async def test_cw02_list_for_investment_sorted(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_actor_and_investment(app_engine, tenant_id, email="cw02@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentCountryWeightsRepository(session)
        await repo.replace_snapshot_for_investment(
            inv.id,
            _D1,
            [
                CountryWeightInput("US", Decimal("25")),
                CountryWeightInput("DE", Decimal("50")),
                CountryWeightInput("GB", Decimal("25")),
            ],
            basis="reported",
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = InvestmentCountryWeightsRepository(session)
        rows = await repo.list_for_investment(inv.id)
    # Ordered by country_iso_code ascending.
    assert [r.country_iso_code for r in rows] == ["DE", "GB", "US"]


# ---------------------------------------------------------------------------
# CW-03: cross-tenant isolation
# ---------------------------------------------------------------------------


async def test_cw03_cross_tenant_isolation(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant(name="Tenant A")
    tenant_b = await seed_tenant(name="Tenant B")

    actor_a, inv_a = await _seed_actor_and_investment(
        app_engine,
        tenant_a,
        email="cw03-a@example.com",
        investment_name="Inv-A",
    )
    actor_b, inv_b = await _seed_actor_and_investment(
        app_engine,
        tenant_b,
        email="cw03-b@example.com",
        investment_name="Inv-B",
    )

    async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
        await InvestmentCountryWeightsRepository(session).replace_snapshot_for_investment(
            inv_a.id,
            _D1,
            [CountryWeightInput("DE", Decimal("100"))],
            basis="reported",
            created_by=actor_a.id,
        )
    async with tenant_context(app_engine, tenant_b, user_id=actor_b.id) as session:
        await InvestmentCountryWeightsRepository(session).replace_snapshot_for_investment(
            inv_b.id,
            _D1,
            [CountryWeightInput("US", Decimal("100"))],
            basis="reported",
            created_by=actor_b.id,
        )

    # Tenant B's session must NOT see Tenant A's investment id, even
    # if asked directly. RLS filters by tenant_id on the weights row.
    async with tenant_context(app_engine, tenant_b) as session:
        repo = InvestmentCountryWeightsRepository(session)
        rows_for_a = await repo.list_for_investment(inv_a.id)
        rows_for_b = await repo.list_for_investment(inv_b.id)
    assert rows_for_a == []
    assert len(rows_for_b) == 1
    assert rows_for_b[0].country_iso_code == "US"


# ---------------------------------------------------------------------------
# CW-04: weight_pct range CHECK rejects out-of-range values
# ---------------------------------------------------------------------------


async def test_cw04_weight_pct_range_check(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_actor_and_investment(app_engine, tenant_id, email="cw04@example.com")

    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            repo = InvestmentCountryWeightsRepository(session)
            await repo.replace_snapshot_for_investment(
                inv.id,
                _D1,
                [CountryWeightInput("DE", Decimal("150"))],
                basis="reported",
                created_by=actor.id,
            )


# ---------------------------------------------------------------------------
# CW-05: empty input clears the snapshot
# ---------------------------------------------------------------------------


async def test_cw05_empty_input_clears_existing(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_actor_and_investment(app_engine, tenant_id, email="cw05@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentCountryWeightsRepository(session)
        await repo.replace_snapshot_for_investment(
            inv.id,
            _D1,
            [CountryWeightInput("DE", Decimal("50"))],
            basis="reported",
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentCountryWeightsRepository(session)
        result = await repo.replace_snapshot_for_investment(
            inv.id, _D1, [], basis="reported", created_by=actor.id
        )
    assert result == []

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await InvestmentCountryWeightsRepository(session).list_for_investment(inv.id)
    assert rows == []


# ---------------------------------------------------------------------------
# Batched plural method (P6-H)
# ---------------------------------------------------------------------------


async def _seed_three_investments_with_country_weights(
    app_engine: AsyncEngine, tenant_id, *, email: str
):
    """Seed actor, asset class, three investments and country weight rows.

    Returns ``(actor, [inv_a, inv_b, inv_c])``: Inv A has two weight
    rows, Inv B has one, Inv C has none. All at snapshot ``_D1``.
    """
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        ac = await AssetClassRepository(session).create(
            code="batched_class", display_name="Batched Class"
        )
        inv_repo = InvestmentRepository(session)
        inv_a = await inv_repo.create(
            name="Alpha",
            investment_type="private_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
        inv_b = await inv_repo.create(
            name="Beta",
            investment_type="private_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
        inv_c = await inv_repo.create(
            name="Gamma",
            investment_type="private_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
        weights_repo = InvestmentCountryWeightsRepository(session)
        await weights_repo.replace_snapshot_for_investment(
            inv_a.id,
            _D1,
            [
                CountryWeightInput("US", Decimal("40")),
                CountryWeightInput("DE", Decimal("60")),
            ],
            basis="reported",
            created_by=actor.id,
        )
        await weights_repo.replace_snapshot_for_investment(
            inv_b.id,
            _D1,
            [CountryWeightInput("FR", Decimal("100"))],
            basis="reported",
            created_by=actor.id,
        )
    return actor, [inv_a, inv_b, inv_c]


async def test_cw06_list_by_investments_matches_singular(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    _actor, invs = await _seed_three_investments_with_country_weights(
        app_engine, tenant_id, email="cw06@example.com"
    )
    inv_a, inv_b, inv_c = invs

    async with tenant_context(app_engine, tenant_id) as session:
        repo = InvestmentCountryWeightsRepository(session)
        singular = {
            inv_a.id: await repo.list_for_investment(inv_a.id),
            inv_b.id: await repo.list_for_investment(inv_b.id),
            inv_c.id: await repo.list_for_investment(inv_c.id),
        }
        batched = await repo.list_by_investments([inv_a.id, inv_b.id, inv_c.id])

    assert set(batched.keys()) == {inv_a.id, inv_b.id, inv_c.id}
    for inv_id, rows in singular.items():
        assert batched[inv_id] == rows
    # Inv A rows sorted by country_iso_code ascending.
    assert [w.country_iso_code for w in batched[inv_a.id]] == ["DE", "US"]


async def test_cw07_list_by_investments_empty_input_returns_empty_dict(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        result = await InvestmentCountryWeightsRepository(session).list_by_investments([])
    assert result == {}


async def test_cw08_list_by_investments_missing_id_maps_to_empty_list(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    _actor, invs = await _seed_three_investments_with_country_weights(
        app_engine, tenant_id, email="cw08@example.com"
    )
    inv_a, inv_b, _inv_c = invs
    fresh_id = uuid4()

    async with tenant_context(app_engine, tenant_id) as session:
        result = await InvestmentCountryWeightsRepository(session).list_by_investments(
            [inv_a.id, inv_b.id, fresh_id]
        )

    assert set(result.keys()) == {inv_a.id, inv_b.id, fresh_id}
    assert result[fresh_id] == []
    assert len(result[inv_a.id]) == 2
    assert len(result[inv_b.id]) == 1


# ---------------------------------------------------------------------------
# ADR-0080 historisation behaviour (Tests 2–4)
# ---------------------------------------------------------------------------


async def _seed_two_snapshots(app_engine: AsyncEngine, tenant_id, *, email: str):
    """Seed one investment with two country snapshots.

    Snapshot ``_D1`` holds ``DE`` (100%); snapshot ``_D2`` (later)
    holds ``US`` (100%). Returns ``(actor, inv)``.
    """
    actor, inv = await _seed_actor_and_investment(
        app_engine, tenant_id, email=email, investment_name="Historised"
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentCountryWeightsRepository(session)
        await repo.replace_snapshot_for_investment(
            inv.id,
            _D1,
            [CountryWeightInput("DE", Decimal("100"))],
            basis="reported",
            created_by=actor.id,
        )
        await repo.replace_snapshot_for_investment(
            inv.id,
            _D2,
            [CountryWeightInput("US", Decimal("100"))],
            basis="reported",
            created_by=actor.id,
        )
    return actor, inv


async def test_cw09_two_snapshots_latest_vs_full_history(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """ADR-0080 Test 2: two snapshots coexist; latest reader picks D2."""
    tenant_id = await seed_tenant()
    _actor, inv = await _seed_two_snapshots(app_engine, tenant_id, email="cw09@example.com")

    async with tenant_context(app_engine, tenant_id) as session:
        repo = InvestmentCountryWeightsRepository(session)
        full = await repo.list_by_investments([inv.id])
        latest = await repo.list_latest_by_investments([inv.id])

    assert len(full[inv.id]) == 2
    assert {r.as_of_date for r in full[inv.id]} == {_D1, _D2}

    assert len(latest[inv.id]) == 1
    assert latest[inv.id][0].as_of_date == _D2
    assert latest[inv.id][0].country_iso_code == "US"


async def test_cw10_date_scoped_replace_leaves_other_snapshot(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """ADR-0080 Test 3: replacing D2 does not touch D1."""
    tenant_id = await seed_tenant()
    actor, inv = await _seed_two_snapshots(app_engine, tenant_id, email="cw10@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentCountryWeightsRepository(session)
        await repo.replace_snapshot_for_investment(
            inv.id,
            _D2,
            [CountryWeightInput("GB", Decimal("100"))],
            basis="reported",
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = InvestmentCountryWeightsRepository(session)
        d1_rows = await repo.list_for_investment(inv.id, as_of_cutoff=_D1)
        d2_rows = await repo.list_latest_for_investment(inv.id)

    assert len(d1_rows) == 1
    assert d1_rows[0].as_of_date == _D1
    assert d1_rows[0].country_iso_code == "DE"
    assert len(d2_rows) == 1
    assert d2_rows[0].as_of_date == _D2
    assert d2_rows[0].country_iso_code == "GB"


async def test_cw11_latest_cutoff_selects_earlier_snapshot(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """ADR-0080 Test 4: list_latest_* with cutoff=D1 returns the D1 snapshot."""
    tenant_id = await seed_tenant()
    _actor, inv = await _seed_two_snapshots(app_engine, tenant_id, email="cw11@example.com")

    async with tenant_context(app_engine, tenant_id) as session:
        repo = InvestmentCountryWeightsRepository(session)
        latest_for = await repo.list_latest_for_investment(inv.id, as_of_cutoff=_D1)
        latest_by = await repo.list_latest_by_investments([inv.id], as_of_cutoff=_D1)

    assert len(latest_for) == 1
    assert latest_for[0].as_of_date == _D1
    assert latest_for[0].country_iso_code == "DE"

    assert len(latest_by[inv.id]) == 1
    assert latest_by[inv.id][0].as_of_date == _D1
    assert latest_by[inv.id][0].country_iso_code == "DE"
