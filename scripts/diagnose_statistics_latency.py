# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Standalone diagnostic — Statistics service latency breakdown.

NOT COMMITTED. Produced for a one-off measurement during
sub-stream 6F-3b polish in 2026-05.

Usage from project root:
    python scripts/diagnose_statistics_latency.py
    python scripts/diagnose_statistics_latency.py --tenant-id <uuid>
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from collections import defaultdict
from pathlib import Path
from uuid import UUID

from dotenv import load_dotenv
from sqlalchemy import NullPool, event, text
from sqlalchemy.ext.asyncio import create_async_engine

from core.repositories._session import tenant_context
from core.repositories.fx_rate_repository import FxRateRepository
from core.repositories.investment_nav_repository import (
    InvestmentNavRepository,
)
from core.repositories.investment_repository import InvestmentRepository
from core.repositories.tenant_repository import TenantRepository
from services.statistics import StatisticsService


load_dotenv(Path(__file__).resolve().parents[1] / ".env")


async def resolve_tenant_id(engine, override: str | None) -> UUID:
    if override:
        return UUID(override)
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT id FROM tenants ORDER BY created_at ASC LIMIT 1"))
        row = result.first()
        if row is None:
            raise RuntimeError("No tenants found — pass --tenant-id explicitly.")
        return row.id


