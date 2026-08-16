# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Orchestration tests for the shared market-data tick runner (ADR-0093).

Fully-mocked orchestration tests mirroring ``test_irene_tick_runner.py``:
every seam the tick reaches (the cross-tenant due read, the single-tenant
resolver, ``tenant_context``, the refresh core, and the schedule
repository) is monkeypatched at the ``services.scheduler.tick_runner``
module level and the engine is injected as a fake, so no live DB and no
provider network is required. They assert the tick's control flow — one
refresh per due tenant, advisory-lock skip on contention, schedule advance
on success, per-tenant error isolation, and the non-persisting
``tenant_ref`` / ``provider`` test-seam parameters.

They moved here from ``tests/cli/test_market_data_tick.py`` with the
orchestration itself (ADR-0117 §2); the CLI keeps the wrapper's own
surface — exit codes, engine lifecycle, and the ``--tenant`` / ``--provider``
flags that feed these parameters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import services.scheduler.tick_runner as tick_runner
from services.investments.live_refresh import TenantRefreshReport
from services.investments.live_schedule import DueMarketDataTenant
from services.scheduler.tick_runner import run_market_data_tick


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeConn:
    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeEngine:
    """The injected engine. Counts connects; disposal is the caller's job."""

    def __init__(self) -> None:
        self.connects = 0
        self.disposed = False

    def connect(self) -> _FakeConn:
        self.connects += 1
        return _FakeConn()

    async def dispose(self) -> None:
        self.disposed = True


class _FakeResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one(self) -> Any:
        return self._value


class _FakeSession:
    """A session whose only executed statement is the advisory-lock claim."""

    def __init__(self, claim: bool) -> None:
        self._claim = claim

    async def execute(self, statement: Any, params: Any = None) -> _FakeResult:
        return _FakeResult(self._claim)


class _FakeTenantCtx:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeSession:
        return self._session

    async def __aexit__(self, *exc: Any) -> bool:
        return False


@dataclass
class _Recorder:
    engine: _FakeEngine = field(default_factory=_FakeEngine)
    tenant_contexts: list = field(default_factory=list)
    refreshes: list = field(default_factory=list)  # (tenant_id, forced_provider)
    marks: list = field(default_factory=list)  # schedule_id
    find_due_called: bool = False
    resolve_single_called: bool = False


def _due(n: int) -> list[DueMarketDataTenant]:
    return [
        DueMarketDataTenant(
            tenant_id=uuid4(),
            schedule_id=uuid4(),
            cadence="daily",
            timezone="UTC",
            preferred_hour=6,
            last_run_at=None,
        )
        for _ in range(n)
    ]


def _install(
    monkeypatch: Any,
    *,
    due: list[DueMarketDataTenant],
    single: list[DueMarketDataTenant] | None = None,
    claims: dict | None = None,
    refresh_raises: dict | None = None,
) -> _Recorder:
    """Patch every ``services.scheduler.tick_runner`` seam and return a recorder."""
    claims = claims or {}
    refresh_raises = refresh_raises or {}
    rec = _Recorder()

    async def _fake_find_due(conn: Any) -> list[DueMarketDataTenant]:
        rec.find_due_called = True
        return due

    monkeypatch.setattr(tick_runner, "find_due_market_data_tenants", _fake_find_due)

    async def _fake_resolve_single(conn: Any, tenant_ref: str):
        rec.resolve_single_called = True
        return single or []

    monkeypatch.setattr(tick_runner, "_resolve_single_tenant", _fake_resolve_single)

    all_due = list(due) + list(single or [])
    claim_by_tenant = {d.tenant_id: claims.get(d.tenant_id, True) for d in all_due}

    def _fake_tenant_context(engine: Any, tenant_id: Any, *a: Any, **k: Any):
        rec.tenant_contexts.append(tenant_id)
        return _FakeTenantCtx(_FakeSession(claim_by_tenant.get(tenant_id, True)))

    monkeypatch.setattr(tick_runner, "tenant_context", _fake_tenant_context)

    async def _fake_refresh(
        session: Any,
        *,
        now: Any,
        last_run_at: Any,
        forced_provider: Any = None,
    ) -> TenantRefreshReport:
        # The refresh runs inside the just-entered tenant context, so the
        # most-recently-opened tenant is the one being refreshed.
        current_tenant = rec.tenant_contexts[-1]
        rec.refreshes.append((current_tenant, forced_provider))
        if current_tenant in refresh_raises:
            raise refresh_raises[current_tenant]
        return TenantRefreshReport(considered=1, refreshed=1, inserted=1)

    monkeypatch.setattr(tick_runner, "refresh_tenant_live_data", _fake_refresh)

    class _FakeRepo:
        def __init__(self, session: Any) -> None: ...

        async def mark_run_done(
            self, *, schedule_id: Any, last_run_at: Any, next_due_at: Any
        ) -> None:
            rec.marks.append(schedule_id)

    monkeypatch.setattr(tick_runner, "MarketDataScheduleRepository", _FakeRepo)

    return rec


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_no_due_tenants_no_refresh(monkeypatch: Any) -> None:
    rec = _install(monkeypatch, due=[])
    summary = await run_market_data_tick(rec.engine)
    assert rec.refreshes == []
    assert rec.marks == []
    assert summary.due == 0 and summary.refreshed == 0
    # The engine was used for the due read — and left to its owner to
    # dispose of (the web host's engine outlives the tick, ADR-0117 §2).
    assert rec.engine.connects == 1
    assert rec.engine.disposed is False


