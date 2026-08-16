# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Pure FX conversion layer for the functional-currency model (ADR-0099).

Exposes :class:`FxConverter`, the stateless conversion machinery that sits
at the ADR-0099 §4 boundary between data assembly and the pure analytics
layer. It consumes rate frames loaded by
:class:`core.repositories.fx_rate_repository.FxRateRepository` and performs
no I/O of its own, so ``services/analytics/`` keeps its single-currency
contract and its ADR-0013 purity untouched.

ADR-0099's Implementation Notes name this class ``FxConversionService``; it
is implemented as ``FxConverter`` because it is a pure value object built
from a frame, not a DB-touching Service in the glossary's sense.

The Block-3 conversion boundary (ADR-0099 §4) is assembled in
:mod:`services.fx.functional_currency`, which exposes
:class:`PortfolioFxConverter` and the :func:`build_portfolio_fx_converter`
builder both review and limits seams call.

:mod:`services.fx.plan_shock` adds the Planning Desk's one operation on that
boundary: an ``fx_shock`` (ADR-0104 §2/§3) restates a currency's plan-world FX
path *before the seam conversion runs*. It lives here rather than in
:mod:`services.overlay` because a rate path is not a plan frame — the overlay
package is rate-free and DB-free by regression guard, so the one transformation
kind that acts on the seam is applied by the seam.
"""

from services.fx.conversion import FxConverter
from services.fx.functional_currency import (
    PortfolioFxConverter,
    build_portfolio_fx_converter,
)
from services.fx.plan_shock import SHOCK_NEUTRAL, shock_factor, shock_plan_fx_path

__all__ = [
    "SHOCK_NEUTRAL",
    "FxConverter",
    "PortfolioFxConverter",
    "build_portfolio_fx_converter",
    "shock_factor",
    "shock_plan_fx_path",
]
