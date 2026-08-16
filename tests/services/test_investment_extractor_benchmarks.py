# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for ``extract_benchmarks_from_snapshot`` (ADR-0061).

Pure unit tests — the extractor has no DB / FastAPI dependency.
The fixtures construct the ``DataFrame.to_json(orient="split")``
shape persisted by :class:`DataUploadRepository`.

Coverage:

* Happy path: snapshot with benchmarks + mappings → typed DTOs.
* ``benchmarks_actual`` missing, ``benchmark_mapping`` present →
  :class:`ImportFormatError`.
* ``benchmark_mapping`` missing, ``benchmarks_actual`` present →
  benchmarks/observations returned, warning recorded.
* Both sheets absent → four empty lists, no error.
* Mapping row with empty ``benchmark_id`` and ``weight == 0`` →
  valid; mapping returned as deliberate non-mapping.
* Mapping row with ``benchmark_id`` not in ``benchmarks_actual`` →
  :class:`ImportFormatError`.
* Duplicate ``(benchmark_code, as_of_date)`` → :class:`ImportRowError`
  recorded, last value wins.
* Non-numeric return cell → :class:`ImportRowError`, cell dropped.
* Weight outside ``[0, 1]`` → :class:`ImportRowError`, row dropped.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from services.data_normalization import (
    ImportedBenchmark,
    ImportedBenchmarkMapping,
    ImportedBenchmarkObservation,
    ImportFormatError,
    extract_benchmarks_from_snapshot,
)


def _benchmarks_actual_payload(
    benchmark_codes: list[str],
    rows: list[tuple[str, list[object]]],
) -> dict:
    return {
        "columns": list(benchmark_codes),
        "index": [iso for iso, _ in rows],
        "data": [vals for _, vals in rows],
    }


def _benchmark_mapping_payload(
    mapping_rows: list[tuple[object, object, object, object]],
) -> dict:
    return {
        "columns": ["asset_class", "benchmark_id", "weight", "comment"],
        "index": list(range(len(mapping_rows))),
        "data": [list(row) for row in mapping_rows],
    }


def test_bx01_happy_path_two_benchmarks_and_mapping() -> None:
    snapshot = {
        "benchmarks_actual": _benchmarks_actual_payload(
            ["BM_EQ", "BM_BOND"],
            [
                ("2026-01-01", [0.001, -0.0005]),
                ("2026-01-02", [0.002, 0.0007]),
            ],
        ),
        "benchmark_mapping": _benchmark_mapping_payload(
            [
                ("equities", "BM_EQ", 1.0, "Pure Equity"),
                ("bonds", "BM_BOND", 1.0, "Pure Bond"),
            ]
        ),
    }
    benchmarks, observations, mappings, errors = extract_benchmarks_from_snapshot(snapshot)
    assert benchmarks == [
        ImportedBenchmark("BM_EQ", "BM_EQ", None, None),
        ImportedBenchmark("BM_BOND", "BM_BOND", None, None),
    ]
    assert len(observations) == 4
    assert ImportedBenchmarkObservation("BM_EQ", date(2026, 1, 1), Decimal("0.001")) in observations
    assert (
        ImportedBenchmarkObservation("BM_BOND", date(2026, 1, 2), Decimal("0.0007")) in observations
    )
    assert mappings == [
        ImportedBenchmarkMapping("equities", "BM_EQ", Decimal("1")),
        ImportedBenchmarkMapping("bonds", "BM_BOND", Decimal("1")),
    ]
    assert errors == []


def test_bx02_benchmarks_actual_missing_but_mapping_present_raises() -> None:
    snapshot = {
        "benchmark_mapping": _benchmark_mapping_payload(
            [("equities", "BM_EQ", 1.0, "Pure Equity")]
        ),
    }
    with pytest.raises(ImportFormatError) as exc_info:
        extract_benchmarks_from_snapshot(snapshot)
    assert "Benchmarks actual" in str(exc_info.value)


def test_bx03_mapping_missing_returns_benchmarks_with_warning() -> None:
    snapshot = {
        "benchmarks_actual": _benchmarks_actual_payload(
            ["BM_EQ"],
            [("2026-01-01", [0.001])],
        ),
    }
    benchmarks, observations, mappings, errors = extract_benchmarks_from_snapshot(snapshot)
    assert len(benchmarks) == 1
    assert len(observations) == 1
    assert mappings == []
    assert len(errors) == 1
    assert errors[0].sheet == "benchmark_mapping"
    assert "missing" in errors[0].message.lower()


