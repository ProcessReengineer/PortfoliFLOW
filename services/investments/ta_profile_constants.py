# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Takahashi–Alexander parameter defaults, per capital-account type (ADR-0105 §3).

One coarse parameter set per capital-account ``investment_type`` — the
``RC_t`` rate-of-contribution schedule, growth ``G``, distribution bow ``B``,
and fund lifetime ``L`` — for the pure remaining-profile generator
(:mod:`services.investments.ta_profile`). This module is the **single home**
for those numbers: ADR-0105 §3 fixes them as code defaults with *no schema, no
tenant/investment overrides, and no tuning surface* in v1, so a second copy
anywhere would be a governance surface the ADR deliberately withheld.

Provenance
----------
The model and its base case are Takahashi & Alexander's:

    Takahashi, D. & Alexander, S. (2002). "Illiquid Alternative Asset Fund
    Modeling." *The Journal of Portfolio Management*, 28(1), 90–100.

Their private-equity base case anchors the :data:`Archetype.CAPITAL_ACCOUNT`
defaults here: a long fund life, an annual NAV growth around the low teens, a
distribution *bow* around 2.5 (distributions concentrated in the back half of
life), and a front-loaded contribution schedule that draws the commitment down
over the first several years. The three non-PE capital-account variants
(private debt, real estate, infrastructure equity) are **coarse
practitioner-informed adaptations** of that base case — a faster or slower
drawdown, a shorter or longer life, an earlier or later bow — not values lifted
verbatim from a single published table. That coarseness is the ADR's explicit
posture (ADR-0105 §3, §Consequences): "deliberately coarse … v1 accepts this
visibly rather than hiding it." A richer, sourced, per-strategy calibration is
a successor ADR with a migration, not a quiet edit to this file.

The four keys are exactly the ``investment_type`` values that resolve to
:attr:`services.investments.archetype.Archetype.CAPITAL_ACCOUNT`
(``private_equity``, ``private_debt``, ``real_estate``, ``infra_equity``); a
type outside them has no capital-account drawdown shape to model and is
rejected loudly by :func:`parameters_for`, never defaulted — the same posture
:func:`services.investments.archetype.resolve_archetype` takes towards an
unknown type at its own seam, kept here so a mis-routed non-capital-account
fund cannot silently receive a private-equity profile.

Import-pure — stdlib only, no database, no FastAPI, no provider SDK — guarded
by ``tests/regression/test_ta_profile_pure.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from core.exceptions import ValidationError


class TAProfileUnsupportedTypeError(ValidationError):
    """Raised when an ``investment_type`` has no capital-account TA parameters.

    ADR-0105 §3 fixes one parameter set per capital-account type and nothing
    else. A type outside those four — ``listed_equity``, ``listed_bonds``,
    ``cash``, ``other``, or any unknown string — has no drawdown shape to
    project, and the generator refuses it rather than falling back to a
    private-equity default it was never given standing to assume. The web
    layer (were it ever surfaced) would render it ``400`` with
    ``field='investment_type'``, like its sibling :class:`ValidationError`
    subclasses.
    """


@dataclass(frozen=True)
class TAParameters:
    """One capital-account type's Takahashi–Alexander parameters (ADR-0105 §3).

    Attributes:
        rate_of_contribution: The ``RC_t`` schedule — the fraction of the
            **remaining** uncalled commitment drawn in each model year, index 0
            being the first model year. The final entry is the steady-state
            rate: a model year beyond the schedule's length reuses it, so the
            drawdown tapers naturally as the uncalled balance shrinks rather
            than stopping at a hard cliff. Rates are fractions in ``[0, 1]``.
        growth: The annual NAV growth rate ``G`` applied to the prior year's
            NAV before contributions and distributions (a fraction, e.g.
            ``Decimal("0.13")`` for 13%).
        bow: The distribution bow exponent ``B`` in the rate
            ``d_t = (t / L) ** B``. A larger bow pushes distributions later in
            the fund's life; a smaller bow distributes earlier and more evenly.
        lifetime_years: The fund life ``L`` in model years — the number of
            annual model periods generated from ``t0``, the last of which
            liquidates the residual NAV.
    """

    rate_of_contribution: tuple[Decimal, ...]
    growth: Decimal
    bow: Decimal
    lifetime_years: int


