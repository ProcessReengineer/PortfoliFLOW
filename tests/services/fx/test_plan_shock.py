# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The ``fx_shock`` rate restatement (ADR-0104 §2/§3).

The arithmetic half of the FX shock, tested where it lives: given a converter
and a ``(currency, magnitude)`` pair, a **new** converter whose plan-world FX
path for that currency is restated. The seam half — that the restatement runs
after the value fold and before functional aggregation — is pinned in
``tests/services/investments/test_cash_flow_timeline.py``.

What is pinned here:

* **Direction.** ``rate_to_reference`` is normatively the price of one unit of
  the currency in the reference currency (ADR-0099 §2), so a −10 % shock scales
  it by 0.9 and one unit of the currency buys 10 % less functional currency.
  A book long USD therefore *falls* in functional terms under a −10 % USD shock.
* **The seam gate.** The restatement takes effect **strictly after t₀**. Rates
  in force at or before it prevailed; restating them would rewrite the
  functional value of realised statements, which ADR-0104 §5's
  identical-history invariant forbids — the same exclusive boundary
  :func:`services.overlay.steps.scale_after` applies to a ``market_shock``.
* **Triangulation.** The shocked currency appears in exactly one leg of
  ``rate(from) / rate(to)``, so its own cross moves and every cross that does
  not involve it stays put — including when the shocked currency is the
  *reference*, whose rate is an application-level identity and never a row.
* **Immutability.** The baseline converter survives untouched; the
  Baseline/Scenario pair is rendered from one request (ADR-0104 §4).
* **The two structural no-ops** — the identity converter and the functional
  currency — and the fact that a shock never *invents* a rate.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from core.exceptions import MissingFxRateError
from services.fx.conversion import FxConverter
from services.fx.functional_currency import PortfolioFxConverter
from services.fx.plan_shock import SHOCK_NEUTRAL, shock_factor, shock_plan_fx_path

_D = Decimal

#: The plan/actual seam of every fixture. Rates dated at or before it are
#: realised; the plan world reads them held flat past it (ADR-0104 §3, N1).
_T0 = date(2026, 6, 30)

#: A plan-horizon date — right of the seam, past every stored rate, so the
#: lookup is the carry-forward the shock has to reach.
_PLAN = date(2026, 12, 31)


def _converter(
    rates: dict[str, dict[str, str]],
    *,
    functional_currency: str = "EUR",
    reference_currency: str = "EUR",
) -> PortfolioFxConverter:
    """A rate-backed converter. ``rates`` is ``{currency: {iso date: rate}}``."""
    frame = pd.DataFrame(
        [
            {
                "as_of_date": pd.Timestamp(day),
                "currency": currency,
                "rate_to_reference": _D(rate),
                "reference_currency": reference_currency,
            }
            for currency, series in rates.items()
            for day, rate in series.items()
        ]
    )
    return PortfolioFxConverter(functional_currency, FxConverter(frame, reference_currency))


def _eur_usd_gbp() -> PortfolioFxConverter:
    """EUR-functional, EUR-reference, one realised rate per foreign currency."""
    return _converter(
        {
            "USD": {"2026-06-30": "0.92"},
            "GBP": {"2026-06-30": "1.15"},
        }
    )


def _shocked(
    converter: PortfolioFxConverter, currency: str, magnitude: str
) -> PortfolioFxConverter:
    """Apply one shock at the fixture seam."""
    return shock_plan_fx_path(converter, [(currency, _D(magnitude))], t0=_T0)


# ---------------------------------------------------------------------------
# 1. The magnitude → factor rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("magnitude", "expected"),
    [("-10", "0.9"), ("10", "1.1"), ("-100", "0"), ("0", "1")],
)
def test_shock_factor_is_exact_decimal(magnitude: str, expected: str) -> None:
    """``1 + magnitude / 100``, in Decimal — never through a float."""
    assert shock_factor(_D(magnitude)) == _D(expected)


