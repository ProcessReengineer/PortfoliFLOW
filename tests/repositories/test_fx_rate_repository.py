# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""FxRateRepository tests against the live compose Postgres.

Coverage (ADR-0099 §2, mirroring the instrument-price repository):

* ``upsert`` is INSERT-then-UPDATE on ``(tenant_id, currency, as_of_date)``.
* ``upsert_live`` guard matrix (ADR-0092): insert new / refresh own live /
  no-op on an ``'excel'`` row / no-op on a ``'manual'`` row — the no-op
  returns ``None`` and leaves the book-of-record row byte-identical.
* The unique key rejects a duplicate ``(tenant_id, currency, as_of_date)``.
* The ``rate_to_reference > 0``, ``currency <> reference_currency`` and
  ``ingest_origin`` CHECKs reject bad rows. The second one is the schema
  half of the identity short-circuit: the rate of the reference currency
  against itself is never a row.
* ``list_by_currency`` orders ascending and honours a hard date window.
* ``load_rates_frame`` pulls the per-currency **carry-forward anchor** —
  the latest row at or before the window start — and returns the typed
  empty frame when the tenant holds no rates.
* ``delete`` / ``delete_by_currency`` report what they removed.
* RLS isolates rates between tenants, the WITH CHECK clause rejects a
  foreign-tenant write, and the audit trigger records the writer
  (following ``test_investment_audit_and_isolation.py``).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    RATES_FRAME_COLUMNS,
    FxRateRepository,
    UserRepository,
    tenant_context,
)


async def _seed_actor(app_engine: AsyncEngine, tenant_id, *, email: str):
    """Create the one user an ``fx_rates`` write needs for ``created_by``.

    Unlike ``instrument_prices``, ``fx_rates`` hangs off the tenant rather
    than an investment — no asset class and no investment are required.
    """
    async with tenant_context(app_engine, tenant_id) as session:
        return await UserRepository(session).create(email=email, password_hash="x" * 8)


async def _upsert(
    app_engine: AsyncEngine,
    tenant_id,
    actor_id,
    *,
    currency: str = "USD",
    as_of_date: date = date(2025, 12, 31),
    rate: str = "0.9200000000",
    reference_currency: str = "EUR",
    source: str = "ecb",
    ingest_origin: str = "excel",
):
    async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
        return await FxRateRepository(session).upsert(
            currency=currency,
            as_of_date=as_of_date,
            rate_to_reference=Decimal(rate),
            reference_currency=reference_currency,
            source=source,
            created_by=actor_id,
            ingest_origin=ingest_origin,
        )


# ---------------------------------------------------------------------------
# FX-01: upsert inserts on first call, updates on second call
# ---------------------------------------------------------------------------


