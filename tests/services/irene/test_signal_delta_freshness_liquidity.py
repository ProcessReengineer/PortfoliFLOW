# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Live-DB tests for the ``freshness`` and ``liquidity`` families (ADR-0116 §4).

The book-shaped half of the signal families. Where ``price`` and ``fx``
watch a series the platform imports, these two watch the platform's own
data: whether the NAVs are current, and whether the cash covers what the
plan world has promised to pay. ``test_nav_freshness.py`` and
``test_cash_coverage_watch.py`` pin the pure measurements; these pin the
fetch, the enumeration, the singleton's settings, the floors, and the
wording.

They run against the compose Postgres through the shared ``app_engine`` /
``superuser_engine`` / ``seed_tenant`` fixtures. No limit set is seeded:
both families are book-wide and need no coverage bundle.

The claims that carry weight here:

* **One row, many subjects.** A ``freshness`` watchpoint enumerates
  *exactly* the active book — every active investment, nothing
  deactivated, and nothing that is not an investment at all.
* **The singleton's settings reach every subject it enumerated.** Muting
  the one row silences the family; a WARN override on it moves every
  subject's Approaching band. Both go through the wildcard fallback, and
  neither would work if the enumerated key were looked up literally.
* **Approaching is reachable for both families.** This is the recorded
  deviation's whole purpose (see the two producers' module docstrings);
  under ADR-0116 §4's literal magnitudes both bands would be dead, and
  these are the tests that would catch a silent regression to that.
* **The freshness cap holds.** A stale NAV is a data-quality problem: its
  finding floors at 3 and is capped at 5, so it can never outrank a
  breach however long the staleness runs.
* **Silence is silence.** An investment with no NAV, a book with no
  forward plan path — both log and write no watch-state row, so no
  acknowledged state is reset by the absence of data.
* **No deterministic string either family carries says "breach".**
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    InvestmentCashflowRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    tenant_context,
)
from core.repositories.irene_finding_repository import IreneFindingRepository
from core.repositories.irene_watch_state_repository import IreneWatchStateRepository
from core.repositories.watchpoint_repository import WatchpointRepository
from services.ai_service_core import ResolvedLLM
from services.irene.beat import run_beat
from services.irene.signal_delta import SignalEligibleFinding, evaluate_signal_deltas
from tests.services.irene._book_fixtures import D, SurfacingCore, resolution, seed_user

_TEST_LLM = ResolvedLLM(
    base_url="https://openrouter.test/api/v1",
    api_key="sk-signal-test",
    model="test-model",
)

#: The v1 seeded defaults (ADR-0116 §8), used unchanged unless a test is
#: specifically about moving one.
_MAX_AGE_DAYS = 120
_HORIZON_MONTHS = 12
_MIN_RATIO = D("1.2")

#: 90% of the age limit — the last age that still reads Calm.
_WARN_AGE = 108

_FUND_NAME = "Alpha Buyout Fund IV"
_CASH_NAME = "Cash EUR"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _days_ago(days: int) -> date:
    return _now().date() - timedelta(days=days)


def _months_ahead(months: int) -> datetime:
    """A plan date comfortably inside a twelve-month horizon."""
    return _now() + timedelta(days=30 * months)


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def _seed_investment(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    *,
    name: str,
    class_code: str,
    investment_type: str = "private_equity",
    currency: str = "EUR",
    is_active: bool = True,
) -> UUID:
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        asset_class = await AssetClassRepository(session).create(
            code=class_code, display_name=class_code.title()
        )
        investments = InvestmentRepository(session)
        created = await investments.create(
            name=name,
            investment_type=investment_type,
            asset_class_id=asset_class.id,
            currency=currency,
            created_by=user_id,
        )
        if not is_active:
            await investments.set_active(created.id, False)
        return created.id


async def _seed_nav(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    investment_id: UUID,
    *,
    as_of_date: date,
    value: str,
    nav_kind: str = "actual",
    currency: str = "EUR",
    ingest_origin: str = "excel",
) -> None:
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        await InvestmentNavRepository(session).upsert(
            investment_id=investment_id,
            as_of_date=as_of_date,
            nav_kind=nav_kind,
            nav_value=D(value),
            currency=currency,
            source=None,
            created_by=user_id,
            ingest_origin=ingest_origin,
        )


