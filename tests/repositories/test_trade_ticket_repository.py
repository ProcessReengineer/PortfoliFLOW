# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""TradeTicketRepository tests against the live compose Postgres.

The ``trade_tickets`` / ``trade_ticket_effects`` tables are tenant-scoped
(RLS-policed, per ADR-0128 / ADR-0035). The repository is deliberately
*mechanism, not policy* — it moves a ticket between the stations of
ADR-0128 §3 and records what a booking emitted, without deciding which
transitions are legal. These tests pin the mechanism and the guards that
protect the store, not the workflow the service seam will own.

Coverage
--------
* TT-01: tenant-sequential numbering, per-tenant independence, and the
  single-retry recovery on a ``uq_trade_tickets_tenant_ticket_number``
  collision.
* TT-02: every column round-trips, ``master_data`` JSONB and
  ``set_inactive`` included; an unknown id reads back ``None``.
* TT-03: ``update_draft`` mutates a draft, distinguishes "no such ticket"
  from "no longer a draft", and refuses a non-whitelisted field.
* TT-04: ``set_status`` writes each station's attribution, and the b034
  CHECK refuses a hand-forged ``booked`` row that has none.
* TT-05: the effect round-trip incl. ``prior_state``, the duplicate-effect
  constraint, and vocabulary validation *before* any write.
* TT-06: cross-tenant invisibility on BOTH tables (RLS smoke).
* TT-07: deleting a ticket cascades its effects; the ``investment_id`` FK
  really does RESTRICT.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import TicketNotFound, TicketStateInvalid
from core.repositories import (
    AssetClassRepository,
    InvestmentRepository,
    UserRepository,
    tenant_context,
)
from core.repositories.trade_ticket_repository import (
    EffectInput,
    TradeTicketRepository,
)

_T0 = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)
_TRADE_DATE = date(2026, 8, 31)


class _FakeUniqueViolation(Exception):
    """Stand-in for asyncpg's ``UniqueViolationError`` in the retry test."""

    constraint_name = "uq_trade_tickets_tenant_ticket_number"


