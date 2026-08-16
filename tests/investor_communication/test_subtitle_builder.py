# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :meth:`ReportEngine._build_subtitle`."""

from __future__ import annotations

import pandas as pd

from services.reporting.report_engine import ReportEngine


def _engine() -> ReportEngine:
    return ReportEngine()


def _df(rows: dict[str, dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame.from_dict(rows, orient="index")


def test_all_fields_populated() -> None:
    """All four metadata fields produce the expected formatted line."""
    df = _df(
        {
            "Manager / Fondsname": {"A": "Continental Capital Buyout Fund"},
            "Vintage Year": {"A": 2018},
            "Investment Sub-Class": {"A": "Global Buyout"},
            "Asset Class": {"A": "Private Equity"},
        }
    )
    subtitle = _engine()._build_subtitle("A", df)
    assert subtitle == (
        "Continental Capital Buyout Fund, Vintage 2018, Global Buyout, Private Equity"
    )


def test_some_fields_missing() -> None:
    """Missing fields are dropped; commas separate only the populated ones."""
    df = _df(
        {
            "Manager / Fondsname": {"A": "Mgr A"},
            "Vintage Year": {"A": float("nan")},
            "Investment Sub-Class": {"A": ""},
            "Asset Class": {"A": "Private Equity"},
        }
    )
    subtitle = _engine()._build_subtitle("A", df)
    assert subtitle == "Mgr A, Private Equity"


def test_all_fields_missing() -> None:
    """No metadata at all yields the empty string."""
    df = _df({"Other": {"A": "x"}})
    subtitle = _engine()._build_subtitle("A", df)
    assert subtitle == ""


def test_vintage_year_as_float_renders_as_int() -> None:
    """``2018.0`` renders as ``Vintage 2018`` (no trailing decimal)."""
    df = _df(
        {
            "Vintage Year": {"A": 2018.0},
        }
    )
    subtitle = _engine()._build_subtitle("A", df)
    assert subtitle == "Vintage 2018"


def test_investment_column_missing() -> None:
    """If the investment is not a column, the subtitle is empty."""
    df = _df(
        {
            "Manager / Fondsname": {"A": "Mgr A"},
        }
    )
    subtitle = _engine()._build_subtitle("UNKNOWN", df)
    assert subtitle == ""


def test_placeholder_strings_are_dropped() -> None:
    """Known placeholder text (``Klasse der Investition``) is treated as empty."""
    df = _df(
        {
            "Manager / Fondsname": {"A": "Mgr A"},
            "Investment Sub-Class": {"A": "Klasse der Investition"},
        }
    )
    subtitle = _engine()._build_subtitle("A", df)
    assert subtitle == "Mgr A"
