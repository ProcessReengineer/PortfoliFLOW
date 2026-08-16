# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :class:`services.reporting.data_providers.KeyFiguresProvider`."""

from __future__ import annotations

import pytest

from services.reporting.data_providers import (
    IRRProvider,
    KeyFiguresProvider,
    MultiplesProvider,
    ProviderContext,
)


def test_key_figures_match_underlying_providers(
    populated_store_canonical: None,  # noqa: ARG001
    basic_ctx: ProviderContext,
    investments: tuple[str, ...],
) -> None:
    """Per-investment key figures match the per-investment provider outputs."""
    multiples = MultiplesProvider().get(basic_ctx)
    irr = IRRProvider().get(basic_ctx)

    for inv in investments:
        ctx = ProviderContext(
            report_date=basic_ctx.report_date,
            all_investments=investments,
            investment_filter=inv,
        )
        kf = KeyFiguresProvider().get(ctx)
        expected_tvpi = float(multiples.loc[inv, "TVPI"])
        expected_dpi = float(multiples.loc[inv, "DPI"])
        expected_irr = float(irr.loc[inv, "IRR"])

        if expected_tvpi == expected_tvpi:  # not NaN
            assert kf.tvpi == pytest.approx(expected_tvpi, abs=1e-9)
        else:
            assert kf.tvpi is None

        if expected_dpi == expected_dpi:
            assert kf.dpi == pytest.approx(expected_dpi, abs=1e-9)
        else:
            assert kf.dpi is None

        if expected_irr == expected_irr:
            assert kf.irr == pytest.approx(expected_irr, abs=1e-9)
        else:
            assert kf.irr is None

        assert kf.nav_eur is not None
        assert kf.nav_eur > 0


def test_portfolio_key_figures_use_aggregated_streams(
    populated_store_canonical: None,  # noqa: ARG001
    basic_ctx: ProviderContext,
) -> None:
    """Portfolio-level NAV equals the sum of latest NAVs."""
    kf = KeyFiguresProvider().get(basic_ctx)
    # NAVs at report date: 130_000 + 230_000 + 60_000 = 420_000
    assert kf.nav_eur == pytest.approx(420_000.0, abs=1e-6)
    # TVPI = (sum_dist + total_NAV) / sum_calls = (110_000 + 420_000) / 430_000
    assert kf.tvpi == pytest.approx((110_000.0 + 420_000.0) / 430_000.0, abs=1e-9)
    # DPI = sum_dist / sum_calls = 110_000 / 430_000
    assert kf.dpi == pytest.approx(110_000.0 / 430_000.0, abs=1e-9)