async def _seed_plan_flow(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    investment_id: UUID,
    *,
    amount: str,
    flow_type: str = "capital_call",
    when: datetime | None = None,
    currency: str = "EUR",
) -> None:
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        await InvestmentCashflowRepository(session).create(
            investment_id=investment_id,
            flow_timestamp=when or _months_ahead(3),
            flow_type=flow_type,
            flow_kind="plan",
            amount=D(amount),
            currency=currency,
            description=None,
            created_by=user_id,
        )


async def _freshness_watchpoint(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    *,
    max_age_days: int = _MAX_AGE_DAYS,
    muted: bool = False,
    warn_threshold_pct: Decimal | None = None,
) -> UUID:
    """Create the tenant's one ``freshness`` singleton, effective an hour ago."""
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        created = await WatchpointRepository(session).create(
            family="freshness",
            subject_key="freshness:*",
            display_name="NAV freshness (all investments)",
            effective_from=_now() - timedelta(hours=1),
            muted=muted,
            warn_threshold_pct=warn_threshold_pct,
            max_age_days=max_age_days,
        )
        return created.watchpoint_id


async def _liquidity_watchpoint(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    *,
    horizon_months: int = _HORIZON_MONTHS,
    min_coverage_ratio: Decimal = _MIN_RATIO,
) -> UUID:
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        created = await WatchpointRepository(session).create(
            family="liquidity",
            subject_key="liquidity:cash_coverage",
            display_name="Cash coverage of projected calls",
            effective_from=_now() - timedelta(hours=1),
            horizon_months=horizon_months,
            min_coverage_ratio=min_coverage_ratio,
        )
        return created.watchpoint_id


async def _evaluate(app_engine: AsyncEngine, tenant_id: UUID) -> list[SignalEligibleFinding]:
    async with tenant_context(app_engine, tenant_id) as session:
        return await evaluate_signal_deltas(
            session, now=_now(), resolution=await resolution(session)
        )


async def _watch_state(app_engine: AsyncEngine, tenant_id: UUID, subject_key: str):
    async with tenant_context(app_engine, tenant_id) as session:
        return await IreneWatchStateRepository(session).get_by_subject(subject_key)


async def _stale_book(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    tenant_id: UUID,
    *,
    age_days: int = 134,
) -> tuple[UUID, UUID, str]:
    """One active fund whose newest actual NAV is ``age_days`` old."""
    user_id = await seed_user(superuser_engine, tenant_id)
    investment_id = await _seed_investment(
        app_engine, tenant_id, user_id, name=_FUND_NAME, class_code="private_equity"
    )
    await _seed_nav(
        app_engine,
        tenant_id,
        user_id,
        investment_id,
        as_of_date=_days_ago(age_days),
        value="1250000.00",
    )
    await _freshness_watchpoint(app_engine, tenant_id, user_id)
    return user_id, investment_id, f"freshness:{investment_id}"


async def _covered_book(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    tenant_id: UUID,
    *,
    balance: str,
    call_amount: str = "-1000000",
) -> tuple[UUID, UUID]:
    """A cash position with a forward plan path and one projected call.

    The fund carries the call; the cash position carries the balance and
    the materialised plan row that evidences a projection at all.
    """
    user_id = await seed_user(superuser_engine, tenant_id)
    fund_id = await _seed_investment(
        app_engine, tenant_id, user_id, name=_FUND_NAME, class_code="private_equity"
    )
    cash_id = await _seed_investment(
        app_engine,
        tenant_id,
        user_id,
        name=_CASH_NAME,
        class_code="cash",
        investment_type="cash",
    )
    await _seed_nav(app_engine, tenant_id, user_id, cash_id, as_of_date=_days_ago(1), value=balance)
    await _seed_plan_flow(app_engine, tenant_id, user_id, fund_id, amount=call_amount)
    # The materialised cash plan path, as ADR-0103 §6 writes it. Read here,
    # never recomputed: its presence is what makes the horizon answerable.
    await _seed_nav(
        app_engine,
        tenant_id,
        user_id,
        cash_id,
        as_of_date=_months_ahead(3).date(),
        value=str(D(balance) + D(call_amount)),
        nav_kind="plan",
        ingest_origin="system",
    )
    await _liquidity_watchpoint(app_engine, tenant_id, user_id)
    return user_id, cash_id


# ---------------------------------------------------------------------------
# freshness — one row, many subjects
# ---------------------------------------------------------------------------


