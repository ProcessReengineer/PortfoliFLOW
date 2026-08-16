# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Pure FX conversion into the functional currency (ADR-0099 §3).

:class:`FxConverter` is the conversion machinery the ADR-0099 boundary is
built from. It is **pure and stateless** under the ADR-0013 contract: it
performs no I/O, holds no session, and imports no repository. Rates arrive
as the frame :meth:`core.repositories.fx_rate_repository.FxRateRepository.load_rates_frame`
produces; the converter never goes looking for more. Block 3 wires it into
the data-assembly seam in front of ``services/analytics/`` — this module
lands dormant.

Three currency concepts meet here and must not be conflated: the
**functional currency** (the tenant's reporting currency), the **position
currency** (an investment's own), and the **reference currency** (the base
the FX dataset is quoted against). This converter knows only the last one;
the other two are simply the ``from``/``to`` arguments its callers pass.

Semantics, in the order the code applies them:

1. **Identity short-circuit.** ``from == to`` returns the amount untouched
   — no frame consulted, no rate looked up. This is the
   backwards-compatibility guarantee of ADR-0099 §3: a single-currency
   tenant holds zero FX rows and every figure it computes is byte-identical
   to the pre-ADR-0099 result.
2. **Carry-forward.** The rate applied on a date is the latest at or
   before it, mirroring the ADR-0060 NAV carry-forward and the
   ``_latest_at_or_before`` idiom in
   ``services/analytics/portfolio_aggregation.py`` (reimplemented here
   rather than imported — analytics must stay a leaf, and the dependency
   would run the wrong way). ECB-style series have weekend and holiday
   gaps; without carry-forward a Saturday NAV would have no rate.
3. **Triangulation.** ``amount × rate(from) / rate(to)``, where
   ``rate(reference) = 1``. Storing rates against a single reference keeps
   the dataset linear rather than quadratic in the number of currencies.
4. **Typed failure.** A rate that cannot be resolved raises
   :class:`~core.exceptions.MissingFxRateError`. There is no silent 1:1
   fallback, anywhere — that is precisely the wrong-number failure mode
   ADR-0099 exists to eliminate.
5. **Restatement.** :meth:`FxConverter.shocked` returns a *new* converter
   whose rate path for one currency is rescaled strictly after a seam date —
   the rate arithmetic an ``fx_shock`` is made of (ADR-0104 §2/§3). It lives
   here because this is where rates live: the overlay package is rate-free and
   DB-free, and the shock acts on the conversion seam rather than on any value
   path. Immutability is the contract — the Planning Desk renders baseline and
   scenario from the same request and needs the unshocked converter intact.

**The Decimal / float boundary.** The repository speaks
:class:`~decimal.Decimal`, because that is what the database holds and what
a monetary amount deserves. The analytics layer speaks ``float64``, because
that is what pandas and numpy compute in. :class:`FxConverter` keeps both
honest instead of picking a side: it ingests the frame's ``Decimal`` rates
once and retains **both** representations, so :meth:`convert` is exact
``Decimal`` arithmetic (``Decimal`` in, ``Decimal`` out) while
:meth:`convert_series` is a vectorised ``float64`` path over a date-indexed
:class:`pandas.Series`. Neither path ever silently converts through the
other.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date as _date
from datetime import timedelta
from decimal import Decimal

import numpy as np
import pandas as pd

from core.exceptions import MissingFxRateError, ValidationError

#: The identity rate, returned for the reference currency without a lookup.
_IDENTITY_RATE: Decimal = Decimal(1)

#: The rescale that changes nothing. Short-circuited rather than applied, so a
#: neutral shock returns the very converter it was handed.
_NEUTRAL_FACTOR: Decimal = Decimal(1)

#: The frame :meth:`FxConverter._from_rate_paths` constructs through. The
#: constructor's ``rates.empty`` branch returns before touching a column, so
#: the rebuilt converter takes its paths from the argument rather than a frame.
_EMPTY_FRAME: pd.DataFrame = pd.DataFrame()


@dataclass(frozen=True)
class _CurrencyRates:
    """One currency's rate series, in both representations.

    ``dates`` is sorted ascending and drives the ``bisect``-based scalar
    lookup; ``dates64`` is the same series as ``datetime64[ns]`` and drives
    the vectorised ``searchsorted`` lookup. ``rates`` (exact) and
    ``rates64`` (float) are parallel to both.
    """

    dates: list[_date]
    dates64: np.ndarray
    rates: list[Decimal]
    rates64: np.ndarray


def _normalise_index(index: pd.Index) -> pd.DatetimeIndex:
    """Coerce a series index to a naive :class:`pandas.DatetimeIndex`.

    Timezone-aware indices (the cashflow frames carry UTC timestamps) are
    converted to UTC and stripped, so a same-day comparison against the
    midnight-stamped rate dates behaves as "the rate of that day". The
    original index is never replaced on the way out — this normalisation is
    for lookup only.
    """
    normalised = pd.DatetimeIndex(pd.to_datetime(index))
    if normalised.tz is not None:
        normalised = normalised.tz_convert("UTC").tz_localize(None)
    return normalised


def _restate_after(
    known: _CurrencyRates,
    after: _date,
    scale: Callable[[Decimal], Decimal],
) -> _CurrencyRates:
    """Rescale one currency's rate path strictly after ``after``.

    The path is a step function read by carry-forward, so restating "the rate
    in force from ``after`` onward" is not the same as rescaling the stored
    rows dated after it: a plan date typically has **no** row of its own and
    resolves back to the last realised rate. Three parts therefore compose the
    new path:

    * the rows at or before ``after`` — bit-identical, the realised segment;
    * an **anchor** one day past ``after``, carrying the scaled level of the
      rate in force *at* ``after`` — this is the held-flat plan path (ADR-0104
      §3, N1) restated, and it is what the plan horizon's carry-forward
      lookups land on;
    * the rows after ``after`` — scaled, so a dataset that already extends
      past the seam keeps its shape.

    One day is the right step because both lookups are day-granular: the scalar
    path bisects a list of :class:`datetime.date`, and the vectorised path
    compares midnight-stamped rate dates against the query index, so no query
    can fall between ``after`` and ``after + 1 day`` and read the wrong
    segment. The anchor is omitted where the row at ``after + 1 day`` already
    exists (it would duplicate an index point) and where no rate is in force at
    ``after`` at all (there is no level to carry forward — a conversion in that
    gap raises :class:`~core.exceptions.MissingFxRateError`, as it did before).

    Args:
        known: The currency's rate path. Not mutated.
        after: The seam. Rescaling takes effect strictly after it.
        scale: The rescale applied to a Decimal rate — ``r * factor`` for the
            shocked currency, ``r / factor`` for every other currency when the
            **reference** is the one shocked. Exact Decimal arithmetic in both
            directions; the float representation is derived from the result, so
            the two never drift.

    Returns:
        A new :class:`_CurrencyRates` with both representations rebuilt.
    """
    anchor_date = after + timedelta(days=1)

    head = [(day, rate) for day, rate in zip(known.dates, known.rates, strict=True) if day <= after]
    tail = [
        (day, scale(rate))
        for day, rate in zip(known.dates, known.rates, strict=True)
        if day > after
    ]
    if head and (not tail or tail[0][0] != anchor_date):
        tail.insert(0, (anchor_date, scale(head[-1][1])))

    restated = head + tail
    dates = [day for day, _ in restated]
    rates = [rate for _, rate in restated]
    return _CurrencyRates(
        dates=dates,
        dates64=np.array(dates, dtype="datetime64[ns]"),
        rates=rates,
        rates64=np.array([float(rate) for rate in rates], dtype=float),
    )


class FxConverter:
    """Point-in-time currency conversion over a fixed set of rates.

    Constructed from the tidy rates frame the FX repository loads and the
    reference currency those rates are quoted against. Instances are
    immutable and safe to share across a request.

    Args:
        rates: Frame with columns ``as_of_date`` (datetime-like),
            ``currency``, ``rate_to_reference`` (:class:`~decimal.Decimal`)
            and ``reference_currency``, as returned by
            :meth:`core.repositories.fx_rate_repository.FxRateRepository.load_rates_frame`.
            An **empty frame is valid and expected**: an EUR-only tenant has
            no FX rows, and every conversion it asks for is an identity.
        reference_currency: The base every stored rate is quoted against.

    Raises:
        ValidationError: If any row of ``rates`` is quoted against a
            different reference currency than the one declared. Mixed
            references in one frame would make triangulation meaningless,
            and the mistake is silent unless it is checked here.
    """

    def __init__(self, rates: pd.DataFrame, reference_currency: str) -> None:
        self._reference_currency = reference_currency
        self._by_currency: dict[str, _CurrencyRates] = {}

        if rates.empty:
            return

        declared = set(rates["reference_currency"].unique())
        if declared != {reference_currency}:
            raise ValidationError(
                "FX rates frame is quoted against "
                f"{sorted(declared)!r}, not {reference_currency!r}. "
                "Triangulation requires a single reference currency.",
                field="reference_currency",
            )

        ordered = rates.sort_values(["currency", "as_of_date"])
        for currency, group in ordered.groupby("currency", sort=False):
            dates64 = group["as_of_date"].to_numpy(dtype="datetime64[ns]")
            decimals = [Decimal(str(r)) for r in group["rate_to_reference"]]
            self._by_currency[str(currency)] = _CurrencyRates(
                dates=[ts.date() for ts in group["as_of_date"]],
                dates64=dates64,
                rates=decimals,
                rates64=np.array([float(r) for r in decimals], dtype=float),
            )

    @property
    def reference_currency(self) -> str:
        """The base currency every stored rate is quoted against."""
        return self._reference_currency

    def rate(self, currency: str, as_of_date: _date) -> Decimal:
        """Return the rate of one unit of ``currency`` on ``as_of_date``.

        The rate is the latest stored one at or before ``as_of_date``
        (carry-forward). The reference currency short-circuits to ``1``
        without a lookup, so it needs no rows — and by
        ``ck_fx_rates_currency_not_reference`` it has none.

        Args:
            currency: The currency to price, in the reference currency.
            as_of_date: The date to price it on.

        Returns:
            The price of one unit of ``currency`` in the reference
            currency, as an exact :class:`~decimal.Decimal`.

        Raises:
            MissingFxRateError: If ``currency`` has no rate at or before
                ``as_of_date`` — either it is uncovered entirely, or
                ``as_of_date`` precedes its first stored rate.
        """
        return self._rate(currency, as_of_date, leg=None)

    def convert(
        self,
        amount: Decimal,
        from_currency: str,
        to_currency: str,
        as_of_date: _date,
    ) -> Decimal:
        """Convert ``amount`` between two currencies at ``as_of_date``.

        Triangulates through the reference currency:
        ``amount × rate(from) / rate(to)``. The identity case is checked
        **first** and returns the amount untouched — no frame, no lookup, no
        rounding (ADR-0099 §3).

        Args:
            amount: The monetary amount, in ``from_currency``.
            from_currency: The amount's current currency.
            to_currency: The currency to express it in — the tenant's
                functional currency, at the ADR-0099 §4 boundary.
            as_of_date: The date whose rates apply. Conversion is
                point-in-time: a NAV converts at its own date's rate, a
                cashflow at its flow date's, so the resulting IRR / TVPI /
                DPI include the FX effect.

        Returns:
            The converted amount as an exact :class:`~decimal.Decimal`. The
            division carries the default :mod:`decimal` context precision
            (28 significant digits); callers quantise for presentation.

        Raises:
            MissingFxRateError: If either leg's rate cannot be resolved.
                The error names the failing currency, the date, and which
                leg failed.
        """
        if from_currency == to_currency:
            return amount
        rate_from = self._rate(from_currency, as_of_date, leg="from")
        rate_to = self._rate(to_currency, as_of_date, leg="to")
        return amount * rate_from / rate_to

    def convert_series(
        self,
        series: pd.Series,
        from_currency: str,
        to_currency: str,
    ) -> pd.Series:
        """Convert a date-indexed series point-in-time, vectorised.

        Each observation converts at the carry-forward rate of **its own**
        index date, not at a single period-end rate. Converting a whole
        history at the latest rate would silently remove the FX effect from
        it, which is the one thing a functional-currency IRR must retain.

        The identity case returns a copy of the input: same values, same
        index, same dtype, no rate consulted. An empty series likewise
        returns a copy — there is nothing to price, so an uncovered currency
        is not an error.

        Args:
            series: Float-valued series indexed by date or timestamp.
                Timezone-aware indices are compared in UTC; the returned
                series keeps the caller's original index untouched.
            from_currency: The series' current currency.
            to_currency: The currency to express it in.

        Returns:
            A new series of the converted values, preserving ``series``'
            index, name, and ``float64`` dtype.

        Raises:
            MissingFxRateError: If either leg has no rate at or before any
                index date. The error names the earliest offending date.
        """
        if from_currency == to_currency or series.empty:
            return series.copy()

        index = _normalise_index(series.index)
        rates_from = self._rate_vector(from_currency, index, leg="from")
        rates_to = self._rate_vector(to_currency, index, leg="to")
        values = series.to_numpy(dtype=float) * rates_from / rates_to
        return pd.Series(values, index=series.index, name=series.name)

    def shocked(self, currency: str, factor: Decimal, *, after: _date) -> FxConverter:
        """Return a converter whose rate path for ``currency`` is restated.

        The rate arithmetic behind an ``fx_shock`` (ADR-0104 §2/§3), living
        with the rates rather than in the overlay package: the overlay is
        DB-free *and* rate-free, and a scenario parameter has no business
        knowing how a triangulated rate is stored.

        **What "restate" means.** One unit of ``currency`` becomes worth
        ``factor`` times what it was, measured in every other currency, from
        ``after`` onward. Since the dataset quotes ``rate_to_reference`` as
        *the price of one unit of the currency in the reference currency*
        (ADR-0099 §2, normative), that is simply ``rate × factor`` — and a
        ``factor`` below 1 is a depreciation: with EUR as reference, scaling
        ``USD → 0.92`` by ``0.9`` makes one USD worth 0.828 EUR, so every USD
        position translates 10 % lower. Triangulation is unharmed: the shocked
        currency appears in exactly one leg of ``rate(from) / rate(to)`` for
        any conversion that involves it, and in neither leg for one that does
        not, so a USD shock moves the USD/EUR cross and leaves GBP/EUR alone.

        **Strictly after ``after``, and inclusive-at is not an option.** The
        caller passes the plan/actual seam t₀, and a rate at or before it is a
        rate that *prevailed*. Restating it would rewrite the functional value
        of realised statements — the identical-history invariant (ADR-0104 §5)
        forbids exactly that, and
        :func:`services.overlay.steps.scale_after` refuses it on value paths
        for the same reason. So the path keeps its realised segment bit-identical
        and the **held-flat plan segment** (ADR-0104 §3, N1) is what moves: an
        anchor point one day past ``after`` carries the shocked level of the
        last realised rate forward, which is what makes the shock visible at
        all — the plan horizon's carry-forward would otherwise resolve back to
        an unshocked realised rate and the scenario would silently equal its
        baseline.

        **Shocking the reference currency.** ``rate(reference) = 1`` is an
        application-level short-circuit and never a stored row
        (``ck_fx_rates_currency_not_reference``), so the reference's path
        cannot be scaled in place. Scaling *every other* currency by
        ``1 / factor`` is the same statement and is exact here: it leaves every
        cross between two non-reference currencies untouched and moves only the
        crosses against the reference. Without this branch a shock on the
        reference currency would vanish silently, which is the one failure mode
        a scenario surface must not have.

        Args:
            currency: The currency whose plan-world rate path is restated.
            factor: The multiplier on the currency's value —
                ``1 + magnitude / 100``. ``1`` is the identity.
            after: The plan/actual seam. Rates in force at or before it are
                returned bit-identical; the path in force strictly after it is
                scaled.

        Returns:
            A **new** converter. ``self`` is never mutated — the seam reuses the
            unshocked converter for the baseline leg of the Baseline/Scenario
            pair (ADR-0104 §4). ``self`` itself where the shock is neutral, or
            where ``currency`` has no rate path to restate: a currency the
            dataset never priced cannot be shocked, and the conversion that
            needs it raises :class:`~core.exceptions.MissingFxRateError` at its
            own date rather than being papered over with a fabricated rate.
        """
        if factor == _NEUTRAL_FACTOR:
            return self

        if currency == self._reference_currency:
            paths = {
                code: _restate_after(rates, after, lambda r: r / factor)
                for code, rates in self._by_currency.items()
            }
            return self._from_rate_paths(paths, self._reference_currency)

        known = self._by_currency.get(currency)
        if known is None:
            return self

        paths = dict(self._by_currency)
        paths[currency] = _restate_after(known, after, lambda r: r * factor)
        return self._from_rate_paths(paths, self._reference_currency)

    # -- internals ----------------------------------------------------------

    @classmethod
    def _from_rate_paths(
        cls, paths: dict[str, _CurrencyRates], reference_currency: str
    ) -> FxConverter:
        """Build a converter directly from prepared rate paths.

        The frame is the converter's *ingest* form, not its working form, and a
        restatement already holds the working form. Round-tripping it back
        through a frame would re-parse every Decimal it just computed.
        """
        converter = cls(_EMPTY_FRAME, reference_currency)
        converter._by_currency = paths
        return converter

    def _rate(self, currency: str, as_of_date: _date, *, leg: str | None) -> Decimal:
        """Scalar carry-forward lookup. ``leg`` labels a failing conversion."""
        if currency == self._reference_currency:
            return _IDENTITY_RATE
        known = self._by_currency.get(currency)
        if known is None:
            raise MissingFxRateError(currency, as_of_date, leg)
        # bisect_right - 1 is the latest entry at or before as_of_date.
        position = bisect_right(known.dates, as_of_date) - 1
        if position < 0:
            raise MissingFxRateError(currency, as_of_date, leg)
        return known.rates[position]

    def _rate_vector(self, currency: str, index: pd.DatetimeIndex, *, leg: str) -> np.ndarray:
        """Vectorised carry-forward lookup over a whole index."""
        if currency == self._reference_currency:
            return np.ones(len(index), dtype=float)
        known = self._by_currency.get(currency)
        if known is None:
            raise MissingFxRateError(currency, index.min().date(), leg)
        # searchsorted(..., side="right") counts entries <= t; minus one is
        # the latest at or before t. A -1 means t precedes the first rate.
        positions = np.searchsorted(known.dates64, index.to_numpy(), side="right") - 1
        uncovered = positions < 0
        if uncovered.any():
            raise MissingFxRateError(currency, index[uncovered].min().date(), leg)
        return known.rates64[positions]
