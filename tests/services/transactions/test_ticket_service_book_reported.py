# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Booking the reported kinds against the live compose Postgres (S2b).

Where ``test_ticket_service_book.py`` pins the two-leg settlement of an
order, this file is about the three flows that write more than a ledger:
R-SEC-SELL (proceeds, NAV, inactivation, cash), R-COMMIT (an investment and
nothing else) and R-SEC-BUY (an investment, its opening NAV, cash) — plus
U-NEW, which creates an instrument and then books exactly as a U-BUY.

Two properties carry most of the weight:

* **The investment row is an emission effect** (MD-12, D-I). Nothing exists
  before ``book``, and what booking created is legible from the effect table
  alone: ``investment_update`` with ``prior_state`` NULL means "this booking
  made this row". TR-04 / TR-06 / TR-10 assert the encoding, and TR-07
  asserts the other half of it — that a failure anywhere leaves no shell,
  no identifiers and no link behind.
* **Consequences are not options** (MD-17, D-S). A secondary sale writes the
  stake down to zero and retires the position whatever ``set_inactive``
  says, because a secondary sale is always a full disposal (MD-18). TR-02
  and TR-11 pin both directions of that.

Effects are asserted **by** ``effect_type``, never by position: the order is
a readability choice inside one transaction, and a test that froze it would
refuse a future reordering that changed nothing.

Coverage
--------
* TR-01: the R-SEC-SELL happy path — four effects and four kinds of row.
* TR-02: ``set_inactive=False`` still deactivates (D-S).
* TR-03: the NAV collision at ``trade_date`` refuses, and rolls the
  already-written cashflow back with it (D-N).
* TR-04: R-COMMIT books one investment and moves nothing (MD-19).
* TR-05: ticket and payload disagreeing about the commitment (D-U).
* TR-06: R-SEC-BUY — the created row, its acquired-NAV opening, cash out,
  the identifier, and the overdraft warning.
* TR-07: creating-flow atomicity — a broken cash leg leaves no shell.
* TR-08: the creating gates at Propose (D-J, MD-21, D-O).
* TR-09: the target checks — inactive (D-P), and both valuation modes (D-Q).
* TR-10: U-NEW creates a unitised instrument, then books as a U-BUY (D-M).
* TR-11: MD-7's refusal never fires on a secondary sale.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import TicketIncomplete, ValuationModeError
from core.repositories import (
    AssetClassRepository,
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
from services.investments.investment_service import InvestmentService
from services.investments.nav_materialisation import NavMaterialisationService
from services.transactions import TicketService
from services.transactions.constants import (
    BLOCK_DUPLICATE_INVESTMENT_NAME,
    BLOCK_INVESTMENT_INACTIVE,
    BLOCK_MISSING_ANLV,
    BLOCK_NAV_EXISTS_AT_TRADE_DATE,
    INCOMPLETE_COMMITMENT_SHAPE,
    INCOMPLETE_MISSING_MASTER_DATA,
    MD_ACQUIRED_NAV,
    MD_ANLV_CODE,
    MD_ASSET_CLASS_ID,
    MD_ASSUMED_UNFUNDED,
    MD_COMMITMENT_AMOUNT,
    MD_CURRENCY,
    MD_FIGI,
    MD_IDENTIFIER_SCHEME,
    MD_IDENTIFIER_VALUE,
    MD_INVESTMENT_TYPE,
    MD_NAME,
    MD_VINTAGE_YEAR,
    WARNING_NEGATIVE_CASH,
)

_NOW = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)
_TRADE_DATE = date(2026, 8, 31)
_TODAY = date(2026, 8, 31)
_OPENING_DATE = date(2026, 1, 2)
_PRIOR_NAV_DATE = date(2026, 6, 30)
_NOON = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

_NEW_NAME = "Continuation Fund V"


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


