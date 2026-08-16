# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Serialisation of analysis results into DataFrame form.

This module converts result objects produced by PortfoliFLOW's analysis
modules (analytics/portfolio_optimizer, services/scraper) into pandas
DataFrames with stable, predictable schemas. The DataFrames are designed
to be stored under the ``analysis_results.{producer}.{result_type}``
naming convention in the in-memory DataStore (ADR-0004) and inspected
by AI tools via the existing generic DataStore tools (datastore_tools.py).

The DataStore itself is unchanged — it remains a generic named-DataFrame
store that knows nothing about analysis-result schemas. This module also
does not write to the DataStore; it produces DataFrames and metadata that
producers (Phase 2) will store under the agreed naming convention.

Layering note (ADR-0001, ADR-0013): this module lives under ``services/``
because it imports both ``analytics.portfolio_optimizer.PortfolioResult``
and ``services.scraper.models.ScraperResult``. Placing it under
``analytics/`` would violate ADR-0013, which restricts ``analytics/`` to
``core.exceptions`` and third-party numerical libraries. Placing it under
``modules/`` would couple a cross-cutting helper to a single Area.

References:
    ADR-0001 (Layered architecture and strict one-way dependencies),
    ADR-0004 (In-memory DataStore singleton),
    ADR-0013 (Analytics layer pure and stateless).
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from services.analytics.portfolio_optimizer import PortfolioResult
from services.scraper.models import ScraperResult

logger = logging.getLogger(__name__)

__all__ = [
    "PRODUCER_FO_OPTIMIZER",
    "PRODUCER_SCRAPER",
    "RESULT_TYPE_CURRENT",
    "RESULT_TYPE_FINDINGS",
    "RESULT_TYPE_FRONTIER",
    "RESULT_TYPE_MIN_VAR",
    "RESULT_TYPE_TANGENCY",
    "build_analysis_metadata",
    "frontier_to_dataframe",
    "portfolio_result_to_dataframe",
    "scraper_result_to_dataframe",
]

# Producer identifiers — string constants (not Enums) so they survive
# JSON-style serialisation in metadata dicts unchanged.
PRODUCER_FO_OPTIMIZER = "fo_optimizer"
PRODUCER_SCRAPER = "scraper"

# Result-type identifiers used as the trailing segment of the
# ``analysis_results.{producer}.{result_type}`` dataset name.
RESULT_TYPE_TANGENCY = "tangency"
RESULT_TYPE_MIN_VAR = "min_var"
RESULT_TYPE_CURRENT = "current"
RESULT_TYPE_FRONTIER = "frontier"
RESULT_TYPE_FINDINGS = "findings"

# Metadata keys produced by build_analysis_metadata itself; callers may not
# override these via **extra without explicit error.
_RESERVED_METADATA_KEYS: tuple[str, ...] = ("producer", "result_type", "computed_at")

# Column order for the frontier DataFrame's metric columns. Asset weight
# columns are appended after these in their original asset order.
_FRONTIER_METRIC_COLUMNS: tuple[str, ...] = (
    "point_index",
    "expected_return",
    "volatility",
    "sharpe_ratio",
)

# Column order for the scraper DataFrame.
_SCRAPER_COLUMNS: tuple[str, ...] = (
    "filename",
    "fund_name",
    "period",
    "keyword",
    "keyword_type",
    "value",
    "source",
    "confidence",
    "error",
)


