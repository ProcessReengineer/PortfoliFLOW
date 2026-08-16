# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

# PortfoliFLOW — Data Import Module
"""Front Office — Data Import module (registry shell).

Purpose and responsibility:
    This module registers the :class:`DataImport` unit of business logic that
    loads an external portfolio workbook into PortfoliFLOW and persists the
    result into the :class:`~core.data_store.DataStore` singleton.

Relocation note (A5):
    The workbook-parsing implementation — :func:`load_excel`,
    :func:`validate_dataframe`, :func:`validate_workbook`, and their private
    helpers — was relocated to
    :mod:`services.data_normalization.excel_workbook_loader`. That services
    module is the canonical home of the parsing path (``docs/architecture.md``
    names ``services/data_normalization/`` as the shared parser), which lets
    the web surface consume it without ``web/`` importing from ``modules/``.
    The names are re-exported below for backward-compatible imports pending
    test migration.

DataStore integration:
    :class:`DataImport` (the registry-registered class) stores every loaded
    dataset in the :class:`~core.data_store.DataStore` singleton so that
    modules in other areas can access the data without importing this module
    directly.

Usage examples::

    # Programmatic (CLI / headless) — canonical import path
    from services.data_normalization.excel_workbook_loader import load_excel
    datasets = load_excel("data/sample/PortfoliFLOW_Test_V2.xlsx")
    navs = datasets["navs_actual"]
    print(navs.shape)       # (n_dates, n_investments)
    print(navs.index.name)  # 'Date'

    # Via the module registry
    from modules.module_registry import registry
    from core.config import get_config
    cls = registry.get("data_import")
    mod = cls(config=get_config())
    result = mod.run(action="load_excel", source="data/sample/PortfoliFLOW_Test_V2.xlsx")
    navs = result["datasets"]["navs_actual"]
"""

from __future__ import annotations

import pathlib
from typing import Any

import pandas as pd

from core.base_module import BaseModule
from core.data_store import get_data_store
from core.exceptions import DataImportError, ValidationError
from modules.module_registry import registry

# Backward-compatible re-exports. The canonical home of the workbook parser is
# now ``services/data_normalization/excel_workbook_loader.py`` (A5). These
# aliases keep existing importers working pending test migration; new code
# should import from the services path directly.
from services.data_normalization.excel_workbook_loader import (  # noqa: F401
    load_excel,
    validate_dataframe,
    validate_workbook,
)

__all__ = ["load_excel", "validate_dataframe", "validate_workbook", "DataImport"]


# ---------------------------------------------------------------------------
# BaseModule subclass — registry integration and module entry point
# ---------------------------------------------------------------------------


