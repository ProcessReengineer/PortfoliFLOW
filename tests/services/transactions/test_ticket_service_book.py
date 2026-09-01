# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""TicketService.book against the live compose Postgres (ADR-0128 §2, S2a).

Booking is where a ticket stops being intent and becomes a fact, so these
tests are about the book rather than about the service: what rows exist
afterwards, what they say, and — the half that matters more — what exists
afterwards when something goes wrong.

Two properties carry most of the weight here:

* **Both or neither** (TB-03, TB-07b). A two-leg settlement that can land
  half-written is worse than one that refuses, because the second half is
  invisible until someone reconciles a statement. The guarantee is
  structural — one session, no commits in the chain, no ``except`` anywhere
  in the emission — and TB-03 pins it by breaking the second leg on purpose.
* **Cash is exempt, the instrument is not** (TB-02, TB-04). ADR-0130's whole
  claim, end to end: an overdraft books and warns, an oversell refuses.

Coverage
--------
* TB-01: the U-SELL happy path — two legs, two effects, full attribution.
* TB-02: a U-BUY overdraws its settlement position and books anyway.
* TB-03: atomicity — a failure on the second leg leaves nothing behind.
* TB-04: a stale ``approved`` ticket is re-validated and refused.
* TB-05: the status gate (``booked`` / ``cancelled``).
* TB-06: settlement-position validation, all three refusals.
* TB-07: MD-7 ``set_inactive`` — full disposal, and the partial refusal.
* TB-08: existing attribution survives an implicit traversal.
* TB-09: the ADR-0098 materialisation fires once per leg, and only there.
* TB-10: negative net proceeds put the cash leg on the other side.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import (
    CurrencyMismatchError,
    NonNegativeHoldingsError,
    TicketIncomplete,
    TicketStateInvalid,
)
from core.repositories import (
    AssetClassRepository,
    InstrumentPriceRepository,
    InvestmentCashflowRepository,
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
    INCOMPLETE_INACTIVE_CASH_POSITION,
    INCOMPLETE_MISSING_CASH_POSITION,
    INCOMPLETE_SET_INACTIVE_NOT_FULL_DISPOSAL,
    WARNING_NEGATIVE_CASH,
    WARNING_NET_NON_POSITIVE,
)
from services.transactions.validation import TicketWarnings

_NOW = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)
_LATER = datetime(2026, 8, 31, 17, 0, tzinfo=timezone.utc)
_TRADE_DATE = date(2026, 8, 31)
_TODAY = date(2026, 8, 31)
_OPENING_DATE = date(2026, 1, 2)


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


class _Fixture:
    """The seeded world one booking test works against."""

    def __init__(self, actor, instrument, cash, foreign_cash) -> None:
        self.actor = actor
        self.instrument = instrument
        self.cash = cash
        self.foreign_cash = foreign_cash