def test_bx04_both_sheets_absent_returns_empty() -> None:
    benchmarks, observations, mappings, errors = extract_benchmarks_from_snapshot({})
    assert benchmarks == []
    assert observations == []
    assert mappings == []
    assert errors == []


def test_bx05_empty_benchmark_code_with_zero_weight_is_valid() -> None:
    snapshot = {
        "benchmarks_actual": _benchmarks_actual_payload(["BM_EQ"], [("2026-01-01", [0.001])]),
        "benchmark_mapping": _benchmark_mapping_payload(
            [
                ("equities", "BM_EQ", 1.0, "Pure Equity"),
                ("cash", None, 0, "No benchmark for cash"),
            ]
        ),
    }
    _, _, mappings, errors = extract_benchmarks_from_snapshot(snapshot)
    # The cash row is included in the returned list (the service
    # layer interprets it as a deliberate non-mapping).
    assert ImportedBenchmarkMapping("cash", "", Decimal("0")) in mappings
    # No errors for the empty-code-with-zero-weight case.
    assert errors == []


def test_bx06_unknown_benchmark_code_raises() -> None:
    snapshot = {
        "benchmarks_actual": _benchmarks_actual_payload(["BM_EQ"], [("2026-01-01", [0.001])]),
        "benchmark_mapping": _benchmark_mapping_payload(
            [("equities", "BM_DOES_NOT_EXIST", 1.0, "")]
        ),
    }
    with pytest.raises(ImportFormatError) as exc_info:
        extract_benchmarks_from_snapshot(snapshot)
    assert "BM_DOES_NOT_EXIST" in str(exc_info.value)


def test_bx07_duplicate_observation_records_error_last_wins() -> None:
    snapshot = {
        "benchmarks_actual": _benchmarks_actual_payload(
            ["BM_EQ"],
            [
                ("2026-01-01", [0.001]),
                ("2026-01-01", [0.999]),  # duplicate (BM_EQ, 2026-01-01)
            ],
        ),
    }
    _, observations, _, errors = extract_benchmarks_from_snapshot(snapshot)
    # Last value wins.
    assert observations == [
        ImportedBenchmarkObservation("BM_EQ", date(2026, 1, 1), Decimal("0.999"))
    ]
    # An error record is emitted for the duplicate. Plus the missing
    # mapping-sheet warning.
    dup_errors = [e for e in errors if "Duplicate" in e.message]
    assert len(dup_errors) == 1
    assert "BM_EQ" in dup_errors[0].message


def test_bx08_non_numeric_return_records_error() -> None:
    snapshot = {
        "benchmarks_actual": _benchmarks_actual_payload(
            ["BM_EQ"],
            [
                ("2026-01-01", [0.001]),
                ("2026-01-02", ["not_a_number"]),
            ],
        ),
    }
    _, observations, _, errors = extract_benchmarks_from_snapshot(snapshot)
    assert len(observations) == 1
    assert observations[0].as_of_date == date(2026, 1, 1)
    numeric_errors = [e for e in errors if "numeric" in e.message]
    assert len(numeric_errors) == 1
    assert numeric_errors[0].column == "BM_EQ"


def test_bx09_weight_out_of_range_drops_row() -> None:
    snapshot = {
        "benchmarks_actual": _benchmarks_actual_payload(["BM_EQ"], [("2026-01-01", [0.001])]),
        "benchmark_mapping": _benchmark_mapping_payload(
            [
                ("equities", "BM_EQ", 1.5, ""),  # out of range
                ("bonds", "BM_EQ", 0.5, ""),
            ]
        ),
    }
    _, _, mappings, errors = extract_benchmarks_from_snapshot(snapshot)
    assert mappings == [
        ImportedBenchmarkMapping("bonds", "BM_EQ", Decimal("0.5")),
    ]
    range_errors = [e for e in errors if "outside" in e.message]
    assert len(range_errors) == 1


def test_bx10_non_iso_date_records_error() -> None:
    snapshot = {
        "benchmarks_actual": _benchmarks_actual_payload(
            ["BM_EQ"],
            [
                ("2026-01-01", [0.001]),
                ("not-a-date", [0.999]),
            ],
        ),
    }
    _, observations, _, errors = extract_benchmarks_from_snapshot(snapshot)
    assert len(observations) == 1
    date_errors = [e for e in errors if "ISO date" in e.message]
    assert len(date_errors) == 1
