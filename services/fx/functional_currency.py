# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Per-request conversion into the tenant's functional currency (ADR-0099 §4).

The ADR-0099 conversion boundary sits in front of ``services/analytics/``:
every monetary series the analytics layer receives is already in one
currency — the tenant's functional currency. This module is where that
boundary is assembled, and it is the **single** seam both the Portfolio
Review (:class:`services.portfolio_review.PortfolioReviewService`) and the
Investment Limits (:class:`services.limits.LimitsCoverageService`) load
paths call, so the conversion setup lives once rather than twice.

Two properties are established here and nowhere else:

1. **The identity short-circuit is *proven*, not merely honoured.**
   :func:`build_portfolio_fx_converter` computes the set of position
   currencies that differ from the functional currency. When that set is
   empty — a single-currency tenant — it returns *without loading a single
   FX row*: :meth:`~core.repositories.fx_rate_repository.FxRateRepository.load_rates_frame`
   is never called and the returned :class:`PortfolioFxConverter` is a pure
   pass-through. This is the backwards-compatibility guarantee of
   ADR-0099 §3 made structural — a pure-EUR portfolio's figures are
   byte-identical to the pre-ADR-0099 result *because the FX machinery
   never runs*.
2. **The reference currency comes from the data.** The frame the FX
   repository returns is quoted against one reference currency (``EUR`` for
   the reference deployment). That reference, read off the frame, is
   authoritative for the :class:`~services.fx.conversion.FxConverter`. When
   the tenant's functional currency differs from it, the functional leg is
   loaded too — triangulation ``rate(from) / rate(to)`` needs the ``to``
   leg.

The shape-specific *application* stays with each seam: Seam A converts
``float64`` pandas series (:meth:`PortfolioFxConverter.convert_series`),
Seam B converts ``Decimal`` NAV amounts one at a time
(:meth:`PortfolioFxConverter.convert_amount`). Both go through the same
underlying converter, so a missing rate fails identically
(:class:`~core.exceptions.MissingFxRateError`) on either surface.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date as _date
from decimal import Decimal

import pandas as pd

from core.repositories.fx_rate_repository import FxRateRepository
from core.repositories.tenant_repository import TenantRepository
from services.fx.conversion import FxConverter

#: The functional currency assumed when no tenant row is visible. The
#: ``tenants.functional_currency`` column is ``NOT NULL DEFAULT 'EUR'``, so a
#: ``None`` read only ever means "no tenant in scope", never "no currency".
_DEFAULT_FUNCTIONAL_CURRENCY: str = "EUR"


class PortfolioFxConverter:
    """Convert monetary data from a position currency into the functional one.

    A thin, request-scoped facade over
    :class:`~services.fx.conversion.FxConverter` that pins the conversion
    target to the tenant's functional currency and carries the identity
    fast-path. Construct it via :func:`build_portfolio_fx_converter`, never
    directly — the builder is what enforces the zero-read guarantee.

    Attributes:
        functional_currency: The currency every conversion targets.
    """

    def __init__(self, functional_currency: str, converter: FxConverter | None) -> None:
        """Store the functional currency and the (optional) rate converter.

        Args:
            functional_currency: The tenant's reporting currency.
            converter: The rate-backed :class:`FxConverter`, or ``None`` for
                the identity fast-path — every position is already in the
                functional currency, so no rate frame was loaded.
        """
        self._functional_currency = functional_currency
        self._converter = converter

    @property
    def functional_currency(self) -> str:
        """The currency every conversion targets."""
        return self._functional_currency

    @property
    def is_identity(self) -> bool:
        """Whether this is a pure pass-through (no FX rows were loaded)."""
        return self._converter is None

    def convert_series(self, series: pd.Series, from_currency: str) -> pd.Series:
        """Convert a date-indexed float series into the functional currency.

        Point-in-time: each observation converts at the carry-forward rate
        of its own index date (ADR-0099 §4). On the identity fast-path, and
        whenever ``from_currency`` already equals the functional currency,
        the series is returned as an untouched copy — no rate is consulted.

        Args:
            series: Float-valued series indexed by date/timestamp.
            from_currency: The series' current (position) currency.

        Returns:
            A new series of converted values, preserving the input's index,
            name and ``float64`` dtype.

        Raises:
            MissingFxRateError: If a required rate is absent for any index
                date — never a silent 1:1 fallback.
        """
        if self._converter is None:
            return series.copy()
        return self._converter.convert_series(series, from_currency, self._functional_currency)

    def convert_amount(self, amount: Decimal, from_currency: str, as_of_date: _date) -> Decimal:
        """Convert one Decimal amount into the functional currency.

        The exact-arithmetic counterpart to :meth:`convert_series`, used by
        Seam B where NAV amounts are :class:`~decimal.Decimal` and the
        coverage engine computes in Decimal. Point-in-time at ``as_of_date``
        (carry-forward): an amount dated past the last stored rate converts
        at that last, frozen rate — the ADR-0060-style semantics, not a
        defect.

        Args:
            amount: The monetary amount, in ``from_currency``.
            from_currency: The amount's current (position) currency.
            as_of_date: The date whose carry-forward rate applies.

        Returns:
            The converted amount as an exact :class:`~decimal.Decimal`.

        Raises:
            MissingFxRateError: If a required rate is absent.
        """
        if self._converter is None:
            return amount
        return self._converter.convert(amount, from_currency, self._functional_currency, as_of_date)

    def shocked(self, currency: str, factor: Decimal, *, after: _date) -> PortfolioFxConverter:
        """Return a converter whose plan-world path for ``currency`` is shocked.

        The functional-currency face of :meth:`services.fx.conversion.FxConverter.shocked`
        — an ``fx_shock`` (ADR-0104 §2/§3) applied at this seam, after the
        value-level transformations and before functional aggregation. Two
        structural no-ops are decided **here**, because this is the only object
        that knows the functional currency:

        1. **The identity fast-path.** No FX row was loaded, so there is no path
           to restate and nothing to load one from. A single-currency tenant's
           scenario is its baseline, and the ADR-0099 §3 zero-read guarantee
           survives a shock in the parameter set: an ``fx_shock`` must never be
           the reason FX rates get read.
        2. **The functional currency is the numéraire.** The path an
           ``fx_shock`` restates is the one "used to translate every position
           of that currency into the functional currency" (ADR-0104 §2) — and
           for the functional currency itself that path is the identity, which
           :meth:`convert_amount` short-circuits before any rate is consulted.
           There is nothing to scale. An operator who means "the functional
           currency weakens" is making a statement about the *others*, and
           states it by shocking them.

        Args:
            currency: The currency whose plan-world FX path is restated.
            factor: The multiplier on the currency's value —
                ``1 + magnitude / 100``.
            after: The plan/actual seam t₀. Realised rates are untouched.

        Returns:
            A new :class:`PortfolioFxConverter`, or ``self`` where the shock
            changes nothing — the two no-ops above, a neutral factor, or a
            currency the dataset never priced. The identity is by ``is``, the
            same law :func:`services.overlay.pipeline.apply_overlay` holds for a
            neutral ``market_shock``: a scenario that says nothing returns the
            very world it was given, rather than an equal copy of it. Never a
            mutation: the baseline leg of the Baseline/Scenario pair converts
            through the unshocked converter.
        """
        if self._converter is None:
            return self
        if currency == self._functional_currency:
            return self
        shocked = self._converter.shocked(currency, factor, after=after)
        if shocked is self._converter:
            return self
        return PortfolioFxConverter(self._functional_currency, shocked)


