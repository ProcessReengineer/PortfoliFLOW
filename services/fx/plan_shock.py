# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The ``fx_shock`` seam operation (ADR-0104 §2/§3).

An ``fx_shock`` is the one transformation kind that does **not** act on a value
path. ADR-0104 §2's kind table gives its scope as "one currency", what it acts
on as "the **conversion seam** — the plan-world FX path for that currency", and
its executor dispatch as "none"; the ADR's own Rationale draws the line in one
sentence — *"Three transformations act on values and dispatch on archetype; the
FX shock acts on the seam and is archetype-blind."* §3 fixes the ordering:
an active ``fx_shock`` "restates the held-flat path for its currency **before
the seam conversion runs**".

So this module is the ``fx_shock``'s executor in everything but name, and it
lives in :mod:`services.fx` rather than in :mod:`services.overlay` for a
structural reason, not a stylistic one: the FX path is not in
:class:`~services.overlay.pipeline.PlanFrames` and never has been. It lives in
the converter, built from rows the overlay package is forbidden to read
(``tests/regression/test_overlay_layer_pure.py``). A literal ``frames → frames``
executor would have nothing to act on. The overlay stays a pure parameter set;
the rate arithmetic stays with the rates.

**It is blind to the overlay contract.** The seam hands it ``(currency,
magnitude)`` pairs rather than :class:`~services.overlay.contract.FxShock`
values, so the FX layer — which sits *below* the Planning Desk — never imports
the scenario contract. Splitting the pairs out of an overlay is the overlay
package's own job
(:func:`services.overlay.pipeline.partition_fx_shocks`).

**Order.** Shocks fold in list order, and two shocks on one currency compose
multiplicatively (−10 % then −10 % is 0.81, not 0.80): each restates the path
the previous one left. Interleaving an ``fx_shock`` between two value
transformations changes nothing, and that is a theorem rather than an
oversight — the value executors touch only frames and the shock touches only
rates, so the two commute and the partition is order-preserving on the result.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date as _date
from decimal import Decimal

from services.fx.functional_currency import PortfolioFxConverter

#: Per-cent is the unit of both shock kinds' magnitude (ADR-0104 §2), so the
#: unified Scope → Operator → Magnitude → Timing form reads one way across
#: ``market_shock`` and ``fx_shock`` alike.
_PER_CENT: Decimal = Decimal(100)

#: The magnitude that says nothing. Folded through :func:`shock_factor` it is
#: the neutral factor, and the converter is returned untouched.
SHOCK_NEUTRAL: Decimal = Decimal(0)


def shock_factor(magnitude: Decimal) -> Decimal:
    """Translate a per-cent magnitude into a multiplicative factor.

    The same rule :class:`~services.overlay.contract.MarketShock` states for a
    level shift, applied to a rate: ADR-0104 §2 puts both magnitudes "in %",
    and a per-cent magnitude on a *rate* has no additive reading. So
    ``Decimal("-10")`` is ``0.9`` — one unit of the shocked currency is worth
    90 % of what it was, in every other currency.

    Args:
        magnitude: The move in per cent. Negative depreciates the currency,
            positive appreciates it.

    Returns:
        ``1 + magnitude / 100``. Exact: the division is Decimal, so a magnitude
        of ``-10`` yields exactly ``0.9`` and never ``0.8999999999999999``.
    """
    return Decimal(1) + magnitude / _PER_CENT


def shock_plan_fx_path(
    converter: PortfolioFxConverter,
    shocks: Sequence[tuple[str, Decimal]],
    *,
    t0: _date,
) -> PortfolioFxConverter:
    """Restate the plan-world FX paths the shocks name (ADR-0104 §3).

    The seam hook. Called with the converter the request already built — never
    with a repository, and never in a position to build one — so an
    ``fx_shock`` **cannot** cause an FX row to be read that the baseline would
    not have read. On a functional-currency-only book the converter is the
    identity fast-path and every shock folds through it unchanged, which is the
    ADR-0099 §3 zero-read guarantee holding under a scenario.

    Realised history is never restated: each shock takes effect **strictly
    after** ``t0``, so the actual columns of the two worlds convert at the rates
    that actually prevailed. That is the identical-history invariant (ADR-0104
    §5) reaching the total row, and it is the same exclusive seam
    :func:`services.overlay.steps.scale_after` applies to a ``market_shock``'s
    value path.

    A shock on a currency the plan world holds nothing in is **vacuous, not
    wrong**: it restates a path no balance reads. A shock on a currency the
    *dataset* never priced is not papered over either — no rate is fabricated,
    so the first conversion that needs one raises
    :class:`~core.exceptions.MissingFxRateError` naming the currency and the
    date, exactly as it would without the shock.

    Args:
        converter: The functional-currency converter of the request. Not
            mutated — the baseline leg of the Baseline/Scenario pair keeps
            converting through it (ADR-0104 §4).
        shocks: The ``(currency, magnitude)`` pairs, in overlay order.
            Magnitudes are in per cent. Currency codes are matched
            case-insensitively against the rate dataset, which is uppercase by
            convention, as are the timeline's cash-path keys.
        t0: The plan/actual seam (ADR-0060) the restatement takes effect after.

    Returns:
        A converter carrying the restated paths — ``converter`` itself where the
        shocks are empty or every one of them is a structural no-op.
    """
    for currency, magnitude in shocks:
        converter = converter.shocked(currency.upper(), shock_factor(magnitude), after=t0)
    return converter


__all__ = ["SHOCK_NEUTRAL", "shock_factor", "shock_plan_fx_path"]