def test_the_neutral_magnitude_returns_the_very_converter() -> None:
    """A 0 % shock says nothing, and says it without rebuilding a rate path."""
    converter = _eur_usd_gbp()
    assert _shocked(converter, "USD", str(SHOCK_NEUTRAL)) is converter


def test_no_shocks_returns_the_very_converter() -> None:
    """The empty case is the identity — the baseline leg of the toggle."""
    converter = _eur_usd_gbp()
    assert shock_plan_fx_path(converter, [], t0=_T0) is converter


# ---------------------------------------------------------------------------
# 2. Direction, and the seam gate (ADR-0104 §3, §5)
# ---------------------------------------------------------------------------


def test_a_negative_shock_lowers_the_functional_value_of_the_currency() -> None:
    """−10 % on USD: one USD buys 10 % less EUR over the plan horizon.

    The direction the codebase's own quoting convention fixes (ADR-0099 §2,
    normative): ``rate_to_reference`` is the price of one unit of the currency
    in the reference currency, so scaling ``USD → 0.92`` by 0.9 makes one USD
    worth 0.828 EUR. A book long USD is worth *less* in functional terms — the
    reading an operator means by "the dollar weakens 10 %".
    """
    shocked = _shocked(_eur_usd_gbp(), "USD", "-10")
    assert shocked.convert_amount(_D("100"), "USD", _PLAN) == _D("82.800")


def test_a_positive_shock_raises_it() -> None:
    """+10 % is the mirror image, and the sign is not silently swallowed."""
    shocked = _shocked(_eur_usd_gbp(), "USD", "10")
    assert shocked.convert_amount(_D("100"), "USD", _PLAN) == _D("101.200")


def test_realised_rates_at_or_before_the_seam_are_untouched() -> None:
    """The identical-history invariant reaches the rate path (ADR-0104 §5).

    A shock restates the **held-flat plan path** (§3, N1), not the rates that
    actually prevailed. Left of t₀ the two worlds convert identically, so the
    scenario cannot restate the functional value of a realised statement — the
    same exclusive seam ``scale_after`` applies to a value path.
    """
    baseline = _eur_usd_gbp()
    shocked = _shocked(baseline, "USD", "-10")

    at_seam = _D("100")
    assert shocked.convert_amount(at_seam, "USD", _T0) == _D("92.00")
    assert shocked.convert_amount(at_seam, "USD", _T0) == (
        baseline.convert_amount(at_seam, "USD", _T0)
    )
    # And one day later — the first plan day — the shock is in full force.
    assert shocked.convert_amount(at_seam, "USD", date(2026, 7, 1)) == _D("82.800")


def test_rates_stored_after_the_seam_are_scaled_too() -> None:
    """A dataset extending past t₀ keeps its shape, scaled — no flat step."""
    converter = _converter({"USD": {"2026-06-30": "0.92", "2026-08-31": "1.00"}})
    shocked = _shocked(converter, "USD", "-10")

    assert shocked.convert_amount(_D("100"), "USD", _T0) == _D("92.00")
    assert shocked.convert_amount(_D("100"), "USD", date(2026, 7, 15)) == _D("82.800")
    assert shocked.convert_amount(_D("100"), "USD", _PLAN) == _D("90.000")


def test_the_shocked_path_is_still_carried_forward() -> None:
    """The plan world reads the restated level held flat, arbitrarily far out."""
    shocked = _shocked(_eur_usd_gbp(), "USD", "-10")
    assert shocked.convert_amount(_D("100"), "USD", date(2031, 3, 31)) == _D("82.800")


# ---------------------------------------------------------------------------
# 3. Triangulation (ADR-0099 §2)
# ---------------------------------------------------------------------------