@registry.register
class DataImport(BaseModule):
    """Import and normalise portfolio data from external Excel workbooks.

    This class wraps the :func:`load_excel`, :func:`validate_dataframe`,
    and :func:`validate_workbook` functions (relocated to
    :mod:`services.data_normalization.excel_workbook_loader`) and exposes them
    through the standard PortfoliFLOW module lifecycle
    (``validate_inputs`` → ``run``).

    After a successful load, all datasets are stored in the application-wide
    :class:`~core.data_store.DataStore` so that every other module can access
    them without importing this module directly.

    Attributes:
        module_name: ``"data_import"``
        module_area: ``"front_office"``
    """

    module_name = "data_import"
    module_area = "front_office"

    # ------------------------------------------------------------------
    # BaseModule interface
    # ------------------------------------------------------------------

    def validate_inputs(self, **kwargs: Any) -> None:
        """Validate keyword arguments before ``run()`` proceeds.

        Args:
            **kwargs: Same keyword arguments as :meth:`run`.

        Raises:
            ValidationError: If ``action="load_excel"`` is requested but
                ``source`` is not provided.
        """
        action = kwargs.get("action", "load_excel")
        if action == "load_excel":
            if not kwargs.get("source"):
                raise ValidationError(
                    "'source' (file path) is required for action 'load_excel'.",
                    field="source",
                )

    def run(self, *args: Any, **kwargs: Any) -> dict:
        """Execute the data import workflow.

        Dispatches based on the ``action`` keyword argument.  After loading,
        every dataset is stored in the :class:`~core.data_store.DataStore` under
        its canonical key so downstream modules can retrieve it via
        ``get_data_store().get("navs_actual")``, etc.

        Keyword Args:
            action (str): Currently only ``"load_excel"`` is supported.
                Defaults to ``"load_excel"``.
            source (str): File path to the ``.xlsx`` file.
            sheets (list[str] | None): Optional list of sheet names to load.
                If omitted, all recognised sheets are loaded.

        Returns:
            dict with:

            * ``"status"``: ``"ok"`` or ``"error"``.
            * ``"datasets"``: ``dict[str, pd.DataFrame]`` — one entry per
              loaded sheet, keyed by canonical name.
            * ``"metadata"``: dict with ``source``, ``n_datasets``, ``keys``.

        Raises:
            DataImportError: If the file cannot be read or parsed.
            ValidationError: If ``validate_inputs`` fails or a sheet fails
                structural validation.
        """
        action = kwargs.get("action", "load_excel")

        if action == "load_excel":
            self.validate_inputs(**kwargs)
            source = pathlib.Path(kwargs["source"])
            sheets: list[str] | None = kwargs.get("sheets", None)

            datasets = load_excel(source, sheets=sheets)

            # Persist every dataset so other modules can read them immediately
            store = get_data_store()
            for key, df in datasets.items():
                store.store(key, df, metadata={"source": str(source)})

            self._logger.info(
                "run(action='load_excel') completed: %d datasets stored from '%s'.",
                len(datasets),
                source.name,
            )
            return {
                "status": "ok",
                "datasets": datasets,
                "metadata": {
                    "source": str(source),
                    "n_datasets": len(datasets),
                    "keys": list(datasets.keys()),
                },
            }

        raise DataImportError(
            f"Unknown action '{action}' for DataImport.run().  Supported actions: 'load_excel'."
        )

    # ------------------------------------------------------------------
    # Convenience instance methods
    # ------------------------------------------------------------------

    def load_excel(  # type: ignore[override]
        self,
        path: str | pathlib.Path,
        sheet_name: str | int | None = None,
        date_column: str | None = None,
    ) -> pd.DataFrame:
        """Load all import-format sheets, populate DataStore, and return a preview DataFrame.

        This is the primary preview entry point: the caller receives a single
        DataFrame suitable for inline preview.  All nine sheets are loaded in
        the background and stored in the DataStore so that every other module
        has immediate access.

        The ``sheet_name`` and ``date_column`` parameters are accepted for
        backward compatibility with existing wiring but are **ignored** —
        the Excel import format always loads all recognised sheets, and the
        date column is always column A.

        Args:
            path: Path to the ``.xlsx`` file.
            sheet_name: Ignored (accepted for backward compatibility).
            date_column: Ignored (accepted for backward compatibility).

        Returns:
            The ``navs_actual`` DataFrame for preview.  Falls back to the
            first available time-series sheet if ``navs_actual`` is absent, or
            to the ``attributes`` sheet if no time-series sheets were loaded.

        Raises:
            DataImportError: If the file cannot be read.
            ValidationError: If any sheet fails structural validation.
        """
        # Module-level load_excel returns dict[str, pd.DataFrame]
        datasets = load_excel(path)

        # Persist to DataStore so all other modules can read the data
        store = get_data_store()
        for key, df in datasets.items():
            store.store(key, df, metadata={"source": str(path)})

        self._logger.info(
            "load_excel(): %d datasets stored from '%s'.",
            len(datasets),
            pathlib.Path(str(path)).name,
        )

        # Return navs_actual as preview — absolute NAV values are most intuitive
        # for a quick visual sanity check
        if "navs_actual" in datasets:
            return datasets["navs_actual"]

        # Fallback: first non-attributes dataset
        for key, df in datasets.items():
            if key != "attributes":
                return df

        # Final fallback if only attributes was loaded
        return next(iter(datasets.values()))
