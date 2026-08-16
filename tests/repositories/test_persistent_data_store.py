# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Round-trip tests for :class:`core.persistent_data_store.PersistentDataStore`.

Verifies the substrate works end-to-end against the live compose DB:
the same DataFrame flows in and back out, RLS isolates DataFrames
between tenants, and the standard remove/list operations work under a
tenant scope.

These tests use ``asyncio.run`` indirectly through the persistent store
itself. They live alongside the other repository tests because they
share the ``app_engine`` / ``superuser_engine`` / ``seed_tenant`` /
``reset_schema`` fixtures from ``tests/repositories/conftest.py``.

Note: the persistent store calls ``asyncio.run`` internally, which
cannot be invoked from inside an already-running event loop. The
``pytest-asyncio`` ``auto`` mode wraps every test function in a loop —
so each test below uses ``asyncio.to_thread`` to run the synchronous
store calls on a worker thread that has no current loop. This mirrors
how a future Phase-2 sync caller (e.g. a PyQt6 widget) would invoke
the store.
"""

from __future__ import annotations

import asyncio

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncEngine

from core.persistent_data_store import PersistentDataStore


def _sample_df() -> pd.DataFrame:
    idx = pd.date_range("2020-01-31", periods=3, freq="ME")
    return pd.DataFrame(
        {"Fund_A": [0.01, 0.02, -0.01], "Fund_B": [0.03, -0.02, 0.04]},
        index=idx,
    )


# ---------------------------------------------------------------------------
# B-08: store / get round-trips a DataFrame unchanged
# ---------------------------------------------------------------------------


async def test_b08_round_trip_preserves_dataframe(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    store = PersistentDataStore(app_engine, tenant_id)
    df = _sample_df()

    await asyncio.to_thread(store.store, "returns", df)
    retrieved = await asyncio.to_thread(store.get, "returns")

    assert retrieved is not None
    # JSONB round-trip turns datetime indexes into ISO-8601 strings;
    # canonicalise both sides for the equality check. PortfoliFLOW
    # does not depend on dtype identity for stored DataFrames — it
    # depends on values, columns, and index labels matching.
    expected = df.copy()
    expected.index = [t.isoformat() for t in expected.index]
    pd.testing.assert_frame_equal(retrieved, expected, check_dtype=False)


async def test_b08_metadata_round_trips(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    store = PersistentDataStore(app_engine, tenant_id)
    df = _sample_df()
    meta = {"source": "alpha.xlsx", "imported_at": "2026-05-03T12:00:00"}

    await asyncio.to_thread(store.store, "returns", df, meta)
    retrieved_meta = await asyncio.to_thread(store.get_metadata, "returns")

    assert retrieved_meta == meta


async def test_b08_store_replaces_existing_entry(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    store = PersistentDataStore(app_engine, tenant_id)
    df1 = _sample_df()
    df2 = df1 * 10.0

    await asyncio.to_thread(store.store, "returns", df1)
    await asyncio.to_thread(store.store, "returns", df2)
    retrieved = await asyncio.to_thread(store.get, "returns")

    assert retrieved is not None
    expected = df2.copy()
    expected.index = [t.isoformat() for t in expected.index]
    pd.testing.assert_frame_equal(retrieved, expected, check_dtype=False)


# ---------------------------------------------------------------------------
# B-09: RLS isolation — two tenants storing under the same name don't collide
# ---------------------------------------------------------------------------


async def test_b09_tenants_with_same_name_do_not_collide(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_a = await seed_tenant(name="Tenant A")
    tenant_b = await seed_tenant(name="Tenant B")

    store_a = PersistentDataStore(app_engine, tenant_a)
    store_b = PersistentDataStore(app_engine, tenant_b)

    df_a = _sample_df()
    df_b = df_a * -1.0

    await asyncio.to_thread(store_a.store, "returns", df_a)
    await asyncio.to_thread(store_b.store, "returns", df_b)

    retrieved_a = await asyncio.to_thread(store_a.get, "returns")
    retrieved_b = await asyncio.to_thread(store_b.get, "returns")

    assert retrieved_a is not None
    assert retrieved_b is not None

    expected_a = df_a.copy()
    expected_a.index = [t.isoformat() for t in expected_a.index]
    expected_b = df_b.copy()
    expected_b.index = [t.isoformat() for t in expected_b.index]
    pd.testing.assert_frame_equal(retrieved_a, expected_a, check_dtype=False)
    pd.testing.assert_frame_equal(retrieved_b, expected_b, check_dtype=False)

    # And the unique constraint plus RLS isolation means each tenant
    # sees exactly one entry, not the other tenant's row.
    list_a = await asyncio.to_thread(store_a.list)
    list_b = await asyncio.to_thread(store_b.list)
    assert [s["name"] for s in list_a] == ["returns"]
    assert [s["name"] for s in list_b] == ["returns"]


# ---------------------------------------------------------------------------
# B-10: list and remove work under tenant scope
# ---------------------------------------------------------------------------


async def test_b10_list_summaries(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    store = PersistentDataStore(app_engine, tenant_id)
    df = _sample_df()

    await asyncio.to_thread(store.store, "alpha", df, {"source": "alpha.xlsx"})
    await asyncio.to_thread(store.store, "beta", df)

    summaries = await asyncio.to_thread(store.list)
    by_name = {s["name"]: s for s in summaries}
    assert set(by_name) == {"alpha", "beta"}
    for s in summaries:
        assert s["shape"] == (3, 2)
        assert set(s["columns"]) == {"Fund_A", "Fund_B"}
    assert by_name["alpha"]["metadata"] == {"source": "alpha.xlsx"}
    assert by_name["beta"]["metadata"] == {}


async def test_b10_remove_existing_returns_true(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    store = PersistentDataStore(app_engine, tenant_id)
    df = _sample_df()

    await asyncio.to_thread(store.store, "to_delete", df)

    removed = await asyncio.to_thread(store.remove, "to_delete")
    assert removed is True
    after = await asyncio.to_thread(store.get, "to_delete")
    assert after is None


async def test_b10_remove_nonexistent_returns_false(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    store = PersistentDataStore(app_engine, tenant_id)
    removed = await asyncio.to_thread(store.remove, "never_stored")
    assert removed is False


async def test_b10_clear_empties_tenants_data_only(app_engine: AsyncEngine, seed_tenant) -> None:
    """clear() under tenant A must NOT touch tenant B's data."""
    tenant_a = await seed_tenant(name="A")
    tenant_b = await seed_tenant(name="B")
    store_a = PersistentDataStore(app_engine, tenant_a)
    store_b = PersistentDataStore(app_engine, tenant_b)
    df = _sample_df()

    await asyncio.to_thread(store_a.store, "shared_name", df)
    await asyncio.to_thread(store_b.store, "shared_name", df)

    await asyncio.to_thread(store_a.clear)

    assert await asyncio.to_thread(store_a.list) == []
    list_b = await asyncio.to_thread(store_b.list)
    assert [s["name"] for s in list_b] == ["shared_name"]