async def test_a_stale_nav_is_a_rising_edge_naming_the_investment(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """The whole pipeline in one pass, and the note a human would read."""
    tenant_id = await seed_tenant("sig-fresh-rise")
    _, _, subject_key = await _stale_book(app_engine, superuser_engine, tenant_id)

    eligible = await _evaluate(app_engine, tenant_id)

    assert len(eligible) == 1
    finding = eligible[0]
    assert finding.subject_key == subject_key
    assert finding.family == "freshness"
    assert finding.kind == "rising_edge"
    assert finding.status_label == "Triggered"
    assert finding.magnitude == D(134)
    assert finding.threshold_pct == D(_MAX_AGE_DAYS)
    # The note names the investment and the age — the two things a reader
    # would otherwise have to open the book for.
    assert finding.note == (
        f"NAV for {_FUND_NAME} is 134 days old — freshness watchpoint triggered (limit 120 days)."
    )

    state = await _watch_state(app_engine, tenant_id, subject_key)
    assert state is not None
    assert state.magnitude == D(134)
    assert state.acknowledged_magnitude == D(134)

    # Edge-triggered, not level-triggered. The NAV is a day older on the
    # next beat, but 1 day is under the family's 5.0-day re-trigger delta.
    assert await _evaluate(app_engine, tenant_id) == []


async def test_freshness_enumerates_exactly_the_active_book(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """One watchpoint, one subject per active investment — and no others.

    The deactivated position is the load-bearing half: ``list_active()`` is
    what AUM, coverage and the charts mean by "the book", and a position
    nobody holds any more is not something to complain about the staleness
    of.
    """
    tenant_id = await seed_tenant("sig-fresh-enumerate")
    user_id = await seed_user(superuser_engine, tenant_id)
    watched = [
        await _seed_investment(
            app_engine, tenant_id, user_id, name="Alpha", class_code="private_equity"
        ),
        await _seed_investment(
            app_engine, tenant_id, user_id, name="Beta", class_code="real_estate"
        ),
    ]
    retired = await _seed_investment(
        app_engine,
        tenant_id,
        user_id,
        name="Gamma (exited)",
        class_code="infra_equity",
        is_active=False,
    )
    for investment_id in (*watched, retired):
        await _seed_nav(
            app_engine,
            tenant_id,
            user_id,
            investment_id,
            as_of_date=_days_ago(200),
            value="1000000",
        )
    await _freshness_watchpoint(app_engine, tenant_id, user_id)

    eligible = await _evaluate(app_engine, tenant_id)

    assert {finding.subject_key for finding in eligible} == {
        f"freshness:{investment_id}" for investment_id in watched
    }
    assert await _watch_state(app_engine, tenant_id, f"freshness:{retired}") is None


async def test_the_freshness_approaching_band_is_reachable(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """The recorded deviation, end to end.

    Under ADR-0116 §4's literal magnitude ("days *over* the limit") a
    109-day-old NAV would score zero and this beat would be silent. The
    family measures the age instead, so the band between the warn fraction
    and the limit is a real range with a real edge in it.
    """
    tenant_id = await seed_tenant("sig-fresh-warn")
    _, _, subject_key = await _stale_book(
        app_engine, superuser_engine, tenant_id, age_days=_WARN_AGE + 1
    )

    eligible = await _evaluate(app_engine, tenant_id)

    assert len(eligible) == 1
    assert eligible[0].subject_key == subject_key
    assert eligible[0].status == "WARN"
    assert eligible[0].status_label == "Approaching"
    assert eligible[0].magnitude == D(_WARN_AGE + 1)
    assert "approaching the freshness watchpoint limit of 120 days" in eligible[0].note


async def test_a_nav_at_the_warn_fraction_is_still_calm(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """The band's near edge, so "reachable" is not confused with "always"."""
    tenant_id = await seed_tenant("sig-fresh-calm")
    await _stale_book(app_engine, superuser_engine, tenant_id, age_days=_WARN_AGE)

    assert await _evaluate(app_engine, tenant_id) == []


async def test_an_investment_with_no_nav_writes_no_watch_state_row(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An age since nothing is a guess; the subject is logged, not scored."""
    tenant_id = await seed_tenant("sig-fresh-nonav")
    user_id = await seed_user(superuser_engine, tenant_id)
    investment_id = await _seed_investment(
        app_engine, tenant_id, user_id, name="Freshly created", class_code="private_debt"
    )
    await _freshness_watchpoint(app_engine, tenant_id, user_id)

    with caplog.at_level(logging.INFO, logger="services.irene.signal_delta"):
        assert await _evaluate(app_engine, tenant_id) == []

    assert await _watch_state(app_engine, tenant_id, f"freshness:{investment_id}") is None
    assert any("cannot be evaluated" in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------------
# freshness — the singleton's settings reach the subjects it enumerated
# ---------------------------------------------------------------------------


async def test_muting_the_singleton_silences_every_enumerated_subject(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """The wildcard fallback, from the operator's point of view.

    The mute is set on ``freshness:*``; the subjects are
    ``freshness:{investment_id}``. A literal lookup would find nothing and
    the mute would silently do nothing at all — which is the failure this
    test exists to catch. The watch-state rows still advance: mute
    suppresses findings, never observations (ADR-0116 §3).
    """
    tenant_id = await seed_tenant("sig-fresh-muted")
    user_id = await seed_user(superuser_engine, tenant_id)
    investments = [
        await _seed_investment(
            app_engine, tenant_id, user_id, name="Alpha", class_code="private_equity"
        ),
        await _seed_investment(
            app_engine, tenant_id, user_id, name="Beta", class_code="real_estate"
        ),
    ]
    for investment_id in investments:
        await _seed_nav(
            app_engine,
            tenant_id,
            user_id,
            investment_id,
            as_of_date=_days_ago(200),
            value="1000000",
        )
    await _freshness_watchpoint(app_engine, tenant_id, user_id, muted=True)

    assert await _evaluate(app_engine, tenant_id) == []

    for investment_id in investments:
        state = await _watch_state(app_engine, tenant_id, f"freshness:{investment_id}")
        assert state is not None
        assert state.magnitude == D(200)
        assert state.acknowledged_magnitude == D(200)


async def test_the_closing_all_clear_of_a_muted_freshness_subject_still_fires(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """Never strand a raised card — the family-agnostic half of the rule.

    The subject is muted throughout. Its trigger is withheld; when a fresh
    statement arrives the all-clear passes anyway, because the
    acknowledged state records the level and a card raised for that level
    on an earlier beat must be closeable.
    """
    tenant_id = await seed_tenant("sig-fresh-muted-clear")
    user_id = await seed_user(superuser_engine, tenant_id)
    investment_id = await _seed_investment(
        app_engine, tenant_id, user_id, name=_FUND_NAME, class_code="private_equity"
    )
    await _seed_nav(
        app_engine,
        tenant_id,
        user_id,
        investment_id,
        as_of_date=_days_ago(200),
        value="1000000",
    )
    await _freshness_watchpoint(app_engine, tenant_id, user_id, muted=True)
    subject_key = f"freshness:{investment_id}"

    assert await _evaluate(app_engine, tenant_id) == []

    # A new statement lands today.
    await _seed_nav(
        app_engine,
        tenant_id,
        user_id,
        investment_id,
        as_of_date=_days_ago(0),
        value="1100000",
    )
    eligible = await _evaluate(app_engine, tenant_id)

    assert len(eligible) == 1
    assert eligible[0].kind == "falling_edge"
    assert eligible[0].status_label == "Calm"
    assert eligible[0].magnitude == D(0)
    assert "was restated today" in eligible[0].note
    assert "eased back within its limit" in eligible[0].note

    state = await _watch_state(app_engine, tenant_id, subject_key)
    assert state is not None
    assert state.acknowledged_at is None


async def test_a_warn_override_on_the_singleton_moves_every_subjects_band(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """The same fallback, on the other sensitivity field.

    60% of 120 days is 72; a 100-day-old NAV is calm by default and
    Approaching once the tenant says it watches earlier.
    """
    tenant_id = await seed_tenant("sig-fresh-warn-override")
    user_id = await seed_user(superuser_engine, tenant_id)
    investment_id = await _seed_investment(
        app_engine, tenant_id, user_id, name=_FUND_NAME, class_code="private_equity"
    )
    await _seed_nav(
        app_engine,
        tenant_id,
        user_id,
        investment_id,
        as_of_date=_days_ago(100),
        value="1000000",
    )
    await _freshness_watchpoint(app_engine, tenant_id, user_id, warn_threshold_pct=D("60"))

    eligible = await _evaluate(app_engine, tenant_id)

    assert len(eligible) == 1
    assert eligible[0].subject_key == f"freshness:{investment_id}"
    assert eligible[0].status_label == "Approaching"


# ---------------------------------------------------------------------------
# liquidity — one subject, two sides of a ratio
# ---------------------------------------------------------------------------


async def test_a_book_at_its_coverage_floor_is_a_rising_edge_in_ratio_language(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """The note speaks in ratios; the 100-scale never reaches a sentence."""
    tenant_id = await seed_tenant("sig-liq-rise")
    await _covered_book(app_engine, superuser_engine, tenant_id, balance="1200000")

    eligible = await _evaluate(app_engine, tenant_id)

    assert len(eligible) == 1
    finding = eligible[0]
    assert finding.subject_key == "liquidity:cash_coverage"
    assert finding.family == "liquidity"
    assert finding.kind == "rising_edge"
    assert finding.status_label == "Triggered"
    assert finding.magnitude == D("100")
    assert finding.threshold_pct == D("100")
    assert finding.note == (
        "cash covers projected calls 1.20× over 12 months — below your 1.20× floor."
    )
    # The internal scale is arithmetic, not communication.
    assert "100" not in finding.note

    state = await _watch_state(app_engine, tenant_id, "liquidity:cash_coverage")
    assert state is not None
    assert state.acknowledged_magnitude == D("100")


async def test_the_liquidity_approaching_band_is_reachable(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """The recorded deviation, end to end, for the second family.

    A 1.30× book is above its 1.20× floor and would score zero under the
    ADR's literal "shortfall below the floor" magnitude. On the 100-scale
    it is 92.3% of the way down, which is an Approaching worth saying.
    """
    tenant_id = await seed_tenant("sig-liq-warn")
    await _covered_book(app_engine, superuser_engine, tenant_id, balance="1300000")

    eligible = await _evaluate(app_engine, tenant_id)

    assert len(eligible) == 1
    assert eligible[0].status == "WARN"
    assert eligible[0].status_label == "Approaching"
    assert eligible[0].magnitude == D("92.3077")
    assert eligible[0].note == (
        "cash covers projected calls 1.30× over 12 months — approaching your 1.20× floor."
    )


async def test_a_comfortable_book_produces_no_finding(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """Calm is the ordinary outcome, and it is silent."""
    tenant_id = await seed_tenant("sig-liq-calm")
    await _covered_book(app_engine, superuser_engine, tenant_id, balance="3000000")

    assert await _evaluate(app_engine, tenant_id) == []


async def test_only_capital_calls_reach_the_coverage_denominator(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """A projected distribution is not a promise the book has to fund.

    The book sits exactly at its floor on the call alone; a distribution
    twice its size, projected inside the same horizon, must not talk it
    back into calm.
    """
    tenant_id = await seed_tenant("sig-liq-flow-types")
    user_id, _ = await _covered_book(app_engine, superuser_engine, tenant_id, balance="1200000")
    async with tenant_context(app_engine, tenant_id) as session:
        fund = next(
            inv
            for inv in await InvestmentRepository(session).list_active()
            if inv.name == _FUND_NAME
        )
    await _seed_plan_flow(
        app_engine,
        tenant_id,
        user_id,
        fund.id,
        amount="2000000",
        flow_type="distribution",
    )

    eligible = await _evaluate(app_engine, tenant_id)

    assert len(eligible) == 1
    assert eligible[0].magnitude == D("100")


async def test_a_book_with_no_forward_plan_path_writes_no_watch_state_row(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Absence of a projection is shown, never guessed over (ADR-0116 §4).

    Everything else is in place — a cash position, a balance, a projected
    call — but nothing has materialised a forward path, so the platform
    holds no forward view of the balance and says so.
    """
    tenant_id = await seed_tenant("sig-liq-noplan")
    user_id = await seed_user(superuser_engine, tenant_id)
    fund_id = await _seed_investment(
        app_engine, tenant_id, user_id, name=_FUND_NAME, class_code="private_equity"
    )
    cash_id = await _seed_investment(
        app_engine,
        tenant_id,
        user_id,
        name=_CASH_NAME,
        class_code="cash",
        investment_type="cash",
    )
    await _seed_nav(
        app_engine, tenant_id, user_id, cash_id, as_of_date=_days_ago(1), value="100000"
    )
    await _seed_plan_flow(app_engine, tenant_id, user_id, fund_id, amount="-1000000")
    await _liquidity_watchpoint(app_engine, tenant_id, user_id)

    with caplog.at_level(logging.INFO, logger="services.irene.signal_delta"):
        assert await _evaluate(app_engine, tenant_id) == []

    assert await _watch_state(app_engine, tenant_id, "liquidity:cash_coverage") is None
    assert any("no materialised cash plan path" in record.getMessage() for record in caplog.records)


async def test_a_book_with_no_explicit_cash_position_reports_no_observation(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """Without a cash position the platform does not know the balance.

    ADR-0103 retired the residual that used to guess it, so there is
    nothing left to infer from — and inferring is exactly what the family
    must not do.
    """
    tenant_id = await seed_tenant("sig-liq-nocash")
    user_id = await seed_user(superuser_engine, tenant_id)
    fund_id = await _seed_investment(
        app_engine, tenant_id, user_id, name=_FUND_NAME, class_code="private_equity"
    )
    await _seed_nav(
        app_engine, tenant_id, user_id, fund_id, as_of_date=_days_ago(1), value="5000000"
    )
    await _seed_plan_flow(app_engine, tenant_id, user_id, fund_id, amount="-1000000")
    await _liquidity_watchpoint(app_engine, tenant_id, user_id)

    assert await _evaluate(app_engine, tenant_id) == []
    assert await _watch_state(app_engine, tenant_id, "liquidity:cash_coverage") is None


# ---------------------------------------------------------------------------
# Wording (ADR-0116 §4) — asserted, not eyeballed
# ---------------------------------------------------------------------------


async def test_no_deterministic_string_of_either_family_says_breach(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """ "Breach" is regulatory language, reserved for the quota families.

    Both families are walked across every kind their book can reach in one
    pass — a rising edge each, a ``liquidity`` re-trigger, and the
    all-clear that closes each of them — and every deterministic string is
    checked. The internal status field is exempt by design: it is the edge
    machinery's vocabulary and never reaches a human.
    """
    tenant_id = await seed_tenant("sig-book-wording")
    user_id, investment_id, _ = await _stale_book(app_engine, superuser_engine, tenant_id)
    cash_id = await _seed_investment(
        app_engine,
        tenant_id,
        user_id,
        name=_CASH_NAME,
        class_code="cash",
        investment_type="cash",
    )
    await _seed_nav(
        app_engine, tenant_id, user_id, cash_id, as_of_date=_days_ago(1), value="1200000"
    )
    await _seed_plan_flow(app_engine, tenant_id, user_id, investment_id, amount="-1000000")
    await _seed_nav(
        app_engine,
        tenant_id,
        user_id,
        cash_id,
        as_of_date=_months_ahead(3).date(),
        value="200000",
        nav_kind="plan",
        ingest_origin="system",
    )
    await _liquidity_watchpoint(app_engine, tenant_id, user_id)

    # The fund's NAV is 134 days old and the book sits exactly at its
    # coverage floor: one rising edge per family. The cash position's own
    # NAV is a day old, so its freshness subject stays calm and silent.
    collected: list[SignalEligibleFinding] = list(await _evaluate(app_engine, tenant_id))
    assert len(collected) == 2

    # The balance falls to 0.90× coverage — a 33 pp deepening, well past
    # the family's 5.0 re-trigger delta.
    await _seed_nav(
        app_engine, tenant_id, user_id, cash_id, as_of_date=_days_ago(0), value="900000"
    )
    collected += await _evaluate(app_engine, tenant_id)

    # Both ease: a fresh statement for the fund, and a balance well clear
    # of the floor.
    await _seed_nav(
        app_engine,
        tenant_id,
        user_id,
        investment_id,
        as_of_date=_days_ago(0),
        value="1300000.00",
    )
    await _seed_nav(
        app_engine, tenant_id, user_id, cash_id, as_of_date=_days_ago(0), value="5000000"
    )
    collected += await _evaluate(app_engine, tenant_id)

    kinds = {(finding.family, finding.kind) for finding in collected}
    assert kinds == {
        ("freshness", "rising_edge"),
        ("liquidity", "rising_edge"),
        ("liquidity", "magnitude_retrigger"),
        ("freshness", "falling_edge"),
        ("liquidity", "falling_edge"),
    }
    for finding in collected:
        for text in (finding.note, finding.reason, finding.status_label, finding.display_name):
            assert "breach" not in text.lower(), f"{finding.family}/{finding.kind}: {text!r}"


# ---------------------------------------------------------------------------
# Through the beat: the floors, and the cap that only freshness has
# ---------------------------------------------------------------------------


async def test_a_surfaced_freshness_finding_is_floored_at_three(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """``freshness_trigger`` floors at 3 (ADR-0116 §4), so a 1 cannot persist.

    Also pins that the context Irene reads speaks the human vocabulary:
    the surest way to keep "breach" off a card is to keep it out of what
    the model was shown.
    """
    tenant_id = await seed_tenant("sig-fresh-floor")
    _, _, subject_key = await _stale_book(app_engine, superuser_engine, tenant_id)
    core = SurfacingCore(subject_key, urgency_suggestion=1)

    async with tenant_context(app_engine, tenant_id) as session:
        result = await run_beat(session, core, llm=_TEST_LLM, now=_now())
        findings = await IreneFindingRepository(session).list_open()

    assert result.error is None
    assert result.findings_written == 1
    assert findings[0].subject_key == subject_key
    assert findings[0].urgency == 3  # raised from the suggested 1 by the floor
    assert findings[0].band == "informational"

    context = core.calls[0]["context_messages"][0]["content"]
    assert subject_key in context
    assert "status=Triggered" in context
    assert "BREACH" not in context


async def test_a_freshness_finding_can_never_outrank_a_breach(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """The cap, and the only one of the four families that carries one.

    A NAV two and a half years old, with the model reaching for the top of
    the scale: ``freshness_trigger`` is capped at 5 (ADR-0116 §4), one
    below the noteworthy band's ceiling and well below the 7 a regulatory
    breach floors to. Staleness is a data-quality problem however long it
    runs, and the cap is where that judgement is enforced.
    """
    tenant_id = await seed_tenant("sig-fresh-cap")
    _, _, subject_key = await _stale_book(app_engine, superuser_engine, tenant_id, age_days=900)

    async with tenant_context(app_engine, tenant_id) as session:
        result = await run_beat(
            session,
            SurfacingCore(subject_key, urgency_suggestion=10),
            llm=_TEST_LLM,
            now=_now(),
        )
        findings = await IreneFindingRepository(session).list_open()

    assert result.findings_written == 1
    assert findings[0].urgency == 5  # capped, not the suggested 10
    assert findings[0].band == "noteworthy"


async def test_a_surfaced_liquidity_finding_is_floored_at_six(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """The highest floor of the four: a coverage shortfall has a payment date.

    Unlike ``freshness`` the family carries no cap of its own, so its
    ceiling is the source cap — an internal finding may reach the critical
    band when the model and the evidence both say so.
    """
    tenant_id = await seed_tenant("sig-liq-floor")
    await _covered_book(app_engine, superuser_engine, tenant_id, balance="1200000")

    async with tenant_context(app_engine, tenant_id) as session:
        result = await run_beat(
            session,
            SurfacingCore("liquidity:cash_coverage", urgency_suggestion=1),
            llm=_TEST_LLM,
            now=_now(),
        )
        findings = await IreneFindingRepository(session).list_open()

    assert result.error is None
    assert result.findings_written == 1
    assert findings[0].subject_key == "liquidity:cash_coverage"
    assert findings[0].urgency == 6  # raised from the suggested 1 by the floor
    # 6 is the top of the noteworthy band, not the bottom of critical: the
    # family floors highest of the four without being a rule violation.
    assert findings[0].band == "noteworthy"


async def test_an_eased_book_wide_watchpoint_is_capped_at_informational(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """An all-clear is never itself urgent, whichever family raised it.

    ``derive_trigger_type`` checks the falling edge *before* the family
    axis, so this holds for the two book-wide families with no code of
    their own — the assertion is that no branch was added.
    """
    tenant_id = await seed_tenant("sig-liq-allclear")
    user_id, cash_id = await _covered_book(
        app_engine, superuser_engine, tenant_id, balance="1200000"
    )
    assert len(await _evaluate(app_engine, tenant_id)) == 1

    await _seed_nav(
        app_engine, tenant_id, user_id, cash_id, as_of_date=_days_ago(0), value="5000000"
    )
    async with tenant_context(app_engine, tenant_id) as session:
        result = await run_beat(
            session,
            SurfacingCore("liquidity:cash_coverage", urgency_suggestion=9),
            llm=_TEST_LLM,
            now=_now(),
        )
        findings = await IreneFindingRepository(session).list_open()

    assert result.findings_written == 1
    assert findings[0].urgency == 3  # capped, not the suggested 9
    assert findings[0].band == "informational"