def test_an_unshocked_currency_is_bit_identical() -> None:
    """A USD shock is not a statement about GBP."""
    baseline = _eur_usd_gbp()
    shocked = _shocked(baseline, "USD", "-10")

    assert shocked.convert_amount(_D("100"), "GBP", _PLAN) == (
        baseline.convert_amount(_D("100"), "GBP", _PLAN)
    )
    assert shocked.convert_amount(_D("100"), "GBP", _PLAN) == _D("115.00")


def test_the_functional_leg_triangulates_after_the_restatement() -> None:
    """Functional ≠ reference: the shocked currency moves only its own cross.

    EUR is the reference the rates are quoted against; CHF is what the tenant
    reports in. ``rate(from) / rate(to)`` therefore has a real ``to`` leg, and
    a USD shock must move USD/CHF while leaving EUR/CHF exactly where it was.
    """
    baseline = _converter(
        {"USD": {"2026-06-30": "0.92"}, "CHF": {"2026-06-30": "1.05"}},
        functional_currency="CHF",
        reference_currency="EUR",
    )
    shocked = _shocked(baseline, "USD", "-10")

    before = baseline.convert_amount(_D("100"), "USD", _PLAN)
    after = shocked.convert_amount(_D("100"), "USD", _PLAN)
    assert after == before * _D("0.9")

    # The `to` leg is untouched, so a EUR position's CHF value does not move.
    assert shocked.convert_amount(_D("100"), "EUR", _PLAN) == (
        baseline.convert_amount(_D("100"), "EUR", _PLAN)
    )


def test_shocking_the_reference_currency_moves_its_crosses_only() -> None:
    """``rate(reference) = 1`` is an identity, never a row — and still shockable.

    ``ck_fx_rates_currency_not_reference`` forbids a row for the reference
    currency, so its path cannot be scaled in place. Scaling every *other*
    currency by ``1 / factor`` is the same statement: EUR/CHF falls 10 % and the
    USD/CHF cross — which does not involve EUR — does not move. Without this
    branch the shock would vanish silently, which is the failure mode a scenario
    surface must not have.

    The untouched cross is compared to a tolerance rather than bit-identically:
    the inversion divides both legs, and Decimal division rounds at the context's
    28 significant digits. The residue is ~1e-26 relative — arithmetic dust, not
    a moved number.
    """
    baseline = _converter(
        {"USD": {"2026-06-30": "0.92"}, "CHF": {"2026-06-30": "1.05"}},
        functional_currency="CHF",
        reference_currency="EUR",
    )
    shocked = _shocked(baseline, "EUR", "-10")

    eur_before = baseline.convert_amount(_D("100"), "EUR", _PLAN)
    eur_after = shocked.convert_amount(_D("100"), "EUR", _PLAN)
    assert eur_after == pytest.approx(eur_before * _D("0.9"))

    usd_before = baseline.convert_amount(_D("100"), "USD", _PLAN)
    usd_after = shocked.convert_amount(_D("100"), "USD", _PLAN)
    assert usd_after == pytest.approx(usd_before)

    # And the realised segment is untouched here too.
    assert shocked.convert_amount(_D("100"), "EUR", _T0) == (
        baseline.convert_amount(_D("100"), "EUR", _T0)
    )


# ---------------------------------------------------------------------------
# 4. Immutability and composition
# ---------------------------------------------------------------------------


def test_the_baseline_converter_is_never_mutated() -> None:
    """The Baseline leg of the toggle converts through the converter it was given."""
    baseline = _eur_usd_gbp()
    before = baseline.convert_amount(_D("100"), "USD", _PLAN)

    _shocked(baseline, "USD", "-10")

    assert baseline.convert_amount(_D("100"), "USD", _PLAN) == before == _D("92.00")


def test_two_shocks_on_one_currency_compose_multiplicatively() -> None:
    """List order, folded: −10 % then −10 % is 0.81, not 0.80."""
    shocked = shock_plan_fx_path(
        _eur_usd_gbp(),
        [("USD", _D("-10")), ("USD", _D("-10"))],
        t0=_T0,
    )
    assert shocked.convert_amount(_D("100"), "USD", _PLAN) == _D("74.5200")


