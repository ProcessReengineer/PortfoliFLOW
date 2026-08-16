# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression tests for :mod:`services.reporting.attributes_partition`.

Bug 4 (sparse breakdown rows): a sector block containing one row with only
a single non-zero cell — followed by a country block — must be detected as
two distinct blocks.  Prior to the fix, the country block was collapsed to
the lone sparse sector row.
"""

from __future__ import annotations

import pandas as pd

from services.reporting.attributes_partition import partition_attributes


def test_sparse_sector_row_does_not_collapse_country_block() -> None:
    """Sector with a sparse row (single non-zero cell) keeps the country block intact."""
    df = pd.DataFrame(
        {
            "A": [0.6, 0.05, 0.35, 1.0, 0.0],
            "B": [1.0, 0.0, 0.0, 0.5, 0.5],
        },
        index=[
            "Tech",
            "Real Estate",  # sparse: only one non-zero cell across columns
            "Healthcare",
            "DE",  # country block follows immediately
            "US",
        ],
    )
    partition = partition_attributes(df)
    assert partition.sector_rows == ("Tech", "Real Estate", "Healthcare")
    assert partition.country_rows == ("DE", "US")


def test_all_empty_row_in_sector_block_is_absorbed() -> None:
    """All-empty placeholder rows are absorbed into the active block, not separators."""
    df = pd.DataFrame(
        {
            "A": [0.6, None, 0.4, 1.0, 0.0],
            "B": [0.5, None, 0.5, 0.5, 0.5],
        },
        index=[
            "Tech",
            "Renewables (placeholder)",  # all None → placeholder
            "Healthcare",
            "DE",
            "US",
        ],
    )
    partition = partition_attributes(df)
    assert "Renewables (placeholder)" in partition.sector_rows
    assert partition.country_rows == ("DE", "US")


def test_v18_like_layout() -> None:
    """A condensed v18-like layout has rows with all-NaN and sparse values, then countries."""
    rows: dict[str, dict[str, object]] = {
        "Investment Type": {"A": "Aktien", "B": "Aktien", "C": "Aktien", "D": "Immobilien"},
        "Investment Sub-Class": {"A": "Large Cap", "B": "Growth", "C": "EM", "D": "Open End"},
        "Region": {"A": "Eu", "B": "USA", "C": "EM", "D": "Eu"},
        "Vintage Year": {"A": 2000, "B": 2000, "C": 2000, "D": 2000},
        "Tech": {"A": 0.5, "B": 0.5, "C": 0.5, "D": None},
        "Healthcare": {"A": 0.3, "B": 0.3, "C": 0.3, "D": None},
        "Renewables": {"A": None, "B": None, "C": None, "D": None},  # placeholder row
        "Real Estate": {"A": None, "B": None, "C": None, "D": 1.0},  # sparse
        "Infrastructure": {"A": None, "B": None, "C": None, "D": None},  # placeholder row
        "Materials": {"A": 0.2, "B": 0.2, "C": 0.2, "D": None},
        "DACH": {"A": 0.5, "B": 0.5, "C": 0.5, "D": 0.5},
        "USA": {"A": 0.5, "B": 0.5, "C": 0.5, "D": 0.5},
    }
    df = pd.DataFrame.from_dict(rows, orient="index", columns=["A", "B", "C", "D"])
    partition = partition_attributes(df)
    sectors = set(partition.sector_rows)
    countries = set(partition.country_rows)
    # Sector block contains all sector-like labels (including sparse and placeholder).
    assert {"Tech", "Healthcare", "Real Estate", "Materials"} <= sectors
    # Country block contains only the actual country labels.
    assert countries == {"DACH", "USA"}
