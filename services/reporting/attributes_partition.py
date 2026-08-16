# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Partition the ``attributes`` DataFrame into known-scalar, sector, and country rows.

The ``Attributes`` sheet of the Excel import workbook is user-extensible:
new rows can be added by the importer without touching code.  Some of those
rows are scalar metadata (``Region``, ``Vintage Year``, ...) while others
are *breakdown rows* whose values are weights in ``[0, 1]`` summing to ``1``
per investment column (sector splits, country splits).

Partitioning algorithm
----------------------
1. Filter out a known set of scalar attribute names.  These never participate
   in a breakdown block.
2. Walk the remaining rows top-to-bottom.  A row is classified as

   * **numeric breakdown** — every non-empty cell is a finite float in
     ``[0, 1.05]`` and at least one cell is non-empty;
   * **all-empty** — every cell is empty / NaN; absorbed into the current
     block but contributes no weight.  All-empty rows do *not* terminate a
     block (they are valid "placeholder" rows in the Excel import workbook);
   * **non-breakdown** — at least one non-numeric cell or a value outside
     ``[0, 1.05]``; terminates the current block.

3. Among consecutive numeric breakdown rows, also force a block boundary
   whenever adding the next row would push a column whose running sum has
   already reached ``~1.0`` strictly above that threshold.  This is the only
   reliable way to detect a sector→country transition when the Excel import workbook
   places the two breakdown groups directly adjacent (no separator row).

4. The first block produced is the sector breakdown, the second is the
   country breakdown.  Any further blocks are ignored.

TODO: replace the implicit-block heuristic with explicit row tagging
(e.g. delimiter rows like ``=== SECTORS ===`` in the Excel input) once the
import format is extended.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

KNOWN_SCALAR_ATTRS: frozenset[str] = frozenset(
    {
        "Investment Type",
        "Investment Sub-Class",
        "Region",
        "Vintage Year",
        "Währung",
        "Currency",
        "Asset Class",
        "Manager / Fondsname",
        "Manager Name",
        # Security-identifier rows (ADR-0090). These carry free-text codes
        # (ISIN / ticker), never breakdown weights, so they must be filtered
        # out before the numeric-block heuristic runs — otherwise a trailing
        # identifier row could truncate or mis-classify a sector / country
        # block. Identifier extraction reads these rows independently from
        # the extractor's ``attr_table``, not from this partition.
        "ISIN",
        "Ticker",
    }
)

_BLOCK_COMPLETE_THRESHOLD: float = 0.999


@dataclass(frozen=True)
class AttributesPartition:
    """Partitioned view of the attributes DataFrame.

    Attributes:
        sector_rows: Index labels of rows representing the sector breakdown.
        country_rows: Index labels of rows representing the country breakdown.
    """

    sector_rows: tuple[str, ...]
    country_rows: tuple[str, ...]


def is_breakdown_row(row: pd.Series) -> bool:
    """Return ``True`` if every non-empty cell is a numeric weight in ``[0, 1.05]``.

    A row qualifies iff:

    * it has at least one non-empty cell, and
    * every non-empty cell parses to a finite float, and
    * every parsed value lies in ``[0, 1.05]``.

    Empty cells (``""`` or ``NaN``) are ignored — even a single non-zero
    value in an otherwise sparse row is accepted.  Entirely-empty rows
    return ``False``: callers (notably :func:`partition_attributes`) decide
    separately whether to treat them as block separators or as neutral
    placeholders that continue the current block.

    Args:
        row: A single attribute row.

    Returns:
        ``True`` if the row qualifies, ``False`` otherwise.
    """
    cleaned = row.replace("", float("nan")).dropna()
    if cleaned.empty:
        return False
    try:
        nums = pd.to_numeric(cleaned, errors="raise")
    except (ValueError, TypeError):
        return False
    return bool(((nums >= 0) & (nums <= 1.05)).all())


def partition_attributes(df_attributes: pd.DataFrame) -> AttributesPartition:
    """Split the attributes index into sector and country breakdown rows.

    The first contiguous block of breakdown rows (with all-empty rows
    absorbed) is the sector block; the second is the country block.  See the
    module docstring for the full algorithm.

    Args:
        df_attributes: The ``attributes`` DataFrame produced by the Excel
            import (rows indexed by attribute name, columns are investment
            names).

    Returns:
        :class:`AttributesPartition` with the sector and country row labels.
        Either tuple may be empty if no breakdown rows are detected.
    """
    if df_attributes is None or df_attributes.empty:
        return AttributesPartition(sector_rows=(), country_rows=())

    candidate_index = [r for r in df_attributes.index if r not in KNOWN_SCALAR_ATTRS]

    blocks: list[list[str]] = []
    current: list[str] = []
    running: pd.Series | None = None

    for label in candidate_index:
        row = df_attributes.loc[label]
        cleaned = row.replace("", float("nan")).dropna()

        if cleaned.empty:
            # All-empty row: absorbed into the current block (if any).  Does
            # not contribute to the running sum and does not terminate a
            # block — the Excel import workbook commonly leaves "placeholder" sector
            # rows entirely empty.
            if current:
                current.append(str(label))
            continue

        try:
            nums = pd.to_numeric(cleaned, errors="raise")
        except (ValueError, TypeError):
            # Non-numeric row: terminates the current block.
            if current:
                blocks.append(current)
                current = []
            running = None
            continue

        if not bool(((nums >= 0) & (nums <= 1.05)).all()):
            # Numeric but out of range — treat like non-breakdown.
            if current:
                blocks.append(current)
                current = []
            running = None
            continue

        # Numeric breakdown row.
        num_row = pd.to_numeric(row.replace("", float("nan")), errors="coerce").fillna(0.0)

        if running is None:
            current = [str(label)]
            running = num_row.copy()
            continue

        # Boundary triggers when adding ``num_row`` would push a column whose
        # running sum already reached ~1.0 strictly above that threshold.
        common = running.index.intersection(num_row.index)
        running_aligned = running.reindex(common).fillna(0.0)
        nrow_aligned = num_row.reindex(common).fillna(0.0)
        would_overfill = bool(
            ((running_aligned >= _BLOCK_COMPLETE_THRESHOLD) & (nrow_aligned > 0.0)).any()
        )
        if would_overfill:
            blocks.append(current)
            current = [str(label)]
            running = num_row.copy()
        else:
            current.append(str(label))
            running = running.add(num_row, fill_value=0.0)

    if current:
        blocks.append(current)

    sector_rows: tuple[str, ...] = tuple(blocks[0]) if blocks else ()
    country_rows: tuple[str, ...] = tuple(blocks[1]) if len(blocks) >= 2 else ()
    return AttributesPartition(sector_rows=sector_rows, country_rows=country_rows)
