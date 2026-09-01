# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""TicketService.reverse against the live compose Postgres (ADR-0128 §6, S2c).

Reversal is the only operation in the area that *removes* facts, so these
tests are written from the book's point of view: what is gone afterwards,
what came back, and — the half that matters more — what is still exactly
where it was when the reversal refused.

Three properties carry most of the weight:

* **All-or-nothing, checked before touched** (TV-03, TV-04, TV-05, TV-11).
  Every effect is verified untouched before a single row is deleted, and the
  deletions themselves ride the caller's transaction. A reversal either
  happens completely or leaves no trace of having been attempted.
* **The book, not the row, is the witness** (TV-03). Modification is read off
  the audit log, because ``position_transactions.updated_at`` is not
  maintained by its update path and a check built on it would wave an edited
  ledger row straight through.
* **A created shell is deleted only while it is still only the shell**
  (TV-07, TV-08, TV-09). Platform artefacts — computed NAVs, prices, the
  identifiers written at creation — cascade with it. A human's row retains
  it, deactivated, and the report says which table did that.

Effects are asserted **by** ``effect_type``, never by position: every effect
of one booking shares the transaction's ``NOW()``, so the surviving tie-break
is a random UUID and emission order is not recoverable from the table.

Coverage
--------
* TV-01: the U-SELL reversal — both legs gone, holdings restored, effects
  retained, one materialisation per leg.
* TV-02: ``set_inactive`` reversed — the investment comes back from its
  before-image.
* TV-03: an edited effect blocks, and nothing is deleted.
* TV-04: a deleted effect blocks, naming it.
* TV-05: units sold on since the booking block, chained from the ledger's
  own refusal.
* TV-06: the R-SEC-SELL reversal — four kinds of row undone.
* TV-07: R-COMMIT — the shell is deleted, the ticket unlinked, identifiers
  cascade.
* TV-08: R-SEC-BUY with a user NAV — the shell is retained, deactivated.
* TV-09: U-NEW with only platform rows — the shell goes, and takes them.
* TV-10: the gates — status, reason, and ``cancel`` still refusing ``booked``.
* TV-11: atomicity — a failure mid-reversal restores everything.
* TV-12: corrections are cancel plus re-enter.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import (
    NonNegativeHoldingsError,
    TicketIncomplete,
    TicketReversalBlocked,
    TicketStateInvalid,
)
from core.repositories import (
    AssetClassRepository,
    AuditLogRepository,
    InstrumentPriceRepository,
    InvestmentCashflowRepository,
    InvestmentIdentifierRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    PositionTransactionRepository,
    TradeTicketRepository,
    UserRepository,
    tenant_context,
)
from services.investments.holdings import holdings_as_of
from services.investments.investment_service import InvestmentService
from services.investments.nav_materialisation import NavMaterialisationService
from services.transactions import TicketService
from services.transactions.constants import (
    INCOMPLETE_MISSING_CANCEL_REASON,
    REVERSAL_CAUSE_CONSUMED,
    REVERSAL_CAUSE_HOLDINGS_CONSUMED,
    REVERSAL_CAUSE_MODIFIED,
)
from services.transactions.emission import (
    EFFECT_CASHFLOW,
    EFFECT_INVESTMENT_UPDATE,
    EFFECT_NAV,
    EFFECT_POSITION_TXN,
)

_NOW = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)
_REVERSED_AT = datetime(2026, 9, 2, 14, 0, tzinfo=timezone.utc)
_TRADE_DATE = date(2026, 8, 31)
_TODAY = date(2026, 8, 31)
_OPENING_DATE = date(2026, 1, 2)
_PRIOR_NAV_DATE = date(2026, 6, 30)
_LATER_NAV_DATE = date(2026, 9, 1)
_REASON = "booked against the wrong custodian statement"

_NEW_NAME = "Continuation Fund V"
_OPENING_UNITS = Decimal("100")
_CASH_BALANCE = Decimal("1000000")


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


class _Fixture:
    """The seeded world one reversal test works against.

    Deliberately carries both valuation modes and a settlement position, so
    one seeding function serves the order flows and the reported ones — a
    reversal test's subject is what a booking *left behind*, and that is the
    same world whichever flow put it there.
    """

    def __init__(self, actor, asset_class, instrument, stake, cash) -> None:
        self.actor = actor
        self.asset_class = asset_class
        #: Unit-dealt and ``unitised``: the U-BUY / U-SELL target.
        self.instrument = instrument
        #: Statement-valued and ``reported``: what R-SEC-SELL disposes of.
        self.stake = stake
        self.cash = cash


