# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""TicketService tests against the live compose Postgres (ADR-0128 §3, §4).

Where ``tests/repositories/test_trade_ticket_repository.py`` pins the
*mechanism* — numbering, column round-trips, draft-only editing — these pin
the **policy**: what "complete and validated" means per flow, which gaps
refuse the transition, and which merely warn. The distinction that most of
this file exists to defend is D-2's: a block protects the book's
invariants, a warning protects nobody and blocks nothing.

Coverage
--------
* TS-01: ``create_draft`` per kind; vocabulary refusals; the commitment shape.
* TS-02: the ``propose`` happy path — status, attribution, no warnings.
* TS-03: every block, each verified to have written **nothing**; plus the
  MD-21 converse (an existing investment with no AnlV code proposes fine).
* TS-04: every warning, with its documented data shape, blocking nothing.
* TS-05: ``propose`` on a non-draft, and on an unknown id.
* TS-06: ``cancel`` from draft / proposed / a hand-forged ``booked``.
* TS-07: the cash-effect derivation, exercised directly.
* TS-08: ``preview`` — the read-only dry run (P-0b): quiet on a sparse
  transient ticket, writing nothing, reporting the oversell offence the
  raising path still raises, and returning the warnings ``propose`` returns.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import (
    CurrencyMismatchError,
    NonNegativeHoldingsError,
    TicketIncomplete,
    TicketNotFound,
    TicketStateInvalid,
)
from core.repositories import (
    AssetClassRepository,
    InstrumentPriceRepository,
    InvestmentRepository,
    PositionTransactionRepository,
    TradeTicketDTO,
    TradeTicketRepository,
    UserRepository,
    tenant_context,
)
from services.transactions import TicketService
from services.transactions.constants import (
    BLOCK_MISSING_ANLV,
    BLOCK_MISSING_PRICE,
    BLOCK_OVERSELL,
    INCOMPLETE_COMMITMENT_SHAPE,
    INCOMPLETE_MISSING_CANCEL_REASON,
    INCOMPLETE_MISSING_CASH_POSITION,
    MD_ANLV_CODE,
    MD_ASSET_CLASS_ID,
    MD_CURRENCY,
    MD_INVESTMENT_TYPE,
    MD_NAME,
    STATUSES,
    WARNING_FUTURE_TRADE_DATE,
    WARNING_NEGATIVE_CASH,
    WARNING_NET_NON_POSITIVE,
    WARNING_PRICE_DEVIATION,
)
from services.transactions.validation import (
    TicketBlock,
    TicketWarnings,
    derive_cash_effect,
)

_NOW = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)
_TRADE_DATE = date(2026, 8, 31)
_TODAY = date(2026, 8, 31)
_OPENING_DATE = date(2026, 1, 2)


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


class _Fixture:
    """The seeded world one test works against."""

    def __init__(self, actor, instrument, cash, asset_class) -> None:
        self.actor = actor
        self.instrument = instrument
        self.cash = cash
        self.asset_class = asset_class


