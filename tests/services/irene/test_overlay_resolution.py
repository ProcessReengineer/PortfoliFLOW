# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Live-DB tests for per-tenant calibration in the beat (ADR-0116 §3/§5).

Where ``test_internal_delta.py`` pins the delta layer's edge semantics on
code defaults, these pin what a **watchpoint overlay** and a
``floor_calibration`` revision change about them:

* a per-subject WARN override moves the edge classification;
* a per-subject re-trigger delta moves the re-trigger decision;
* mute suppresses a non-breach finding while the watch-state row still
  advances;
* a breach edge fires under mute, and the all-clear that closes it passes
  under mute too;
* a stored revision that no longer composes fails the tenant's beat loudly
  and is journalled as an error, never degraded to the code defaults.

They run against the compose Postgres through the shared ``app_engine`` /
``superuser_engine`` / ``seed_tenant`` fixtures, over the same two-
investment book the other delta tests use.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import FloorCalibrationInvalid
from core.repositories import tenant_context
from core.repositories.floor_calibration_repository import FloorCalibrationRepository
from core.repositories.irene_watch_state_repository import IreneWatchStateRepository
from core.repositories.watchpoint_repository import WatchpointRepository
from services.ai_service_core import ResolvedLLM, SynthesisResult
from services.analytics.irene_floor import (
    DEFAULT_FLOOR_CONFIG,
    DEFAULT_WARN_THRESHOLD_PCT,
)
from services.irene.beat import run_beat
from services.irene.internal_delta import evaluate_internal_deltas
from services.watch_desk.calibration import save_calibration_revision
from services.watch_desk.overlay import resolve_watch_desk
from tests.services.irene._book_fixtures import (
    BREACH_NAV,
    CALM_NAV,
    SAA_SUBJECT,
    D,
    resolution,
    seed_book,
    seed_user,
    set_latest_nav,
)

# 35% of the 1M book against a 50% ceiling: OK at the tenant's default 90%
# WARN (which puts the WARN floor at 45% coverage), WARN once the subject's
# own threshold is lowered to 55% (WARN floor 27.5%). One NAV, two
# classifications — which is the whole claim of the per-subject override.
_HALFWAY_NAV = Decimal("350000")

#: A legal per-subject WARN override. The repository bounds it strictly
#: inside (50, 100), so the flip has to be arranged with the NAV rather
#: than with an arbitrarily low threshold.
_LOW_WARN = Decimal("55")

