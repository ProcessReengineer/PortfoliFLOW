# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Persistent data store backed by Postgres.

Subclasses :class:`core.data_store.DataStore` per the extension contract
documented in ``core/data_store.py``. Stores DataFrames as JSONB blobs
in the ``data_store_entries`` table — tenant-scoped, RLS-policed,
audit-grade.

PHASE 1 STATUS
==============

Implemented and unit-tested. **Not** wired into :func:`core.data_store.get_data_store`
— the in-memory ``DataStore`` remains the operational default. Phase 2
will switch the factory to return a :class:`PersistentDataStore` based
on a config flag (``Settings.persistence_backend``); Phase 1 only
proves the substrate works.

Design notes
============

- The constructor takes an :class:`AsyncEngine` and a tenant-scoping
  context manager (defaults to :func:`core.repositories.tenant_context`).
  Every method that touches the DB acquires its own scoped session,
  performs its work, and commits — there is no per-instance session
  state. This matches the synchronous ``DataStore`` API shape:
  callers see ``store(...)``, ``get(...)``, ``list()``, ``remove(...)``,
  not ``await``.
- The DataStore base class is sync. The persistent variant uses
  ``asyncio.run`` to bridge the gap. That is fine for the in-app
  single-thread Phase-1 use case; if a Phase-2 caller is already
  inside an event loop, a separate ``async`` API will be added at
  that point. We do NOT expose async methods today because Phase 1's
  only consumers are tests.
- DataFrames are serialised via ``DataFrame.to_dict(orient='split')``
  for round-trip fidelity (preserves column order, index name, dtypes
  reasonably well). Round-trip equality on the test fixtures is the
  acceptance bar.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from uuid import UUID

import pandas as pd
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core.data_store import DataStore
from core.models.data_store_entry import DataStoreEntry
from core.repositories import tenant_context as default_tenant_context

logger = logging.getLogger(__name__)


SessionFactory = Callable[[AsyncEngine, UUID], AbstractAsyncContextManager[AsyncSession]]