def test_two_currencies_shocked_independently() -> None:
    """Each leg carries its own factor."""
    shocked = shock_plan_fx_path(
        _eur_usd_gbp(),
        [("USD", _D("-10")), ("GBP", _D("20"))],
        t0=_T0,
    )
    assert shocked.convert_amount(_D("100"), "USD", _PLAN) == _D("82.800")
    assert shocked.convert_amount(_D("100"), "GBP", _PLAN) == _D("138.000")


def test_the_currency_code_is_matched_case_insensitively() -> None:
    """The rate dataset is uppercase by convention; a hand-typed link may not be."""
    shocked = _shocked(_eur_usd_gbp(), "usd", "-10")
    assert shocked.convert_amount(_D("100"), "USD", _PLAN) == _D("82.800")


# ---------------------------------------------------------------------------
# 5. The structural no-ops (ADR-0099 §3, the zero-read guarantee)
# ---------------------------------------------------------------------------


def test_a_shock_on_an_identity_converter_is_a_no_op() -> None:
    """A single-currency tenant loaded no FX row, and a shock does not make it.

    The converter is handed to the seam already built. An ``fx_shock`` is
    therefore *structurally* unable to cause an FX read the baseline would not
    have made — the ADR-0099 §3 zero-read guarantee holding under a scenario.
    """
    identity = PortfolioFxConverter("EUR", None)
    shocked = _shocked(identity, "USD", "-10")

    assert shocked is identity
    assert shocked.is_identity
    assert shocked.convert_amount(_D("100"), "EUR", _PLAN) == _D("100")


def test_shocking_the_functional_currency_is_a_no_op() -> None:
    """The functional currency is the numéraire; you cannot shock the ruler.

    The path an ``fx_shock`` restates is the one "used to translate every
    position of that currency into the functional currency" (ADR-0104 §2) — and
    for the functional currency that path is the identity, short-circuited before
    any rate is consulted. An operator who means "EUR weakens" is making a
    statement about the other currencies, and states it by shocking them.
    """
    baseline = _eur_usd_gbp()
    shocked = _shocked(baseline, "EUR", "-10")

    assert shocked is baseline
    assert shocked.convert_amount(_D("100"), "USD", _PLAN) == _D("92.00")


# ---------------------------------------------------------------------------
# 6. A shock restates a path; it never invents one
# ---------------------------------------------------------------------------


def test_a_shock_on_an_unpriced_currency_does_not_paper_over_the_missing_pair() -> None:
    """The typed failure survives the shock — no fabricated rate, no silent zero.

    The book holds JPY but the dataset never priced it. Shocking JPY cannot
    conjure a path to scale, so the conversion that needs one still raises
    :class:`~core.exceptions.MissingFxRateError` naming the currency and the
    date. A shock that quietly made an uncovered currency convertible would be
    the ADR-0099 §3 1:1 fallback by another route.
    """
    shocked = _shocked(_eur_usd_gbp(), "JPY", "-10")

    with pytest.raises(MissingFxRateError) as caught:
        shocked.convert_amount(_D("100"), "JPY", _PLAN)

    assert caught.value.currency == "JPY"
    assert caught.value.as_of_date == _PLAN


def test_a_shock_before_a_currencys_first_rate_still_fails_loudly() -> None:
    """No anchor to carry forward: the gap in front of the first rate stays a gap."""
    converter = _converter({"USD": {"2026-08-31": "0.92"}})
    shocked = _shocked(converter, "USD", "-10")

    with pytest.raises(MissingFxRateError):
        shocked.convert_amount(_D("100"), "USD", date(2026, 1, 31))

    # Past the first stored rate it converts — shocked, since that rate is
    # right of the seam.
    assert shocked.convert_amount(_D("100"), "USD", _PLAN) == _D("82.800")
