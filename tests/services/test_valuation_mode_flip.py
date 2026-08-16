# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The one-way flip to unitised valuation (ADR-0097 §6, strand S5).

Two layers:

* the **pure predicate** in ``services/investments/valuation_mode.py`` — the
  single source of truth for the preconditions, shared by the flip itself and
  the positions panel's disabled-button explanation;
* the **service method** ``InvestmentService.flip_to_unitised``, which must
  set the mode, delete the investment's ``'live'``-origin NAV rows, and run
  the initial full materialisation, in that order and in one transaction.

The ordering is not cosmetic: materialisation is a no-op on a ``reported``
investment, so the mode must move first; and a surviving ``'live'`` row would
be counted as ``skipped_live`` (ADR-0098 §1) instead of being removed, so the
cleanup must precede materialisation.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import ValuationModeError
from core.repositories import (
    AssetClassRepository,
    InstrumentPriceRepository,
    InvestmentCashflowRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    PositionTransactionRepository,
    UserRepository,
    tenant_context,
)
from services.investments import InvestmentService
from services.investments.valuation_mode import (
    UNITISABLE_TYPES,
    can_flip_to_unitised,
    flip_precondition_error,
    shows_positions_panel,
)

_OPEN = date(2026, 1, 5)
_PRICED = date(2026, 1, 8)


# ---------------------------------------------------------------------------
# The pure predicate — no DB, no fixtures
# ---------------------------------------------------------------------------


def test_unitisable_types_are_the_two_listed_types_and_cash() -> None:
    """ADR-0103 §1 extends the ADR-0097 §6 set by ``cash`` — three members."""
    assert {"listed_equity", "listed_bonds", "cash"} == UNITISABLE_TYPES


@pytest.mark.parametrize("unitisable", sorted(UNITISABLE_TYPES))
def test_unitisable_type_with_opening_may_flip(unitisable: str) -> None:
    assert flip_precondition_error(unitisable, "reported", has_opening=True) is None
    assert can_flip_to_unitised(unitisable, "reported", has_opening=True)


@pytest.mark.parametrize(
    "private_type",
    ["private_equity", "private_debt", "real_estate", "infra_equity", "other"],
)
def test_private_markets_types_may_not_flip(private_type: str) -> None:
    reason = flip_precondition_error(private_type, "reported", has_opening=True)
    # The exact copy, so the pre-ADR-0103 sentence cannot survive the change.
    assert reason == (
        "Unitised valuation is available for listed equity, listed bonds, and cash only."
    )


# ---------------------------------------------------------------------------
# Cash — the degenerate unitised case (ADR-0103 §1)
# ---------------------------------------------------------------------------


def test_cash_with_opening_may_flip() -> None:
    """The one clause ADR-0103 §1 changes: cash clears the type gate."""
    assert flip_precondition_error("cash", "reported", has_opening=True) is None
    assert can_flip_to_unitised("cash", "reported", has_opening=True)


def test_cash_without_opening_is_blocked_on_the_opening_clause() -> None:
    """Cash clears the type gate but not the ledger anchor — same as any type.

    Its opening is derived from the first Cash-sheet statement date
    (ADR-0103 §4); until one exists there is no balance to unitise.
    """
    reason = flip_precondition_error("cash", "reported", has_opening=False)
    assert reason == ("Add an opening transaction before switching to unitised valuation.")


def test_already_unitised_cash_may_not_flip_again() -> None:
    """The flip stays one-way for cash — including for the ADR-0103 §9 rows."""
    reason = flip_precondition_error("cash", "unitised", has_opening=True)
    assert reason == "This investment already uses unitised valuation."
    assert not can_flip_to_unitised("cash", "unitised", has_opening=True)


def test_missing_opening_blocks_the_flip() -> None:
    reason = flip_precondition_error("listed_equity", "reported", has_opening=False)
    assert reason == ("Add an opening transaction before switching to unitised valuation.")


def test_already_unitised_blocks_the_flip() -> None:
    """One-way: the predicate refuses a second flip before the DB is touched."""
    reason = flip_precondition_error("listed_equity", "unitised", has_opening=True)
    assert reason == "This investment already uses unitised valuation."
    assert not can_flip_to_unitised("listed_equity", "unitised", has_opening=True)


def test_panel_visibility_spares_private_markets_pages() -> None:
    assert not shows_positions_panel("private_equity", "reported", has_transactions=False)
    assert shows_positions_panel("listed_equity", "reported", has_transactions=False)
    assert shows_positions_panel("listed_bonds", "unitised", has_transactions=True)
    # Defensive: a ledger on an unexpected type stays reachable.
    assert shows_positions_panel("private_equity", "reported", has_transactions=True)


def test_panel_is_visible_for_cash() -> None:
    """No logic change: the panel composes over UNITISABLE_TYPES and follows.

    A cash position carries a ledger (ADR-0103 §4), so its detail page must
    offer the panel — before the flip (to expose the flip button) as much as
    after it.
    """
    assert shows_positions_panel("cash", "reported", has_transactions=False)
    assert shows_positions_panel("cash", "unitised", has_transactions=True)