async def _seed_actor_and_investments(
    app_engine: AsyncEngine,
    tenant_id,
    *,
    email: str,
    currency: str = "EUR",
):
    """Create one user, one asset class, one instrument and one cash position.

    The cash position is an ordinary investment row of
    ``investment_type='cash'`` (ADR-0100) — the settlement position a
    unitised ticket points at through ``cash_investment_id``.
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
            currency=currency,
            created_by=actor.id,
        )
        cash = await investments.create(
            name=f"Cash {currency}",
            investment_type="cash",
            asset_class_id=asset_class.id,
            currency=currency,
            created_by=actor.id,
        )
    return actor, instrument, cash


async def _draft(repo: TradeTicketRepository, actor_id, **overrides):
    """Create a minimal ``order`` / ``buy`` draft, overridable per test."""
    values = {
        "kind": "order",
        "direction": "buy",
        "currency": "EUR",
        "trade_date": _TRADE_DATE,
        "created_by": actor_id,
        "now": _T0,
    }
    values.update(overrides)
    return await repo.create_draft(**values)


# ---------------------------------------------------------------------------
# TT-01: numbering, per-tenant independence, and the single-retry recovery
# ---------------------------------------------------------------------------


async def test_tt01_numbering_increments_per_tenant(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant("TT-01a")
    tenant_b = await seed_tenant("TT-01b")
    actor_a, _, _ = await _seed_actor_and_investments(
        app_engine, tenant_a, email="pm@tt01a.example"
    )
    actor_b, _, _ = await _seed_actor_and_investments(
        app_engine, tenant_b, email="pm@tt01b.example"
    )

    async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
        repo = TradeTicketRepository(session)
        first = await _draft(repo, actor_a.id)
        second = await _draft(repo, actor_a.id)
        third = await _draft(repo, actor_a.id)

    # A second tenant numbers from 1 independently — the MAX() runs under RLS.
    async with tenant_context(app_engine, tenant_b, user_id=actor_b.id) as session:
        other = await _draft(TradeTicketRepository(session), actor_b.id)

    assert [first.ticket_number, second.ticket_number, third.ticket_number] == [1, 2, 3]
    assert other.ticket_number == 1
    # Every ticket is born a draft; no other status is creatable.
    assert {first.status, second.status, third.status, other.status} == {"draft"}

    async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
        found = await session.execute(
            text(
                "SELECT 1 FROM pg_constraint "
                "WHERE conname = 'uq_trade_tickets_tenant_ticket_number'"
            )
        )
        assert found.scalar_one_or_none() == 1


async def test_tt01_number_collision_retries_once(
    app_engine: AsyncEngine, seed_tenant, monkeypatch
) -> None:
    tenant_id = await seed_tenant("TT-01c")
    actor, _, _ = await _seed_actor_and_investments(app_engine, tenant_id, email="pm@tt01c.example")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = TradeTicketRepository(session)
        await _draft(repo, actor.id)
        await _draft(repo, actor.id)

        # The first attempt collides on uq_trade_tickets_tenant_ticket_number;
        # the retry re-reads MAX(ticket_number) and lands the next number.
        calls = {"n": 0}
        original = repo._attempt_create_draft

        async def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise IntegrityError("INSERT INTO trade_tickets ...", {}, _FakeUniqueViolation())
            return await original(**kwargs)

        monkeypatch.setattr(repo, "_attempt_create_draft", flaky)
        third = await _draft(repo, actor.id)

    assert calls["n"] == 2  # first raised, retry delegated to the real insert
    assert third.ticket_number == 3


async def test_tt01_savepoint_recovers_from_real_unique_violation(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A genuine number collision inside the SAVEPOINT leaves the session usable.

    The retry only recovers because each attempt runs in ``begin_nested()``:
    a duplicate-number collision rolls back to the savepoint rather than
    poisoning the whole transaction. Pinned with a *real* Postgres unique
    violation, not a hand-built error.
    """
    tenant_id = await seed_tenant("TT-01d")
    actor, _, _ = await _seed_actor_and_investments(app_engine, tenant_id, email="pm@tt01d.example")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = TradeTicketRepository(session)
        first = await _draft(repo, actor.id)

        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "INSERT INTO trade_tickets "
                        "(tenant_id, ticket_number, kind, direction, currency, "
                        " trade_date, created_by) "
                        "VALUES (:tid, :num, 'order', 'buy', 'EUR', :td, :uid)"
                    ),
                    {
                        "tid": str(first.tenant_id),
                        "num": first.ticket_number,
                        "td": _TRADE_DATE,
                        "uid": str(actor.id),
                    },
                )

        second = await _draft(repo, actor.id)
        read_back = await repo.get(second.id)

    assert second.ticket_number == 2
    assert read_back is not None
    assert read_back.ticket_number == 2


# ---------------------------------------------------------------------------
# TT-02: the full column round-trip
# ---------------------------------------------------------------------------