async def _seed(app_engine: AsyncEngine, tenant_id: UUID, *, email: str) -> _Fixture:
    """Seed a user, a unitised instrument, a reported stake and a cash position."""
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        asset_class = await AssetClassRepository(session).create(
            code="mixed_class", display_name="Mixed Class"
        )
        investments = InvestmentRepository(session)
        instrument = await investments.create(
            name="Listed Fund",
            investment_type="listed_equity",
            asset_class_id=asset_class.id,
            currency="EUR",
            created_by=actor.id,
            anlv_code="anlv_13",
            valuation_mode="unitised",
        )
        stake = await investments.create(
            name="Legacy Buyout Fund III",
            investment_type="private_equity",
            asset_class_id=asset_class.id,
            currency="EUR",
            created_by=actor.id,
            anlv_code="anlv_13",
            valuation_mode="reported",
            commitment_amount=Decimal("10000000"),
        )
        cash = await investments.create(
            name="Cash EUR",
            investment_type="cash",
            asset_class_id=asset_class.id,
            currency="EUR",
            created_by=actor.id,
            valuation_mode="unitised",
        )

        await InvestmentNavRepository(session).upsert(
            investment_id=stake.id,
            as_of_date=_PRIOR_NAV_DATE,
            nav_kind="actual",
            nav_value=Decimal("2400000"),
            currency="EUR",
            source="excel-import",
            created_by=actor.id,
            ingest_origin="excel",
        )

        ledger = PositionTransactionRepository(session)
        await ledger.add(
            investment_id=instrument.id,
            txn_type="opening",
            trade_date=_OPENING_DATE,
            units=_OPENING_UNITS,
            currency="EUR",
            ingest_origin="excel",
            created_by=actor.id,
        )
        await ledger.add(
            investment_id=cash.id,
            txn_type="opening",
            trade_date=_OPENING_DATE,
            units=_CASH_BALANCE,
            currency="EUR",
            ingest_origin="excel",
            created_by=actor.id,
        )

    return _Fixture(actor, asset_class, instrument, stake, cash)


def _investment_service(session) -> InvestmentService:
    """The write seam, wired for identifiers too — a creating flow needs them (D-L)."""
    return InvestmentService(
        investments=InvestmentRepository(session),
        navs=InvestmentNavRepository(session),
        cashflows=InvestmentCashflowRepository(session),
        identifiers=InvestmentIdentifierRepository(session),
        position_transactions=PositionTransactionRepository(session),
        instrument_prices=InstrumentPriceRepository(session),
    )


def _service(session) -> TicketService:
    """A fully wired service — reversal needs the audit log and the cashflows."""
    return TicketService(
        tickets=TradeTicketRepository(session),
        investments=InvestmentRepository(session),
        position_transactions=PositionTransactionRepository(session),
        instrument_prices=InstrumentPriceRepository(session),
        investment_service=_investment_service(session),
        navs=InvestmentNavRepository(session),
        audit_log=AuditLogRepository(session),
        cashflows=InvestmentCashflowRepository(session),
    )


def _order_kwargs(fixture: _Fixture, **overrides):
    """A complete, bookable U-BUY draft; override to vary the case."""
    values = {
        "kind": "order",
        "direction": "buy",
        "currency": "EUR",
        "trade_date": _TRADE_DATE,
        "created_by": fixture.actor.id,
        "now": _NOW,
        "investment_id": fixture.instrument.id,
        "cash_investment_id": fixture.cash.id,
        "units": Decimal("10"),
        "price_per_unit": Decimal("10.00"),
        "note": "quarterly rebalance",
    }
    values.update(overrides)
    return values


def _master_data(fixture: _Fixture, **overrides) -> dict[str, object]:
    """A complete creating payload as JSONB carries it; ``None`` drops a key."""
    values: dict[str, object] = {
        "name": _NEW_NAME,
        "investment_type": "private_equity",
        "asset_class_id": str(fixture.asset_class.id),
        "currency": "EUR",
        "anlv_code": "anlv_13",
        "vintage_year": "2024",
    }
    values.update(overrides)
    return {key: value for key, value in values.items() if value is not None}


def _secondary_sell_kwargs(fixture: _Fixture, **overrides):
    """A complete, bookable R-SEC-SELL draft."""
    values = {
        "kind": "secondary",
        "direction": "sell",
        "currency": "EUR",
        "trade_date": _TRADE_DATE,
        "created_by": fixture.actor.id,
        "now": _NOW,
        "investment_id": fixture.stake.id,
        "cash_investment_id": fixture.cash.id,
        "net_amount": Decimal("2250000"),
        "note": "secondary exit",
    }
    values.update(overrides)
    return values


def _by_type(effects) -> dict[str, list]:
    """Group effects by ``effect_type`` — the assertion order does not fix."""
    grouped: dict[str, list] = {}
    for effect in effects:
        grouped.setdefault(effect.effect_type, []).append(effect)
    return grouped


