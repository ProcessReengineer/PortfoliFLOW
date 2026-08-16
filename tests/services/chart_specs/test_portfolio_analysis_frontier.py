# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Structural tests for ``services.chart_specs.portfolio_analysis_frontier``.

Plotly figures are large dicts; full-fidelity diffs are brittle and
do not surface real regressions. These tests assert the structural
invariants the route consumer relies on: trace count, marker
shapes, theme-driven colours, axis formatters.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from services.analytics.efficient_frontier import (
    compute_capital_market_line,
    compute_efficient_frontier,
    compute_min_variance_portfolio,
    compute_tangency_portfolio,
)
from services.chart_specs import build_portfolio_analysis_frontier_spec
from services.chart_specs.base import get_chart_theme


@pytest.fixture(scope="module")
def _bundle() -> dict:
    """Synthetic three-investment bundle reused by the structural tests."""
    mu = pd.Series({"X": 0.08, "Y": 0.12, "Z": 0.05})
    sigmas = np.array([0.12, 0.20, 0.07])
    corr = np.array(
        [
            [1.0, 0.1, 0.05],
            [0.1, 1.0, 0.2],
            [0.05, 0.2, 1.0],
        ]
    )
    cov = pd.DataFrame(np.outer(sigmas, sigmas) * corr, index=mu.index, columns=mu.index)

    efr = compute_efficient_frontier(mu, cov, n_points=30)
    tang = compute_tangency_portfolio(efr, risk_free_rate=0.02)
    mv = compute_min_variance_portfolio(efr)
    cml = compute_capital_market_line(
        risk_free_rate=0.02, tangency=tang, x_max=1.5 * tang.volatility
    )
    investment_points = {
        n: (float(np.sqrt(cov.iloc[i, i])), float(mu.iloc[i])) for i, n in enumerate(mu.index)
    }
    return {
        "frontier": efr,
        "tangency": tang,
        "min_variance": mv,
        "capital_market_line": cml,
        "investment_points": investment_points,
        "current_portfolio": (0.13, 0.07),
        "risk_free_rate": 0.02,
    }


def test_top_level_keys(_bundle: dict) -> None:
    spec = build_portfolio_analysis_frontier_spec(**_bundle)
    assert set(spec.keys()) == {"data", "layout", "config"}


def test_default_trace_set_with_current_portfolio(_bundle: dict) -> None:
    spec = build_portfolio_analysis_frontier_spec(**_bundle)
    names = [t.get("name") for t in spec["data"]]
    assert "Efficient Frontier" in names
    assert "Capital Market Line" in names
    assert "Tangency Portfolio" in names
    assert "Min Variance" in names
    assert "Current Portfolio" in names
    assert "Investments" in names
    # Risk-free rate trace name carries the percentage.
    assert any("Risk-Free Rate" in (n or "") for n in names)


def test_current_portfolio_omitted_when_none(_bundle: dict) -> None:
    payload = dict(_bundle)
    payload["current_portfolio"] = None
    spec = build_portfolio_analysis_frontier_spec(**payload)
    names = [t.get("name") for t in spec["data"]]
    assert "Current Portfolio" not in names


def test_current_portfolio_omitted_when_nan(_bundle: dict) -> None:
    payload = dict(_bundle)
    payload["current_portfolio"] = (math.nan, math.nan)
    spec = build_portfolio_analysis_frontier_spec(**payload)
    names = [t.get("name") for t in spec["data"]]
    assert "Current Portfolio" not in names


def test_marker_shapes_match_visual_spec(_bundle: dict) -> None:
    spec = build_portfolio_analysis_frontier_spec(**_bundle)
    by_name = {t["name"]: t for t in spec["data"]}
    assert by_name["Tangency Portfolio"]["marker"]["symbol"] == "star"
    assert by_name["Min Variance"]["marker"]["symbol"] == "diamond"
    assert by_name["Current Portfolio"]["marker"]["symbol"] == "circle"
    assert by_name["Investments"]["marker"]["symbol"] == "x"


def test_investment_markers_carry_text_labels(_bundle: dict) -> None:
    spec = build_portfolio_analysis_frontier_spec(**_bundle)
    investments = next(t for t in spec["data"] if t.get("name") == "Investments")
    assert investments["mode"] == "markers+text"
    assert sorted(investments["text"]) == ["X", "Y", "Z"]
    assert len(investments["x"]) == 3
    assert len(investments["y"]) == 3


def test_current_portfolio_uses_theme_colour(_bundle: dict) -> None:
    spec = build_portfolio_analysis_frontier_spec(**_bundle)
    current = next(t for t in spec["data"] if t.get("name") == "Current Portfolio")
    expected_colour = get_chart_theme()["optimization"]["current_portfolio_colour"]
    assert current["marker"]["color"] == expected_colour


def test_axis_formatters_are_percent_and_anchored_to_zero(
    _bundle: dict,
) -> None:
    spec = build_portfolio_analysis_frontier_spec(**_bundle)
    layout = spec["layout"]
    assert layout["xaxis"]["tickformat"] == ".1%"
    assert layout["yaxis"]["tickformat"] == ".1%"
    assert layout["xaxis"]["rangemode"] == "tozero"
    assert layout["yaxis"]["rangemode"] == "tozero"


def test_legend_visible_and_responsive_config(_bundle: dict) -> None:
    spec = build_portfolio_analysis_frontier_spec(**_bundle)
    assert spec["layout"]["showlegend"] is True
    assert spec["config"]["responsive"] is True
    assert spec["config"]["displaylogo"] is False


def test_title_default_matches_qt_title(_bundle: dict) -> None:
    spec = build_portfolio_analysis_frontier_spec(**_bundle)
    assert spec["layout"]["title"]["text"] == "Portfolio Optimisation — Efficient Frontier"


def test_title_override_applied(_bundle: dict) -> None:
    spec = build_portfolio_analysis_frontier_spec(**_bundle, title="Custom Title")
    assert spec["layout"]["title"]["text"] == "Custom Title"


def test_empty_frontier_omits_frontier_trace_but_keeps_others(
    _bundle: dict,
) -> None:
    """Edge case: an empty frontier still yields a usable chart shell."""
    payload = dict(_bundle)
    empty = payload["frontier"]
    # Build a minimal EFR with empty arrays via the dataclass directly.
    from services.analytics.efficient_frontier import EfficientFrontierResult

    payload["frontier"] = EfficientFrontierResult(
        frontier_returns=np.empty(0, dtype=float),
        frontier_volatilities=np.empty(0, dtype=float),
        frontier_weights=np.empty((0, len(empty.asset_names)), dtype=float),
        asset_names=list(empty.asset_names),
        expected_returns=empty.expected_returns,
        cov_matrix=empty.cov_matrix,
        bounds_min=empty.bounds_min,
        bounds_max=empty.bounds_max,
    )
    spec = build_portfolio_analysis_frontier_spec(**payload)
    names = [t.get("name") for t in spec["data"]]
    assert "Efficient Frontier" not in names
    # The CML / RF / tangency / min-var traces still render so the
    # chart is not blank.
    assert "Capital Market Line" in names
    assert "Tangency Portfolio" in names
