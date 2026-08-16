# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :mod:`services.reporting.attributes_partition`."""

from __future__ import annotations

import pandas as pd

from services.reporting.attributes_partition import (
    is_breakdown_row,
    partition_attributes,
)


def test_is_breakdown_row_numeric_weights() -> None:
    """Rows of numeric weights in [0, 1] are detected."""
    row = pd.Series({"A": 0.4, "B": 0.5, "C": 0.1})
    assert is_breakdown_row(row) is True


def test_is_breakdown_row_with_empty_strings() -> None:
    """Empty strings are treated as missing and the rest is checked."""
    row = pd.Series({"A": 0.6, "B": "", "C": 0.4})
    assert is_breakdown_row(row) is True


def test_is_breakdown_row_rejects_strings() -> None:
    """Rows containing non-numeric values are not breakdown rows."""
    row = pd.Series({"A": "Europe", "B": "USA", "C": "Asia"})
    assert is_breakdown_row(row) is False


def test_is_breakdown_row_rejects_out_of_range() -> None:
    """Values outside [0, 1.05] disqualify the row."""
    row = pd.Series({"A": 0.5, "B": 2.0, "C": 0.0})
    assert is_breakdown_row(row) is False


def test_is_breakdown_row_rejects_all_empty() -> None:
    """All-empty rows are not breakdown rows."""
    row = pd.Series({"A": float("nan"), "B": float("nan")})
    assert is_breakdown_row(row) is False


def test_partition_finds_sector_then_country_blocks() -> None:
    """First contiguous block after scalar attrs is sectors, second is countries."""
    df = pd.DataFrame(
        {
            "Investition A": [
                "Private Equity",
                "Buyout",
                "Mgr A",  # scalars
                0.6,
                0.4,
                0.0,  # sectors
                1.0,
                0.0,  # countries
            ],
            "Investition B": [
                "Infrastructure",
                "Energy",
                "Mgr B",
                0.0,
                0.0,
                1.0,
                0.5,
                0.5,
            ],
        },
        index=[
            "Investment Type",
            "Investment Sub-Class",
            "Manager / Fondsname",
            "Tech",
            "Healthcare",
            "Energy",
            "DE",
            "US",
        ],
    )
    partition = partition_attributes(df)
    assert partition.sector_rows == ("Tech", "Healthcare", "Energy")
    assert partition.country_rows == ("DE", "US")


def test_partition_only_one_block_means_country_empty() -> None:
    """If only one breakdown block exists it is treated as the sector block."""
    df = pd.DataFrame(
        {
            "Investition A": ["Private Equity", "Mgr A", 0.5, 0.5],
            "Investition B": ["Infrastructure", "Mgr B", 0.0, 1.0],
        },
        index=["Investment Type", "Manager / Fondsname", "Tech", "Energy"],
    )
    partition = partition_attributes(df)
    assert partition.sector_rows == ("Tech", "Energy")
    assert partition.country_rows == ()


def test_partition_empty_dataframe() -> None:
    """Empty input returns empty tuples."""
    partition = partition_attributes(pd.DataFrame())
    assert partition.sector_rows == ()
    assert partition.country_rows == ()


def test_partition_ignores_trailing_identifier_rows() -> None:
    """Trailing ISIN / Ticker string rows neither join nor corrupt the
    sector / country breakdown blocks (ADR-0090).

    The security-identifier rows are known scalar attributes and must be
    filtered out before the numeric-block heuristic runs — otherwise a
    trailing free-text ``ISIN`` row could be mistaken for a block
    separator or, worse, absorbed into a block. Here they sit *below*
    both breakdown blocks; the partition must still resolve sectors and
    countries exactly as if the identifier rows were absent, and neither
    label may appear in either tuple.
    """
    df = pd.DataFrame(
        {
            "Investition A": [
                "Private Equity",
                "Buyout",
                "Mgr A",  # scalars
                0.6,
                0.4,  # sectors
                1.0,
                0.0,  # countries
                "DE000BASF111",
                "BAS",  # identifiers
            ],
            "Investition B": [
                "Infrastructure",
                "Energy",
                "Mgr B",
                0.3,
                0.7,
                0.5,
                0.5,
                "US0378331005",
                "AAPL",
            ],
        },
        index=[
            "Investment Type",
            "Investment Sub-Class",
            "Manager / Fondsname",
            "Tech",
            "Healthcare",
            "DE",
            "US",
            "ISIN",
            "Ticker",
        ],
    )
    partition = partition_attributes(df)
    assert partition.sector_rows == ("Tech", "Healthcare")
    assert partition.country_rows == ("DE", "US")
    # The identifier rows are filtered out, not classified as breakdowns.
    assert "ISIN" not in partition.sector_rows
    assert "ISIN" not in partition.country_rows
    assert "Ticker" not in partition.sector_rows
    assert "Ticker" not in partition.country_rows
