# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Migration guard for b029 — ADR-0100 cash rows → unitised (ADR-0103 §9).

Two halves, both against a live Postgres server:

**The conversion.** A ``'reported'`` cash row fed by ``investment_navs``
levels comes out the other side with a synthesised ledger (opening + signed
delta transfers, zero deltas omitted), one unity price per NAV date, and
``valuation_mode='unitised'`` — while a row that *already* carries a
Cash-sheet ledger (the premature-v32 case S1.3 permits) is flipped without
being double-booked.

**The acceptance proof (ADR-0103 §9.5).** The real
:class:`~services.investments.NavMaterialisationService` is run against each
migrated row and must report ``inserted == 0`` and ``updated == 0``, with
every NAV date counted as a precedence skip and every pre-existing NAV row
left byte-identical. §9.5 asserts this in prose — "the run is a provable
no-op on them, which is the migration's acceptance check" — and this module
is that sentence as an executable fact. It is the reason the migration itself
does not (and must not) call the service: it cannot, without importing the
application package into a migration, and it does not need to.

The migration operates on **all tenants** under the superuser context, so the
fixtures seed their own throwaway tenant rather than leaning on a bootstrap
one.

Runs against a **per-test scratch database**: the ``scratch_db`` fixture
creates one, migrates it to ``head`` and drops it again afterwards, so the
downgrade this module drives never reaches the shared dev database (see
``tests/regression/conftest.py`` for why that matters — b029's own
``down_revision`` is b028, whose downgrade fails outright once a real
``'investor_flow'`` row exists). The migration still runs through the Alembic
CLI in a subprocess (the b028 idiom, so it does not contend with this module's
own connections) and ``head`` is always restored. If the server is unreachable
the module skips, like every other live-DB regression guard.
"""

from __future__ import annotations

import importlib.util
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    InstrumentPriceRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    PositionTransactionRepository,
    tenant_context,
)
from services.investments import NavMaterialisationService
from services.investments.unity_price import UNITY_PRICE
from tests.regression.conftest import ScratchDatabase

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: b029's own ``down_revision`` — named, never ``-1``: a relative downgrade is
#: relative to the DB's current head, so it silently stops undoing *this*
#: migration the moment a newer one lands (the b028 guard's reasoning).
_BELOW = "b028_add_investor_flow_type"

_MIGRATION = (
    _REPO_ROOT
    / "db"
    / "migrations"
    / "versions"
    / "2026_07_13_1300_b029_migrate_cash_to_unitised.py"
)

#: The deterministic provenance marker b029 stamps on every row it synthesises.
#: Pinned against the migration's own literal by
#: :func:`test_migration_constants_are_anchored`.
_MARKER = "adr-0103-s14-migration"

# Fixture A — the ADR-0100 shape: NAV levels only, no ledger, no prices.
_A1 = date(2026, 1, 31)
_A2 = date(2026, 2, 28)  # repeated balance → zero delta → no transfer
_A3 = date(2026, 3, 31)  # 'manual' origin → absorbed as a level
_A4 = date(2026, 4, 30)
_A_PLAN = date(2026, 5, 31)  # nav_kind='plan' → must not participate at all

_A_BALANCES: dict[date, Decimal] = {
    _A1: Decimal("1000.0000"),
    _A2: Decimal("1000.0000"),
    _A3: Decimal("1500.0000"),
    _A4: Decimal("1200.0000"),
}

# Fixture B — the premature-v32 shape: a Cash-sheet ledger and unity prices
# already present (S1.3 writes them without flipping the mode), balances
# additionally carried as 'excel' NAV rows (the S1.3 fallback).
_B1 = date(2026, 1, 31)
_B2 = date(2026, 2, 28)
_B_BALANCES: dict[date, Decimal] = {
    _B1: Decimal("2000.0000"),
    _B2: Decimal("2500.0000"),
}


@dataclass(frozen=True)
class _Seeded:
    """Ids of one seeded scenario."""

    tenant_id: UUID
    user_id: UUID
    cash_a: UUID
    cash_b: UUID


# ---------------------------------------------------------------------------
# Alembic, seeding
# ---------------------------------------------------------------------------


def _upgrade_head(scratch: ScratchDatabase) -> None:
    result = scratch.alembic("upgrade", "head")
    assert result.returncode == 0, f"b029 upgrade failed:\n{result.stderr}"


def _downgrade_below(scratch: ScratchDatabase) -> None:
    result = scratch.alembic("downgrade", _BELOW)
    assert result.returncode == 0, f"b029 downgrade failed:\n{result.stderr}"


async def _seed(engine: AsyncEngine) -> _Seeded:
    """Create a throwaway tenant carrying both cash-row shapes, pre-migration.

    Runs as superuser (RLS bypassed), exactly as the migration itself will:
    every row states its ``tenant_id`` explicitly.
    """
    tenant_id, user_id = uuid4(), uuid4()
    cash_a, cash_b, asset_class_id = uuid4(), uuid4(), uuid4()

    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, :sub)"),
            {
                "id": tenant_id,
                "name": f"B029 Guard {tenant_id}",
                "sub": f"b029-{tenant_id.hex[:12]}",
            },
        )
        await conn.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, password_hash) "
                "VALUES (:id, :tenant, :email, 'x')"
            ),
            {
                "id": user_id,
                "tenant": tenant_id,
                "email": f"b029-{tenant_id.hex[:12]}@example.test",
            },
        )
        await conn.execute(
            text(
                "INSERT INTO asset_classes (id, tenant_id, code, display_name) "
                "VALUES (:id, :tenant, 'cash', 'Cash')"
            ),
            {"id": asset_class_id, "tenant": tenant_id},
        )
        for investment_id, name in ((cash_a, "Cash USD A"), (cash_b, "Cash USD B")):
            await conn.execute(
                text(
                    "INSERT INTO investments "
                    "(id, tenant_id, name, investment_type, asset_class_id, "
                    " currency, valuation_mode, created_by) "
                    "VALUES (:id, :tenant, :name, 'cash', :ac, 'USD', "
                    " 'reported', :user)"
                ),
                {
                    "id": investment_id,
                    "tenant": tenant_id,
                    "name": name,
                    "ac": asset_class_id,
                    "user": user_id,
                },
            )

        # --- Fixture A: 'excel' levels, one 'manual' level, one plan row. ---
        for as_of, origin in (
            (_A1, "excel"),
            (_A2, "excel"),
            (_A3, "manual"),
            (_A4, "excel"),
        ):
            await _insert_nav(
                conn,
                tenant_id=tenant_id,
                investment_id=cash_a,
                as_of=as_of,
                value=_A_BALANCES[as_of],
                kind="actual",
                origin=origin,
                user_id=user_id,
            )
        # A plan row on the same position: a different series entirely. It must
        # not reach the ledger, the prices, or the materialised set.
        await _insert_nav(
            conn,
            tenant_id=tenant_id,
            investment_id=cash_a,
            as_of=_A_PLAN,
            value=Decimal("9999.0000"),
            kind="plan",
            origin="excel",
            user_id=user_id,
        )

        # --- Fixture B: the premature-v32 artefacts. ---
        for as_of in (_B1, _B2):
            await _insert_nav(
                conn,
                tenant_id=tenant_id,
                investment_id=cash_b,
                as_of=as_of,
                value=_B_BALANCES[as_of],
                kind="actual",
                origin="excel",
                user_id=user_id,
            )
            await conn.execute(
                text(
                    "INSERT INTO instrument_prices "
                    "(tenant_id, investment_id, as_of_date, price, currency, "
                    " source, ingest_origin, created_by) "
                    "VALUES (:tenant, :inv, :as_of, 1.00000000, 'USD', "
                    " 'excel-import:cash-statement', 'excel', :user)"
                ),
                {
                    "tenant": tenant_id,
                    "inv": cash_b,
                    "as_of": as_of,
                    "user": user_id,
                },
            )
        for txn_type, trade_date, units in (
            ("opening", _B1, _B_BALANCES[_B1]),
            ("transfer", _B2, _B_BALANCES[_B2] - _B_BALANCES[_B1]),
        ):
            await conn.execute(
                text(
                    "INSERT INTO position_transactions "
                    "(tenant_id, investment_id, txn_type, trade_date, units, "
                    " currency, source, ingest_origin, created_by) "
                    "VALUES (:tenant, :inv, :txn, :on, :units, 'USD', "
                    " 'excel-import:cash-statement', 'excel', :user)"
                ),
                {
                    "tenant": tenant_id,
                    "inv": cash_b,
                    "txn": txn_type,
                    "on": trade_date,
                    "units": units,
                    "user": user_id,
                },
            )

    return _Seeded(tenant_id=tenant_id, user_id=user_id, cash_a=cash_a, cash_b=cash_b)


async def _insert_nav(
    conn,
    *,
    tenant_id: UUID,
    investment_id: UUID,
    as_of: date,
    value: Decimal,
    kind: str,
    origin: str,
    user_id: UUID,
) -> None:
    await conn.execute(
        text(
            "INSERT INTO investment_navs "
            "(tenant_id, investment_id, as_of_date, nav_value, currency, "
            " nav_kind, basis, source, ingest_origin, created_by) "
            "VALUES (:tenant, :inv, :as_of, :value, 'USD', :kind, 'reported', "
            " 'adr-0100-import', :origin, :user)"
        ),
        {
            "tenant": tenant_id,
            "inv": investment_id,
            "as_of": as_of,
            "value": value,
            "kind": kind,
            "origin": origin,
            "user": user_id,
        },
    )


async def _cleanup(engine: AsyncEngine, seeded: _Seeded) -> None:
    """Remove the throwaway tenant and everything hanging off it.

    Order is FK-driven: the domain rows first (their audit triggers fire and
    write further ``audit_log`` rows), then the audit trail, then the tenant —
    ``audit_log.tenant_id`` is ``ON DELETE RESTRICT``, so it must be empty
    before the tenant goes. ``investments`` cascades to its NAVs, prices and
    ledger; ``users`` is ``ON DELETE SET NULL`` from ``audit_log``.
    """
    async with engine.begin() as conn:
        for statement in (
            "DELETE FROM investments WHERE tenant_id = :tenant",
            "DELETE FROM asset_classes WHERE tenant_id = :tenant",
            "DELETE FROM users WHERE tenant_id = :tenant",
            "DELETE FROM audit_log WHERE tenant_id = :tenant",
            "DELETE FROM tenants WHERE id = :tenant",
        ):
            await conn.execute(text(statement), {"tenant": seeded.tenant_id})


@pytest_asyncio.fixture
async def seeded(
    scratch_db: ScratchDatabase, scratch_superuser_engine: AsyncEngine
) -> AsyncGenerator[_Seeded, None]:
    """Put the DB one revision below b029 and seed both pre-migration shapes.

    Each test drives the upgrade itself — the migration under test is the
    subject, not a fixture side effect.
    """
    _downgrade_below(scratch_db)
    rows = await _seed(scratch_superuser_engine)
    try:
        yield rows
    finally:
        # Rows first, head second: leaving 'reported' cash rows behind while
        # re-upgrading would hand b029 a second helping of the fixtures.
        await _cleanup(scratch_superuser_engine, rows)
        scratch_db.alembic("upgrade", "head")


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


async def _ledger(engine: AsyncEngine, investment_id: UUID) -> list[dict]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT txn_type, trade_date, units, price_per_unit, "
                "       consideration, currency, source, ingest_origin, "
                "       created_by, tenant_id "
                "  FROM position_transactions WHERE investment_id = :inv "
                " ORDER BY trade_date"
            ),
            {"inv": investment_id},
        )
        return [dict(row) for row in result.mappings()]


async def _prices(engine: AsyncEngine, investment_id: UUID) -> list[dict]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT as_of_date, price, currency, source, ingest_origin, "
                "       created_by, tenant_id "
                "  FROM instrument_prices WHERE investment_id = :inv "
                " ORDER BY as_of_date"
            ),
            {"inv": investment_id},
        )
        return [dict(row) for row in result.mappings()]


async def _navs(engine: AsyncEngine, investment_id: UUID) -> list[dict]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT as_of_date, nav_value, nav_kind, ingest_origin, "
                "       source, updated_at "
                "  FROM investment_navs WHERE investment_id = :inv "
                " ORDER BY nav_kind, as_of_date"
            ),
            {"inv": investment_id},
        )
        return [dict(row) for row in result.mappings()]


async def _valuation_mode(engine: AsyncEngine, investment_id: UUID) -> str:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT valuation_mode FROM investments WHERE id = :id"),
            {"id": investment_id},
        )
        return result.scalar_one()


async def _materialise(app_engine: AsyncEngine, seeded: _Seeded, investment_id: UUID):
    """Run the real ADR-0098 service, under the real RLS role."""
    async with tenant_context(app_engine, seeded.tenant_id, user_id=seeded.user_id) as session:
        service = NavMaterialisationService(
            InvestmentRepository(session),
            InvestmentNavRepository(session),
            InstrumentPriceRepository(session),
            PositionTransactionRepository(session),
        )
        return await service.materialise(investment_id, acting_user=seeded.user_id)


# ---------------------------------------------------------------------------
# 1. Fixture A — the ADR-0100 shape converts
# ---------------------------------------------------------------------------


async def test_reported_cash_row_converts_to_a_unitised_ledger(
    scratch_db: ScratchDatabase, scratch_superuser_engine: AsyncEngine, seeded: _Seeded
) -> None:
    """§9.1–9.4: levels become a ledger, dates become unity prices, mode flips."""
    assert await _valuation_mode(scratch_superuser_engine, seeded.cash_a) == "reported"

    _upgrade_head(scratch_db)

    ledger = await _ledger(scratch_superuser_engine, seeded.cash_a)
    # One opening at the earliest NAV date carrying the level itself; signed
    # transfers afterwards — and *nothing* on _A2, whose balance was unchanged
    # (ADR-0103 §4: a zero delta is not an event).
    assert [(r["txn_type"], r["trade_date"]) for r in ledger] == [
        ("opening", _A1),
        ("transfer", _A3),
        ("transfer", _A4),
    ]
    assert ledger[0]["units"] == _A_BALANCES[_A1]
    assert ledger[1]["units"] == _A_BALANCES[_A3] - _A_BALANCES[_A2]  # +500
    assert ledger[2]["units"] == _A_BALANCES[_A4] - _A_BALANCES[_A3]  # −300

    for row in ledger:
        assert row["price_per_unit"] is None
        assert row["consideration"] is None
        assert row["currency"] == "USD"
        assert row["ingest_origin"] == "excel"
        assert row["source"] == _MARKER
        # Provenance mirrored from the source NAV rows, never invented.
        assert row["created_by"] == seeded.user_id
        assert row["tenant_id"] == seeded.tenant_id

    # One unity price per *actual* NAV date — the plan date is not a statement
    # date and gets nothing.
    prices = await _prices(scratch_superuser_engine, seeded.cash_a)
    assert [r["as_of_date"] for r in prices] == [_A1, _A2, _A3, _A4]
    for row in prices:
        assert row["price"] == UNITY_PRICE
        assert str(row["price"]) == "1.00000000"  # stored at scale 8
        assert row["currency"] == "USD"
        assert row["ingest_origin"] == "excel"
        assert row["source"] == _MARKER
        assert row["created_by"] == seeded.user_id
        assert row["tenant_id"] == seeded.tenant_id

    assert await _valuation_mode(scratch_superuser_engine, seeded.cash_a) == "unitised"

    # The 'manual' level was absorbed into the ledger as an ordinary level —
    # the transfer into _A3 is the delta *to* it — and the row itself is
    # untouched, still 'manual'.
    navs = await _navs(scratch_superuser_engine, seeded.cash_a)
    manual = [n for n in navs if n["ingest_origin"] == "manual"]
    assert len(manual) == 1
    assert manual[0]["as_of_date"] == _A3
    assert manual[0]["nav_value"] == _A_BALANCES[_A3]


async def test_plan_navs_never_reach_the_ledger_or_the_price_series(
    scratch_db: ScratchDatabase, scratch_superuser_engine: AsyncEngine, seeded: _Seeded
) -> None:
    """Only ``nav_kind='actual'`` rows are levels; the plan series is a no-op."""
    _upgrade_head(scratch_db)

    ledger_dates = {r["trade_date"] for r in await _ledger(scratch_superuser_engine, seeded.cash_a)}
    price_dates = {r["as_of_date"] for r in await _prices(scratch_superuser_engine, seeded.cash_a)}
    assert _A_PLAN not in ledger_dates
    assert _A_PLAN not in price_dates

    # And the plan row itself survives the migration untouched.
    plan = [
        n for n in await _navs(scratch_superuser_engine, seeded.cash_a) if n["nav_kind"] == "plan"
    ]
    assert len(plan) == 1
    assert plan[0]["nav_value"] == Decimal("9999.0000")


# ---------------------------------------------------------------------------
# 2. The acceptance proof — ADR-0103 §9.5
# ---------------------------------------------------------------------------


async def test_materialisation_after_the_migration_is_a_provable_no_op(
    scratch_db: ScratchDatabase,
    scratch_superuser_engine: AsyncEngine,
    scratch_app_engine: AsyncEngine,
    seeded: _Seeded,
) -> None:
    """§9.5, executable: the ADR-0098 run writes nothing on a migrated row.

    Every price date the migration wrote is a NAV date that already carries an
    ``'excel'`` / ``'manual'`` row, so every target date is precedence-
    protected: the run inserts nothing, updates nothing, and counts each NAV
    date as a skip. This is the migration's acceptance check, and the reason it
    is safe for the migration not to run the service itself.
    """
    _upgrade_head(scratch_db)

    before_a = await _navs(scratch_superuser_engine, seeded.cash_a)
    before_b = await _navs(scratch_superuser_engine, seeded.cash_b)

    report_a = await _materialise(scratch_app_engine, seeded, seeded.cash_a)
    report_b = await _materialise(scratch_app_engine, seeded, seeded.cash_b)

    # Fixture A: four actual NAV dates → three 'excel' skips + one 'manual'.
    assert report_a.inserted == 0
    assert report_a.updated == 0
    assert report_a.deleted == 0
    assert report_a.skipped_excel == 3
    assert report_a.skipped_manual == 1
    assert report_a.skipped_live == 0
    assert report_a.skipped_excel + report_a.skipped_manual == len(_A_BALANCES), (
        "every actual NAV date must be a counted skip"
    )

    # Fixture B: two actual NAV dates, both 'excel'.
    assert report_b.inserted == 0
    assert report_b.updated == 0
    assert report_b.deleted == 0
    assert report_b.skipped_excel == len(_B_BALANCES)
    assert report_b.skipped_manual == 0

    # Byte-identical: same values, same origins, and — the load-bearing part —
    # no updated_at bumped. A single write would show up here.
    assert await _navs(scratch_superuser_engine, seeded.cash_a) == before_a
    assert await _navs(scratch_superuser_engine, seeded.cash_b) == before_b

    # No 'system' row was created anywhere: the target set is exactly the set
    # of dates the book already covers.
    for navs in (before_a, before_b):
        assert all(n["ingest_origin"] != "system" for n in navs)


# ---------------------------------------------------------------------------
# 3. Fixture B — premature-v32 artefacts are not double-booked
# ---------------------------------------------------------------------------


async def test_premature_v32_artefacts_are_flipped_but_never_duplicated(
    scratch_db: ScratchDatabase, scratch_superuser_engine: AsyncEngine, seeded: _Seeded
) -> None:
    """A row that already has a Cash-sheet ledger keeps it; only the mode moves.

    S1.3 lets a v32 import write an ``'excel'`` ledger and unity prices onto a
    cash row that is still ``'reported'`` (it warns, and never flips the mode
    itself). Such a row arrives here with a book of record already; the
    existence guards must leave it exactly as it is.
    """
    ledger_before = await _ledger(scratch_superuser_engine, seeded.cash_b)
    prices_before = await _prices(scratch_superuser_engine, seeded.cash_b)

    _upgrade_head(scratch_db)

    ledger_after = await _ledger(scratch_superuser_engine, seeded.cash_b)
    prices_after = await _prices(scratch_superuser_engine, seeded.cash_b)

    # No second opening (the partial unique index would refuse one anyway), no
    # duplicate transfer, no duplicate price — and not one marker row: this
    # row's ledger synthesis was skipped whole.
    assert ledger_after == ledger_before
    assert prices_after == prices_before
    assert [r["txn_type"] for r in ledger_after] == ["opening", "transfer"]
    assert all(r["source"] != _MARKER for r in ledger_after)
    assert all(r["source"] != _MARKER for r in prices_after)

    # The one thing that does change.
    assert await _valuation_mode(scratch_superuser_engine, seeded.cash_b) == "unitised"


# ---------------------------------------------------------------------------
# 4. Downgrade
# ---------------------------------------------------------------------------


async def test_downgrade_reverts_marker_rows_and_leaves_documented_residue(
    scratch_db: ScratchDatabase, scratch_superuser_engine: AsyncEngine, seeded: _Seeded
) -> None:
    """The marker is the downgrade's only handle — and it is honest about that.

    Fixture A was synthesised by the migration, so it reverts completely.
    Fixture B was only *flipped* (its ledger was already there), which leaves
    no marker behind — so it stays ``'unitised'``, exactly the one-way residue
    the migration docstring documents (the b028 "reversible only while …"
    precedent).
    """
    _upgrade_head(scratch_db)
    assert await _valuation_mode(scratch_superuser_engine, seeded.cash_a) == "unitised"

    _downgrade_below(scratch_db)

    # Fixture A: back to the ADR-0100 shape, marker-free.
    assert await _valuation_mode(scratch_superuser_engine, seeded.cash_a) == "reported"
    assert await _ledger(scratch_superuser_engine, seeded.cash_a) == []
    assert await _prices(scratch_superuser_engine, seeded.cash_a) == []
    # Its NAV levels — the thing the migration read — were never touched.
    actual = [
        n for n in await _navs(scratch_superuser_engine, seeded.cash_a) if n["nav_kind"] == "actual"
    ]
    assert {n["as_of_date"]: n["nav_value"] for n in actual} == _A_BALANCES

    # Fixture B: documented one-way residue. The mode stays flipped (no marker
    # opening to identify it by) and its imported ledger is untouched — the
    # downgrade deletes marker rows only, never another producer's.
    assert await _valuation_mode(scratch_superuser_engine, seeded.cash_b) == "unitised"
    assert len(await _ledger(scratch_superuser_engine, seeded.cash_b)) == 2
    assert len(await _prices(scratch_superuser_engine, seeded.cash_b)) == 2


# ---------------------------------------------------------------------------
# 5. Integrity gates — abort, never repair
# ---------------------------------------------------------------------------


async def test_a_negative_actual_balance_aborts_the_migration(
    scratch_db: ScratchDatabase, scratch_superuser_engine: AsyncEngine, seeded: _Seeded
) -> None:
    """ADR-0100 §5: an actual cash balance cannot be negative.

    A negative level is corruption, not input — synthesising a ledger from it
    would launder it into the book of record. The migration names the row and
    refuses, and because Alembic wraps the run in one transaction, nothing is
    written: the DB stays at b028 with the cash rows untouched.
    """
    async with scratch_superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE investment_navs SET nav_value = -1.0000 "
                " WHERE investment_id = :inv AND as_of_date = :on"
            ),
            {"inv": seeded.cash_a, "on": _A4},
        )

    result = scratch_db.alembic("upgrade", "head")

    assert result.returncode != 0, "the migration must refuse a negative balance"
    assert "negative actual" in result.stderr
    assert "Cash USD A" in result.stderr, "the offending row must be named"

    # Nothing partially applied: no ledger, no prices, no flip.
    assert await _valuation_mode(scratch_superuser_engine, seeded.cash_a) == "reported"
    assert await _ledger(scratch_superuser_engine, seeded.cash_a) == []
    assert await _prices(scratch_superuser_engine, seeded.cash_a) == []


async def test_a_zero_opening_balance_aborts_the_migration(
    scratch_db: ScratchDatabase, scratch_superuser_engine: AsyncEngine, seeded: _Seeded
) -> None:
    """A zero earliest balance has no representable opening — so the run stops.

    ``ck_position_transactions_sign`` requires ``units > 0`` on an ``opening``,
    which makes ADR-0103 §9.2's "``units = nav_value``" impossible to satisfy
    when that value is zero. Neither the schema nor §9 has an answer, so the
    migration names the row rather than inventing one (it would otherwise die
    on a raw CHECK violation naming nothing). Fixture B, whose opening already
    exists, is unaffected by the gate — it synthesises nothing.
    """
    async with scratch_superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE investment_navs SET nav_value = 0.0000 "
                " WHERE investment_id = :inv AND as_of_date = :on"
            ),
            {"inv": seeded.cash_a, "on": _A1},
        )

    result = scratch_db.alembic("upgrade", "head")

    assert result.returncode != 0, "the migration must refuse a zero opening"
    assert "zero balance" in result.stderr
    assert "Cash USD A" in result.stderr

    assert await _valuation_mode(scratch_superuser_engine, seeded.cash_a) == "reported"
    assert await _ledger(scratch_superuser_engine, seeded.cash_a) == []


async def test_a_foreign_currency_nav_row_aborts_the_migration(
    scratch_db: ScratchDatabase, scratch_superuser_engine: AsyncEngine, seeded: _Seeded
) -> None:
    """ADR-0097 §5: a ledger is stated in the position's own currency.

    A NAV row denominated in something else has no representable ledger row,
    and taking its number anyway would smuggle a silent 1:1 conversion into the
    write path — which ADR-0099 forbids outright.
    """
    async with scratch_superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE investment_navs SET currency = 'EUR' "
                " WHERE investment_id = :inv AND as_of_date = :on"
            ),
            {"inv": seeded.cash_a, "on": _A3},
        )

    result = scratch_db.alembic("upgrade", "head")

    assert result.returncode != 0, "the migration must refuse a converted level"
    assert "currency other than" in result.stderr
    assert "Cash USD A" in result.stderr

    assert await _valuation_mode(scratch_superuser_engine, seeded.cash_a) == "reported"
    assert await _ledger(scratch_superuser_engine, seeded.cash_a) == []


# ---------------------------------------------------------------------------
# 6. Anchors — the b012 constants-pin precedent
# ---------------------------------------------------------------------------


def _load_migration():
    spec = importlib.util.spec_from_file_location("b029_migration", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_constants_are_anchored() -> None:
    """The migration restates two literals; both are pinned to their originals.

    A migration may not import the application package (b012 idiom), so
    ``_UNITY_PRICE`` and ``_MARKER`` are anchor copies. This is what stops the
    copy from drifting from ``services.investments.unity_price.UNITY_PRICE`` —
    a drifted price literal would silently restate every migrated balance,
    which is precisely the failure the unity-price seam exists to prevent.
    """
    module = _load_migration()

    assert module._UNITY_PRICE == str(UNITY_PRICE) == "1.00000000"
    assert Decimal(module._UNITY_PRICE) == UNITY_PRICE
    assert module._MARKER == _MARKER

    # The downgrade's only handle: deterministic, so a re-run spells it the
    # same way (no timestamp, no run id).
    assert module._MARKER.isascii() and " " not in module._MARKER
