# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The normalised market-data DTO — the single format every adapter docks onto.

This module defines the **result contract** of the market-data layer
(ADR-0091 §"The result contract"). Its stability — not the port signature —
is what lets an unknown future provider (Bloomberg, Preqin, PitchBook) be
added as a pure adapter with zero downstream change. Everything here is
defined around the **canonical domain** (the target tables
``investment_navs`` / ``investment_cashflows`` and the historised
composition-weight tables of ADR-0079 / ADR-0080), never around any one
provider.

The DTO contract has four load-bearing properties (ADR-0091), all enforced
at construction so the adapter is the boundary that guarantees them and
downstream code can trust them:

1. **Provider-blindness.** No field carries a provider-specific name, code,
   or unit. ``provider`` is pure provenance — a reviewer reading a DTO
   instance must not be able to tell *which* provider produced it beyond
   that one string.
2. **Explicit non-availability, never a silent gap.** A metric a provider
   does not supply at all is declared absent in the capability matrix
   (``config/market_data_capabilities.yaml``), never represented as a
   ``None`` inside a series. A DTO cannot be constructed with ``None``
   points; an empty ``points`` tuple means "no data in this window" (a real
   gap), which is distinct from "this kind is unsupported".
3. **Units, scale, and calendar normalised at the adapter edge.** Values are
   :class:`~decimal.Decimal` in a stated currency, and ``as_of_date`` is a
   statement day matching the ``investment_navs`` DATE semantics. The DTO
   carries only already-normalised values.
4. **Identity and provenance carried through.** Each DTO states the
   :class:`NormalizedIdentifier` it was fetched against and the ``provider``
   name, so the ingest write path (ADR-0092) can set ``source`` /
   ``ingest_origin`` without re-interrogating the adapter.

The dataclasses mirror the codebase's frozen-dataclass idiom (e.g.
``services/web_research/fetcher.py``); validation mirrors the strict
construction boundary of the ``InvestmentExtractor`` (ADR-0043 §3). Nothing
in this module touches the database, an LLM, or any provider SDK.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

# The closed set of identifier schemes, mirroring ADR-0090 §Decision (extended
# by ADR-0096) and ``core.models.investment_identifier.IDENTIFIER_SCHEMES``. It
# is duplicated here deliberately: the market-data layer must not import
# ``core.models`` (which pulls in SQLAlchemy), enforced by
# ``tests/regression/test_market_data_layer_pure.py``. The two sets are kept in
# sync by ADR + migration, never by an import edge — and pinned together by
# ``tests/regression/test_identifier_scheme_set_consistency.py``. ``preqin`` /
# ``pitchbook`` are the provider-native private-markets schemes of ADR-0096;
# they are deliberately NOT FIGI-resolvable (see ``_SCHEME_TO_ID_TYPE`` in
# ``services/market_data/normalisation.py``).
IDENTIFIER_SCHEMES: frozenset[str] = frozenset(
    {"isin", "ticker", "figi", "cusip", "internal", "preqin", "pitchbook"}
)


class SeriesKind(StrEnum):
    """The closed set of series kinds a fetch can return (ADR-0091 property 2).

    Each member's value is exactly the canonical string literal the target
    table uses, so the ingest write path (ADR-0092) can consume it without
    translation. Being a :class:`~enum.StrEnum` — the codebase convention for
    closed value sets (cf. :class:`services.tool_classes.ToolClass`) — a
    member *is* its string, so ``SeriesKind.DIVIDEND == "dividend"`` holds and
    the values line up with the ``flow_type`` / ``nav_kind`` CHECK sets.

    Members:
        NAV_PRICE: A statement-day valuation / price point, landing in
            ``investment_navs.nav_value``.
        DIVIDEND, COUPON, DISTRIBUTION, CAPITAL_CALL, FEE, CARRY, OTHER: The
            seven canonical cashflow kinds, exactly the
            ``investment_cashflows.flow_type`` CHECK set.
        WEIGHT_SECTOR, WEIGHT_REGION, WEIGHT_COUNTRY, WEIGHT_RATING,
            WEIGHT_MATURITY: The five historised composition-weight families
            (ADR-0079 / ADR-0080), each a time-series keyed on
            ``(investment_id, as_of_date, <bucket>)``.
    """

    NAV_PRICE = "nav_price"

    # The seven canonical cashflow kinds — value-identical to the
    # ``investment_cashflows.flow_type`` CHECK set.
    DIVIDEND = "dividend"
    COUPON = "coupon"
    DISTRIBUTION = "distribution"
    CAPITAL_CALL = "capital_call"
    FEE = "fee"
    CARRY = "carry"
    OTHER = "other"

    # The five historised composition-weight families (ADR-0079 / ADR-0080).
    WEIGHT_SECTOR = "weight_sector"
    WEIGHT_REGION = "weight_region"
    WEIGHT_COUNTRY = "weight_country"
    WEIGHT_RATING = "weight_rating"
    WEIGHT_MATURITY = "weight_maturity"