async def test_tt02_every_column_round_trips(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("TT-02")
    actor, instrument, cash = await _seed_actor_and_investments(
        app_engine, tenant_id, email="pm@tt02.example"
    )
    master_data = {
        "name": "Neue Beteiligung",
        "investment_type": "private_equity",
        "identifier": {"scheme": "isin", "value": "DE0001234567", "figi": "BBG000BLNNH6"},
        "vintage_year": 2026,
        "assumed_unfunded": "2500000.00",
    }

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = TradeTicketRepository(session)
        created = await repo.create_draft(
            kind="order",
            direction="buy",
            currency="EUR",
            trade_date=_TRADE_DATE,
            created_by=actor.id,
            investment_id=instrument.id,
            cash_investment_id=cash.id,
            settlement_date=date(2026, 9, 2),
            units=Decimal("125.50000000"),
            price_per_unit=Decimal("98.25000000"),
            gross_amount=Decimal("12330.38"),
            fees=Decimal("12.50"),
            taxes=Decimal("3.75"),
            net_amount=Decimal("12346.63"),
            commitment_amount=Decimal("5000000.00"),
            master_data=master_data,
            set_inactive=True,
            note="Rebalancing tranche 2",
            source="ticket:manual",
            now=_T0,
        )
        read_back = await repo.get(created.id)
        missing = await repo.get(uuid4())

    assert missing is None
    assert read_back is not None
    assert read_back == created  # the write path and the read path agree

    assert read_back.tenant_id == tenant_id
    assert read_back.ticket_number == 1
    assert read_back.kind == "order"
    assert read_back.direction == "buy"
    assert read_back.status == "draft"
    assert read_back.investment_id == instrument.id
    assert read_back.cash_investment_id == cash.id
    assert read_back.trade_date == _TRADE_DATE
    assert read_back.settlement_date == date(2026, 9, 2)
    assert read_back.units == Decimal("125.5")
    assert read_back.price_per_unit == Decimal("98.25")
    assert read_back.gross_amount == Decimal("12330.38")
    assert read_back.fees == Decimal("12.50")
    assert read_back.taxes == Decimal("3.75")
    assert read_back.net_amount == Decimal("12346.63")
    assert read_back.currency == "EUR"
    assert read_back.commitment_amount == Decimal("5000000")
    # The master-data inventory survives verbatim — opaque JSONB (MD-12).
    assert read_back.master_data == master_data
    assert read_back.set_inactive is True
    assert read_back.note == "Rebalancing tranche 2"
    assert read_back.source == "ticket:manual"
    assert read_back.cancel_reason is None
    assert read_back.case_id is None
    # Every station is still empty on a draft.
    assert read_back.proposed_by is None and read_back.proposed_at is None
    assert read_back.approved_by is None and read_back.approved_at is None
    assert read_back.booked_by is None and read_back.booked_at is None
    assert read_back.cancelled_at is None
    assert read_back.created_by == actor.id
    assert read_back.created_at == _T0
    assert read_back.updated_at == _T0


async def test_tt02_defaults_on_a_minimal_draft(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("TT-02b")
    actor, _, _ = await _seed_actor_and_investments(app_engine, tenant_id, email="pm@tt02b.example")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        ticket = await _draft(TradeTicketRepository(session), actor.id)

    assert ticket.master_data is None
    assert ticket.set_inactive is False
    assert ticket.investment_id is None
    assert ticket.cash_investment_id is None


# ---------------------------------------------------------------------------
# TT-03: update_draft — drafts only, and the two failure modes stay distinct
# ---------------------------------------------------------------------------


async def test_tt03_update_draft_mutates_a_draft(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("TT-03")
    actor, instrument, cash = await _seed_actor_and_investments(
        app_engine, tenant_id, email="pm@tt03.example"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = TradeTicketRepository(session)
        ticket = await _draft(repo, actor.id)
        updated = await repo.update_draft(
            ticket.id,
            investment_id=instrument.id,
            cash_investment_id=cash.id,
            units=Decimal("10"),
            price_per_unit=Decimal("42.5"),
            net_amount=Decimal("425"),
            master_data={"note": "still assembling"},
            set_inactive=True,
            direction="sell",
        )
        read_back = await repo.get(ticket.id)

    assert updated.investment_id == instrument.id
    assert updated.cash_investment_id == cash.id
    assert updated.units == Decimal("10")
    assert updated.price_per_unit == Decimal("42.5")
    assert updated.net_amount == Decimal("425")
    assert updated.master_data == {"note": "still assembling"}
    assert updated.set_inactive is True
    assert updated.direction == "sell"
    assert updated.status == "draft"
    assert updated.updated_at > ticket.updated_at
    assert read_back == updated


async def test_tt03_update_draft_unknown_id_raises_not_found(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("TT-03b")
    actor, _, _ = await _seed_actor_and_investments(app_engine, tenant_id, email="pm@tt03b.example")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = TradeTicketRepository(session)
        with pytest.raises(TicketNotFound):
            await repo.update_draft(uuid4(), note="nowhere")


async def test_tt03_update_draft_after_leaving_draft_raises_state_invalid(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("TT-03c")
    actor, _, _ = await _seed_actor_and_investments(app_engine, tenant_id, email="pm@tt03c.example")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = TradeTicketRepository(session)
        ticket = await _draft(repo, actor.id)
        await repo.set_status(ticket.id, status="proposed", actor_user_id=actor.id, now=_T0)

        with pytest.raises(TicketStateInvalid) as excinfo:
            await repo.update_draft(ticket.id, note="too late")

        # Distinct from TicketNotFound: the ticket exists, it is just no
        # longer a form. A silent no-op would conflate the two.
        assert excinfo.value.field == "status"
        assert "proposed" in str(excinfo.value)
        unchanged = await repo.get(ticket.id)

    assert unchanged is not None
    assert unchanged.note is None


async def test_tt03_update_draft_rejects_non_whitelisted_field(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("TT-03d")
    actor, _, _ = await _seed_actor_and_investments(app_engine, tenant_id, email="pm@tt03d.example")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = TradeTicketRepository(session)
        ticket = await _draft(repo, actor.id)

        # A programming error at the call site, not a domain condition.
        for field, value in (
            ("status", "booked"),
            ("ticket_number", 99),
            ("tenant_id", uuid4()),
            ("booked_by", actor.id),
            ("created_at", _T0),
            ("no_such_column", 1),
        ):
            with pytest.raises(ValueError):
                await repo.update_draft(ticket.id, **{field: value})

        untouched = await repo.get(ticket.id)

    assert untouched is not None
    assert untouched.status == "draft"
    assert untouched.ticket_number == 1


# ---------------------------------------------------------------------------
# TT-04: set_status writes each station's attribution
# ---------------------------------------------------------------------------


async def test_tt04_set_status_writes_station_attribution(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("TT-04")
    actor, _, _ = await _seed_actor_and_investments(app_engine, tenant_id, email="pm@tt04.example")
    t_proposed = _T0 + timedelta(minutes=1)
    t_approved = _T0 + timedelta(minutes=2)
    t_booked = _T0 + timedelta(minutes=3)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = TradeTicketRepository(session)
        ticket = await _draft(repo, actor.id)

        proposed = await repo.set_status(
            ticket.id, status="proposed", actor_user_id=actor.id, now=t_proposed
        )
        approved = await repo.set_status(
            ticket.id, status="approved", actor_user_id=actor.id, now=t_approved
        )
        booked = await repo.set_status(
            ticket.id, status="booked", actor_user_id=actor.id, now=t_booked
        )

    assert proposed.status == "proposed"
    assert proposed.proposed_by == actor.id
    assert proposed.proposed_at == t_proposed
    assert proposed.approved_by is None and proposed.booked_by is None

    assert approved.status == "approved"
    assert approved.approved_by == actor.id
    assert approved.approved_at == t_approved
    # v1 permits self-approval (D-4); the earlier station is preserved.
    assert approved.proposed_by == actor.id
    assert approved.proposed_at == t_proposed

    assert booked.status == "booked"
    assert booked.booked_by == actor.id
    assert booked.booked_at == t_booked
    assert booked.updated_at == t_booked


async def test_tt04_cancel_writes_timestamp_and_reason(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("TT-04b")
    actor, _, _ = await _seed_actor_and_investments(app_engine, tenant_id, email="pm@tt04b.example")
    t_cancel = _T0 + timedelta(hours=2)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = TradeTicketRepository(session)
        ticket = await _draft(repo, actor.id)
        cancelled = await repo.set_status(
            ticket.id,
            status="cancelled",
            now=t_cancel,
            cancel_reason="Counterparty withdrew",
        )

    assert cancelled.status == "cancelled"
    assert cancelled.cancelled_at == t_cancel
    assert cancelled.cancel_reason == "Counterparty withdrew"


async def test_tt04_unknown_status_and_unknown_ticket(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("TT-04c")
    actor, _, _ = await _seed_actor_and_investments(app_engine, tenant_id, email="pm@tt04c.example")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = TradeTicketRepository(session)
        ticket = await _draft(repo, actor.id)

        with pytest.raises(TicketStateInvalid):
            await repo.set_status(ticket.id, status="settled", now=_T0)
        with pytest.raises(TicketNotFound):
            await repo.set_status(uuid4(), status="cancelled", now=_T0)

        still_draft = await repo.get(ticket.id)

    assert still_draft is not None
    assert still_draft.status == "draft"


async def test_tt04_forged_booked_row_without_actor_is_refused(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The b034 CHECK is the backstop under the repository's discipline.

    ``set_status`` writes the attribution, so the CHECK never fires on the
    happy path. This forges the row the repository would never write — a
    ``booked`` status with no ``booked_by`` — and asserts the schema refuses
    it by name.
    """
    tenant_id = await seed_tenant("TT-04d")
    actor, _, _ = await _seed_actor_and_investments(app_engine, tenant_id, email="pm@tt04d.example")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = TradeTicketRepository(session)
        ticket = await _draft(repo, actor.id)
        await repo.set_status(ticket.id, status="proposed", actor_user_id=actor.id, now=_T0)
        await repo.set_status(ticket.id, status="approved", actor_user_id=actor.id, now=_T0)

        with pytest.raises(IntegrityError) as excinfo:
            async with session.begin_nested():
                await session.execute(
                    text("UPDATE trade_tickets SET status = 'booked' WHERE id = :id"),
                    {"id": str(ticket.id)},
                )
        assert "ck_trade_tickets_booked_attribution" in str(excinfo.value)

        # The savepoint rolled back; the ticket is untouched and the session
        # is still usable.
        recovered = await repo.get(ticket.id)

    assert recovered is not None
    assert recovered.status == "approved"


async def test_tt04_list_by_status(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("TT-04e")
    actor, _, _ = await _seed_actor_and_investments(app_engine, tenant_id, email="pm@tt04e.example")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = TradeTicketRepository(session)
        first = await _draft(repo, actor.id)
        second = await _draft(repo, actor.id)
        third = await _draft(repo, actor.id)
        await repo.set_status(second.id, status="proposed", actor_user_id=actor.id, now=_T0)

        drafts = await repo.list_by_status(["draft"])
        proposed = await repo.list_by_status(["proposed"])
        both = await repo.list_by_status(["draft", "proposed"])
        none_asked = await repo.list_by_status([])

        with pytest.raises(TicketStateInvalid) as excinfo:
            await repo.list_by_status(["draft", "settled"])

    # Newest number first — the blotter's order.
    assert [t.id for t in drafts] == [third.id, first.id]
    assert [t.id for t in proposed] == [second.id]
    assert [t.ticket_number for t in both] == [3, 2, 1]
    assert none_asked == []
    assert excinfo.value.field == "status"


# ---------------------------------------------------------------------------
# TT-05: the effect linkage (ADR-0128 §2)
# ---------------------------------------------------------------------------


async def test_tt05_effects_round_trip(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("TT-05")
    actor, instrument, cash = await _seed_actor_and_investments(
        app_engine, tenant_id, email="pm@tt05.example"
    )
    instrument_leg, cash_leg, nav_row = uuid4(), uuid4(), uuid4()
    prior_state = {"is_active": True, "commitment_amount": "1000000.0000"}

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = TradeTicketRepository(session)
        ticket = await _draft(
            repo, actor.id, investment_id=instrument.id, cash_investment_id=cash.id
        )
        written = await repo.add_effects(
            ticket.id,
            [
                EffectInput(effect_type="position_txn", effect_id=instrument_leg),
                EffectInput(effect_type="position_txn", effect_id=cash_leg),
                EffectInput(effect_type="nav", effect_id=nav_row),
                EffectInput(
                    effect_type="investment_update",
                    effect_id=instrument.id,
                    prior_state=prior_state,
                ),
            ],
        )
        listed = await repo.list_effects(ticket.id)
        empty = await repo.add_effects(ticket.id, [])

    assert empty == []
    assert len(written) == 4
    assert {e.tenant_id for e in written} == {tenant_id}
    assert {e.ticket_id for e in written} == {ticket.id}
    # The effect_id carries no FK: instrument_leg / cash_leg / nav_row are
    # ids of rows this test never created (ADR-0128 §2).
    assert {(e.effect_type, e.effect_id) for e in written} == {
        ("position_txn", instrument_leg),
        ("position_txn", cash_leg),
        ("nav", nav_row),
        ("investment_update", instrument.id),
    }
    # prior_state is populated only for investment_update (D-2).
    by_type = {e.effect_type: e for e in written}
    assert by_type["investment_update"].prior_state == prior_state
    assert by_type["nav"].prior_state is None
    assert len(listed) == 4
    assert {e.id for e in listed} == {e.id for e in written}


async def test_tt05_duplicate_effect_is_refused(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("TT-05b")
    actor, _, _ = await _seed_actor_and_investments(app_engine, tenant_id, email="pm@tt05b.example")
    effect_id = uuid4()

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = TradeTicketRepository(session)
        ticket = await _draft(repo, actor.id)
        await repo.add_effects(
            ticket.id, [EffectInput(effect_type="cashflow", effect_id=effect_id)]
        )

        with pytest.raises(IntegrityError) as excinfo:
            async with session.begin_nested():
                await repo.add_effects(
                    ticket.id, [EffectInput(effect_type="cashflow", effect_id=effect_id)]
                )
        assert "uq_trade_ticket_effects_ticket_effect" in str(excinfo.value)

        # A different effect_type against the same row is a different effect.
        await repo.add_effects(ticket.id, [EffectInput(effect_type="nav", effect_id=effect_id)])
        listed = await repo.list_effects(ticket.id)

    assert len(listed) == 2


async def test_tt05_vocabulary_violation_writes_nothing(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("TT-05c")
    actor, _, _ = await _seed_actor_and_investments(app_engine, tenant_id, email="pm@tt05c.example")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = TradeTicketRepository(session)
        ticket = await _draft(repo, actor.id)

        # The good effect precedes the bad one; validation runs over the
        # whole batch before any SQL, so neither is written.
        with pytest.raises(TicketStateInvalid) as excinfo:
            await repo.add_effects(
                ticket.id,
                [
                    EffectInput(effect_type="cashflow", effect_id=uuid4()),
                    EffectInput(effect_type="ledger_row", effect_id=uuid4()),
                ],
            )
        assert excinfo.value.field == "effect_type"

        with pytest.raises(TicketNotFound):
            await repo.add_effects(uuid4(), [EffectInput(effect_type="nav", effect_id=uuid4())])

        listed = await repo.list_effects(ticket.id)

    assert listed == []


async def test_tt05_delete_effects_for_ticket(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("TT-05d")
    actor, _, _ = await _seed_actor_and_investments(app_engine, tenant_id, email="pm@tt05d.example")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = TradeTicketRepository(session)
        ticket = await _draft(repo, actor.id)
        other = await _draft(repo, actor.id)
        await repo.add_effects(
            ticket.id,
            [
                EffectInput(effect_type="position_txn", effect_id=uuid4()),
                EffectInput(effect_type="cashflow", effect_id=uuid4()),
            ],
        )
        await repo.add_effects(other.id, [EffectInput(effect_type="nav", effect_id=uuid4())])

        removed = await repo.delete_effects_for_ticket(ticket.id)
        again = await repo.delete_effects_for_ticket(ticket.id)
        remaining = await repo.list_effects(other.id)

    assert removed == 2
    assert again == 0
    # Only the named ticket's linkage is removed.
    assert len(remaining) == 1


# ---------------------------------------------------------------------------
# TT-06: cross-tenant invisibility on both tables (RLS smoke)
# ---------------------------------------------------------------------------


async def test_tt06_cross_tenant_invisibility(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant(name="A")
    tenant_b = await seed_tenant(name="B")
    actor_a, _, _ = await _seed_actor_and_investments(app_engine, tenant_a, email="pm@a-tt.example")
    actor_b, _, _ = await _seed_actor_and_investments(app_engine, tenant_b, email="pm@b-tt.example")

    async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
        repo = TradeTicketRepository(session)
        ticket = await _draft(repo, actor_a.id)
        await repo.add_effects(
            ticket.id, [EffectInput(effect_type="position_txn", effect_id=uuid4())]
        )

    async with tenant_context(app_engine, tenant_b, user_id=actor_b.id) as session:
        repo = TradeTicketRepository(session)
        assert await repo.get(ticket.id) is None
        assert await repo.list_by_status(["draft", "proposed", "booked"]) == []
        assert await repo.list_effects(ticket.id) == []
        assert await repo.delete_effects_for_ticket(ticket.id) == 0
        for table in ("trade_tickets", "trade_ticket_effects"):
            count = await session.execute(text(f"SELECT count(*) FROM {table}"))
            assert count.scalar_one() == 0

    # Tenant A still sees its own rows — nothing was destroyed above.
    async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
        repo = TradeTicketRepository(session)
        assert await repo.get(ticket.id) is not None
        assert len(await repo.list_effects(ticket.id)) == 1


# ---------------------------------------------------------------------------
# TT-07: the FK behaviours — CASCADE downwards, RESTRICT sideways
# ---------------------------------------------------------------------------


async def test_tt07_deleting_a_ticket_cascades_its_effects(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("TT-07")
    actor, _, _ = await _seed_actor_and_investments(app_engine, tenant_id, email="pm@tt07.example")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = TradeTicketRepository(session)
        ticket = await _draft(repo, actor.id)
        survivor = await _draft(repo, actor.id)
        await repo.add_effects(
            ticket.id,
            [
                EffectInput(effect_type="position_txn", effect_id=uuid4()),
                EffectInput(effect_type="cashflow", effect_id=uuid4()),
            ],
        )
        await repo.add_effects(survivor.id, [EffectInput(effect_type="nav", effect_id=uuid4())])

        await session.execute(
            text("DELETE FROM trade_tickets WHERE id = :id"), {"id": str(ticket.id)}
        )

        assert await repo.get(ticket.id) is None
        assert await repo.list_effects(ticket.id) == []
        assert len(await repo.list_effects(survivor.id)) == 1


async def test_tt07_referenced_investment_cannot_be_deleted(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """``investment_id`` is ondelete RESTRICT — the ticket is the record.

    A ticket naming an investment is the provenance of a portfolio change;
    letting the investment vanish underneath it would leave the record
    dangling.
    """
    tenant_id = await seed_tenant("TT-07b")
    actor, instrument, cash = await _seed_actor_and_investments(
        app_engine, tenant_id, email="pm@tt07b.example"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = TradeTicketRepository(session)
        ticket = await _draft(
            repo, actor.id, investment_id=instrument.id, cash_investment_id=cash.id
        )

        for referenced in (instrument.id, cash.id):
            with pytest.raises(IntegrityError) as excinfo:
                async with session.begin_nested():
                    await session.execute(
                        text("DELETE FROM investments WHERE id = :id"),
                        {"id": str(referenced)},
                    )
            assert "trade_tickets" in str(excinfo.value)

        # The savepoints rolled back; the ticket and both investments stand.
        still_there = await repo.get(ticket.id)

    assert still_there is not None
    assert still_there.investment_id == instrument.id
    assert still_there.cash_investment_id == cash.id