async def _seed(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    *,
    email: str,
    instrument_units: Decimal | None = Decimal("100"),
    cash_balance: Decimal | None = Decimal("1000"),
    foreign_cash_currency: str | None = None,
) -> _Fixture:
    """Seed a user, a unitised instrument and a cash position, with ledgers.

    Deliberately the same shape as the propose-side fixture: both positions
    are ``valuation_mode='unitised'``, so every ledger write a booking makes
    trips the ADR-0098 materialisation trigger — which is the realistic case
    and the one TB-09 counts. No instrument prices are seeded, so the
    materialisation is an all-zero no-op rather than a source of NAV noise.
    """
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        asset_class = await AssetClassRepository(session).create(
            code="listed_class", display_name="Listed Class"
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
        cash = await investments.create(
            name="Cash EUR",
            investment_type="cash",
            asset_class_id=asset_class.id,
            currency="EUR",
            created_by=actor.id,
            valuation_mode="unitised",
        )
        foreign_cash = None
        if foreign_cash_currency is not None:
            foreign_cash = await investments.create(
                name=f"Cash {foreign_cash_currency}",
                investment_type="cash",
                asset_class_id=asset_class.id,
                currency=foreign_cash_currency,
                created_by=actor.id,
                valuation_mode="unitised",
            )

        ledger = PositionTransactionRepository(session)
        if instrument_units is not None:
            await ledger.add(
                investment_id=instrument.id,
                txn_type="opening",
                trade_date=_OPENING_DATE,
                units=instrument_units,
                currency=instrument.currency,
                ingest_origin="excel",
                created_by=actor.id,
            )
        if cash_balance is not None:
            await ledger.add(
                investment_id=cash.id,
                txn_type="opening",
                trade_date=_OPENING_DATE,
                units=cash_balance,
                currency=cash.currency,
                ingest_origin="excel",
                created_by=actor.id,
            )

    return _Fixture(actor, instrument, cash, foreign_cash)


def _investment_service(session) -> InvestmentService:
    """The ledger write seam, on the caller's session (ADR-0128 §2)."""
    return InvestmentService(
        investments=InvestmentRepository(session),
        navs=InvestmentNavRepository(session),
        cashflows=InvestmentCashflowRepository(session),
        position_transactions=PositionTransactionRepository(session),
        instrument_prices=InstrumentPriceRepository(session),
    )


def _service(session) -> TicketService:
    """A fully wired service — every optional dependency present."""
    return TicketService(
        tickets=TradeTicketRepository(session),
        investments=InvestmentRepository(session),
        position_transactions=PositionTransactionRepository(session),
        instrument_prices=InstrumentPriceRepository(session),
        investment_service=_investment_service(session),
    )


def _order_draft_kwargs(fixture: _Fixture, **overrides):
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


async def _ledgers(app_engine, tenant, fixture) -> tuple[list, list]:
    """Read both ledgers back in a fresh context."""
    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        ledger = PositionTransactionRepository(session)
        return (
            await ledger.list_for_investment(fixture.instrument.id),
            await ledger.list_for_investment(fixture.cash.id),
        )


# ---------------------------------------------------------------------------
# TB-01: the U-SELL happy path
# ---------------------------------------------------------------------------


async def test_tb01_sell_books_two_legs_two_effects_and_full_attribution(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant = await seed_tenant("TB-01")
    fixture = await _seed(app_engine, tenant, email="pm@tb01.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(**_order_draft_kwargs(fixture, direction="sell"))
        booked, warnings = await service.book(
            draft.id, booked_by=fixture.actor.id, now=_NOW, today=_TODAY
        )
        effects = await TradeTicketRepository(session).list_effects(draft.id)

    instrument_rows, cash_rows = await _ledgers(app_engine, tenant, fixture)

    # -- the instrument leg: units out, at the execution price --------------
    sell = next(row for row in instrument_rows if row.txn_type == "sell")
    assert sell.units == Decimal("-10")
    assert sell.price_per_unit == Decimal("10.00")
    assert sell.consideration == Decimal("100.00")  # D-C: signed cash effect

    # -- the cash leg: cash in, at 1.0000, stating no consideration ---------
    assert len(cash_rows) == 2
    cash_in = next(row for row in cash_rows if row.txn_type == "buy")
    assert cash_in.units == Decimal("100.00")
    assert cash_in.price_per_unit == Decimal("1.0000")
    assert cash_in.consideration is None

    # -- provenance on both (D-D) ------------------------------------------
    for row in (sell, cash_in):
        assert row.ingest_origin == "manual"
        assert row.source == f"ticket #{booked.ticket_number}"
        assert row.note == "quarterly rebalance"

    # -- the effects enumerate exactly those rows (ADR-0128 §2) ------------
    # Asserted as a set, not a sequence: `list_effects` orders by
    # `(emitted_at, id)` and every effect of one booking shares the
    # transaction's `NOW()`, so the surviving tie-break is a random UUID.
    # Emission order is not recoverable from the table, and no consumer
    # should be written as though it were.
    assert [effect.effect_type for effect in effects] == ["position_txn"] * 2
    assert {effect.effect_id for effect in effects} == {sell.id, cash_in.id}
    assert all(effect.prior_state is None for effect in effects)

    # -- the ticket landed, with every station attributed ------------------
    assert booked.status == "booked"
    assert booked.proposed_by == booked.approved_by == booked.booked_by == fixture.actor.id
    assert booked.proposed_at == booked.approved_at == booked.booked_at == _NOW
    assert isinstance(warnings, TicketWarnings)


# ---------------------------------------------------------------------------
# TB-02: ADR-0130 end to end
# ---------------------------------------------------------------------------


async def test_tb02_buy_overdraws_the_settlement_position_and_books(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The cash exemption on the emission path, proved end to end.

    1000 in the settlement position, a purchase costing 1500. The book
    records the overdraft and the composer is warned; nothing is refused,
    because a negative cash balance is an economic fact rather than an
    impossible state (ADR-0130, ADR-0128 Q-2).
    """
    tenant = await seed_tenant("TB-02")
    fixture = await _seed(app_engine, tenant, email="pm@tb02.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(
            **_order_draft_kwargs(fixture, units=Decimal("150"), price_per_unit=Decimal("10.00"))
        )
        booked, warnings = await service.book(
            draft.id, booked_by=fixture.actor.id, now=_NOW, today=_TODAY
        )

    assert booked.status == "booked"

    negative_cash = [w for w in warnings.warnings if w.identifier == WARNING_NEGATIVE_CASH]
    assert len(negative_cash) == 1
    assert negative_cash[0].data["resulting_balance"] == Decimal("-500")

    instrument_rows, cash_rows = await _ledgers(app_engine, tenant, fixture)
    assert holdings_as_of(cash_rows, _TRADE_DATE) == Decimal("-500")
    assert holdings_as_of(instrument_rows, _TRADE_DATE) == Decimal("250")


# ---------------------------------------------------------------------------
# TB-03: atomicity
# ---------------------------------------------------------------------------


async def test_tb03_a_failed_second_leg_leaves_nothing_behind(
    app_engine: AsyncEngine, seed_tenant, monkeypatch
) -> None:
    """Both or neither (ADR-0128 §2).

    The first leg succeeds and the second is made to fail. Because the whole
    chain runs on the caller's one session and nothing commits inside it,
    the ``tenant_context`` block's rollback is the entire recovery: there is
    no compensating write to get wrong, and nothing to reconcile later.
    """
    tenant = await seed_tenant("TB-03")
    fixture = await _seed(app_engine, tenant, email="pm@tb03.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        draft = await _service(session).create_draft(
            **_order_draft_kwargs(fixture, direction="sell")
        )

    calls = {"n": 0}
    original = InvestmentService.add_position_transaction

    async def flaky(self, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("forced")
        return await original(self, **kwargs)

    monkeypatch.setattr(InvestmentService, "add_position_transaction", flaky)

    with pytest.raises(RuntimeError, match="forced"):
        async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
            await _service(session).book(
                draft.id, booked_by=fixture.actor.id, now=_NOW, today=_TODAY
            )

    assert calls["n"] == 2  # the first leg really did run

    instrument_rows, cash_rows = await _ledgers(app_engine, tenant, fixture)
    assert [row.txn_type for row in instrument_rows] == ["opening"]
    assert [row.txn_type for row in cash_rows] == ["opening"]

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        tickets = TradeTicketRepository(session)
        assert await tickets.list_effects(draft.id) == []
        after = await tickets.get(draft.id)

    assert after is not None
    assert after.status == "draft"
    assert (after.proposed_by, after.approved_by, after.booked_by) == (None, None, None)


# ---------------------------------------------------------------------------
# TB-04: booking re-validates
# ---------------------------------------------------------------------------


async def test_tb04_a_stale_approved_ticket_is_refused(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """MD-11 / MD-21: the gates apply at Propose *and* Book.

    An approved ticket may have been sitting while its holdings moved. The
    second station re-asks the question against the book as it stands now,
    which is the whole reason the block set runs again rather than being
    trusted from propose time.
    """
    tenant = await seed_tenant("TB-04")
    fixture = await _seed(app_engine, tenant, email="pm@tb04.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(
            **_order_draft_kwargs(fixture, direction="sell", units=Decimal("100"))
        )
        await service.propose(draft.id, proposed_by=fixture.actor.id, now=_NOW, today=_TODAY)
        await TradeTicketRepository(session).set_status(
            draft.id, status="approved", actor_user_id=fixture.actor.id, now=_NOW
        )

    # The holding is sold away through the ordinary CRUD path, behind the
    # ticket's back — exactly what a week of latency looks like.
    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        await _investment_service(session).add_position_transaction(
            investment_id=fixture.instrument.id,
            txn_type="sell",
            trade_date=_TRADE_DATE,
            units=Decimal("-100"),
            currency="EUR",
            ingest_origin="manual",
            created_by=fixture.actor.id,
            price_per_unit=Decimal("10.00"),
        )

    with pytest.raises(NonNegativeHoldingsError):
        async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
            await _service(session).book(
                draft.id, booked_by=fixture.actor.id, now=_LATER, today=_TODAY
            )

    instrument_rows, cash_rows = await _ledgers(app_engine, tenant, fixture)
    assert len(instrument_rows) == 2  # the opening and the CRUD sell, nothing more
    assert [row.txn_type for row in cash_rows] == ["opening"]

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        tickets = TradeTicketRepository(session)
        assert await tickets.list_effects(draft.id) == []
        after = await tickets.get(draft.id)

    assert after is not None
    assert after.status == "approved"
    assert after.booked_by is None


# ---------------------------------------------------------------------------
# TB-05: the status gate
# ---------------------------------------------------------------------------


async def test_tb05_booked_and_cancelled_tickets_cannot_book(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant = await seed_tenant("TB-05")
    fixture = await _seed(app_engine, tenant, email="pm@tb05.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        booked_draft = await service.create_draft(**_order_draft_kwargs(fixture, direction="sell"))
        await service.book(booked_draft.id, booked_by=fixture.actor.id, now=_NOW, today=_TODAY)
        cancelled_draft = await service.create_draft(**_order_draft_kwargs(fixture))
        await service.cancel(cancelled_draft.id, cancelled_by=fixture.actor.id, now=_NOW)

    before_instrument, before_cash = await _ledgers(app_engine, tenant, fixture)

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        with pytest.raises(TicketStateInvalid) as already_booked:
            await service.book(
                booked_draft.id, booked_by=fixture.actor.id, now=_LATER, today=_TODAY
            )
        with pytest.raises(TicketStateInvalid) as cancelled:
            await service.book(
                cancelled_draft.id, booked_by=fixture.actor.id, now=_LATER, today=_TODAY
            )

    assert already_booked.value.field == "status"
    assert cancelled.value.field == "status"

    after_instrument, after_cash = await _ledgers(app_engine, tenant, fixture)
    assert len(after_instrument) == len(before_instrument)
    assert len(after_cash) == len(before_cash)


# ---------------------------------------------------------------------------
# TB-06: settlement-position validation (D-F)
# ---------------------------------------------------------------------------


async def test_tb06_settlement_position_must_exist_be_live_and_match(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Three refusals, three identifiers — the distinction is the point.

    ``missing_cash_position`` is the structured signal S4 turns into an
    inline "create one" offer. An *inactive* position must not produce it:
    the right remedy there is to revive or re-pick, not to create a second
    row for the same money.
    """
    tenant = await seed_tenant("TB-06")
    fixture = await _seed(app_engine, tenant, email="pm@tb06.example", foreign_cash_currency="USD")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        unconfirmed = await service.create_draft(
            **_order_draft_kwargs(fixture, cash_investment_id=None)
        )
        inactive = await service.create_draft(**_order_draft_kwargs(fixture))
        foreign = await service.create_draft(
            **_order_draft_kwargs(fixture, cash_investment_id=fixture.foreign_cash.id)
        )
        await InvestmentRepository(session).set_active(fixture.cash.id, False)

    for draft, expected in (
        (unconfirmed, INCOMPLETE_MISSING_CASH_POSITION),
        (inactive, INCOMPLETE_INACTIVE_CASH_POSITION),
    ):
        with pytest.raises(TicketIncomplete) as excinfo:
            async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
                await _service(session).book(
                    draft.id, booked_by=fixture.actor.id, now=_NOW, today=_TODAY
                )
        assert excinfo.value.identifier == expected
        assert excinfo.value.field == "cash_investment_id"

    with pytest.raises(CurrencyMismatchError):
        async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
            await _service(session).book(
                foreign.id, booked_by=fixture.actor.id, now=_NOW, today=_TODAY
            )

    instrument_rows, cash_rows = await _ledgers(app_engine, tenant, fixture)
    assert [row.txn_type for row in instrument_rows] == ["opening"]
    assert [row.txn_type for row in cash_rows] == ["opening"]


# ---------------------------------------------------------------------------
# TB-07: MD-7 set_inactive
# ---------------------------------------------------------------------------


async def test_tb07_full_disposal_deactivates_and_records_the_before_image(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant = await seed_tenant("TB-07a")
    fixture = await _seed(app_engine, tenant, email="pm@tb07a.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(
            **_order_draft_kwargs(
                fixture, direction="sell", units=Decimal("100"), set_inactive=True
            )
        )
        await service.book(draft.id, booked_by=fixture.actor.id, now=_NOW, today=_TODAY)

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        effects = await TradeTicketRepository(session).list_effects(draft.id)
        investment = await InvestmentRepository(session).get_by_id(fixture.instrument.id)

    assert investment is not None and investment.is_active is False

    # By type, not by position — see TB-01 on why `list_effects` cannot
    # report emission order.
    assert sorted(effect.effect_type for effect in effects) == [
        "investment_update",
        "position_txn",
        "position_txn",
    ]
    update = next(effect for effect in effects if effect.effect_type == "investment_update")
    assert update.effect_id == fixture.instrument.id
    assert update.prior_state is not None
    # The before-image is the whole row as it stood, not just the flag —
    # a reversal restores from this and nothing else (D-H).
    assert update.prior_state["is_active"] is True
    assert update.prior_state["id"] == str(fixture.instrument.id)
    assert update.prior_state["name"] == "Listed Fund"


async def test_tb07_partial_sale_refuses_deactivation_and_rolls_the_legs_back(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """An inactive investment holding units is a corrupted book (D-2, D-E).

    The refusal fires *after* both legs are written, so this also proves the
    raise rolls them back — the atomicity guarantee seen from a second angle.
    """
    tenant = await seed_tenant("TB-07b")
    fixture = await _seed(app_engine, tenant, email="pm@tb07b.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        draft = await _service(session).create_draft(
            **_order_draft_kwargs(fixture, direction="sell", units=Decimal("10"), set_inactive=True)
        )

    with pytest.raises(TicketIncomplete) as excinfo:
        async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
            await _service(session).book(
                draft.id, booked_by=fixture.actor.id, now=_NOW, today=_TODAY
            )

    assert excinfo.value.identifier == INCOMPLETE_SET_INACTIVE_NOT_FULL_DISPOSAL
    assert excinfo.value.field == "set_inactive"

    instrument_rows, cash_rows = await _ledgers(app_engine, tenant, fixture)
    assert [row.txn_type for row in instrument_rows] == ["opening"]
    assert [row.txn_type for row in cash_rows] == ["opening"]

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        assert await TradeTicketRepository(session).list_effects(draft.id) == []
        investment = await InvestmentRepository(session).get_by_id(fixture.instrument.id)
        after = await TradeTicketRepository(session).get(draft.id)

    assert investment is not None and investment.is_active is True
    assert after is not None and after.status == "draft"


# ---------------------------------------------------------------------------
# TB-08: attribution
# ---------------------------------------------------------------------------


async def test_tb08_an_existing_proposer_survives_the_implicit_traversal(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Q-6: the traversal fills the gaps, it does not overwrite the record.

    A ticket proposed by A and booked by B records exactly that. Overwriting
    A would erase the only evidence that two people were involved — which is
    the whole point of having the columns.
    """
    tenant = await seed_tenant("TB-08")
    fixture = await _seed(app_engine, tenant, email="pm@tb08.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        booker = await UserRepository(session).create(
            email="booker@tb08.example", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(**_order_draft_kwargs(fixture, direction="sell"))
        await service.propose(draft.id, proposed_by=fixture.actor.id, now=_NOW, today=_TODAY)

    async with tenant_context(app_engine, tenant, user_id=booker.id) as session:
        booked, _ = await _service(session).book(
            draft.id, booked_by=booker.id, now=_LATER, today=_TODAY
        )

    assert booked.proposed_by == fixture.actor.id
    assert booked.proposed_at == _NOW
    assert booked.approved_by == booker.id
    assert booked.booked_by == booker.id
    assert booked.approved_at == booked.booked_at == _LATER


# ---------------------------------------------------------------------------
# TB-09: the ADR-0098 trigger
# ---------------------------------------------------------------------------


async def test_tb09_materialisation_fires_once_per_leg(
    app_engine: AsyncEngine, seed_tenant, monkeypatch
) -> None:
    """The trigger is the write seam's, not the emission's (D-A).

    Both positions are unitised, so exactly two materialisations run — one
    per emitted row, each bounded to the trade date. The emission never
    calls the materialiser itself; that it fires at all is evidence the
    booking went through the sanctioned seam rather than around it.
    """
    tenant = await seed_tenant("TB-09")
    fixture = await _seed(app_engine, tenant, email="pm@tb09.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        draft = await _service(session).create_draft(
            **_order_draft_kwargs(fixture, direction="sell")
        )

    seen: list[tuple[UUID, object]] = []
    original = NavMaterialisationService.materialise

    async def spy(self, investment_id, *, acting_user, since=None):
        seen.append((investment_id, since))
        return await original(self, investment_id, acting_user=acting_user, since=since)

    monkeypatch.setattr(NavMaterialisationService, "materialise", spy)

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        await _service(session).book(draft.id, booked_by=fixture.actor.id, now=_NOW, today=_TODAY)

    assert len(seen) == 2
    assert [investment_id for investment_id, _ in seen] == [
        fixture.instrument.id,
        fixture.cash.id,
    ]
    assert {since for _, since in seen} == {_TRADE_DATE}


# ---------------------------------------------------------------------------
# TB-10: negative net proceeds
# ---------------------------------------------------------------------------


async def test_tb10_a_sale_costing_more_than_it_yields_moves_cash_out(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """D-B's real subject, booked.

    Gross 100, fees 150. The ticket says ``sell`` and the settlement
    position still loses 50 — a cash leg keyed off the ticket's direction
    would book an inflow and the ledger would be quietly, permanently wrong.
    """
    tenant = await seed_tenant("TB-10")
    fixture = await _seed(app_engine, tenant, email="pm@tb10.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(
            **_order_draft_kwargs(fixture, direction="sell", fees=Decimal("150.00"))
        )
        booked, warnings = await service.book(
            draft.id, booked_by=fixture.actor.id, now=_NOW, today=_TODAY
        )

    assert booked.status == "booked"
    assert [w.identifier for w in warnings.warnings] == [WARNING_NET_NON_POSITIVE]

    _, cash_rows = await _ledgers(app_engine, tenant, fixture)
    cash_out = next(row for row in cash_rows if row.txn_type == "sell")
    assert cash_out.units == Decimal("-50.00")
    assert cash_out.price_per_unit == Decimal("1.0000")
    assert holdings_as_of(cash_rows, _TRADE_DATE) == Decimal("950")