#: The coarse default parameter set per capital-account ``investment_type``
#: (ADR-0105 §3). Keyed by exactly the four ``investment_type`` values that map
#: to :attr:`services.investments.archetype.Archetype.CAPITAL_ACCOUNT`. See the
#: module docstring for provenance: the ``private_equity`` row follows the
#: Takahashi–Alexander (2002) private-equity base case; the other three are
#: coarse practitioner-informed adaptations of it.
_PARAMETERS_BY_TYPE: dict[str, TAParameters] = {
    # Private equity (buyout) — the Takahashi–Alexander base case: a slow,
    # front-loaded drawdown over the first several years, low-teens growth, a
    # back-loaded bow (~2.5), and a long ~12-year life.
    "private_equity": TAParameters(
        rate_of_contribution=(
            Decimal("0.25"),
            Decimal("0.30"),
            Decimal("0.30"),
            Decimal("0.25"),
        ),
        growth=Decimal("0.13"),
        bow=Decimal("2.5"),
        lifetime_years=12,
    ),
    # Private debt — capital drawn faster, lower growth, and distributions
    # arriving earlier and more evenly (a lower bow) over a shorter life, as
    # income-oriented credit funds return cash sooner than buyout equity.
    "private_debt": TAParameters(
        rate_of_contribution=(
            Decimal("0.50"),
            Decimal("0.40"),
            Decimal("0.25"),
        ),
        growth=Decimal("0.08"),
        bow=Decimal("1.5"),
        lifetime_years=8,
    ),
    # Real estate — a moderate drawdown and a mid bow over a ~10-year life,
    # between the credit and buyout shapes.
    "real_estate": TAParameters(
        rate_of_contribution=(
            Decimal("0.35"),
            Decimal("0.35"),
            Decimal("0.20"),
        ),
        growth=Decimal("0.09"),
        bow=Decimal("2.0"),
        lifetime_years=10,
    ),
    # Infrastructure equity — a long life, a steady drawdown, and a relatively
    # early, even distribution profile (a lower bow) reflecting the yield-like
    # cash returns of operating infrastructure assets.
    "infra_equity": TAParameters(
        rate_of_contribution=(
            Decimal("0.30"),
            Decimal("0.30"),
            Decimal("0.25"),
            Decimal("0.20"),
        ),
        growth=Decimal("0.10"),
        bow=Decimal("1.8"),
        lifetime_years=15,
    ),
}

#: The capital-account ``investment_type`` values a TA profile exists for —
#: the keys of :data:`_PARAMETERS_BY_TYPE`, exposed for callers that want the
#: supported set without reaching into the private table.
CAPITAL_ACCOUNT_TYPES: frozenset[str] = frozenset(_PARAMETERS_BY_TYPE)


def parameters_for(investment_type: str) -> TAParameters:
    """Return the TA parameter set for a capital-account ``investment_type``.

    Args:
        investment_type: A canonical ``investments.investment_type`` value.

    Returns:
        The :class:`TAParameters` for a capital-account type.

    Raises:
        TAProfileUnsupportedTypeError: If ``investment_type`` is not one of the
            four capital-account types. The lookup never falls back to a
            default set — an un-modelled type is surfaced as such (ADR-0105 §3),
            so a mis-routed fund cannot receive a private-equity profile it was
            never entitled to.
    """
    try:
        return _PARAMETERS_BY_TYPE[investment_type]
    except KeyError:
        raise TAProfileUnsupportedTypeError(
            f"no Takahashi–Alexander parameter set for investment type "
            f"{investment_type!r}: a TA profile exists only for the "
            f"capital-account types {sorted(CAPITAL_ACCOUNT_TYPES)} "
            f"(ADR-0105 §3), and an un-modelled type is surfaced as such, "
            f"never defaulted.",
            field="investment_type",
        ) from None


__all__ = [
    "CAPITAL_ACCOUNT_TYPES",
    "TAParameters",
    "TAProfileUnsupportedTypeError",
    "parameters_for",
]