async def test_two_due_tenants_two_refreshes_and_advances(monkeypatch: Any) -> None:
    due = _due(2)
    rec = _install(monkeypatch, due=due)
    summary = await run_market_data_tick(rec.engine)
    assert [t for t, _ in rec.refreshes] == [d.tenant_id for d in due]
    assert rec.tenant_contexts == [d.tenant_id for d in due]
    # Production path (no test-seam parameters) advances the schedule for
    # each success.
    assert rec.marks == [d.schedule_id for d in due]
    # No provider forced on the production path.
    assert all(p is None for _, p in rec.refreshes)
    assert (summary.due, summary.refreshed, summary.errors) == (2, 2, 0)


async def test_advisory_lock_contention_skips_that_tenant(monkeypatch: Any) -> None:
    due = _due(2)
    claims = {due[1].tenant_id: False}
    rec = _install(monkeypatch, due=due, claims=claims)
    summary = await run_market_data_tick(rec.engine)
    # Only the first tenant is refreshed; the second is skipped, not blocked.
    assert [t for t, _ in rec.refreshes] == [due[0].tenant_id]
    assert rec.marks == [due[0].schedule_id]
    # Both tenants still opened a context (the claim happens inside it).
    assert rec.tenant_contexts == [d.tenant_id for d in due]
    assert (summary.refreshed, summary.skipped) == (1, 1)


async def test_refresh_error_isolated_does_not_advance_or_fail_tick(
    monkeypatch: Any,
) -> None:
    due = _due(2)
    raises = {due[0].tenant_id: RuntimeError("provider down")}
    rec = _install(monkeypatch, due=due, refresh_raises=raises)
    # A single tenant's failure does not fail the tick.
    summary = await run_market_data_tick(rec.engine)
    # Both were attempted; only the successful one advanced its schedule.
    assert [t for t, _ in rec.refreshes] == [d.tenant_id for d in due]
    assert rec.marks == [due[1].schedule_id]
    assert (summary.refreshed, summary.errors) == (1, 1)


async def test_tenant_ref_limits_scope_and_bypasses_due(monkeypatch: Any) -> None:
    single = _due(1)
    rec = _install(monkeypatch, due=_due(3), single=single)
    await run_market_data_tick(rec.engine, tenant_ref="minathena-capital")
    # The cross-tenant due read is bypassed; only the resolved tenant runs.
    assert rec.find_due_called is False
    assert rec.resolve_single_called is True
    assert [t for t, _ in rec.refreshes] == [single[0].tenant_id]
    # Test seam does not persist schedule state (no advance).
    assert rec.marks == []


async def test_provider_forces_routing_and_does_not_persist(monkeypatch: Any) -> None:
    due = _due(1)
    rec = _install(monkeypatch, due=due)
    await run_market_data_tick(rec.engine, provider="synthetic")
    # The forced provider is threaded into the refresh core.
    assert rec.refreshes == [(due[0].tenant_id, "synthetic")]
    # A test-seam parameter was passed → no schedule advance.
    assert rec.marks == []