async def test_fx01_upsert_inserts_then_updates(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed_actor(app_engine, tenant_id, email="fx01@example.com")

    first = await _upsert(app_engine, tenant_id, actor.id, rate="0.9200000000", source="initial")
    second = await _upsert(app_engine, tenant_id, actor.id, rate="0.9350000000", source="corrected")

    assert first.id == second.id  # UPDATE, not a second INSERT
    assert second.rate_to_reference == Decimal("0.9350000000")
    assert second.source == "corrected"
    assert second.reference_currency == "EUR"
    assert second.created_by == actor.id  # original author preserved


# ---------------------------------------------------------------------------
# FX-02..04: the ADR-0092 upsert_live guard matrix
# ---------------------------------------------------------------------------


async def test_fx02_upsert_live_inserts_new(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed_actor(app_engine, tenant_id, email="fx02@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        result = await FxRateRepository(session).upsert_live(
            currency="USD",
            as_of_date=date(2025, 12, 31),
            rate_to_reference=Decimal("0.9200000000"),
            reference_currency="EUR",
            source="ecb",
            created_by=actor.id,
        )

    assert result is not None
    assert result.ingest_origin == "live"
    assert result.rate_to_reference == Decimal("0.9200000000")


async def test_fx03_upsert_live_refreshes_own_live_row(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed_actor(app_engine, tenant_id, email="fx03@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        first = await FxRateRepository(session).upsert_live(
            currency="USD",
            as_of_date=date(2025, 12, 31),
            rate_to_reference=Decimal("0.9200000000"),
            reference_currency="EUR",
            source="ecb",
            created_by=actor.id,
        )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        second = await FxRateRepository(session).upsert_live(
            currency="USD",
            as_of_date=date(2025, 12, 31),
            rate_to_reference=Decimal("0.9310000000"),
            reference_currency="EUR",
            source="ecb",
            created_by=actor.id,
        )

    assert first is not None and second is not None
    assert first.id == second.id  # refreshed in place
    assert second.rate_to_reference == Decimal("0.9310000000")


@pytest.mark.parametrize("origin", ["excel", "manual"])
async def test_fx04_upsert_live_noops_on_book_of_record(
    app_engine: AsyncEngine, seed_tenant, origin: str
) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed_actor(app_engine, tenant_id, email=f"fx04-{origin}@example.com")
    seeded = await _upsert(
        app_engine,
        tenant_id,
        actor.id,
        rate="0.9200000000",
        source="book",
        ingest_origin=origin,
    )

    # A live write on the same key must be a recorded no-op.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        result = await FxRateRepository(session).upsert_live(
            currency="USD",
            as_of_date=date(2025, 12, 31),
            rate_to_reference=Decimal("0.7700000000"),
            reference_currency="EUR",
            source="yahoo",
            created_by=actor.id,
        )
    assert result is None  # guarded no-op, never an error

    # The book-of-record row is byte-identical.
    async with tenant_context(app_engine, tenant_id) as session:
        rows = await FxRateRepository(session).list_by_currency("USD")
    assert len(rows) == 1
    row = rows[0]
    assert row.ingest_origin == origin
    assert row.rate_to_reference == Decimal("0.9200000000")
    assert row.source == "book"
    assert row.updated_at == seeded.updated_at  # not bumped


# ---------------------------------------------------------------------------
# FX-05: the schema constraints
# ---------------------------------------------------------------------------


async def test_fx05a_rate_positive_check(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed_actor(app_engine, tenant_id, email="fx05a@example.com")

    with pytest.raises(IntegrityError):
        await _upsert(app_engine, tenant_id, actor.id, rate="0")


async def test_fx05b_identity_rate_is_never_stored(app_engine: AsyncEngine, seed_tenant) -> None:
    """``ck_fx_rates_currency_not_reference``: rate(reference) is code, not data."""
    tenant_id = await seed_tenant()
    actor = await _seed_actor(app_engine, tenant_id, email="fx05b@example.com")

    with pytest.raises(IntegrityError):
        await _upsert(
            app_engine,
            tenant_id,
            actor.id,
            currency="EUR",
            reference_currency="EUR",
            rate="1.0",
        )


async def test_fx05c_invalid_ingest_origin_rejected(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed_actor(app_engine, tenant_id, email="fx05c@example.com")

    with pytest.raises(IntegrityError):
        await _upsert(app_engine, tenant_id, actor.id, ingest_origin="bogus")


async def test_fx05d_unique_key_rejects_a_raw_duplicate(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A raw INSERT bypassing ON CONFLICT hits ``uq_fx_rates_tenant_currency_date``."""
    tenant_id = await seed_tenant()
    actor = await _seed_actor(app_engine, tenant_id, email="fx05d@example.com")
    await _upsert(app_engine, tenant_id, actor.id)

    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            await session.execute(
                text(
                    "INSERT INTO fx_rates (tenant_id, as_of_date, currency, "
                    "rate_to_reference, reference_currency, source, "
                    "ingest_origin, created_by) "
                    "VALUES (:tid, '2025-12-31', 'USD', 0.95, 'EUR', "
                    "'dupe', 'excel', :uid)"
                ),
                {"tid": str(tenant_id), "uid": str(actor.id)},
            )


# ---------------------------------------------------------------------------
# FX-06: list_by_currency ordering and hard window
# ---------------------------------------------------------------------------


async def test_fx06_list_orders_ascending_and_windows_hard(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed_actor(app_engine, tenant_id, email="fx06@example.com")
    for day in (date(2025, 6, 30), date(2024, 12, 31), date(2025, 12, 31)):
        await _upsert(app_engine, tenant_id, actor.id, as_of_date=day)
    await _upsert(app_engine, tenant_id, actor.id, currency="GBP", rate="1.17")

    async with tenant_context(app_engine, tenant_id) as session:
        repo = FxRateRepository(session)
        all_usd = await repo.list_by_currency("USD")
        windowed = await repo.list_by_currency(
            "USD", from_date=date(2025, 6, 1), to_date=date(2025, 7, 1)
        )

    assert [r.as_of_date for r in all_usd] == [
        date(2024, 12, 31),
        date(2025, 6, 30),
        date(2025, 12, 31),
    ]
    # The window is hard on both ends: no anchor row is pulled in here.
    assert [r.as_of_date for r in windowed] == [date(2025, 6, 30)]


# ---------------------------------------------------------------------------
# FX-07: load_rates_frame — the anchor row is what makes carry-forward work
# ---------------------------------------------------------------------------


async def test_fx07a_load_rates_frame_includes_the_carry_forward_anchor(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The latest row at or before ``from_date`` comes along, per currency.

    Without it, the first days of the window would have no rate at or
    before them and conversion would raise ``MissingFxRateError`` on data
    the tenant actually supplied.
    """
    tenant_id = await seed_tenant()
    actor = await _seed_actor(app_engine, tenant_id, email="fx07a@example.com")
    for day, rate in (
        (date(2025, 1, 2), "0.9000000000"),
        (date(2025, 1, 6), "0.9100000000"),
        (date(2025, 1, 10), "0.9200000000"),
    ):
        await _upsert(app_engine, tenant_id, actor.id, as_of_date=day, rate=rate)
    # GBP starts inside the window: it has no anchor to pull.
    await _upsert(
        app_engine,
        tenant_id,
        actor.id,
        currency="GBP",
        as_of_date=date(2025, 1, 9),
        rate="1.1700000000",
    )

    async with tenant_context(app_engine, tenant_id) as session:
        frame = await FxRateRepository(session).load_rates_frame(
            ["USD", "GBP"],
            from_date=date(2025, 1, 8),
            to_date=date(2025, 1, 10),
        )

    assert tuple(frame.columns) == RATES_FRAME_COLUMNS
    usd = frame[frame["currency"] == "USD"]
    gbp = frame[frame["currency"] == "GBP"]
    # 2025-01-06 is the anchor for a window starting 2025-01-08; 2025-01-02
    # is older still and stays behind.
    assert [d.date() for d in usd["as_of_date"]] == [
        date(2025, 1, 6),
        date(2025, 1, 10),
    ]
    assert [d.date() for d in gbp["as_of_date"]] == [date(2025, 1, 9)]
    # DB truth survives the hand-off to the pure service.
    assert usd["rate_to_reference"].iloc[0] == Decimal("0.9100000000")
    assert set(frame["reference_currency"]) == {"EUR"}


async def test_fx07b_load_rates_frame_upper_bound_is_hard(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed_actor(app_engine, tenant_id, email="fx07b@example.com")
    for day in (date(2025, 1, 2), date(2025, 1, 10)):
        await _upsert(app_engine, tenant_id, actor.id, as_of_date=day)

    async with tenant_context(app_engine, tenant_id) as session:
        frame = await FxRateRepository(session).load_rates_frame(["USD"], to_date=date(2025, 1, 5))

    assert [d.date() for d in frame["as_of_date"]] == [date(2025, 1, 2)]


async def test_fx07c_load_rates_frame_is_typed_when_empty(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """An EUR-only tenant gets a well-formed zero-row frame, not a surprise."""
    tenant_id = await seed_tenant()

    async with tenant_context(app_engine, tenant_id) as session:
        repo = FxRateRepository(session)
        no_currencies = await repo.load_rates_frame([])
        no_rows = await repo.load_rates_frame(["USD"])

    for frame in (no_currencies, no_rows):
        assert frame.empty
        assert tuple(frame.columns) == RATES_FRAME_COLUMNS
        assert frame["as_of_date"].dtype == "datetime64[ns]"


# ---------------------------------------------------------------------------
# FX-08: deletes
# ---------------------------------------------------------------------------


async def test_fx08_delete_and_delete_by_currency(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed_actor(app_engine, tenant_id, email="fx08@example.com")
    for day in (date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6)):
        await _upsert(app_engine, tenant_id, actor.id, as_of_date=day)
    gbp = await _upsert(app_engine, tenant_id, actor.id, currency="GBP", rate="1.17")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = FxRateRepository(session)
        assert await repo.delete(gbp.id) is True
        assert await repo.delete(gbp.id) is False  # already gone
        assert await repo.delete_by_currency("USD") == 3

    async with tenant_context(app_engine, tenant_id) as session:
        assert await FxRateRepository(session).list_by_currency("USD") == []


async def test_fx08b_get_by_id(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed_actor(app_engine, tenant_id, email="fx08b@example.com")
    seeded = await _upsert(app_engine, tenant_id, actor.id)

    async with tenant_context(app_engine, tenant_id) as session:
        repo = FxRateRepository(session)
        found = await repo.get_by_id(seeded.id)
        missing = await repo.get_by_id(actor.id)  # a UUID that is not a rate

    assert found is not None and found.id == seeded.id
    assert missing is None


# ---------------------------------------------------------------------------
# FX-09: RLS isolation, WITH CHECK, and the audit trigger
# ---------------------------------------------------------------------------


async def test_fx09a_cross_tenant_isolation(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant(name="Tenant A")
    tenant_b = await seed_tenant(name="Tenant B")
    actor_a = await _seed_actor(app_engine, tenant_a, email="fxa@example.com")
    actor_b = await _seed_actor(app_engine, tenant_b, email="fxb@example.com")

    await _upsert(app_engine, tenant_a, actor_a.id, rate="0.9200000000")
    await _upsert(app_engine, tenant_b, actor_b.id, rate="0.8100000000")

    async with tenant_context(app_engine, tenant_a) as session:
        repo = FxRateRepository(session)
        rows = await repo.list_by_currency("USD")
        frame = await repo.load_rates_frame(["USD"])

    # Tenant A sees exactly its own rate — two tenants may legitimately hold
    # different rates for the same (currency, date) pair (ADR-0099 §2).
    assert [r.rate_to_reference for r in rows] == [Decimal("0.9200000000")]
    assert list(frame["rate_to_reference"]) == [Decimal("0.9200000000")]


async def test_fx09b_with_check_rejects_foreign_tenant(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A raw INSERT carrying a foreign ``tenant_id`` is rejected at the DB."""
    tenant_a = await seed_tenant(name="A")
    tenant_b = await seed_tenant(name="B")
    actor_a = await _seed_actor(app_engine, tenant_a, email="a@example.com")

    with pytest.raises(ProgrammingError):
        async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
            await session.execute(
                text(
                    "INSERT INTO fx_rates (tenant_id, as_of_date, currency, "
                    "rate_to_reference, reference_currency, source, "
                    "ingest_origin, created_by) "
                    "VALUES (:tid_b, '2025-12-31', 'USD', 0.92, 'EUR', "
                    "'ecb', 'excel', :uid)"
                ),
                {"tid_b": str(tenant_b), "uid": str(actor_a.id)},
            )


async def test_fx09c_audit_log_captures_fx_rate_insert(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """FX rates are a valuation input, so they carry the audit trigger."""
    tenant_id = await seed_tenant()
    actor = await _seed_actor(app_engine, tenant_id, email="fx09c@example.com")
    rate = await _upsert(app_engine, tenant_id, actor.id)

    async with tenant_context(app_engine, tenant_id) as session:
        result = await session.execute(
            text(
                """
                SELECT tenant_id, user_id, table_name, operation, record_id
                FROM audit_log
                WHERE table_name = 'fx_rates'
                  AND record_id  = :rid
                """
            ),
            {"rid": str(rate.id)},
        )
        row = result.mappings().one()

    assert row["tenant_id"] == tenant_id
    assert row["user_id"] == actor.id
    assert row["operation"] == "INSERT"
    assert row["record_id"] == rate.id
