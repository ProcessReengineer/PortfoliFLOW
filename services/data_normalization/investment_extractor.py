# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InvestmentExtractor — Excel-import JSONB snapshot → normalised investment data.

Per ADR-0043 §3, the Phase-4 Excel-import path is **asynchronous**:
the Phase-2 upload pathway persists a JSONB snapshot per workbook
(``data_uploads`` / ``data_upload_sheets``); a separate transformation
step reads that snapshot and writes normalised rows into
``investments`` / ``investment_navs`` / ``investment_cashflows``. The
extractor in this module is the pure-transformation half of that
step. It has no FastAPI, session, or audit-log dependency and is
therefore unit-testable in isolation.

Design conventions (ADR-0043 §3 + the import-format specification per ADR-0009)

* The extractor receives the JSONB shape produced by
  :meth:`pandas.DataFrame.to_json(orient="split")` per sheet, keyed by
  canonical snake_case sheet name (``"attributes"``, ``"navs_actual"``,
  …). The shape is ``{"columns": [...], "index": [...], "data":
  [[...], ...]}``. Date-indexed sheets carry ISO-8601 strings in
  ``index`` and floats / ``None`` in ``data``.
* The eight canonical investment-type values
  (``private_equity``, ``private_debt``, ``real_estate``,
  ``infra_equity``, ``listed_equity``, ``listed_bonds``, ``other``,
  and ``cash`` — the last added by ADR-0100 §1) are enforced strictly.
  A non-resolvable Excel value raises a row-level
  :class:`ImportRowError` (collected, not thrown) for the affected
  investment so the rest of the import still succeeds.
* Cashflow signs are validated, **not** silently coerced. A positive
  amount in a ``Cash Flow Out`` sheet is an :class:`ImportRowError`
  per the ADR-0043 §3 strict-validation convention, mirrored from the
  cashflow-provider's defensive guard
  (``services/reporting/data_providers/cashflow_provider.py``) but
  reversed: the importer is the boundary that enforces the
  convention; downstream code can then trust signs.
* ``flow_timestamp`` defaults to 12:00 UTC on the Excel date — Excel
  cells carry no time-of-day, and the convention from ADR-0043 §1 is
  to centre the day rather than land on midnight in the local
  timezone of the importer.
* One ``ImportedCashflow`` per ``(investment, date, sheet)``: Excel
  pre-aggregates same-day flows; the extractor preserves that
  granularity rather than fabricating a tranche structure.

Sheet inventory (canonical snake_case keys, as ``load_excel`` emits them)

* ``attributes`` — investment stammdaten; required.
* ``navs_actual`` / ``navs_plan`` — position values, one column per
  investment.
* ``cash_flow_in_actual`` / ``_plan``, ``cash_flow_out_actual`` /
  ``_plan`` — calls and distributions; on a **cash** column, investor
  flows instead (ADR-0103 §5).
* ``cash_flow_income_actual`` / ``_plan`` — dividends / coupons for the
  liquid archetypes (ADR-0081).
* ``cash`` — the **Cash statement sheet** (ADR-0103 §3, workbook v32):
  one column per cash position, one row per statement date, each cell a
  **balance level** in the position currency. It is the book of record
  for cash balances and takes precedence over any NAV column for the
  same investment; the service derives the position ledger from it
  (ADR-0103 §4). Optional — a v31 workbook has no such sheet and its
  cash positions stay NAV-column-fed.
* ``benchmarks_actual`` / ``benchmark_mapping`` (ADR-0061),
  ``fx_rates`` (ADR-0099 §5), ``bond_analytics`` / ``rating_weights`` /
  ``maturity_weights`` (ADR-0079/0081) — reference data, each with its
  own extraction entry point.