def portfolio_result_to_dataframe(result: PortfolioResult) -> pd.DataFrame:
    """Serialise a single PortfolioResult into a flat DataFrame.

    The resulting DataFrame has one row per asset, with the asset weight
    in its own column and the portfolio-level metrics
    (``expected_return``, ``volatility``, ``sharpe_ratio``) repeated on
    every row. Repeating the metrics keeps the table trivially queryable
    from ``get_dataset_slice`` without requiring callers (or AI tools)
    to look up portfolio-level values elsewhere; the redundancy is
    bounded by ``len(asset_names)``, which is small in practice.

    Args:
        result: A single :class:`PortfolioResult` (e.g. the tangency,
            minimum-variance, or current portfolio).

    Returns:
        DataFrame with columns ``asset`` (object), ``weight`` (float64),
        ``expected_return`` (float64), ``volatility`` (float64),
        ``sharpe_ratio`` (float64), in this order. The number of rows
        equals ``len(result.asset_names)``. The index is a plain
        :class:`pandas.RangeIndex`.

    Raises:
        ValueError: If ``result`` is None, or if ``len(result.weights)``
            does not equal ``len(result.asset_names)``.

    Example:
        >>> import numpy as np
        >>> from services.analytics.portfolio_optimizer import PortfolioResult
        >>> r = PortfolioResult(
        ...     weights=np.array([0.6, 0.4]),
        ...     expected_return=0.08,
        ...     volatility=0.12,
        ...     sharpe_ratio=0.5,
        ...     asset_names=["Equity", "Bonds"],
        ... )
        >>> df = portfolio_result_to_dataframe(r)
        >>> list(df.columns)
        ['asset', 'weight', 'expected_return', 'volatility', 'sharpe_ratio']
        >>> df.iloc[0]["asset"], df.iloc[0]["weight"]
        ('Equity', 0.6)
    """
    if result is None:
        raise ValueError("portfolio_result_to_dataframe: result must not be None")

    n_weights = len(result.weights)
    n_names = len(result.asset_names)
    if n_weights != n_names:
        raise ValueError(
            f"portfolio_result_to_dataframe: weights length ({n_weights}) "
            f"does not match asset_names length ({n_names})"
        )

    df = pd.DataFrame(
        {
            "asset": list(result.asset_names),
            "weight": [float(w) for w in result.weights],
            "expected_return": [float(result.expected_return)] * n_weights,
            "volatility": [float(result.volatility)] * n_weights,
            "sharpe_ratio": [float(result.sharpe_ratio)] * n_weights,
        }
    )
    # Pin object dtype on the string column so the schema is deterministic
    # regardless of pandas' future_infer_string global setting.
    df["asset"] = df["asset"].astype("object")

    logger.debug(
        "portfolio_result_to_dataframe: built DataFrame for %d assets",
        n_weights,
    )
    return df


def frontier_to_dataframe(frontier: list[PortfolioResult]) -> pd.DataFrame:
    """Serialise an efficient frontier into a wide DataFrame.

    The result has one row per frontier point and one column per asset
    weight, plus four metric columns (``point_index``,
    ``expected_return``, ``volatility``, ``sharpe_ratio``).
    ``point_index`` is 0-based and matches the input list order — the
    serializer does not re-sort (the optimizer already sorts the
    frontier by ascending volatility).

    Asset weight columns are named after the assets themselves (no
    prefix) so the DataFrame reads naturally when displayed by
    ``get_dataset_slice``. Phase 2 callers should pass
    ``asset_columns=result.asset_names`` in the metadata so consumers
    can distinguish weight columns from metric columns without
    heuristics.

    Args:
        frontier: List of :class:`PortfolioResult` along the efficient
            frontier. May be empty.

    Returns:
        DataFrame with columns ``point_index`` (int64),
        ``expected_return`` (float64), ``volatility`` (float64),
        ``sharpe_ratio`` (float64), followed by one float64 column per
        asset (named after the asset). The index is a plain
        :class:`pandas.RangeIndex`.

        If ``frontier`` is empty, the returned DataFrame has only the
        four metric columns and zero rows — asset columns cannot be
        inferred without at least one PortfolioResult.

    Raises:
        ValueError: If two frontier points have differing
            ``asset_names``; if any frontier point's ``weights`` length
            does not match its ``asset_names`` length; or if any asset
            name collides with one of the metric column names
            (``point_index``, ``expected_return``, ``volatility``,
            ``sharpe_ratio``).

    Example:
        >>> import numpy as np
        >>> from services.analytics.portfolio_optimizer import PortfolioResult
        >>> p0 = PortfolioResult(np.array([1.0, 0.0]), 0.05, 0.10, 0.3,
        ...                      ["Equity", "Bonds"])
        >>> p1 = PortfolioResult(np.array([0.5, 0.5]), 0.07, 0.12, 0.4,
        ...                      ["Equity", "Bonds"])
        >>> df = frontier_to_dataframe([p0, p1])
        >>> list(df.columns)
        ['point_index', 'expected_return', 'volatility', 'sharpe_ratio', 'Equity', 'Bonds']
        >>> df["point_index"].tolist()
        [0, 1]
    """
    if len(frontier) == 0:
        empty = pd.DataFrame(
            {
                "point_index": pd.Series([], dtype="int64"),
                "expected_return": pd.Series([], dtype="float64"),
                "volatility": pd.Series([], dtype="float64"),
                "sharpe_ratio": pd.Series([], dtype="float64"),
            }
        )
        logger.debug("frontier_to_dataframe: empty frontier")
        return empty

    canonical_assets = list(frontier[0].asset_names)

    # Reject asset names that would collide with metric columns; otherwise
    # the asset weight column would silently overwrite the metric column
    # when the dict is later passed to pd.DataFrame.
    collisions = [a for a in canonical_assets if a in _FRONTIER_METRIC_COLUMNS]
    if collisions:
        raise ValueError(
            f"frontier_to_dataframe: asset names collide with metric column names: {collisions}"
        )

    for i, point in enumerate(frontier):
        point_names = list(point.asset_names)
        if point_names != canonical_assets:
            raise ValueError(
                f"frontier_to_dataframe: asset_names mismatch at frontier "
                f"point {i}: expected {canonical_assets}, got {point_names}"
            )
        if len(point.weights) != len(point_names):
            raise ValueError(
                f"frontier_to_dataframe: frontier point {i} has weights "
                f"length {len(point.weights)} but asset_names length "
                f"{len(point_names)}"
            )

    n_points = len(frontier)
    data: dict[str, list] = {
        "point_index": list(range(n_points)),
        "expected_return": [float(p.expected_return) for p in frontier],
        "volatility": [float(p.volatility) for p in frontier],
        "sharpe_ratio": [float(p.sharpe_ratio) for p in frontier],
    }
    for j, asset in enumerate(canonical_assets):
        data[asset] = [float(p.weights[j]) for p in frontier]

    df = pd.DataFrame(data)

    logger.debug(
        "frontier_to_dataframe: built DataFrame for %d points × %d assets",
        n_points,
        len(canonical_assets),
    )
    return df