async def _book(app_engine, tenant, fixture, **draft_kwargs):
    """Create and book one ticket, returning ``(booked, effects)``."""
    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(**draft_kwargs)
        booked, _ = await service.book(draft.id, booked_by=fixture.actor.id, now=_NOW, today=_TODAY)
        effects = await TradeTicketRepository(session).list_effects(draft.id)
    return booked, effects


async def _reverse(app_engine, tenant, fixture, ticket_id, *, reason: str = _REASON):
    """Reverse one booked ticket in its own transaction."""
    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        return await _service(session).reverse(
            ticket_id,
            cancelled_by=fixture.actor.id,
            now=_REVERSED_AT,
            reason=reason,
        )


async def _ledgers(app_engine, tenant, fixture, *, instrument_id=None):
    """Read the instrument and cash ledgers back in a fresh context."""
    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        ledger = PositionTransactionRepository(session)
        return (
            await ledger.list_for_investment(instrument_id or fixture.instrument.id),
            await ledger.list_for_investment(fixture.cash.id),
        )


def _materialisation_spy(monkeypatch) -> list[tuple[UUID, object]]:
    """Record every ADR-0098 materialisation call, then delegate to the real one."""
    seen: list[tuple[UUID, object]] = []
    original = NavMaterialisationService.materialise

    async def spy(self, investment_id, *, acting_user, since=None):
        seen.append((investment_id, since))
        return await original(self, investment_id, acting_user=acting_user, since=since)

    monkeypatch.setattr(NavMaterialisationService, "materialise", spy)
    return seen


# ---------------------------------------------------------------------------
# TV-01: the U-SELL reversal
# ---------------------------------------------------------------------------


async def test_tv01_sell_reversal_removes_both_legs_and_keeps_the_effects(
    app_engine: AsyncEngine, seed_tenant, monkeypatch
) -> None:
    """The happy path, and the two halves of it that are easy to get wrong.

    The ledger goes back to exactly what it was — both legs, not one — and
    the *effects* do not. ``trade_ticket_effects`` rows survive a reversal by
    design (D-AD): they are the record of what this ticket once did, and a
    history surface that could only say "cancelled" would have lost the
    interesting half.
    """
    tenant = await seed_tenant("TV-01")
    fixture = await _seed(app_engine, tenant, email="pm@tv01.example")
    booked, effects = await _book(
        app_engine, tenant, fixture, **_order_kwargs(fixture, direction="sell")
    )
    assert len(effects) == 2

    seen = _materialisation_spy(monkeypatch)
    report = await _reverse(app_engine, tenant, fixture, booked.id)

    instrument_rows, cash_rows = await _ledgers(app_engine, tenant, fixture)

    # -- the book is exactly as it was -------------------------------------
    assert [row.txn_type for row in instrument_rows] == ["opening"]
    assert [row.txn_type for row in cash_rows] == ["opening"]
    assert holdings_as_of(instrument_rows, _TRADE_DATE) == _OPENING_UNITS
    assert holdings_as_of(cash_rows, _TRADE_DATE) == _CASH_BALANCE

    # -- the ticket is cancelled, with its reason --------------------------
    assert report.ticket.status == "cancelled"
    assert report.ticket.cancel_reason == _REASON
    assert report.ticket.cancelled_at == _REVERSED_AT
    assert report.ticket.booked_by == fixture.actor.id  # the booking is still recorded
    assert report.shell is None
    assert {effect.effect_id for effect in report.reversed} == {e.effect_id for e in effects}

    # -- and the effects are still there (D-AD) ----------------------------
    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        surviving = await TradeTicketRepository(session).list_effects(booked.id)
    assert len(surviving) == 2
    assert {e.effect_id for e in surviving} == {e.effect_id for e in effects}

    # -- one materialisation per deleted leg (as TB-09 counts the booking) -
    assert len(seen) == 2
    assert {investment_id for investment_id, _ in seen} == {
        fixture.instrument.id,
        fixture.cash.id,
    }
    assert {since for _, since in seen} == {_TRADE_DATE}


# ---------------------------------------------------------------------------
# TV-02: the before-image, restored
# ---------------------------------------------------------------------------


