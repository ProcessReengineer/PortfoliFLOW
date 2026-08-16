# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""DataStore inspection tools for AI-callable use.

Three read-only tools that let the AI query what data is loaded in PortfoliFLOW:
- list_datasets: overview of all datasets
- get_dataset_summary: detailed stats for one dataset
- get_dataset_slice: filtered tabular data from one dataset

All three are registered with the ToolRegistry at import time.
"""

from __future__ import annotations

import logging

import pandas as pd

from core.data_store import get_data_store
from services.tool_classes import ToolClass
from services.tool_registry import get_tool_registry

logger = logging.getLogger(__name__)


def list_datasets() -> str:
    """List all datasets currently loaded in the DataStore.

    Returns:
        Human-readable multi-line summary of each dataset. If no datasets
        are loaded, returns a message saying so.
    """
    store = get_data_store()
    items = store.list()
    if not items:
        return (
            "No datasets are currently loaded in the DataStore. "
            "Data must be imported first via the Front Office Data Import module."
        )

    lines = []
    for item in items:
        name = item["name"]
        rows, cols = item["shape"]
        columns = item["columns"]
        metadata = item["metadata"]

        # Build column display (first 10, then summary)
        col_display = ", ".join(str(c) for c in columns[:10])
        if len(columns) > 10:
            col_display += f" ... and {len(columns) - 10} more"

        # Date range from index (requires re-fetching the DataFrame)
        df = store.get(name)
        date_range_str = ""
        if df is not None and isinstance(df.index, pd.DatetimeIndex) and len(df) > 0:
            date_range_str = f"\n  Date range: {df.index.min().date()} to {df.index.max().date()}"

        # Metadata summary
        meta_parts = []
        if metadata.get("source"):
            meta_parts.append(f"Source: {metadata['source']}")
        if metadata.get("import_time"):
            meta_parts.append(f"Imported: {metadata['import_time']}")
        meta_str = " | ".join(meta_parts)

        block = (
            f'Dataset: "{name}"\n'
            f"  Shape: {rows:,} rows × {cols} columns"
            f"{date_range_str}\n"
            f"  Columns: {col_display}"
        )
        if meta_str:
            block += f"\n  {meta_str}"
        lines.append(block)

    return "\n\n".join(lines)


def get_dataset_summary(dataset_name: str) -> str:
    """Get detailed summary statistics for a specific dataset.

    Args:
        dataset_name: Exact name of the dataset in the DataStore.

    Returns:
        Formatted summary with shape, column names + dtypes, index info,
        and descriptive statistics for numeric columns. Error message if
        the dataset is not found.
    """
    store = get_data_store()
    df = store.get(dataset_name)
    if df is None:
        return f"Dataset '{dataset_name}' not found. Use list_datasets to see available datasets."

    rows, cols = df.shape
    lines = [
        f'Dataset: "{dataset_name}"',
        f"Shape: {rows:,} rows × {cols} columns",
    ]

    # Index info
    if isinstance(df.index, pd.DatetimeIndex) and len(df) > 0:
        lines.append(f"Index: DatetimeIndex — {df.index.min().date()} to {df.index.max().date()}")
    else:
        lines.append(f"Index type: {type(df.index).__name__}")

    # Column listing with dtypes
    col_info = "\n".join(f"  {col} ({dtype!s})" for col, dtype in df.dtypes.items())
    lines.append(f"Columns:\n{col_info}")

    # Descriptive statistics (first 10 numeric columns)
    numeric_cols = df.select_dtypes(include="number").columns.tolist()
    if numeric_cols:
        desc_cols = numeric_cols[:10]
        desc = df[desc_cols].describe().to_string()
        lines.append(f"Statistics:\n{desc}")
        if len(numeric_cols) > 10:
            lines.append(
                f"Showing statistics for first 10 of {len(numeric_cols)} numeric columns. "
                "Use get_dataset_slice for specific columns."
            )

    result = "\n\n".join(lines)
    # Cap at ~2000 characters
    if len(result) > 2000:
        result = result[:1970] + "\n...[truncated]"
    return result


def get_dataset_slice(
    dataset_name: str,
    columns: list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    last_n_rows: int | None = None,
) -> str:
    """Retrieve a filtered slice of a dataset as formatted text.

    Args:
        dataset_name: Exact name of the dataset in the DataStore.
        columns: Optional column names to include. Defaults to first 8.
        start_date: Optional ISO start date (DatetimeIndex datasets only).
        end_date: Optional ISO end date (DatetimeIndex datasets only).
        last_n_rows: If set, return only the last N rows (after filtering).

    Returns:
        Tabular text data. Maximum 50 rows — if the result exceeds 50 rows,
        shows first 25 and last 25 with an omission note.
    """
    store = get_data_store()
    df = store.get(dataset_name)
    if df is None:
        return f"Dataset '{dataset_name}' not found. Use list_datasets to see available datasets."

    warnings: list[str] = []

    # Column filter
    if columns is not None:
        missing = [c for c in columns if c not in df.columns]
        if missing:
            warnings.append(f"Warning: columns not found and skipped: {missing}")
        available = [c for c in columns if c in df.columns]
        if not available:
            return (
                f"No requested columns exist in '{dataset_name}'. "
                f"Available: {list(df.columns[:20])}"
            )
        df = df[available]
    elif len(df.columns) > 8:
        warnings.append(
            f"Showing 8 of {len(df.columns)} columns. Specify column names to see others."
        )
        df = df.iloc[:, :8]

    # Date filter
    if start_date is not None or end_date is not None:
        if isinstance(df.index, pd.DatetimeIndex):
            try:
                start = pd.to_datetime(start_date) if start_date else None
                end = pd.to_datetime(end_date) if end_date else None
                df = df.loc[start:end]
            except ValueError as exc:
                return f"Invalid date format: {exc}. Use ISO format (YYYY-MM-DD)."
        else:
            warnings.append("Date filtering skipped — dataset does not have a DatetimeIndex.")

    # last_n_rows filter
    if last_n_rows is not None:
        df = df.tail(last_n_rows)

    # Row limit: max 50
    rows, cols = df.shape
    if rows > 50:
        head_df = df.head(25)
        tail_df = df.tail(25)
        omitted = rows - 50
        body = (
            head_df.to_string(max_rows=25)
            + f"\n... ({omitted} rows omitted) ...\n"
            + tail_df.to_string(max_rows=25)
        )
    else:
        body = df.to_string(max_rows=50)

    # Header line
    date_range = ""
    if isinstance(df.index, pd.DatetimeIndex) and len(df) > 0:
        date_range = f" | Date range: {df.index.min().date()} to {df.index.max().date()}"
    header = f"Dataset: '{dataset_name}' | {rows} rows × {cols} columns{date_range}"

    parts = [header]
    if warnings:
        parts.append("\n".join(warnings))
    parts.append(body)

    return "\n".join(parts)


def list_analysis_results(producer: str | None = None) -> str:
    """List all analysis-result datasets currently in the DataStore.

    Filters the DataStore to entries whose name starts with the prefix
    ``"analysis_results."``. For each match, returns the dataset name,
    shape, ``producer`` / ``result_type`` from metadata, ``computed_at``
    timestamp, and any producer-specific metadata extras (e.g.
    ``risk_free_rate``, ``cancelled``, ``n_files``).

    Use this tool before calling ``get_dataset_summary`` or
    ``get_dataset_slice`` to discover which analyses the user has
    already run, especially when answering questions like "what did
    the optimiser produce?" or "did the scraper finish?"

    Args:
        producer: Optional producer identifier. If given, only datasets
            whose metadata ``producer`` matches are returned. Examples:
            ``"fo_optimizer"``, ``"scraper"``. Names of the producer
            constants are documented in
            :mod:`services.results_serialization`.

    Returns:
        Human-readable multi-line summary, one block per matching
        dataset. If no analysis results are loaded, returns an
        explanatory message.
    """
    store = get_data_store()
    items = store.list()

    matches = [item for item in items if item["name"].startswith("analysis_results.")]
    if producer is not None:
        matches = [item for item in matches if item["metadata"].get("producer") == producer]

    if not matches:
        if producer is not None:
            return (
                f"No analysis results from producer '{producer}' are currently "
                "loaded. Either the analysis has not been run in this session, "
                "or the producer name does not match. Use list_analysis_results() "
                "without an argument to see what is available."
            )
        return (
            "No analysis results are currently loaded. Analysis-result "
            "datasets are written when the user runs the Front Office "
            "Portfolio Optimiser or the Report Scraper. Use list_datasets "
            "to see other (non-analysis) data."
        )

    # Sort by computed_at descending so the most recent runs come first.
    # Items without computed_at (defensive: should not happen for
    # analysis_results.*) sort last.
    def sort_key(item: dict) -> str:
        return item["metadata"].get("computed_at") or ""

    matches.sort(key=sort_key, reverse=True)

    lines: list[str] = []
    for item in matches:
        name = item["name"]
        rows, cols = item["shape"]
        meta = item["metadata"]
        prod = meta.get("producer", "?")
        rtype = meta.get("result_type", "?")
        computed_at = meta.get("computed_at", "?")

        # Standard keys that are already shown above; remaining keys are
        # producer-specific extras worth surfacing.
        skip = {"producer", "result_type", "computed_at"}
        extra_pairs = [f"{k}={v}" for k, v in meta.items() if k not in skip]
        extras_str = (" | " + ", ".join(extra_pairs)) if extra_pairs else ""

        block = (
            f'Dataset: "{name}"\n'
            f"  Shape: {rows:,} rows × {cols} columns\n"
            f"  Producer: {prod} | Result type: {rtype}\n"
            f"  Computed at: {computed_at}{extras_str}"
        )
        lines.append(block)

    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Register tools at import time
# ---------------------------------------------------------------------------

_registry = get_tool_registry()

_registry.register_tool(
    name="list_datasets",
    function=list_datasets,
    description=(
        "List all datasets currently loaded in PortfoliFLOW's DataStore. "
        "Returns dataset names, dimensions, date ranges, column names, "
        "and import metadata. Use this to discover what data is available "
        "before querying specific datasets."
    ),
    parameters={"type": "object", "properties": {}, "required": []},
    tool_class=ToolClass.READ_INTERNAL,
)

_registry.register_tool(
    name="get_dataset_summary",
    function=get_dataset_summary,
    description=(
        "Get detailed summary statistics for a specific dataset in the DataStore. "
        "Returns shape, column names and dtypes, index info, and descriptive "
        "statistics. Call list_datasets first to find exact dataset names."
    ),
    parameters={
        "type": "object",
        "properties": {
            "dataset_name": {
                "type": "string",
                "description": "Exact name of the dataset in the DataStore.",
            }
        },
        "required": ["dataset_name"],
    },
    tool_class=ToolClass.READ_INTERNAL,
)

_registry.register_tool(
    name="get_dataset_slice",
    function=get_dataset_slice,
    description=(
        "Retrieve a filtered slice of a dataset as formatted text. "
        "Supports column selection, date range filtering, and row limiting. "
        "Example: to find the latest NAV of 'Fund X', call with "
        "dataset_name='navs_actual', columns=['Fund X'], last_n_rows=1. "
        "Maximum 50 rows returned."
    ),
    parameters={
        "type": "object",
        "properties": {
            "dataset_name": {
                "type": "string",
                "description": "Exact name of the dataset in the DataStore.",
            },
            "columns": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of column names to include. "
                    "Defaults to first 8 columns if not specified."
                ),
            },
            "start_date": {
                "type": "string",
                "description": (
                    "Optional ISO start date for filtering (YYYY-MM-DD). "
                    "Only applicable to DatetimeIndex datasets."
                ),
            },
            "end_date": {
                "type": "string",
                "description": (
                    "Optional ISO end date for filtering (YYYY-MM-DD). "
                    "Only applicable to DatetimeIndex datasets."
                ),
            },
            "last_n_rows": {
                "type": "integer",
                "description": ("If set, return only the last N rows after other filtering."),
            },
        },
        "required": ["dataset_name"],
    },
    tool_class=ToolClass.READ_INTERNAL,
)

_registry.register_tool(
    name="list_analysis_results",
    function=list_analysis_results,
    description=(
        "List all analysis-result datasets currently in the DataStore "
        "(those whose name starts with 'analysis_results.'). Returns "
        "name, shape, producer, result type, computed_at timestamp, "
        "and producer-specific metadata. Use this to discover what "
        "analyses the user has run before drilling into a specific "
        "result with get_dataset_summary or get_dataset_slice. "
        "Optionally filter by producer (e.g. 'fo_optimizer', 'scraper')."
    ),
    parameters={
        "type": "object",
        "properties": {
            "producer": {
                "type": "string",
                "description": (
                    "Optional producer identifier to filter by. "
                    "Examples: 'fo_optimizer' (Front Office Portfolio "
                    "Optimizer), 'scraper' (Report Scraper). Omit to "
                    "list all analysis results."
                ),
            },
        },
        "required": [],
    },
    tool_class=ToolClass.READ_INTERNAL,
)