# ---------------------------------------------------------------------------
# The service method — live DB
# ---------------------------------------------------------------------------


async def _seed_investment(
    app_engine: AsyncEngine,
    tenant_id,
    *,
    email: str,
    investment_type: str = "listed_equity",
    name: str = "Listed Fund",
    asset_class_code: str = "listed_class",
):
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        ac = await AssetClassRepository(session).create(
            code=asset_class_code, display_name=asset_class_code.title()
        )
        inv = await InvestmentRepository(session).create(
            name=name,
            investment_type=investment_type,
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
    return actor, inv


def _service(session) -> InvestmentService:
    return InvestmentService(
        investments=InvestmentRepository(session),
        navs=InvestmentNavRepository(session),
        cashflows=InvestmentCashflowRepository(session),
        position_transactions=PositionTransactionRepository(session),
        instrument_prices=InstrumentPriceRepository(session),
    )


async def _add_opening(app_engine, tenant_id, actor, inv, units="100") -> None:
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _service(session).add_position_transaction(
            investment_id=inv.id,
            txn_type="opening",
            trade_date=_OPEN,
            units=Decimal(units),
            currency="EUR",
            ingest_origin="manual",
            created_by=actor.id,
        )


async def _set_price(app_engine, tenant_id, actor, inv, *, on, price) -> None:
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await InstrumentPriceRepository(session).upsert(
            investment_id=inv.id,
            as_of_date=on,
            price=Decimal(price),
            currency="EUR",
            source="book",
            created_by=actor.id,
        )


async def _seed_nav(app_engine, tenant_id, actor, inv, *, on, value, origin):
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await InvestmentNavRepository(session).upsert(
            investment_id=inv.id,
            as_of_date=on,
            nav_kind="actual",
            nav_value=Decimal(value),
            currency="EUR",
            source=None,
            created_by=actor.id,
            ingest_origin=origin,
        )


async def _actual_navs(app_engine, tenant_id, inv):
    async with tenant_context(app_engine, tenant_id) as session:
        return await InvestmentNavRepository(session).list_by_investment_and_kind(inv.id, "actual")


async def _mode(app_engine, tenant_id, inv) -> str:
    async with tenant_context(app_engine, tenant_id) as session:
        row = await InvestmentRepository(session).get_by_id(inv.id)
        assert row is not None
        return row.valuation_mode


async def test_vf01_flip_sets_mode_and_materialises(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="vf01@example.com")
    await _add_opening(app_engine, tenant_id, actor, inv)
    await _set_price(app_engine, tenant_id, actor, inv, on=_PRICED, price="13")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        report = await _service(session).flip_to_unitised(inv.id, acting_user=actor.id)

    assert report.inserted == 1
    assert await _mode(app_engine, tenant_id, inv) == "unitised"

    navs = await _actual_navs(app_engine, tenant_id, inv)
    assert len(navs) == 1
    assert navs[0].nav_value == Decimal("1300.00000000")
    assert navs[0].basis == "computed"
    assert navs[0].ingest_origin == "system"


async def test_vf02_flip_deletes_live_navs_only(app_engine: AsyncEngine, seed_tenant) -> None:
    """The F1 artifacts go; excel and manual precedence rows stay."""
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="vf02@example.com")
    await _seed_nav(
        app_engine,
        tenant_id,
        actor,
        inv,
        on=date(2026, 1, 5),
        value="12.5",
        origin="live",
    )
    await _seed_nav(
        app_engine,
        tenant_id,
        actor,
        inv,
        on=date(2026, 1, 6),
        value="9000",
        origin="excel",
    )
    await _seed_nav(
        app_engine,
        tenant_id,
        actor,
        inv,
        on=date(2026, 1, 7),
        value="9100",
        origin="manual",
    )
    await _add_opening(app_engine, tenant_id, actor, inv)
    await _set_price(app_engine, tenant_id, actor, inv, on=_PRICED, price="13")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        report = await _service(session).flip_to_unitised(inv.id, acting_user=actor.id)

    # The live row was deleted before materialisation, so it was never
    # encountered as a skipped_live conflict.
    assert report.skipped_live == 0

    by_date = {n.as_of_date: n for n in await _actual_navs(app_engine, tenant_id, inv)}
    assert date(2026, 1, 5) not in by_date
    assert by_date[date(2026, 1, 6)].ingest_origin == "excel"
    assert by_date[date(2026, 1, 6)].nav_value == Decimal("9000.0000")
    assert by_date[date(2026, 1, 7)].ingest_origin == "manual"
    assert by_date[_PRICED].ingest_origin == "system"


