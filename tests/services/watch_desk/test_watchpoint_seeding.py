# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Default-watchpoint seeding against the live compose Postgres (ADR-0116 §8).

The installer runs at provisioning time — before any book exists — and
again after the first workbook import, so both the "nothing to derive
yet" case and the "derive it now" case are real operating states rather
than edge cases.

* SEED-01: a bookless tenant gets the two singletons and nothing else.
  No ``fx`` pair is derivable, and ``price`` is never seeded for an
  ordinary tenant.
* SEED-02: with a book, one ``fx`` watchpoint per non-functional
  currency, quoted against the functional currency.
* SEED-03: the demo tenant additionally gets one ``price`` watchpoint per
  market-identified instrument — ten of them for the demo book — and
  nothing for the private-markets funds that carry no identifier.
* SEED-04: idempotency. A second run creates nothing, and a threshold
  someone revised in between survives it.
* SEED-05: the demo-tenant rule itself is the Primary Tenant and nobody
  else.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    InvestmentRepository,
    UserRepository,
    WatchpointRepository,
    tenant_context,
)
from core.repositories.investment_identifier_repository import (
    InvestmentIdentifierRepository,
)
from core.tenant_constants import PRIMARY_TENANT_ID, SYSTEM_TENANT_ID
from services.watch_desk.seeding import (
    FRESHNESS_SUBJECT_KEY,
    LIQUIDITY_SUBJECT_KEY,
    install_default_watchpoints,
    seeds_price_watchpoints,
)

_NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
_LATER = _NOW + timedelta(days=1)

#: The demo book's shape: ten market-identified ETFs (three of them USD)
#: plus five private-markets funds that carry no identifier at all.
_ETFS: tuple[tuple[str, str, str], ...] = (
    ("Investment A", "EUR", "IE00B4L5Y983"),
    ("Investment B", "USD", "IE00B53SZB19"),
    ("Investment C", "USD", "IE00BKM4GZ66"),
    ("Investment H", "EUR", "IE00BQN1K901"),
    ("Investment I", "EUR", "IE00B4WXJJ64"),
    ("Investment J", "USD", "IE00BSKRJZ44"),
    ("Investment K", "EUR", "IE00B3F81R35"),
    ("Investment L", "USD", "IE00B7J7TB45"),
    ("Investment M", "EUR", "IE00B66F4759"),
    ("Investment T", "EUR", "LU0290358497"),
)
_FUNDS: tuple[tuple[str, str], ...] = (
    ("Investment D", "EUR"),
    ("Investment E", "EUR"),
    ("Investment F", "EUR"),
    ("Investment G", "EUR"),
    ("Investment N", "EUR"),
)


async def _seed_owner(app_engine: AsyncEngine, tenant_id: UUID) -> UUID:
    async with tenant_context(app_engine, tenant_id) as session:
        user = await UserRepository(session).create(
            email=f"owner-{uuid4().hex[:8]}@example.test", password_hash="x" * 8
        )
    return user.id


async def _seed_book(app_engine: AsyncEngine, tenant_id: UUID, owner_id: UUID) -> None:
    """Create the demo-shaped book: ten identified ETFs, five bare funds."""
    async with tenant_context(app_engine, tenant_id, user_id=owner_id) as session:
        asset_class = await AssetClassRepository(session).create(
            code="equities", display_name="Equities"
        )
        investments = InvestmentRepository(session)
        identifiers = InvestmentIdentifierRepository(session)

        for name, currency, isin in _ETFS:
            investment = await investments.create(
                name=name,
                investment_type="listed_equity",
                asset_class_id=asset_class.id,
                currency=currency,
                created_by=owner_id,
            )
            await identifiers.add(
                investment_id=investment.id,
                scheme="isin",
                value=isin,
                created_by=owner_id,
                is_primary=True,
            )
        for name, currency in _FUNDS:
            await investments.create(
                name=name,
                investment_type="private_equity",
                asset_class_id=asset_class.id,
                currency=currency,
                created_by=owner_id,
            )


async def _install(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    owner_id: UUID,
    *,
    now: datetime = _NOW,
    seed_price_watchpoints: bool = False,
) -> int:
    async with tenant_context(app_engine, tenant_id, user_id=owner_id) as session:
        return await install_default_watchpoints(
            WatchpointRepository(session),
            InvestmentRepository(session),
            InvestmentIdentifierRepository(session),
            functional_currency="EUR",
            now=now,
            seed_price_watchpoints=seed_price_watchpoints,
        )