def scraper_result_to_dataframe(result: ScraperResult) -> pd.DataFrame:
    """Flatten a ScraperResult into a DataFrame with one row per Finding.

    Each :class:`Finding` becomes one row, with the file-level fields
    (``filename``, ``fund_name``, ``period``) duplicated across all
    findings of the same file. A failed extraction (one with ``error``
    set and an empty ``findings`` list) becomes a single row with the
    keyword-related columns set to empty strings and the ``error``
    column populated — this keeps the failure visible in the DataFrame
    rather than letting the file disappear silently. An extraction with
    no findings and no error contributes zero rows.

    The ``cancelled`` flag of :class:`ScraperResult` is intentionally
    not represented in the DataFrame. Phase 2 producers should pass it
    as a metadata extra (``cancelled=result.cancelled``) instead.

    Args:
        result: The :class:`ScraperResult` to serialise. May contain
            zero extractions.

    Returns:
        DataFrame with object-dtype columns ``filename``, ``fund_name``,
        ``period``, ``keyword``, ``keyword_type``, ``value``, ``source``,
        ``confidence``, ``error`` (in this order). ``keyword_type`` and
        ``confidence`` are the ``.value`` strings of their respective
        Enums (e.g. ``"Number"``, ``"High"``). The index is a plain
        :class:`pandas.RangeIndex`.

        If ``result.extractions`` is empty, the returned DataFrame has
        all nine columns and zero rows.

    Example:
        >>> from services.scraper.models import (
        ...     Confidence, Finding, Keyword, KeywordType,
        ...     ReportExtraction, ScraperResult,
        ... )
        >>> finding = Finding(
        ...     keyword=Keyword("NAV", KeywordType.NUMBER),
        ...     value="123.45",
        ...     source="Page 12",
        ...     confidence=Confidence.HIGH,
        ... )
        >>> ext = ReportExtraction(
        ...     filename="report.pdf", fund_name="Apollo IX",
        ...     period="Q1 2026", findings=[finding],
        ... )
        >>> df = scraper_result_to_dataframe(ScraperResult(extractions=[ext]))
        >>> df.iloc[0]["keyword"], df.iloc[0]["keyword_type"]
        ('NAV', 'Number')
    """
    rows: list[dict[str, str]] = []

    for extraction in result.extractions:
        file_error = extraction.error if extraction.error is not None else ""

        if extraction.findings:
            for finding in extraction.findings:
                rows.append(
                    {
                        "filename": extraction.filename,
                        "fund_name": extraction.fund_name,
                        "period": extraction.period,
                        "keyword": finding.keyword.name,
                        "keyword_type": finding.keyword.type.value,
                        "value": finding.value,
                        "source": finding.source,
                        "confidence": finding.confidence.value,
                        "error": file_error,
                    }
                )
        elif extraction.error is not None:
            # Failed extraction: surface as one row so the file does not
            # disappear from the DataFrame (Shirley should be able to see
            # "extraction attempted but failed").
            rows.append(
                {
                    "filename": extraction.filename,
                    "fund_name": extraction.fund_name,
                    "period": extraction.period,
                    "keyword": "",
                    "keyword_type": "",
                    "value": "",
                    "source": "",
                    "confidence": "",
                    "error": file_error,
                }
            )
        # else: no findings, no error — this extraction contributes nothing.

    if not rows:
        empty = pd.DataFrame({col: pd.Series([], dtype="object") for col in _SCRAPER_COLUMNS})
        logger.debug("scraper_result_to_dataframe: empty result")
        return empty

    df = pd.DataFrame(rows, columns=list(_SCRAPER_COLUMNS))
    # Pin object dtype on every string column so the schema is deterministic
    # regardless of pandas' future_infer_string global setting.
    for col in _SCRAPER_COLUMNS:
        df[col] = df[col].astype("object")

    logger.debug("scraper_result_to_dataframe: built DataFrame for %d rows", len(rows))
    return df