Trust delimiters and security: the extractor does not call any
external service. It only consumes JSONB content already inside the
trust boundary (the operator's own upload). No ADR-0023 trust
delimiter wrapping is required.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date as _date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

import pandas as pd

import re

from core.exceptions import AnlVCodeUnknown, DataImportError, ValidationError
from services.reporting.attributes_partition import (
    AttributesPartition,
    partition_attributes,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public exception types
# ---------------------------------------------------------------------------


class ImportFormatError(DataImportError):
    """Raised when the Excel JSONB snapshot is so broken that even
    high-level extraction cannot proceed.

    Examples: missing ``Attributes`` sheet entirely, an Attributes
    sheet with no investment columns, or a structurally invalid JSONB
    payload (e.g. ``"index"`` key missing from a sheet dict).

    Row-level errors are **not** raised as :class:`ImportFormatError`.
    They are collected as :class:`ImportRowError` records on the
    extractor and returned alongside the successfully extracted
    investments.
    """


class UploadNotFoundError(DataImportError):
    """Raised by the InvestmentService transform path when the supplied
    ``upload_id`` does not exist in the active tenant.

    Lives in this module (rather than on the service) so the extractor
    and the service share the same import-time exception namespace.
    """


# ---------------------------------------------------------------------------
# Sheet-key constants (canonical snake_case keys produced by load_excel)
# ---------------------------------------------------------------------------


_ATTRIBUTES_KEY: str = "attributes"
_NAVS_ACTUAL_KEY: str = "navs_actual"
_NAVS_PLAN_KEY: str = "navs_plan"
_CF_IN_ACTUAL_KEY: str = "cash_flow_in_actual"
_CF_IN_PLAN_KEY: str = "cash_flow_in_plan"
_CF_OUT_ACTUAL_KEY: str = "cash_flow_out_actual"
_CF_OUT_PLAN_KEY: str = "cash_flow_out_plan"
_BENCHMARKS_ACTUAL_KEY: str = "benchmarks_actual"
_BENCHMARK_MAPPING_KEY: str = "benchmark_mapping"
_FX_RATES_KEY: str = "fx_rates"

#: The ``Cash`` statement sheet (ADR-0103 §3, workbook v32). Wide,
#: investment-keyed like the NAV sheets, but its cells are *levels* — the
#: custodian statement balance of a cash position on a statement date, in
#: the position's own currency. The service derives the ledger from it
#: (ADR-0103 §4); the extractor's job ends at a validated, chronologically
#: ordered statement series per cash column.
_CASH_KEY: str = "cash"

# (sheet_key, flow_type, flow_kind, expected_sign)
# expected_sign:  1 → amount must be > 0 (Cash Flow In)
#                -1 → amount must be < 0 (Cash Flow Out)
_CASHFLOW_SHEETS: tuple[tuple[str, str, str, int], ...] = (
    (_CF_IN_ACTUAL_KEY, "distribution", "actual", 1),
    (_CF_IN_PLAN_KEY, "distribution", "plan", 1),
    (_CF_OUT_ACTUAL_KEY, "capital_call", "actual", -1),
    (_CF_OUT_PLAN_KEY, "capital_call", "plan", -1),
)

_NAV_SHEETS: tuple[tuple[str, str], ...] = (
    (_NAVS_ACTUAL_KEY, "actual"),
    (_NAVS_PLAN_KEY, "plan"),
)

# Resolved-type → an override of the per-sheet fixed ``flow_type`` of
# ``_CASHFLOW_SHEETS`` (ADR-0103 §5). A **cash** column on the four Cash
# Flow In/Out sheets does not carry capital calls and distributions — a
# cash position makes none. It carries **investor flows**: net
# contributions to (In) and withdrawals from (Out) the mandate, which
# settle in the cash position of their currency (decision N4).
#
# The workbook needs no new sheet for this: the existing flow-sheet
# convention already encodes direction (In / Out) and kind
# (actual / plan), which is exactly what an investor flow needs. The
# override is therefore the whole format story — the sign guards, the
# zero-cell drop and the 12:00-UTC stamp below apply unchanged, so an In
# cell is a positive contribution and an Out cell a negative withdrawal.
#
# Any type not listed here keeps the sheet's fixed flow_type verbatim,
# which is the regression obligation of this mapping: it is a lookup miss
# for every non-cash investment.
_CASHFLOW_TYPE_OVERRIDE_BY_TYPE: dict[str, str] = {
    "cash": "investor_flow",
}


# ---------------------------------------------------------------------------
# Liquid-archetype sheet keys + taxonomies (ADR-0081 / ADR-0079)
# ---------------------------------------------------------------------------


_BOND_ANALYTICS_KEY: str = "bond_analytics"
_RATING_WEIGHTS_KEY: str = "rating_weights"
_MATURITY_WEIGHTS_KEY: str = "maturity_weights"
_CF_INCOME_ACTUAL_KEY: str = "cash_flow_income_actual"
_CF_INCOME_PLAN_KEY: str = "cash_flow_income_plan"

# (sheet_key, flow_kind). The two listed-instrument income sheets ride
# the existing wide cash-flow idiom; ``flow_type`` is *not* fixed per
# sheet (unlike ``_CASHFLOW_SHEETS``) — it is derived per investment
# from the resolved investment type (ADR-0081 §1).
_INCOME_SHEETS: tuple[tuple[str, str], ...] = (
    (_CF_INCOME_ACTUAL_KEY, "actual"),
    (_CF_INCOME_PLAN_KEY, "plan"),
)

# Resolved-type → income flow_type. ``listed_equity`` pays a dividend,
# ``listed_bonds`` (including a money-market fund, ADR-0081 §3) pays a
# coupon. Any other investment type produces no income (the mapping miss
# is a silent skip, not an error — ADR-0081 §Task C).
#
# ``cash`` is deliberately absent (ADR-0100 §4: cash positions are a
# NAV-only series — interest accrues into the balance, so any Income-sheet
# cell for a cash column is silently skipped by the mapping miss). That
# silent-skip behaviour *is* the defined v1 semantics for cash income
# columns; the omission is intentional, not an oversight.
_INCOME_FLOW_TYPE_BY_TYPE: dict[str, str] = {
    "listed_equity": "dividend",
    "listed_bonds": "coupon",
}

# Bucket taxonomies — mirror the merged CHECK constraints exactly
# (ADR-0079 §2; migration b016).
_RATING_BUCKETS: frozenset[str] = frozenset(
    {"AAA", "AA", "A", "BBB", "BB", "B", "CCC_and_below", "NR"}
)
_MATURITY_BUCKETS: frozenset[str] = frozenset({"0-1y", "1-3y", "3-5y", "5-7y", "7-10y", "10y+"})

# Tidy/long reference-sheet column schemas (ADR-0081 §1). The dedicated
# loader parsers always emit exactly these columns, so a missing column
# is a structural fault (ImportFormatError), not a row-level concern.
_BOND_ANALYTICS_COLUMNS: tuple[str, ...] = (
    "as_of_date",
    "investment",
    "ytm",
    "eff_duration",
    "oas",
    "convexity",
)
_RATING_WEIGHTS_COLUMNS: tuple[str, ...] = (
    "as_of_date",
    "investment",
    "rating_bucket",
    "weight_pct",
)
_MATURITY_WEIGHTS_COLUMNS: tuple[str, ...] = (
    "as_of_date",
    "investment",
    "maturity_bucket",
    "weight_pct",
)

_WEIGHT_PCT_MIN: Decimal = Decimal("0")
_WEIGHT_PCT_MAX: Decimal = Decimal("100")


# ---------------------------------------------------------------------------
# Investment-type alias mapping (Excel free-text → canonical eight values)
#
# Keys are normalised on the fly: lower-cased, whitespace + hyphens
# collapsed to a single underscore. The eight canonical values
# themselves are listed so that already-canonical Excel cells survive
# normalisation as a no-op. German labels match the sample Excel import workbook
# (``"Aktien"``, ``"Anleihen"``, ``"Immobilien"``, ``"Infrastruktur"``).
# Update this map *only* when a real Excel input surfaces a previously
# unseen label — it is a normalisation table, not an alias festival.
# ---------------------------------------------------------------------------


_VALID_INVESTMENT_TYPES: frozenset[str] = frozenset(
    {
        "private_equity",
        "private_debt",
        "real_estate",
        "infra_equity",
        "listed_equity",
        "listed_bonds",
        "other",
        # ADR-0100 §1: the eighth type. A foreign-currency cash balance is
        # a first-class investment row (NAV-only, converted at the ADR-0099
        # seam) rather than the functional-currency residual.
        "cash",
    }
)


_INVESTMENT_TYPE_ALIASES: dict[str, str] = {
    # Canonical (no-op self-map after normalisation).
    "private_equity": "private_equity",
    "private_debt": "private_debt",
    "real_estate": "real_estate",
    "infra_equity": "infra_equity",
    "listed_equity": "listed_equity",
    "listed_bonds": "listed_bonds",
    "other": "other",
    # English variants.
    "pe": "private_equity",
    "pd": "private_debt",
    "infrastructure": "infra_equity",
    "infrastructure_equity": "infra_equity",
    "real_estate_equity": "real_estate",
    "listed_equities": "listed_equity",
    "equity": "listed_equity",
    "equities": "listed_equity",
    "bond": "listed_bonds",
    "bonds": "listed_bonds",
    "fixed_income": "listed_bonds",
    # Liquid-archetype Fixed-Income fixtures (ADR-0081 §3). ``Credit``
    # (the sample workbook's credit funds) resolves to ``listed_bonds``.
    "credit": "listed_bonds",
    # ADR-0100 §5 supersedes the ADR-0081 §3 modelling of the sample
    # workbook's money-market fund. That earlier decision pointed the
    # ``Cash`` label at ``listed_bonds`` "rather than introducing a fourth
    # archetype now" — ADR-0100 is that "now". §5 rules the underlying
    # instrument: a money-market fund is a *fund*, not a cash row, so the
    # MMF (sample Investment T) keeps ``listed_bonds`` via its relabelled
    # ``Money Market`` cell, while the ``Cash`` label is re-pointed to the
    # new ``cash`` type (a currency balance, NAV-only). ``money_market`` /
    # ``geldmarkt`` therefore alias to ``listed_bonds``; ``kasse`` and
    # ``liquidität`` are German operator labels for a cash balance.
    "cash": "cash",
    "kasse": "cash",
    "liquiditaet": "cash",
    "liquidität": "cash",
    "money_market": "listed_bonds",
    "geldmarkt": "listed_bonds",
    "andere": "other",
    "sonstige": "other",
    "sonstiges": "other",
    # German labels (ADR-0009 sample Excel import workbook).
    "aktien": "listed_equity",
    "anleihen": "listed_bonds",
    "immobilien": "real_estate",
    "infrastruktur": "infra_equity",
    "privater_kredit": "private_debt",
    "privates_eigenkapital": "private_equity",
}


# ---------------------------------------------------------------------------
# Attribute-row aliases (Excel keys → ImportedInvestment fields)
# ---------------------------------------------------------------------------


_REGION_ATTR_KEYS: frozenset[str] = frozenset({"region", "regionen"})
_MANAGER_ATTR_KEYS: frozenset[str] = frozenset(
    {"manager_/_fondsname", "manager", "fondsname", "manager_name"}
)
_VINTAGE_ATTR_KEYS: frozenset[str] = frozenset({"vintage_year", "vintage", "auflagejahr"})
_CURRENCY_ATTR_KEYS: frozenset[str] = frozenset({"währung", "waehrung", "currency"})
_ASSET_CLASS_ATTR_KEYS: frozenset[str] = frozenset({"asset_class", "anlageklasse"})
_ANLV_ATTR_KEYS: frozenset[str] = frozenset({"anlv"})
# Security-identifier attribute rows (ADR-0090). Looked up like the
# other attribute keys via :meth:`InvestmentExtractor._lookup_attr`.
# The v29 workbook labels the rows exactly ``ISIN`` and ``Ticker``,
# which :func:`_normalise_attribute_key` reduces to these keys.
_ISIN_ATTR_KEYS: frozenset[str] = frozenset({"isin"})
_TICKER_ATTR_KEYS: frozenset[str] = frozenset({"ticker"})
# Unitised-position attribute rows (ADR-0097 §7). Two optional trailing
# rows synthesise the single ``excel``-origin ``opening`` transaction. The
# v30 workbook labels them in English (``Units`` / ``Units As Of``,
# consistent with the sheet's other English attribute labels); the German
# aliases are accepted for the same bilingual robustness as
# ``currency``/``währung`` and ``asset_class``/``anlageklasse``.
_UNITS_ATTR_KEYS: frozenset[str] = frozenset({"units", "stück", "stücke", "stückzahl"})
_UNITS_AS_OF_ATTR_KEYS: frozenset[str] = frozenset(
    {"units_as_of", "stück_per", "stücke_per", "stückzahl_per"}
)


# Pattern for AnlV code normalisation: accepts ``"Nr. 13"``, ``"Nr.13"``,
# ``"13"``, ``"anlv_13"`` (case-insensitive). The captured group holds
# the digit run; the normaliser composes ``anlv_<digits>``.
_ANLV_CODE_PATTERN: re.Pattern[str] = re.compile(r"^(?:nr\.?\s*|anlv_)?(\d+)$", re.IGNORECASE)


def _normalise_anlv_code(value: Any) -> str | None:
    """Resolve an Excel ``AnlV`` cell to a canonical ``"anlv_<n>"`` code.

    Per ADR-0057 §Excel import the importer accepts four lenient forms
    (``"Nr. 13"``, ``"Nr.13"``, ``"13"``, ``"anlv_13"``) and produces
    the snake_case identifier the FK points at. The result is **not**
    validated against the catalogue here — the extractor's row-handling
    code performs that lookup so the row-level
    :class:`AnlVCodeUnknown` carries the offending value and row
    context.

    Args:
        value: The raw cell value from the ``Attributes`` sheet.

    Returns:
        The canonical code (``"anlv_13"``), or ``None`` for an empty
        cell (the operator's "no classification" signal — ADR-0057).

    Raises:
        ValueError: When the cell carries a non-empty value that does
            not match any of the accepted forms. The caller converts
            the ``ValueError`` to :class:`AnlVCodeUnknown` so the
            error surfaces with row context.
    """
    if _is_blank(value):
        return None
    if isinstance(value, bool):
        raise ValueError(f"AnlV cell {value!r} is a boolean; expected text or integer.")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or not value.is_integer()):
            raise ValueError(
                f"AnlV cell {value!r} is a fractional or NaN numeric; "
                "expected integer category number."
            )
        candidate = str(int(value))
    elif isinstance(value, str):
        candidate = value.strip().replace(" ", " ")
    else:
        raise ValueError(f"AnlV cell {value!r} has unsupported type {type(value).__name__}.")
    match = _ANLV_CODE_PATTERN.match(candidate.strip().replace(" ", ""))
    if match is None:
        raise ValueError(
            f"AnlV cell {value!r} does not match any accepted form "
            "('Nr. 13', 'Nr.13', '13', 'anlv_13')."
        )
    digits = match.group(1)
    # Strip leading zeros for canonical form (anlv_07 → anlv_7) but
    # preserve the single ``0`` if someone literally writes 'Nr. 0'.
    digits = str(int(digits))
    return f"anlv_{digits}"


_DEFAULT_CURRENCY: str = "EUR"
_UNCLASSIFIED_CODE: str = "unclassified"

# Region / sector split rows in the ``Attributes`` sheet carry
# fractional weights (e.g. ``0.6`` for 60%). The block-identification
# heuristic in :func:`services.reporting.attributes_partition
# .partition_attributes` enforces the ``[0, 1.05]`` range; the
# extractor multiplies by 100 to convert to the DB-side percentage
# unit before persistence.
_PERCENT_SCALE: Decimal = Decimal("100")


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportedNav:
    """One NAV row extracted from an Excel snapshot.

    Attributes:
        as_of_date: Statement-day date.
        nav_value: NAV value as :class:`Decimal` (Excel cells are
            float; we coerce defensively to keep currency-amount
            arithmetic exact downstream).
        currency: ISO 4217 currency code from the investment's
            ``Währung`` attribute.
        nav_kind: ``"plan"`` or ``"actual"``.
    """

    as_of_date: _date
    nav_value: Decimal
    currency: str
    nav_kind: str


@dataclass(frozen=True)
class ImportedCashflow:
    """One cashflow row extracted from an Excel snapshot.

    Attributes:
        flow_timestamp: TIMESTAMPTZ at 12:00 UTC on the Excel date
            (ADR-0043 §1 default).
        flow_type: One of the eight canonical values, derived from the
            sheet the cell came from and the investment's resolved type.
            The Cash Flow Out / In sheets yield ``capital_call`` /
            ``distribution`` for every type but ``cash``, whose columns
            yield ``investor_flow`` in both directions (ADR-0103 §5); the
            Income sheets yield ``dividend`` / ``coupon`` for the liquid
            archetypes (ADR-0081 §1). The remaining values (``fee``,
            ``carry``, ``other``) are reserved for direct CRUD entry today.
        flow_kind: ``"plan"`` or ``"actual"``.
        amount: Signed cashflow amount (Cash Flow Out negative, Cash Flow
            In positive — ADR-0043 §3 strict-validation convention; a cash
            column's investor flows obey the same sheet convention, so a
            withdrawal is negative and a contribution positive).
        currency: ISO 4217 currency code from the investment's
            ``Währung`` attribute.
    """

    flow_timestamp: datetime
    flow_type: str
    flow_kind: str
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class ImportedCashStatement:
    """One custodian statement balance for a cash position (ADR-0103 §3).

    A **level**, not a flow: the balance the statement reports on
    ``statement_date``, in the cash position's own currency. The
    statement-to-ledger derivation (ADR-0103 §4) happens at the service
    layer, which turns the first statement into the ``opening`` and every
    subsequent one into a signed ``transfer`` of the delta; the extractor
    owns only the parsed, validated, chronologically ordered series.

    Attributes:
        statement_date: The statement day (the sheet's row index).
        balance: The reported balance. Non-negative — an actual cash
            balance cannot be negative (ADR-0100 §5, unchanged by
            ADR-0103 §4); the importer rejects a negative cell rather
            than booking an overdraft. ``0`` is a legal balance (an
            emptied account), distinct from a blank cell (no statement).
    """

    statement_date: _date
    balance: Decimal


@dataclass(frozen=True)
class ImportedIdentifier:
    """One security identifier parsed from the Attributes sheet.

    Per ADR-0090 §"Identifiers enter through both import paths" the
    Excel import format gains optional ``ISIN`` / ``Ticker`` attribute
    rows. The extractor stays permissive: it coerces the cell to a
    trimmed string and imposes **no** checksum or format validation
    (the repository owns normalisation — upper-casing — and the DB owns
    non-emptiness). A blank cell produces no identifier at all, which is
    the correct state for an illiquid instrument.

    Attributes:
        scheme: The identifier scheme. In this slice always ``'isin'``
            or ``'ticker'`` (the two rows the Excel format defines);
            the closed CHECK set on ``investment_identifiers.scheme``
            also permits ``figi`` / ``cusip`` / ``internal``, which
            enter through other paths (ADR-0091, manual CRUD).
        value: The identifier value, trimmed but not upper-cased — the
            repository upper-cases on write.
    """

    scheme: str
    value: str


@dataclass(frozen=True)
class ImportedInvestment:
    """One investment as extracted from an Excel snapshot.

    Attributes:
        name: Investment name (the row-1 cell of the Excel import workbook).
            Tenant-unique by
            the ``uq_investments_tenant_name`` DB constraint; the
            extractor preserves the name verbatim and leaves the
            UNIQUE check to the service / DB layer.
        investment_type: One of the eight canonical CHECK-allowed
            values (the seven original plus ``cash``, ADR-0100 §1);
            resolution from Excel free-text via
            :func:`_normalise_investment_type`.
        asset_class_code: Tenant-scoped asset-class code. Empty
            ``Asset Class`` Excel cells map to ``"unclassified"``
            (ADR-0043 §1).
        manager_name: Optional fund-manager / GP label.
        region: Optional geographic region label.
        currency: ISO 4217 currency code (defaults to ``"EUR"`` if
            the ``Währung`` attribute row is absent — the sample Excel
            import workbooks do supply it).
        vintage_year: Optional integer vintage year. Excel often
            stores the year as a string (``"2020"``); this field
            holds the parsed integer.
        commitment_amount: Always ``None`` in Phase 4 — the Excel import
            format has no commitment column. CRUD edits add the value manually.
        anlv_code: Canonical AnlV code (``"anlv_13"``) resolved from
            the ``Attributes`` sheet's ``AnlV`` row. ``None`` represents
            the "AnlV unallocated" engine-fallback case (ADR-0057).
        navs: Plan and actual NAV rows for the investment.
        cashflows: Plan and actual cashflow rows for the investment.
        anlv_code: Canonical AnlV code (see above).
        identifiers: Security identifiers (ISIN / ticker) parsed from
            the Attributes sheet (ADR-0090). Empty tuple for illiquid
            instruments with no identifier rows — the correct state,
            never an error.
    """

    name: str
    investment_type: str
    asset_class_code: str
    manager_name: str | None
    region: str | None
    currency: str
    vintage_year: int | None
    commitment_amount: Decimal | None
    navs: tuple[ImportedNav, ...]
    cashflows: tuple[ImportedCashflow, ...]
    # Phase-7 AnlV classification (ADR-0057). Optional and trailing
    # so pre-Phase-7 extractor-only tests continue to construct
    # :class:`ImportedInvestment` without supplying it.
    anlv_code: str | None = None
    # Security identifiers (ADR-0090). Optional and trailing so every
    # pre-existing construction site stays valid; the default empty
    # tuple is the illiquid-instrument state.
    identifiers: tuple[ImportedIdentifier, ...] = ()
    # Unitised opening (ADR-0097 §7). ``units`` is the positive unit count
    # from the optional ``Units`` row; ``units_as_of`` is the date it
    # refers to — the explicit ``Units As Of`` cell, else the investment's
    # earliest actual NAV date (resolved here so the pair is always
    # self-consistent). Both ``None`` for the common no-units case, which
    # synthesises no opening transaction and leaves the investment
    # ``valuation_mode='reported'``.
    #
    # Mutually exclusive with ``cash_statements``: one book of record per
    # investment. A cash column carrying a statement series derives its
    # opening from that series, and a stray ``Units`` cell on it is
    # ignored with a warning.
    units: Decimal | None = None
    units_as_of: _date | None = None
    # Cash statement series (ADR-0103 §3), chronologically ordered and
    # non-empty only for a ``cash`` column of the ``Cash`` sheet. Empty for
    # every other investment — and for a cash position in a v31 workbook,
    # which has no ``Cash`` sheet and is still NAV-column-fed.
    cash_statements: tuple[ImportedCashStatement, ...] = ()


@dataclass(frozen=True)
class ImportedBenchmark:
    """One benchmark definition extracted from the Excel snapshot.

    Attributes:
        code: Stable identifier, must be unique within the tenant
            (e.g. ``"BM_EQUITIES_DM"``). Read from row 1 of the
            ``Benchmarks actual`` sheet (i.e. the column header).
        display_name: Defaults to ``code`` if row 2 of the
            corresponding sheet column is empty.
        description: From row 2 of the sheet — operator-facing label.
        provider_hint: From row 3 of the sheet — provenance note.
    """

    code: str
    display_name: str
    description: str | None
    provider_hint: str | None


@dataclass(frozen=True)
class ImportedBenchmarkObservation:
    """One ``(benchmark, date, return)`` triple extracted from the snapshot."""

    benchmark_code: str  # FK resolved at the service layer.
    as_of_date: _date
    period_return: Decimal


@dataclass(frozen=True)
class ImportedBenchmarkMapping:
    """One ``(asset_class, benchmark)`` mapping with weight.

    Attributes:
        asset_class_code: Tenant-scoped asset-class code. Resolution
            against the per-tenant catalogue happens at the service
            layer; the extractor does not see the DB.
        benchmark_code: Tenant-scoped benchmark code. Empty string
            is **allowed** when ``weight == 0`` — it represents a
            deliberate "no benchmark for this asset class" mapping
            row (e.g. Cash). The service layer interprets
            ``weight == 0`` + empty code as a non-mapping and skips
            DB persistence for the row.
        weight: Weight in ``[0, 1]``. ``0`` means "no benchmark".
    """

    asset_class_code: str
    benchmark_code: str
    weight: Decimal


@dataclass(frozen=True)
class ImportedFxRate:
    """One ``(currency, date, rate)`` FX observation extracted from a snapshot.

    Mirrors the ``fx_rates`` natural key of ADR-0099 §2. The quoting
    convention is normative: ``rate_to_reference`` is the price of one
    unit of ``currency`` expressed in ``reference_currency`` (an
    EUR-based deployment stores ``USD/EUR`` as ``currency='USD'``,
    ``reference_currency='EUR'``).

    Attributes:
        as_of_date: The rate date, already normalised by the standard
            market-reference parser (:func:`_parse_iso_date` decodes the
            ISO string the upload serialisation persists).
        currency: The priced (base) currency — the left side of the
            ``XXX/YYY`` column header (ISO 4217).
        rate_to_reference: Strictly-positive price of one ``currency``
            unit in ``reference_currency``.
        reference_currency: The quote (right) side of the header. Constant
            across every column in the sheet — the sheet declares one
            reference currency (ADR-0099 §5).
    """

    as_of_date: _date
    currency: str
    rate_to_reference: Decimal
    reference_currency: str


@dataclass(frozen=True)
class ImportedRegionWeight:
    """One region-allocation row extracted from an Excel snapshot.

    Per ADR-0046, Excel import region rows (``"DACH"``, ``"Asia
    Emerging"``, …) are resolved strictly against the per-tenant
    ``regions`` catalogue. The extractor emits typed payloads only
    when the Excel label resolves; unknown labels raise an
    :class:`ImportRowError` and are dropped.

    Attributes:
        region_id: UUID of the resolved region row. The extractor
            receives the lookup map from the service.
        weight_pct: Percentage weight in the closed interval
            ``[0, 100]``. The DB-side CHECK constraint mirrors the
            extractor's range guard.
    """

    region_id: UUID
    weight_pct: Decimal


@dataclass(frozen=True)
class ImportedSectorWeight:
    """One sector-allocation row extracted from an Excel snapshot.

    Per ADR-0045 §2, sector splits live as attribute rows in the
    ``Attributes`` sheet whose label normalises to
    ``sector_<code>``. Unlike countries, sector resolution is
    **strict**: the extractor only emits a typed payload when the
    sector code resolves against the per-tenant catalogue. Unknown
    sectors generate an :class:`ImportRowError` and are dropped.

    Attributes:
        sector_id: UUID of the resolved sector row. The extractor
            receives the lookup map from the service.
        weight_pct: Percentage weight in the closed interval
            ``[0, 100]``.
    """

    sector_id: UUID
    weight_pct: Decimal


@dataclass(frozen=True)
class ImportedBondAnalytics:
    """One Fixed-Income characteristics row extracted from a snapshot.

    Maps onto the ``investment_bond_analytics`` natural key
    ``(investment_id, as_of_date)`` (ADR-0079 §2). FK resolution from
    ``investment_name`` to ``investment_id`` happens at the service
    layer; the extractor stays DB-free.

    Attributes:
        investment_name: The investment the row belongs to. Resolved
            against the ``Attributes`` columns at extraction time.
        as_of_date: Statement-day date.
        ytm: Yield-to-maturity (decimal fraction; may be negative).
            NOT NULL — a blank cell is a row-level error.
        eff_duration: Effective duration in years. NOT NULL.
        oas: Option-adjusted spread, or ``None`` (the nullable column).
        convexity: Convexity, or ``None`` (the nullable column).
    """

    investment_name: str
    as_of_date: _date
    ytm: Decimal
    eff_duration: Decimal
    oas: Decimal | None
    convexity: Decimal | None


@dataclass(frozen=True)
class ImportedRatingWeight:
    """One credit-rating-bucket weight row extracted from a snapshot.

    Maps onto the ``investment_rating_weight`` natural key
    ``(investment_id, as_of_date, rating_bucket)`` (ADR-0079 §2).

    Attributes:
        investment_name: The investment the row belongs to.
        as_of_date: Statement-day date.
        rating_bucket: One of the eight canonical rating buckets.
        weight_pct: Percentage in the closed interval ``[0, 100]``;
            weights need not sum to 100.
    """

    investment_name: str
    as_of_date: _date
    rating_bucket: str
    weight_pct: Decimal


@dataclass(frozen=True)
class ImportedMaturityWeight:
    """One maturity-bucket weight row extracted from a snapshot.

    Maps onto the ``investment_maturity_weight`` natural key
    ``(investment_id, as_of_date, maturity_bucket)`` (ADR-0079 §2).

    Attributes:
        investment_name: The investment the row belongs to.
        as_of_date: Statement-day date.
        maturity_bucket: One of the six canonical maturity buckets.
        weight_pct: Percentage in the closed interval ``[0, 100]``;
            weights need not sum to 100.
    """

    investment_name: str
    as_of_date: _date
    maturity_bucket: str
    weight_pct: Decimal


@dataclass(frozen=True)
class ExtractionWarning:
    """One soft, non-fatal note collected during an Excel import.

    Warnings are reported alongside :class:`ImportRowError`s but do
    not represent dropped data. The Phase-5a country-split path
    uses warnings to record values that were *mapped* to the ``XX``
    sentinel rather than rejected outright.

    Attributes:
        investment_name: The investment whose row triggered the
            warning, or ``None`` if the warning is sheet-level.
        field: Short identifier of the affected field (e.g.
            ``"country_split"``).
        raw_value: The unrecognised raw value from the Excel cell or
            row label.
        action: Short identifier of the soft action taken (e.g.
            ``"mapped_to_XX"``).
        message: Human-readable description.
    """

    investment_name: str | None
    field: str
    raw_value: str
    action: str
    message: str


@dataclass(frozen=True)
class ImportRowError:
    """One row-level error in an Excel import.

    Note:
        The dataclass is not an exception — it is a structured record
        of a per-row problem that the extractor surfaces as data so
        the rest of the import can proceed (ADR-0043 §3 partial-success
        convention). The original name was ``ImportError``; it was
        renamed to ``ImportRowError`` in Phase-5 hygiene to avoid
        shadowing the Python builtin within the module namespace.

    Attributes:
        investment_name: The investment whose row triggered the
            error, or ``None`` if the error is sheet-level.
        sheet: Canonical snake_case sheet key (e.g. ``"navs_actual"``)
            or the synthetic key ``"attributes"``.
        row_index: Optional row position (the Excel row index, not the
            ``data`` array index — Phase 4 reports the canonical date
            string for time-series sheets and a numeric attribute-row
            index for the Attributes sheet, since the operator-facing
            row number is hard to reconstruct from the JSONB shape
            alone).
        column: Optional sheet column or attribute label.
        message: Human-readable description.
    """

    investment_name: str | None
    sheet: str
    row_index: str | None
    column: str | None
    message: str


@dataclass(frozen=True)
class InvestmentExtractionResult:
    """Outcome of an Excel-to-investments transformation.

    The counts reported here describe the *write* effect produced by
    :meth:`InvestmentService.transform_upload_to_investments`. A
    pure-extractor invocation (without persistence) returns this
    dataclass with all counts set to zero except those derivable from
    the extraction itself; the persistence layer fills the rest.

    Attributes:
        investments_created: Number of new ``investments`` rows
            inserted.
        investments_updated: Number of pre-existing investments whose
            mutable fields were refreshed from the Excel attributes.
        investments_deactivated: Number of investments soft-deleted
            (``is_active = FALSE``) because they were absent from the
            Excel snapshot (ADR-0043 §3 B2.b).
        investments_reactivated: Number of investments transitioned
            from ``is_active = FALSE`` back to ``TRUE`` because they
            reappeared in the snapshot.
        navs_replaced: Total NAV rows deleted-and-reinserted across
            the per-investment replace loop (``navs_inserted``,
            because the per-investment delete clears the previous
            generation and the new generation is inserted).
        cashflows_replaced: Same convention for cashflows.
        region_weights_replaced: Total region-weight rows
            deleted-and-reinserted across the per-investment replace
            loop (Phase-6 region model, ADR-0046). Replaces the
            former ``country_weights_replaced`` counter — the Excel
            import path writes ``investment_region_weights``, not
            ``investment_country_weights``.
        sector_weights_replaced: Same convention for sector weights.
        bond_analytics_replaced: Total ``investment_bond_analytics``
            rows written across the per-investment replace loop
            (delete-for-investment then per-date upsert). 0 unless the
            liquid-archetype path is active (ADR-0081).
        rating_weights_replaced: Same convention for
            ``investment_rating_weight`` rows.
        maturity_weights_replaced: Same convention for
            ``investment_maturity_weight`` rows.
        cash_statement_rows: Statement cells read from the ``Cash``
            sheet across every cash position (ADR-0103 §3). The other
            six cash counters describe the *write* effect the
            statement-to-ledger derivation (ADR-0103 §4) produced from
            them — and unlike the replace-by-investment counters above,
            they are genuine deltas: the importer reconciles its own
            ``'excel'`` ledger and price rows in place, so an unchanged
            Cash sheet re-imports with all six at zero. That is the
            operator-visible idempotency signal.
        cash_ledger_inserted: ``position_transactions`` rows written —
            the ``opening`` and the per-statement-date ``transfer``
            deltas. A zero delta writes nothing (ADR-0103 §4).
        cash_ledger_updated: Existing ``'excel'`` ledger rows whose
            units or trade date the workbook restated.
        cash_ledger_deleted: ``'excel'`` ledger rows stranded by a
            statement date that left the sheet. ``'manual'`` rows are
            never touched.
        cash_prices_written: Unity (``1.0000``) ``instrument_prices``
            rows written, one per new statement date (ADR-0103 §1).
        cash_prices_deleted: Unity price rows stranded by a removed
            statement date.
        errors: Collected row-level errors. May be non-empty even on
            a successful transformation (ADR-0043 §3 partial-success).
        warnings: Collected soft notes — emitted, for example, when a
            sector label was auto-created.
    """

    investments_created: int = 0
    investments_updated: int = 0
    investments_deactivated: int = 0
    investments_reactivated: int = 0
    navs_replaced: int = 0
    cashflows_replaced: int = 0
    region_weights_replaced: int = 0
    sector_weights_replaced: int = 0
    bond_analytics_replaced: int = 0
    rating_weights_replaced: int = 0
    maturity_weights_replaced: int = 0
    # Cash statement path (ADR-0103 §3/§4). Zero for every workbook
    # without a ``Cash`` sheet, so a v31 import reports exactly what it
    # reported before.
    cash_statement_rows: int = 0
    cash_ledger_inserted: int = 0
    cash_ledger_updated: int = 0
    cash_ledger_deleted: int = 0
    cash_prices_written: int = 0
    cash_prices_deleted: int = 0
    errors: tuple[ImportRowError, ...] = field(default_factory=tuple)
    warnings: tuple[ExtractionWarning, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_blank(value: Any) -> bool:
    """Return ``True`` if ``value`` should be treated as an empty cell.

    Pandas ``to_json`` serialises ``NaN`` as ``null`` and ``None``
    survives the JSON round-trip, so the JSONB shape carries Python
    ``None`` for missing cells. Empty / whitespace-only strings count
    as blanks too — Excel import spreadsheets sometimes carry a literal blank
    string instead of a true None.
    """
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return bool(isinstance(value, str) and not value.strip())


def _normalise_attribute_key(raw: Any) -> str:
    """Normalise an Attributes-sheet row label for lookup.

    Trims, lower-cases, replaces internal whitespace and hyphens with
    underscores so ``"Manager / Fondsname"`` and
    ``"manager_/_fondsname"`` collide on a single key.
    """
    if not isinstance(raw, str):
        return ""
    cleaned = raw.strip().lower()
    # Collapse runs of whitespace + hyphens to a single underscore.
    out: list[str] = []
    last_underscore = False
    for ch in cleaned:
        if ch.isspace() or ch == "-":
            if not last_underscore:
                out.append("_")
                last_underscore = True
        else:
            out.append(ch)
            last_underscore = False
    return "".join(out).strip("_")


def _normalise_investment_type(raw: Any) -> str | None:
    """Resolve an Excel investment-type cell to one of the eight canonical values.

    Args:
        raw: The raw cell value.

    Returns:
        The canonical type, or ``None`` if the value is blank or
        does not match any known alias. The caller turns ``None``
        into a row-level :class:`ImportRowError`.
    """
    if _is_blank(raw):
        return None
    if not isinstance(raw, str):
        return None
    key = _normalise_attribute_key(raw)
    if not key:
        return None
    canonical = _INVESTMENT_TYPE_ALIASES.get(key)
    if canonical is not None and canonical in _VALID_INVESTMENT_TYPES:
        return canonical
    return None


def _coerce_decimal(raw: Any) -> Decimal | None:
    """Convert a numeric-or-stringified-numeric to :class:`Decimal`.

    Returns ``None`` for blank cells. NaN floats and unparseable
    strings also return ``None`` so the caller can decide whether to
    skip or error.
    """
    if _is_blank(raw):
        return None
    if isinstance(raw, bool):  # bool is an int subclass; reject.
        return None
    if isinstance(raw, (int, float)):
        if isinstance(raw, float) and math.isnan(raw):
            return None
        # Decimal(str(...)) avoids float-binary surprises like
        # Decimal(0.1) producing a 17-digit value.
        return Decimal(str(raw))
    if isinstance(raw, str):
        candidate = raw.strip().replace(",", "")
        if not candidate:
            return None
        try:
            return Decimal(candidate)
        except InvalidOperation:
            return None
    return None


def _parse_iso_date(raw: Any) -> _date | None:
    """Parse an ISO-8601 date string from a JSONB index entry.

    Pandas writes timestamps as ``"YYYY-MM-DDTHH:MM:SS.fff"`` or
    plain ``"YYYY-MM-DD"`` depending on the dtype. ``fromisoformat``
    handles both forms on Python 3.11+. Returns ``None`` on parse
    failure so the caller can attach a row-level error.
    """
    if not isinstance(raw, str):
        return None
    candidate = raw.strip()
    if not candidate:
        return None
    try:
        # Strip a trailing 'Z' since fromisoformat doesn't accept it
        # before Python 3.12 — even then, a date-only string never
        # carries one, and our DataFrame.to_json output uses
        # ".000" rather than 'Z'.
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        parsed = datetime.fromisoformat(candidate)
        return parsed.date()
    except ValueError:
        return None


def _coerce_attr_date(raw: Any) -> _date | None:
    """Coerce an ``Attributes``-cell value to a :class:`date`.

    The ``Attributes`` sheet carries **mixed dtypes**, so a date-valued
    attribute cell can arrive in three shapes and this helper accepts all
    of them:

    * an ISO-8601 string — the shape produced by the upload serialisation
      (``DataUploadRepository`` uses ``to_json(orient="split",
      date_format="iso")``), so a real Excel date cell lands as
      ``"2016-05-01T00:00:00.000"`` and a hand-typed date as
      ``"2016-05-01"``;
    * a ``datetime``/``date`` object — defensive, should a caller pass the
      DataFrame through without the JSON round-trip;
    * an **epoch-milliseconds** ``int``/``float`` (``1462060800000``) —
      defensive, the shape a default (``date_format="epoch"``)
      serialisation would emit for a datetime in an object column.

    Returns ``None`` for a blank or unparseable cell so the caller can
    raise a row-level :class:`ImportRowError` with the offending value.
    """
    if _is_blank(raw):
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, _date):
        return raw
    if isinstance(raw, bool):  # bool is an int subclass; reject.
        return None
    if isinstance(raw, (int, float)):
        if isinstance(raw, float) and math.isnan(raw):
            return None
        # Epoch milliseconds (UTC) — the pandas ``to_json`` encoding for a
        # datetime in an object-dtype cell. An Excel date is a naive
        # midnight, so the UTC instant maps back to the same calendar day.
        try:
            return datetime.fromtimestamp(raw / 1000, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(raw, str):
        return _parse_iso_date(raw)
    return None


def _coerce_int_year(raw: Any) -> int | None:
    """Convert a vintage-year cell (often ``"2020"`` or ``2020``) to int."""
    if _is_blank(raw):
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        if math.isnan(raw):
            return None
        if not raw.is_integer():
            return None
        return int(raw)
    if isinstance(raw, str):
        candidate = raw.strip()
        if not candidate:
            return None
        try:
            return int(candidate)
        except ValueError:
            try:
                # Accept "2020.0" by going via float → int with
                # is_integer() check.
                f = float(candidate)
                if f.is_integer():
                    return int(f)
            except ValueError:
                return None
            return None
    return None


def _validate_split_payload(sheet: str, payload: Any) -> dict[str, Any]:
    """Sanity-check the JSONB shape of one sheet.

    Returns the payload as a typed dict. Raises
    :class:`ImportFormatError` if the payload is structurally invalid
    — that is a precondition violation, not a row-level concern.
    """
    if not isinstance(payload, dict):
        raise ImportFormatError(
            f"Sheet {sheet!r} has a non-dict JSONB payload (got {type(payload).__name__})."
        )
    for key in ("columns", "index", "data"):
        if key not in payload:
            raise ImportFormatError(
                f"Sheet {sheet!r} is missing the {key!r} key in its JSONB split payload."
            )
    return payload


def _attributes_dataframe_from_sheets(
    upload_sheets: dict[str, dict[str, Any]],
) -> pd.DataFrame:
    """Reconstruct the ``Attributes`` DataFrame from a JSONB sheet dict.

    Mirrors the shape consumed by
    :func:`services.reporting.attributes_partition.partition_attributes`:
    rows indexed by attribute label, columns by investment name.
    """
    attributes_payload = upload_sheets.get(_ATTRIBUTES_KEY)
    if attributes_payload is None:
        raise ImportFormatError(
            "The Excel snapshot has no 'Attributes' sheet — split extraction requires it."
        )
    attr_split = _validate_split_payload(_ATTRIBUTES_KEY, attributes_payload)
    investment_names: list[str] = [str(c).strip() for c in attr_split["columns"] if str(c).strip()]
    if not investment_names:
        raise ImportFormatError(
            "The 'Attributes' sheet has no investment columns; "
            "the Excel import format discovers columns from row 1 dynamically "
            "and at least one is required."
        )
    return pd.DataFrame(
        data=list(attr_split["data"]),
        index=list(attr_split["index"]),
        columns=investment_names,
    )


# ---------------------------------------------------------------------------
# The extractor itself
# ---------------------------------------------------------------------------


class InvestmentExtractor:
    """Stateful Excel-import-snapshot → :class:`ImportedInvestment` extractor.

    Stateful only in the sense that it accumulates row-level errors
    on ``self._errors``; calling :meth:`extract` more than once on
    the same instance resets the buffer. The extractor has no
    FastAPI / DB / session dependency and is unit-testable in
    isolation.
    """

    def __init__(self) -> None:
        self._errors: list[ImportRowError] = []
        self._warnings: list[ExtractionWarning] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def errors(self) -> list[ImportRowError]:
        """Errors collected during the most recent :meth:`extract` call."""
        return list(self._errors)

    @property
    def warnings(self) -> list[ExtractionWarning]:
        """Warnings collected during the most recent extractor invocation.

        Mirrors :attr:`errors` but holds soft, non-fatal notes — the
        canonical example is an unrecognised country ISO code that
        was mapped to the ``XX`` sentinel.
        """
        return list(self._warnings)

    def extract(
        self,
        upload_sheets: dict[str, dict[str, Any]],
        *,
        valid_anlv_codes: frozenset[str] | None = None,
    ) -> list[ImportedInvestment]:
        """Parse the JSONB sheets dict into typed :class:`ImportedInvestment`s.

        Args:
            upload_sheets: Mapping from canonical snake_case sheet
                name (``"attributes"``, ``"navs_actual"``, …) to the
                ``DataFrame.to_json(orient="split")`` payload that
                Phase 2 persists in ``data_upload_sheets.data``.
            valid_anlv_codes: Optional set of valid AnlV catalogue
                codes (e.g. ``frozenset({"anlv_13", "anlv_14", …})``).
                When supplied, every non-empty AnlV cell is validated
                against this set and an :class:`ImportRowError` is
                appended for codes that fail the lookup. When
                ``None`` (e.g. in extractor-only unit tests) the
                normalisation step still runs but no catalogue
                validation is performed.

        Returns:
            One :class:`ImportedInvestment` per investment column
            present in the ``Attributes`` sheet. Investments whose
            ``Attributes`` row triggers a hard error (e.g. unknown
            investment type) are *omitted* from the return value;
            the corresponding :class:`ImportRowError` records appear in
            :attr:`errors`.

        Raises:
            ImportFormatError: If ``Attributes`` is missing entirely,
                if any sheet has an invalid JSONB shape, or if no
                investment columns are discoverable in
                ``Attributes``. Row-level errors do not raise.
        """
        self._errors = []
        self._warnings = []

        attributes_payload = upload_sheets.get(_ATTRIBUTES_KEY)
        if attributes_payload is None:
            raise ImportFormatError(
                "The Excel snapshot has no 'Attributes' sheet — the "
                "investment-stammdaten table is required for the "
                "Phase-4 transformation."
            )

        attr_split = _validate_split_payload(_ATTRIBUTES_KEY, attributes_payload)
        investment_names: list[str] = [
            str(c).strip() for c in attr_split["columns"] if str(c).strip()
        ]
        if not investment_names:
            raise ImportFormatError(
                "The 'Attributes' sheet has no investment columns; "
                "the Excel import format discovers columns from row 1 dynamically "
                "and at least one is required."
            )

        # Walk attribute rows once into a dict[label_key → list[per-column-value]]
        attr_index = list(attr_split["index"])
        attr_rows = list(attr_split["data"])
        attr_table: dict[str, list[Any]] = {}
        for row_idx, label in enumerate(attr_index):
            if row_idx >= len(attr_rows):
                break
            key = _normalise_attribute_key(label)
            if not key:
                continue
            row_values = list(attr_rows[row_idx])
            # Pad / truncate row to the discovered column count so
            # downstream lookups never IndexError on inconsistent
            # JSONB shapes.
            if len(row_values) < len(investment_names):
                row_values.extend([None] * (len(investment_names) - len(row_values)))
            attr_table[key] = row_values[: len(investment_names)]

        # Validate the JSONB shape of every time-series sheet we will
        # consume so a structural fault surfaces *before* we walk the
        # attributes table — the operator otherwise sees a confusing
        # mix of partial output plus a structural error.
        for sheet_key in (k for k, *_ in _NAV_SHEETS):
            payload = upload_sheets.get(sheet_key)
            if payload is not None:
                _validate_split_payload(sheet_key, payload)
        for sheet_key in (k for k, *_ in _CASHFLOW_SHEETS):
            payload = upload_sheets.get(sheet_key)
            if payload is not None:
                _validate_split_payload(sheet_key, payload)
        # Liquid-archetype income sheets (ADR-0081) ride the same wide
        # cash-flow idiom; validate their JSONB shape up front too so a
        # structural fault surfaces before the per-investment walk.
        for sheet_key, _ in _INCOME_SHEETS:
            payload = upload_sheets.get(sheet_key)
            if payload is not None:
                _validate_split_payload(sheet_key, payload)
        # The Cash statement sheet (ADR-0103 §3) — optional, same wide idiom.
        cash_payload = upload_sheets.get(_CASH_KEY)
        if cash_payload is not None:
            _validate_split_payload(_CASH_KEY, cash_payload)

        results: list[ImportedInvestment] = []
        for col_idx, name in enumerate(investment_names):
            extracted = self._extract_one_investment(
                col_idx=col_idx,
                name=name,
                investment_names=investment_names,
                attr_table=attr_table,
                upload_sheets=upload_sheets,
                valid_anlv_codes=valid_anlv_codes,
            )
            if extracted is not None:
                results.append(extracted)

        logger.info(
            "InvestmentExtractor.extract: %d investment(s) extracted, %d error(s) collected.",
            len(results),
            len(self._errors),
        )
        return results

    def partition_attributes_from_sheets(
        self, upload_sheets: dict[str, dict[str, Any]]
    ) -> AttributesPartition:
        """Identify sector and country row labels in the ``Attributes`` sheet.

        Wraps :func:`services.reporting.attributes_partition
        .partition_attributes` so the persistence orchestrator can
        discover sector labels (and auto-create missing tenant
        sectors) before calling :meth:`extract_sector_weights`.

        Args:
            upload_sheets: Same JSONB dict accepted by :meth:`extract`.

        Returns:
            :class:`AttributesPartition` with the sector and country
            row labels.

        Raises:
            ImportFormatError: If the ``Attributes`` sheet is missing
                or has no investment columns.
        """
        df = _attributes_dataframe_from_sheets(upload_sheets)
        return partition_attributes(df)

    def extract_region_weights(
        self,
        upload_sheets: dict[str, dict[str, Any]],
        regions_by_display_name: dict[str, UUID],
    ) -> dict[str, list[ImportedRegionWeight]]:
        """Parse region-split rows from the ``Attributes`` sheet.

        Block identification is delegated to
        :func:`services.reporting.attributes_partition
        .partition_attributes`: the second contiguous block of
        numeric breakdown rows historically housed the country split.
        Phase-6 retains the block layout but resolves the labels
        against the per-tenant ``regions`` catalogue rather than the
        ISO ``countries`` table (ADR-0046).

        Row values are fractions in ``[0, 1.05]``; the extractor
        multiplies by 100 to produce the DB-side percentage value.

        Region resolution is **strict** (ADR-0046): the regions
        catalogue is pre-seeded by ``portfoliflow bootstrap``, and an
        unknown Excel label is a data error rather than a soft
        fallback. The extractor appends an :class:`ImportRowError`
        for each unresolved label and drops every weight in the
        affected row. Range violations (negative, sum > 100) drop
        the whole region-split block for the affected investment.

        This method does **not** reset the error / warning buffers —
        it is meant to be called *after* :meth:`extract` so the
        persistence orchestrator can collect every problem in one
        pass.

        Args:
            upload_sheets: Same JSONB dict accepted by :meth:`extract`.
            regions_by_display_name: Mapping from lower-cased region
                display name → tenant ``regions.id`` UUID. The
                service constructs this from
                :meth:`RegionRepository.list_all`.

        Returns:
            Mapping from investment name → list of
            :class:`ImportedRegionWeight`. Investments with no
            region-split rows in the snapshot map to an empty list.

        Raises:
            ImportFormatError: If the ``Attributes`` sheet is
                missing or has no investment columns.
        """
        df = _attributes_dataframe_from_sheets(upload_sheets)
        investment_names: list[str] = list(df.columns)
        out: dict[str, list[ImportedRegionWeight]] = {name: [] for name in investment_names}

        partition = partition_attributes(df)
        region_labels: tuple[str, ...] = partition.country_rows
        if not region_labels:
            return out

        # Resolve labels to region UUIDs once. The mapping is per
        # row, not per investment, so two investments share the same
        # resolved id. ``None`` for an unknown label flags an error
        # later in the loop.
        resolved_ids: list[tuple[str, UUID | None]] = []
        for label in region_labels:
            cleaned = str(label).strip()
            resolved = regions_by_display_name.get(cleaned.lower())
            resolved_ids.append((cleaned, resolved))

        for name in investment_names:
            aggregated: dict[UUID, Decimal] = {}
            block_invalid = False
            for label, resolved in resolved_ids:
                cell_value = df.at[label, name] if label in df.index else None
                weight_frac = _coerce_decimal(cell_value)
                if weight_frac is None or weight_frac == 0:
                    continue
                if resolved is None:
                    self._errors.append(
                        ImportRowError(
                            investment_name=name,
                            sheet=_ATTRIBUTES_KEY,
                            row_index=label,
                            column=name,
                            message=(
                                f"Region label {label!r} did not match "
                                "any region display_name in the tenant. "
                                "Either update the Excel to use one of "
                                "the existing region labels, or extend "
                                "the regions catalogue via a bootstrap "
                                "update (see ADR-0046)."
                            ),
                        )
                    )
                    continue
                if weight_frac < 0:
                    self._errors.append(
                        ImportRowError(
                            investment_name=name,
                            sheet=_ATTRIBUTES_KEY,
                            row_index=str(label),
                            column=name,
                            message=(
                                f"Region split weight {weight_frac} is "
                                "negative; expected a fraction in [0, 1]. "
                                "Whole region-split block for this "
                                "investment dropped."
                            ),
                        )
                    )
                    block_invalid = True
                    break
                weight_pct = weight_frac * _PERCENT_SCALE
                aggregated[resolved] = aggregated.get(resolved, Decimal("0")) + weight_pct
            if block_invalid:
                out[name] = []
                continue

            total = sum(aggregated.values(), Decimal("0"))
            if total > Decimal("100"):
                self._errors.append(
                    ImportRowError(
                        investment_name=name,
                        sheet=_ATTRIBUTES_KEY,
                        row_index="region_split_sum",
                        column=name,
                        message=(
                            f"Region split weights sum to {total} > 100. "
                            "Whole region-split block for this "
                            "investment dropped."
                        ),
                    )
                )
                out[name] = []
                continue
            out[name] = [
                ImportedRegionWeight(region_id=rid, weight_pct=weight)
                for rid, weight in aggregated.items()
            ]

        return out

    def extract_sector_weights(
        self,
        upload_sheets: dict[str, dict[str, Any]],
        sectors_by_label: dict[str, UUID],
    ) -> dict[str, list[ImportedSectorWeight]]:
        """Parse sector-split rows from the ``Attributes`` sheet.

        Block identification is delegated to
        :func:`services.reporting.attributes_partition
        .partition_attributes`: the first contiguous block of numeric
        breakdown rows is the sector block. Row values are fractions
        in ``[0, 1.05]``; the extractor multiplies by 100 to produce
        the DB-side percentage value.

        Sector resolution is **lookup-only** at this layer — the
        service is responsible for ensuring every label in the sector
        block has a corresponding entry in ``sectors_by_label`` (the
        auto-creation case lives in the persistence orchestrator so
        the extractor remains pure / Qt-free / DB-free). Labels that
        still miss after the service's pre-pass produce a row-level
        error and are dropped.

        Range violations (negative, sum > 100) drop the whole
        sector-split block for the affected investment.

        This method does **not** reset the error / warning buffers.

        Args:
            upload_sheets: Same JSONB dict accepted by :meth:`extract`.
            sectors_by_label: Mapping from lower-cased lookup label
                (display name or canonical code) → tenant sector
                UUID. The service constructs this from
                :meth:`SectorRepository.list_active` and inserts
                additional entries for auto-created sectors before
                calling this method.

        Returns:
            Mapping from investment name → list of
            :class:`ImportedSectorWeight`. Investments with no sector
            rows in the snapshot map to an empty list.

        Raises:
            ImportFormatError: If the ``Attributes`` sheet is
                missing or has no investment columns.
        """
        df = _attributes_dataframe_from_sheets(upload_sheets)
        investment_names: list[str] = list(df.columns)
        out: dict[str, list[ImportedSectorWeight]] = {name: [] for name in investment_names}

        partition = partition_attributes(df)
        sector_labels: tuple[str, ...] = partition.sector_rows
        if not sector_labels:
            return out

        # Per-investment aggregation — entries are combined by
        # ``sector_id`` so the downstream UNIQUE ``(investment_id,
        # sector_id)`` constraint is satisfied even if the same
        # sector appears under multiple labels.
        for name in investment_names:
            aggregated: dict[UUID, Decimal] = {}
            block_invalid = False
            for label in sector_labels:
                cleaned = str(label).strip()
                cell_value = df.at[label, name] if label in df.index else None
                weight_frac = _coerce_decimal(cell_value)
                if weight_frac is None or weight_frac == 0:
                    continue
                resolved_id = sectors_by_label.get(cleaned.lower())
                if resolved_id is None:
                    self._errors.append(
                        ImportRowError(
                            investment_name=name,
                            sheet=_ATTRIBUTES_KEY,
                            row_index=cleaned,
                            column=name,
                            message=(
                                f"Sector label {cleaned!r} not found in "
                                "tenant catalogue; sector split for this "
                                "row dropped."
                            ),
                        )
                    )
                    continue
                if weight_frac < 0:
                    self._errors.append(
                        ImportRowError(
                            investment_name=name,
                            sheet=_ATTRIBUTES_KEY,
                            row_index=cleaned,
                            column=name,
                            message=(
                                f"Sector split weight {weight_frac} is "
                                "negative; expected a fraction in [0, 1]. "
                                "Whole sector-split block for this "
                                "investment dropped."
                            ),
                        )
                    )
                    block_invalid = True
                    break
                weight_pct = weight_frac * _PERCENT_SCALE
                aggregated[resolved_id] = aggregated.get(resolved_id, Decimal("0")) + weight_pct
            if block_invalid:
                out[name] = []
                continue

            total = sum(aggregated.values(), Decimal("0"))
            if total > Decimal("100"):
                self._errors.append(
                    ImportRowError(
                        investment_name=name,
                        sheet=_ATTRIBUTES_KEY,
                        row_index="sector_split_sum",
                        column=name,
                        message=(
                            f"Sector split weights sum to {total} > 100. "
                            "Whole sector-split block for this "
                            "investment dropped."
                        ),
                    )
                )
                out[name] = []
                continue
            out[name] = [
                ImportedSectorWeight(sector_id=sid, weight_pct=weight)
                for sid, weight in aggregated.items()
            ]

        return out

    # ------------------------------------------------------------------
    # Liquid-archetype reference-sheet extraction (ADR-0081 / ADR-0079)
    #
    # Three tidy/long sheets, each one workbook row → one DB row on the
    # table's natural key. Names are resolved against the ``Attributes``
    # columns; failures are collected as row-level ``ImportRowError``
    # (partial-success, ADR-0043 §3) — never thrown. These methods do
    # **not** reset the error/warning buffers so the orchestrator can
    # collect every problem in one pass (same contract as
    # :meth:`extract_region_weights`).
    # ------------------------------------------------------------------

    def _iter_tidy_reference_rows(
        self,
        *,
        upload_sheets: dict[str, dict[str, Any]],
        sheet_key: str,
        columns: tuple[str, ...],
        investment_names: list[str],
    ):
        """Yield ``(investment_name, as_of_date, value_cells)`` per tidy row.

        ``value_cells`` maps each non-key canonical column name to its
        raw cell value (the caller coerces / validates it). Rows whose
        ``investment`` cell is blank or not present in the ``Attributes``
        columns, or whose ``as_of_date`` does not parse, are dropped with
        a row-level :class:`ImportRowError`.

        Args:
            upload_sheets: Same JSONB dict accepted by :meth:`extract`.
            sheet_key: Canonical snake_case key of the tidy sheet.
            columns: The canonical column tuple (must contain
                ``"as_of_date"`` and ``"investment"``).
            investment_names: The investment columns discovered from the
                ``Attributes`` sheet.

        Raises:
            ImportFormatError: If the sheet is present but missing one
                of the required ``columns`` — a structural fault.
        """
        payload = upload_sheets.get(sheet_key)
        if payload is None:
            return
        split = _validate_split_payload(sheet_key, payload)
        sheet_columns = [str(c) for c in split["columns"]]
        col_index: dict[str, int] = {}
        for required in columns:
            try:
                col_index[required] = sheet_columns.index(required)
            except ValueError as exc:
                raise ImportFormatError(
                    f"Sheet {sheet_key!r} is missing the required column "
                    f"{required!r}. Expected columns {list(columns)}."
                ) from exc

        name_set = set(investment_names)
        value_columns = [c for c in columns if c not in ("as_of_date", "investment")]
        rows = list(split["data"])
        index = list(split["index"])

        for row_idx, raw_row in enumerate(rows):
            row = list(raw_row)
            row_ref = str(index[row_idx]) if row_idx < len(index) else str(row_idx)

            def _cell(colname: str) -> Any:
                pos = col_index[colname]
                return row[pos] if pos < len(row) else None

            raw_name = _cell("investment")
            if _is_blank(raw_name):
                # Fully-blank rows are skipped silently; a blank name
                # alongside other data is a genuine row error.
                if all(_is_blank(c) for c in row):
                    continue
                self._errors.append(
                    ImportRowError(
                        investment_name=None,
                        sheet=sheet_key,
                        row_index=row_ref,
                        column="investment",
                        message="Row has no investment name; dropped.",
                    )
                )
                continue
            inv_name = str(raw_name).strip()
            if inv_name not in name_set:
                self._errors.append(
                    ImportRowError(
                        investment_name=inv_name,
                        sheet=sheet_key,
                        row_index=row_ref,
                        column="investment",
                        message=(
                            f"Investment {inv_name!r} is not present in the "
                            "Attributes columns; row dropped."
                        ),
                    )
                )
                continue

            as_of = _parse_iso_date(_cell("as_of_date"))
            if as_of is None:
                self._errors.append(
                    ImportRowError(
                        investment_name=inv_name,
                        sheet=sheet_key,
                        row_index=row_ref,
                        column="as_of_date",
                        message=(
                            f"Could not parse {_cell('as_of_date')!r} as an ISO date; row dropped."
                        ),
                    )
                )
                continue

            yield inv_name, as_of, {c: _cell(c) for c in value_columns}

    def extract_bond_analytics(
        self,
        upload_sheets: dict[str, dict[str, Any]],
    ) -> dict[str, list[ImportedBondAnalytics]]:
        """Parse the ``Bond Analytics`` tidy sheet into typed rows.

        ``ytm`` and ``eff_duration`` are NOT NULL (a blank cell drops
        the row with an error); ``oas`` and ``convexity`` may be blank
        (the nullable columns). ``ytm`` may be negative.

        Args:
            upload_sheets: Same JSONB dict accepted by :meth:`extract`.

        Returns:
            Mapping from investment name → list of
            :class:`ImportedBondAnalytics`. Every name present in the
            ``Attributes`` columns is a key (empty list when the sheet
            carries no rows for it).

        Raises:
            ImportFormatError: If the ``Attributes`` sheet is missing /
                column-less, or the present ``Bond Analytics`` sheet is
                missing a required column.
        """
        df = _attributes_dataframe_from_sheets(upload_sheets)
        investment_names: list[str] = list(df.columns)
        out: dict[str, list[ImportedBondAnalytics]] = {name: [] for name in investment_names}
        for inv_name, as_of, vals in self._iter_tidy_reference_rows(
            upload_sheets=upload_sheets,
            sheet_key=_BOND_ANALYTICS_KEY,
            columns=_BOND_ANALYTICS_COLUMNS,
            investment_names=investment_names,
        ):
            ytm = _coerce_decimal(vals["ytm"])
            if ytm is None:
                self._errors.append(
                    ImportRowError(
                        investment_name=inv_name,
                        sheet=_BOND_ANALYTICS_KEY,
                        row_index=as_of.isoformat(),
                        column="ytm",
                        message=(
                            f"ytm {vals['ytm']!r} is missing or non-numeric; "
                            "ytm is required (NOT NULL). Row dropped."
                        ),
                    )
                )
                continue
            eff_duration = _coerce_decimal(vals["eff_duration"])
            if eff_duration is None:
                self._errors.append(
                    ImportRowError(
                        investment_name=inv_name,
                        sheet=_BOND_ANALYTICS_KEY,
                        row_index=as_of.isoformat(),
                        column="eff_duration",
                        message=(
                            f"eff_duration {vals['eff_duration']!r} is "
                            "missing or non-numeric; eff_duration is "
                            "required (NOT NULL). Row dropped."
                        ),
                    )
                )
                continue
            out[inv_name].append(
                ImportedBondAnalytics(
                    investment_name=inv_name,
                    as_of_date=as_of,
                    ytm=ytm,
                    eff_duration=eff_duration,
                    oas=_coerce_decimal(vals["oas"]),
                    convexity=_coerce_decimal(vals["convexity"]),
                )
            )
        return out

    def extract_rating_weights(
        self,
        upload_sheets: dict[str, dict[str, Any]],
    ) -> dict[str, list[ImportedRatingWeight]]:
        """Parse the ``Rating Weights`` tidy sheet into typed rows.

        ``rating_bucket`` is validated against the eight canonical
        buckets (unknown → row error, dropped); ``weight_pct`` must be a
        number in ``[0, 100]`` (weights need not sum to 100).

        Args:
            upload_sheets: Same JSONB dict accepted by :meth:`extract`.

        Returns:
            Mapping from investment name → list of
            :class:`ImportedRatingWeight`. Every name present in the
            ``Attributes`` columns is a key.

        Raises:
            ImportFormatError: As for :meth:`extract_bond_analytics`.
        """
        return self._extract_bucket_weights(
            upload_sheets=upload_sheets,
            sheet_key=_RATING_WEIGHTS_KEY,
            columns=_RATING_WEIGHTS_COLUMNS,
            bucket_column="rating_bucket",
            valid_buckets=_RATING_BUCKETS,
            factory=lambda name, as_of, bucket, weight: ImportedRatingWeight(
                investment_name=name,
                as_of_date=as_of,
                rating_bucket=bucket,
                weight_pct=weight,
            ),
        )

    def extract_maturity_weights(
        self,
        upload_sheets: dict[str, dict[str, Any]],
    ) -> dict[str, list[ImportedMaturityWeight]]:
        """Parse the ``Maturity Weights`` tidy sheet into typed rows.

        ``maturity_bucket`` is validated against the six canonical
        buckets (unknown → row error, dropped); ``weight_pct`` must be a
        number in ``[0, 100]``.

        Args:
            upload_sheets: Same JSONB dict accepted by :meth:`extract`.

        Returns:
            Mapping from investment name → list of
            :class:`ImportedMaturityWeight`. Every name present in the
            ``Attributes`` columns is a key.

        Raises:
            ImportFormatError: As for :meth:`extract_bond_analytics`.
        """
        return self._extract_bucket_weights(
            upload_sheets=upload_sheets,
            sheet_key=_MATURITY_WEIGHTS_KEY,
            columns=_MATURITY_WEIGHTS_COLUMNS,
            bucket_column="maturity_bucket",
            valid_buckets=_MATURITY_BUCKETS,
            factory=lambda name, as_of, bucket, weight: ImportedMaturityWeight(
                investment_name=name,
                as_of_date=as_of,
                maturity_bucket=bucket,
                weight_pct=weight,
            ),
        )

    def _extract_bucket_weights(
        self,
        *,
        upload_sheets: dict[str, dict[str, Any]],
        sheet_key: str,
        columns: tuple[str, ...],
        bucket_column: str,
        valid_buckets: frozenset[str],
        factory,
    ) -> dict[str, list]:
        """Shared body for the rating / maturity bucket-weight sheets.

        The two ladders differ only in their bucket column name and
        taxonomy; everything else (name / date resolution, bucket
        validation, weight range guard, partial-success error
        collection) is identical.
        """
        df = _attributes_dataframe_from_sheets(upload_sheets)
        investment_names: list[str] = list(df.columns)
        out: dict[str, list] = {name: [] for name in investment_names}
        for inv_name, as_of, vals in self._iter_tidy_reference_rows(
            upload_sheets=upload_sheets,
            sheet_key=sheet_key,
            columns=columns,
            investment_names=investment_names,
        ):
            raw_bucket = vals[bucket_column]
            bucket = "" if _is_blank(raw_bucket) else str(raw_bucket).strip()
            if bucket not in valid_buckets:
                self._errors.append(
                    ImportRowError(
                        investment_name=inv_name,
                        sheet=sheet_key,
                        row_index=as_of.isoformat(),
                        column=bucket_column,
                        message=(
                            f"Unknown {bucket_column} {raw_bucket!r}; expected "
                            f"one of {sorted(valid_buckets)}. Row dropped."
                        ),
                    )
                )
                continue
            weight = _coerce_decimal(vals["weight_pct"])
            if weight is None or weight < _WEIGHT_PCT_MIN or weight > _WEIGHT_PCT_MAX:
                self._errors.append(
                    ImportRowError(
                        investment_name=inv_name,
                        sheet=sheet_key,
                        row_index=as_of.isoformat(),
                        column="weight_pct",
                        message=(
                            f"weight_pct {vals['weight_pct']!r} is missing or "
                            "outside [0, 100]. Row dropped."
                        ),
                    )
                )
                continue
            out[inv_name].append(factory(inv_name, as_of, bucket, weight))
        return out

    # ------------------------------------------------------------------
    # Per-investment extraction
    # ------------------------------------------------------------------

    def _extract_one_investment(
        self,
        *,
        col_idx: int,
        name: str,
        investment_names: list[str],
        attr_table: dict[str, list[Any]],
        upload_sheets: dict[str, dict[str, Any]],
        valid_anlv_codes: frozenset[str] | None = None,
    ) -> ImportedInvestment | None:
        """Build one :class:`ImportedInvestment` or attach an error.

        Returns ``None`` (and pushes an :class:`ImportRowError`) when
        extraction must be aborted for this investment specifically;
        the rest of the import continues.
        """
        # 1) Investment type — synthetic row built from Excel row 2.
        type_row = attr_table.get("investment_type", [])
        raw_type: Any = type_row[col_idx] if col_idx < len(type_row) else None
        canonical_type = _normalise_investment_type(raw_type)
        if canonical_type is None:
            self._errors.append(
                ImportRowError(
                    investment_name=name,
                    sheet=_ATTRIBUTES_KEY,
                    row_index="Investment Type",
                    column=name,
                    message=(
                        f"Unknown or empty Investment Type {raw_type!r}. "
                        "Expected one of "
                        f"{sorted(_VALID_INVESTMENT_TYPES)} (or a known "
                        "alias such as 'Aktien', 'Anleihen', 'Private "
                        "Equity', 'Immobilien', 'Infrastruktur', 'Cash', "
                        "'Money Market')."
                    ),
                )
            )
            return None

        # 2) Asset-class code — fall back to "unclassified" if absent.
        asset_class_code = self._lookup_attr(attr_table, _ASSET_CLASS_ATTR_KEYS, col_idx)
        if asset_class_code is None or not str(asset_class_code).strip():
            asset_class_code_value = _UNCLASSIFIED_CODE
        else:
            asset_class_code_value = str(asset_class_code).strip()

        # 3) Manager / region / vintage / currency.
        manager = self._lookup_attr(attr_table, _MANAGER_ATTR_KEYS, col_idx)
        region = self._lookup_attr(attr_table, _REGION_ATTR_KEYS, col_idx)
        raw_vintage = self._lookup_attr(attr_table, _VINTAGE_ATTR_KEYS, col_idx)
        vintage_year = _coerce_int_year(raw_vintage)
        if raw_vintage is not None and not _is_blank(raw_vintage) and vintage_year is None:
            self._errors.append(
                ImportRowError(
                    investment_name=name,
                    sheet=_ATTRIBUTES_KEY,
                    row_index="Vintage Year",
                    column=name,
                    message=(
                        f"Vintage Year {raw_vintage!r} could not be "
                        "parsed as an integer year — leaving NULL."
                    ),
                )
            )

        raw_currency = self._lookup_attr(attr_table, _CURRENCY_ATTR_KEYS, col_idx)
        currency = _DEFAULT_CURRENCY
        if isinstance(raw_currency, str) and raw_currency.strip():
            currency_candidate = raw_currency.strip().upper()
            if len(currency_candidate) == 3 and currency_candidate.isalpha():
                currency = currency_candidate
            else:
                self._errors.append(
                    ImportRowError(
                        investment_name=name,
                        sheet=_ATTRIBUTES_KEY,
                        row_index="Währung",
                        column=name,
                        message=(
                            f"Currency {raw_currency!r} is not a 3-letter "
                            f"ISO 4217 code; defaulting to {currency!r}."
                        ),
                    )
                )

        # 3b) AnlV code — optional regulatory classification (ADR-0057).
        raw_anlv = self._lookup_attr(attr_table, _ANLV_ATTR_KEYS, col_idx)
        anlv_code: str | None = None
        if not _is_blank(raw_anlv):
            try:
                normalised = _normalise_anlv_code(raw_anlv)
            except ValueError as exc:
                self._errors.append(
                    ImportRowError(
                        investment_name=name,
                        sheet=_ATTRIBUTES_KEY,
                        row_index="AnlV",
                        column=name,
                        message=str(AnlVCodeUnknown(str(exc))),
                    )
                )
                normalised = None
            if normalised is not None:
                if valid_anlv_codes is not None and normalised not in valid_anlv_codes:
                    self._errors.append(
                        ImportRowError(
                            investment_name=name,
                            sheet=_ATTRIBUTES_KEY,
                            row_index="AnlV",
                            column=name,
                            message=(
                                f"AnlV code {normalised!r} (from cell "
                                f"{raw_anlv!r}) is not present in the "
                                "anlv_categories catalogue. Extend the "
                                "fixture and re-run migrations, or correct "
                                "the Excel cell."
                            ),
                        )
                    )
                else:
                    anlv_code = normalised

        # 4) NAVs.
        navs = self._extract_navs_for_investment(
            name=name,
            col_idx=col_idx,
            currency=currency,
            upload_sheets=upload_sheets,
        )

        # 4b) Cash statements (ADR-0103 §3) — and the precedence they carry.
        #     The Cash sheet is the *book of record* for a cash position's
        #     balances, so when it carries a column for this investment, any
        #     NAV column for the same investment is skipped: one book of
        #     record per investment, never two. The warning fires only when
        #     NAV rows were actually dropped — a v32 workbook whose cash
        #     column has (correctly) left the NAV sheets produces none.
        #
        #     When the Cash sheet is absent the v31 path is byte-identical:
        #     the series is empty and the cash NAV column imports as ordinary
        #     'excel' NAV rows. Both worlds coexist for the strand's duration.
        cash_statements = self._extract_cash_statements(
            name=name,
            col_idx=col_idx,
            canonical_type=canonical_type,
            upload_sheets=upload_sheets,
        )
        if cash_statements and navs:
            self._warnings.append(
                ExtractionWarning(
                    investment_name=name,
                    field="navs",
                    raw_value=f"{len(navs)} NAV row(s)",
                    action="skipped_cash_sheet_precedence",
                    message=(
                        f"{name!r} has both a Cash-sheet column and NAV-sheet "
                        "values; the Cash sheet is the book of record for cash "
                        "balances (ADR-0103 §3), so the NAV column is ignored. "
                        "Remove the NAV column to silence this warning."
                    ),
                )
            )
            navs = []

        # 5) Cashflows (private-markets calls / distributions, or — on a
        #    cash column — investor flows, ADR-0103 §5) plus the
        #    liquid-archetype income flows (ADR-0081). Income rides the
        #    same ``imp.cashflows`` collection so the service's existing
        #    delete-by-investment + create write path persists it with
        #    no extra wiring.
        cashflows = list(
            self._extract_cashflows_for_investment(
                name=name,
                col_idx=col_idx,
                currency=currency,
                canonical_type=canonical_type,
                upload_sheets=upload_sheets,
            )
        )
        cashflows.extend(
            self._extract_income_for_investment(
                name=name,
                col_idx=col_idx,
                currency=currency,
                canonical_type=canonical_type,
                upload_sheets=upload_sheets,
            )
        )

        manager_clean: str | None = (
            manager.strip() if isinstance(manager, str) and manager.strip() else None
        )
        region_clean: str | None = (
            region.strip() if isinstance(region, str) and region.strip() else None
        )

        # 6) Security identifiers — optional ISIN / Ticker rows (ADR-0090).
        identifiers = self._extract_identifiers(attr_table=attr_table, col_idx=col_idx)

        # 7) Unitised opening — optional Units / Units As Of rows (ADR-0097 §7).
        #    A cash position with a statement series derives its opening from
        #    that series instead (ADR-0103 §4 step 1), so the Units path is
        #    skipped for it: one book of record per investment. A stray Units
        #    cell on such a column is ignored with a warning rather than
        #    silently competing with the statement-derived opening.
        units: Decimal | None = None
        units_as_of: _date | None = None
        if cash_statements:
            if not _is_blank(self._lookup_attr(attr_table, _UNITS_ATTR_KEYS, col_idx)):
                self._warnings.append(
                    ExtractionWarning(
                        investment_name=name,
                        field="units",
                        raw_value=str(self._lookup_attr(attr_table, _UNITS_ATTR_KEYS, col_idx)),
                        action="skipped_cash_sheet_precedence",
                        message=(
                            f"{name!r} carries both a 'Units' row and a "
                            "Cash-sheet column; the statement series is the "
                            "book of record for a cash position's opening "
                            "(ADR-0103 §4), so the Units row is ignored. "
                            "Remove it to silence this warning."
                        ),
                    )
                )
        else:
            units, units_as_of = self._extract_units(
                name=name, attr_table=attr_table, col_idx=col_idx, navs=navs
            )

        return ImportedInvestment(
            name=name,
            investment_type=canonical_type,
            asset_class_code=asset_class_code_value,
            manager_name=manager_clean,
            region=region_clean,
            currency=currency,
            vintage_year=vintage_year,
            commitment_amount=None,
            anlv_code=anlv_code,
            navs=tuple(navs),
            cashflows=tuple(cashflows),
            identifiers=identifiers,
            units=units,
            units_as_of=units_as_of,
            cash_statements=cash_statements,
        )

    def _extract_identifiers(
        self,
        *,
        attr_table: dict[str, list[Any]],
        col_idx: int,
    ) -> tuple[ImportedIdentifier, ...]:
        """Parse the optional ISIN / Ticker rows for one investment.

        Per ADR-0090 the extractor is permissive: a blank cell yields
        no identifier (the correct illiquid-instrument state, never an
        error and never a warning); a non-blank cell is coerced to
        ``str`` and trimmed, and an empty-after-trim value yields no
        row. No checksum or format validation is imposed — the
        repository owns normalisation (upper-casing) and the DB owns
        non-emptiness.

        Args:
            attr_table: The per-label attribute rows built by
                :meth:`extract`.
            col_idx: The investment column position.

        Returns:
            A (possibly empty) tuple of :class:`ImportedIdentifier`, in
            ``isin``-then-``ticker`` order.
        """
        identifiers: list[ImportedIdentifier] = []
        for scheme, keys in (
            ("isin", _ISIN_ATTR_KEYS),
            ("ticker", _TICKER_ATTR_KEYS),
        ):
            raw = self._lookup_attr(attr_table, keys, col_idx)
            if _is_blank(raw):
                continue
            value = str(raw).strip()
            if not value:
                continue
            identifiers.append(ImportedIdentifier(scheme=scheme, value=value))
        return tuple(identifiers)

    def _extract_units(
        self,
        *,
        name: str,
        attr_table: dict[str, list[Any]],
        col_idx: int,
        navs: list[ImportedNav],
    ) -> tuple[Decimal | None, _date | None]:
        """Parse the optional ``Units`` / ``Units As Of`` rows (ADR-0097 §7).

        A present ``Units`` row is synthesised downstream into the single
        ``excel``-origin ``opening`` transaction. This method resolves the
        pair and validates it, raising row-level :class:`ImportRowError`s
        (which surface to the operator without dropping the investment's
        NAV/cashflow data). The rules:

        - No ``Units`` cell → ``(None, None)``; the common case, no error.
          A stray ``Units As Of`` without a count is flagged.
        - ``Units`` non-numeric or ``<= 0`` → error, no opening.
        - ``Units As Of`` present but unparseable → error, no opening.
        - ``Units As Of`` absent → default to the earliest **actual** NAV
          date; if the investment has no actual NAV to anchor it → error.
        - ``Units As Of`` after the latest actual NAV date → error (the
          units date must lie within or before the NAV series).

        Args:
            name: Investment name, for error attribution.
            attr_table: The per-label attribute rows built by
                :meth:`extract`.
            col_idx: The investment column position.
            navs: The investment's already-extracted NAV rows; the actual
                subset anchors and bounds the units-as-of date.

        Returns:
            ``(units, units_as_of)`` when a valid units row is present, else
            ``(None, None)``. When non-``None``, ``units_as_of`` is always
            concrete (explicit or defaulted).
        """
        raw_units = self._lookup_attr(attr_table, _UNITS_ATTR_KEYS, col_idx)
        raw_as_of = self._lookup_attr(attr_table, _UNITS_AS_OF_ATTR_KEYS, col_idx)

        if _is_blank(raw_units):
            if not _is_blank(raw_as_of):
                self._errors.append(
                    ImportRowError(
                        investment_name=name,
                        sheet=_ATTRIBUTES_KEY,
                        row_index="Units As Of",
                        column=name,
                        message=(
                            "'Units As Of' is set but 'Units' is empty; a "
                            "units-as-of date has no meaning without a unit "
                            "count."
                        ),
                    )
                )
            return (None, None)

        units = _coerce_decimal(raw_units)
        if units is None or units <= 0:
            self._errors.append(
                ImportRowError(
                    investment_name=name,
                    sheet=_ATTRIBUTES_KEY,
                    row_index="Units",
                    column=name,
                    message=(f"Invalid 'Units' value {raw_units!r}; expected a positive number."),
                )
            )
            return (None, None)

        actual_dates = [n.as_of_date for n in navs if n.nav_kind == "actual"]

        if not _is_blank(raw_as_of):
            units_as_of = _coerce_attr_date(raw_as_of)
            if units_as_of is None:
                self._errors.append(
                    ImportRowError(
                        investment_name=name,
                        sheet=_ATTRIBUTES_KEY,
                        row_index="Units As Of",
                        column=name,
                        message=(
                            f"Unparseable 'Units As Of' date {raw_as_of!r}; "
                            "expected a date (e.g. 2016-05-01)."
                        ),
                    )
                )
                return (None, None)
        elif actual_dates:
            units_as_of = min(actual_dates)
        else:
            self._errors.append(
                ImportRowError(
                    investment_name=name,
                    sheet=_ATTRIBUTES_KEY,
                    row_index="Units",
                    column=name,
                    message=(
                        "'Units' is set but the investment has no actual NAV "
                        "to anchor the opening date; add a 'Units As Of' date."
                    ),
                )
            )
            return (None, None)

        if actual_dates and units_as_of > max(actual_dates):
            self._errors.append(
                ImportRowError(
                    investment_name=name,
                    sheet=_ATTRIBUTES_KEY,
                    row_index="Units As Of",
                    column=name,
                    message=(
                        f"'Units As Of' {units_as_of.isoformat()} is after the "
                        f"latest actual NAV {max(actual_dates).isoformat()}; "
                        "the units date must lie within or before the NAV "
                        "series."
                    ),
                )
            )
            return (None, None)

        return (units, units_as_of)

    @staticmethod
    def _lookup_attr(
        attr_table: dict[str, list[Any]],
        keys: frozenset[str],
        col_idx: int,
    ) -> Any:
        """Return the first matching attribute value across alias keys.

        Returns ``None`` when none of the alias keys are present or
        the column slot is blank.
        """
        for key in keys:
            row = attr_table.get(key)
            if row is None:
                continue
            if col_idx >= len(row):
                continue
            value = row[col_idx]
            if not _is_blank(value):
                return value
        return None

    # ------------------------------------------------------------------
    # NAV extraction
    # ------------------------------------------------------------------

    def _extract_navs_for_investment(
        self,
        *,
        name: str,
        col_idx: int,
        currency: str,
        upload_sheets: dict[str, dict[str, Any]],
    ) -> list[ImportedNav]:
        navs: list[ImportedNav] = []
        for sheet_key, nav_kind in _NAV_SHEETS:
            payload = upload_sheets.get(sheet_key)
            if payload is None:
                continue
            for as_of, value in self._iter_timeseries_cells(
                payload=payload,
                sheet_key=sheet_key,
                name=name,
                col_idx=col_idx,
            ):
                navs.append(
                    ImportedNav(
                        as_of_date=as_of,
                        nav_value=value,
                        currency=currency,
                        nav_kind=nav_kind,
                    )
                )
        return navs

    # ------------------------------------------------------------------
    # Cash statement extraction (ADR-0103 §3)
    # ------------------------------------------------------------------

    def _extract_cash_statements(
        self,
        *,
        name: str,
        col_idx: int,
        canonical_type: str,
        upload_sheets: dict[str, dict[str, Any]],
    ) -> tuple[ImportedCashStatement, ...]:
        """Extract one cash position's statement series from the Cash sheet.

        The ``Cash`` sheet is the book of record for cash balances
        (ADR-0103 §3): each cell is the custodian's reported **level** on
        that statement date, in the position's own currency. This method
        parses one column of it.

        Cell semantics, all riding the shared time-series iterator:

        - **Blank** → *no statement* on that date. Skipped silently; the
          series simply has no entry there, and consumers see the previous
          balance carried forward (ADR-0060).
        - **Explicit ``0``** → a statement, and a legal one: the account
          was emptied. Distinct from blank, and never dropped (unlike the
          cash-flow sheets, where a literal zero is an ambiguous non-event).
        - **Negative** → an :class:`ImportRowError`, and the cell is
          dropped. An *actual* balance cannot be negative (ADR-0100 §5,
          carried forward by ADR-0103 §4); negative values are plan-world
          semantics, where they signal a funding need. Dropping the cell
          simply removes that date from the statement series: the next
          delta spans to the following valid statement, and because the
          sheet carries **levels** rather than flows, the ledger the
          service derives stays self-correcting — no balance is left
          misstated by the omission.

        **Type guard.** The sheet is cash-only by definition, so a column
        whose Attributes-resolved type is not ``'cash'`` yields one
        :class:`ImportRowError` per populated cell and an empty series. The
        converse is fine and is the whole v31 world: a cash investment with
        no ``Cash``-sheet column keeps its NAV-column representation.

        Args:
            name: Investment name, for error attribution.
            col_idx: The investment column position.
            canonical_type: The Attributes-resolved investment type.
            upload_sheets: Same JSONB dict accepted by :meth:`extract`.

        Returns:
            The statement series ordered by date, or an empty tuple when
            the sheet is absent, the column is empty, or the column is not
            a cash position. On a duplicated statement date the last cell
            in sheet order wins — the same last-write-wins convention the
            NAV natural key applies.
        """
        payload = upload_sheets.get(_CASH_KEY)
        if payload is None:
            return ()

        cells = list(
            self._iter_timeseries_cells(
                payload=payload,
                sheet_key=_CASH_KEY,
                name=name,
                col_idx=col_idx,
            )
        )
        if not cells:
            return ()

        if canonical_type != "cash":
            for as_of, _value in cells:
                self._errors.append(
                    ImportRowError(
                        investment_name=name,
                        sheet=_CASH_KEY,
                        row_index=as_of.isoformat(),
                        column=name,
                        message=(
                            f"The Cash sheet carries a balance for {name!r}, "
                            f"whose investment type is {canonical_type!r}, not "
                            "'cash'. The Cash sheet is the book of record for "
                            "cash positions only — move this column to the "
                            "'NAVs actual' sheet, or correct the investment "
                            "type in the Attributes sheet."
                        ),
                    )
                )
            return ()

        by_date: dict[_date, Decimal] = {}
        for as_of, value in cells:
            if value < 0:
                self._errors.append(
                    ImportRowError(
                        investment_name=name,
                        sheet=_CASH_KEY,
                        row_index=as_of.isoformat(),
                        column=name,
                        message=(
                            f"Cash sheet contains a negative balance ({value}); "
                            "an actual cash balance cannot be negative "
                            "(ADR-0103 §4). The statement date is dropped — the "
                            "next balance change spans to the following "
                            "statement. Correct the cell, or book the overdraft "
                            "as a plan balance once the plan path exists."
                        ),
                    )
                )
                continue
            by_date[as_of] = value

        return tuple(
            ImportedCashStatement(statement_date=as_of, balance=by_date[as_of])
            for as_of in sorted(by_date)
        )

    def _extract_cashflows_for_investment(
        self,
        *,
        name: str,
        col_idx: int,
        currency: str,
        canonical_type: str,
        upload_sheets: dict[str, dict[str, Any]],
    ) -> list[ImportedCashflow]:
        """Extract the four Cash Flow In/Out sheets for one investment.

        The ``flow_type`` is the sheet's fixed one (``distribution`` for
        In, ``capital_call`` for Out) for every investment type but
        ``cash``, whose columns carry ``investor_flow`` rows instead
        (ADR-0103 §5, via :data:`_CASHFLOW_TYPE_OVERRIDE_BY_TYPE`) — the
        same derive-from-resolved-type idiom as
        :meth:`_extract_income_for_investment` (ADR-0081 §1). Everything
        else is common to both: the strict sign guard, the zero-cell drop,
        and the 12:00-UTC timestamp.
        """
        override = _CASHFLOW_TYPE_OVERRIDE_BY_TYPE.get(canonical_type)
        # The sign guards below name the flow the operator should have
        # written instead. On a cash column that flow is an investor flow —
        # a contribution or a withdrawal — never a call or a distribution,
        # which a cash position does not make (ADR-0103 §5). Only the
        # corrective noun changes; the guard mechanics are identical.
        wrong_in, wrong_out = (
            ("withdrawals", "contributions")
            if override == "investor_flow"
            else ("calls", "distributions")
        )

        cashflows: list[ImportedCashflow] = []
        for (
            sheet_key,
            sheet_flow_type,
            flow_kind,
            expected_sign,
        ) in _CASHFLOW_SHEETS:
            flow_type = override if override is not None else sheet_flow_type
            payload = upload_sheets.get(sheet_key)
            if payload is None:
                continue
            for as_of, value in self._iter_timeseries_cells(
                payload=payload,
                sheet_key=sheet_key,
                name=name,
                col_idx=col_idx,
            ):
                # Strict sign validation per ADR-0043 §3: the
                # extractor refuses sign coercion. Drop sign-violating
                # rows with a row-level error; downstream code can
                # then trust the convention without re-checking.
                if expected_sign == 1 and value < 0:
                    self._errors.append(
                        ImportRowError(
                            investment_name=name,
                            sheet=sheet_key,
                            row_index=as_of.isoformat(),
                            column=name,
                            message=(
                                "Cash Flow In sheet contains a negative "
                                f"value ({value}); expected positive — "
                                f"{wrong_in} should live in the corresponding "
                                "Cash Flow Out sheet."
                            ),
                        )
                    )
                    continue
                if expected_sign == -1 and value > 0:
                    self._errors.append(
                        ImportRowError(
                            investment_name=name,
                            sheet=sheet_key,
                            row_index=as_of.isoformat(),
                            column=name,
                            message=(
                                "Cash Flow Out sheet contains a positive "
                                f"value ({value}); expected negative — "
                                f"{wrong_out} should live in the "
                                "corresponding Cash Flow In sheet."
                            ),
                        )
                    )
                    continue
                if value == 0:
                    # Ambiguous: a literal zero in a cashflow sheet
                    # is dropped rather than fabricated as a typed
                    # zero-call / zero-distribution row.
                    continue

                flow_timestamp = datetime.combine(as_of, time(12, 0), tzinfo=timezone.utc)
                cashflows.append(
                    ImportedCashflow(
                        flow_timestamp=flow_timestamp,
                        flow_type=flow_type,
                        flow_kind=flow_kind,
                        amount=value,
                        currency=currency,
                    )
                )
        return cashflows

    def _extract_income_for_investment(
        self,
        *,
        name: str,
        col_idx: int,
        currency: str,
        canonical_type: str,
        upload_sheets: dict[str, dict[str, Any]],
    ) -> list[ImportedCashflow]:
        """Extract listed-instrument income flows for one investment.

        Per ADR-0081 §1 the income kind is derived from the resolved
        investment type: ``listed_equity`` → ``dividend``,
        ``listed_bonds`` → ``coupon`` (Cash is mapped to ``listed_bonds``
        via the alias fix and therefore yields ``coupon``). Any other
        type produces **no income** — a silent skip, not an error.

        Income is a positive inflow (the IRR / return engines partition
        by sign, so income enters naturally as an inflow). A non-positive
        cell is an :class:`ImportRowError` and is dropped, mirroring the
        ``_CASHFLOW_SHEETS`` expected-sign guard. Blank cells are skipped
        silently by the shared time-series iterator.
        """
        flow_type = _INCOME_FLOW_TYPE_BY_TYPE.get(canonical_type)
        if flow_type is None:
            return []

        income: list[ImportedCashflow] = []
        for sheet_key, flow_kind in _INCOME_SHEETS:
            payload = upload_sheets.get(sheet_key)
            if payload is None:
                continue
            for as_of, value in self._iter_timeseries_cells(
                payload=payload,
                sheet_key=sheet_key,
                name=name,
                col_idx=col_idx,
            ):
                if value <= 0:
                    self._errors.append(
                        ImportRowError(
                            investment_name=name,
                            sheet=sheet_key,
                            row_index=as_of.isoformat(),
                            column=name,
                            message=(
                                "Income sheet contains a non-positive "
                                f"value ({value}); income is an inflow and "
                                "must be positive."
                            ),
                        )
                    )
                    continue
                flow_timestamp = datetime.combine(as_of, time(12, 0), tzinfo=timezone.utc)
                income.append(
                    ImportedCashflow(
                        flow_timestamp=flow_timestamp,
                        flow_type=flow_type,
                        flow_kind=flow_kind,
                        amount=value,
                        currency=currency,
                    )
                )
        return income

    # ------------------------------------------------------------------
    # Single time-series iterator shared by NAV and cashflow paths
    # ------------------------------------------------------------------

    def _iter_timeseries_cells(
        self,
        *,
        payload: dict[str, Any],
        sheet_key: str,
        name: str,
        col_idx: int,
    ):
        """Yield ``(as_of_date, decimal_value)`` for non-blank cells.

        Skips blank cells silently. Records a row-level
        :class:`ImportRowError` for unparseable dates / numerics and
        skips the offending row. The caller decides how to wrap the
        yielded values into typed dataclasses.
        """
        split = _validate_split_payload(sheet_key, payload)
        index = list(split["index"])
        rows = list(split["data"])

        for row_idx, raw_index in enumerate(index):
            if row_idx >= len(rows):
                break
            row = rows[row_idx]
            if col_idx >= len(row):
                continue
            cell_value = row[col_idx]
            if _is_blank(cell_value):
                continue

            as_of = _parse_iso_date(raw_index)
            if as_of is None:
                self._errors.append(
                    ImportRowError(
                        investment_name=name,
                        sheet=sheet_key,
                        row_index=str(raw_index),
                        column=name,
                        message=(f"Could not parse {raw_index!r} as an ISO date."),
                    )
                )
                continue

            value = _coerce_decimal(cell_value)
            if value is None:
                self._errors.append(
                    ImportRowError(
                        investment_name=name,
                        sheet=sheet_key,
                        row_index=str(raw_index),
                        column=name,
                        message=(f"Could not parse {cell_value!r} as a numeric value."),
                    )
                )
                continue

            yield as_of, value


# ---------------------------------------------------------------------------
# Benchmarks & Attribution — module-level extractor (ADR-0061)
# ---------------------------------------------------------------------------


def extract_benchmarks_from_snapshot(
    snapshot: dict[str, Any],
) -> tuple[
    list[ImportedBenchmark],
    list[ImportedBenchmarkObservation],
    list[ImportedBenchmarkMapping],
    list[ImportRowError],
]:
    """Extract benchmark catalogue, observations, and mappings from a snapshot.

    Pure transformation per ADR-0061 §Decision. FK resolution from
    codes to UUIDs happens at the service layer; this function only
    deals with row shape and value coercion.

    Args:
        snapshot: The JSONB-shaped workbook snapshot keyed by canonical
            snake_case sheet name. Relevant keys:

            * ``"benchmarks_actual"`` — daily ``DataFrame.to_json(
              orient="split")`` payload. Columns are benchmark codes
              (row 1 of the Excel sheet); index is ISO date strings;
              data is per-date period returns.
            * ``"benchmark_mapping"`` — four-column DataFrame
              ``(asset_class, benchmark_id, weight, comment)``
              produced by :func:`modules.front_office.data_import
              ._parse_benchmark_mapping_sheet`.

    Returns:
        Four-tuple:

        * list of :class:`ImportedBenchmark` (one per data column in
          ``Benchmarks actual``)
        * list of :class:`ImportedBenchmarkObservation` (one per
          non-null ``(column, row)`` pair)
        * list of :class:`ImportedBenchmarkMapping` (one per row in
          ``Benchmark Mapping``)
        * list of :class:`ImportRowError` — collected, not raised.

    Raises:
        ImportFormatError: When the two sheets are structurally
            inconsistent — specifically when ``benchmark_mapping``
            references a ``benchmark_id`` that is not a column of
            ``benchmarks_actual``, or when ``benchmark_mapping``
            is present without ``benchmarks_actual``. Asset-class
            code mismatches are NOT raised here — they require DB
            lookup and live at the service layer.

    Behavioural contract:

    * If neither sheet is present: returns four empty lists. Benign.
    * If ``benchmarks_actual`` is present but ``benchmark_mapping``
      is not: returns benchmarks and observations, empty mappings,
      and an :class:`ImportRowError` warning that the mapping sheet
      is missing.
    * If ``benchmark_mapping`` is present but ``benchmarks_actual``
      is not: raises :class:`ImportFormatError`.
    * Empty ``benchmark_code`` in mapping rows with ``weight == 0``
      is valid (means "no benchmark for this asset class"; e.g.
      Cash). Such rows are included in the returned mapping list
      and the service layer interprets the combination as a
      deliberate non-mapping.
    * Non-numeric returns: :class:`ImportRowError` with row reference.
    * Duplicate ``(benchmark_code, as_of_date)``:
      :class:`ImportRowError`, last value wins.
    * Weight outside ``[0, 1]``: :class:`ImportRowError`, row dropped.
    """
    errors: list[ImportRowError] = []
    benchmarks_payload = snapshot.get(_BENCHMARKS_ACTUAL_KEY)
    mapping_payload = snapshot.get(_BENCHMARK_MAPPING_KEY)

    if benchmarks_payload is None and mapping_payload is None:
        return [], [], [], errors

    if benchmarks_payload is None and mapping_payload is not None:
        raise ImportFormatError(
            "Benchmark Mapping references benchmarks but 'Benchmarks actual' sheet is missing."
        )

    # benchmarks_actual is present.
    assert benchmarks_payload is not None  # for type narrowing
    bm_split = _validate_split_payload(_BENCHMARKS_ACTUAL_KEY, benchmarks_payload)
    benchmark_codes: list[str] = [str(c).strip() for c in bm_split["columns"]]
    benchmark_codes = [c for c in benchmark_codes if c]
    benchmarks: list[ImportedBenchmark] = [
        ImportedBenchmark(
            code=code,
            display_name=code,
            description=None,
            provider_hint=None,
        )
        for code in benchmark_codes
    ]

    # Build observations. Duplicate (code, date) pairs are tracked
    # so the second occurrence overwrites the first (last wins) and
    # is recorded as an ImportRowError per the behavioural contract.
    seen: dict[tuple[str, _date], Decimal] = {}
    duplicate_keys: set[tuple[str, _date]] = set()
    index = list(bm_split["index"])
    rows = list(bm_split["data"])
    n_cols = len(benchmark_codes)
    for row_idx, raw_index in enumerate(index):
        if row_idx >= len(rows):
            break
        as_of = _parse_iso_date(raw_index)
        if as_of is None:
            errors.append(
                ImportRowError(
                    investment_name=None,
                    sheet=_BENCHMARKS_ACTUAL_KEY,
                    row_index=str(raw_index),
                    column=None,
                    message=(f"Could not parse {raw_index!r} as an ISO date."),
                )
            )
            continue
        row = list(rows[row_idx])
        for col_idx, code in enumerate(benchmark_codes):
            if col_idx >= len(row) or col_idx >= n_cols:
                continue
            cell = row[col_idx]
            if _is_blank(cell):
                continue
            value = _coerce_decimal(cell)
            if value is None:
                errors.append(
                    ImportRowError(
                        investment_name=None,
                        sheet=_BENCHMARKS_ACTUAL_KEY,
                        row_index=as_of.isoformat(),
                        column=code,
                        message=(f"Could not parse {cell!r} as a numeric period return."),
                    )
                )
                continue
            key = (code, as_of)
            if key in seen:
                duplicate_keys.add(key)
            seen[key] = value
    for code, as_of in sorted(duplicate_keys):
        errors.append(
            ImportRowError(
                investment_name=None,
                sheet=_BENCHMARKS_ACTUAL_KEY,
                row_index=as_of.isoformat(),
                column=code,
                message=(
                    f"Duplicate ({code!r}, {as_of.isoformat()}) in "
                    "Benchmarks actual; last value retained."
                ),
            )
        )
    observations: list[ImportedBenchmarkObservation] = [
        ImportedBenchmarkObservation(
            benchmark_code=code,
            as_of_date=as_of,
            period_return=value,
        )
        for (code, as_of), value in sorted(seen.items())
    ]

    # Mapping sheet — optional. When absent, surface a warning but
    # continue the import.
    mappings: list[ImportedBenchmarkMapping] = []
    if mapping_payload is None:
        errors.append(
            ImportRowError(
                investment_name=None,
                sheet=_BENCHMARK_MAPPING_KEY,
                row_index=None,
                column=None,
                message=(
                    "Benchmark Mapping sheet missing — benchmarks "
                    "imported but not associated with asset classes."
                ),
            )
        )
        return benchmarks, observations, mappings, errors

    map_split = _validate_split_payload(_BENCHMARK_MAPPING_KEY, mapping_payload)
    map_columns = [str(c) for c in map_split["columns"]]
    try:
        ac_idx = map_columns.index("asset_class")
        bm_idx = map_columns.index("benchmark_id")
        w_idx = map_columns.index("weight")
    except ValueError as exc:
        raise ImportFormatError(
            f"Benchmark Mapping sheet is missing a required column: "
            f"{exc}. Expected at least 'asset_class', 'benchmark_id', "
            "and 'weight'."
        ) from exc

    benchmark_code_set: set[str] = set(benchmark_codes)
    map_rows = list(map_split["data"])
    map_index = list(map_split["index"])
    for row_idx, row in enumerate(map_rows):
        row_list = list(row)
        if len(row_list) < max(ac_idx, bm_idx, w_idx) + 1:
            errors.append(
                ImportRowError(
                    investment_name=None,
                    sheet=_BENCHMARK_MAPPING_KEY,
                    row_index=str(map_index[row_idx] if row_idx < len(map_index) else row_idx),
                    column=None,
                    message="Benchmark Mapping row is truncated; skipped.",
                )
            )
            continue

        ac_raw = row_list[ac_idx]
        bm_raw = row_list[bm_idx]
        w_raw = row_list[w_idx]

        if _is_blank(ac_raw):
            errors.append(
                ImportRowError(
                    investment_name=None,
                    sheet=_BENCHMARK_MAPPING_KEY,
                    row_index=str(map_index[row_idx] if row_idx < len(map_index) else row_idx),
                    column="asset_class",
                    message=("Benchmark Mapping row has no asset_class; skipped."),
                )
            )
            continue

        asset_class_code = str(ac_raw).strip()
        benchmark_code = "" if _is_blank(bm_raw) else str(bm_raw).strip()

        weight = _coerce_decimal(w_raw)
        if weight is None:
            # An empty weight is treated as zero (== "no mapping")
            # ONLY when the benchmark_code is also empty. Otherwise
            # it is a row-level error and the row is dropped.
            if benchmark_code == "":
                weight = Decimal("0")
            else:
                errors.append(
                    ImportRowError(
                        investment_name=None,
                        sheet=_BENCHMARK_MAPPING_KEY,
                        row_index=str(map_index[row_idx] if row_idx < len(map_index) else row_idx),
                        column="weight",
                        message=(
                            f"Weight {w_raw!r} for asset_class "
                            f"{asset_class_code!r} is not numeric; "
                            "row dropped."
                        ),
                    )
                )
                continue

        if weight < Decimal("0") or weight > Decimal("1"):
            errors.append(
                ImportRowError(
                    investment_name=None,
                    sheet=_BENCHMARK_MAPPING_KEY,
                    row_index=str(map_index[row_idx] if row_idx < len(map_index) else row_idx),
                    column="weight",
                    message=(
                        f"Weight {weight} for asset_class "
                        f"{asset_class_code!r} is outside [0, 1]; "
                        "row dropped."
                    ),
                )
            )
            continue

        # Empty benchmark code is only valid when weight == 0.
        if benchmark_code == "":
            if weight != Decimal("0"):
                errors.append(
                    ImportRowError(
                        investment_name=None,
                        sheet=_BENCHMARK_MAPPING_KEY,
                        row_index=str(map_index[row_idx] if row_idx < len(map_index) else row_idx),
                        column="benchmark_id",
                        message=(
                            f"Benchmark Mapping row for asset_class "
                            f"{asset_class_code!r} has empty "
                            "benchmark_id but non-zero weight "
                            f"{weight}; row dropped."
                        ),
                    )
                )
                continue
        else:
            if benchmark_code not in benchmark_code_set:
                # Hard fail: mapping references an unknown benchmark.
                raise ImportFormatError(
                    f"Benchmark {benchmark_code!r} in Benchmark "
                    "Mapping is not defined in the 'Benchmarks "
                    "actual' sheet. Check spelling or add a column "
                    "in the Benchmarks actual sheet."
                )

        mappings.append(
            ImportedBenchmarkMapping(
                asset_class_code=asset_class_code,
                benchmark_code=benchmark_code,
                weight=weight,
            )
        )

    return benchmarks, observations, mappings, errors


# ---------------------------------------------------------------------------
# FX-rate extraction (ADR-0099 §5)
# ---------------------------------------------------------------------------


#: A valid ``FX rates`` column header — two three-letter uppercase ISO 4217
#: codes joined by a slash, e.g. ``USD/EUR``. The left (base) group is the
#: priced currency; the right (quote) group is the reference currency.
_FX_PAIR_RE = re.compile(r"^([A-Z]{3})/([A-Z]{3})$")


def extract_fx_rates_from_snapshot(
    snapshot: dict[str, Any],
) -> tuple[list[ImportedFxRate], list[ImportRowError]]:
    """Extract FX-rate observations from an Excel workbook snapshot.

    Pure transformation per ADR-0099 §5. The ``FX rates`` sheet is a
    wide-format market-reference sheet on the ``Benchmarks actual``
    idiom (ADR-0061): column A holds dates, each remaining column is one
    rate series whose header uses pair notation ``XXX/YYY``. The value
    is the price of one unit of the base currency ``XXX`` expressed in
    the quote currency ``YYY``, so a ``USD/EUR`` column lands as
    ``currency='USD'``, ``reference_currency='EUR'`` — the
    ``rate_to_reference`` convention of ADR-0099 §2 exactly. The quote
    side declares the reference currency of the dataset, which is why
    the sheet is self-describing and needs no configuration cell.

    Args:
        snapshot: The JSONB-shaped workbook snapshot keyed by canonical
            snake_case sheet name. The only relevant key is
            ``"fx_rates"`` — a daily ``DataFrame.to_json(orient="split")``
            payload whose columns are ``XXX/YYY`` pair headers, whose
            index is ISO date strings, and whose data are the rates.

    Returns:
        Two-tuple ``(rates, errors)``:

        * ``rates`` — one :class:`ImportedFxRate` per non-blank,
          strictly-positive ``(column, row)`` cell.
        * ``errors`` — collected :class:`ImportRowError` records (never
          raised) for cell-level problems.

    Raises:
        ValidationError: On an operator-actionable **header** problem —
            a header that does not parse as ``XXX/YYY``, a sheet that
            mixes more than one quote (reference) currency, or an
            identity pair (``EUR/EUR``, whose rate is always 1 and which
            the ``ck_fx_rates_currency_not_reference`` CHECK forbids).
            Header problems are structural, not row-level, and fail the
            whole sheet loudly rather than being deferred to the DB.
        ImportFormatError: On a structurally invalid JSONB payload
            (raised by :func:`_validate_split_payload`).

    Behavioural contract:

    * No ``fx_rates`` key → ``([], [])``. The sheet is optional; its
      absence is silent (not even a warning), so a workbook without it
      imports byte-identically.
    * Blank cells are simply absent observations (the series is sparse;
      the Block-1 carry-forward absorbs gaps) — never an error.
    * Non-numeric cell → :class:`ImportRowError`, cell dropped.
    * Cell ``<= 0`` → :class:`ImportRowError`, cell dropped (the
      ``ck_fx_rates_rate_positive`` CHECK would refuse it).

    Note:
        Dates arrive already normalised from the standard
        market-reference parser; :func:`_parse_iso_date` only decodes the
        ISO string the upload serialisation persists.
    """
    errors: list[ImportRowError] = []
    payload = snapshot.get(_FX_RATES_KEY)
    if payload is None:
        return [], errors

    split = _validate_split_payload(_FX_RATES_KEY, payload)

    # --- Header validation (hard failure, operator-actionable) ---------
    # The market-reference parser discovers a contiguous, non-empty
    # column span from row 1, so ``headers`` is parallel to every data
    # row and carries no blanks in practice. Iterate it strictly.
    headers = [str(c).strip() for c in split["columns"]]
    if not headers:
        return [], errors

    column_currencies: list[str] = []
    reference_currency: str | None = None
    for header in headers:
        match = _FX_PAIR_RE.match(header)
        if match is None:
            raise ValidationError(
                f"FX rates column header {header!r} is not a valid "
                "currency pair. Expected 'XXX/YYY' with two three-letter "
                "uppercase ISO 4217 codes (e.g. 'USD/EUR')."
            )
        base, quote = match.group(1), match.group(2)
        if base == quote:
            raise ValidationError(
                f"FX rates column header {header!r} is an identity pair. "
                "The reference currency's own rate is always 1 and must "
                "not appear in the sheet."
            )
        if reference_currency is None:
            reference_currency = quote
        elif quote != reference_currency:
            raise ValidationError(
                f"FX rates sheet mixes reference currencies: column "
                f"{header!r} quotes against {quote!r} but an earlier "
                f"column quotes against {reference_currency!r}. Every "
                "column must share one reference currency."
            )
        column_currencies.append(base)

    assert reference_currency is not None  # headers non-empty ⇒ resolved

    # --- Cell validation (soft, partial-success) -----------------------
    index = list(split["index"])
    rows = list(split["data"])
    n_cols = len(headers)
    rates: list[ImportedFxRate] = []
    for row_idx, raw_index in enumerate(index):
        if row_idx >= len(rows):
            break
        as_of = _parse_iso_date(raw_index)
        if as_of is None:
            errors.append(
                ImportRowError(
                    investment_name=None,
                    sheet=_FX_RATES_KEY,
                    row_index=str(raw_index),
                    column=None,
                    message=f"Could not parse {raw_index!r} as an ISO date.",
                )
            )
            continue
        row = list(rows[row_idx])
        for col_idx, currency in enumerate(column_currencies):
            if col_idx >= len(row) or col_idx >= n_cols:
                continue
            cell = row[col_idx]
            if _is_blank(cell):
                # Sparse series: an absent observation, not an error.
                continue
            value = _coerce_decimal(cell)
            if value is None:
                errors.append(
                    ImportRowError(
                        investment_name=None,
                        sheet=_FX_RATES_KEY,
                        row_index=as_of.isoformat(),
                        column=headers[col_idx],
                        message=(f"Could not parse {cell!r} as a numeric FX rate."),
                    )
                )
                continue
            if value <= Decimal("0"):
                errors.append(
                    ImportRowError(
                        investment_name=None,
                        sheet=_FX_RATES_KEY,
                        row_index=as_of.isoformat(),
                        column=headers[col_idx],
                        message=(
                            f"FX rate {value} for {currency!r} is not "
                            "strictly positive; row dropped."
                        ),
                    )
                )
                continue
            rates.append(
                ImportedFxRate(
                    as_of_date=as_of,
                    currency=currency,
                    rate_to_reference=value,
                    reference_currency=reference_currency,
                )
            )

    return rates, errors