@dataclass(frozen=True)
class NormalizedIdentifier:
    """A security identifier normalised the way the repository stores it.

    ``value`` is trimmed and upper-cased at construction — the same transform
    ``core.repositories.investment_identifier_repository`` applies on write —
    so a DTO identity matches stored rows. ``scheme`` must be one of the
    closed :data:`IDENTIFIER_SCHEMES`.

    Attributes:
        scheme: One of ``isin``, ``ticker``, ``figi``, ``cusip``,
            ``internal`` (ADR-0090), or the provider-native private-markets
            schemes ``preqin`` / ``pitchbook`` (ADR-0096).
        value: The identifier value, trimmed and upper-cased.
    """

    scheme: str
    value: str

    def __post_init__(self) -> None:
        """Validate the scheme and normalise the value (trim + upper-case).

        Raises:
            ValueError: If ``scheme`` is not in :data:`IDENTIFIER_SCHEMES` or
                ``value`` is empty after trimming.
        """
        if self.scheme not in IDENTIFIER_SCHEMES:
            raise ValueError(
                f"Unknown identifier scheme {self.scheme!r}; "
                f"expected one of {sorted(IDENTIFIER_SCHEMES)}."
            )
        normalised = self.value.strip().upper()
        if not normalised:
            raise ValueError("Identifier value must be non-empty after trimming.")
        object.__setattr__(self, "value", normalised)


@dataclass(frozen=True)
class DateWindow:
    """A closed ``[start, end]`` date window, validated ``start <= end``.

    Attributes:
        start: The inclusive first day.
        end: The inclusive last day.
    """

    start: date
    end: date

    def __post_init__(self) -> None:
        """Validate the window bounds.

        Raises:
            ValueError: If either bound is not a plain :class:`~datetime.date`
                or ``start`` is after ``end``.
        """
        for label, value in (("start", self.start), ("end", self.end)):
            if not _is_plain_date(value):
                raise ValueError(
                    f"DateWindow.{label} must be a datetime.date "
                    f"(not a datetime); got {type(value).__name__}."
                )
        if self.start > self.end:
            raise ValueError(f"DateWindow start {self.start} is after end {self.end}.")

    def contains(self, day: date) -> bool:
        """Return whether ``day`` falls within ``[start, end]`` inclusive."""
        return self.start <= day <= self.end


@dataclass(frozen=True)
class SeriesPoint:
    """One ``(as_of_date, value)`` point already in canonical form.

    Attributes:
        as_of_date: The statement day (a plain :class:`~datetime.date`,
            matching the ``investment_navs`` DATE semantics).
        value: The value as a :class:`~decimal.Decimal` — never a float, per
            the codebase's money convention (``Numeric`` columns).
    """

    as_of_date: date
    value: Decimal

    def __post_init__(self) -> None:
        """Enforce the point invariants.

        Raises:
            ValueError: If ``as_of_date`` is not a plain date, or ``value`` is
                not a :class:`~decimal.Decimal`.
        """
        if not _is_plain_date(self.as_of_date):
            raise ValueError(
                "SeriesPoint.as_of_date must be a datetime.date (not a "
                f"datetime); got {type(self.as_of_date).__name__}."
            )
        # bool is a subclass of int but never a Decimal; the isinstance check
        # rejects float, int, str, and None alike.
        if not isinstance(self.value, Decimal):
            raise ValueError(
                "SeriesPoint.value must be a decimal.Decimal (never a float); "
                f"got {type(self.value).__name__}."
            )


