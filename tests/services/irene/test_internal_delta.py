# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Live-DB tests for the stateful internal delta (ADR-0087).

Exercises :func:`services.irene.internal_delta.evaluate_internal_deltas`
end-to-end against the compose Postgres, via the shared ``app_engine`` /
``superuser_engine`` / ``seed_tenant`` fixtures — the same pattern as
``tests/services/test_limits_coverage_service.py``. The delta reads the
real coverage snapshot and reads/writes ``irene_watch_state``, so a live
tenant context is required. Book seeding is shared with the beat tests
via ``tests/services/irene/_book_fixtures``.

Coverage:

* Calm book ⇒ zero eligible findings; watch-state rows upserted; no
  acknowledgement written.
* A fresh breach ⇒ one rising-edge eligible finding; acknowledgement
  written.
* A second identical beat ⇒ silence (the edge is already acknowledged).
* A falling edge ⇒ one all-clear eligible finding; ``acknowledged_*``
  reset.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import tenant_context
from core.repositories.irene_watch_state_repository import (
    IreneWatchStateRepository,
)
from services.irene.internal_delta import (
    EligibleFinding,
    evaluate_internal_deltas,
)
from tests.services.irene._book_fixtures import (
    ANLV_SUBJECT,
    BREACH_NAV,
    CALM_NAV,
    SAA_SUBJECT,
    D,
    resolution,
    seed_book,
    seed_user,
    set_latest_nav,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Calm book
# ---------------------------------------------------------------------------


async def test_calm_book_no_eligible_upserts_without_ack(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
) -> None:
    tenant_id = await seed_tenant("irene-calm")
    user_id = await seed_user(superuser_engine, tenant_id)
    # 100k / 1M = 10% coverage, well under the 50% ceiling → OK.
    await seed_book(app_engine, tenant_id, user_id, latest_nav=CALM_NAV)

    async with tenant_context(app_engine, tenant_id) as session:
        eligible = await evaluate_internal_deltas(
            session, now=_now(), resolution=await resolution(session)
        )
        assert eligible == []

        watch = IreneWatchStateRepository(session)
        saa = await watch.get_by_subject(SAA_SUBJECT)
        anlv = await watch.get_by_subject(ANLV_SUBJECT)
        # Both constrained subjects were upserted (world state recorded)…
        assert saa is not None and anlv is not None
        assert saa.band == "note" and anlv.band == "note"
        # …but nothing was acknowledged on a calm book.
        assert saa.acknowledged_at is None
        assert saa.acknowledged_magnitude is None
        assert anlv.acknowledged_at is None


# ---------------------------------------------------------------------------
# Fresh breach → rising edge + acknowledgement
# ---------------------------------------------------------------------------


async def test_fresh_breach_rising_edge_and_acknowledges(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
) -> None:
    tenant_id = await seed_tenant("irene-breach")
    user_id = await seed_user(superuser_engine, tenant_id)
    # 600k / 1M = 60% coverage, over the 50% ceiling → BREACH.
    await seed_book(app_engine, tenant_id, user_id, latest_nav=BREACH_NAV)

    async with tenant_context(app_engine, tenant_id) as session:
        eligible = await evaluate_internal_deltas(
            session, now=_now(), resolution=await resolution(session)
        )

        assert len(eligible) == 1
        finding = eligible[0]
        assert isinstance(finding, EligibleFinding)
        assert finding.subject_key == SAA_SUBJECT
        assert finding.kind == "rising_edge"
        assert finding.status == "BREACH"
        assert finding.band == "act"
        assert finding.coverage_pct == D("60")
        assert finding.max_pct == D("50.0")
        assert finding.acknowledged_magnitude is None
        assert finding.provisional_urgency_hint == 4

        watch = IreneWatchStateRepository(session)
        saa = await watch.get_by_subject(SAA_SUBJECT)
        assert saa is not None
        # The surfaced level is acknowledged so it will not re-fire.
        assert saa.acknowledged_at is not None
        assert saa.acknowledged_magnitude == D("60")


# ---------------------------------------------------------------------------
# Second identical beat → silence
# ---------------------------------------------------------------------------


async def test_second_identical_beat_is_silent(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
) -> None:
    tenant_id = await seed_tenant("irene-repeat")
    user_id = await seed_user(superuser_engine, tenant_id)
    await seed_book(app_engine, tenant_id, user_id, latest_nav=BREACH_NAV)

    async with tenant_context(app_engine, tenant_id) as session:
        first = await evaluate_internal_deltas(
            session, now=_now(), resolution=await resolution(session)
        )
        assert len(first) == 1

    # A second beat with an unchanged breach: the edge is already
    # acknowledged, so nothing new is eligible.
    async with tenant_context(app_engine, tenant_id) as session:
        second = await evaluate_internal_deltas(
            session, now=_now(), resolution=await resolution(session)
        )
        assert second == []


# ---------------------------------------------------------------------------
# Falling edge → all-clear + acknowledgement reset
# ---------------------------------------------------------------------------


async def test_falling_edge_all_clear_and_resets_acknowledgement(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
) -> None:
    tenant_id = await seed_tenant("irene-fall")
    user_id = await seed_user(superuser_engine, tenant_id)
    investment_id = await seed_book(app_engine, tenant_id, user_id, latest_nav=BREACH_NAV)

    # Beat 1: the breach rises and is acknowledged.
    async with tenant_context(app_engine, tenant_id) as session:
        rising = await evaluate_internal_deltas(
            session, now=_now(), resolution=await resolution(session)
        )
        assert len(rising) == 1 and rising[0].kind == "rising_edge"

    # The position is unwound back under the ceiling (10% coverage).
    await set_latest_nav(app_engine, tenant_id, user_id, investment_id, CALM_NAV)

    # Beat 2: the improvement to benign is a falling-edge all-clear.
    async with tenant_context(app_engine, tenant_id) as session:
        falling = await evaluate_internal_deltas(
            session, now=_now(), resolution=await resolution(session)
        )

        assert len(falling) == 1
        finding = falling[0]
        assert finding.subject_key == SAA_SUBJECT
        assert finding.kind == "falling_edge"
        assert finding.status == "OK"
        assert finding.band == "note"
        # All-clear hint is the lowest; Prompt 4's floor caps it.
        assert finding.provisional_urgency_hint == 0

        watch = IreneWatchStateRepository(session)
        saa = await watch.get_by_subject(SAA_SUBJECT)
        assert saa is not None
        # Acknowledgement reset so a later re-entry edge-triggers afresh.
        assert saa.acknowledged_at is None
        assert saa.acknowledged_magnitude is None