async def _seed(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    *,
    email: str,
    currency: str = "EUR",
    instrument_currency: str | None = None,
    anlv_code: str | None = "anlv_13",
    instrument_units: Decimal | None = None,
    cash_balance: Decimal | None = None,
    prices: dict[date, Decimal] | None = None,
) -> _Fixture:
    """Seed one user, a unitised instrument and a cash position, with ledgers.

    Everything goes through the ordinary repositories — the same rows the
    Excel import and the per-investment CRUD write — so the service under
    test sees a realistic book rather than a fixture-shaped one. The cash
    position is an ordinary ``investment_type='cash'`` row (ADR-0100) whose
    balance *is* its holdings (F-2), which is why an opening is all it takes
    to give it money.
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
            currency=instrument_currency or currency,
            created_by=actor.id,
            anlv_code=anlv_code,
            valuation_mode="unitised",
        )
        cash = await investments.create(
            name=f"Cash {currency}",
            investment_type="cash",
            asset_class_id=asset_class.id,
            currency=currency,
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
        if prices:
            price_repo = InstrumentPriceRepository(session)
            for as_of, price in prices.items():
                await price_repo.upsert(
                    investment_id=instrument.id,
                    as_of_date=as_of,
                    price=price,
                    currency=instrument.currency,
                    source="test",
                    created_by=actor.id,
                )

    return _Fixture(actor, instrument, cash, asset_class)


def _service(session) -> TicketService:
    """A fully wired service — every optional repository present."""
    return TicketService(
        tickets=TradeTicketRepository(session),
        investments=InvestmentRepository(session),
        position_transactions=PositionTransactionRepository(session),
        instrument_prices=InstrumentPriceRepository(session),
    )


def _master_data(fixture: _Fixture, **overrides) -> dict[str, object]:
    """A complete creating payload (D-J); override to drop or vary one key.

    Since S2b the payload has to carry everything the ``investments`` row is
    ``NOT NULL`` in — name, type, asset class, currency (MD-12, D-J) — so the
    older two-key spelling no longer reaches the gates these tests are about.
    Pass ``None`` for a key to remove it.
    """
    values: dict[str, object] = {
        MD_NAME: "New Fund IV",
        MD_INVESTMENT_TYPE: "private_equity",
        MD_ASSET_CLASS_ID: str(fixture.asset_class.id),
        MD_CURRENCY: "EUR",
        MD_ANLV_CODE: "anlv_13",
    }
    values.update(overrides)
    return {key: value for key, value in values.items() if value is not None}


def _order_draft_kwargs(fixture: _Fixture, **overrides):
    """A complete, proposable U-BUY draft; override to break one thing."""
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
    }
    values.update(overrides)
    return values


# ---------------------------------------------------------------------------
# TS-01: create_draft
# ---------------------------------------------------------------------------


async def test_ts01_create_draft_per_kind(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant = await seed_tenant("TS-01")
    fixture = await _seed(app_engine, tenant, email="pm@ts01.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)

        order = await service.create_draft(**_order_draft_kwargs(fixture))
        commitment = await service.create_draft(
            kind="commitment",
            direction="buy",
            currency="EUR",
            trade_date=_TRADE_DATE,
            created_by=fixture.actor.id,
            now=_NOW,
            commitment_amount=Decimal("5000000"),
            master_data=_master_data(fixture),
        )
        secondary = await service.create_draft(
            kind="secondary",
            direction="sell",
            currency="EUR",
            trade_date=_TRADE_DATE,
            created_by=fixture.actor.id,
            now=_NOW,
            investment_id=fixture.instrument.id,
            cash_investment_id=fixture.cash.id,
            net_amount=Decimal("250000"),
        )

    assert [order.status, commitment.status, secondary.status] == ["draft"] * 3
    assert order.units == Decimal("10.00000000")
    # A commitment books no cash leg (R-3 / MD-19).
    assert commitment.cash_investment_id is None
    assert commitment.commitment_amount == Decimal("5000000.0000")
    assert secondary.net_amount == Decimal("250000.0000")


async def test_ts01_create_draft_may_be_sparse(app_engine: AsyncEngine, seed_tenant) -> None:
    """MD-2: the draft exists from the first explicit gesture, however early."""
    tenant = await seed_tenant("TS-01b")
    fixture = await _seed(app_engine, tenant, email="pm@ts01b.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        draft = await _service(session).create_draft(
            kind="order",
            direction="buy",
            currency="EUR",
            trade_date=_TRADE_DATE,
            created_by=fixture.actor.id,
            now=_NOW,
        )

    assert draft.investment_id is None
    assert draft.units is None
    assert draft.price_per_unit is None
    assert draft.cash_investment_id is None


async def test_ts01_create_draft_rejects_bad_vocabulary(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant = await seed_tenant("TS-01c")
    fixture = await _seed(app_engine, tenant, email="pm@ts01c.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)

        with pytest.raises(TicketStateInvalid) as bad_kind:
            await service.create_draft(**_order_draft_kwargs(fixture, kind="rebalance"))
        assert bad_kind.value.field == "kind"

        with pytest.raises(TicketStateInvalid) as bad_direction:
            await service.create_draft(**_order_draft_kwargs(fixture, direction="short"))
        assert bad_direction.value.field == "direction"


async def test_ts01_commitment_shape_is_enforced(app_engine: AsyncEngine, seed_tenant) -> None:
    """R-3 / MD-19, refused as a rule rather than as a constraint name."""
    tenant = await seed_tenant("TS-01d")
    fixture = await _seed(app_engine, tenant, email="pm@ts01d.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)

        with pytest.raises(TicketIncomplete) as with_cash:
            await service.create_draft(
                kind="commitment",
                direction="buy",
                currency="EUR",
                trade_date=_TRADE_DATE,
                created_by=fixture.actor.id,
                now=_NOW,
                cash_investment_id=fixture.cash.id,
                commitment_amount=Decimal("1000000"),
            )
        assert with_cash.value.identifier == INCOMPLETE_COMMITMENT_SHAPE
        assert with_cash.value.field == "cash_investment_id"

        with pytest.raises(TicketIncomplete) as as_sell:
            await service.create_draft(
                kind="commitment",
                direction="sell",
                currency="EUR",
                trade_date=_TRADE_DATE,
                created_by=fixture.actor.id,
                now=_NOW,
                commitment_amount=Decimal("1000000"),
            )
        assert as_sell.value.identifier == INCOMPLETE_COMMITMENT_SHAPE


async def test_ts01_update_draft_validates_provided_fields(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant = await seed_tenant("TS-01e")
    fixture = await _seed(app_engine, tenant, email="pm@ts01e.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(**_order_draft_kwargs(fixture))

        with pytest.raises(TicketStateInvalid):
            await service.update_draft(draft.id, kind="rebalance")

        updated = await service.update_draft(draft.id, units=Decimal("25"), note="revised")

    assert updated.units == Decimal("25.00000000")
    assert updated.note == "revised"


# ---------------------------------------------------------------------------
# TS-02: the propose happy path
# ---------------------------------------------------------------------------


async def test_ts02_propose_u_sell_happy_path(app_engine: AsyncEngine, seed_tenant) -> None:
    """Blocks pass, the status flips with attribution, and nothing is warned."""
    tenant = await seed_tenant("TS-02")
    fixture = await _seed(
        app_engine,
        tenant,
        email="pm@ts02.example",
        instrument_units=Decimal("500"),
        cash_balance=Decimal("100000"),
        prices={_TRADE_DATE: Decimal("10.00")},
    )

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(
            **_order_draft_kwargs(
                fixture,
                direction="sell",
                units=Decimal("200"),
                price_per_unit=Decimal("10.00"),
            )
        )
        proposed, warnings = await service.propose(
            draft.id,
            proposed_by=fixture.actor.id,
            now=_NOW,
            today=_TODAY,
        )

    assert proposed.status == "proposed"
    assert proposed.proposed_by == fixture.actor.id
    assert proposed.proposed_at == _NOW
    assert warnings == TicketWarnings(warnings=())
    assert not warnings
    assert warnings.identifiers == ()


# ---------------------------------------------------------------------------
# TS-03: the blocks — each raises and writes nothing
# ---------------------------------------------------------------------------


async def _assert_still_draft(app_engine: AsyncEngine, tenant: UUID, actor_id, ticket_id) -> None:
    """A block wrote nothing: the ticket is still a draft afterwards."""
    async with tenant_context(app_engine, tenant, user_id=actor_id) as session:
        reloaded = await TradeTicketRepository(session).get(ticket_id)
    assert reloaded is not None
    assert reloaded.status == "draft"
    assert reloaded.proposed_by is None
    assert reloaded.proposed_at is None


async def test_ts03_missing_price_blocks(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant = await seed_tenant("TS-03a")
    fixture = await _seed(app_engine, tenant, email="pm@ts03a.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(**_order_draft_kwargs(fixture, price_per_unit=None))
        with pytest.raises(TicketIncomplete) as excinfo:
            await service.propose(draft.id, proposed_by=fixture.actor.id, now=_NOW, today=_TODAY)

    assert excinfo.value.identifier == BLOCK_MISSING_PRICE
    assert excinfo.value.field == "price_per_unit"
    await _assert_still_draft(app_engine, tenant, fixture.actor.id, draft.id)


async def test_ts03_missing_cash_position_blocks(app_engine: AsyncEngine, seed_tenant) -> None:
    """MD-3: no default is ever picked, so an unconfirmed position refuses."""
    tenant = await seed_tenant("TS-03b")
    fixture = await _seed(app_engine, tenant, email="pm@ts03b.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(**_order_draft_kwargs(fixture, cash_investment_id=None))
        with pytest.raises(TicketIncomplete) as excinfo:
            await service.propose(draft.id, proposed_by=fixture.actor.id, now=_NOW, today=_TODAY)

    assert excinfo.value.identifier == INCOMPLETE_MISSING_CASH_POSITION
    await _assert_still_draft(app_engine, tenant, fixture.actor.id, draft.id)


async def test_ts03_cash_position_wrong_currency_blocks(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """PortfoliFLOW never converts on your behalf (ADR-0099/0100)."""
    tenant = await seed_tenant("TS-03c")
    fixture = await _seed(app_engine, tenant, email="pm@ts03c.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        investments = InvestmentRepository(session)
        instrument = await investments.get_by_id(fixture.instrument.id)
        assert instrument is not None
        usd_cash = await investments.create(
            name="Cash USD",
            investment_type="cash",
            asset_class_id=instrument.asset_class_id,
            currency="USD",
            created_by=fixture.actor.id,
            valuation_mode="unitised",
        )
        service = _service(session)
        draft = await service.create_draft(
            **_order_draft_kwargs(fixture, cash_investment_id=usd_cash.id)
        )
        with pytest.raises(CurrencyMismatchError) as excinfo:
            await service.propose(draft.id, proposed_by=fixture.actor.id, now=_NOW, today=_TODAY)

    assert excinfo.value.field == "currency"
    await _assert_still_draft(app_engine, tenant, fixture.actor.id, draft.id)


async def test_ts03_investment_currency_mismatch_blocks(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """F-3: the ticket currency is the investment's, with no conversion."""
    tenant = await seed_tenant("TS-03d")
    fixture = await _seed(
        app_engine,
        tenant,
        email="pm@ts03d.example",
        currency="EUR",
        instrument_currency="USD",
    )

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(**_order_draft_kwargs(fixture))
        with pytest.raises(CurrencyMismatchError) as excinfo:
            await service.propose(draft.id, proposed_by=fixture.actor.id, now=_NOW, today=_TODAY)

    assert "USD" in str(excinfo.value)
    await _assert_still_draft(app_engine, tenant, fixture.actor.id, draft.id)