async def test_vf03_flip_is_one_way(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="vf03@example.com")
    await _add_opening(app_engine, tenant_id, actor, inv)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _service(session).flip_to_unitised(inv.id, acting_user=actor.id)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        with pytest.raises(ValuationModeError, match="already uses unitised"):
            await _service(session).flip_to_unitised(inv.id, acting_user=actor.id)

    assert await _mode(app_engine, tenant_id, inv) == "unitised"


async def test_vf04_flip_refused_without_opening(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="vf04@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        with pytest.raises(ValuationModeError, match="opening transaction"):
            await _service(session).flip_to_unitised(inv.id, acting_user=actor.id)

    assert await _mode(app_engine, tenant_id, inv) == "reported"


async def test_vf05_flip_refused_for_private_markets_type(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(
        app_engine,
        tenant_id,
        email="vf05@example.com",
        investment_type="private_equity",
    )
    await _add_opening(app_engine, tenant_id, actor, inv)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        with pytest.raises(ValuationModeError, match="listed equity"):
            await _service(session).flip_to_unitised(inv.id, acting_user=actor.id)

    assert await _mode(app_engine, tenant_id, inv) == "reported"


async def test_vf06_failed_flip_leaves_navs_untouched(app_engine: AsyncEngine, seed_tenant) -> None:
    """A refused flip deletes nothing — the live row survives the attempt."""
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="vf06@example.com")
    await _seed_nav(
        app_engine,
        tenant_id,
        actor,
        inv,
        on=date(2026, 1, 5),
        value="12.5",
        origin="live",
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        with pytest.raises(ValuationModeError):
            await _service(session).flip_to_unitised(inv.id, acting_user=actor.id)

    navs = await _actual_navs(app_engine, tenant_id, inv)
    assert [n.ingest_origin for n in navs] == ["live"]


async def test_vf08_cash_flips_and_materialises_the_statement_balance(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """ADR-0103 §1: cash runs through the unchanged flip and materialisation.

    The whole point of the stored unity price: ``holdings × 1.0000`` *is* the
    statement balance, so the ADR-0098 service values a cash position with no
    branch of its own. Nothing in ``flip_to_unitised`` knows cash exists.
    """
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(
        app_engine,
        tenant_id,
        email="vf08@example.com",
        investment_type="cash",
        name="Cash EUR",
        asset_class_code="cash",
    )
    # The opening carries the first statement balance as units (ADR-0103 §4).
    await _add_opening(app_engine, tenant_id, actor, inv, units="25000")
    await _set_price(app_engine, tenant_id, actor, inv, on=_PRICED, price="1.0000")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        report = await _service(session).flip_to_unitised(inv.id, acting_user=actor.id)

    assert report.inserted == 1
    assert await _mode(app_engine, tenant_id, inv) == "unitised"

    navs = await _actual_navs(app_engine, tenant_id, inv)
    assert len(navs) == 1
    # 25 000 units × 1.0000 = the balance, unchanged.
    assert navs[0].nav_value == Decimal("25000.00000000")
    assert navs[0].basis == "computed"
    assert navs[0].ingest_origin == "system"


async def test_vf09_cash_without_opening_is_refused(app_engine: AsyncEngine, seed_tenant) -> None:
    """Cash clears the type gate; it does not skip the ledger anchor."""
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(
        app_engine,
        tenant_id,
        email="vf09@example.com",
        investment_type="cash",
        name="Cash EUR",
        asset_class_code="cash",
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        with pytest.raises(ValuationModeError, match="opening transaction"):
            await _service(session).flip_to_unitised(inv.id, acting_user=actor.id)

    assert await _mode(app_engine, tenant_id, inv) == "reported"


async def test_vf07_update_and_delete_rematerialise(app_engine: AsyncEngine, seed_tenant) -> None:
    """ADR-0098 §3: edit and delete are materialisation choke-points too."""
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="vf07@example.com")
    await _add_opening(app_engine, tenant_id, actor, inv, units="100")
    await _set_price(app_engine, tenant_id, actor, inv, on=_PRICED, price="10")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _service(session).flip_to_unitised(inv.id, acting_user=actor.id)

    navs = await _actual_navs(app_engine, tenant_id, inv)
    assert navs[0].nav_value == Decimal("1000.00000000")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        service = _service(session)
        opening = (await service.list_position_transactions(inv.id))[0]
        await service.update_position_transaction(
            investment_id=inv.id,
            transaction_id=opening.id,
            trade_date=_OPEN,
            units=Decimal("250"),
            acting_user=actor.id,
        )

    navs = await _actual_navs(app_engine, tenant_id, inv)
    assert navs[0].nav_value == Decimal("2500.00000000")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        service = _service(session)
        opening = (await service.list_position_transactions(inv.id))[0]
        assert await service.delete_position_transaction(
            investment_id=inv.id,
            transaction_id=opening.id,
            acting_user=actor.id,
        )

    # Holdings went to zero: the stranded 'system' row was deleted.
    assert await _actual_navs(app_engine, tenant_id, inv) == []