def build_analysis_metadata(
    producer: str,
    result_type: str,
    /,
    **extra: Any,
) -> dict:
    """Build the metadata dict for an analysis-results DataStore entry.

    The returned dict always contains ``producer``, ``result_type``,
    and ``computed_at`` (the current UTC time, ISO 8601 with second
    precision). Any keyword arguments in ``extra`` are merged in
    unchanged, so callers can attach producer-specific context
    (``risk_free_rate``, ``asset_columns``, ``cancelled``, …).
    Validation of ``extra`` values is the caller's responsibility — this
    helper only guards the three reserved keys.

    Using UTC for ``computed_at`` avoids ambiguity when the persistent
    DataVault (ADR-0017) lands and stores timestamps across sessions
    and across users in different timezones.

    Args:
        producer: Identifier of the producing module — typically one of
            the ``PRODUCER_*`` constants in this module. Positional-only
            (so a caller passing ``producer="x"`` lands in ``extra``,
            where the reserved-key check catches it). Must be a
            non-empty string.
        result_type: Identifier of the result kind — typically one of
            the ``RESULT_TYPE_*`` constants in this module.
            Positional-only for the same reason as ``producer``. Must
            be a non-empty string.
        **extra: Additional metadata keys to merge into the result. May
            not include any of the reserved keys (``producer``,
            ``result_type``, ``computed_at``).

    Returns:
        New dict containing ``producer``, ``result_type``,
        ``computed_at`` (an ISO 8601 string in UTC with second
        precision, e.g. ``"2026-04-28T14:32:11+00:00"``), plus all
        key/value pairs from ``extra``.

    Raises:
        ValueError: If ``producer`` or ``result_type`` is empty or not
            a string, or if ``extra`` contains one of the reserved
            keys.

    Example:
        >>> meta = build_analysis_metadata(
        ...     PRODUCER_FO_OPTIMIZER,
        ...     RESULT_TYPE_TANGENCY,
        ...     risk_free_rate=0.025,
        ...     n_assets=7,
        ... )
        >>> meta["producer"], meta["result_type"]
        ('fo_optimizer', 'tangency')
        >>> meta["risk_free_rate"]
        0.025
    """
    if not isinstance(producer, str) or producer == "":
        raise ValueError("build_analysis_metadata: producer must be a non-empty string")
    if not isinstance(result_type, str) or result_type == "":
        raise ValueError("build_analysis_metadata: result_type must be a non-empty string")

    for key in _RESERVED_METADATA_KEYS:
        if key in extra:
            raise ValueError(
                f"build_analysis_metadata: cannot override reserved key '{key}' via extra"
            )

    computed_at = pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds")

    return {
        "producer": producer,
        "result_type": result_type,
        "computed_at": computed_at,
        **extra,
    }