async def test_ts03_oversell_blocks(app_engine: AsyncEngine, seed_tenant) -> None:
    """The instrument leg keeps the ADR-0097 §4 guard unconditionally (Q-2)."""
    tenant = await seed_tenant("TS-03e")
    fixture = await _seed(
        app_engine,
        tenant,
        email="pm@ts03e.example",
        instrument_units=Decimal("100"),
        cash_balance=Decimal("100000"),
    )

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(
            **_order_draft_kwargs(fixture, direction="sell", units=Decimal("200"))
        )
        with pytest.raises(NonNegativeHoldingsError) as excinfo:
            await service.propose(draft.id, proposed_by=fixture.actor.id, now=_NOW, today=_TODAY)

    assert excinfo.value.field == "units"
    await _assert_still_draft(app_engine, tenant, fixture.actor.id, draft.id)


async def test_ts03_missing_anlv_blocks_investment_creating_flow(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """MD-11 / MD-21: the gate applies where the investment row is created."""
    tenant = await seed_tenant("TS-03f")
    fixture = await _seed(
        app_engine, tenant, email="pm@ts03f.example", cash_balance=Decimal("100000")
    )

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(
            **_order_draft_kwargs(
                fixture,
                investment_id=None,
                master_data=_master_data(
                    fixture,
                    **{MD_NAME: "Brand New Fund", MD_ANLV_CODE: None},
                ),
            )
        )
        with pytest.raises(TicketIncomplete) as excinfo:
            await service.propose(draft.id, proposed_by=fixture.actor.id, now=_NOW, today=_TODAY)

    assert excinfo.value.identifier == BLOCK_MISSING_ANLV
    await _assert_still_draft(app_engine, tenant, fixture.actor.id, draft.id)


async def test_ts03_existing_investment_without_anlv_proposes_fine(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """MD-21's converse: U-BUY / U-SELL carry no AnlV gate at all."""
    tenant = await seed_tenant("TS-03g")
    fixture = await _seed(
        app_engine,
        tenant,
        email="pm@ts03g.example",
        anlv_code=None,
        cash_balance=Decimal("100000"),
    )

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        investment = await InvestmentRepository(session).get_by_id(fixture.instrument.id)
        assert investment is not None and investment.anlv_code is None

        service = _service(session)
        draft = await service.create_draft(**_order_draft_kwargs(fixture))
        proposed, _ = await service.propose(
            draft.id, proposed_by=fixture.actor.id, now=_NOW, today=_TODAY
        )

    assert proposed.status == "proposed"


async def test_ts03_secondary_buy_requires_acquired_nav(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant = await seed_tenant("TS-03h")
    fixture = await _seed(
        app_engine, tenant, email="pm@ts03h.example", cash_balance=Decimal("100000")
    )

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(
            kind="secondary",
            direction="buy",
            currency="EUR",
            trade_date=_TRADE_DATE,
            created_by=fixture.actor.id,
            now=_NOW,
            cash_investment_id=fixture.cash.id,
            gross_amount=Decimal("750000"),
            master_data=_master_data(fixture, **{MD_NAME: "Secondary Stake"}),
        )
        with pytest.raises(TicketIncomplete) as excinfo:
            await service.propose(draft.id, proposed_by=fixture.actor.id, now=_NOW, today=_TODAY)

    assert excinfo.value.field == "master_data"
    await _assert_still_draft(app_engine, tenant, fixture.actor.id, draft.id)


async def test_ts03_commitment_proposes_without_a_cash_position(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """MD-19: no cash moves with a commitment, so none is demanded."""
    tenant = await seed_tenant("TS-03i")
    fixture = await _seed(app_engine, tenant, email="pm@ts03i.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(
            kind="commitment",
            direction="buy",
            currency="EUR",
            trade_date=_TRADE_DATE,
            created_by=fixture.actor.id,
            now=_NOW,
            commitment_amount=Decimal("5000000"),
            master_data=_master_data(fixture),
        )
        proposed, warnings = await service.propose(
            draft.id, proposed_by=fixture.actor.id, now=_NOW, today=_TODAY
        )

    assert proposed.status == "proposed"
    assert not warnings


# ---------------------------------------------------------------------------
# TS-04: the warnings — documented shapes, and they block nothing
# ---------------------------------------------------------------------------


async def test_ts04_negative_cash_warns_and_books_anyway(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """OP-06 is struck (MD-5): nothing is refused because cash goes negative."""
    tenant = await seed_tenant("TS-04a")
    fixture = await _seed(
        app_engine,
        tenant,
        email="pm@ts04a.example",
        instrument_units=Decimal("500"),
        cash_balance=Decimal("1000"),
    )

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(
            **_order_draft_kwargs(
                fixture,
                units=Decimal("200"),
                price_per_unit=Decimal("10.00"),
            )
        )
        proposed, warnings = await service.propose(
            draft.id, proposed_by=fixture.actor.id, now=_NOW, today=_TODAY
        )

    assert proposed.status == "proposed"  # warned, never blocked
    assert warnings.identifiers == (WARNING_NEGATIVE_CASH,)
    data = warnings.warnings[0].data
    # 1 000 held − (200 × 10.00) spent.
    assert data["resulting_balance"] == Decimal("-1000")
    assert data["currency"] == "EUR"


async def test_ts04_price_deviation_warns_above_five_percent(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant = await seed_tenant("TS-04b")
    fixture = await _seed(
        app_engine,
        tenant,
        email="pm@ts04b.example",
        cash_balance=Decimal("1000000"),
        prices={_OPENING_DATE: Decimal("9.00"), _TRADE_DATE: Decimal("10.00")},
    )

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(
            **_order_draft_kwargs(fixture, price_per_unit=Decimal("11.00"))
        )
        _, warnings = await service.propose(
            draft.id, proposed_by=fixture.actor.id, now=_NOW, today=_TODAY
        )

    assert warnings.identifiers == (WARNING_PRICE_DEVIATION,)
    data = warnings.warnings[0].data
    assert data["reference_price"] == Decimal("10.00000000")
    assert data["reference_date"] == _TRADE_DATE
    assert data["deviation_ratio"] == Decimal("0.1")


async def test_ts04_price_deviation_silent_at_four_percent(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant = await seed_tenant("TS-04c")
    fixture = await _seed(
        app_engine,
        tenant,
        email="pm@ts04c.example",
        cash_balance=Decimal("1000000"),
        prices={_TRADE_DATE: Decimal("10.00")},
    )

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(
            **_order_draft_kwargs(fixture, price_per_unit=Decimal("10.40"))
        )
        _, warnings = await service.propose(
            draft.id, proposed_by=fixture.actor.id, now=_NOW, today=_TODAY
        )

    assert not warnings


async def test_ts04_no_price_series_is_not_suspicious(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant = await seed_tenant("TS-04d")
    fixture = await _seed(
        app_engine, tenant, email="pm@ts04d.example", cash_balance=Decimal("1000000")
    )

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(
            **_order_draft_kwargs(fixture, price_per_unit=Decimal("999.00"))
        )
        _, warnings = await service.propose(
            draft.id, proposed_by=fixture.actor.id, now=_NOW, today=_TODAY
        )

    assert not warnings


async def test_ts04_net_non_positive_warns_on_a_sell(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant = await seed_tenant("TS-04e")
    fixture = await _seed(
        app_engine,
        tenant,
        email="pm@ts04e.example",
        instrument_units=Decimal("500"),
        cash_balance=Decimal("1000000"),
        prices={_TRADE_DATE: Decimal("10.00")},
    )

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(
            **_order_draft_kwargs(
                fixture,
                direction="sell",
                units=Decimal("10"),
                price_per_unit=Decimal("10.00"),
                fees=Decimal("120.00"),
            )
        )
        _, warnings = await service.propose(
            draft.id, proposed_by=fixture.actor.id, now=_NOW, today=_TODAY
        )

    assert warnings.identifiers == (WARNING_NET_NON_POSITIVE,)
    data = warnings.warnings[0].data
    # 10 × 10.00 gross − 120.00 of fees.
    assert data["net_amount"] == Decimal("-20.0000")
    assert data["currency"] == "EUR"


async def test_ts04_future_trade_date_warns(app_engine: AsyncEngine, seed_tenant) -> None:
    """The injected ``today`` is the only clock — no hidden read (ADR-0127)."""
    tenant = await seed_tenant("TS-04f")
    fixture = await _seed(
        app_engine, tenant, email="pm@ts04f.example", cash_balance=Decimal("1000000")
    )

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(**_order_draft_kwargs(fixture))
        _, warnings = await service.propose(
            draft.id,
            proposed_by=fixture.actor.id,
            now=_NOW,
            today=date(2026, 8, 1),
        )

    assert warnings.identifiers == (WARNING_FUTURE_TRADE_DATE,)
    data = warnings.warnings[0].data
    assert data["trade_date"] == _TRADE_DATE
    assert data["today"] == date(2026, 8, 1)


async def test_ts04_several_warnings_are_all_returned(app_engine: AsyncEngine, seed_tenant) -> None:
    """One ticket can be several kinds of unwise; a composer shows them together."""
    tenant = await seed_tenant("TS-04g")
    fixture = await _seed(
        app_engine,
        tenant,
        email="pm@ts04g.example",
        cash_balance=Decimal("1000"),
        prices={_TRADE_DATE: Decimal("10.00")},
    )

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(
            **_order_draft_kwargs(
                fixture,
                units=Decimal("200"),
                price_per_unit=Decimal("11.00"),
            )
        )
        _, warnings = await service.propose(
            draft.id,
            proposed_by=fixture.actor.id,
            now=_NOW,
            today=date(2026, 8, 1),
        )

    assert set(warnings.identifiers) == {
        WARNING_NEGATIVE_CASH,
        WARNING_PRICE_DEVIATION,
        WARNING_FUTURE_TRADE_DATE,
    }
    assert len(warnings) == 3


# ---------------------------------------------------------------------------
# TS-05: propose against the wrong state
# ---------------------------------------------------------------------------


async def test_ts05_propose_on_a_non_draft_raises(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant = await seed_tenant("TS-05a")
    fixture = await _seed(
        app_engine, tenant, email="pm@ts05a.example", cash_balance=Decimal("1000000")
    )

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(**_order_draft_kwargs(fixture))
        await service.propose(draft.id, proposed_by=fixture.actor.id, now=_NOW, today=_TODAY)

        with pytest.raises(TicketStateInvalid) as excinfo:
            await service.propose(draft.id, proposed_by=fixture.actor.id, now=_NOW, today=_TODAY)

    assert excinfo.value.field == "status"


async def test_ts05_propose_unknown_ticket_raises(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant = await seed_tenant("TS-05b")
    fixture = await _seed(app_engine, tenant, email="pm@ts05b.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        with pytest.raises(TicketNotFound):
            await _service(session).propose(
                uuid4(), proposed_by=fixture.actor.id, now=_NOW, today=_TODAY
            )


# ---------------------------------------------------------------------------
# TS-06: cancel
# ---------------------------------------------------------------------------


async def test_ts06_cancel_draft_needs_no_reason(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant = await seed_tenant("TS-06a")
    fixture = await _seed(app_engine, tenant, email="pm@ts06a.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(**_order_draft_kwargs(fixture))
        cancelled = await service.cancel(draft.id, cancelled_by=fixture.actor.id, now=_NOW)

    assert cancelled.status == "cancelled"
    assert cancelled.cancelled_at == _NOW
    assert cancelled.cancel_reason is None


async def test_ts06_cancel_proposed_requires_a_reason(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant = await seed_tenant("TS-06b")
    fixture = await _seed(
        app_engine, tenant, email="pm@ts06b.example", cash_balance=Decimal("1000000")
    )

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(**_order_draft_kwargs(fixture))
        await service.propose(draft.id, proposed_by=fixture.actor.id, now=_NOW, today=_TODAY)

        with pytest.raises(TicketIncomplete) as excinfo:
            await service.cancel(draft.id, cancelled_by=fixture.actor.id, now=_NOW)
        assert excinfo.value.identifier == INCOMPLETE_MISSING_CANCEL_REASON

        cancelled = await service.cancel(
            draft.id,
            cancelled_by=fixture.actor.id,
            now=_NOW,
            reason="Counterparty withdrew.",
        )

    assert cancelled.status == "cancelled"
    assert cancelled.cancel_reason == "Counterparty withdrew."


async def test_ts06_cancel_booked_is_refused(app_engine: AsyncEngine, seed_tenant) -> None:
    """A booked ticket is reversed (S2c), not cancelled (ADR-0128 §6)."""
    tenant = await seed_tenant("TS-06c")
    fixture = await _seed(app_engine, tenant, email="pm@ts06c.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(**_order_draft_kwargs(fixture))

        # Hand-forged: the service has no booking path in S1, so the stations
        # are walked through the repository directly to reach 'booked'.
        tickets = TradeTicketRepository(session)
        for status in ("proposed", "approved", "booked"):
            await tickets.set_status(
                draft.id, status=status, actor_user_id=fixture.actor.id, now=_NOW
            )

        with pytest.raises(TicketStateInvalid) as excinfo:
            await service.cancel(draft.id, cancelled_by=fixture.actor.id, now=_NOW, reason="oops")

    assert excinfo.value.field == "status"


async def test_ts06_cancel_unknown_ticket_raises(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant = await seed_tenant("TS-06d")
    fixture = await _seed(app_engine, tenant, email="pm@ts06d.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        with pytest.raises(TicketNotFound):
            await _service(session).cancel(uuid4(), cancelled_by=fixture.actor.id, now=_NOW)


# ---------------------------------------------------------------------------
# TS-07: the cash-effect derivation, exercised directly
# ---------------------------------------------------------------------------


def test_ts07_cash_effect_takes_a_stated_net_as_given() -> None:
    """Shape 1 wins outright — fees are understood to be inside it already."""
    assert derive_cash_effect(
        direction="buy",
        net_amount=Decimal("1000"),
        gross_amount=Decimal("900"),
        fees=Decimal("50"),
        taxes=Decimal("10"),
    ) == Decimal("1000")


def test_ts07_cash_effect_from_gross_plus_costs() -> None:
    """Shape 2: costs are added on a buy and subtracted on a sell."""
    assert derive_cash_effect(
        direction="buy",
        gross_amount=Decimal("1000"),
        fees=Decimal("20"),
        taxes=Decimal("5"),
    ) == Decimal("1025")
    assert derive_cash_effect(
        direction="sell",
        gross_amount=Decimal("1000"),
        fees=Decimal("20"),
        taxes=Decimal("5"),
    ) == Decimal("975")


def test_ts07_cash_effect_from_units_times_price() -> None:
    """Shape 3, with absent fees and taxes reading as zero."""
    assert derive_cash_effect(
        direction="buy",
        units=Decimal("200"),
        price_per_unit=Decimal("10.50"),
    ) == Decimal("2100.00")
    assert derive_cash_effect(
        direction="sell",
        units=Decimal("200"),
        price_per_unit=Decimal("10.50"),
        fees=Decimal("100"),
    ) == Decimal("2000.00")


def test_ts07_cash_effect_is_none_when_nothing_is_derivable() -> None:
    """A draft that does not yet say what it moves is not an error."""
    assert derive_cash_effect(direction="buy") is None
    assert derive_cash_effect(direction="buy", units=Decimal("10")) is None
    assert derive_cash_effect(direction="sell", price_per_unit=Decimal("10")) is None


# ---------------------------------------------------------------------------
# TS-08: preview — the read-only dry run (P-0b)
# ---------------------------------------------------------------------------


def _transient(fixture: _Fixture, tenant: UUID, **overrides) -> TradeTicketDTO:
    """A ticket that was never persisted — what the S4a composer will hand in.

    Built directly rather than through ``create_draft`` on purpose: the whole
    point of :meth:`TicketService.preview` is that the ticket need not exist,
    and a test that first created a row would be exercising something else.
    The defaults are the sparse ones — a composer that has typed almost
    nothing — and each test overrides only the fields it is about.
    """
    values: dict[str, object] = {
        "id": uuid4(),
        "tenant_id": tenant,
        # Never allocated: no row was created, so no number was taken.
        "ticket_number": 0,
        "kind": "order",
        "direction": "buy",
        "status": "draft",
        "investment_id": None,
        "cash_investment_id": None,
        "trade_date": _TRADE_DATE,
        "settlement_date": None,
        "units": None,
        "price_per_unit": None,
        "gross_amount": None,
        "fees": None,
        "taxes": None,
        "net_amount": None,
        "currency": "EUR",
        "commitment_amount": None,
        "master_data": None,
        "set_inactive": False,
        "note": None,
        "source": None,
        "cancel_reason": None,
        "case_id": None,
        "proposed_by": None,
        "proposed_at": None,
        "approved_by": None,
        "approved_at": None,
        "booked_by": None,
        "booked_at": None,
        "cancelled_at": None,
        "created_by": fixture.actor.id,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    values.update(overrides)
    return TradeTicketDTO(**values)


async def test_ts08_sparse_preview_is_quiet(app_engine: AsyncEngine, seed_tenant) -> None:
    """M-1 runs on every keystroke, so an unfinished ticket must not complain."""
    tenant = await seed_tenant("TS-08a")
    fixture = await _seed(app_engine, tenant, email="pm@ts08a.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        preview = await _service(session).preview(
            _transient(fixture, tenant), now=_NOW, today=_TODAY
        )

    assert preview.blocks == ()
    assert preview.warnings == TicketWarnings()
    assert preview.cash_effect is None


async def test_ts08_preview_writes_nothing(app_engine: AsyncEngine, seed_tenant) -> None:
    """The dry run is dry: no row appears, and the previewed draft does not move."""
    tenant = await seed_tenant("TS-08b")
    fixture = await _seed(
        app_engine,
        tenant,
        email="pm@ts08b.example",
        cash_balance=Decimal("1000"),
    )

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        tickets = TradeTicketRepository(session)
        draft = await service.create_draft(
            **_order_draft_kwargs(
                fixture,
                units=Decimal("200"),
                price_per_unit=Decimal("10.00"),
            )
        )

        before = await tickets.list_by_status(sorted(STATUSES))
        transient = await service.preview(
            _transient(
                fixture,
                tenant,
                cash_investment_id=fixture.cash.id,
                units=Decimal("200"),
                price_per_unit=Decimal("10.00"),
            ),
            now=_NOW,
            today=_TODAY,
        )
        persisted = await service.preview(draft, now=_NOW, today=_TODAY)
        after = await tickets.list_by_status(sorted(STATUSES))

    # Both previews did real work, so "wrote nothing" is not vacuous.
    assert transient.warnings.identifiers == (WARNING_NEGATIVE_CASH,)
    assert persisted.warnings.identifiers == (WARNING_NEGATIVE_CASH,)
    assert [len(before), len(after)] == [1, 1]

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        reloaded = await TradeTicketRepository(session).get(draft.id)
    assert reloaded is not None
    assert reloaded.status == "draft"
    assert reloaded.updated_at == draft.updated_at


async def test_ts08_oversell_previews_as_a_block(app_engine: AsyncEngine, seed_tenant) -> None:
    """The same offence, reported as a value here and raised on propose."""
    tenant = await seed_tenant("TS-08c")
    fixture = await _seed(
        app_engine,
        tenant,
        email="pm@ts08c.example",
        instrument_units=Decimal("100"),
        cash_balance=Decimal("100000"),
    )

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        preview = await service.preview(
            _transient(
                fixture,
                tenant,
                direction="sell",
                investment_id=fixture.instrument.id,
                cash_investment_id=fixture.cash.id,
                units=Decimal("200"),
                price_per_unit=Decimal("10.00"),
            ),
            now=_NOW,
            today=_TODAY,
        )

        twin = await service.create_draft(
            **_order_draft_kwargs(fixture, direction="sell", units=Decimal("200"))
        )
        with pytest.raises(NonNegativeHoldingsError) as excinfo:
            await service.propose(twin.id, proposed_by=fixture.actor.id, now=_NOW, today=_TODAY)

    assert preview.blocks == (
        TicketBlock(
            identifier=BLOCK_OVERSELL,
            data={
                "units": Decimal("200"),
                "trade_date": _TRADE_DATE,
                "offending_date": _TRADE_DATE,
            },
        ),
    )
    # A block does not suppress the warning pass — both are collected.
    assert preview.warnings == TicketWarnings()

    # The refactor left the raising path byte-identical: same message, same field.
    assert excinfo.value.message == (
        f"Selling {twin.units} units on {_TRADE_DATE} would drive holdings "
        f"below zero on {_TRADE_DATE} for investment {fixture.instrument.id}; "
        "short positions are out of scope (ADR-0097 §4)."
    )
    assert excinfo.value.field == "units"
    await _assert_still_draft(app_engine, tenant, fixture.actor.id, twin.id)


async def test_ts08_preview_warnings_equal_propose_warnings(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The one-arithmetic guarantee: the composer is shown what propose would say."""
    tenant = await seed_tenant("TS-08d")
    fixture = await _seed(
        app_engine,
        tenant,
        email="pm@ts08d.example",
        cash_balance=Decimal("1000"),
        prices={_TRADE_DATE: Decimal("10.00")},
    )
    earlier = date(2026, 8, 1)

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(
            **_order_draft_kwargs(
                fixture,
                units=Decimal("200"),
                price_per_unit=Decimal("11.00"),
            )
        )
        preview = await service.preview(draft, now=_NOW, today=earlier)
        _, proposed = await service.propose(
            draft.id, proposed_by=fixture.actor.id, now=_NOW, today=earlier
        )

    assert preview.warnings == proposed
    assert set(preview.warnings.identifiers) == {
        WARNING_NEGATIVE_CASH,
        WARNING_PRICE_DEVIATION,
        WARNING_FUTURE_TRADE_DATE,
    }
    # 200 × 11.00, the number the Amounts block and the settlement radio share.
    assert preview.cash_effect == Decimal("2200")


async def test_ts08_preview_without_a_ledger_repository_is_loud(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A missing dependency must refuse, never preview clean (the one fatal bug)."""
    tenant = await seed_tenant("TS-08e")
    fixture = await _seed(
        app_engine,
        tenant,
        email="pm@ts08e.example",
        cash_balance=Decimal("1000"),
    )

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = TicketService(tickets=TradeTicketRepository(session))
        with pytest.raises(RuntimeError) as excinfo:
            await service.preview(
                _transient(
                    fixture,
                    tenant,
                    investment_id=fixture.instrument.id,
                    cash_investment_id=fixture.cash.id,
                    units=Decimal("200"),
                    price_per_unit=Decimal("10.00"),
                ),
                now=_NOW,
                today=_TODAY,
            )

    assert "position-transaction" in str(excinfo.value)