class PersistentDataStore(DataStore):
    """Postgres-backed implementation of the DataStore contract.

    All instance methods preserve the parent class' synchronous shape
    (``store(name, df)``, ``get(name) -> DataFrame | None``, ...). The
    asyncio surface is hidden behind ``asyncio.run`` calls so callers
    do not need to be aware of the engine.

    Args:
        engine: Async SQLAlchemy engine bound to Postgres.
        tenant_id: The tenant whose data this store reads and writes.
            Every operation runs inside a session scoped to this id.
        session_factory: Optional custom factory for tenant-scoped
            sessions, primarily for testing. Defaults to
            :func:`core.repositories.tenant_context`.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        tenant_id: UUID,
        session_factory: SessionFactory | None = None,
    ) -> None:
        # Intentionally do NOT call super().__init__() — the in-memory
        # dict the parent allocates is unused here. Skipping it avoids
        # a phantom in-memory cache that could mask persistence bugs.
        self._engine = engine
        self._tenant_id = tenant_id
        self._session_factory = session_factory or default_tenant_context

    # ------------------------------------------------------------------
    # Public API — same shape as :class:`DataStore`
    # ------------------------------------------------------------------

    def store(
        self,
        name: str,
        df: pd.DataFrame,
        metadata: dict | None = None,
    ) -> None:
        asyncio.run(self._store_async(name, df, metadata))
        logger.debug(
            "PersistentDataStore.store: '%s' stored (%d rows × %d cols).",
            name,
            df.shape[0],
            df.shape[1],
        )

    def get(self, name: str) -> pd.DataFrame | None:
        df = asyncio.run(self._get_async(name))
        if df is None:
            logger.debug("PersistentDataStore.get: '%s' not found.", name)
            return None
        logger.debug(
            "PersistentDataStore.get: '%s' retrieved (%d rows × %d cols).",
            name,
            df.shape[0],
            df.shape[1],
        )
        return df

    def get_metadata(self, name: str) -> dict | None:
        return asyncio.run(self._get_metadata_async(name))

    def list(self) -> list[dict]:
        return asyncio.run(self._list_async())

    def remove(self, name: str) -> bool:
        removed = asyncio.run(self._remove_async(name))
        logger.debug(
            "PersistentDataStore.remove: '%s' %s.",
            name,
            "removed" if removed else "not found",
        )
        return removed

    def clear(self) -> None:
        count = asyncio.run(self._clear_async())
        logger.debug("PersistentDataStore.clear: %d datasets removed.", count)

    # ------------------------------------------------------------------
    # Async implementations
    # ------------------------------------------------------------------

    async def _store_async(self, name: str, df: pd.DataFrame, metadata: dict | None) -> None:
        payload = _df_to_jsonb(df)
        meta_payload = dict(metadata) if metadata is not None else {}
        async with self._session_factory(self._engine, self._tenant_id) as session:
            stmt = pg_insert(DataStoreEntry).values(
                tenant_id=self._tenant_id,
                name=name,
                data=payload,
                meta=meta_payload,
            )
            # ON CONFLICT (tenant_id, name) DO UPDATE — equivalent to
            # the in-memory store's "replace existing" semantics.
            stmt = stmt.on_conflict_do_update(
                index_elements=["tenant_id", "name"],
                set_={
                    "data": payload,
                    "meta": meta_payload,
                    "updated_at": text("NOW()"),
                },
            )
            await session.execute(stmt)

    async def _get_async(self, name: str) -> pd.DataFrame | None:
        async with self._session_factory(self._engine, self._tenant_id) as session:
            result = await session.execute(
                select(DataStoreEntry.data).where(DataStoreEntry.name == name)
            )
            row = result.scalar_one_or_none()
        if row is None:
            return None
        return _jsonb_to_df(row)

    async def _get_metadata_async(self, name: str) -> dict | None:
        async with self._session_factory(self._engine, self._tenant_id) as session:
            result = await session.execute(
                select(DataStoreEntry.meta).where(DataStoreEntry.name == name)
            )
            row = result.scalar_one_or_none()
        return None if row is None else dict(row)

    async def _list_async(self) -> list[dict]:
        async with self._session_factory(self._engine, self._tenant_id) as session:
            result = await session.execute(select(DataStoreEntry))
            entries = result.scalars().all()
        summaries: list[dict] = []
        for entry in entries:
            df = _jsonb_to_df(entry.data)
            summaries.append(
                {
                    "name": entry.name,
                    "shape": df.shape,
                    "columns": list(df.columns),
                    "dtype_summary": {col: str(dtype) for col, dtype in df.dtypes.items()},
                    "metadata": dict(entry.meta or {}),
                }
            )
        return summaries

    async def _remove_async(self, name: str) -> bool:
        async with self._session_factory(self._engine, self._tenant_id) as session:
            result = await session.execute(
                select(DataStoreEntry).where(DataStoreEntry.name == name)
            )
            entry = result.scalar_one_or_none()
            if entry is None:
                return False
            await session.delete(entry)
        return True

    async def _clear_async(self) -> int:
        async with self._session_factory(self._engine, self._tenant_id) as session:
            result = await session.execute(select(DataStoreEntry))
            entries = result.scalars().all()
            for entry in entries:
                await session.delete(entry)
        return len(entries)


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _df_to_jsonb(df: pd.DataFrame) -> dict:
    """Serialise a DataFrame to a JSONB-friendly dict.

    ``orient='split'`` is the most round-trip-faithful built-in
    pandas format: it preserves column order, the index, and the
    column names as separate fields. JSON cannot losslessly carry
    every numpy dtype, but for the DataFrames PortfoliFLOW handles
    (numeric NAVs and returns, datetime indexes) it is sufficient.
    """
    record = df.to_dict(orient="split")
    # to_dict("split") leaves index/values as numpy types in some
    # pandas versions; round-trip via json-pure types ensures the
    # JSONB driver does not choke.
    return {
        "index": [_jsonable(v) for v in record.get("index", [])],
        "columns": list(record.get("columns", [])),
        "data": [[_jsonable(v) for v in row] for row in record.get("data", [])],
    }


def _jsonb_to_df(payload: dict) -> pd.DataFrame:
    """Reconstruct a DataFrame from the dict produced by ``_df_to_jsonb``."""
    return pd.DataFrame(
        data=payload.get("data", []),
        index=payload.get("index", []),
        columns=payload.get("columns", []),
    )


def _jsonable(value: object) -> object:
    """Coerce numpy / pandas scalars to plain JSON-friendly types."""
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item"):  # numpy scalars
        try:
            return value.item()
        except (ValueError, TypeError):
            pass
    return value