@dataclass(frozen=True)
class NormalizedSeries:
    """An ordered, provider-blind series of canonical points.

    Attributes:
        ident: The :class:`NormalizedIdentifier` the series was fetched
            against.
        provider: The provider name — pure provenance (ADR-0091 property 4).
        kind: The :class:`SeriesKind`; coerced from its string value if a
            plain string is passed.
        currency: The ISO-4217-style currency code (trimmed + upper-cased,
            non-empty).
        points: The points, ordered strictly ascending by ``as_of_date`` with
            no duplicate dates. May be empty — an empty series is a real "no
            data in window" gap, distinct from an unsupported ``kind``.
    """

    ident: NormalizedIdentifier
    provider: str
    kind: SeriesKind
    currency: str
    points: tuple[SeriesPoint, ...] = field(default=())

    def __post_init__(self) -> None:
        """Normalise the envelope and enforce the ordering invariant.

        Raises:
            ValueError: If ``provider`` or ``currency`` is empty, ``kind`` is
                not a known :class:`SeriesKind`, any point is not a
                :class:`SeriesPoint`, or the points are not strictly ascending
                by date (which also forbids duplicate dates).
        """
        if not self.provider or not self.provider.strip():
            raise ValueError("NormalizedSeries.provider must be non-empty.")

        currency = self.currency.strip().upper()
        if not currency:
            raise ValueError("NormalizedSeries.currency must be non-empty.")
        object.__setattr__(self, "currency", currency)

        # Coerce a plain string to the enum; SeriesKind("bogus") raises
        # ValueError, enforcing the closed set.
        object.__setattr__(self, "kind", SeriesKind(self.kind))

        points = tuple(self.points)
        _validate_ascending_points(points)
        object.__setattr__(self, "points", points)


@dataclass(frozen=True)
class NormalizedQuote:
    """The single-point degenerate case of :class:`NormalizedSeries`.

    Attributes:
        ident: The identifier the quote was fetched against.
        provider: The provider name (provenance).
        kind: The :class:`SeriesKind`; coerced from a string if needed.
        currency: The currency code (trimmed + upper-cased, non-empty).
        as_of_date: The statement day of the quote.
        value: The quoted value as a :class:`~decimal.Decimal`.
    """

    ident: NormalizedIdentifier
    provider: str
    kind: SeriesKind
    currency: str
    as_of_date: date
    value: Decimal

    def __post_init__(self) -> None:
        """Validate the quote by reusing the point and series invariants.

        Raises:
            ValueError: On the same conditions as :class:`SeriesPoint` and the
                :class:`NormalizedSeries` envelope.
        """
        if not self.provider or not self.provider.strip():
            raise ValueError("NormalizedQuote.provider must be non-empty.")
        currency = self.currency.strip().upper()
        if not currency:
            raise ValueError("NormalizedQuote.currency must be non-empty.")
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "kind", SeriesKind(self.kind))
        # Reuse SeriesPoint's construction validation for date/value.
        SeriesPoint(as_of_date=self.as_of_date, value=self.value)

    @property
    def point(self) -> SeriesPoint:
        """Return this quote as a single :class:`SeriesPoint`."""
        return SeriesPoint(as_of_date=self.as_of_date, value=self.value)

    def to_series(self) -> NormalizedSeries:
        """Return the equivalent one-point :class:`NormalizedSeries`."""
        return NormalizedSeries(
            ident=self.ident,
            provider=self.provider,
            kind=self.kind,
            currency=self.currency,
            points=(self.point,),
        )


def _is_plain_date(value: object) -> bool:
    """Return whether ``value`` is a :class:`date` but not a :class:`datetime`.

    ``datetime`` subclasses ``date``, so a bare ``isinstance(x, date)`` would
    admit a datetime. The DTO carries statement-day *dates*; a datetime slipping
    in would smuggle a time-of-day the DATE-semantics tables cannot hold.
    """
    return isinstance(value, date) and not isinstance(value, datetime)


def _validate_ascending_points(points: tuple[SeriesPoint, ...]) -> None:
    """Assert ``points`` are all :class:`SeriesPoint` and strictly ascending.

    Strict ascension by ``as_of_date`` simultaneously guarantees ordering and
    the absence of duplicate dates.

    Raises:
        ValueError: If any element is not a :class:`SeriesPoint`, or a date is
            not strictly greater than its predecessor.
    """
    previous: date | None = None
    for point in points:
        if not isinstance(point, SeriesPoint):
            raise ValueError(
                "NormalizedSeries.points must contain SeriesPoint instances; "
                f"got {type(point).__name__}."
            )
        if previous is not None and point.as_of_date <= previous:
            raise ValueError(
                "NormalizedSeries.points must be strictly ascending by date "
                f"with no duplicates; {point.as_of_date} follows {previous}."
            )
        previous = point.as_of_date