_TEST_LLM = ResolvedLLM(
    base_url="https://openrouter.test/api/v1",
    api_key="sk-overlay-test",
    model="test-model",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _SilentCore:
    """Duck-typed AI core that surfaces nothing — the silence path."""

    def get_system_prompt(self, prompt_name: str = "irene") -> str:
        return "You are Irene."

    async def run_synthesis(self, **_kwargs: Any) -> SynthesisResult:
        return SynthesisResult(tool_calls=[], raw_text="")


async def _overlay(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    *,
    subject_key: str = SAA_SUBJECT,
    muted: bool = False,
    warn_threshold_pct: Decimal | None = None,
    re_trigger_delta: Decimal | None = None,
) -> UUID:
    """Create one sensitivity overlay, effective an hour ago."""
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        created = await WatchpointRepository(session).create(
            family=subject_key.split(":", 1)[0],
            subject_key=subject_key,
            display_name=subject_key,
            effective_from=_now() - timedelta(hours=1),
            muted=muted,
            warn_threshold_pct=warn_threshold_pct,
            re_trigger_delta=re_trigger_delta,
        )
        return created.watchpoint_id


# ---------------------------------------------------------------------------
# Per-subject WARN override → a different edge classification
# ---------------------------------------------------------------------------


async def test_per_subject_warn_override_changes_the_edge_classification(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
) -> None:
    """One unchanged NAV: calm on the default, a WARN edge on the override."""
    tenant_id = await seed_tenant("ovl-warn")
    user_id = await seed_user(superuser_engine, tenant_id)
    await seed_book(app_engine, tenant_id, user_id, latest_nav=_HALFWAY_NAV)

    # Baseline: 50% utilisation of the ceiling is comfortably under the
    # tenant's 90% WARN threshold, so nothing is eligible.
    async with tenant_context(app_engine, tenant_id) as session:
        assert (
            await evaluate_internal_deltas(
                session, now=_now(), resolution=await resolution(session)
            )
            == []
        )

    await _overlay(app_engine, tenant_id, user_id, warn_threshold_pct=_LOW_WARN)

    async with tenant_context(app_engine, tenant_id) as session:
        eligible = await evaluate_internal_deltas(
            session, now=_now(), resolution=await resolution(session)
        )

    assert len(eligible) == 1
    assert eligible[0].subject_key == SAA_SUBJECT
    assert eligible[0].kind == "rising_edge"
    # The figure is unchanged — only the threshold it is judged against is.
    assert eligible[0].status == "WARN"
    assert eligible[0].coverage_pct == D("35")


# ---------------------------------------------------------------------------
# Per-subject delta override → a different re-trigger decision
# ---------------------------------------------------------------------------


async def test_per_subject_delta_override_changes_the_retrigger_decision(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
) -> None:
    """A 4 pp move is noise at the 5.0 pp default and material at 1.0 pp."""
    tenant_id = await seed_tenant("ovl-delta")
    user_id = await seed_user(superuser_engine, tenant_id)
    investment_id = await seed_book(app_engine, tenant_id, user_id, latest_nav=BREACH_NAV)

    # Beat 1: the breach rises and is acknowledged at 60%.
    async with tenant_context(app_engine, tenant_id) as session:
        rising = await evaluate_internal_deltas(
            session, now=_now(), resolution=await resolution(session)
        )
        assert len(rising) == 1 and rising[0].kind == "rising_edge"

    # Deepen the breach by 4 pp — below the 5.0 pp family default.
    await set_latest_nav(app_engine, tenant_id, user_id, investment_id, Decimal("640000"))

    async with tenant_context(app_engine, tenant_id) as session:
        assert (
            await evaluate_internal_deltas(
                session, now=_now(), resolution=await resolution(session)
            )
            == []
        )

    await _overlay(app_engine, tenant_id, user_id, re_trigger_delta=D("1.0"))

    async with tenant_context(app_engine, tenant_id) as session:
        eligible = await evaluate_internal_deltas(
            session, now=_now(), resolution=await resolution(session)
        )

    assert len(eligible) == 1
    assert eligible[0].kind == "magnitude_retrigger"
    assert eligible[0].acknowledged_magnitude == D("60")
    assert eligible[0].current_magnitude == D("64")


# ---------------------------------------------------------------------------
# Mute — suppresses the finding, never the state
# ---------------------------------------------------------------------------


async def test_mute_suppresses_a_non_breach_finding_but_advances_watch_state(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
) -> None:
    """The eligible is withheld; the row is still upserted and acknowledged."""
    tenant_id = await seed_tenant("ovl-mute-warn")
    user_id = await seed_user(superuser_engine, tenant_id)
    await seed_book(app_engine, tenant_id, user_id, latest_nav=_HALFWAY_NAV)
    # WARN (not BREACH) under the override, and muted.
    await _overlay(
        app_engine,
        tenant_id,
        user_id,
        muted=True,
        warn_threshold_pct=_LOW_WARN,
    )

    async with tenant_context(app_engine, tenant_id) as session:
        eligible = await evaluate_internal_deltas(
            session, now=_now(), resolution=await resolution(session)
        )
        assert eligible == []

        watch = await IreneWatchStateRepository(session).get_by_subject(SAA_SUBJECT)

    assert watch is not None
    # Mute suppresses finding creation only — the world state is recorded…
    assert watch.magnitude == D("35")
    assert watch.band == "watch"
    # …and the level is acknowledged exactly as it would be unmuted.
    assert watch.acknowledged_at is not None
    assert watch.acknowledged_magnitude == D("35")


async def test_a_breach_edge_fires_under_mute_and_its_all_clear_passes_too(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
) -> None:
    """A BREACH cannot be muted, and a raised breach must be able to resolve."""
    tenant_id = await seed_tenant("ovl-mute-breach")
    user_id = await seed_user(superuser_engine, tenant_id)
    investment_id = await seed_book(app_engine, tenant_id, user_id, latest_nav=BREACH_NAV)
    await _overlay(app_engine, tenant_id, user_id, muted=True)

    async with tenant_context(app_engine, tenant_id) as session:
        rising = await evaluate_internal_deltas(
            session, now=_now(), resolution=await resolution(session)
        )

    assert len(rising) == 1
    assert rising[0].kind == "rising_edge"
    assert rising[0].status == "BREACH"

    # Unwind under the ceiling: the all-clear closes out a breach raised
    # under mute, so it passes the gate the ordinary all-clear would not.
    await set_latest_nav(app_engine, tenant_id, user_id, investment_id, CALM_NAV)

    async with tenant_context(app_engine, tenant_id) as session:
        falling = await evaluate_internal_deltas(
            session, now=_now(), resolution=await resolution(session)
        )

    assert len(falling) == 1
    assert falling[0].kind == "falling_edge"
    assert falling[0].status == "OK"


async def test_an_ordinary_all_clear_is_suppressed_under_mute(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
) -> None:
    """The exception is for breaches only — a muted subject's calm is muted."""
    tenant_id = await seed_tenant("ovl-mute-allclear")
    user_id = await seed_user(superuser_engine, tenant_id)
    investment_id = await seed_book(app_engine, tenant_id, user_id, latest_nav=_HALFWAY_NAV)
    watchpoint_id = await _overlay(
        app_engine,
        tenant_id,
        user_id,
        warn_threshold_pct=_LOW_WARN,
    )

    # Beat 1: unmuted, a WARN edge rises and is acknowledged.
    async with tenant_context(app_engine, tenant_id) as session:
        rising = await evaluate_internal_deltas(
            session, now=_now(), resolution=await resolution(session)
        )
        assert len(rising) == 1 and rising[0].status == "WARN"

    # Mute it, then let it fall back to calm.
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        await WatchpointRepository(session).revise(
            watchpoint_id,
            effective_from=_now(),
            display_name=SAA_SUBJECT,
            muted=True,
            warn_threshold_pct=_LOW_WARN,
        )
    await set_latest_nav(app_engine, tenant_id, user_id, investment_id, CALM_NAV)

    async with tenant_context(app_engine, tenant_id) as session:
        falling = await evaluate_internal_deltas(
            session, now=_now(), resolution=await resolution(session)
        )
        assert falling == []

        watch = await IreneWatchStateRepository(session).get_by_subject(SAA_SUBJECT)

    assert watch is not None
    # The falling edge still reset the acknowledgement: the state machine
    # ran in full, only the finding was withheld.
    assert watch.acknowledged_at is None


# ---------------------------------------------------------------------------
# Beat and monitor share one resolution
# ---------------------------------------------------------------------------


async def test_the_resolution_carries_calibration_and_overlays_together(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
) -> None:
    """One call returns the config, the WARN default and the overlay map."""
    tenant_id = await seed_tenant("ovl-resolve")
    user_id = await seed_user(superuser_engine, tenant_id)
    await seed_book(app_engine, tenant_id, user_id, latest_nav=CALM_NAV)
    await _overlay(
        app_engine,
        tenant_id,
        user_id,
        muted=True,
        warn_threshold_pct=D("70"),
        re_trigger_delta=D("2.5"),
    )

    async with tenant_context(app_engine, tenant_id) as session:
        resolved = await resolve_watch_desk(session, as_of=_now())

    assert resolved.config is DEFAULT_FLOOR_CONFIG
    assert resolved.warn_default_pct == DEFAULT_WARN_THRESHOLD_PCT
    assert resolved.warn_threshold_for(SAA_SUBJECT) == D("70.000")
    assert resolved.re_trigger_delta_for(SAA_SUBJECT) == D("2.5000")
    assert resolved.is_muted(SAA_SUBJECT)
    # A subject with no overlay falls back to the tenant default, and the
    # family delta from the effective config.
    assert resolved.warn_threshold_for("saa:other") == resolved.warn_default_pct
    assert (
        resolved.re_trigger_delta_for("saa:other") == DEFAULT_FLOOR_CONFIG.re_trigger_delta["saa"]
    )


async def test_defined_families_are_not_resolved_as_overlays(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
) -> None:
    """A freshness watchpoint has no producer yet, so it overlays nothing."""
    tenant_id = await seed_tenant("ovl-defined")
    user_id = await seed_user(superuser_engine, tenant_id)

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        await WatchpointRepository(session).create(
            family="freshness",
            subject_key="freshness:*",
            display_name="NAV freshness",
            effective_from=_now() - timedelta(hours=1),
            max_age_days=120,
        )
        resolved = await resolve_watch_desk(session, as_of=_now())

    assert resolved.overlays == {}


# ---------------------------------------------------------------------------
# A stored revision that no longer composes fails the run loudly
# ---------------------------------------------------------------------------


async def test_uncomposable_calibration_fails_the_tenant_beat_loudly(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Never a silent fallback to the code defaults (ADR-0116 §5).

    The revision below is written through the sanctioned write path against
    *shifted* defaults, so it is valid when made — then the beat composes it
    over the real ``DEFAULT_FLOOR_CONFIG``, where the pinned invariant it no
    longer satisfies (a limit-breach floor below the critical band) makes it
    uncomposable. That is exactly the case the write path cannot cover, and
    the one the composition check exists for.
    """
    tenant_id = await seed_tenant("ovl-broken")
    user_id = await seed_user(superuser_engine, tenant_id)
    await seed_book(app_engine, tenant_id, user_id, latest_nav=CALM_NAV)

    # Defaults with a lower critical band, under which floor[limit_breach]=5
    # is legal. Composed over the real defaults (critical starts at 7) it is
    # not.
    shifted = replace(
        DEFAULT_FLOOR_CONFIG,
        band_boundaries=(2, 4),
        cap={**DEFAULT_FLOOR_CONFIG.cap, "rss": 2, "all_clear": 2},
    )
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        await save_calibration_revision(
            FloorCalibrationRepository(session),
            effective_from=_now() - timedelta(hours=1),
            floor={"limit_breach": 5},
            defaults=shifted,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        with pytest.raises(FloorCalibrationInvalid):
            await resolve_watch_desk(session, as_of=_now())

        # The beat turns it into a per-tenant error rather than raising out
        # of the tick — and it is journalled at ERROR, never degraded.
        with caplog.at_level("ERROR", logger="services.irene.beat"):
            result = await run_beat(session, _SilentCore(), llm=_TEST_LLM, now=_now())

    assert result.error is not None
    assert "calibration invalid" in result.error
    assert result.findings_written == 0
    assert any("refusing to beat on the code defaults" in rec.message for rec in caplog.records)