async def build_portfolio_fx_converter(
    *,
    tenants: TenantRepository,
    fx_rates: FxRateRepository,
    position_currencies: Iterable[str],
) -> PortfolioFxConverter:
    """Assemble the per-request converter into the functional currency.

    The zero-read guarantee lives here: when every position currency already
    equals the functional currency, this returns an identity
    :class:`PortfolioFxConverter` **without calling**
    :meth:`~core.repositories.fx_rate_repository.FxRateRepository.load_rates_frame`
    — no FX row is read, so a single-currency tenant is provably unaffected
    (ADR-0099 §3).

    Otherwise it loads the rate frame once for exactly the non-functional
    currencies, reads the reference currency off the frame, and — when the
    functional currency differs from that reference — loads the functional
    leg too so triangulation has its ``to`` leg. The window is unbounded on
    both sides: FX histories are short and the carry-forward anchor needs the
    earliest rows regardless.

    Args:
        tenants: Tenant-scoped repository supplying the functional currency.
        fx_rates: Tenant-scoped FX-rate repository supplying the rate frame.
        position_currencies: The position currencies present in the request's
            investment universe. Duplicates and the functional currency are
            harmless — they are filtered out.

    Returns:
        A :class:`PortfolioFxConverter` ready to convert into the tenant's
        functional currency.
    """
    functional_currency = (
        await tenants.get_current_functional_currency() or _DEFAULT_FUNCTIONAL_CURRENCY
    )

    non_functional = sorted(
        {
            currency
            for currency in position_currencies
            if currency and currency != functional_currency
        }
    )
    if not non_functional:
        # Zero-read fast-path: no foreign currency in play, so no frame is
        # loaded and every conversion is an identity (ADR-0099 §3).
        return PortfolioFxConverter(functional_currency, None)

    frame = await fx_rates.load_rates_frame(non_functional)
    reference_currency = _reference_currency_of(frame, functional_currency)

    if functional_currency != reference_currency:
        # Triangulation rate(from)/rate(to) needs the functional (`to`) leg
        # in the frame when the functional currency is not the reference.
        functional_leg = await fx_rates.load_rates_frame([functional_currency])
        frame = pd.concat([frame, functional_leg], ignore_index=True)

    converter = FxConverter(frame, reference_currency)
    return PortfolioFxConverter(functional_currency, converter)


def _reference_currency_of(frame: pd.DataFrame, fallback: str) -> str:
    """Read the reference currency off a rates frame, or fall back.

    Every row of a well-formed frame carries the same ``reference_currency``
    (:class:`FxConverter` enforces this). An empty frame — a tenant with
    foreign positions but no rates at all — carries none, so the functional
    currency is used as the fallback; any *actual* conversion then fails
    loudly with :class:`~core.exceptions.MissingFxRateError`, which is the
    correct behaviour, not a silent pass.
    """
    if frame.empty:
        return fallback
    return str(frame["reference_currency"].iloc[0])


__all__ = ["PortfolioFxConverter", "build_portfolio_fx_converter"]
