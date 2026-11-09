# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The case wake-up — the shared tick runner's third step (ADR-0138 §4, P-CS-2c).

Database-backed, against the test container: the runner gets the
RLS-bypassing ``superuser_engine`` exactly as its hosts hand it one, and
the data is written through ``app_engine`` inside ``tenant_context``. (The
runner tests in ``tests/services/scheduler/`` are fake-engine only; this
module is the DB-backed one, so it lives beside the other Cases tests.)

Every tenant here carries an Irene schedule row, as every real tenant does
(``install_irene_schedule``), because the wake step enumerates tenants from
``irene_schedule`` (D-CS-B-26).

Coverage
--------
* WK-01: due today and overdue cases are lifted by ``system`` with reason
  ``date`` and a frozen snapshot; a future one is untouched.
* WK-02: two tenants, each lifted in its own context.
* WK-03: a held cases-domain advisory lock skips that tenant only.
* WK-04: one tenant's failure is isolated; the tick returns normally.
* WK-05: a second run lifts nothing; enumeration is by schedule row; an
  open or closed row with a stale ``wait_until`` is never due.
* WK-06: the wake day is the UTC calendar day of ``now`` (D-CS-B-21).
* WK-07: a due case the PM resumed between the due read and its lift is
  skipped; the next is lifted and the tenant's tick commits (P-CS-H1).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import CaseRepository, IreneScheduleRepository, tenant_context
from core.repositories.case_repository import CaseDTO
from services.irene.scheduling import advisory_lock_key
from services.scheduler.tick_runner import run_case_wake_tick
from services.tenant_defaults import install_irene_schedule
from tests.services.irene._book_fixtures import seed_user

_T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
_TICK = datetime(2026, 11, 3, 6, 0, tzinfo=timezone.utc)
_TODAY = date(2026, 11, 3)


async def _tenant(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant: Any, name: str
) -> tuple[UUID, UUID]:
    """A tenant with its seeded Irene schedule row, and a PM."""
    tenant_id = await seed_tenant(name)
    user_id = await seed_user(superuser_engine, tenant_id)
    async with tenant_context(app_engine, tenant_id) as session:
        await install_irene_schedule(IreneScheduleRepository(session), now=_T0)
    return tenant_id, user_id


async def _waiting_case(
    app_engine: AsyncEngine, tenant_id: UUID, user_id: UUID, title: str, wait_until: date
) -> CaseDTO:
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        repo = CaseRepository(session)
        case = await repo.create(title=title, opened_by=user_id, opened_actor="pm", now=_T0)
        return await repo.wait(case.id, wait_until=wait_until, by=user_id, now=_T0)


async def _state(app_engine: AsyncEngine, tenant_id: UUID, case_id: UUID) -> CaseDTO:
    async with tenant_context(app_engine, tenant_id) as session:
        case = await CaseRepository(session).get(case_id)
    assert case is not None
    return case


async def _assert_lifted_by_date(
    app_engine: AsyncEngine, tenant_id: UUID, case: CaseDTO, *, now: datetime
) -> None:
    """Open, no date, one ``waiting_lifted`` by ``system`` (reason ``date``) and its snapshot."""
    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)
        after = await repo.get(case.id)
        entries = await repo.list_entries(case.id, include_frozen=True)
    assert after is not None and (after.state, after.wait_until) == ("open", None)
    assert [e.kind for e in entries[-2:]] == ["waiting_lifted", "frozen"]
    lifted, frozen = entries[-2:]
    assert (lifted.actor, lifted.actor_user_id) == ("system", None)
    assert lifted.created_at == now == frozen.created_at
    assert lifted.payload == {
        "reason": "date",
        "wait_until": case.wait_until.isoformat() if case.wait_until else None,
        "event": None,
    }
    assert frozen.payload["milestone_entry_id"] == str(lifted.id)
    assert frozen.payload["file"]["case"]["state"] == "open"


# ---------------------------------------------------------------------------
# WK-01
# ---------------------------------------------------------------------------