class _Fixture:
    """The seeded world one reported-kind booking test works against."""

    def __init__(self, actor, asset_class, stake, instrument, cash) -> None:
        self.actor = actor
        self.asset_class = asset_class
        #: A statement-valued private-markets position (``reported``): what
        #: R-SEC-SELL disposes of.
        self.stake = stake
        #: A unit-dealt listed position (``unitised``): the U-* target, and
        #: the wrong-mode case for a secondary.
        self.instrument = instrument
        self.cash = cash


async def _seed(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    *,
    email: str,
    cash_balance: Decimal = Decimal("1000000"),
    stake_active: bool = True,
    instrument_active: bool = True,
    prior_nav_date: date | None = _PRIOR_NAV_DATE,
) -> _Fixture:
    """Seed a user, a reported stake, a unitised instrument and a cash position.

    Both valuation modes are present in every test's world because D-Q is
    about the *pairing* of a kind with a mode: a fixture carrying only one
    mode could not tell a rule that checks from a rule that does not.
    """
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        asset_class = await AssetClassRepository(session).create(
            code="pe_class", display_name="Private Equity"
        )
        investments = InvestmentRepository(session)
        stake = await investments.create(
            name="Legacy Buyout Fund III",
            investment_type="private_equity",
            asset_class_id=asset_class.id,
            currency="EUR",
            created_by=actor.id,
            anlv_code="anlv_13",
            valuation_mode="reported",
            is_active=stake_active,
            commitment_amount=Decimal("10000000"),
        )
        instrument = await investments.create(
            name="Listed Fund",
            investment_type="listed_equity",
            asset_class_id=asset_class.id,
            currency="EUR",
            created_by=actor.id,
            anlv_code="anlv_13",
            valuation_mode="unitised",
            is_active=instrument_active,
        )
        cash = await investments.create(
            name="Cash EUR",
            investment_type="cash",
            asset_class_id=asset_class.id,
            currency="EUR",
            created_by=actor.id,
            valuation_mode="unitised",
        )

        if prior_nav_date is not None:
            await InvestmentNavRepository(session).upsert(
                investment_id=stake.id,
                as_of_date=prior_nav_date,
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
            units=Decimal("100"),
            currency="EUR",
            ingest_origin="excel",
            created_by=actor.id,
        )
        await ledger.add(
            investment_id=cash.id,
            txn_type="opening",
            trade_date=_OPENING_DATE,
            units=cash_balance,
            currency="EUR",
            ingest_origin="excel",
            created_by=actor.id,
        )

    return _Fixture(actor, asset_class, stake, instrument, cash)


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
    """A fully wired service — every optional dependency present."""
    return TicketService(
        tickets=TradeTicketRepository(session),
        investments=InvestmentRepository(session),
        position_transactions=PositionTransactionRepository(session),
        instrument_prices=InstrumentPriceRepository(session),
        investment_service=_investment_service(session),
        navs=InvestmentNavRepository(session),
    )


def _master_data(fixture: _Fixture, **overrides) -> dict[str, object]:
    """A complete creating payload as JSONB carries it; ``None`` drops a key."""
    values: dict[str, object] = {
        MD_NAME: _NEW_NAME,
        MD_INVESTMENT_TYPE: "private_equity",
        MD_ASSET_CLASS_ID: str(fixture.asset_class.id),
        MD_CURRENCY: "EUR",
        MD_ANLV_CODE: "anlv_13",
        MD_VINTAGE_YEAR: "2024",
    }
    values.update(overrides)
    return {key: value for key, value in values.items() if value is not None}


def _sell_draft_kwargs(fixture: _Fixture, **overrides):
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


# ---------------------------------------------------------------------------
# TR-01: the R-SEC-SELL happy path
# ---------------------------------------------------------------------------


