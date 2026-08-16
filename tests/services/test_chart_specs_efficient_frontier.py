# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for ``services.chart_specs.efficient_frontier``.

The spec builder is a pure function that turns a small set of
``PortfolioResult`` instances + scalar inputs into a Plotly figure
dict. The tests exercise:

* The shape of the returned dict (top-level keys, trace count).
* That the layout colours come from the canonical theme JSON.
* That the tangency / min-variance hover templates carry the asset
  names so the hover tooltip is useful.
* That the function is deterministic for identical inputs.
"""

from __future__ import annotations

import numpy as np
import pytest

from services.analytics.portfolio_optimizer import (
    PortfolioConstraints,
    PortfolioOptimizer,
    PortfolioResult,
)
from services.chart_specs import build_efficient_frontier_spec
from services.chart_specs.base import get_chart_theme


@pytest.fixture(scope="module")
def sample_inputs() -> dict:
    """Build a small, fast 3-asset optimisation input set.

    Returns a dict carrying the seven kwargs required by
    :func:`build_efficient_frontier_spec`. Reused across tests so
    repeated optimiser runs do not slow the suite.
    """
    cov = np.array(
        [
            [0.04, 0.005, 0.001],
            [0.005, 0.025, 0.002],
            [0.001, 0.002, 0.0036],
        ]
    )
    optimizer = PortfolioOptimizer(
        expected_returns=np.array([0.075, 0.06, 0.035]),
        cov_matrix=cov,
        asset_names=["Equity", "Bonds", "Cash"],
        risk_free_rate=0.025,
        constraints=PortfolioConstraints(long_only=True),
    )
    return {
        "frontier": optimizer.efficient_frontier(n_points=20),
        "tangency": optimizer.tangency_portfolio(),
        "min_var": optimizer.minimum_variance_portfolio(),
        "cloud": optimizer.random_portfolios(n_portfolios=200),
        "cml": optimizer.capital_market_line(n_points=10),
        "asset_names": ["Equity", "Bonds", "Cash"],
        "risk_free_rate": 0.025,
    }


def test_top_level_keys(sample_inputs: dict) -> None:
    spec = build_efficient_frontier_spec(**sample_inputs)
    assert set(spec.keys()) == {"data", "layout", "config"}


def test_trace_count_and_names(sample_inputs: dict) -> None:
    spec = build_efficient_frontier_spec(**sample_inputs)
    # cloud, frontier, CML, RF line, tangency, min-var → 6 traces.
    assert len(spec["data"]) == 6
    names = [trace["name"] for trace in spec["data"]]
    assert "Efficient Frontier" in names
    assert "Capital Market Line" in names
    assert "Tangency Portfolio" in names
    assert "Min Variance" in names


def test_layout_colours_match_theme(sample_inputs: dict) -> None:
    theme = get_chart_theme()
    spec = build_efficient_frontier_spec(**sample_inputs)
    assert spec["layout"]["paper_bgcolor"] == theme["colours"]["background"]
    assert spec["layout"]["plot_bgcolor"] == theme["colours"]["plot_area"]


def test_tangency_hover_includes_weights(sample_inputs: dict) -> None:
    spec = build_efficient_frontier_spec(**sample_inputs)
    tangency_trace = next(t for t in spec["data"] if t["name"] == "Tangency Portfolio")
    template = tangency_trace["hovertemplate"]
    assert "Tangency Portfolio" in template
    # At least one asset name from the inputs should appear in the
    # weight breakdown — we can't guarantee all three remain above the
    # 0.1% display threshold, but the optimiser's tangency should
    # allocate to at least one.
    assert any(name in template for name in sample_inputs["asset_names"])


def test_min_var_hover_includes_weights(sample_inputs: dict) -> None:
    spec = build_efficient_frontier_spec(**sample_inputs)
    min_var_trace = next(t for t in spec["data"] if t["name"] == "Min Variance")
    template = min_var_trace["hovertemplate"]
    assert "Min-Variance Portfolio" in template
    assert any(name in template for name in sample_inputs["asset_names"])


def test_pure_function_deterministic(sample_inputs: dict) -> None:
    spec_a = build_efficient_frontier_spec(**sample_inputs)
    spec_b = build_efficient_frontier_spec(**sample_inputs)
    # Same kwargs → same output dict (deep-equality on the JSON shape).
    assert spec_a == spec_b


def test_zero_weights_suppressed_in_hover() -> None:
    """A tangency with one near-zero weight should hide that line."""
    asset_names = ["Equity", "Bonds", "Cash"]
    weights = np.array([1.0, 0.0, 0.0])
    tangency = PortfolioResult(
        weights=weights,
        expected_return=0.075,
        volatility=0.20,
        sharpe_ratio=0.25,
        asset_names=asset_names,
    )
    min_var = PortfolioResult(
        weights=np.array([0.0, 0.5, 0.5]),
        expected_return=0.0475,
        volatility=0.05,
        sharpe_ratio=0.45,
        asset_names=asset_names,
    )
    spec = build_efficient_frontier_spec(
        frontier=[tangency, min_var],
        tangency=tangency,
        min_var=min_var,
        cloud=[],
        cml=[(0.0, 0.025), (0.20, 0.075)],
        asset_names=asset_names,
        risk_free_rate=0.025,
    )
    tangency_trace = next(t for t in spec["data"] if t["name"] == "Tangency Portfolio")
    # Bonds and Cash are exactly zero — they must not appear inside the
    # hover-template weight list, but Equity (100%) must.
    assert "Equity: 100.0%" in tangency_trace["hovertemplate"]
    assert "Bonds: 0.0%" not in tangency_trace["hovertemplate"]
    assert "Cash: 0.0%" not in tangency_trace["hovertemplate"]