async def test_wk01_due_cases_are_lifted_by_date(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id, user_id = await _tenant(app_engine, superuser_engine, seed_tenant, "WK-01")
    today = await _waiting_case(app_engine, tenant_id, user_id, "today", _TODAY)
    overdue = await _waiting_case(
        app_engine, tenant_id, user_id, "overdue", _TODAY - timedelta(days=1)
    )
    future = await _waiting_case(
        app_engine, tenant_id, user_id, "tomorrow", _TODAY + timedelta(days=1)
    )

    summary = await run_case_wake_tick(superuser_engine, now=_TICK)

    assert (summary.tenants_seen, summary.tenants_claimed, summary.cases_lifted) == (1, 1, 2)
    assert (summary.tenants_skipped_locked, summary.tenants_failed) == (0, 0)
    for case in (today, overdue):
        await _assert_lifted_by_date(app_engine, tenant_id, case, now=_TICK)
    untouched = await _state(app_engine, tenant_id, future.id)
    assert (untouched.state, untouched.wait_until) == ("waiting", future.wait_until)


# ---------------------------------------------------------------------------
# WK-02
# ---------------------------------------------------------------------------


async def test_wk02_two_tenants_each_in_their_own_context(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    first, first_user = await _tenant(app_engine, superuser_engine, seed_tenant, "WK-02a")
    second, second_user = await _tenant(app_engine, superuser_engine, seed_tenant, "WK-02b")
    a = await _waiting_case(app_engine, first, first_user, "a", _TODAY)
    b = await _waiting_case(app_engine, second, second_user, "b", _TODAY)

    summary = await run_case_wake_tick(superuser_engine, now=_TICK)

    assert (summary.tenants_seen, summary.tenants_claimed, summary.cases_lifted) == (2, 2, 2)
    await _assert_lifted_by_date(app_engine, first, a, now=_TICK)
    await _assert_lifted_by_date(app_engine, second, b, now=_TICK)
    # Each tenant's lift is that tenant's row: RLS shows the other's case as absent.
    async with tenant_context(app_engine, first) as session:
        assert await CaseRepository(session).get(b.id) is None


# ---------------------------------------------------------------------------
# WK-03
# ---------------------------------------------------------------------------


async def test_wk03_a_held_lock_skips_that_tenant(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    locked, locked_user = await _tenant(app_engine, superuser_engine, seed_tenant, "WK-03a")
    free, free_user = await _tenant(app_engine, superuser_engine, seed_tenant, "WK-03b")
    held = await _waiting_case(app_engine, locked, locked_user, "held", _TODAY)
    lifted = await _waiting_case(app_engine, free, free_user, "lifted", _TODAY)

    # Another tick is "mid-wake" for ``locked``: it holds the cases-domain
    # transaction lock on a second connection until this block ends.
    async with superuser_engine.begin() as other_tick:
        await other_tick.execute(
            text("SELECT pg_advisory_xact_lock(:k)"),
            {"k": advisory_lock_key(locked, domain="cases")},
        )
        summary = await run_case_wake_tick(superuser_engine, now=_TICK)

    assert summary.tenants_seen == 2
    assert summary.tenants_skipped_locked == 1
    assert (summary.tenants_claimed, summary.cases_lifted) == (1, 1)
    assert (await _state(app_engine, locked, held.id)).state == "waiting"
    await _assert_lifted_by_date(app_engine, free, lifted, now=_TICK)


# ---------------------------------------------------------------------------
# WK-04
# ---------------------------------------------------------------------------


async def test_wk04_one_tenants_failure_is_isolated(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken, broken_user = await _tenant(app_engine, superuser_engine, seed_tenant, "WK-04a")
    healthy, healthy_user = await _tenant(app_engine, superuser_engine, seed_tenant, "WK-04b")
    fails = await _waiting_case(app_engine, broken, broken_user, "fails", _TODAY)
    fine = await _waiting_case(app_engine, healthy, healthy_user, "fine", _TODAY)

    real_lift = CaseRepository.lift_wait

    async def _lift(self: CaseRepository, case_id: UUID, **kwargs: Any) -> CaseDTO:
        if await self._active_tenant() == broken:
            raise RuntimeError("lift failed (test)")
        return await real_lift(self, case_id, **kwargs)

    monkeypatch.setattr(CaseRepository, "lift_wait", _lift)

    summary = await run_case_wake_tick(superuser_engine, now=_TICK)

    assert summary.tenants_seen == 2
    assert summary.tenants_failed == 1
    assert (summary.tenants_claimed, summary.cases_lifted) == (1, 1)
    assert (await _state(app_engine, broken, fails.id)).state == "waiting"
    await _assert_lifted_by_date(app_engine, healthy, fine, now=_TICK)


# ---------------------------------------------------------------------------
# WK-05
# ---------------------------------------------------------------------------


async def test_wk05_a_second_run_lifts_nothing(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id, user_id = await _tenant(app_engine, superuser_engine, seed_tenant, "WK-05")
    case = await _waiting_case(app_engine, tenant_id, user_id, "once", _TODAY)

    # Raw rows the program never writes: an open and a closed case each
    # carrying a past ``wait_until``. Neither is waiting, so neither is due.
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        repo = CaseRepository(session)
        stale_open = await repo.create(
            title="stale open", opened_by=user_id, opened_actor="pm", now=_T0
        )
        stale_closed = await repo.create(
            title="stale closed", opened_by=user_id, opened_actor="pm", now=_T0
        )
        await repo.close(stale_closed.id, closed_by=user_id, closing_note="done", now=_T0)
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("UPDATE cases SET wait_until = :d WHERE id IN (:a, :b)"),
            {"d": _TODAY - timedelta(days=3), "a": stale_open.id, "b": stale_closed.id},
        )

    first = await run_case_wake_tick(superuser_engine, now=_TICK)
    second = await run_case_wake_tick(superuser_engine, now=_TICK + timedelta(hours=1))

    assert (first.tenants_seen, first.cases_lifted) == (1, 1)
    assert (second.tenants_seen, second.tenants_claimed, second.cases_lifted) == (1, 1, 0)
    await _assert_lifted_by_date(app_engine, tenant_id, case, now=_TICK)
    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)
        for stale_id, state in ((stale_open.id, "open"), (stale_closed.id, "closed")):
            stale = await repo.get(stale_id)
            assert stale is not None and stale.state == state
            kinds = [e.kind for e in await repo.list_entries(stale_id)]
            assert "waiting_lifted" not in kinds


# ---------------------------------------------------------------------------
# WK-06
# ---------------------------------------------------------------------------


async def test_wk06_the_wake_day_is_the_utc_day(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id, user_id = await _tenant(app_engine, superuser_engine, seed_tenant, "WK-06")
    day = date(2026, 11, 10)
    case = await _waiting_case(app_engine, tenant_id, user_id, "the day", day)

    late_evening = datetime(2026, 11, 9, 23, 30, tzinfo=timezone.utc)
    before = await run_case_wake_tick(superuser_engine, now=late_evening)
    assert before.cases_lifted == 0
    assert (await _state(app_engine, tenant_id, case.id)).state == "waiting"

    just_after_midnight = datetime(2026, 11, 10, 0, 30, tzinfo=timezone.utc)
    after = await run_case_wake_tick(superuser_engine, now=just_after_midnight)
    assert after.cases_lifted == 1
    await _assert_lifted_by_date(app_engine, tenant_id, case, now=just_after_midnight)


# ---------------------------------------------------------------------------
# WK-07
# ---------------------------------------------------------------------------


async def test_wk07_a_case_resumed_meanwhile_is_skipped(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id, user_id = await _tenant(app_engine, superuser_engine, seed_tenant, "WK-07")
    first = await _waiting_case(app_engine, tenant_id, user_id, "first", _TODAY)
    second = await _waiting_case(app_engine, tenant_id, user_id, "second", _TODAY)

    real_lift = CaseRepository.lift_wait
    resumed: list[UUID] = []

    async def _lift(self: CaseRepository, case_id: UUID, **kwargs: Any) -> CaseDTO:
        if not resumed:
            # The PM resumes this case in a transaction of their own, committed
            # after the tick's due read and before its lift.
            resumed.append(case_id)
            async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
                await real_lift(
                    CaseRepository(session), case_id, now=_TICK, by=user_id, reason="manual"
                )
        return await real_lift(self, case_id, **kwargs)

    monkeypatch.setattr(CaseRepository, "lift_wait", _lift)

    summary = await run_case_wake_tick(superuser_engine, now=_TICK)

    assert (summary.tenants_claimed, summary.tenants_failed, summary.cases_lifted) == (1, 0, 1)
    (resumed_id,) = resumed
    lifted = second if resumed_id == first.id else first
    await _assert_lifted_by_date(app_engine, tenant_id, lifted, now=_TICK)
    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)
        by_hand = await repo.get(resumed_id)
        lifts = [e for e in await repo.list_entries(resumed_id) if e.kind == "waiting_lifted"]
    assert by_hand is not None and by_hand.state == "open"
    assert [(e.actor, e.payload["reason"]) for e in lifts] == [("pm", "manual")]
