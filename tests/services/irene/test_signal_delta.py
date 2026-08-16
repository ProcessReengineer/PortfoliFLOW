# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Live-DB tests for the ``price`` and ``fx`` signal families (ADR-0116 §4).

Where ``test_price_watch.py`` / ``test_fx_watch.py`` pin the pure
measurement, these pin what the beat does with it: the fetch, the
watch-state pipeline the two families ride, the mute rule as ADR-0116 §3
scopes it, and the wording ADR-0116 §4 fixes.

They run against the compose Postgres through the shared ``app_engine`` /
``superuser_engine`` / ``seed_tenant`` fixtures. No limit set is seeded:
the signal families need no coverage bundle, and a tenant that watches a
price without configuring a single limit is a legitimate — indeed
likely — state of the world.

The claims that carry weight here:

* a triggered ``price`` subject **can** be muted (unlike a quota breach),
  because the operator set the threshold themselves and no regulatory
  floor stands behind it;
* the closing all-clear of a muted, previously raised subject still fires,
  because the alternative is a stranded card — that half of the mute rule
  is family-agnostic;
* retirement stops evaluation, and missing data writes no watch-state row;
* an ``fx`` watchpoint is served in **its own** orientation, derived from
  the leg the database actually stores;
* no deterministic string a signal finding carries says "breach".
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
    FxRateRepository,
    InstrumentPriceRepository,
    InvestmentRepository,
    tenant_context,
)
from core.repositories.irene_finding_repository import IreneFindingRepository
from core.repositories.irene_watch_state_repository import IreneWatchStateRepository
from core.repositories.watchpoint_repository import WatchpointRepository
from services.ai_service_core import ResolvedLLM
from services.irene.beat import run_beat
from services.irene.signal_delta import (
    SignalEligibleFinding,
    evaluate_signal_deltas,
)
from tests.services.irene._book_fixtures import D, SurfacingCore, resolution, seed_user

_TEST_LLM = ResolvedLLM(
    base_url="https://openrouter.test/api/v1",
    api_key="sk-signal-test",
    model="test-model",
)

#: The v1 seeded defaults (ADR-0116 §8), used unchanged unless a test is
#: specifically about moving one.
_DROP_PCT = D("5.0")
_MOVE_PCT = D("3.0")
_WINDOW_DAYS = 5

_INSTRUMENT_NAME = "World Equity ETF"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _days_ago(days: int) -> date:
    return _now().date() - timedelta(days=days)


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def _seed_instrument(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    *,
    currency: str = "EUR",
) -> UUID:
    """Create one asset class and one listed investment to price."""
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        asset_class = await AssetClassRepository(session).create(
            code="equities", display_name="Equities"
        )
        investment = await InvestmentRepository(session).create(
            name=_INSTRUMENT_NAME,
            investment_type="listed_equity",
            asset_class_id=asset_class.id,
            currency=currency,
            created_by=user_id,
        )
        return investment.id


async def _seed_prices(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    investment_id: UUID,
    points: list[tuple[date, str]],
    *,
    currency: str = "EUR",
) -> None:
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        prices = InstrumentPriceRepository(session)
        for as_of_date, value in points:
            await prices.upsert(
                investment_id=investment_id,
                as_of_date=as_of_date,
                price=D(value),
                currency=currency,
                source="test",
                created_by=user_id,
            )


async def _seed_rates(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    currency: str,
    points: list[tuple[date, str]],
    *,
    reference_currency: str = "EUR",
) -> None:
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        rates = FxRateRepository(session)
        for as_of_date, value in points:
            await rates.upsert(
                currency=currency,
                as_of_date=as_of_date,
                rate_to_reference=D(value),
                reference_currency=reference_currency,
                source="test",
                created_by=user_id,
            )


async def _price_watchpoint(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    investment_id: UUID,
    *,
    drop_pct: Decimal = _DROP_PCT,
    window_days: int = _WINDOW_DAYS,
    muted: bool = False,
    warn_threshold_pct: Decimal | None = None,
    re_trigger_delta: Decimal | None = None,
) -> UUID:
    """Create one ``price`` watchpoint, effective an hour ago."""
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        created = await WatchpointRepository(session).create(
            family="price",
            subject_key=f"price:{investment_id}",
            display_name=_INSTRUMENT_NAME,
            effective_from=_now() - timedelta(hours=1),
            muted=muted,
            warn_threshold_pct=warn_threshold_pct,
            re_trigger_delta=re_trigger_delta,
            instrument_id=investment_id,
            drop_pct=drop_pct,
            window_days=window_days,
        )
        return created.watchpoint_id