async def test_tv02_set_inactive_is_undone_from_the_before_image(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """MD-7's flag comes back from ``prior_state``, not from an assumption.

    A full disposal with ``set_inactive`` emits three effects, and the third
    is the whole ``investments`` row as it stood (D-H). The reversal writes
    back the one field an emission can change — and the effect stays on the
    table afterwards like the other two.
    """
    tenant = await seed_tenant("TV-02")
    fixture = await _seed(app_engine, tenant, email="pm@tv02.example")
    booked, effects = await _book(
        app_engine,
        tenant,
        fixture,
        **_order_kwargs(
            fixture,
            direction="sell",
            units=_OPENING_UNITS,
            set_inactive=True,
        ),
    )
    grouped = _by_type(effects)
    assert sorted(grouped) == [EFFECT_INVESTMENT_UPDATE, EFFECT_POSITION_TXN]
    assert grouped[EFFECT_INVESTMENT_UPDATE][0].prior_state is not None

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        after_booking = await InvestmentRepository(session).get_by_id(fixture.instrument.id)
    assert after_booking is not None and after_booking.is_active is False

    report = await _reverse(app_engine, tenant, fixture, booked.id)

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        restored = await InvestmentRepository(session).get_by_id(fixture.instrument.id)
        surviving = await TradeTicketRepository(session).list_effects(booked.id)

    assert restored is not None and restored.is_active is True
    assert restored.commitment_amount == fixture.instrument.commitment_amount
    assert restored.anlv_code == fixture.instrument.anlv_code
    assert len(surviving) == 3
    assert report.shell is None
    assert report.ticket.status == "cancelled"


# ---------------------------------------------------------------------------
# TV-03: an edited effect blocks
# ---------------------------------------------------------------------------


async def test_tv03_an_edited_ledger_row_blocks_and_nothing_is_deleted(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The case ``updated_at`` cannot see, and the reason D-Y reads the audit log.

    ``position_transactions.update`` writes by ORM assignment with no
    ``onupdate`` behind the column, so the edited row's ``updated_at`` still
    reads as its insert time. The audit trigger fired anyway, which is what
    the check asks — and the refusal leaves the booking completely intact,
    including the leg the reversal would have reached second.
    """
    tenant = await seed_tenant("TV-03")
    fixture = await _seed(app_engine, tenant, email="pm@tv03.example")
    booked, effects = await _book(
        app_engine, tenant, fixture, **_order_kwargs(fixture, direction="sell")
    )

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        instrument_rows = await PositionTransactionRepository(session).list_for_investment(
            fixture.instrument.id
        )
        sell = next(row for row in instrument_rows if row.txn_type == "sell")
        await _investment_service(session).update_position_transaction(
            investment_id=fixture.instrument.id,
            transaction_id=sell.id,
            trade_date=_TRADE_DATE,
            units=Decimal("-5"),
            price_per_unit=Decimal("10.00"),
            consideration=Decimal("50.00"),
            acting_user=fixture.actor.id,
        )

    with pytest.raises(TicketReversalBlocked) as excinfo:
        await _reverse(app_engine, tenant, fixture, booked.id)

    assert excinfo.value.cause == REVERSAL_CAUSE_MODIFIED
    assert excinfo.value.effect_type == EFFECT_POSITION_TXN
    assert excinfo.value.effect_id == sell.id

    # Nothing was deleted, and the ticket is still booked.
    instrument_rows, cash_rows = await _ledgers(app_engine, tenant, fixture)
    assert sorted(row.txn_type for row in instrument_rows) == ["opening", "sell"]
    assert sorted(row.txn_type for row in cash_rows) == ["buy", "opening"]

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        still = await TradeTicketRepository(session).get(booked.id)
        surviving = await TradeTicketRepository(session).list_effects(booked.id)
    assert still is not None and still.status == "booked"
    assert still.cancel_reason is None
    assert len(surviving) == len(effects) == 2


# ---------------------------------------------------------------------------
# TV-04: a deleted effect blocks
# ---------------------------------------------------------------------------


async def test_tv04_a_deleted_effect_row_blocks_and_names_itself(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Absence is checked before modification, and it names the row that is gone.

    The instrument leg is untouched and would delete cleanly; the reversal
    still refuses, because half a reversal is worse than none — the operator
    would be left reconciling a settlement with one side missing.
    """
    tenant = await seed_tenant("TV-04")
    fixture = await _seed(app_engine, tenant, email="pm@tv04.example")
    booked, _ = await _book(app_engine, tenant, fixture, **_order_kwargs(fixture, direction="sell"))

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        cash_rows = await PositionTransactionRepository(session).list_for_investment(
            fixture.cash.id
        )
        cash_in = next(row for row in cash_rows if row.txn_type == "buy")
        await _investment_service(session).delete_position_transaction(
            investment_id=fixture.cash.id,
            transaction_id=cash_in.id,
            acting_user=fixture.actor.id,
        )

    with pytest.raises(TicketReversalBlocked) as excinfo:
        await _reverse(app_engine, tenant, fixture, booked.id)

    assert excinfo.value.cause == REVERSAL_CAUSE_CONSUMED
    assert excinfo.value.effect_type == EFFECT_POSITION_TXN
    assert excinfo.value.effect_id == cash_in.id

    instrument_rows, _ = await _ledgers(app_engine, tenant, fixture)
    assert sorted(row.txn_type for row in instrument_rows) == ["opening", "sell"]

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        still = await TradeTicketRepository(session).get(booked.id)
    assert still is not None and still.status == "booked"


# ---------------------------------------------------------------------------
# TV-05: units sold on since the booking
# ---------------------------------------------------------------------------


async def test_tv05_units_traded_on_since_the_booking_block_the_reversal(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The economic form of "consumed", and it comes from the ledger's own guard.

    The emitted row is untouched and still there, so both checks pass; it is
    the *delete* that cannot happen, because the units this buy created have
    since been sold. ``NonNegativeHoldingsError`` says so and is chained
    rather than swallowed — the ledger's diagnosis stays in the traceback
    while the operator gets a sentence about the later trade.

    The cash leg survives the attempt: the instrument leg is undone first, it
    raised, and the whole transaction rolled back.
    """
    tenant = await seed_tenant("TV-05")
    fixture = await _seed(app_engine, tenant, email="pm@tv05.example")
    booked, _ = await _book(app_engine, tenant, fixture, **_order_kwargs(fixture))

    # 100 opening + 10 bought, then 105 sold on: undoing the buy would leave -5.
    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        await _investment_service(session).add_position_transaction(
            investment_id=fixture.instrument.id,
            txn_type="sell",
            trade_date=date(2026, 9, 1),
            units=Decimal("-105"),
            currency="EUR",
            ingest_origin="manual",
            created_by=fixture.actor.id,
            price_per_unit=Decimal("11.00"),
        )

    with pytest.raises(TicketReversalBlocked) as excinfo:
        await _reverse(app_engine, tenant, fixture, booked.id)

    assert excinfo.value.cause == REVERSAL_CAUSE_HOLDINGS_CONSUMED
    assert excinfo.value.effect_type == EFFECT_POSITION_TXN
    assert isinstance(excinfo.value.__cause__, NonNegativeHoldingsError)

    instrument_rows, cash_rows = await _ledgers(app_engine, tenant, fixture)
    assert sorted(row.txn_type for row in instrument_rows) == ["buy", "opening", "sell"]
    # The cash leg the reversal never reached is still there.
    assert sorted(row.txn_type for row in cash_rows) == ["opening", "sell"]


# ---------------------------------------------------------------------------
# TV-06: the R-SEC-SELL reversal
# ---------------------------------------------------------------------------


async def test_tv06_secondary_sell_reversal_undoes_four_kinds_of_row(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The widest emission, reversed: cashflow, NAV, reactivation, cash.

    The prior statement NAV is the control — a reversal deletes the row the
    booking *wrote*, and nothing else, so the stake's history before the sale
    is untouched.
    """
    tenant = await seed_tenant("TV-06")
    fixture = await _seed(app_engine, tenant, email="pm@tv06.example")
    booked, effects = await _book(app_engine, tenant, fixture, **_secondary_sell_kwargs(fixture))
    grouped = _by_type(effects)
    assert sorted(grouped) == [
        EFFECT_CASHFLOW,
        EFFECT_INVESTMENT_UPDATE,
        EFFECT_NAV,
        EFFECT_POSITION_TXN,
    ]

    report = await _reverse(app_engine, tenant, fixture, booked.id)

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        flows = await InvestmentCashflowRepository(session).list_by_investment(fixture.stake.id)
        navs = await InvestmentNavRepository(session).list_by_investment(fixture.stake.id)
        stake = await InvestmentRepository(session).get_by_id(fixture.stake.id)
        cash_rows = await PositionTransactionRepository(session).list_for_investment(
            fixture.cash.id
        )
        surviving = await TradeTicketRepository(session).list_effects(booked.id)

    assert flows == []
    assert [row.as_of_date for row in navs] == [_PRIOR_NAV_DATE]
    assert stake is not None and stake.is_active is True
    assert [row.txn_type for row in cash_rows] == ["opening"]
    assert holdings_as_of(cash_rows, _TRADE_DATE) == _CASH_BALANCE
    assert len(surviving) == 4
    assert report.shell is None
    assert report.ticket.status == "cancelled"


# ---------------------------------------------------------------------------
# TV-07: R-COMMIT — the shell is deleted
# ---------------------------------------------------------------------------


async def test_tv07_commitment_reversal_deletes_the_shell_and_unlinks_the_ticket(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A created row is not restored, it is removed — and the link goes first.

    ``trade_tickets.investment_id`` is ``ON DELETE RESTRICT``, so the unlink
    is not tidiness: without it the delete fails at the constraint. The
    identifiers written at creation are not effects (D-L) and are gone too,
    by CASCADE — which is exactly why enumerating them would have been
    recording rows that cannot outlive their owner.

    The effect itself stays, still carrying ``prior_state IS NULL``: the
    ticket's history says it created an investment, even though the
    investment is gone.
    """
    tenant = await seed_tenant("TV-07")
    fixture = await _seed(app_engine, tenant, email="pm@tv07.example")
    booked, effects = await _book(
        app_engine,
        tenant,
        fixture,
        kind="commitment",
        direction="buy",
        currency="EUR",
        trade_date=_TRADE_DATE,
        created_by=fixture.actor.id,
        now=_NOW,
        commitment_amount=Decimal("5000000"),
        master_data=_master_data(
            fixture,
            identifier_scheme="preqin",
            identifier_value="PQ-777",
        ),
    )
    assert [effect.effect_type for effect in effects] == [EFFECT_INVESTMENT_UPDATE]
    assert effects[0].prior_state is None
    created_id = effects[0].effect_id

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        identifiers = await InvestmentIdentifierRepository(session).list_for_investment(created_id)
    assert identifiers != []

    report = await _reverse(app_engine, tenant, fixture, booked.id)

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        assert await InvestmentRepository(session).get_by_id(created_id) is None
        assert await InvestmentRepository(session).get_by_name(_NEW_NAME) is None
        assert await InvestmentIdentifierRepository(session).list_for_investment(created_id) == []
        ticket = await TradeTicketRepository(session).get(booked.id)
        surviving = await TradeTicketRepository(session).list_effects(booked.id)

    assert report.shell is not None
    assert report.shell.investment_id == created_id
    assert report.shell.deleted is True
    assert report.shell.retained_because is None
    assert ticket is not None and ticket.investment_id is None
    assert ticket.status == "cancelled"
    assert len(surviving) == 1 and surviving[0].prior_state is None


# ---------------------------------------------------------------------------
# TV-08: R-SEC-BUY — a user row retains the shell
# ---------------------------------------------------------------------------


async def test_tv08_a_user_nav_retains_the_shell_deactivated(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Someone has adopted the row, so the reversal stops short of deleting it.

    Every ``investments`` child is ``ON DELETE CASCADE``, so deleting the
    shell would take the operator's NAV with it silently. The row is retired
    instead — ``is_active`` false, invisible in a picker (MD-12) — and the
    report names the table that kept it, because a row nobody was told about
    is worse than one they were.
    """
    tenant = await seed_tenant("TV-08")
    fixture = await _seed(app_engine, tenant, email="pm@tv08.example")
    booked, effects = await _book(
        app_engine,
        tenant,
        fixture,
        kind="secondary",
        direction="buy",
        currency="EUR",
        trade_date=_TRADE_DATE,
        created_by=fixture.actor.id,
        now=_NOW,
        cash_investment_id=fixture.cash.id,
        gross_amount=Decimal("750000"),
        fees=Decimal("5000"),
        master_data=_master_data(
            fixture,
            acquired_nav="800000",
            assumed_unfunded="250000",
        ),
    )
    grouped = _by_type(effects)
    created_id = grouped[EFFECT_INVESTMENT_UPDATE][0].effect_id
    booked_nav_id = grouped[EFFECT_NAV][0].effect_id

    # An operator adds a statement NAV of their own after the booking.
    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        await _investment_service(session).add_nav(
            investment_id=created_id,
            as_of_date=_LATER_NAV_DATE,
            nav_kind="actual",
            nav_value=Decimal("820000"),
            currency="EUR",
            source="Q3 statement",
            created_by=fixture.actor.id,
        )

    report = await _reverse(app_engine, tenant, fixture, booked.id)

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        shell = await InvestmentRepository(session).get_by_id(created_id)
        navs = await InvestmentNavRepository(session).list_by_investment(created_id)
        cash_rows = await PositionTransactionRepository(session).list_for_investment(
            fixture.cash.id
        )
        ticket = await TradeTicketRepository(session).get(booked.id)
        surviving = await TradeTicketRepository(session).list_effects(booked.id)

    # The booking's own rows are undone.
    assert [row.as_of_date for row in navs] == [_LATER_NAV_DATE]
    assert booked_nav_id not in {row.id for row in navs}
    assert [row.txn_type for row in cash_rows] == ["opening"]

    # The shell survives, retired, still named by its ticket.
    assert shell is not None and shell.is_active is False
    assert report.shell is not None
    assert report.shell.investment_id == created_id
    assert report.shell.deleted is False
    assert report.shell.retained_because is not None
    assert "investment_navs" in report.shell.retained_because
    assert ticket is not None and ticket.investment_id == created_id
    assert ticket.status == "cancelled"
    assert len(surviving) == len(effects)


# ---------------------------------------------------------------------------
# TV-09: U-NEW — platform rows do not retain the shell
# ---------------------------------------------------------------------------


async def test_tv09_platform_artefacts_do_not_retain_the_shell(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Prices and computed NAVs cascade; if they blocked, no U-NEW could ever reverse.

    A unitised instrument acquires an ``instrument_prices`` series and a
    ``'system'`` NAV series as a matter of course — they are the platform's
    own arithmetic (ADR-0098), re-derivable and owned by nobody. Letting them
    veto the clean-up would mean the flow that most needs reversing is the
    one that never could.
    """
    tenant = await seed_tenant("TV-09")
    fixture = await _seed(app_engine, tenant, email="pm@tv09.example")
    booked, effects = await _book(
        app_engine,
        tenant,
        fixture,
        kind="order",
        direction="buy",
        currency="EUR",
        trade_date=_TRADE_DATE,
        created_by=fixture.actor.id,
        now=_NOW,
        cash_investment_id=fixture.cash.id,
        units=Decimal("10"),
        price_per_unit=Decimal("10.00"),
        master_data=_master_data(
            fixture,
            name="New Listed Fund",
            investment_type="listed_equity",
        ),
    )
    grouped = _by_type(effects)
    created_id = grouped[EFFECT_INVESTMENT_UPDATE][0].effect_id

    # The platform's own rows: a price series, and the computed NAV over it.
    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        await InstrumentPriceRepository(session).upsert(
            investment_id=created_id,
            as_of_date=_TRADE_DATE,
            price=Decimal("10.50"),
            currency="EUR",
            source="market-data",
            created_by=fixture.actor.id,
            ingest_origin="live",
        )
        await InvestmentNavRepository(session).upsert_computed(
            investment_id=created_id,
            as_of_date=_TRADE_DATE,
            nav_kind="actual",
            nav_value=Decimal("105.00"),
            currency="EUR",
            source="computed",
            created_by=fixture.actor.id,
        )
        seeded = await InvestmentNavRepository(session).list_by_investment(created_id)
    assert [row.ingest_origin for row in seeded] == ["system"]

    report = await _reverse(app_engine, tenant, fixture, booked.id)

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        assert await InvestmentRepository(session).get_by_id(created_id) is None
        assert await InstrumentPriceRepository(session).list_by_investment(created_id) == []
        assert await InvestmentNavRepository(session).list_by_investment(created_id) == []
        ticket = await TradeTicketRepository(session).get(booked.id)
        _, cash_rows = await _ledgers(app_engine, tenant, fixture)

    assert report.shell is not None and report.shell.deleted is True
    assert report.shell.retained_because is None
    assert ticket is not None and ticket.investment_id is None
    assert [row.txn_type for row in cash_rows] == ["opening"]


# ---------------------------------------------------------------------------
# TV-10: the gates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["draft", "proposed", "cancelled"])
async def test_tv10_only_a_booked_ticket_reverses(
    app_engine: AsyncEngine, seed_tenant, status: str
) -> None:
    """Nothing else has effects to undo (D-AE)."""
    tenant = await seed_tenant(f"TV-10-{status}")
    fixture = await _seed(app_engine, tenant, email=f"pm@tv10{status}.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(**_order_kwargs(fixture, direction="sell"))
        if status == "proposed":
            await service.propose(draft.id, proposed_by=fixture.actor.id, now=_NOW, today=_TODAY)
        elif status == "cancelled":
            await service.cancel(draft.id, cancelled_by=fixture.actor.id, now=_NOW)

        with pytest.raises(TicketStateInvalid) as excinfo:
            await service.reverse(
                draft.id, cancelled_by=fixture.actor.id, now=_REVERSED_AT, reason=_REASON
            )

    assert excinfo.value.field == "status"
    assert status in str(excinfo.value)


async def test_tv10_a_blank_reason_is_refused(app_engine: AsyncEngine, seed_tenant) -> None:
    """A booking undone without a stated reason is not an audit trail (D-X)."""
    tenant = await seed_tenant("TV-10b")
    fixture = await _seed(app_engine, tenant, email="pm@tv10b.example")
    booked, _ = await _book(app_engine, tenant, fixture, **_order_kwargs(fixture, direction="sell"))

    with pytest.raises(TicketIncomplete) as excinfo:
        await _reverse(app_engine, tenant, fixture, booked.id, reason="   ")

    assert excinfo.value.identifier == INCOMPLETE_MISSING_CANCEL_REASON
    assert excinfo.value.field == "reason"

    # And the refusal wrote nothing.
    instrument_rows, _ = await _ledgers(app_engine, tenant, fixture)
    assert sorted(row.txn_type for row in instrument_rows) == ["opening", "sell"]


async def test_tv10_cancel_still_refuses_a_booked_ticket(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The two terminal endings stay separate: ``cancel`` never touches a ledger."""
    tenant = await seed_tenant("TV-10c")
    fixture = await _seed(app_engine, tenant, email="pm@tv10c.example")
    booked, _ = await _book(app_engine, tenant, fixture, **_order_kwargs(fixture, direction="sell"))

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        with pytest.raises(TicketStateInvalid):
            await _service(session).cancel(
                booked.id, cancelled_by=fixture.actor.id, now=_REVERSED_AT, reason=_REASON
            )

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        still = await TradeTicketRepository(session).get(booked.id)
    assert still is not None and still.status == "booked"


# ---------------------------------------------------------------------------
# TV-11: atomicity
# ---------------------------------------------------------------------------


async def test_tv11_a_failure_mid_reversal_restores_everything(
    app_engine: AsyncEngine, seed_tenant, monkeypatch
) -> None:
    """Both or neither, on the way out as much as on the way in.

    The NAV delete is the third step of an R-SEC-SELL reversal, so by the
    time it raises the cash leg and the distribution are already gone from
    the session. Nothing catches, nothing compensates: the caller's
    ``tenant_context`` block rolls back and the booking stands, which is the
    same guarantee ``book`` gets from the same mechanism.
    """
    tenant = await seed_tenant("TV-11")
    fixture = await _seed(app_engine, tenant, email="pm@tv11.example")
    booked, effects = await _book(app_engine, tenant, fixture, **_secondary_sell_kwargs(fixture))

    async def explode(self, nav_id):
        raise RuntimeError("nav delete failed")

    monkeypatch.setattr(InvestmentService, "delete_nav", explode)

    with pytest.raises(RuntimeError, match="nav delete failed"):
        await _reverse(app_engine, tenant, fixture, booked.id)

    monkeypatch.undo()

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        flows = await InvestmentCashflowRepository(session).list_by_investment(fixture.stake.id)
        navs = await InvestmentNavRepository(session).list_by_investment(fixture.stake.id)
        stake = await InvestmentRepository(session).get_by_id(fixture.stake.id)
        cash_rows = await PositionTransactionRepository(session).list_for_investment(
            fixture.cash.id
        )
        ticket = await TradeTicketRepository(session).get(booked.id)
        surviving = await TradeTicketRepository(session).list_effects(booked.id)

    assert len(flows) == 1
    assert {row.as_of_date for row in navs} == {_PRIOR_NAV_DATE, _TRADE_DATE}
    assert stake is not None and stake.is_active is False
    assert sorted(row.txn_type for row in cash_rows) == ["buy", "opening"]
    assert ticket is not None and ticket.status == "booked"
    assert ticket.cancelled_at is None
    assert len(surviving) == len(effects) == 4


# ---------------------------------------------------------------------------
# TV-12: corrections are cancel plus re-enter
# ---------------------------------------------------------------------------


async def test_tv12_an_identical_ticket_books_cleanly_after_a_reversal(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """ADR-0128 §6's remedy, end to end.

    After ``booked`` there is no editing, only reversal — so the correction
    path has to actually work: the reversed booking must leave the book in a
    state the same ticket can be entered into again. It does, and the second
    ticket gets its own number and its own effects while the first keeps its.
    """
    tenant = await seed_tenant("TV-12")
    fixture = await _seed(app_engine, tenant, email="pm@tv12.example")
    first, first_effects = await _book(
        app_engine, tenant, fixture, **_order_kwargs(fixture, direction="sell")
    )
    await _reverse(app_engine, tenant, fixture, first.id)

    second, second_effects = await _book(
        app_engine, tenant, fixture, **_order_kwargs(fixture, direction="sell")
    )

    assert second.status == "booked"
    assert second.ticket_number == first.ticket_number + 1
    assert len(second_effects) == 2
    assert {e.effect_id for e in second_effects}.isdisjoint({e.effect_id for e in first_effects})

    instrument_rows, cash_rows = await _ledgers(app_engine, tenant, fixture)
    assert sorted(row.txn_type for row in instrument_rows) == ["opening", "sell"]
    assert sorted(row.txn_type for row in cash_rows) == ["buy", "opening"]
    assert holdings_as_of(instrument_rows, _TRADE_DATE) == _OPENING_UNITS - Decimal("10")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        reversed_ticket = await TradeTicketRepository(session).get(first.id)
        retained = await TradeTicketRepository(session).list_effects(first.id)
    assert reversed_ticket is not None and reversed_ticket.status == "cancelled"
    assert len(retained) == 2
