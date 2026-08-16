# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for the currency-exposure donut spec (ADR-0101 §1).

Pure spec generation — no DB, no browser. The tests pin the three things
the tile is accountable for:

* it is a **donut** over the exposure rows, in row order (the aggregation
  already sorted them; the spec must not re-sort);
* it carries the **"by position currency (unhedged)"** subtitle, the
  misreading-preemption ADR-0101 §Consequences requires;
* its colours come from the **theme** palette (crimson primary first), not
  a hardcoded hex (ADR-0021).
"""

from __future__ import annotations

from services.analytics.portfolio_aggregation import (
    CurrencyExposure,
    CurrencyExposureRow,
)
from services.chart_specs import build_currency_exposure_spec
from services.chart_specs.base import get_chart_theme
from services.chart_specs.portfolio_currency_exposure import (
    EXPOSURE_SUBTITLE,
)


def _sample_exposure() -> CurrencyExposure:
    """EUR 60 % / USD 30 % / CHF 10 % of a 1,000 functional-currency book."""
    return CurrencyExposure(
        rows=[
            CurrencyExposureRow(currency="EUR", amount=600.0, weight_pct=60.0),
            CurrencyExposureRow(currency="USD", amount=300.0, weight_pct=30.0),
            CurrencyExposureRow(currency="CHF", amount=100.0, weight_pct=10.0),
        ]
    )


class TestCurrencyExposureSpec:
    def test_renders_a_donut_not_a_pie(self) -> None:
        spec = build_currency_exposure_spec(_sample_exposure())
        trace = spec["data"][0]
        assert trace["type"] == "pie"
        assert trace["hole"] > 0.0

    def test_slices_carry_currencies_and_amounts_in_row_order(self) -> None:
        """Row order is the aggregation's (amount descending) — ``sort`` off."""
        spec = build_currency_exposure_spec(_sample_exposure())
        trace = spec["data"][0]
        assert trace["labels"] == ["EUR", "USD", "CHF"]
        assert trace["values"] == [600.0, 300.0, 100.0]
        # Plotly would otherwise re-sort and desync labels from the palette.
        assert trace["sort"] is False

    def test_subtitle_states_position_currency_and_unhedged(self) -> None:
        """The subtitle is load-bearing: it bounds what the donut claims."""
        spec = build_currency_exposure_spec(_sample_exposure())
        texts = [a["text"] for a in spec["layout"]["annotations"]]
        assert EXPOSURE_SUBTITLE in texts
        assert "by position currency (unhedged)" in texts

    def test_colours_come_from_the_theme_palette(self) -> None:
        palette = get_chart_theme()["colours"]["series_palette"]
        spec = build_currency_exposure_spec(_sample_exposure())
        colours = spec["data"][0]["marker"]["colors"]
        assert colours == palette[:3]
        # The crimson primary leads (ADR-0021).
        assert colours[0] == get_chart_theme()["colours"]["primary"]

    def test_hover_prefixes_amounts_with_the_functional_currency(self) -> None:
        """Slice labels are position currencies; the *amount* is functional."""
        eur = build_currency_exposure_spec(_sample_exposure(), functional_currency="EUR")
        chf = build_currency_exposure_spec(_sample_exposure(), functional_currency="CHF")
        assert "€%{value:,.0f}" in eur["data"][0]["hovertemplate"]
        assert "CHF %{value:,.0f}" in chf["data"][0]["hovertemplate"]

    def test_hovermode_is_closest_not_the_themed_x_unified(self) -> None:
        """``apply_theme`` defaults hovermode to "x unified" — meaningless here."""
        spec = build_currency_exposure_spec(_sample_exposure())
        assert spec["layout"]["hovermode"] == "closest"

    def test_theme_is_applied(self) -> None:
        theme = get_chart_theme()["colours"]
        spec = build_currency_exposure_spec(_sample_exposure())
        assert spec["layout"]["paper_bgcolor"] == theme["background"]

    def test_empty_exposure_yields_a_valid_slice_free_figure(self) -> None:
        spec = build_currency_exposure_spec(CurrencyExposure(rows=[]))
        assert spec["data"][0]["labels"] == []
        assert spec["data"][0]["values"] == []
        assert "layout" in spec and "config" in spec

    def test_palette_cycles_past_its_length(self) -> None:
        """A book in more currencies than the palette has entries still colours."""
        palette = get_chart_theme()["colours"]["series_palette"]
        rows = [
            CurrencyExposureRow(currency=f"C{i:02d}", amount=1.0, weight_pct=1.0)
            for i in range(len(palette) + 2)
        ]
        spec = build_currency_exposure_spec(CurrencyExposure(rows=rows))
        colours = spec["data"][0]["marker"]["colors"]
        assert len(colours) == len(rows)
        # Wraps rather than running off the end.
        assert colours[len(palette)] == palette[0]