async def _fx_watchpoint(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    pair: str,
    *,
    move_pct: Decimal = _MOVE_PCT,
    window_days: int = _WINDOW_DAYS,
) -> UUID:
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        created = await WatchpointRepository(session).create(
            family="fx",
            subject_key=f"fx:{pair}",
            display_name=f"FX move {pair}",
            effective_from=_now() - timedelta(hours=1),
            currency_pair=pair,
            move_pct=move_pct,
            window_days=window_days,
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


async def _triggered_price_book(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    tenant_id: UUID,
    *,
    latest_price: str = "93.8",
) -> tuple[UUID, UUID, str]:
    """A watched instrument down 6.2% over the window, by default.

    Returns the actor, the instrument and its subject key.
    """
    user_id = await seed_user(superuser_engine, tenant_id)
    investment_id = await _seed_instrument(app_engine, tenant_id, user_id)
    await _seed_prices(
        app_engine,
        tenant_id,
        user_id,
        investment_id,
        [(_days_ago(_WINDOW_DAYS), "100"), (_days_ago(0), latest_price)],
    )
    await _price_watchpoint(app_engine, tenant_id, user_id, investment_id)
    return user_id, investment_id, f"price:{investment_id}"


# ---------------------------------------------------------------------------
# The rising edge, and the pipeline it rides
# ---------------------------------------------------------------------------


async def test_a_decline_past_the_threshold_is_a_rising_edge(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """The whole pipeline in one pass: fetch, produce, upsert, acknowledge."""
    tenant_id = await seed_tenant("sig-price-rise")
    _, _, subject_key = await _triggered_price_book(app_engine, superuser_engine, tenant_id)

    eligible = await _evaluate(app_engine, tenant_id)

    assert len(eligible) == 1
    finding = eligible[0]
    assert finding.subject_key == subject_key
    assert finding.family == "price"
    assert finding.kind == "rising_edge"
    assert finding.status == "BREACH"
    assert finding.status_label == "Triggered"
    assert finding.magnitude == D("6.2000")
    assert finding.threshold_pct == D("5.0000")
    assert finding.window_days == _WINDOW_DAYS
    # The note names every concrete figure a reader would otherwise have to
    # open the watchpoint for.
    assert _INSTRUMENT_NAME in finding.note
    assert "6.20%" in finding.note
    assert "5 days" in finding.note
    assert "triggered" in finding.note
    assert "5.00%" in finding.note

    state = await _watch_state(app_engine, tenant_id, subject_key)
    assert state is not None
    assert state.magnitude == D("6.2000")
    assert state.band == "act"
    assert state.acknowledged_magnitude == D("6.2000")

    # Edge-triggered, not level-triggered: an unchanged book is silent.
    assert await _evaluate(app_engine, tenant_id) == []


async def test_a_per_subject_warn_override_moves_the_approaching_band(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """One unchanged price series, two classifications."""
    tenant_id = await seed_tenant("sig-price-warn")
    user_id = await seed_user(superuser_engine, tenant_id)
    investment_id = await _seed_instrument(app_engine, tenant_id, user_id)
    # 3% down against a 5% trigger: below the 90% default warn floor (4.5),
    # above a 55% one (2.75).
    await _seed_prices(
        app_engine,
        tenant_id,
        user_id,
        investment_id,
        [(_days_ago(_WINDOW_DAYS), "100"), (_days_ago(0), "97")],
    )
    await _price_watchpoint(
        app_engine, tenant_id, user_id, investment_id, warn_threshold_pct=D("55")
    )

    eligible = await _evaluate(app_engine, tenant_id)

    assert len(eligible) == 1
    assert eligible[0].status == "WARN"
    assert eligible[0].status_label == "Approaching"
    assert eligible[0].magnitude == D("3.0000")
    assert "approaching" in eligible[0].note


async def test_a_deepening_move_re_triggers_at_the_family_delta(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """Below the 5.0 pp default it is noise; at or above it is a re-trigger."""
    tenant_id = await seed_tenant("sig-price-retrigger")
    user_id, investment_id, subject_key = await _triggered_price_book(
        app_engine, superuser_engine, tenant_id
    )

    rising = await _evaluate(app_engine, tenant_id)
    assert len(rising) == 1 and rising[0].kind == "rising_edge"

    # Down to 90.0: magnitude 10.0 pp, a 3.8 pp deepening — under the
    # family's 5.0 pp delta.
    await _seed_prices(app_engine, tenant_id, user_id, investment_id, [(_days_ago(0), "90.0")])
    assert await _evaluate(app_engine, tenant_id) == []

    # Down to 88.0: magnitude 12.0 pp, a 5.8 pp deepening off the
    # acknowledged 6.2 — material.
    await _seed_prices(app_engine, tenant_id, user_id, investment_id, [(_days_ago(0), "88.0")])
    eligible = await _evaluate(app_engine, tenant_id)

    assert len(eligible) == 1
    assert eligible[0].kind == "magnitude_retrigger"
    assert eligible[0].acknowledged_magnitude == D("6.2000")
    assert eligible[0].magnitude == D("12.0000")
    assert "deepened" in eligible[0].reason

    state = await _watch_state(app_engine, tenant_id, subject_key)
    assert state is not None
    assert state.acknowledged_magnitude == D("12.0000")


async def test_a_per_subject_delta_override_re_triggers_on_a_smaller_move(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """The same 3.8 pp deepening, material once the subject says so."""
    tenant_id = await seed_tenant("sig-price-delta")
    user_id, investment_id, _ = await _triggered_price_book(app_engine, superuser_engine, tenant_id)
    rising = await _evaluate(app_engine, tenant_id)
    assert len(rising) == 1

    # A second identity would be a second subject; the override belongs on
    # the one that already exists, so it is written as a revision.
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        current = (await WatchpointRepository(session).effective_watchpoints(_now()))[0]
        await WatchpointRepository(session).revise(
            current.watchpoint_id,
            effective_from=_now(),
            display_name=current.display_name,
            re_trigger_delta=D("1.0"),
            drop_pct=current.drop_pct,
            window_days=current.window_days,
        )

    await _seed_prices(app_engine, tenant_id, user_id, investment_id, [(_days_ago(0), "90.0")])
    eligible = await _evaluate(app_engine, tenant_id)

    assert len(eligible) == 1
    assert eligible[0].kind == "magnitude_retrigger"
    assert eligible[0].magnitude == D("10.0000")


# ---------------------------------------------------------------------------
# Mute — quota-only breach exception (ADR-0116 §3)
# ---------------------------------------------------------------------------


async def test_a_muted_triggered_price_subject_yields_no_finding(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """A signal family has no regulatory floor, so its trigger *can* be muted.

    This is the deliberate asymmetry against ``saa``/``anlv``, where a live
    BREACH fires through the mute. The watch-state row still advances — the
    mute suppresses the finding, never the observation.
    """
    tenant_id = await seed_tenant("sig-price-muted")
    user_id = await seed_user(superuser_engine, tenant_id)
    investment_id = await _seed_instrument(app_engine, tenant_id, user_id)
    await _seed_prices(
        app_engine,
        tenant_id,
        user_id,
        investment_id,
        [(_days_ago(_WINDOW_DAYS), "100"), (_days_ago(0), "93.8")],
    )
    await _price_watchpoint(app_engine, tenant_id, user_id, investment_id, muted=True)
    subject_key = f"price:{investment_id}"

    assert await _evaluate(app_engine, tenant_id) == []

    state = await _watch_state(app_engine, tenant_id, subject_key)
    assert state is not None
    assert state.magnitude == D("6.2000")
    assert state.band == "act"
    assert state.acknowledged_magnitude == D("6.2000")


async def test_the_closing_all_clear_of_a_muted_triggered_subject_still_fires(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """Family-agnostic half of the rule: never strand a raised card.

    The subject is muted throughout. Its trigger is withheld (the test
    above), and when it eases the all-clear passes anyway — because the
    acknowledged state records the level, and a card raised for that level
    on an earlier beat must be closeable.
    """
    tenant_id = await seed_tenant("sig-price-muted-clear")
    user_id = await seed_user(superuser_engine, tenant_id)
    investment_id = await _seed_instrument(app_engine, tenant_id, user_id)
    await _seed_prices(
        app_engine,
        tenant_id,
        user_id,
        investment_id,
        [(_days_ago(_WINDOW_DAYS), "100"), (_days_ago(0), "93.8")],
    )
    await _price_watchpoint(app_engine, tenant_id, user_id, investment_id, muted=True)
    subject_key = f"price:{investment_id}"

    assert await _evaluate(app_engine, tenant_id) == []

    # The instrument recovers to par: magnitude 0, a falling edge off the
    # acknowledged trigger.
    await _seed_prices(app_engine, tenant_id, user_id, investment_id, [(_days_ago(0), "101")])
    eligible = await _evaluate(app_engine, tenant_id)

    assert len(eligible) == 1
    assert eligible[0].kind == "falling_edge"
    assert eligible[0].status_label == "Calm"
    assert eligible[0].magnitude == D("0.0000")
    assert "eased" in eligible[0].note
    assert "at or above its price" in eligible[0].note

    state = await _watch_state(app_engine, tenant_id, subject_key)
    assert state is not None
    assert state.acknowledged_at is None


# ---------------------------------------------------------------------------
# Retirement and missing data
# ---------------------------------------------------------------------------


async def test_retiring_a_watchpoint_stops_evaluation(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """No further observations — and the history stays queryable."""
    tenant_id = await seed_tenant("sig-price-retired")
    user_id, _, subject_key = await _triggered_price_book(app_engine, superuser_engine, tenant_id)
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        current = (await WatchpointRepository(session).effective_watchpoints(_now()))[0]
        await WatchpointRepository(session).retire(
            current.watchpoint_id, effective_from=_now(), notes="no longer held"
        )

    assert await _evaluate(app_engine, tenant_id) == []
    assert await _watch_state(app_engine, tenant_id, subject_key) is None

    async with tenant_context(app_engine, tenant_id) as session:
        versions = await WatchpointRepository(session).list_versions(current.watchpoint_id)
    assert len(versions) == 2 and versions[-1].retired


async def test_missing_prices_write_no_watch_state_row(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Silence is logged per subject, never recorded as calm.

    A watch-state row here would let an outage in the data supply reset an
    acknowledged state — the subject would look like it had eased when in
    truth nothing was observed at all.
    """
    tenant_id = await seed_tenant("sig-price-nodata")
    user_id = await seed_user(superuser_engine, tenant_id)
    investment_id = await _seed_instrument(app_engine, tenant_id, user_id)
    await _price_watchpoint(app_engine, tenant_id, user_id, investment_id)

    with caplog.at_level(logging.INFO, logger="services.irene.signal_delta"):
        assert await _evaluate(app_engine, tenant_id) == []

    assert await _watch_state(app_engine, tenant_id, f"price:{investment_id}") is None
    assert any("cannot be evaluated" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# fx — orientation is the caller's job, and it does it
# ---------------------------------------------------------------------------


async def test_an_fx_pair_is_served_in_its_own_orientation(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """The stored leg is USD→EUR; ``EUR/USD`` is served by inverting it.

    ``fx_rates`` holds one row per currency against the dataset's reference
    (EUR here), never a pair. USD moves 0.80 → 0.84, so:

    * ``USD/EUR`` reads the leg as stored: +5.0000%;
    * ``EUR/USD`` is its inverse, 1.25 → 1.190476…: 4.7619%.

    Both fire; the two magnitudes are *different*, which is exactly why the
    orientation has to be settled before the pure producer sees a number.
    """
    tenant_id = await seed_tenant("sig-fx-orientation")
    user_id = await seed_user(superuser_engine, tenant_id)
    await _seed_rates(
        app_engine,
        tenant_id,
        user_id,
        "USD",
        [(_days_ago(_WINDOW_DAYS), "0.80"), (_days_ago(0), "0.84")],
    )
    await _fx_watchpoint(app_engine, tenant_id, user_id, "USD/EUR")
    await _fx_watchpoint(app_engine, tenant_id, user_id, "EUR/USD")

    eligible = {finding.subject_key: finding for finding in await _evaluate(app_engine, tenant_id)}

    assert set(eligible) == {"fx:USD/EUR", "fx:EUR/USD"}
    assert eligible["fx:USD/EUR"].magnitude == D("5.0000")
    assert eligible["fx:EUR/USD"].magnitude == D("4.7619")
    for finding in eligible.values():
        assert finding.family == "fx"
        assert finding.kind == "rising_edge"
        assert finding.status_label == "Triggered"
        assert "FX watchpoint triggered" in finding.note


async def test_an_uncovered_pair_cannot_be_evaluated(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """A tenant holding no rates gets silence, not an exception."""
    tenant_id = await seed_tenant("sig-fx-nodata")
    user_id = await seed_user(superuser_engine, tenant_id)
    await _fx_watchpoint(app_engine, tenant_id, user_id, "CHF/EUR")

    assert await _evaluate(app_engine, tenant_id) == []
    assert await _watch_state(app_engine, tenant_id, "fx:CHF/EUR") is None


# ---------------------------------------------------------------------------
# Wording (ADR-0116 §4) — asserted, not eyeballed
# ---------------------------------------------------------------------------


async def test_no_deterministic_signal_string_says_breach(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """ "Breach" is regulatory language, reserved for the quota families.

    Every kind and every status a signal subject can reach is walked here —
    a rising edge into Triggered, a re-trigger, and the all-clear that
    closes it — and each one's deterministic strings are checked. The
    internal status field is exempt by design: it is the edge machinery's
    vocabulary and never reaches a human.
    """
    tenant_id = await seed_tenant("sig-wording")
    user_id, investment_id, _ = await _triggered_price_book(app_engine, superuser_engine, tenant_id)

    collected: list[SignalEligibleFinding] = list(await _evaluate(app_engine, tenant_id))
    await _seed_prices(app_engine, tenant_id, user_id, investment_id, [(_days_ago(0), "88.0")])
    collected += await _evaluate(app_engine, tenant_id)
    await _seed_prices(app_engine, tenant_id, user_id, investment_id, [(_days_ago(0), "101")])
    collected += await _evaluate(app_engine, tenant_id)

    assert [finding.kind for finding in collected] == [
        "rising_edge",
        "magnitude_retrigger",
        "falling_edge",
    ]
    for finding in collected:
        for text in (finding.note, finding.reason, finding.status_label, finding.display_name):
            assert "breach" not in text.lower(), f"{finding.kind}: {text!r}"


# ---------------------------------------------------------------------------
# Through the beat: the floor, and what reaches Irene
# ---------------------------------------------------------------------------


async def test_a_surfaced_price_signal_is_floored_at_four(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """``price_trigger`` floors at 4 (ADR-0116 §4), so a 1 cannot persist.

    Also pins that the context Irene reads speaks the human vocabulary: the
    surest way to keep "breach" off a card is to keep it out of what the
    model was shown.
    """
    tenant_id = await seed_tenant("sig-beat-floor")
    _, _, subject_key = await _triggered_price_book(app_engine, superuser_engine, tenant_id)
    core = SurfacingCore(subject_key, urgency_suggestion=1)

    async with tenant_context(app_engine, tenant_id) as session:
        result = await run_beat(session, core, llm=_TEST_LLM, now=_now())
        findings = await IreneFindingRepository(session).list_open()

    assert result.error is None
    assert result.findings_written == 1
    assert len(findings) == 1
    assert findings[0].subject_key == subject_key
    assert findings[0].urgency == 4  # raised from the suggested 1 by the floor
    assert findings[0].band == "noteworthy"

    context = core.calls[0]["context_messages"][0]["content"]
    assert subject_key in context
    assert "status=Triggered" in context
    assert "BREACH" not in context
    assert "never as a breach" in context  # the instruction, not a status


async def test_a_signal_all_clear_is_capped_at_informational(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """An eased watchpoint is an ``all_clear`` like any other (ADR-0116 §4).

    The family axis is checked *after* the falling edge in
    ``derive_trigger_type``, which is what keeps the pinned "an all-clear is
    never itself urgent" invariant true for the signal families too.
    """
    tenant_id = await seed_tenant("sig-beat-allclear")
    user_id, investment_id, subject_key = await _triggered_price_book(
        app_engine, superuser_engine, tenant_id
    )
    assert len(await _evaluate(app_engine, tenant_id)) == 1

    await _seed_prices(app_engine, tenant_id, user_id, investment_id, [(_days_ago(0), "101")])
    core = SurfacingCore(subject_key, urgency_suggestion=9)

    async with tenant_context(app_engine, tenant_id) as session:
        result = await run_beat(session, core, llm=_TEST_LLM, now=_now())
        findings = await IreneFindingRepository(session).list_open()

    assert result.findings_written == 1
    assert findings[0].urgency == 3  # capped, not the suggested 9
    assert findings[0].band == "informational"