async def run_diagnostic(tenant_id: UUID) -> None:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL not set in .env")
    engine = create_async_engine(db_url, future=True, poolclass=NullPool)

    # --- DB-time / statement-count instrumentation ------------------
    db_time_ms = 0.0
    stmt_count = 0
    stmt_kinds: dict[str, int] = defaultdict(int)
    # Use a stack since SQLAlchemy fires before/after pairs in
    # nested patterns (e.g. SAVEPOINTs).
    t0_stack: list[float] = []

    def before_exec(conn, cursor, statement, parameters, context, executemany):
        t0_stack.append(time.perf_counter())

    def after_exec(conn, cursor, statement, parameters, context, executemany):
        nonlocal db_time_ms, stmt_count
        if t0_stack:
            t0 = t0_stack.pop()
            db_time_ms += (time.perf_counter() - t0) * 1000.0
        stmt_count += 1
        s = statement.strip().lower()
        if "from investments " in s or "from investments\n" in s:
            stmt_kinds["investments"] += 1
        elif "from investment_navs" in s:
            stmt_kinds["investment_navs"] += 1
        elif "from sessions" in s:
            stmt_kinds["sessions"] += 1
        elif "set_config" in s or "select set_config" in s:
            stmt_kinds["set_config"] += 1
        else:
            stmt_kinds["other"] += 1

    sync_engine = engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", before_exec)
    event.listen(sync_engine, "after_cursor_execute", after_exec)

    # --- Analytics timing via monkey-patch --------------------------
    from services.analytics import (
        compute_correlation_matrix,
        compute_full_distribution_stats,
        compute_risk_metrics,
        compute_total_return_series,
    )
    import services.statistics.statistics_service as svc_mod

    analytics_time_ms: dict[str, float] = defaultdict(float)
    analytics_calls: dict[str, int] = defaultdict(int)

    def wrap(name, original):
        def wrapped(*a, **kw):
            t0 = time.perf_counter()
            try:
                return original(*a, **kw)
            finally:
                analytics_time_ms[name] += (time.perf_counter() - t0) * 1000.0
                analytics_calls[name] += 1

        return wrapped

    svc_mod.compute_total_return_series = wrap(
        "compute_total_return_series", compute_total_return_series
    )
    svc_mod.compute_correlation_matrix = wrap(
        "compute_correlation_matrix", compute_correlation_matrix
    )
    svc_mod.compute_full_distribution_stats = wrap(
        "compute_full_distribution_stats", compute_full_distribution_stats
    )
    svc_mod.compute_risk_metrics = wrap("compute_risk_metrics", compute_risk_metrics)

    # --- Warm-up call (primes pandas, pool, ORM compilation) --------
    async with tenant_context(engine, tenant_id) as session:
        service = StatisticsService(
            investments=InvestmentRepository(session),
            navs=InvestmentNavRepository(session),
            tenants=TenantRepository(session),
            fx_rates=FxRateRepository(session),
        )
        await service.get_universe_statistics()

    # --- Three timed runs ------------------------------------------
    timings_ms: list[float] = []
    per_run_db_ms: list[float] = []
    per_run_stmts: list[int] = []
    per_run_analytics_ms: list[dict[str, float]] = []
    per_run_analytics_calls: list[dict[str, int]] = []
    investment_count = 0

    for run_idx in range(3):
        # Reset per-run accumulators so each run reports its own
        # numbers cleanly. Keep stmt_kinds cumulative.
        db_time_ms = 0.0
        stmt_count = 0
        for k in list(analytics_time_ms.keys()):
            analytics_time_ms[k] = 0.0
        for k in list(analytics_calls.keys()):
            analytics_calls[k] = 0
        t0_stack.clear()

        async with tenant_context(engine, tenant_id) as session:
            service = StatisticsService(
                investments=InvestmentRepository(session),
                navs=InvestmentNavRepository(session),
                tenants=TenantRepository(session),
                fx_rates=FxRateRepository(session),
            )
            t_total = time.perf_counter()
            bundle = await service.get_universe_statistics()
            total_ms = (time.perf_counter() - t_total) * 1000.0

            if run_idx == 0:
                investment_count = len(bundle.investment_names)

        timings_ms.append(total_ms)
        per_run_db_ms.append(db_time_ms)
        per_run_stmts.append(stmt_count)
        per_run_analytics_ms.append(dict(analytics_time_ms))
        per_run_analytics_calls.append(dict(analytics_calls))
        print(
            f"Run {run_idx + 1}: total={total_ms:7.1f} ms  "
            f"db={db_time_ms:7.1f} ms  stmts={stmt_count:4d}  "
            f"non-db={total_ms - db_time_ms:7.1f} ms"
        )

    # --- NAV row counts (untimed, separate context) -----------------
    nav_counts: dict[str, int] = {}
    async with tenant_context(engine, tenant_id) as session:
        inv_repo = InvestmentRepository(session)
        nav_repo = InvestmentNavRepository(session)
        invs = await inv_repo.list_active()
        for inv in invs:
            navs = await nav_repo.list_by_investment_and_kind(inv.id, "actual")
            nav_counts[inv.name] = len(navs)

    # --- Report -----------------------------------------------------
    median = sorted(timings_ms)[1]
    # Use the last run for the detail breakdown (steady state).
    final_db = per_run_db_ms[-1]
    final_stmts = per_run_stmts[-1]
    final_analytics_ms = per_run_analytics_ms[-1]
    final_analytics_calls = per_run_analytics_calls[-1]
    final_non_db = timings_ms[-1] - final_db

    print()
    print("=== StatisticsService.get_universe_statistics() diagnostic ===")
    print(f"Tenant id:                {tenant_id}")
    print(f"Investments in universe:  {investment_count}")
    print()
    print(f"T1  Median wall-clock:    {median:7.1f} ms")
    print(
        f"T2  DB time (last run):   {final_db:7.1f} ms "
        f"({(final_db / timings_ms[-1] * 100):.1f}% of last run)"
    )
    print(f"T3  SQL statements:       {final_stmts}")
    print(
        f"T4  Non-DB time:          {final_non_db:7.1f} ms "
        f"({(final_non_db / timings_ms[-1] * 100):.1f}% of last run)"
    )
    print()
    print("    -- Analytics breakdown (last run) --")
    for name in (
        "compute_total_return_series",
        "compute_correlation_matrix",
        "compute_full_distribution_stats",
        "compute_risk_metrics",
    ):
        print(
            f"    {name:40s}  "
            f"(called {final_analytics_calls.get(name, 0):3d} times):  "
            f"{final_analytics_ms.get(name, 0.0):7.1f} ms"
        )
    print()
    print("    -- Statement kinds (cumulative across warm-up + 3 runs) --")
    for kind, count in sorted(stmt_kinds.items(), key=lambda kv: -kv[1]):
        print(f"    {kind:30s}: {count}")
    print()
    print("    -- NAV row counts per investment (actual) --")
    for name, count in sorted(nav_counts.items()):
        print(f"    {name:40s}: {count:5d} rows")

    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-id", default=None)
    args = parser.parse_args()

    async def go():
        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            raise RuntimeError("DATABASE_URL not set in .env")
        engine = create_async_engine(db_url, future=True, poolclass=NullPool)
        try:
            tenant_id = await resolve_tenant_id(engine, args.tenant_id)
        finally:
            await engine.dispose()
        await run_diagnostic(tenant_id)

    asyncio.run(go())


if __name__ == "__main__":
    main()