async def _installed(app_engine: AsyncEngine, tenant_id: UUID, at: datetime = _LATER):
    async with tenant_context(app_engine, tenant_id) as session:
        return await WatchpointRepository(session).effective_watchpoints(at)


# ---------------------------------------------------------------------------
# SEED-01 / SEED-02
# ---------------------------------------------------------------------------


async def test_seed01_a_bookless_tenant_gets_the_two_singletons_only(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("SEED-01")
    owner_id = await _seed_owner(app_engine, tenant_id)

    assert await _install(app_engine, tenant_id, owner_id) == 2

    rows = await _installed(app_engine, tenant_id)
    assert {row.family for row in rows} == {"freshness", "liquidity"}

    freshness = next(row for row in rows if row.family == "freshness")
    liquidity = next(row for row in rows if row.family == "liquidity")
    assert freshness.subject_key == FRESHNESS_SUBJECT_KEY
    assert freshness.max_age_days == 120
    assert liquidity.subject_key == LIQUIDITY_SUBJECT_KEY
    assert liquidity.horizon_months == 12
    assert liquidity.min_coverage_ratio == Decimal("1.2000")


async def test_seed02_one_fx_watchpoint_per_non_functional_currency(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("SEED-02")
    owner_id = await _seed_owner(app_engine, tenant_id)
    await _seed_book(app_engine, tenant_id, owner_id)

    assert await _install(app_engine, tenant_id, owner_id) == 3

    rows = await _installed(app_engine, tenant_id)
    fx_rows = [row for row in rows if row.family == "fx"]
    assert [row.currency_pair for row in fx_rows] == ["USD/EUR"], (
        "EUR is the functional currency, so only USD yields a pair — and it is "
        "quoted against the functional currency, the direction the conversion "
        "boundary uses"
    )
    assert fx_rows[0].subject_key == "fx:USD/EUR"
    assert fx_rows[0].move_pct == Decimal("3.0000")
    assert fx_rows[0].window_days == 5
    assert not [row for row in rows if row.family == "price"], (
        "price watchpoints are not seeded for an ordinary tenant"
    )


# ---------------------------------------------------------------------------
# SEED-03: the demo tenant
# ---------------------------------------------------------------------------


async def test_seed03_the_demo_tenant_gets_a_price_watchpoint_per_identified_instrument(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("SEED-03")
    owner_id = await _seed_owner(app_engine, tenant_id)
    await _seed_book(app_engine, tenant_id, owner_id)

    created = await _install(app_engine, tenant_id, owner_id, seed_price_watchpoints=True)
    assert created == 13, "2 singletons + 1 fx pair + 10 price watchpoints"

    rows = await _installed(app_engine, tenant_id)
    price_rows = [row for row in rows if row.family == "price"]
    assert len(price_rows) == 10
    assert {row.drop_pct for row in price_rows} == {Decimal("5.0000")}
    assert {row.window_days for row in price_rows} == {5}
    assert all(row.subject_key == f"price:{row.instrument_id}" for row in price_rows)

    # The five private-markets funds carry no market identifier, which is
    # exactly what keeps them out — no NULL-column ambiguity involved.
    async with tenant_context(app_engine, tenant_id) as session:
        book = await InvestmentRepository(session).list_all()
    watched = {row.instrument_id for row in price_rows}
    unwatched = {inv.name for inv in book if inv.id not in watched}
    assert unwatched == {name for name, _ in _FUNDS}


# ---------------------------------------------------------------------------
# SEED-04: idempotency
# ---------------------------------------------------------------------------


async def test_seed04_re_running_creates_nothing_and_keeps_revisions(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("SEED-04")
    owner_id = await _seed_owner(app_engine, tenant_id)
    await _seed_book(app_engine, tenant_id, owner_id)

    assert await _install(app_engine, tenant_id, owner_id, seed_price_watchpoints=True) == 13

    # An operator tightens the freshness singleton between runs.
    async with tenant_context(app_engine, tenant_id, user_id=owner_id) as session:
        repository = WatchpointRepository(session)
        freshness = next(
            row for row in await repository.effective_watchpoints(_NOW) if row.family == "freshness"
        )
        await repository.revise(
            freshness.watchpoint_id,
            effective_from=_LATER,
            display_name="NAV freshness (tightened)",
            max_age_days=45,
        )

    assert (
        await _install(app_engine, tenant_id, owner_id, now=_LATER, seed_price_watchpoints=True)
        == 0
    ), "a second run must create nothing"

    rows = await _installed(app_engine, tenant_id, at=_LATER + timedelta(days=1))
    assert len(rows) == 13
    freshness = next(row for row in rows if row.family == "freshness")
    assert freshness.max_age_days == 45, "the operator's revision survives a re-seed"


async def test_seed04_a_renamed_watchpoint_is_recognised_not_duplicated(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Idempotency matches on the subject, not on the seeded label or key.

    An operator who created the freshness singleton by hand under another
    name still has one; seeding a second would hit the singleton rule and
    abort the whole step. The same reasoning applies to a pair someone
    added before the seeder got to it.
    """
    tenant_id = await seed_tenant("SEED-04c")
    owner_id = await _seed_owner(app_engine, tenant_id)
    await _seed_book(app_engine, tenant_id, owner_id)

    async with tenant_context(app_engine, tenant_id, user_id=owner_id) as session:
        repository = WatchpointRepository(session)
        await repository.create(
            family="freshness",
            subject_key="freshness:everything",
            display_name="Hand-made freshness watch",
            effective_from=_NOW,
            max_age_days=200,
        )
        await repository.create(
            family="fx",
            subject_key="fx:dollar-euro",
            display_name="Hand-made USD watch",
            effective_from=_NOW,
            currency_pair="USD/EUR",
            move_pct=Decimal("1.0"),
            window_days=10,
        )

    assert await _install(app_engine, tenant_id, owner_id, now=_LATER) == 1, (
        "only the liquidity singleton is missing"
    )

    rows = await _installed(app_engine, tenant_id)
    assert len([row for row in rows if row.family == "freshness"]) == 1
    fx_rows = [row for row in rows if row.family == "fx"]
    assert [row.move_pct for row in fx_rows] == [Decimal("1.0000")], (
        "the operator's own threshold is left alone"
    )


async def test_seed04_the_second_run_adds_only_what_the_book_made_derivable(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The real operating sequence: provision, import, re-seed."""
    tenant_id = await seed_tenant("SEED-04b")
    owner_id = await _seed_owner(app_engine, tenant_id)

    assert await _install(app_engine, tenant_id, owner_id, seed_price_watchpoints=True) == 2

    await _seed_book(app_engine, tenant_id, owner_id)

    assert (
        await _install(app_engine, tenant_id, owner_id, now=_LATER, seed_price_watchpoints=True)
        == 11
    ), "1 fx pair + 10 price watchpoints; the singletons are already there"
    assert len(await _installed(app_engine, tenant_id, at=_LATER)) == 13


# ---------------------------------------------------------------------------
# SEED-05: who counts as the demo tenant
# ---------------------------------------------------------------------------


async def test_seed06_a_deactivated_position_is_not_watched(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The book is the *active* universe, as it is everywhere else.

    A deactivated USD position must not resurrect a currency pair, and a
    deactivated ETF must not get a price watchpoint — the same
    ``list_active`` reading that AUM, coverage and the chart services use.
    """
    tenant_id = await seed_tenant("SEED-06")
    owner_id = await _seed_owner(app_engine, tenant_id)

    async with tenant_context(app_engine, tenant_id, user_id=owner_id) as session:
        asset_class = await AssetClassRepository(session).create(
            code="equities", display_name="Equities"
        )
        investments = InvestmentRepository(session)
        identifiers = InvestmentIdentifierRepository(session)
        sold = await investments.create(
            name="Sold ETF",
            investment_type="listed_equity",
            asset_class_id=asset_class.id,
            currency="USD",
            created_by=owner_id,
        )
        await identifiers.add(
            investment_id=sold.id,
            scheme="isin",
            value="IE00B4L5Y983",
            created_by=owner_id,
        )
        await investments.set_active(sold.id, False)

    assert await _install(app_engine, tenant_id, owner_id, seed_price_watchpoints=True) == 2, (
        "the singletons only — a sold position contributes neither a pair nor a price watchpoint"
    )
    rows = await _installed(app_engine, tenant_id)
    assert {row.family for row in rows} == {"freshness", "liquidity"}


def test_seed05_only_the_primary_tenant_is_seeded_with_price_watchpoints() -> None:
    assert seeds_price_watchpoints(PRIMARY_TENANT_ID) is True
    assert seeds_price_watchpoints(SYSTEM_TENANT_ID) is False
    assert seeds_price_watchpoints(uuid4()) is False
