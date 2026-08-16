# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Application-wide in-memory DataStore for PortfoliFLOW.

This module provides :class:`DataStore`, a singleton store for named
DataFrames.  Data imported in one module (e.g. Front Office data_import)
is immediately accessible to all other modules, regardless of their area.

Current implementation: in-memory dict.  Data does NOT persist between
application sessions.

Future extension:
    To add persistence, subclass DataStore and override the storage
    methods to write to Parquet/HDF5/SQLite.  The public API (store,
    get, list, remove) remains unchanged.  Modules that use the
    DataStore do not need modification.  The get_data_store() factory
    function should then return the persistent subclass based on a
    config flag (e.g., Settings.persistence_backend).
"""

from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger(__name__)

_instance: DataStore | None = None


class DataStore:
    """In-memory store for named DataFrames, accessible across all modules.

    This store holds DataFrames that have been imported or computed by any
    module.  All modules can read from and write to the store, enabling
    cross-group data sharing (e.g., data imported in Front Office is
    available to Back Office modules).

    Current implementation: in-memory dict.  Data does NOT persist between
    application sessions.

    Future extension:
        To add persistence, subclass DataStore and override the storage
        methods to write to Parquet/HDF5/SQLite.  The public API (store,
        get, list, remove) remains unchanged.  Modules that use the
        DataStore do not need modification.  The get_data_store() factory
        function should then return the persistent subclass based on a
        config flag (e.g., Settings.persistence_backend).
    """

    def __init__(self) -> None:
        self._data: dict[str, pd.DataFrame] = {}
        self._metadata: dict[str, dict] = {}

    def store(
        self,
        name: str,
        df: pd.DataFrame,
        metadata: dict | None = None,
    ) -> None:
        """Store a DataFrame under the given name, replacing any existing entry.

        Args:
            name: Unique identifier for this dataset (e.g., "imported_returns",
                "optimized_weights").  Convention: snake_case.
            df: The DataFrame to store.  A copy is stored to prevent
                unintended mutation by the caller.
            metadata: Optional dict of metadata (source file path, import
                timestamp, sheet name, etc.).  Stored alongside the DataFrame.
        """
        self._data[name] = df.copy()
        self._metadata[name] = dict(metadata) if metadata is not None else {}
        logger.debug(
            "DataStore.store: '%s' stored (%d rows × %d cols).",
            name,
            df.shape[0],
            df.shape[1],
        )

    def get(self, name: str) -> pd.DataFrame | None:
        """Retrieve a stored DataFrame by name.

        Args:
            name: The identifier used when storing the DataFrame.

        Returns:
            A copy of the stored DataFrame, or None if no entry exists
            under that name.  Returns a copy to prevent mutation of the
            stored data.
        """
        df = self._data.get(name)
        if df is None:
            logger.debug("DataStore.get: '%s' not found.", name)
            return None
        logger.debug(
            "DataStore.get: '%s' retrieved (%d rows × %d cols).", name, df.shape[0], df.shape[1]
        )
        return df.copy()

    def get_metadata(self, name: str) -> dict | None:
        """Retrieve metadata for a stored dataset.

        Args:
            name: The dataset identifier.

        Returns:
            The metadata dict, or None if the dataset does not exist or
            has no metadata.
        """
        if name not in self._data:
            logger.debug("DataStore.get_metadata: '%s' not found.", name)
            return None
        return dict(self._metadata.get(name, {}))

    def list(self) -> list[dict]:
        """List all stored datasets with summary information.

        Returns:
            List of dicts, each with keys: 'name', 'shape', 'columns',
            'dtype_summary', 'metadata'.
        """
        result = []
        for name, df in self._data.items():
            result.append(
                {
                    "name": name,
                    "shape": df.shape,
                    "columns": list(df.columns),
                    "dtype_summary": {col: str(dtype) for col, dtype in df.dtypes.items()},
                    "metadata": dict(self._metadata.get(name, {})),
                }
            )
        logger.debug("DataStore.list: %d datasets.", len(result))
        return result

    def remove(self, name: str) -> bool:
        """Remove a dataset from the store.

        Args:
            name: The dataset identifier.

        Returns:
            True if the dataset was found and removed, False if it did
            not exist.
        """
        if name not in self._data:
            logger.debug("DataStore.remove: '%s' not found.", name)
            return False
        del self._data[name]
        self._metadata.pop(name, None)
        logger.debug("DataStore.remove: '%s' removed.", name)
        return True

    def clear(self) -> None:
        """Remove all datasets from the store."""
        count = len(self._data)
        self._data.clear()
        self._metadata.clear()
        logger.debug("DataStore.clear: %d datasets removed.", count)


def get_data_store() -> DataStore:
    """Return the application-wide DataStore singleton.

    The singleton is created on first call and reused on subsequent calls,
    following the same pattern as :func:`core.config.get_config`.

    Returns:
        The global :class:`DataStore` instance.
    """
    global _instance
    if _instance is None:
        _instance = DataStore()
        logger.debug("DataStore singleton created.")
    return _instance
