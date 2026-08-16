# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for core.data_store — the PortfoliFLOW application-wide DataStore."""

from __future__ import annotations

import pandas as pd
import pytest

from core.data_store import DataStore, get_data_store


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store() -> DataStore:
    """Return a fresh DataStore (not the singleton) for isolation."""
    return DataStore()


@pytest.fixture()
def sample_df() -> pd.DataFrame:
    idx = pd.date_range("2020-01", periods=3, freq="ME")
    return pd.DataFrame({"Fund_A": [0.01, 0.02, -0.01], "Fund_B": [0.03, -0.02, 0.04]}, index=idx)


# ---------------------------------------------------------------------------
# store / get
# ---------------------------------------------------------------------------


def test_store_and_retrieve_returns_copy(store: DataStore, sample_df: pd.DataFrame) -> None:
    """Stored DataFrame must be a copy — not the same object."""
    store.store("test", sample_df)
    retrieved = store.get("test")
    assert retrieved is not None
    assert retrieved is not sample_df
    pd.testing.assert_frame_equal(retrieved, sample_df)


def test_get_nonexistent_returns_none(store: DataStore) -> None:
    assert store.get("does_not_exist") is None


def test_store_replaces_existing(store: DataStore, sample_df: pd.DataFrame) -> None:
    """Storing under an existing name replaces the old entry."""
    store.store("test", sample_df)
    new_df = sample_df * 2
    store.store("test", new_df)
    retrieved = store.get("test")
    assert retrieved is not None
    pd.testing.assert_frame_equal(retrieved, new_df)


def test_retrieved_copy_does_not_mutate_store(store: DataStore, sample_df: pd.DataFrame) -> None:
    """Modifying the returned copy must not affect the stored data."""
    store.store("test", sample_df)
    retrieved = store.get("test")
    assert retrieved is not None
    retrieved.iloc[0, 0] = 999.0
    original = store.get("test")
    assert original is not None
    assert original.iloc[0, 0] != 999.0


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def test_store_with_metadata(store: DataStore, sample_df: pd.DataFrame) -> None:
    meta = {"source": "/tmp/file.xlsx", "import_time": "2024-01-01T00:00:00"}
    store.store("test", sample_df, metadata=meta)
    retrieved_meta = store.get_metadata("test")
    assert retrieved_meta == meta


def test_get_metadata_nonexistent_returns_none(store: DataStore) -> None:
    assert store.get_metadata("does_not_exist") is None


def test_store_without_metadata_returns_empty_dict(
    store: DataStore, sample_df: pd.DataFrame
) -> None:
    store.store("test", sample_df)
    assert store.get_metadata("test") == {}


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_returns_correct_summaries(store: DataStore, sample_df: pd.DataFrame) -> None:
    store.store("alpha", sample_df, metadata={"source": "alpha.xlsx"})
    store.store("beta", sample_df)
    summaries = store.list()
    assert len(summaries) == 2
    names = {s["name"] for s in summaries}
    assert names == {"alpha", "beta"}
    for s in summaries:
        assert s["shape"] == (3, 2)
        assert set(s["columns"]) == {"Fund_A", "Fund_B"}
        assert "dtype_summary" in s
        assert "metadata" in s


def test_list_empty_store(store: DataStore) -> None:
    assert store.list() == []


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def test_remove_existing_returns_true(store: DataStore, sample_df: pd.DataFrame) -> None:
    store.store("test", sample_df)
    assert store.remove("test") is True
    assert store.get("test") is None


def test_remove_nonexistent_returns_false(store: DataStore) -> None:
    assert store.remove("does_not_exist") is False


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------


def test_clear_empties_store(store: DataStore, sample_df: pd.DataFrame) -> None:
    store.store("a", sample_df)
    store.store("b", sample_df)
    store.clear()
    assert store.list() == []
    assert store.get("a") is None


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


def test_get_data_store_returns_same_instance() -> None:
    """get_data_store() must return the same object on repeated calls."""
    s1 = get_data_store()
    s2 = get_data_store()
    assert s1 is s2