async def test_tr01_secondary_sell_books_four_effects(app_engine: AsyncEngine, seed_tenant) -> None:
    """Proceeds in, stake to zero, position retired, cash landed (MD-17)."""
    tenant = await seed_tenant("TR-01")
    fixture = await _seed(app_engine, tenant, email="pm@tr01.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(**_sell_draft_kwargs(fixture))
        booked, warnings = await service.book(
            draft.id, booked_by=fixture.actor.id, now=_NOW, today=_TODAY
        )
        effects = await TradeTicketRepository(session).list_effects(draft.id)

    assert booked.status == "booked"
    assert booked.booked_by == fixture.actor.id
    assert warnings.identifiers == ()

    provenance = f"ticket #{booked.ticket_number}"

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        flows = await InvestmentCashflowRepository(session).list_by_investment(fixture.stake.id)
        navs = await InvestmentNavRepository(session).list_by_investment_and_kind(
            fixture.stake.id, "actual"
        )
        stake = await InvestmentRepository(session).get_by_id(fixture.stake.id)
        cash_rows = await PositionTransactionRepository(session).list_for_investment(
            fixture.cash.id
        )

    # -- the proceeds: an ordinary realised distribution (ADR-0128 Q-3) -----
    assert len(flows) == 1
    flow = flows[0]
    assert (flow.flow_type, flow.flow_kind) == ("distribution", "actual")
    assert flow.amount == Decimal("2250000.0000")
    assert flow.currency == "EUR"
    assert flow.flow_timestamp == _NOON
    assert flow.description == provenance
    assert flow.ingest_origin == "manual"

    # -- the stake is written down to nothing (MD-17) ----------------------
    closing = next(row for row in navs if row.as_of_date == _TRADE_DATE)
    assert closing.nav_value == Decimal("0.0000")
    assert closing.source == provenance
    assert closing.ingest_origin == "manual"
    # The prior statement NAV is untouched: this is a new row, not an edit.
    assert {row.as_of_date for row in navs} == {_PRIOR_NAV_DATE, _TRADE_DATE}

    # -- and the position is retired ---------------------------------------
    assert stake is not None and stake.is_active is False

    # -- cash arrives at 1.0000 --------------------------------------------
    cash_in = next(row for row in cash_rows if row.txn_type == "buy")
    assert cash_in.units == Decimal("2250000.00000000")
    assert cash_in.price_per_unit == Decimal("1.0000")
    assert cash_in.consideration is None
    assert cash_in.source == provenance

    # -- four effects, asserted by type ------------------------------------
    grouped = _by_type(effects)
    assert sorted(grouped) == ["cashflow", "investment_update", "nav", "position_txn"]
    assert grouped["cashflow"][0].effect_id == flow.id
    assert grouped["nav"][0].effect_id == closing.id
    update = grouped["investment_update"][0]
    assert update.effect_id == fixture.stake.id
    # D-H: the whole row as it stood, so a reversal restores rather than guesses.
    assert update.prior_state is not None
    assert update.prior_state["is_active"] is True
    assert update.prior_state["name"] == "Legacy Buyout Fund III"


# ---------------------------------------------------------------------------
# TR-02 / TR-11: set_inactive is not consulted on a secondary sale (D-S)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("set_inactive", [False, True], ids=["unchecked", "checked"])
async def test_tr02_secondary_sell_deactivates_whatever_set_inactive_says(
    app_engine: AsyncEngine, seed_tenant, set_inactive: bool
) -> None:
    """MD-17 supersedes MD-7 here: inactivation is the flow, not an option.

    The kickoff's "``is_active=false`` iff ``set_inactive``" reading would
    make a checkbox the difference between a closed position and one that
    holds a zero NAV for ever. The decision record does not offer the choice.
    """
    tenant = await seed_tenant(f"TR-02-{set_inactive}")
    fixture = await _seed(app_engine, tenant, email=f"pm@tr02{int(set_inactive)}.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(**_sell_draft_kwargs(fixture, set_inactive=set_inactive))
        await service.book(draft.id, booked_by=fixture.actor.id, now=_NOW, today=_TODAY)

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        stake = await InvestmentRepository(session).get_by_id(fixture.stake.id)

    assert stake is not None and stake.is_active is False


async def test_tr11_secondary_sell_never_raises_the_md7_refusal(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The U-SELL rule does not apply (D-S).

    ``set_inactive_not_full_disposal`` guards a *partial* U-SELL. A secondary
    sale cannot be partial (MD-18), so the identifier must not be reachable
    from this flow at all — even with the checkbox set on a stake that holds
    no units to dispose of.
    """
    tenant = await seed_tenant("TR-11")
    fixture = await _seed(app_engine, tenant, email="pm@tr11.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(**_sell_draft_kwargs(fixture, set_inactive=True))
        booked, _ = await service.book(draft.id, booked_by=fixture.actor.id, now=_NOW, today=_TODAY)
        effects = await TradeTicketRepository(session).list_effects(draft.id)

    # Reaching 'booked' is the assertion: the MD-7 refusal would have raised
    # ``set_inactive_not_full_disposal`` before any of this existed.
    assert booked.status == "booked"
    assert sorted(_by_type(effects)) == [
        "cashflow",
        "investment_update",
        "nav",
        "position_txn",
    ]


# ---------------------------------------------------------------------------
# TR-03: the NAV collision (D-N)
# ---------------------------------------------------------------------------


async def test_tr03_existing_actual_nav_at_trade_date_refuses_and_rolls_back(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """An overwritten NAV could not be restored, so the booking refuses.

    The cashflow is written *before* the NAV, so this also pins the
    atomicity: the refusal fires mid-emission and the row that had already
    landed goes with it.
    """
    tenant = await seed_tenant("TR-03")
    fixture = await _seed(app_engine, tenant, email="pm@tr03.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        await InvestmentNavRepository(session).upsert(
            investment_id=fixture.stake.id,
            as_of_date=_TRADE_DATE,
            nav_kind="actual",
            nav_value=Decimal("2400000"),
            currency="EUR",
            source="excel-import",
            created_by=fixture.actor.id,
            ingest_origin="excel",
        )
        draft = await _service(session).create_draft(**_sell_draft_kwargs(fixture))

    with pytest.raises(TicketIncomplete) as excinfo:
        async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
            await _service(session).book(
                draft.id, booked_by=fixture.actor.id, now=_NOW, today=_TODAY
            )

    assert excinfo.value.identifier == BLOCK_NAV_EXISTS_AT_TRADE_DATE
    assert excinfo.value.field == "trade_date"

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        flows = await InvestmentCashflowRepository(session).list_by_investment(fixture.stake.id)
        navs = await InvestmentNavRepository(session).list_by_investment_and_kind(
            fixture.stake.id, "actual"
        )
        stake = await InvestmentRepository(session).get_by_id(fixture.stake.id)
        ticket = await TradeTicketRepository(session).get(draft.id)

    assert flows == []  # the distribution written before the check is gone
    assert next(row for row in navs if row.as_of_date == _TRADE_DATE).nav_value == Decimal(
        "2400000.0000"
    )
    assert stake is not None and stake.is_active is True
    assert ticket is not None and ticket.status == "draft"


# ---------------------------------------------------------------------------
# TR-04 / TR-05: R-COMMIT
# ---------------------------------------------------------------------------


async def test_tr04_commitment_books_one_investment_and_moves_nothing(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """MD-19: no cash, no NAV — a commitment is a promise, not a transfer."""
    tenant = await seed_tenant("TR-04")
    fixture = await _seed(app_engine, tenant, email="pm@tr04.example")

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
        booked, warnings = await service.book(
            draft.id, booked_by=fixture.actor.id, now=_NOW, today=_TODAY
        )
        effects = await TradeTicketRepository(session).list_effects(draft.id)

    assert booked.status == "booked"
    assert warnings.identifiers == ()

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        created = await InvestmentRepository(session).get_by_name(_NEW_NAME)
        assert created is not None
        navs = await InvestmentNavRepository(session).list_by_investment(created.id)
        flows = await InvestmentCashflowRepository(session).list_by_investment(created.id)
        ledger = PositionTransactionRepository(session)
        created_rows = await ledger.list_for_investment(created.id)
        cash_rows = await ledger.list_for_investment(fixture.cash.id)

    assert created.commitment_amount == Decimal("5000000.0000")
    assert created.valuation_mode == "reported"
    assert created.anlv_code == "anlv_13"
    assert created.vintage_year == 2024
    assert created.currency == "EUR"
    assert created.is_active is True

    # D-T: the ticket now names the investment its booking created.
    assert booked.investment_id == created.id

    # D-I: one effect, and the NULL is what says "created".
    assert len(effects) == 1
    assert effects[0].effect_type == "investment_update"
    assert effects[0].effect_id == created.id
    assert effects[0].prior_state is None

    assert (navs, flows, created_rows) == ([], [], [])
    assert [row.txn_type for row in cash_rows] == ["opening"]


async def test_tr05_commitment_and_payload_must_agree(app_engine: AsyncEngine, seed_tenant) -> None:
    """D-U: a commitment is a denominator, so two numbers is one too many."""
    tenant = await seed_tenant("TR-05")
    fixture = await _seed(app_engine, tenant, email="pm@tr05.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        draft = await _service(session).create_draft(
            kind="commitment",
            direction="buy",
            currency="EUR",
            trade_date=_TRADE_DATE,
            created_by=fixture.actor.id,
            now=_NOW,
            commitment_amount=Decimal("5000000"),
            master_data=_master_data(fixture, **{MD_COMMITMENT_AMOUNT: "4000000"}),
        )

    with pytest.raises(TicketIncomplete) as excinfo:
        async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
            await _service(session).book(
                draft.id, booked_by=fixture.actor.id, now=_NOW, today=_TODAY
            )

    assert excinfo.value.identifier == INCOMPLETE_COMMITMENT_SHAPE
    assert excinfo.value.field == "commitment_amount"

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        assert await InvestmentRepository(session).get_by_name(_NEW_NAME) is None
        ticket = await TradeTicketRepository(session).get(draft.id)
    assert ticket is not None and ticket.status == "draft"


# ---------------------------------------------------------------------------
# TR-06: R-SEC-BUY
# ---------------------------------------------------------------------------


def _secondary_buy_kwargs(fixture: _Fixture, **overrides):
    """A complete, bookable R-SEC-BUY draft."""
    values = {
        "kind": "secondary",
        "direction": "buy",
        "currency": "EUR",
        "trade_date": _TRADE_DATE,
        "created_by": fixture.actor.id,
        "now": _NOW,
        "cash_investment_id": fixture.cash.id,
        "gross_amount": Decimal("750000"),
        "fees": Decimal("5000"),
        "master_data": _master_data(
            fixture,
            **{
                MD_ACQUIRED_NAV: "800000",
                MD_ASSUMED_UNFUNDED: "250000",
                MD_IDENTIFIER_SCHEME: "preqin",
                MD_IDENTIFIER_VALUE: "PQ-991",
            },
        ),
    }
    values.update(overrides)
    return values


async def test_tr06_secondary_buy_books_row_nav_and_cash(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The stake opens at what it is worth, not at what was paid (MD-20)."""
    tenant = await seed_tenant("TR-06")
    # Seeded thin on purpose: the purchase overdraws, which warns and books.
    fixture = await _seed(app_engine, tenant, email="pm@tr06.example", cash_balance=Decimal("100"))

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(**_secondary_buy_kwargs(fixture))
        booked, warnings = await service.book(
            draft.id, booked_by=fixture.actor.id, now=_NOW, today=_TODAY
        )
        effects = await TradeTicketRepository(session).list_effects(draft.id)

    assert booked.status == "booked"
    # ADR-0130: an overdraft is an economic fact; it warns and books.
    assert WARNING_NEGATIVE_CASH in warnings.identifiers

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        created = await InvestmentRepository(session).get_by_name(_NEW_NAME)
        assert created is not None
        navs = await InvestmentNavRepository(session).list_by_investment_and_kind(
            created.id, "actual"
        )
        identifiers = await InvestmentIdentifierRepository(session).list_for_investment(created.id)
        cash_rows = await PositionTransactionRepository(session).list_for_investment(
            fixture.cash.id
        )
        created_rows = await PositionTransactionRepository(session).list_for_investment(created.id)

    assert created.valuation_mode == "reported"
    # D-U: the unfunded part assumed with the stake is the row's commitment.
    assert created.commitment_amount == Decimal("250000.0000")
    assert booked.investment_id == created.id

    # D-L: the identifier rides with the row and is promoted.
    assert [(row.scheme, row.value, row.is_primary) for row in identifiers] == [
        ("preqin", "PQ-991", True)
    ]

    # The opening NAV is the acquired NAV, not the purchase price.
    assert len(navs) == 1
    assert navs[0].as_of_date == _TRADE_DATE
    assert navs[0].nav_value == Decimal("800000.0000")
    assert navs[0].source == f"ticket #{booked.ticket_number}"

    # Cash out: gross + fees, on the cash position only.
    cash_out = next(row for row in cash_rows if row.txn_type == "sell")
    assert cash_out.units == Decimal("-755000.00000000")
    assert cash_out.price_per_unit == Decimal("1.0000")
    # A reported investment holds no units; nothing is written to its ledger.
    assert created_rows == []

    grouped = _by_type(effects)
    assert sorted(grouped) == ["investment_update", "nav", "position_txn"]
    assert grouped["investment_update"][0].prior_state is None
    assert grouped["position_txn"][0].effect_id == cash_out.id


# ---------------------------------------------------------------------------
# TR-07: creating-flow atomicity
# ---------------------------------------------------------------------------


async def test_tr07_a_failed_cash_leg_leaves_no_shell(
    app_engine: AsyncEngine, seed_tenant, monkeypatch
) -> None:
    """The investment is an effect, so a failed booking un-creates it.

    This is the property MD-12 buys: because nothing exists before ``book``,
    a failure needs no clean-up pass — the transaction *is* the clean-up.
    An investment created at Propose would survive this and become a shell
    nobody asked for.
    """
    tenant = await seed_tenant("TR-07")
    fixture = await _seed(app_engine, tenant, email="pm@tr07.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        draft = await _service(session).create_draft(**_secondary_buy_kwargs(fixture))

    async def boom(self, **kwargs):
        raise RuntimeError("forced")

    monkeypatch.setattr(InvestmentService, "add_position_transaction", boom)

    with pytest.raises(RuntimeError, match="forced"):
        async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
            await _service(session).book(
                draft.id, booked_by=fixture.actor.id, now=_NOW, today=_TODAY
            )

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        assert await InvestmentRepository(session).get_by_name(_NEW_NAME) is None
        ticket = await TradeTicketRepository(session).get(draft.id)
        effects = await TradeTicketRepository(session).list_effects(draft.id)
        cash_rows = await PositionTransactionRepository(session).list_for_investment(
            fixture.cash.id
        )

    assert ticket is not None
    assert ticket.investment_id is None  # the D-T link went with the row
    assert ticket.status == "draft"
    assert effects == []
    assert [row.txn_type for row in cash_rows] == ["opening"]


# ---------------------------------------------------------------------------
# TR-08: the creating gates, at Propose
# ---------------------------------------------------------------------------


async def test_tr08_creating_payload_gates_fire_at_propose(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """MD-11 / MD-21 put these at Propose *and* Book; here is the first station."""
    tenant = await seed_tenant("TR-08")
    fixture = await _seed(app_engine, tenant, email="pm@tr08.example")

    def _commitment_draft(**payload_overrides):
        return {
            "kind": "commitment",
            "direction": "buy",
            "currency": "EUR",
            "trade_date": _TRADE_DATE,
            "created_by": fixture.actor.id,
            "now": _NOW,
            "commitment_amount": Decimal("5000000"),
            "master_data": _master_data(fixture, **payload_overrides),
        }

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)

        # D-J: the payload is the row, so the row's NOT NULL columns are due.
        no_type = await service.create_draft(**_commitment_draft(**{MD_INVESTMENT_TYPE: None}))
        with pytest.raises(TicketIncomplete) as missing_type:
            await service.propose(no_type.id, proposed_by=fixture.actor.id, now=_NOW, today=_TODAY)
        assert missing_type.value.identifier == INCOMPLETE_MISSING_MASTER_DATA
        assert MD_INVESTMENT_TYPE in str(missing_type.value)

        no_class = await service.create_draft(**_commitment_draft(**{MD_ASSET_CLASS_ID: None}))
        with pytest.raises(TicketIncomplete) as missing_class:
            await service.propose(no_class.id, proposed_by=fixture.actor.id, now=_NOW, today=_TODAY)
        assert missing_class.value.identifier == INCOMPLETE_MISSING_MASTER_DATA

        # MD-21: the AnlV gate, on the creating flows only.
        no_anlv = await service.create_draft(**_commitment_draft(**{MD_ANLV_CODE: None}))
        with pytest.raises(TicketIncomplete) as missing_anlv:
            await service.propose(no_anlv.id, proposed_by=fixture.actor.id, now=_NOW, today=_TODAY)
        assert missing_anlv.value.identifier == BLOCK_MISSING_ANLV

        # D-O: the name is the natural key; a second row cannot have it.
        taken = await service.create_draft(
            **_commitment_draft(**{MD_NAME: "Legacy Buyout Fund III"})
        )
        with pytest.raises(TicketIncomplete) as duplicate:
            await service.propose(taken.id, proposed_by=fixture.actor.id, now=_NOW, today=_TODAY)
        assert duplicate.value.identifier == BLOCK_DUPLICATE_INVESTMENT_NAME
        assert duplicate.value.field == "master_data"


# ---------------------------------------------------------------------------
# TR-09: the target checks (D-P, D-Q)
# ---------------------------------------------------------------------------


async def test_tr09_an_inactive_target_refuses(app_engine: AsyncEngine, seed_tenant) -> None:
    """D-P: writing to a retired position would revive it."""
    tenant = await seed_tenant("TR-09a")
    fixture = await _seed(app_engine, tenant, email="pm@tr09a.example", instrument_active=False)

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(
            kind="order",
            direction="sell",
            currency="EUR",
            trade_date=_TRADE_DATE,
            created_by=fixture.actor.id,
            now=_NOW,
            investment_id=fixture.instrument.id,
            cash_investment_id=fixture.cash.id,
            units=Decimal("10"),
            price_per_unit=Decimal("10.00"),
        )
        with pytest.raises(TicketIncomplete) as excinfo:
            await service.propose(draft.id, proposed_by=fixture.actor.id, now=_NOW, today=_TODAY)

    assert excinfo.value.identifier == BLOCK_INVESTMENT_INACTIVE
    assert excinfo.value.field == "investment_id"


async def test_tr09_an_order_against_a_reported_position_refuses(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """D-Q: unit arithmetic on a statement-valued position has no meaning (F-5)."""
    tenant = await seed_tenant("TR-09b")
    fixture = await _seed(app_engine, tenant, email="pm@tr09b.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(
            kind="order",
            direction="buy",
            currency="EUR",
            trade_date=_TRADE_DATE,
            created_by=fixture.actor.id,
            now=_NOW,
            investment_id=fixture.stake.id,
            cash_investment_id=fixture.cash.id,
            units=Decimal("10"),
            price_per_unit=Decimal("10.00"),
        )
        with pytest.raises(ValuationModeError) as excinfo:
            await service.propose(draft.id, proposed_by=fixture.actor.id, now=_NOW, today=_TODAY)

    assert "unitised" in str(excinfo.value)


async def test_tr09_a_secondary_sale_of_a_unitised_position_refuses(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The mirror of the above: a unit-dealt holding is sold as units."""
    tenant = await seed_tenant("TR-09c")
    fixture = await _seed(app_engine, tenant, email="pm@tr09c.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        draft = await service.create_draft(
            **_sell_draft_kwargs(fixture, investment_id=fixture.instrument.id)
        )
        with pytest.raises(ValuationModeError) as excinfo:
            await service.propose(draft.id, proposed_by=fixture.actor.id, now=_NOW, today=_TODAY)

    assert "reported" in str(excinfo.value)


# ---------------------------------------------------------------------------
# TR-10: U-NEW (D-M)
# ---------------------------------------------------------------------------


async def test_tr10_new_order_creates_a_unitised_instrument_then_books_as_a_buy(
    app_engine: AsyncEngine, seed_tenant, monkeypatch
) -> None:
    """A U-NEW is a U-BUY whose instrument did not exist yet.

    The delegation is the point: the wizard and the ordinary purchase must
    write identical ledger rows, so U-NEW creates and then calls the same
    emission rather than restating it. The materialisation spy is the
    evidence the delegation went through the sanctioned write seam — it
    fires once per leg, exactly as it does for a plain U-BUY (TB-09).
    """
    tenant = await seed_tenant("TR-10")
    fixture = await _seed(app_engine, tenant, email="pm@tr10.example")

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        draft = await _service(session).create_draft(
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
                **{
                    MD_NAME: "New Listed Fund",
                    MD_INVESTMENT_TYPE: "listed_equity",
                    MD_IDENTIFIER_SCHEME: "isin",
                    MD_IDENTIFIER_VALUE: "DE0001234567",
                    MD_FIGI: "BBG000BLNNH6",
                },
            ),
        )

    seen: list[tuple[UUID, object]] = []
    original = NavMaterialisationService.materialise

    async def spy(self, investment_id, *, acting_user, since=None):
        seen.append((investment_id, since))
        return await original(self, investment_id, acting_user=acting_user, since=since)

    monkeypatch.setattr(NavMaterialisationService, "materialise", spy)

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        service = _service(session)
        booked, _ = await service.book(draft.id, booked_by=fixture.actor.id, now=_NOW, today=_TODAY)
        effects = await TradeTicketRepository(session).list_effects(draft.id)

    async with tenant_context(app_engine, tenant, user_id=fixture.actor.id) as session:
        created = await InvestmentRepository(session).get_by_name("New Listed Fund")
        assert created is not None
        identifiers = await InvestmentIdentifierRepository(session).list_for_investment(created.id)
        ledger = PositionTransactionRepository(session)
        instrument_rows = await ledger.list_for_investment(created.id)
        cash_rows = await ledger.list_for_investment(fixture.cash.id)

    assert created.valuation_mode == "unitised"
    assert created.commitment_amount is None
    assert booked.investment_id == created.id

    # D-L: the chosen identity is primary; the resolved FIGI rides alongside.
    assert {(row.scheme, row.value, row.is_primary) for row in identifiers} == {
        ("isin", "DE0001234567", True),
        ("figi", "BBG000BLNNH6", False),
    }

    buy = next(row for row in instrument_rows if row.txn_type == "buy")
    assert buy.units == Decimal("10.00000000")
    assert buy.price_per_unit == Decimal("10.0000")
    cash_out = next(row for row in cash_rows if row.txn_type == "sell")
    assert cash_out.units == Decimal("-100.00000000")

    grouped = _by_type(effects)
    assert sorted(grouped) == ["investment_update", "position_txn"]
    assert grouped["investment_update"][0].prior_state is None
    assert len(grouped["position_txn"]) == 2

    # Both legs are unitised, so both materialise — once each, bounded.
    assert [investment_id for investment_id, _ in seen] == [created.id, fixture.cash.id]
    assert {since for _, since in seen} == {_TRADE_DATE}
