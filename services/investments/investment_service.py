# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InvestmentService — workflow aggregator for the Investment domain.

Aggregates the three Phase-4 repositories (investments, NAVs,
cashflows) into coherent domain workflows. The web CRUD surface
(sub-stream 4b) and the Excel-import workflow (sub-stream 4c) both
consume this service so the cross-repository orchestration lives in
exactly one place.

Phase 4 implements the Investment-area-internal workflows. The
sub-stream 4c Excel-import path adds
:meth:`InvestmentService.transform_upload_to_investments`, which
orchestrates the three Investment-domain repositories *plus* the
Phase-2 :class:`DataUploadRepository` and the
Phase-3 :class:`AssetClassRepository` (the latter via the cross-
module API addition documented in ADR-0043 §4 —
:meth:`AssetClassRepository.get_by_code`, eager because Excel-import
is its concrete consumer).

Two documented method groups
----------------------------
- **Read workflows.** Routes and importers consume aggregate read
  DTOs; the service hides the multi-repository fan-out behind a
  single call.
- **Write workflows.** Atomic create / update / delete operations
  for investments, NAVs, and cashflows. The Excel-import path adds
  a dedicated bulk replace-by-investment workflow
  (:meth:`transform_upload_to_investments`) per ADR-0043 §3 B1.1.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date as _date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4

import pandas as pd
from sqlalchemy.exc import IntegrityError

from core.exceptions import (
    CurrencyMismatchError,
    InvestorFlowScopeError,
    LimitValidationError,
    MissingFxRateError,
    NonNegativeHoldingsError,
    ValidationError,
    ValuationModeError,
)
from core.models.investment_identifier import IDENTIFIER_SCHEMES
from core.repositories.anlv_category_repository import AnlVCategoryRepository
from core.repositories.asset_class_benchmark_mapping_repository import (
    AssetClassBenchmarkMappingRepository,
)
from core.repositories.asset_class_repository import (
    AssetClassDTO,
    AssetClassRepository,
)
from core.repositories.benchmark_observation_repository import (
    BenchmarkObservationRepository,
)
from core.repositories.benchmark_repository import (
    BenchmarkRepository,
)
from core.repositories.data_upload_repository import DataUploadRepository
from core.repositories.fx_rate_repository import FxRateRepository
from core.repositories.instrument_price_repository import (
    InstrumentPriceDTO,
    InstrumentPriceRepository,
)
from core.repositories.investment_bond_analytics_repository import (
    InvestmentBondAnalyticsRepository,
)
from core.repositories.investment_maturity_weights_repository import (
    InvestmentMaturityWeightsRepository,
)
from core.repositories.investment_rating_weights_repository import (
    InvestmentRatingWeightsRepository,
)
from core.repositories.limits_repository import LimitsRepository
from core.repositories.investment_cashflow_repository import (
    InvestmentCashflowDTO,
    InvestmentCashflowRepository,
)
from core.repositories.investment_identifier_repository import (
    InvestmentIdentifierDTO,
    InvestmentIdentifierRepository,
)
from core.repositories.investment_nav_repository import (
    InvestmentNavDTO,
    InvestmentNavRepository,
)
from core.repositories.investment_region_weights_repository import (
    InvestmentRegionWeightsRepository,
    RegionWeightInput,
)
from core.repositories.investment_repository import (
    InvestmentDTO,
    InvestmentRepository,
)
from core.repositories.investment_sector_weights_repository import (
    InvestmentSectorWeightsRepository,
    SectorWeightInput,
)
from core.repositories.position_transaction_repository import (
    PositionTransactionDTO,
    PositionTransactionRepository,
)
from core.repositories.region_repository import RegionRepository
from core.repositories.sector_repository import SectorRepository
from core.repositories.tenant_repository import TenantRepository
from services.analytics.investment_returns import (
    compute_net_capital_gain,
    compute_rolling_irr_since_inception,
    compute_rolling_multiples,
    compute_total_return_series,
)
from services.data_normalization import (
    ExtractionWarning,
    ImportedCashStatement,
    ImportedIdentifier,
    ImportedNav,
    ImportRowError,
    InvestmentExtractionResult,
    InvestmentExtractor,
    UploadNotFoundError,
    extract_benchmarks_from_snapshot,
    extract_fx_rates_from_snapshot,
)
from services.fx.functional_currency import (
    build_portfolio_fx_converter,
)
from services.investments.aum import build_nav_series, compute_aum
from services.investments.cash_plan_materialisation import (
    CashPlanMaterialisationService,
    CashPlanReport,
)
from services.investments.cashflow_dedup_key import compute_cashflow_dedup_key
from services.investments.holdings import (
    derive_holdings,
    first_negative_holding_date,
    holdings_as_of,
)
from services.investments.market_linked import MARKET_LINKED_TYPES
from services.investments.nav_materialisation import (
    NavMaterialisationReport,
    NavMaterialisationService,
)
from services.investments.unity_price import (
    UNITY_PRICE,
    unity_price_violation,
)
from services.investments.valuation_mode import (
    flip_precondition_error,
    shows_positions_panel,
)
from services.market_data.dto import (
    NormalizedQuote,
    NormalizedSeries,
    SeriesKind,
)

_LOG = logging.getLogger(__name__)
_UNCLASSIFIED_CODE: str = "unclassified"

#: The canonical snake_case key of the ``AUM`` sheet. Since ADR-0103 §3 the
#: sheet is a reconciliation *control* — read, compared, never persisted.
_AUM_SHEET_KEY: str = "aum"

#: The ``Numeric(20, 4)`` quantum of ``investment_navs.nav_value``
#: (:class:`core.models.investment_nav.InvestmentNav`) — the finest
#: difference the book can even represent. ADR-0103 §3 makes it the AUM
#: reconciliation's tolerance: a deviation at or below it is rounding in the
#: last stored digit, and anything above it is a real disagreement worth an
#: operator's attention. Pinned against the column the way
#: :data:`services.investments.unity_price.PRICE_SCALE` is pinned against
#: ``instrument_prices.price``.
_NAV_QUANTUM: Decimal = Decimal(1).scaleb(-4)

#: How many individual deviating dates the AUM control lists before it
#: switches to a single summary line. A daily AUM sheet against
#: statement-frequency NAVs can deviate on thousands of days, and an
#: uncapped control would drown the import result it is meant to annotate.
_MAX_AUM_FINDINGS: int = 20

_NON_CODE_CHARS = re.compile(r"[^a-z0-9]+")


def _normalise_asset_class_code(value: str) -> str:
    """Normalise an Excel asset-class string to a canonical lowercase snake-code.

    Examples:
        >>> _normalise_asset_class_code("Equities")
        'equities'
        >>> _normalise_asset_class_code("Private Equity")
        'private_equity'
        >>> _normalise_asset_class_code("Gov Bonds (DM)")
        'gov_bonds_dm'

    The function is idempotent: applying it to its own output is a
    no-op for any valid result.
    """
    stripped = value.strip().lower()
    collapsed = _NON_CODE_CHARS.sub("_", stripped)
    return collapsed.strip("_")


def _normalise_identifier_value(value: str) -> str:
    """Trim + upper-case an identifier value for reconciliation compare.

    Mirrors the normalisation :class:`InvestmentIdentifierRepository`
    applies on write (ADR-0090 §Decision) so a workbook value matches
    the stored row. The extractor guarantees only non-empty values
    reach here, so — unlike the repository — no empty-value guard is
    needed.

    Args:
        value: The raw identifier value from an ``ImportedIdentifier``.

    Returns:
        The trimmed, upper-cased value.
    """
    return value.strip().upper()


@dataclass(frozen=True)
class InvestmentDetailDTO:
    """Aggregate read shape: investment + NAV history + cashflow history."""

    investment: InvestmentDTO
    navs: list[InvestmentNavDTO]
    cashflows: list[InvestmentCashflowDTO]


@dataclass(frozen=True)
class BenchmarkImportResult:
    """Outcome of an Excel-to-benchmarks transformation per ADR-0061.

    Attributes:
        n_benchmarks: Number of benchmark catalogue rows
            upserted by code.
        n_observations: Number of period-return observation rows
            inserted across all benchmarks (the previous generation
            was deleted before insert per the replace-by-benchmark
            contract).
        n_mappings: Number of asset-class → benchmark mapping rows
            inserted across all affected asset classes (the previous
            generation was deleted before insert per the
            replace-by-asset-class contract). Empty-code rows with
            ``weight == 0`` are skipped — they represent the
            deliberate "no benchmark for this asset class" case and
            do not produce DB rows.
        warnings: Row-level errors and notes collected by the
            extractor. May be non-empty even on a successful
            transformation (partial-success per ADR-0043 §3).
    """

    n_benchmarks: int
    n_observations: int
    n_mappings: int
    warnings: list[ImportRowError]


@dataclass(frozen=True)
class FxRateImportResult:
    """Outcome of an Excel-to-FX-rates transformation per ADR-0099 §5.

    Attributes:
        currencies: The distinct priced (base) currencies seen in the
            ``FX rates`` sheet, sorted. Empty when the sheet is absent.
        n_rates: Number of ``fx_rates`` rows upserted (one per
            non-blank, strictly-positive cell). Re-importing the same
            workbook re-counts the same rows — the upsert is idempotent
            on the ``(tenant_id, currency, as_of_date)`` natural key, so
            this is a write-effect count, not a net-new count.
        warnings: Row-level errors collected by the extractor
            (non-numeric or non-positive cells). May be non-empty even
            on a successful transformation (partial-success per
            ADR-0043 §3).
    """

    currencies: list[str]
    n_rates: int
    warnings: list[ImportRowError]


@dataclass(frozen=True)
class InvestmentChartsBundle:
    """Pre-computed analytics output for the three Phase-5b investment charts.

    Bundle the four analytics outputs for the Total Return, Cash
    Flows & NAV, and TVPI & DPI charts in one structure so the route
    handler invokes :class:`InvestmentService` once and passes the
    result through to the three spec generators.

    Attributes:
        total_return_series: Period-over-period actual returns from
            NAV ``pct_change()``.
        nav_series: Actual-NAV time series indexed by ``as_of_date``.
        cashflows_actual: Flat DataFrame with ``flow_timestamp``,
            ``flow_type``, and ``amount`` (signed) for the actual
            cashflows.
        net_capital_gain: NCG time series — see
            :func:`services.analytics.investment_returns.compute_net_capital_gain`.
        rolling_multiples: DataFrame with TVPI / DPI / RVPI per NAV
            observation.
        rolling_irr: Rolling IRR since inception per NAV observation.
            Empty Series when the caller passed ``include_irr=False``
            to ``get_charts_data``.
        investment_name: The investment's display name (used in the
            chart titles).
    """

    total_return_series: pd.Series
    nav_series: pd.Series
    cashflows_actual: pd.DataFrame
    net_capital_gain: pd.Series
    rolling_multiples: pd.DataFrame
    rolling_irr: pd.Series
    investment_name: str


# The seven canonical cashflow ``SeriesKind`` values — value-identical to
# the ``investment_cashflows.flow_type`` CHECK set. A live series of any of
# these lands as a cashflow row; ``SeriesKind.NAV_PRICE`` lands as a NAV.
# The **per-share** flow kinds. Their DTO value is a per-unit magnitude
# (a Yahoo per-share dividend, a per-share coupon), while
# ``investment_cashflows.amount`` is a **position-level** value. On a
# ``'unitised'`` investment they are scaled to position level at the routing
# point (``amount = per-share × holdings(as_of_date)``, ADR-0098 §4) before
# reaching a cashflow row (this closes finding F6); on a ``'reported'``
# investment they have no correct landing spot and are refused
# (``skipped_unit_mismatch``). Kept disjoint from
# :data:`_POSITION_LEVEL_CASHFLOW_KINDS` so the router can tell the two apart.
_PER_SHARE_FLOW_KINDS: frozenset[SeriesKind] = frozenset(
    {
        SeriesKind.DIVIDEND,
        SeriesKind.COUPON,
    }
)

# The **position-level** cashflow kinds. These arrive already position-scaled
# from private-markets-style providers, so they route into
# ``investment_cashflows`` unchanged for every valuation mode (ADR-0098 §4).
_POSITION_LEVEL_CASHFLOW_KINDS: frozenset[SeriesKind] = frozenset(
    {
        SeriesKind.DISTRIBUTION,
        SeriesKind.CAPITAL_CALL,
        SeriesKind.FEE,
        SeriesKind.CARRY,
        SeriesKind.OTHER,
    }
)


@dataclass(frozen=True)
class LiveIngestReport:
    """Per-series outcome counts for one live ingest (ADR-0092).

    Idempotency is a property, not a mode: re-running the same
    :class:`~services.market_data.dto.NormalizedSeries` produces all-zero
    change counts on the second run — only ``noop_live`` (unique-keyed
    NAV/weight points already present and unchanged) and the ``skipped_*``
    counters move.

    Attributes:
        inserted: New rows written as ``ingest_origin = 'live'``.
        updated_live: Existing ``'live'`` rows refreshed in place (a NAV
            whose value changed since the last fetch).
        skipped_excel: Points whose target row is ``'excel'`` — left
            byte-identical (book of record is authoritative).
        skipped_manual: Points whose target row is ``'manual'`` — left
            byte-identical (operator edits are live-immune).
        noop_live: Points already present as an identical ``'live'`` row —
            no write issued (the idempotency signal).
        skipped_unit_mismatch: Per-share points (``nav_price`` / ``dividend``
            / ``coupon``) refused because the target investment is
            ``valuation_mode='reported'`` — a per-share magnitude has no
            correct landing spot in a NAV-driven book (findings F1/F6,
            ADR-0098 §4). Counted rather than raised so the defence-in-depth
            layer behind the ``market_linked`` gate degrades to a quiet
            no-op. Replaces the S0 interim guard's blanket refusal.
        skipped_currency_mismatch: Per-share points refused because the
            series currency differs from the investment currency — rejected,
            never converted (ADR-0097 §5). Applies only to the ``'unitised'``
            re-routed paths (price / scaled per-share flow); position-level
            kinds are unaffected.
        skipped_zero_holdings: Per-share flow points on a date whose derived
            holdings are zero — there is nothing to scale by, so the point is
            skipped (ADR-0098 §4). Only the ``'unitised'`` dividend/coupon
            scaling path produces this outcome.
    """

    inserted: int = 0
    updated_live: int = 0
    skipped_excel: int = 0
    skipped_manual: int = 0
    noop_live: int = 0
    skipped_unit_mismatch: int = 0
    skipped_currency_mismatch: int = 0
    skipped_zero_holdings: int = 0

    @property
    def total(self) -> int:
        """Total number of DTO points classified."""
        return (
            self.inserted
            + self.updated_live
            + self.skipped_excel
            + self.skipped_manual
            + self.noop_live
            + self.skipped_unit_mismatch
            + self.skipped_currency_mismatch
            + self.skipped_zero_holdings
        )


@dataclass(frozen=True)
class PositionSummaryDTO:
    """Everything the positions panel renders for one investment (S5).

    An aggregate read DTO in the spirit of :class:`InvestmentDetailDTO`: the
    web route asks once and renders, rather than fanning out over three
    repositories and re-deriving the flip preconditions itself.

    Attributes:
        investment_id: The investment this summary describes.
        valuation_mode: ``'reported'`` or ``'unitised'``.
        currency: The investment's currency — the currency every ledger row
            and price row must carry (ADR-0097 §5).
        shows_panel: Whether the detail page renders the positions panel at
            all. ``False`` for the private-markets majority, whose page then
            stays byte-identical to its pre-ADR-0097 render.
        transactions: The ledger in canonical ``(trade_date, created_at, id)``
            order. Empty when ``shows_panel`` is ``False``.
        holdings_units: Units held as of :attr:`holdings_as_of_date` — the
            last point of the derived step function. ``Decimal(0)`` for an
            empty ledger.
        holdings_as_of_date: The date :attr:`holdings_units` refers to, or
            ``None`` for an empty ledger.
        latest_price: The most recent ``instrument_prices`` row, or ``None``.
        latest_computed_nav: The most recent ``actual`` NAV row this
            platform materialised (``ingest_origin='system'``), or ``None``.
            Carries ``basis='computed'`` — the provenance the panel badges.
        can_flip: Whether the one-way flip to ``'unitised'`` may proceed.
        flip_blocked_reason: The operator-facing sentence explaining why not,
            or ``None`` when :attr:`can_flip` is ``True``.
    """

    investment_id: UUID
    valuation_mode: str
    currency: str
    shows_panel: bool
    transactions: list[PositionTransactionDTO]
    holdings_units: Decimal
    holdings_as_of_date: _date | None
    latest_price: InstrumentPriceDTO | None
    latest_computed_nav: InvestmentNavDTO | None
    can_flip: bool
    flip_blocked_reason: str | None


@dataclass(frozen=True)
class _LedgerCandidate:
    """The as-yet-unwritten transaction, shaped for the holdings check.

    Structurally satisfies
    :class:`services.investments.holdings.LedgerTransaction` so the
    candidate participates in :func:`first_negative_holding_date` alongside
    the persisted rows before it is written. ``created_at`` is set to the
    current instant so the candidate sorts **after** existing same-day rows
    (it is the newest write), and ``id`` a fresh UUID for a total order.
    """

    txn_type: str
    trade_date: _date
    units: Decimal
    created_at: datetime
    id: UUID


@dataclass(frozen=True)
class _CashReconcileCounts:
    """Write-effect counts of the Cash-sheet reconcile (ADR-0103 §4).

    Summed across cash positions with ``+``, then folded into the
    operator-facing :class:`InvestmentExtractionResult`. Every counter is a
    genuine delta rather than a replace-count: an unchanged Cash sheet
    re-imports with all five at zero, which is the idempotency signal the
    ADR requires.
    """

    ledger_inserted: int = 0
    ledger_updated: int = 0
    ledger_deleted: int = 0
    prices_written: int = 0
    prices_deleted: int = 0

    def __add__(self, other: _CashReconcileCounts) -> _CashReconcileCounts:
        return _CashReconcileCounts(
            ledger_inserted=self.ledger_inserted + other.ledger_inserted,
            ledger_updated=self.ledger_updated + other.ledger_updated,
            ledger_deleted=self.ledger_deleted + other.ledger_deleted,
            prices_written=self.prices_written + other.prices_written,
            prices_deleted=self.prices_deleted + other.prices_deleted,
        )


class InvestmentService:
    """Workflow aggregator over the three Investment-domain repositories.

    All three repositories must be tenant-scoped (the caller
    constructs them with a session obtained via
    :func:`core.repositories.tenant_context`). The service does not
    set or read ``app.tenant_id`` itself — that responsibility lives
    on the session.
    """

    def __init__(
        self,
        investments: InvestmentRepository,
        navs: InvestmentNavRepository,
        cashflows: InvestmentCashflowRepository,
        identifiers: InvestmentIdentifierRepository | None = None,
        position_transactions: PositionTransactionRepository | None = None,
        instrument_prices: InstrumentPriceRepository | None = None,
    ) -> None:
        self._investments = investments
        self._navs = navs
        self._cashflows = cashflows
        # Optional so the many existing three-repo construction sites are
        # unaffected (Module-Scope discipline). The identifier CRUD surface
        # (ADR-0096) wires it in; the identifier methods below require it and
        # raise a clear error when it is absent.
        self._identifiers = identifiers
        # Optional for the same reason (ADR-0097 strand S1). The ledger
        # write path (:meth:`add_position_transaction`) requires it and
        # raises a clear error when it is absent; the future Excel-opening
        # synthesis (S4) and web transaction entry (S5) wire it in.
        self._position_transactions = position_transactions
        # Optional (ADR-0098 strand S2). The instrument-price series the
        # computed-NAV materialisation reads. Required only when a *unitised*
        # investment is written through :meth:`add_position_transaction`
        # (materialisation then runs in-transaction); a construction site
        # that never touches unitised investments may omit it.
        self._instrument_prices = instrument_prices

    def _require_identifiers(self) -> InvestmentIdentifierRepository:
        """Return the wired identifier repository or fail loudly.

        The identifier CRUD methods (ADR-0096) are only reachable from a
        service constructed with an ``identifiers`` repository. Callers that
        did not wire one in are a programming error, not a user error.
        """
        if self._identifiers is None:
            raise RuntimeError(
                "InvestmentService was constructed without an identifier "
                "repository; identifier operations are unavailable."
            )
        return self._identifiers

    def _require_position_transactions(self) -> PositionTransactionRepository:
        """Return the wired position-transaction repository or fail loudly.

        The ledger write path (:meth:`add_position_transaction`, ADR-0097)
        is only reachable from a service constructed with a
        ``position_transactions`` repository. Callers that did not wire one
        in are a programming error, not a user error.
        """
        if self._position_transactions is None:
            raise RuntimeError(
                "InvestmentService was constructed without a position-"
                "transaction repository; ledger operations are unavailable."
            )
        return self._position_transactions

    def _require_instrument_prices(self) -> InstrumentPriceRepository:
        """Return the wired instrument-price repository or fail loudly.

        The ``'unitised'`` live-ingest routing (ADR-0098 §4) writes prices
        through this repository and materialises from it. Reached only for a
        ``'unitised'`` investment; a caller that gets there without wiring an
        ``instrument_prices`` repository is a programming error, not a user
        error.
        """
        if self._instrument_prices is None:
            raise RuntimeError(
                "InvestmentService was constructed without an instrument-"
                "price repository; unitised live-ingest routing and "
                "computed-NAV materialisation are unavailable."
            )
        return self._instrument_prices

    def _nav_materialiser(self) -> NavMaterialisationService:
        """Build the computed-NAV materialisation service or fail loudly.

        The materialisation (ADR-0098) needs the instrument-price series in
        addition to the ledger and NAV repositories this service already
        holds; all four share this service's session, so the run is a single
        in-transaction unit (ADR-0098 §3). Reached only when a *unitised*
        investment is written through :meth:`add_position_transaction`; a
        caller that reaches it without wiring an ``instrument_prices``
        repository is a programming error, not a user error.
        """
        if self._instrument_prices is None:
            raise RuntimeError(
                "InvestmentService was constructed without an instrument-"
                "price repository; computed-NAV materialisation for unitised "
                "investments is unavailable."
            )
        return NavMaterialisationService(
            investments=self._investments,
            navs=self._navs,
            prices=self._instrument_prices,
            transactions=self._require_position_transactions(),
        )

    def _cash_plan_materialiser(self) -> CashPlanMaterialisationService:
        """Build the cash plan-path materialisation service (ADR-0103 §6).

        The plan-path sibling of :meth:`_nav_materialiser`. It needs only the
        three repositories this service is **always** constructed with —
        investments, NAVs, cashflows — because plan rows stay value-based
        (compatibility annex §B.1): the projection reads the anchor from the
        NAV series and the events from the cashflow table, and writes NAV
        values directly. No ledger, no prices, no FX.

        Hence there is no ``_require_*`` guard here and none is missing: the
        plan path has no optional dependency that could go silently unwired,
        so the ADR-0098 loud-failure posture is satisfied structurally rather
        than by a raise. Every construction site of this service — including
        the reported-mode-only ones that omit the position repositories —
        gets a working plan path.
        """
        return CashPlanMaterialisationService(
            investments=self._investments,
            navs=self._navs,
            cashflows=self._cashflows,
        )

    async def _materialise_cash_plan_for_flow(
        self,
        *,
        currencies: set[str],
        since: _date | None,
        acting_user: UUID,
    ) -> CashPlanReport:
        """Recompute the cash plan path after a plan-flow mutation.

        The shared tail of the three cashflow CRUD seams (ADR-0103 §6). A
        mutation whose old **and** new state are both ``actual``-kind moves
        nothing — actual flows are informational on cash (ADR-0103 §5); actual
        balances come from statement levels — and passes an empty
        ``currencies`` set, which is a no-op.
        """
        if not currencies:
            return CashPlanReport()
        return await self._cash_plan_materialiser().materialise_currencies(
            currencies, acting_user=acting_user, since=since
        )

    async def add_position_transaction(
        self,
        *,
        investment_id: UUID,
        txn_type: str,
        trade_date: _date,
        units: Decimal,
        currency: str,
        ingest_origin: str,
        created_by: UUID,
        price_per_unit: Decimal | None = None,
        consideration: Decimal | None = None,
        note: str | None = None,
        source: str | None = None,
    ) -> PositionTransactionDTO:
        """Validate and persist one ledger transaction (ADR-0097 §2/§4/§5).

        The single sanctioned write seam for the transaction ledger — the
        future Excel-opening synthesis (strand S4) and web transaction
        entry (strand S5) both enter here. Beyond the DB CHECKs (sign
        rules, price rules, closed ``txn_type`` / ``ingest_origin`` sets,
        and the one-``opening``-per-investment partial unique index), this
        method enforces the two invariants the CHECKs cannot reach:

        1. **Currency equality (ADR-0097 §5).** ``currency`` must equal the
           investment's currency — the write is rejected, never converted.
        2. **Non-negative holdings (ADR-0097 §4).** Holdings are recomputed
           over the existing ledger plus this candidate; if any point goes
           negative the write is rejected. Short positions are out of
           scope.

        On success, if the investment is ``valuation_mode='unitised'``, the
        computed-NAV materialisation (ADR-0098 §3) runs synchronously in the
        same transaction — recomputing ``investment_navs`` rows from
        ``trade_date`` onward. This requires the service to have been
        constructed with an ``instrument_prices`` repository. A
        ``'reported'`` investment triggers nothing and stays byte-identical.

        Args:
            investment_id: The investment this transaction belongs to.
            txn_type: One of ``opening`` / ``buy`` / ``sell`` / ``transfer``.
            trade_date: Statement-day date of the event.
            units: Signed unit quantity.
            currency: ISO 4217 currency code; must equal the investment's
                currency.
            ingest_origin: Producer — ``'excel'`` or ``'manual'``.
            created_by: UUID of the user attributable for the write.
            price_per_unit: Optional per-unit trade price (required by CHECK
                for ``buy``/``sell``).
            consideration: Optional signed cash effect.
            note: Optional free-text note.
            source: Optional free-text provenance.

        Returns:
            The newly created :class:`PositionTransactionDTO`.

        Raises:
            ValidationError: If the investment does not exist.
            CurrencyMismatchError: If ``currency`` differs from the
                investment's currency.
            NonNegativeHoldingsError: If the transaction would drive
                holdings below zero on any date.
        """
        repo = self._require_position_transactions()

        investment = await self._investments.get_by_id(investment_id)
        if investment is None:
            raise ValidationError(
                f"Investment {investment_id} does not exist in this tenant.",
                field="investment_id",
            )
        if currency != investment.currency:
            raise CurrencyMismatchError(
                f"Transaction currency {currency!r} does not match investment "
                f"currency {investment.currency!r}; cross-currency positions "
                "are not supported (ADR-0097 §5).",
                field="currency",
            )

        existing = await repo.list_for_investment(investment_id)
        candidate = _LedgerCandidate(
            txn_type=txn_type,
            trade_date=trade_date,
            units=units,
            created_at=datetime.now(timezone.utc),
            id=uuid4(),
        )
        offending = first_negative_holding_date([*existing, candidate])
        if offending is not None:
            raise NonNegativeHoldingsError(
                f"Transaction would drive holdings below zero on {offending} "
                f"for investment {investment_id}; short positions are out of "
                "scope (ADR-0097 §4).",
                field="units",
            )

        created = await repo.add(
            investment_id=investment_id,
            txn_type=txn_type,
            trade_date=trade_date,
            units=units,
            currency=currency,
            ingest_origin=ingest_origin,
            created_by=created_by,
            price_per_unit=price_per_unit,
            consideration=consideration,
            note=note,
            source=source,
        )

        # Materialisation trigger (ADR-0098 §3): a ledger write on a
        # *unitised* investment recomputes its computed-NAV rows in the same
        # transaction, from the earliest affected date (this transaction's
        # trade_date — a change here cannot alter holdings on any earlier
        # date). A 'reported' investment is untouched: no materialisation
        # runs and its NAV series stays byte-identical, so the many existing
        # 'reported'-mode construction sites need no instrument-price repo.
        if investment.valuation_mode == "unitised":
            await self._nav_materialiser().materialise(
                investment_id,
                acting_user=created_by,
                since=trade_date,
            )

        return created

    async def update_position_transaction(
        self,
        *,
        investment_id: UUID,
        transaction_id: UUID,
        trade_date: _date,
        units: Decimal,
        acting_user: UUID,
        price_per_unit: Decimal | None = None,
        consideration: Decimal | None = None,
        note: str | None = None,
        source: str | None = None,
    ) -> PositionTransactionDTO | None:
        """Restate one ledger transaction in place (ADR-0097 §4).

        The edit sibling of :meth:`add_position_transaction`. ``txn_type``,
        ``currency``, and ``ingest_origin`` are immutable — see
        :meth:`core.repositories.position_transaction_repository.PositionTransactionRepository.update`.
        An ``'excel'``-origin row may be edited: the operator's correction
        holds until the next Excel import restates it (ADR-0097 §7), and the
        web surface says so.

        The non-negativity invariant is re-checked over the ledger with this
        row **replaced** by its restated values — an edit that lowers an
        opening below a later sell is rejected exactly as the sell would have
        been. Materialisation then reruns from ``min(old, new) trade_date``:
        a restatement that moves a row later still changes holdings from its
        *old* date onward.

        Args:
            investment_id: The investment owning the transaction. A
                transaction belonging to a different investment resolves to
                ``None`` — the caller renders that as ``404``.
            transaction_id: The transaction to restate.
            trade_date: Restated statement-day date.
            units: Restated signed unit quantity.
            acting_user: The user attributable for the resulting
                materialisation writes.
            price_per_unit: Restated per-unit trade price, or ``None``.
            consideration: Restated signed cash effect, or ``None``.
            note: Restated free-text note, or ``None``.
            source: Restated free-text provenance, or ``None``.

        Returns:
            The updated :class:`PositionTransactionDTO`, or ``None`` if the
            transaction does not exist in this tenant or does not belong to
            ``investment_id``.

        Raises:
            ValidationError: If the investment does not exist.
            NonNegativeHoldingsError: If the restatement would drive holdings
                below zero on any date.
        """
        repo = self._require_position_transactions()

        investment = await self._investments.get_by_id(investment_id)
        if investment is None:
            raise ValidationError(
                f"Investment {investment_id} does not exist in this tenant.",
                field="investment_id",
            )

        existing = await repo.get_by_id(transaction_id)
        if existing is None or existing.investment_id != investment_id:
            return None

        # Re-check non-negativity over the ledger with this row replaced by
        # its restatement. Preserving the row's created_at and id keeps the
        # (trade_date, created_at, id) tiebreak — and therefore the derived
        # step function — identical to what the write will produce.
        others = [t for t in await repo.list_for_investment(investment_id) if t.id != existing.id]
        candidate = _LedgerCandidate(
            txn_type=existing.txn_type,
            trade_date=trade_date,
            units=units,
            created_at=existing.created_at,
            id=existing.id,
        )
        offending = first_negative_holding_date([*others, candidate])
        if offending is not None:
            raise NonNegativeHoldingsError(
                f"Restated transaction would drive holdings below zero on "
                f"{offending} for investment {investment_id}; short positions "
                "are out of scope (ADR-0097 §4).",
                field="units",
            )

        updated = await repo.update(
            transaction_id,
            trade_date=trade_date,
            units=units,
            price_per_unit=price_per_unit,
            consideration=consideration,
            note=note,
            source=source,
        )

        # Materialisation trigger (ADR-0098 §3). The earliest affected date is
        # the earlier of the row's old and new trade_date: moving a row
        # forward changes holdings from where it left, moving it back changes
        # them from where it landed.
        if updated is not None and investment.valuation_mode == "unitised":
            await self._nav_materialiser().materialise(
                investment_id,
                acting_user=acting_user,
                since=min(existing.trade_date, trade_date),
            )

        return updated

    async def delete_position_transaction(
        self,
        *,
        investment_id: UUID,
        transaction_id: UUID,
        acting_user: UUID,
    ) -> bool:
        """Delete one ledger transaction (ADR-0097 §4, ADR-0098 §3).

        Deleting a ``buy`` or an ``opening`` can strand a later ``sell`` above
        the remaining holdings, so the non-negativity invariant is re-checked
        over the ledger **without** this row before the delete is issued.

        Materialisation reruns from the deleted row's ``trade_date``: removing
        a transaction cannot alter holdings on any earlier date. Where the
        deletion empties the ledger or drives holdings to zero, the rerun
        removes the now-stranded ``'system'`` NAV rows — and only those
        (ADR-0098 §2).

        Args:
            investment_id: The investment owning the transaction.
            transaction_id: The transaction to delete.
            acting_user: The user attributable for the resulting
                materialisation writes.

        Returns:
            ``True`` if a row was deleted; ``False`` if the transaction does
            not exist in this tenant or does not belong to ``investment_id``.

        Raises:
            ValidationError: If the investment does not exist.
            NonNegativeHoldingsError: If removing the transaction would drive
                holdings below zero on any date.
        """
        repo = self._require_position_transactions()

        investment = await self._investments.get_by_id(investment_id)
        if investment is None:
            raise ValidationError(
                f"Investment {investment_id} does not exist in this tenant.",
                field="investment_id",
            )

        existing = await repo.get_by_id(transaction_id)
        if existing is None or existing.investment_id != investment_id:
            return False

        remaining = [
            t for t in await repo.list_for_investment(investment_id) if t.id != existing.id
        ]
        offending = first_negative_holding_date(remaining)
        if offending is not None:
            raise NonNegativeHoldingsError(
                f"Deleting this transaction would drive holdings below zero "
                f"on {offending} for investment {investment_id}; short "
                "positions are out of scope (ADR-0097 §4).",
                field="transaction_id",
            )

        deleted = await repo.delete(transaction_id)

        if deleted and investment.valuation_mode == "unitised":
            await self._nav_materialiser().materialise(
                investment_id,
                acting_user=acting_user,
                since=existing.trade_date,
            )

        return deleted

    async def flip_to_unitised(
        self,
        investment_id: UUID,
        *,
        acting_user: UUID,
    ) -> NavMaterialisationReport:
        """Switch one investment to unitised valuation — one-way (ADR-0097 §6).

        The explicit operator act that moves an investment off reported NAVs
        and onto ``holdings × price``. Three effects, in one transaction, in
        an order the semantics force:

        1. **Set the mode.** :meth:`NavMaterialisationService.materialise` is
           a no-op on a ``'reported'`` investment by design, so the mode must
           already be ``'unitised'`` when it runs.
        2. **Delete the investment's ``'live'``-origin NAV rows.** They are
           per-share prices written into a position-value column — the F1
           defect artifacts — and their information content is re-ingested
           into ``instrument_prices``. This must precede materialisation, or
           the very rows the flip exists to remove would instead be counted
           and warned about as ``skipped_live`` (ADR-0098 §1). ``'excel'``,
           ``'manual'``, and ``'system'`` rows are never touched.
        3. **Materialise the full series** (``since=None``) — every date
           carrying both a price and a positive holding gains a ``'system'``
           row with ``basis='computed'``.

        The flip is **one-way**: an already-unitised investment fails the
        precondition check rather than flapping back. Corrections go through
        ledger edits.

        Args:
            investment_id: The investment to unitise.
            acting_user: ``created_by`` for the materialised rows.

        Returns:
            The :class:`NavMaterialisationReport` of the initial full
            materialisation — its ``inserted`` count is what the operator
            surface reports back.

        Raises:
            ValidationError: If the investment does not exist.
            ValuationModeError: If any ADR-0097 §6 precondition fails — the
                type is not ``listed_equity``/``listed_bonds``/``cash``
                (ADR-0103 §1), no ``opening`` transaction exists, or the
                investment is already unitised.
        """
        repo = self._require_position_transactions()

        investment = await self._investments.get_by_id(investment_id)
        if investment is None:
            raise ValidationError(
                f"Investment {investment_id} does not exist in this tenant.",
                field="investment_id",
            )

        opening = await repo.get_opening(investment_id)
        blocked = flip_precondition_error(
            investment.investment_type,
            investment.valuation_mode,
            has_opening=opening is not None,
        )
        if blocked is not None:
            raise ValuationModeError(blocked, field="valuation_mode")

        await self._investments.set_valuation_mode(investment_id, "unitised")
        deleted_live = await self._navs.delete_live_navs(investment_id)
        report = await self._nav_materialiser().materialise(
            investment_id,
            acting_user=acting_user,
            since=None,
        )

        _LOG.info(
            "flip-to-unitised: investment=%s deleted_live_navs=%d "
            "materialised inserted=%d updated=%d skipped_excel=%d "
            "skipped_manual=%d",
            investment_id,
            deleted_live,
            report.inserted,
            report.updated,
            report.skipped_excel,
            report.skipped_manual,
        )
        return report

    # ------------------------------------------------------------------
    # Group 1: read workflows
    # ------------------------------------------------------------------

    async def list_position_transactions(self, investment_id: UUID) -> list[PositionTransactionDTO]:
        """Return one investment's ledger in canonical order.

        Args:
            investment_id: The investment whose ledger to load.

        Returns:
            The ledger rows ordered ``(trade_date, created_at, id)``; empty
            for an unknown investment or one with no transactions.
        """
        return await self._require_position_transactions().list_for_investment(investment_id)

    async def get_position_summary(self, investment_id: UUID) -> PositionSummaryDTO | None:
        """Aggregate everything the positions panel renders (strand S5).

        Reads the ledger first and short-circuits when the panel is not shown
        for this investment: the private-markets majority pays exactly one
        cheap query and its detail page is unchanged. Only when the panel is
        relevant are the price series and the computed NAV rows loaded.

        Args:
            investment_id: The investment to summarise.

        Returns:
            The :class:`PositionSummaryDTO`, or ``None`` if the investment
            does not exist in the active tenant context.
        """
        investment = await self._investments.get_by_id(investment_id)
        if investment is None:
            return None

        ledger = self._require_position_transactions()
        transactions = await ledger.list_for_investment(investment_id)

        visible = shows_positions_panel(
            investment.investment_type,
            investment.valuation_mode,
            has_transactions=bool(transactions),
        )
        if not visible:
            return PositionSummaryDTO(
                investment_id=investment_id,
                valuation_mode=investment.valuation_mode,
                currency=investment.currency,
                shows_panel=False,
                transactions=[],
                holdings_units=Decimal(0),
                holdings_as_of_date=None,
                latest_price=None,
                latest_computed_nav=None,
                can_flip=False,
                flip_blocked_reason=flip_precondition_error(
                    investment.investment_type,
                    investment.valuation_mode,
                    has_opening=False,
                ),
            )

        points = derive_holdings(transactions)
        prices = await self._require_instrument_prices().list_by_investment(investment_id)
        actual_navs = await self._navs.list_by_investment_and_kind(investment_id, "actual")
        computed = [n for n in actual_navs if n.ingest_origin == "system"]

        has_opening = any(t.txn_type == "opening" for t in transactions)
        blocked = flip_precondition_error(
            investment.investment_type,
            investment.valuation_mode,
            has_opening=has_opening,
        )

        return PositionSummaryDTO(
            investment_id=investment_id,
            valuation_mode=investment.valuation_mode,
            currency=investment.currency,
            shows_panel=True,
            transactions=transactions,
            holdings_units=points[-1].units if points else Decimal(0),
            holdings_as_of_date=points[-1].as_of_date if points else None,
            # Both series are ordered ascending by date at the repository.
            latest_price=prices[-1] if prices else None,
            latest_computed_nav=computed[-1] if computed else None,
            can_flip=blocked is None,
            flip_blocked_reason=blocked,
        )

    async def list_investments(self) -> list[InvestmentDTO]:
        """List every investment (active and inactive) in the active tenant.

        Returns:
            All investments sorted by ``name`` for stable rendering
            in the CRUD list view.
        """
        return await self._investments.list_all()

    async def list_active_investments(self) -> list[InvestmentDTO]:
        """List only investments where ``is_active = TRUE``.

        Returns:
            Active investments only, sorted by ``name``.
        """
        return await self._investments.list_active()

    async def get_investment(self, investment_id: UUID) -> InvestmentDTO | None:
        """Return the investment with the given id, or ``None`` if absent.

        The web CRUD surface (sub-stream 4b) needs a cheap existence
        check before write operations against an investment id —
        :meth:`get_investment_detail` would also load the full
        NAV/cashflow history, which is wasteful for a routing-time
        404 guard. Cross-tenant rows are invisible (RLS hides them);
        the service correctly reports absence rather than raising.

        Args:
            investment_id: The investment to look up.

        Returns:
            The matching :class:`InvestmentDTO`, or ``None`` if no
            investment with this id exists in the active tenant.
        """
        return await self._investments.get_by_id(investment_id)

    async def get_nav(self, nav_id: UUID) -> InvestmentNavDTO | None:
        """Return the NAV row with the given id, or ``None`` if absent.

        Used by the web CRUD surface (sub-stream 4b) for routing-time
        404 guards on NAV update and delete paths.

        Args:
            nav_id: The NAV row to look up.

        Returns:
            The matching :class:`InvestmentNavDTO`, or ``None`` if no
            NAV with this id exists in the active tenant.
        """
        return await self._navs.get_by_id(nav_id)

    async def get_cashflow(self, cashflow_id: UUID) -> InvestmentCashflowDTO | None:
        """Return the cashflow row with the given id, or ``None`` if absent.

        Used by the web CRUD surface (sub-stream 4b) for routing-time
        404 guards on cashflow update and delete paths, and for
        verifying that a path-supplied ``cashflow_id`` belongs to the
        path-supplied ``investment_id`` within one tenant.

        Args:
            cashflow_id: The cashflow row to look up.

        Returns:
            The matching :class:`InvestmentCashflowDTO`, or ``None``
            if no cashflow with this id exists in the active tenant.
        """
        return await self._cashflows.get_by_id(cashflow_id)

    async def get_investment_detail(self, investment_id: UUID) -> InvestmentDetailDTO | None:
        """Return an investment with its full NAV and cashflow history.

        The aggregate DTO is the consumption shape for the
        Investment detail view (sub-stream 4b). The route handler
        does not need to orchestrate three separate repository calls.

        Args:
            investment_id: The investment to load.

        Returns:
            ``None`` if no investment with this id exists in the
            active tenant; otherwise an :class:`InvestmentDetailDTO`.
        """
        investment = await self._investments.get_by_id(investment_id)
        if investment is None:
            return None
        navs = await self._navs.list_by_investment(investment_id)
        cashflows = await self._cashflows.list_by_investment(investment_id)
        return InvestmentDetailDTO(
            investment=investment,
            navs=navs,
            cashflows=cashflows,
        )

    async def get_charts_data(
        self,
        investment_id: UUID,
        *,
        include_irr: bool = True,
    ) -> InvestmentChartsBundle | None:
        """Aggregate inputs and analytics for the Phase-5b chart triple.

        Loads the actual NAV history and the actual cashflows for the
        investment, runs the four analytics functions in
        :mod:`services.analytics.investment_returns`, and returns a
        single :class:`InvestmentChartsBundle` ready for the
        ``services.chart_specs.build_*_spec`` generators. RLS hides
        cross-tenant rows, so an investment in a foreign tenant
        surfaces as ``None``.

        Args:
            investment_id: The investment to load charts data for.
            include_irr: When ``True`` (default) the rolling IRR
                since inception is computed via Brent's method per
                NAV observation. Callers that do not consume the IRR
                series (e.g. chart routes using
                ``build_multiples_spec(..., style="lines")``) should
                pass ``False`` to skip the dominant cost in the
                chart hot path; the bundle's ``rolling_irr`` field
                is then an empty Series.

        Returns:
            ``None`` if the investment does not exist in the active
            tenant context; otherwise an
            :class:`InvestmentChartsBundle`.
        """
        investment = await self._investments.get_by_id(investment_id)
        if investment is None:
            return None
        navs = await self._navs.list_by_investment(investment_id)
        cashflows = await self._cashflows.list_by_investment(investment_id)

        actual_navs = sorted(
            (n for n in navs if n.nav_kind == "actual"),
            key=lambda n: n.as_of_date,
        )
        actual_cashflows = [c for c in cashflows if c.flow_kind == "actual"]

        nav_series = pd.Series(
            data=[float(n.nav_value) for n in actual_navs],
            index=[n.as_of_date for n in actual_navs],
            dtype="float64",
        )
        cashflows_actual = pd.DataFrame(
            {
                "flow_timestamp": [c.flow_timestamp for c in actual_cashflows],
                "flow_type": [c.flow_type for c in actual_cashflows],
                "amount": [float(c.amount) for c in actual_cashflows],
            }
        )

        total_return_series = compute_total_return_series(nav_series)
        net_capital_gain = compute_net_capital_gain(cashflows_actual, nav_series)
        rolling_multiples = compute_rolling_multiples(cashflows_actual, nav_series)
        rolling_irr = (
            compute_rolling_irr_since_inception(cashflows_actual, nav_series)
            if include_irr
            else pd.Series(dtype="float64")
        )

        return InvestmentChartsBundle(
            total_return_series=total_return_series,
            nav_series=nav_series,
            cashflows_actual=cashflows_actual,
            net_capital_gain=net_capital_gain,
            rolling_multiples=rolling_multiples,
            rolling_irr=rolling_irr,
            investment_name=investment.name,
        )

    # ------------------------------------------------------------------
    # Group 2: write workflows
    # ------------------------------------------------------------------

    async def create_investment(
        self,
        name: str,
        investment_type: str,
        asset_class_id: UUID,
        currency: str,
        created_by: UUID,
        *,
        manager_name: str | None = None,
        region: str | None = None,
        vintage_year: int | None = None,
        commitment_amount: Decimal | None = None,
        is_active: bool = True,
        type_specific_data: dict | None = None,
    ) -> InvestmentDTO:
        """Create a new investment.

        Args:
            name: Tenant-unique investment name.
            investment_type: One of eight CHECK-allowed discriminator
                values.
            asset_class_id: 1:1 FK to the per-tenant asset-class
                catalogue.
            currency: ISO 4217 currency code.
            created_by: UUID of the user creating the investment.
            manager_name: Optional fund-manager / GP name.
            region: Optional geographic region label.
            vintage_year: Optional integer vintage year.
            commitment_amount: Optional total commitment amount.
            is_active: Active flag, defaults to ``TRUE``.
            type_specific_data: Optional Phase-5+ extension JSONB
                (should remain ``None`` in Phase 4).

        Returns:
            The newly created :class:`InvestmentDTO`.
        """
        return await self._investments.create(
            name=name,
            investment_type=investment_type,
            asset_class_id=asset_class_id,
            currency=currency,
            created_by=created_by,
            manager_name=manager_name,
            region=region,
            vintage_year=vintage_year,
            commitment_amount=commitment_amount,
            is_active=is_active,
            type_specific_data=type_specific_data,
        )

    async def update_investment(
        self,
        investment_id: UUID,
        *,
        name: str | None = None,
        investment_type: str | None = None,
        asset_class_id: UUID | None = None,
        manager_name: str | None = None,
        region: str | None = None,
        currency: str | None = None,
        vintage_year: int | None = None,
        commitment_amount: Decimal | None = None,
        type_specific_data: dict | None = None,
    ) -> InvestmentDTO | None:
        """Update mutable fields on an investment.

        Args:
            investment_id: The investment to update.
            name: New name.
            investment_type: New type discriminator.
            asset_class_id: New asset-class FK.
            manager_name: New manager-name label.
            region: New region label.
            currency: New currency code.
            vintage_year: New vintage year.
            commitment_amount: New commitment amount.
            type_specific_data: New JSONB payload.

        Returns:
            The refreshed :class:`InvestmentDTO`, or ``None`` if no
            investment with this id exists in the active tenant
            context.
        """
        return await self._investments.update(
            investment_id,
            name=name,
            investment_type=investment_type,
            asset_class_id=asset_class_id,
            manager_name=manager_name,
            region=region,
            currency=currency,
            vintage_year=vintage_year,
            commitment_amount=commitment_amount,
            type_specific_data=type_specific_data,
        )

    async def delete_investment(self, investment_id: UUID) -> bool:
        """Hard-delete an investment.

        Child NAV and cashflow rows are removed automatically via FK
        ``ON DELETE CASCADE``. Asset classes referenced by the
        investment are not deleted (they remain in the catalogue).

        Args:
            investment_id: The investment to delete.

        Returns:
            ``True`` if a row was deleted, ``False`` if no investment
            with this id existed in the active tenant context.
        """
        return await self._investments.delete(investment_id)

    async def set_investment_active(self, investment_id: UUID, is_active: bool) -> None:
        """Toggle the soft-delete flag on an investment.

        Used by the Excel-import workflow (sub-stream 4c) for
        soft-delete-with-reactivation; also exposed to the web CRUD
        surface so an operator can deactivate an investment without
        hard-deleting its history.

        Args:
            investment_id: The investment to update.
            is_active: New value for the active flag.
        """
        await self._investments.set_active(investment_id, is_active)

    async def add_nav(
        self,
        investment_id: UUID,
        as_of_date: _date,
        nav_kind: str,
        nav_value: Decimal,
        currency: str,
        source: str | None,
        created_by: UUID,
    ) -> InvestmentNavDTO:
        """Add or update a NAV row by its ``(investment, date, kind)`` key.

        The underlying repository UPSERTs on
        ``(investment_id, as_of_date, nav_kind)``: re-inserting the
        same triple updates the existing row's value, currency, and
        source rather than failing on a UNIQUE-violation. ``created_by``
        is preserved on update.

        This is a **manual** CRUD write, so the row is stamped
        ``ingest_origin = 'manual'`` (ADR-0092) — a subsequent live
        fetch will not overwrite it; a subsequent Excel re-import still
        does (book of record).

        Args:
            investment_id: The investment this NAV belongs to.
            as_of_date: Statement-day date.
            nav_kind: ``"plan"`` or ``"actual"``.
            nav_value: Numeric NAV value.
            currency: ISO 4217 currency code.
            source: Optional free-form provenance label.
            created_by: UUID of the user attributable for the write.

        Returns:
            The created or updated :class:`InvestmentNavDTO`.
        """
        return await self._navs.upsert(
            investment_id=investment_id,
            as_of_date=as_of_date,
            nav_kind=nav_kind,
            nav_value=nav_value,
            currency=currency,
            source=source,
            created_by=created_by,
            ingest_origin="manual",
        )

    async def update_nav(
        self,
        nav_id: UUID,
        nav_value: Decimal,
        currency: str,
        source: str | None,
        created_by: UUID,
    ) -> InvestmentNavDTO | None:
        """Update a NAV row by id (delegates to UPSERT on the same key).

        The UPSERT path is the single sanctioned write path for NAVs:
        it preserves the natural-key invariant and re-uses the same
        DB-side ON CONFLICT logic. The :meth:`add_nav` and
        :meth:`update_nav` distinction exists for API clarity at the
        web surface level.

        Args:
            nav_id: The NAV row to update.
            nav_value: New NAV value.
            currency: New currency code.
            source: New provenance label.
            created_by: UUID of the user attributable for the write
                (preserved on update by the repository).

        Returns:
            The refreshed :class:`InvestmentNavDTO`, or ``None`` if no
            NAV row with this id exists in the active tenant context.
        """
        # The repository upsert is keyed on (investment_id, as_of_date,
        # nav_kind), so we read the existing row first to recover those
        # natural-key fields. This keeps update_nav callers free of
        # having to know about the natural key.
        existing = await self._navs.get_by_id(nav_id)
        if existing is None:
            return None
        return await self._navs.upsert(
            investment_id=existing.investment_id,
            as_of_date=existing.as_of_date,
            nav_kind=existing.nav_kind,
            nav_value=nav_value,
            currency=currency,
            source=source,
            created_by=created_by,
            ingest_origin="manual",
        )

    async def delete_nav(self, nav_id: UUID) -> bool:
        """Hard-delete a single NAV row.

        Args:
            nav_id: The NAV row to delete.

        Returns:
            ``True`` if a row was deleted, ``False`` if no NAV with
            this id existed in the active tenant context.
        """
        return await self._navs.delete(nav_id)

    async def _require_cash_for_investor_flow(self, investment_id: UUID, flow_type: str) -> None:
        """Reject an ``investor_flow`` booked on a non-cash investment.

        The ADR-0103 §5 cash-only booking rule. It spans two tables
        (``investment_cashflows.flow_type`` and
        ``investments.investment_type``), so no DB CHECK can express it and
        the service seam owns it — every caller of the cashflow write path
        inherits the rule from here rather than restating it.

        Args:
            investment_id: The investment the flow would be booked on.
            flow_type: The effective flow type of the write.

        Raises:
            InvestorFlowScopeError: If ``flow_type`` is ``'investor_flow'``
                and the investment is not of type ``'cash'``.
            ValidationError: If the investment does not exist in this tenant.
        """
        if flow_type != "investor_flow":
            return
        investment = await self._investments.get_by_id(investment_id)
        if investment is None:
            raise ValidationError(
                f"Investment {investment_id} does not exist in this tenant.",
                field="investment_id",
            )
        if investment.investment_type != "cash":
            raise InvestorFlowScopeError(
                "An investor flow may only be booked on a cash position "
                f"(investment {investment.name!r} is of type "
                f"{investment.investment_type!r}). Book it on the cash "
                "investment of the currency the flow settles in "
                "(ADR-0103 §5).",
                field="flow_type",
            )

    async def add_cashflow(
        self,
        investment_id: UUID,
        flow_timestamp: datetime,
        flow_type: str,
        flow_kind: str,
        amount: Decimal,
        currency: str,
        description: str | None,
        created_by: UUID,
    ) -> InvestmentCashflowDTO:
        """Append a new cashflow row.

        There is no UNIQUE constraint on the cashflow table, so every
        invocation creates a fresh row by design. This is a **manual**
        CRUD write, so the row is stamped ``ingest_origin = 'manual'``
        (ADR-0092) — a subsequent live fetch will not overwrite it.

        An ``'investor_flow'`` row is accepted only on a cash investment
        (ADR-0103 §5); both ``flow_kind`` variants and both amount signs are
        legal there — a contribution and a withdrawal are the same flow type
        with opposite signs.

        A ``'plan'``-kind row triggers the cash plan-path recompute in this
        transaction (ADR-0103 §6) — see
        :meth:`_materialise_cash_plan_for_flow`.

        Args:
            investment_id: The investment this cashflow belongs to.
            flow_timestamp: TIMESTAMPTZ of the flow event.
            flow_type: One of eight allowed values.
            flow_kind: ``"plan"`` or ``"actual"``.
            amount: Signed cashflow amount.
            currency: ISO 4217 currency code — the flow's **settlement**
                currency, which selects the cash path it projects onto.
            description: Optional free-form description.
            created_by: UUID of the user creating the row; also the acting
                user attributable for the resulting plan-path writes.

        Returns:
            The newly created :class:`InvestmentCashflowDTO`.

        Raises:
            InvestorFlowScopeError: If an ``'investor_flow'`` row is booked
                on a non-cash investment.
        """
        await self._require_cash_for_investor_flow(investment_id, flow_type)
        created = await self._cashflows.create(
            investment_id=investment_id,
            flow_timestamp=flow_timestamp,
            flow_type=flow_type,
            flow_kind=flow_kind,
            amount=amount,
            currency=currency,
            description=description,
            created_by=created_by,
            ingest_origin="manual",
        )

        # Cash plan-path trigger (ADR-0103 §6). A new plan flow is a new event
        # on its settlement currency's cash path, from its own date onward.
        await self._materialise_cash_plan_for_flow(
            currencies=({created.currency} if created.flow_kind == "plan" else set()),
            since=created.flow_timestamp.date(),
            acting_user=created_by,
        )
        return created

    async def update_cashflow(
        self,
        cashflow_id: UUID,
        *,
        acting_user: UUID,
        flow_timestamp: datetime | None = None,
        flow_type: str | None = None,
        flow_kind: str | None = None,
        amount: Decimal | None = None,
        currency: str | None = None,
        description: str | None = None,
    ) -> InvestmentCashflowDTO | None:
        """Update mutable fields on a cashflow row.

        The ADR-0103 §5 cash-only rule is evaluated against the row's
        **effective** flow type after the update: an update that sets
        ``flow_type='investor_flow'``, and one that leaves an existing
        ``investor_flow`` row's type untouched, are both checked. Changing a
        cash row's flow type *away* from ``investor_flow`` is unrestricted.

        The cash plan path is recomputed in this transaction (ADR-0103 §6)
        whenever the **old or the new** state is plan-kind — so an edit that
        promotes an actual flow to plan, demotes a plan flow to actual, or
        moves a plan flow between currencies all move the projection, and an
        update across currencies moves **both** cash paths.

        Args:
            cashflow_id: The cashflow row to update.
            acting_user: The user attributable for the resulting plan-path
                writes.
            flow_timestamp: New flow timestamp.
            flow_type: New flow type discriminator.
            flow_kind: New flow kind discriminator.
            amount: New amount value.
            currency: New currency code.
            description: New description.

        Returns:
            The refreshed :class:`InvestmentCashflowDTO`, or ``None``
            if no cashflow with this id exists in the active tenant
            context.

        Raises:
            InvestorFlowScopeError: If the update would leave an
                ``'investor_flow'`` row on a non-cash investment.
        """
        existing = await self._cashflows.get_by_id(cashflow_id)
        if existing is None:
            return None
        effective_flow_type = flow_type if flow_type is not None else existing.flow_type
        await self._require_cash_for_investor_flow(existing.investment_id, effective_flow_type)
        updated = await self._cashflows.update(
            cashflow_id,
            flow_timestamp=flow_timestamp,
            flow_type=flow_type,
            flow_kind=flow_kind,
            amount=amount,
            currency=currency,
            description=description,
            ingest_origin="manual",
        )
        if updated is None:
            return None

        # Cash plan-path trigger (ADR-0103 §6). Both states matter: the old one
        # to unwind the event the row used to be, the new one to book what it
        # now is. The recompute starts at the earlier of the two dates —
        # neither state can have moved the projection before that.
        currencies: set[str] = set()
        dates: list[_date] = []
        if existing.flow_kind == "plan":
            currencies.add(existing.currency)
            dates.append(existing.flow_timestamp.date())
        if updated.flow_kind == "plan":
            currencies.add(updated.currency)
            dates.append(updated.flow_timestamp.date())
        await self._materialise_cash_plan_for_flow(
            currencies=currencies,
            since=min(dates) if dates else None,
            acting_user=acting_user,
        )
        return updated

    async def delete_cashflow(self, cashflow_id: UUID, *, acting_user: UUID) -> bool:
        """Hard-delete a single cashflow row.

        Deleting a plan-kind flow removes an event from its settlement
        currency's cash path, so the projection is recomputed in this
        transaction from the deleted flow's date onward (ADR-0103 §6); where
        the deletion empties an event date, the recompute removes the
        now-stranded projected row.

        Args:
            cashflow_id: The cashflow row to delete.
            acting_user: The user attributable for the resulting plan-path
                writes.

        Returns:
            ``True`` if a row was deleted, ``False`` if no cashflow
            with this id existed in the active tenant context.
        """
        existing = await self._cashflows.get_by_id(cashflow_id)
        if existing is None:
            return False

        deleted = await self._cashflows.delete(cashflow_id)
        if deleted:
            await self._materialise_cash_plan_for_flow(
                currencies=({existing.currency} if existing.flow_kind == "plan" else set()),
                since=existing.flow_timestamp.date(),
                acting_user=acting_user,
            )
        return deleted

    # ------------------------------------------------------------------
    # Group 2c: security-identifier CRUD (ADR-0096)
    #
    # The human-operated mapping surface. Provider-native schemes (preqin /
    # pitchbook) and the listed schemes alike are added, re-primed, and
    # removed here — the confirmation that writes a mapping row is always
    # human (ADR-0096 §2); no machine path auto-writes a provider-ID
    # mapping. Creation is always ``source='manual'``; a human is the
    # authority, so the delete / re-prime paths may act on rows of any
    # provenance.
    # ------------------------------------------------------------------

    async def list_identifiers(self, investment_id: UUID) -> list[InvestmentIdentifierDTO]:
        """Return every identifier row for one investment.

        Read seam for the detail-page panel and the post-mutation
        re-render (ADR-0096). Rows are ordered ``(scheme, value)`` by the
        repository for stable rendering; an investment with no identifiers
        (the illiquid-instrument case) yields an empty list.

        Args:
            investment_id: The investment whose identifiers to load.

        Returns:
            The investment's :class:`InvestmentIdentifierDTO` rows in the
            active tenant context.
        """
        return await self._require_identifiers().list_for_investment(investment_id)

    async def add_identifier_manual(
        self,
        investment_id: UUID,
        scheme: str,
        value: str,
        user_id: UUID,
    ) -> InvestmentIdentifierDTO:
        """Add one human-confirmed identifier row (``source='manual'``).

        The row enters through the human-operated CRUD surface, so it is
        stamped ``source='manual'`` and ``created_by=user_id`` (ADR-0096
        §2 — the confirmation that writes a mapping is always human). The
        row is **not** primary by default; promotion is an explicit,
        separate act (:meth:`set_primary_identifier`). ``value`` is
        normalised (trim + upper-case) by the repository.

        Args:
            investment_id: The investment this identifier belongs to.
            scheme: One of the closed :data:`IDENTIFIER_SCHEMES`
                (ADR-0090 / ADR-0096).
            value: The identifier value; normalised on write.
            user_id: The user confirming the mapping; recorded as
                ``created_by``.

        Returns:
            The newly created :class:`InvestmentIdentifierDTO`.

        Raises:
            ValidationError: If ``scheme`` is not in the closed set, or
                ``value`` is empty after trimming (raised by the
                repository).
        """
        if scheme not in IDENTIFIER_SCHEMES:
            raise ValidationError(
                f"Unknown identifier scheme {scheme!r}; expected one of "
                f"{sorted(IDENTIFIER_SCHEMES)}.",
                field="scheme",
            )
        return await self._require_identifiers().add(
            investment_id=investment_id,
            scheme=scheme,
            value=value,
            created_by=user_id,
            is_primary=False,
            source="manual",
        )

    async def set_primary_identifier(
        self,
        investment_id: UUID,
        identifier_id: UUID,
        user_id: UUID,
    ) -> bool:
        """Make ``identifier_id`` the investment's single primary identifier.

        Demote the current primary (if any and distinct from the target),
        then promote the target — both within the caller's single
        transaction, so the partial one-primary-per-investment index
        (``uq_investment_identifiers_primary_per_investment``) sees exactly
        one ``TRUE`` row at every point. This centralises the demotion
        discipline the repository's ``set_primary`` leaves to its caller
        (ADR-0096 §3).

        Args:
            investment_id: The investment whose primary is being set. Used
                to scope the demotion to that investment's rows.
            identifier_id: The identifier row to promote.
            user_id: The acting user (audit context; the write itself is
                attributed through the session).

        Returns:
            ``True`` if the target row was promoted, ``False`` if no
            identifier with ``identifier_id`` belongs to the investment in
            the active tenant context (nothing is changed in that case).
        """
        repository = self._require_identifiers()
        rows = await repository.list_for_investment(investment_id)
        target = next((r for r in rows if r.id == identifier_id), None)
        if target is None:
            # Unknown id (or cross-tenant / cross-investment): no-op. The
            # route maps this to a 404.
            return False
        if target.is_primary:
            # Already primary — idempotent success, no index churn.
            return True
        current_primary = next((r for r in rows if r.is_primary), None)
        if current_primary is not None:
            await repository.set_primary(current_primary.id, is_primary=False)
        return await repository.set_primary(identifier_id, is_primary=True)

    async def delete_identifier(
        self,
        investment_id: UUID,
        identifier_id: UUID,
        user_id: UUID,
    ) -> bool:
        """Delete one identifier row from an investment.

        A plain delete. Deleting the **primary** is allowed and leaves the
        investment without a primary by design (ADR-0096 §3): live
        eligibility simply lapses — the market-linked predicate stops
        matching — with no compensating auto-promotion. The
        ``investment_id`` scopes the delete so a mismatched
        ``(investment, identifier)`` pair is reported as absence rather than
        deleting a sibling investment's row.

        Args:
            investment_id: The investment the identifier must belong to.
            identifier_id: The identifier row to delete.
            user_id: The acting user (audit context).

        Returns:
            ``True`` if a row was deleted, ``False`` if no identifier with
            ``identifier_id`` belongs to the investment in the active
            tenant context.
        """
        repository = self._require_identifiers()
        rows = await repository.list_for_investment(investment_id)
        if not any(r.id == identifier_id for r in rows):
            return False
        return await repository.delete(identifier_id)

    # ------------------------------------------------------------------
    # Group 2b: live-import write path (ADR-0092)
    # ------------------------------------------------------------------

    async def ingest_normalized_series(
        self,
        series: NormalizedSeries | NormalizedQuote,
        *,
        investment_id: UUID,
        user_id: UUID,
    ) -> LiveIngestReport:
        """Write one normalised provider series with Excel-precedence.

        The live-import counterpart to the Excel transform: it consumes a
        provider-blind :class:`~services.market_data.dto.NormalizedSeries`
        (or the single-point :class:`~services.market_data.dto.NormalizedQuote`,
        via its ``to_series()``) for a **known** ``investment_id`` and
        writes it into the same target tables the Excel extractor uses,
        under the Excel-precedence guard of ADR-0092: a live write
        overwrites no ``'excel'`` or ``'manual'`` row, refreshes only its
        own prior ``'live'`` rows, and inserts where the book of record is
        silent. Re-running the same series is a no-op (idempotency).

        Kind routing per the target investment's ``valuation_mode``
        (ADR-0098 §4; the ``SeriesKind`` value is value-identical to the
        target's canonical string, so no translation is needed):

        - ``nav_price`` + ``'unitised'`` → :meth:`InstrumentPriceRepository.upsert_live`
          on ``instrument_prices`` (a per-share price is a per-unit price,
          never a position-value ``investment_navs`` row — this closes
          finding F1). The computed-NAV materialisation (ADR-0098 §2–3) then
          runs synchronously in the same transaction, producing the
          ``basis='computed'`` / ``ingest_origin='system'`` NAV rows. The
          returned counts describe the **price** writes.
        - ``nav_price`` + ``'reported'`` (or unknown) → **refused**, counted
          on ``skipped_unit_mismatch``: a per-share price has no correct
          landing spot in a NAV-driven book row. Replaces the S0 blanket
          guard with a mode-aware refusal.
        - ``dividend`` / ``coupon`` + ``'unitised'`` → **scaled** to position
          level at the routing point (``amount = per-share ×
          holdings(as_of_date)``), then routed into ``investment_cashflows``
          under the unchanged ADR-0092 dedup-key idempotency (this closes
          finding F6). A date with zero holdings is skipped
          (``skipped_zero_holdings``).
        - ``dividend`` / ``coupon`` + ``'reported'`` (or unknown) → refused
          (``skipped_unit_mismatch``), same as ``nav_price``.
        - the five **position-level** cashflow kinds (``distribution``,
          ``capital_call``, ``fee``, ``carry``, ``other``) → unchanged for
          every mode: ``investment_cashflows`` with ``flow_kind='actual'``
          and ``flow_type`` = the kind. The DTO's value already carries the
          correct sign (the adapter normalises it at the edge, ADR-0091
          property 3 / ADR-0043 §3) — the ingest does **not** re-sign.
          Idempotency uses the deterministic dedup key
          (:mod:`services.investments.cashflow_dedup_key`) since the table
          has no unique constraint.
        - ``weight_*`` kinds → **not yet routed**: the DTO carries only
          ``(as_of_date, value)`` per point, with no dimension stating
          *which* bucket a weight belongs to, so a weight series is not
          yet expressible (services/market_data is frozen this slice,
          ADR-0091). The row-level guard already exists on the weight
          repositories (:meth:`InvestmentRegionWeightsRepository.upsert_live`)
          so the invariant is enforced the moment a bucketed-weight DTO
          lands. Raises :class:`NotImplementedError`.

        For both ``'unitised'`` re-routed paths a series whose currency
        differs from the investment currency is refused
        (``skipped_currency_mismatch``), never converted (ADR-0097 §5).

        ``source`` on every written row is the DTO's ``provider`` value;
        ``ingest_origin`` is ``'live'`` (materialised NAV rows carry
        ``'system'``).

        Args:
            series: The provider series (or quote) to ingest.
            investment_id: The already-resolved target investment. RLS
                scopes the write to the active tenant; a foreign-tenant
                ``investment_id`` simply matches no rows.
            user_id: The acting user (``created_by``). The dedicated
                system actor arrives with the tick slice (ADR-0093); any
                user id is accepted here.

        Returns:
            A :class:`LiveIngestReport` with per-outcome counts.

        Raises:
            NotImplementedError: For a ``weight_*`` kind (see above).
        """
        if isinstance(series, NormalizedQuote):
            series = series.to_series()

        kind = series.kind
        if kind == SeriesKind.NAV_PRICE or kind in _PER_SHARE_FLOW_KINDS:
            # Per-share kinds need the target's valuation_mode and currency to
            # route (ADR-0098 §4). An unknown investment (foreign tenant / not
            # found) is treated as non-unitised: refused, never written.
            investment = await self._investments.get_by_id(investment_id)
            mode = investment.valuation_mode if investment else "reported"
            if mode != "unitised":
                # Reported-mode (or unknown) per-share ingest is refused: a
                # per-share magnitude has no correct landing spot in a
                # NAV-driven book (findings F1/F6, ADR-0098 §4).
                report = LiveIngestReport(skipped_unit_mismatch=len(series.points))
            elif series.currency != investment.currency:
                # Currency equality is required, never converted (ADR-0097 §5).
                report = LiveIngestReport(skipped_currency_mismatch=len(series.points))
            elif kind == SeriesKind.NAV_PRICE:
                report = await self._ingest_live_instrument_prices(
                    series, investment_id=investment_id, user_id=user_id
                )
            else:
                report = await self._ingest_live_per_share_flows(
                    series, investment_id=investment_id, user_id=user_id
                )
        elif kind in _POSITION_LEVEL_CASHFLOW_KINDS:
            report = await self._ingest_live_cashflows(
                series, investment_id=investment_id, user_id=user_id
            )
        else:
            raise NotImplementedError(
                f"Live ingest of weight kind {kind.value!r} is not yet "
                "supported: the NormalizedSeries DTO carries no bucket "
                "dimension stating which weight bucket each point belongs "
                "to (ADR-0091 / ADR-0092). The row-level Excel-precedence "
                "guard already exists on the weight repositories. NAV and "
                "the seven cashflow kinds are supported."
            )

        _LOG.info(
            "ingest_normalized_series: investment=%s provider=%s kind=%s "
            "points=%d inserted=%d updated_live=%d skipped_excel=%d "
            "skipped_manual=%d noop_live=%d skipped_unit_mismatch=%d "
            "skipped_currency_mismatch=%d skipped_zero_holdings=%d",
            investment_id,
            series.provider,
            kind.value,
            len(series.points),
            report.inserted,
            report.updated_live,
            report.skipped_excel,
            report.skipped_manual,
            report.noop_live,
            report.skipped_unit_mismatch,
            report.skipped_currency_mismatch,
            report.skipped_zero_holdings,
        )
        return report

    async def _ingest_live_instrument_prices(
        self,
        series: NormalizedSeries,
        *,
        investment_id: UUID,
        user_id: UUID,
    ) -> LiveIngestReport:
        """Ingest a unitised ``nav_price`` series into ``instrument_prices``.

        A per-share price is a per-unit price: it lands in
        ``instrument_prices`` (never ``investment_navs`` — that is finding
        F1). Reads the investment's existing price rows once, classifies each
        incoming point against the row on its ``as_of_date``, and writes only
        where the Excel-precedence guard permits (mirroring
        :meth:`InstrumentPriceRepository.upsert_live` semantics). An unchanged
        ``'live'`` row is a pure no-op — no ``upsert_live`` is issued, so
        ``updated_at`` is not bumped and re-runs stay byte-identical.

        After the price writes, the computed-NAV materialisation (ADR-0098
        §2–3) runs synchronously in the same transaction, from the earliest
        touched date onward — so the ``instrument_prices`` and the derived
        ``investment_navs`` rows can never be observed disagreeing. The
        returned :class:`LiveIngestReport` counts the **price** outcomes; the
        materialisation's own counts are logged by that service.

        Reached only for a ``'unitised'`` investment whose currency matches
        the series currency (both gated by the caller).
        """
        prices = self._require_instrument_prices()
        existing = await prices.list_by_investment(investment_id)
        by_date = {p.as_of_date: p for p in existing}
        inserted = updated_live = 0
        skipped_excel = skipped_manual = noop_live = 0

        for point in series.points:
            current = by_date.get(point.as_of_date)
            if current is None:
                await prices.upsert_live(
                    investment_id=investment_id,
                    as_of_date=point.as_of_date,
                    price=point.value,
                    currency=series.currency,
                    source=series.provider,
                    created_by=user_id,
                )
                inserted += 1
            elif current.ingest_origin == "live":
                if current.price == point.value and current.currency == series.currency:
                    # Identical live price already present — no write, so
                    # updated_at is untouched (idempotency).
                    noop_live += 1
                else:
                    await prices.upsert_live(
                        investment_id=investment_id,
                        as_of_date=point.as_of_date,
                        price=point.value,
                        currency=series.currency,
                        source=series.provider,
                        created_by=user_id,
                    )
                    updated_live += 1
            elif current.ingest_origin == "manual":
                skipped_manual += 1
            else:
                # 'excel' — book of record; also the safe default for any
                # unexpected origin (never overwrite what is not live).
                skipped_excel += 1

        if series.points:
            # Materialisation trigger (ADR-0098 §3): recompute the computed
            # NAVs from the earliest price this ingest touched onward — a
            # price change here cannot alter holdings × price on any earlier
            # date. Idempotent, so running it after a fully-skipped ingest is
            # harmless.
            since = min(point.as_of_date for point in series.points)
            await self._nav_materialiser().materialise(
                investment_id, acting_user=user_id, since=since
            )

        return LiveIngestReport(
            inserted=inserted,
            updated_live=updated_live,
            skipped_excel=skipped_excel,
            skipped_manual=skipped_manual,
            noop_live=noop_live,
        )

    async def _ingest_live_per_share_flows(
        self,
        series: NormalizedSeries,
        *,
        investment_id: UUID,
        user_id: UUID,
    ) -> LiveIngestReport:
        """Ingest a unitised per-share ``dividend`` / ``coupon`` series.

        The DTO value is per-unit; ``investment_cashflows.amount`` is
        position-level. Each point is scaled at the routing point —
        ``amount = per-share value × holdings(as_of_date)`` (findings F6) —
        using the pure holdings derivation over the ledger
        (:func:`services.investments.holdings.holdings_as_of`). A date whose
        derived holdings are zero has nothing to scale by and is skipped
        (``skipped_zero_holdings``). The scaled points then route into
        ``investment_cashflows`` under the unchanged ADR-0092 dedup-key
        idempotency (the key is formed over the **scaled** amount, the value
        actually stored).

        Reached only for a ``'unitised'`` investment whose currency matches
        the series currency (both gated by the caller).
        """
        transactions = await self._require_position_transactions().list_for_investment(
            investment_id
        )
        skipped_zero_holdings = 0
        scaled: list[tuple[_date, Decimal]] = []
        for point in series.points:
            held = holdings_as_of(transactions, point.as_of_date)
            if held <= 0:
                # No position on the ex-date — nothing to scale by (a
                # per-share flow with zero holdings is not a position event).
                skipped_zero_holdings += 1
                continue
            scaled.append((point.as_of_date, point.value * held))

        inserted, skipped_excel, skipped_manual, noop_live = await self._write_live_cashflow_points(
            investment_id=investment_id,
            flow_type=series.kind.value,
            currency=series.currency,
            provider=series.provider,
            points=scaled,
            user_id=user_id,
        )
        return LiveIngestReport(
            inserted=inserted,
            skipped_excel=skipped_excel,
            skipped_manual=skipped_manual,
            noop_live=noop_live,
            skipped_zero_holdings=skipped_zero_holdings,
        )

    async def _ingest_live_cashflows(
        self,
        series: NormalizedSeries,
        *,
        investment_id: UUID,
        user_id: UUID,
    ) -> LiveIngestReport:
        """Ingest a position-level cashflow-kind series into ``investment_cashflows``.

        The DTO value is already position-scaled (private-markets-style
        providers), so it routes unchanged for every valuation mode
        (ADR-0098 §4). Idempotency is the shared ADR-0092 dedup-key logic in
        :meth:`_write_live_cashflow_points`.
        """
        inserted, skipped_excel, skipped_manual, noop_live = await self._write_live_cashflow_points(
            investment_id=investment_id,
            flow_type=series.kind.value,
            currency=series.currency,
            provider=series.provider,
            points=[(p.as_of_date, p.value) for p in series.points],
            user_id=user_id,
        )
        return LiveIngestReport(
            inserted=inserted,
            skipped_excel=skipped_excel,
            skipped_manual=skipped_manual,
            noop_live=noop_live,
        )

    async def _write_live_cashflow_points(
        self,
        *,
        investment_id: UUID,
        flow_type: str,
        currency: str,
        provider: str,
        points: Iterable[tuple[_date, Decimal]],
        user_id: UUID,
    ) -> tuple[int, int, int, int]:
        """Write ``(date, amount)`` points into ``investment_cashflows`` (ADR-0092).

        The shared write core behind both the position-level ingest and the
        unitised per-share scaling: the table has no unique key (ADR-0043
        §1), so idempotency uses the deterministic dedup key (ADR-0092).
        Existing rows are indexed by their key → set of ``ingest_origin``
        values (several rows can share a key). An incoming point whose key
        matches an ``'excel'`` or ``'manual'`` row is skipped (authoritative);
        a match against only ``'live'`` rows is a no-op; no match is a fresh
        ``'live'`` insert. A live write only ever inserts — it never mutates
        an existing row, so Excel / manual rows are immune by construction.

        Args:
            investment_id: The target investment.
            flow_type: One of the seven canonical ``flow_type`` values.
            currency: ISO 4217 currency code stored on inserts.
            provider: The DTO's provider name — stored as ``source`` and part
                of the dedup key.
            points: The ``(as_of_date, amount)`` pairs to write; ``amount``
                already carries the correct sign and (for per-share kinds) the
                position-level scaling.
            user_id: ``created_by`` on inserts.

        Returns:
            ``(inserted, skipped_excel, skipped_manual, noop_live)`` counts.
        """
        existing = await self._cashflows.list_by_investment(investment_id)
        origins_by_key: dict[str, set[str]] = {}
        for cf in existing:
            key = compute_cashflow_dedup_key(
                investment_id=investment_id,
                flow_timestamp=cf.flow_timestamp,
                flow_type=cf.flow_type,
                flow_kind=cf.flow_kind,
                amount=cf.amount,
                source=cf.source,
            )
            origins_by_key.setdefault(key, set()).add(cf.ingest_origin)

        inserted = skipped_excel = skipped_manual = noop_live = 0
        for as_of_date, amount in points:
            # 12:00 UTC when the wall-clock time is unknown — the same
            # convention the extractor uses (ADR-0043 §3).
            flow_timestamp = datetime.combine(as_of_date, time(12, 0), tzinfo=timezone.utc)
            key = compute_cashflow_dedup_key(
                investment_id=investment_id,
                flow_timestamp=flow_timestamp,
                flow_type=flow_type,
                flow_kind="actual",
                amount=amount,
                source=provider,
            )
            origins = origins_by_key.get(key)
            if origins is None:
                await self._cashflows.create(
                    investment_id=investment_id,
                    flow_timestamp=flow_timestamp,
                    flow_type=flow_type,
                    flow_kind="actual",
                    amount=amount,
                    currency=currency,
                    description=None,
                    created_by=user_id,
                    source=provider,
                    ingest_origin="live",
                )
                inserted += 1
                # A later point in this same series that produced the same
                # key would now correctly no-op rather than double-insert.
                origins_by_key[key] = {"live"}
            elif "excel" in origins:
                skipped_excel += 1
            elif "manual" in origins:
                skipped_manual += 1
            else:
                noop_live += 1

        return inserted, skipped_excel, skipped_manual, noop_live

    # ------------------------------------------------------------------
    # Group 3: Excel-import transformation (sub-stream 4c, ADR-0043 §3)
    # ------------------------------------------------------------------

    async def _ensure_sectors_for_labels(
        self,
        *,
        extractor: InvestmentExtractor,
        upload_sheets: dict[str, dict],
        sector_repository: SectorRepository,
        user_id: UUID,
    ) -> dict[str, UUID]:
        """Build the sector lookup map, auto-creating any missing entries.

        Excel is the source of truth for the sector vocabulary at
        import time. Labels surfaced by
        :meth:`InvestmentExtractor.partition_attributes_from_sheets`
        that do not resolve against the per-tenant sector catalogue
        are created with a normalised code (e.g. ``"Technology &
        Software"`` → ``"technology_software"``) and the original
        Excel label as ``display_name``. This mirrors the asset-class
        auto-create path in :func:`resolve_asset_class` below.
        """
        existing = await sector_repository.list_active()
        lookup: dict[str, UUID] = {}
        for s in existing:
            lookup[s.display_name.lower()] = s.id
            lookup[s.code.lower()] = s.id

        partition = extractor.partition_attributes_from_sheets(upload_sheets)
        for raw_label in partition.sector_rows:
            cleaned = str(raw_label).strip()
            if not cleaned:
                continue
            key = cleaned.lower()
            if key in lookup:
                continue
            new_code = _normalise_asset_class_code(cleaned)
            if not new_code:
                continue
            if new_code in lookup:
                lookup[key] = lookup[new_code]
                continue
            _LOG.info(
                "transform: auto-creating sector %r (code %r) from Excel value.",
                cleaned,
                new_code,
            )
            try:
                async with sector_repository._session.begin_nested():
                    created = await sector_repository.create(
                        code=new_code,
                        display_name=cleaned,
                        created_by=user_id,
                    )
            except IntegrityError:
                # Race: a parallel transaction created the same code
                # first. Re-fetch and use the existing row.
                existing_after = await sector_repository.get_by_code(new_code)
                if existing_after is None:
                    raise
                lookup[key] = existing_after.id
                lookup[new_code] = existing_after.id
                continue
            lookup[key] = created.id
            lookup[new_code] = created.id

        return lookup

    async def _reconcile_excel_identifiers(
        self,
        *,
        targets: list[tuple[UUID, str, tuple[ImportedIdentifier, ...]]],
        repository: InvestmentIdentifierRepository,
        user_id: UUID,
    ) -> None:
        """Reconcile the ``source='excel'`` identifier subset per investment.

        Excel is the book of record for its own identifier rows
        (ADR-0090 §"Identifiers enter through both import paths"). For
        each imported investment:

        - the desired set is the normalised ``(scheme, value)`` pairs
          from the workbook;
        - ``source='excel'`` rows not in the desired set are deleted;
        - desired pairs absent from the current rows (any source) are
          inserted with ``source='excel'``;
        - rows with any other ``source`` (``openfigi`` / ``manual``) are
          never deleted or re-inserted.

        All deletions across the whole import are issued before any
        insertion so an identifier moving from one investment to another
        between workbook versions cannot transiently violate the partial
        per-tenant unique index.

        Finally, each investment lacking a primary identifier has one
        promoted, **type-aware** (the live-tick path addresses providers by
        the primary identifier, and the only wired adapter — Yahoo — routes
        ``ticker`` only, ADR-0091):

        - a market-linked type (:data:`MARKET_LINKED_TYPES`:
          ``listed_equity`` / ``listed_bonds``) prefers the **ticker** row,
          falling back to the ISIN row;
        - every other type prefers the **ISIN** row, falling back to the
          ticker row.

        Either way, ``figi`` / ``cusip`` / ``internal`` rows are never
        promoted, and an investment that already has a primary (whatever the
        source) is left untouched — so an operator's manual primary choice
        survives every re-import.

        Args:
            targets: ``(investment_id, investment_type, identifiers)`` tuples
                collected in the write loop — one per investment present in
                the workbook. ``investment_type`` selects the promotion
                preference above.
            repository: The tenant-scoped identifier repository.
            user_id: Acting user; ``created_by`` on inserted rows.
        """
        # Phase 1: read current state and compute the delete / insert
        # sets without issuing any write yet.
        deletes: list[UUID] = []
        inserts: list[tuple[UUID, str, str]] = []
        for investment_id, _investment_type, identifiers in targets:
            existing = await repository.list_for_investment(investment_id)
            desired: set[tuple[str, str]] = {
                (idf.scheme, _normalise_identifier_value(idf.value)) for idf in identifiers
            }
            existing_pairs = {(r.scheme, r.value) for r in existing}
            for row in existing:
                if row.source == "excel" and (row.scheme, row.value) not in desired:
                    deletes.append(row.id)
            for scheme, value in desired:
                if (scheme, value) not in existing_pairs:
                    inserts.append((investment_id, scheme, value))

        # Phase 2: all deletions first.
        for identifier_id in deletes:
            await repository.delete(identifier_id)

        # Phase 3: then all insertions.
        for investment_id, scheme, value in inserts:
            await repository.add(
                investment_id=investment_id,
                scheme=scheme,
                value=value,
                created_by=user_id,
                source="excel",
            )

        # Phase 4: promote a primary for any investment lacking one, with a
        # type-aware preference. A market-linked investment prefers its ticker
        # (the only wired live adapter, Yahoo, routes ticker only — ADR-0091),
        # everything else prefers its ISIN. The `any(is_primary)` guard means a
        # manually chosen primary is never overridden across re-imports.
        for investment_id, investment_type, _identifiers in targets:
            rows = await repository.list_for_investment(investment_id)
            if any(r.is_primary for r in rows):
                continue
            isin_row = next((r for r in rows if r.scheme == "isin"), None)
            ticker_row = next((r for r in rows if r.scheme == "ticker"), None)
            if investment_type in MARKET_LINKED_TYPES:
                promote = ticker_row if ticker_row is not None else isin_row
            else:
                promote = isin_row if isin_row is not None else ticker_row
            if promote is not None:
                await repository.set_primary(promote.id)

    async def _reconcile_excel_openings(
        self,
        *,
        targets: list[tuple[UUID, Decimal, _date, str]],
        user_id: UUID,
    ) -> None:
        """Reconcile each investment's single ``excel`` opening (ADR-0097 §7).

        Excel is the book of record for its own opening rows. For every
        investment carrying a ``Units`` row:

        - **no opening** → create one through
          :meth:`add_position_transaction`, the single sanctioned write
          seam (currency equality, the non-negativity invariant, and — for
          an already-unitised investment — computed-NAV materialisation all
          run there). The row is ``txn_type='opening'``,
          ``ingest_origin='excel'``, ``price_per_unit=NULL``.
        - **an ``excel`` opening whose count and date are unchanged** →
          no-op (``Decimal`` equality ignores scale).
        - **an ``excel`` opening that changed** → update in place
          (:meth:`PositionTransactionRepository.update_opening`), after
          re-checking the non-negativity invariant; a *unitised* investment
          is then re-materialised from the earliest affected date.
        - **an opening from another producer** (``manual`` / ``live``) →
          left untouched, mirroring how the identifier reconcile never
          rewrites non-excel rows. Structurally impossible before strand S5
          introduces a manual opening path, but guarded for forward safety.

        A second opening is never created — the partial unique index
        ``uq_position_transactions_opening`` is the structural backstop and
        this reconcile is the polite path. The import never flips
        ``valuation_mode``; materialisation therefore fires only on
        re-import of an investment an operator has already flipped.

        Args:
            targets: ``(investment_id, units, units_as_of, currency)`` tuples
                collected in the write loop — one per investment with a
                units row.
            user_id: Acting user; ``created_by`` on a created opening and the
                actor for any triggered materialisation.
        """
        repository = self._require_position_transactions()
        for investment_id, units, trade_date, currency in targets:
            existing = await repository.get_opening(investment_id)

            if existing is None:
                await self.add_position_transaction(
                    investment_id=investment_id,
                    txn_type="opening",
                    trade_date=trade_date,
                    units=units,
                    currency=currency,
                    ingest_origin="excel",
                    created_by=user_id,
                    price_per_unit=None,
                    source="excel-import",
                )
                continue

            if existing.ingest_origin != "excel":
                continue

            if existing.units == units and existing.trade_date == trade_date:
                continue

            # Update in place. Re-check the non-negativity invariant over the
            # ledger with the opening replaced by its restated values
            # (trivially satisfied while only openings exist, but correct
            # once strand S5 adds sells).
            others = [
                t
                for t in await repository.list_for_investment(investment_id)
                if t.id != existing.id
            ]
            candidate = _LedgerCandidate(
                txn_type="opening",
                trade_date=trade_date,
                units=units,
                created_at=existing.created_at,
                id=existing.id,
            )
            offending = first_negative_holding_date([*others, candidate])
            if offending is not None:
                raise NonNegativeHoldingsError(
                    f"Restated opening would drive holdings below zero on "
                    f"{offending} for investment {investment_id}; short "
                    "positions are out of scope (ADR-0097 §4).",
                    field="units",
                )

            await repository.update_opening(
                existing.id,
                units=units,
                trade_date=trade_date,
                price_per_unit=None,
            )

            # Re-materialise a unitised investment from the earliest affected
            # date (ADR-0098 §3); a 'reported' investment triggers nothing and
            # stays byte-identical.
            investment = await self._investments.get_by_id(investment_id)
            if investment is not None and investment.valuation_mode == "unitised":
                await self._nav_materialiser().materialise(
                    investment_id,
                    acting_user=user_id,
                    since=min(existing.trade_date, trade_date),
                )

    async def _reconcile_cash_statements(
        self,
        *,
        targets: list[tuple[UUID, tuple[ImportedCashStatement, ...]]],
        user_id: UUID,
        extractor: InvestmentExtractor,
    ) -> _CashReconcileCounts:
        """Derive each cash position's ledger from its statements (ADR-0103 §4).

        The ``Cash`` sheet reports **levels**; the ledger stores **flows**.
        This is the seam that converts one into the other, per cash position
        with a statement series:

        1. **First statement date** → the single ``'excel'`` ``opening``,
           ``units = balance``, ``price_per_unit = NULL``.
        2. **Every subsequent date** → one ``'excel'`` ``transfer`` of
           ``balance(t) − balance(t−1)``, signed. **A zero delta writes
           nothing** — an unchanged balance is not an event.
        3. **Every date, the first included** → one unity
           ``instrument_prices`` row (ADR-0103 §1), so the unchanged
           ADR-0098 service materialises ``holdings × 1 = balance`` with no
           cash branch anywhere in the book path.

        Interest, fees and sweeps need no separate modelling: the statement
        balance already contains them and the delta transfer carries them
        implicitly.

        **Idempotency — classify-then-write.** The importer owns this
        investment's ``'excel'``-origin ``opening``, ``transfer`` and price
        rows and nothing else. Each set is compared against the target
        derived from the sheet: insert what is missing, restate what
        changed, delete what the sheet no longer carries. ``'manual'`` rows
        (and any other foreign origin) are never read as targets and never
        written. An unchanged sheet therefore re-imports as a **full
        no-op** — byte-identical ledger and price state, no ``updated_at``
        bumped, every counter zero (ADR-0103 §4).

        **Ordering.** Prices are written before the ledger so any
        materialisation pass sees the complete price set.

        **Why the ledger diff does not run through**
        :meth:`add_position_transaction`. That seam re-checks the ADR-0097
        §4 non-negativity invariant *per write*, against the ledger as it
        stands at that moment. During a diff the ledger is transiently a
        mixture of restated and not-yet-restated rows, and such a mixture
        can dip negative even when both the old and the new ledger are
        perfectly valid — a restated opening applied before its dependent
        transfers, or an inserted deposit whose matching withdrawal is
        already present, would abort an entirely legitimate import. The
        invariant is a property of the **committed** ledger, so it is
        enforced here exactly once, over the full target, with the same
        pure :func:`first_negative_holding_date` the seam uses; the currency
        equality rule (ADR-0097 §5) is likewise checked once per position.
        The seam's third service — the in-transaction materialisation
        trigger — is subsumed by the explicit full recompute below, which
        this path needs regardless.

        **Why the recompute is full, not bounded.** The transform's
        replace-by-investment step (:meth:`InvestmentNavRepository
        .delete_by_investment`) has already cleared *every* NAV row of this
        investment, ``'system'`` rows included, before this pass runs. A
        ``since``-bounded materialisation would restore only the dates from
        ``since`` onward and leave the earlier history without NAVs, so the
        recompute deliberately runs unbounded. Ledger and price state stay
        byte-identical across a no-op re-import; the NAV rows are
        value-identical but freshly created, exactly as they are for every
        other Excel-imported investment.

        A cash row still in ``'reported'`` mode (an ADR-0100 row whose §9
        migration has not yet run) gets its ledger and prices written
        normally — they materialise nothing while ``'reported'``, an
        ADR-0098 no-op — and one warning pointing at the migration. The
        import never flips ``valuation_mode`` (ADR-0097); the row keeps its
        statement balances as ordinary ``'excel'`` NAV rows until it does.

        Args:
            targets: ``(investment_id, statements)`` pairs collected in the
                write loop — one per cash position carrying a Cash-sheet
                column. ``statements`` is non-empty and date-ordered.
            user_id: Acting user; ``created_by`` on every written row and the
                actor for the materialisation.
            extractor: The extraction run, for appending soft warnings to.

        Returns:
            The per-outcome :class:`_CashReconcileCounts` for the operator
            surface — all zero on an unchanged re-import.

        Raises:
            CurrencyMismatchError: If a stored price row on this position is
                denominated in another currency than the position itself.
            NonNegativeHoldingsError: If the target ledger drives holdings
                below zero on any date — structurally impossible from
                statements alone (balances are validated non-negative), so
                only a foreign ledger row can trigger it.
        """
        prices_repo = self._require_instrument_prices()
        ledger_repo = self._require_position_transactions()
        counts = _CashReconcileCounts()
        affected_currencies: set[str] = set()

        for investment_id, statements in targets:
            investment = await self._investments.get_by_id(investment_id)
            if investment is None or not statements:
                continue
            affected_currencies.add(investment.currency)

            if investment.valuation_mode != "unitised":
                extractor._warnings.append(
                    ExtractionWarning(
                        investment_name=investment.name,
                        field="valuation_mode",
                        raw_value=investment.valuation_mode,
                        action="cash_not_yet_unitised",
                        message=(
                            f"{investment.name!r} is a cash position still in "
                            "'reported' valuation mode. Its ledger and unity "
                            "prices were written, but its balances cannot be "
                            "computed from them until the ADR-0103 §9 "
                            "migration flips it to 'unitised' — until then it "
                            "keeps its statement balances as imported NAV "
                            "rows. The import never flips the mode itself."
                        ),
                    )
                )

            currency = investment.currency
            counts += await self._reconcile_cash_prices(
                investment_id=investment_id,
                currency=currency,
                statements=statements,
                repository=prices_repo,
                user_id=user_id,
            )
            counts += await self._reconcile_cash_ledger(
                investment_id=investment_id,
                currency=currency,
                statements=statements,
                repository=ledger_repo,
                user_id=user_id,
            )

            # Full recompute — see the docstring: the transform has already
            # wiped this investment's NAV rows, so a bounded window would
            # leave the earlier history unmaterialised. A 'reported' row is
            # an all-zero no-op here (ADR-0098 §2).
            await self._nav_materialiser().materialise(
                investment_id, acting_user=user_id, since=None
            )

        # Cash plan-path trigger (ADR-0103 §6). A new statement moves the
        # anchor t₀, which re-bases every projected date and strands the ones
        # the statement has now overtaken — so the recompute is unbounded
        # (``since=None``), and it runs *after* the actual-NAV materialisation
        # above, which is what produces the anchor it reads.
        #
        # The transform runs a full recompute of its own after this pass (the
        # plan-flow trigger, step 6e): a workbook can move both the anchor and
        # the events. That later run finds this one's rows already correct and
        # is a value-equal no-op on them; this trigger sits here so the
        # statement reconcile is correct in its own right rather than by
        # depending on its caller.
        await self._cash_plan_materialiser().materialise_currencies(
            affected_currencies, acting_user=user_id, since=None
        )

        return counts

    async def _reconcile_cash_prices(
        self,
        *,
        investment_id: UUID,
        currency: str,
        statements: tuple[ImportedCashStatement, ...],
        repository: InstrumentPriceRepository,
        user_id: UUID,
    ) -> _CashReconcileCounts:
        """Make the ``'excel'`` price set exactly one unity row per statement.

        The unity price is the *definition* of a cash position's price
        (ADR-0103 §1), so every candidate goes through
        :func:`~services.investments.unity_price.unity_price_violation`
        before it is written. A violation here is a programming error, not
        operator input — this importer writes nothing but
        :data:`~services.investments.unity_price.UNITY_PRICE` in the
        position's own currency — hence the assertion rather than a warning.

        Value-equal rows are left untouched (no write, no ``updated_at``
        bump); rows whose date left the sheet are deleted. Only
        ``'excel'``-origin rows are ever written or deleted — cash is
        permanently ineligible for live ingest (ADR-0103 §1), so a foreign
        origin here is a provisioning fault, left in place rather than
        silently overwritten.
        """
        violation = unity_price_violation(UNITY_PRICE, currency, currency)
        assert violation is None, (
            f"The Cash-sheet importer built an illegal unity price: {violation}"
        )

        existing = await repository.list_by_investment(investment_id)
        existing_by_date: dict[_date, InstrumentPriceDTO] = {
            row.as_of_date: row for row in existing
        }
        target_dates = {s.statement_date for s in statements}

        written = 0
        for as_of_date in sorted(target_dates):
            current = existing_by_date.get(as_of_date)
            if current is not None:
                if current.ingest_origin != "excel":
                    # Not ours to restate. Cash can carry no 'live' price by
                    # construction; leave the row and let the mismatch show.
                    continue
                if current.price == UNITY_PRICE and current.currency == currency:
                    continue  # already unity — the no-op path
            await repository.upsert(
                investment_id=investment_id,
                as_of_date=as_of_date,
                price=UNITY_PRICE,
                currency=currency,
                source="excel-import:cash-statement",
                created_by=user_id,
                ingest_origin="excel",
            )
            written += 1

        deleted = 0
        for row in existing:
            if row.ingest_origin != "excel":
                continue
            if row.as_of_date in target_dates:
                continue
            await repository.delete(row.id)
            deleted += 1

        return _CashReconcileCounts(prices_written=written, prices_deleted=deleted)

    async def _reconcile_cash_ledger(
        self,
        *,
        investment_id: UUID,
        currency: str,
        statements: tuple[ImportedCashStatement, ...],
        repository: PositionTransactionRepository,
        user_id: UUID,
    ) -> _CashReconcileCounts:
        """Make the ``'excel'`` ledger exactly the statement series' flows.

        The target is fully determined by the statements: one ``opening`` at
        the first date carrying that balance, and one ``transfer`` per
        subsequent date carrying the delta to the previous statement (zero
        deltas excluded — they are not events). Classify-then-write against
        the existing ``'excel'`` rows; foreign-origin rows are neither read
        as targets nor written.

        Both ADR-0097 invariants the write seam normally enforces are
        enforced here, once, over the **target** ledger — see
        :meth:`_reconcile_cash_statements` for why per-write enforcement is
        wrong during a diff.
        """
        target_opening = statements[0]
        target_transfers: dict[_date, Decimal] = {}
        previous = target_opening.balance
        for statement in statements[1:]:
            delta = statement.balance - previous
            if delta != 0:
                target_transfers[statement.statement_date] = delta
            previous = statement.balance

        existing = await repository.list_for_investment(investment_id)
        existing_opening = next((t for t in existing if t.txn_type == "opening"), None)
        opening_is_ours = existing_opening is not None and existing_opening.ingest_origin == "excel"

        # Our transfer rows, keyed by date. A second 'excel' transfer on the
        # same date cannot arise from this importer (one row per statement
        # date); should one exist, it is stranded and deleted below.
        excel_transfers: dict[_date, PositionTransactionDTO] = {}
        duplicates: list[PositionTransactionDTO] = []
        for txn in existing:
            if txn.txn_type != "transfer" or txn.ingest_origin != "excel":
                continue
            if txn.trade_date in excel_transfers:
                duplicates.append(txn)
            else:
                excel_transfers[txn.trade_date] = txn

        # ADR-0097 §5: the ledger currency is the position's, never
        # converted. Statements are extracted in the position currency by
        # construction, so this can only fail on a malformed foreign row.
        foreign_currency = next((t for t in existing if t.currency != currency), None)
        if foreign_currency is not None:
            raise CurrencyMismatchError(
                f"Ledger row {foreign_currency.id} on cash position "
                f"{investment_id} is denominated in "
                f"{foreign_currency.currency!r}, not the position's "
                f"{currency!r}; cross-currency positions are not supported "
                "(ADR-0097 §5).",
                field="currency",
            )

        # ADR-0097 §4, checked once against the committed target: every row
        # that survives untouched, plus the target opening and transfers.
        now = datetime.now(timezone.utc)
        retained = [
            txn
            for txn in existing
            if not (txn.txn_type == "transfer" and txn.ingest_origin == "excel")
            and not (
                opening_is_ours and existing_opening is not None and txn.id == existing_opening.id
            )
        ]
        write_opening = existing_opening is None or opening_is_ours
        candidates: list = list(retained)
        if write_opening:
            candidates.append(
                _LedgerCandidate(
                    txn_type="opening",
                    trade_date=target_opening.statement_date,
                    units=target_opening.balance,
                    created_at=now,
                    id=uuid4(),
                )
            )
        candidates.extend(
            _LedgerCandidate(
                txn_type="transfer",
                trade_date=trade_date,
                units=units,
                created_at=now,
                id=uuid4(),
            )
            for trade_date, units in target_transfers.items()
        )
        offending = first_negative_holding_date(candidates)
        if offending is not None:
            raise NonNegativeHoldingsError(
                f"The statement-derived ledger would drive holdings below "
                f"zero on {offending} for cash position {investment_id}; a "
                "foreign ledger row must be conflicting with the statement "
                "series (statement balances are themselves non-negative). "
                "Short positions are out of scope (ADR-0097 §4).",
                field="units",
            )

        inserted = updated = deleted = 0

        # 1) The opening. A foreign-origin opening is left untouched — the
        #    same rule the units-row reconcile follows (ADR-0097 §7).
        if existing_opening is None:
            await repository.add(
                investment_id=investment_id,
                txn_type="opening",
                trade_date=target_opening.statement_date,
                units=target_opening.balance,
                currency=currency,
                ingest_origin="excel",
                created_by=user_id,
                price_per_unit=None,
                consideration=None,
                source="excel-import:cash-statement",
            )
            inserted += 1
        elif opening_is_ours and (
            existing_opening.units != target_opening.balance
            or existing_opening.trade_date != target_opening.statement_date
        ):
            await repository.update_opening(
                existing_opening.id,
                units=target_opening.balance,
                trade_date=target_opening.statement_date,
                price_per_unit=None,
            )
            updated += 1

        # 2) The transfers.
        for trade_date in sorted(target_transfers):
            units = target_transfers[trade_date]
            current = excel_transfers.get(trade_date)
            if current is None:
                await repository.add(
                    investment_id=investment_id,
                    txn_type="transfer",
                    trade_date=trade_date,
                    units=units,
                    currency=currency,
                    ingest_origin="excel",
                    created_by=user_id,
                    price_per_unit=None,
                    consideration=None,
                    source="excel-import:cash-statement",
                )
                inserted += 1
            elif current.units != units:
                await repository.update(
                    current.id,
                    trade_date=trade_date,
                    units=units,
                    price_per_unit=None,
                    consideration=None,
                    source="excel-import:cash-statement",
                )
                updated += 1

        # 3) Stranded transfers — a statement date the sheet no longer
        #    carries, or whose delta collapsed to zero.
        for txn in [
            *duplicates,
            *(
                row
                for trade_date, row in excel_transfers.items()
                if trade_date not in target_transfers
            ),
        ]:
            await repository.delete(txn.id)
            deleted += 1

        return _CashReconcileCounts(
            ledger_inserted=inserted,
            ledger_updated=updated,
            ledger_deleted=deleted,
        )

    async def transform_upload_to_investments(
        self,
        upload_id: UUID,
        *,
        user_id: UUID,
        asset_class_repository: AssetClassRepository,
        data_upload_repository: DataUploadRepository,
        extractor: InvestmentExtractor,
        region_repository: RegionRepository | None = None,
        sector_repository: SectorRepository | None = None,
        region_weights_repository: (InvestmentRegionWeightsRepository | None) = None,
        sector_weights_repository: (InvestmentSectorWeightsRepository | None) = None,
        investment_identifier_repository: (InvestmentIdentifierRepository | None) = None,
        bond_analytics_repository: (InvestmentBondAnalyticsRepository | None) = None,
        rating_weights_repository: (InvestmentRatingWeightsRepository | None) = None,
        maturity_weights_repository: (InvestmentMaturityWeightsRepository | None) = None,
        anlv_category_repository: AnlVCategoryRepository | None = None,
        limits_repository: LimitsRepository | None = None,
        dry_run: bool = False,
    ) -> InvestmentExtractionResult:
        """Read a previously uploaded Excel snapshot and write it normalised.

        Per ADR-0043 §3 the transformation is **replace-by-investment
        (B1.1)** plus **soft-delete-with-reactivation (B2.b)**:

        - Existing NAVs and cashflows for each matched investment are
          deleted and re-inserted from the Excel snapshot. Manual
          single-row edits made between imports are deliberately
          overwritten — the Excel file is the authoritative source.
        - Investments present in the tenant but absent from the Excel
          snapshot are set to ``is_active = FALSE``. Investments that
          reappear are reactivated (``is_active = TRUE``).
        - The whole pass is **idempotent** for a fixed input: a
          repeated invocation produces the same final DB state
          (audit-log entries excepted).

        Transactional semantics: the caller wraps this method in a
        single tenant-scoped session — by convention provided via
        :func:`core.repositories.tenant_context` — so a database error
        anywhere in the loop rolls back the whole transformation. The
        Phase-2 ``data_uploads`` row is therefore untouched and the
        operator can re-attempt after a fix. **Row-level extraction
        errors do not abort.** Per the ADR-0043 §3 partial-success
        convention, extractable investments are written and
        non-extractable ones are reported on
        :attr:`InvestmentExtractionResult.errors`.

        Args:
            upload_id: UUID of a row in ``data_uploads`` in the active
                tenant. RLS hides cross-tenant rows; the service
                surfaces an absent upload as
                :class:`UploadNotFoundError`.
            user_id: The acting user. Becomes ``created_by`` for new
                rows, is reflected in audit-log entries (the session
                already binds ``app.user_id`` to this value), and is
                passed to the per-row repository writes.
            asset_class_repository: For ``asset_class_code`` →
                :class:`AssetClassDTO` resolution. The caller
                instantiates this against the same tenant-scoped
                session as the three Investment-domain repositories.
            data_upload_repository: For reading the upload row + its
                JSONB sheets.
            extractor: The extraction implementation. Tests inject a
                stub extractor; production uses
                :class:`InvestmentExtractor()`.
            region_repository: Optional. When provided alongside
                ``region_weights_repository``, the Phase-6 region-
                split path runs: region splits are extracted from the
                Attributes sheet, resolved strictly against the
                per-tenant ``regions`` catalogue (unknown labels are
                hard import errors per ADR-0046), and persisted via
                replace-by-investment. Pass ``None`` to skip the
                region path.
            sector_repository: Optional. Counterpart for sector
                splits. Sector labels surfaced by the
                :meth:`InvestmentExtractor
                .partition_attributes_from_sheets` heuristic that do
                not resolve against the per-tenant catalogue are
                auto-created here (symmetric with the asset-class
                auto-create path) so the Excel sheet drives the
                vocabulary additively.
            region_weights_repository: Optional. Companion to
                ``region_repository``; receives the replace-by-
                investment writes.
            sector_weights_repository: Optional. Companion to
                ``sector_repository``.
            investment_identifier_repository: Optional. When provided,
                security identifiers (ISIN / ticker) parsed from the
                Attributes sheet are reconciled per investment against
                the workbook with Excel book-of-record semantics
                (ADR-0090): the ``source='excel'`` subset is made to
                match the workbook (excel rows absent from the workbook
                are deleted, new ones inserted with ``source='excel'``),
                while ``openfigi`` / ``manual`` rows are never touched.
                All deletions are applied before any insertion so an
                identifier migrating between investments across workbook
                versions cannot trip the per-tenant unique index
                mid-flight. After reconciliation an investment with no
                primary identifier has its ISIN (else ticker) promoted.
                Pass ``None`` to skip identifier persistence entirely.
            bond_analytics_repository: Optional. When provided, the
                liquid-archetype ``Bond Analytics`` tidy sheet is
                extracted and persisted per investment as a time-series:
                a delete-for-investment then a per-date ``upsert`` with
                ``basis="reported"`` (ADR-0081). Pass ``None`` to skip.
            rating_weights_repository: Optional. Companion for the
                ``Rating Weights`` tidy sheet; same delete-then-upsert
                time-series write, one row per ``(date, bucket)``.
            maturity_weights_repository: Optional. Companion for the
                ``Maturity Weights`` tidy sheet.
            limits_repository: Optional. When provided, the two limit-set
                sheets are validated and persisted (ADR-0056).
            dry_run: If ``True``, the extraction runs and the result
                is returned but **no writes** are issued — useful for
                a UI preview ("you are about to deactivate 3
                investments — confirm?"). Defaults to ``False``.

        Returns:
            Structured :class:`InvestmentExtractionResult` with
            counts and row-level errors.

        Raises:
            UploadNotFoundError: If ``upload_id`` does not resolve in
                the active tenant.
            ImportFormatError: If the JSONB snapshot is structurally
                invalid (missing ``Attributes`` sheet, etc.).
            ValueError: If the bootstrap-installed
                ``"unclassified"`` asset class is missing — that is a
                deployment fault, not a per-row condition.
        """
        # 1) Validate that the upload belongs to the active tenant.
        upload = await data_upload_repository.get_by_id(upload_id)
        if upload is None:
            raise UploadNotFoundError(
                f"Data upload {upload_id} is not visible in the active "
                "tenant context (no row, or RLS hid a foreign-tenant "
                "row)."
            )

        # 2) Read all sheets and convert to extractor-shaped dict.
        sheet_dtos = await data_upload_repository.get_sheets(upload_id)
        upload_sheets: dict[str, dict] = {sheet.sheet_name: sheet.data for sheet in sheet_dtos}

        # 3) Run the extractor (raises ImportFormatError on hard faults).
        valid_anlv_codes: frozenset[str] | None = None
        if anlv_category_repository is not None:
            catalogue_codes = await anlv_category_repository.list_codes()
            valid_anlv_codes = frozenset(catalogue_codes)
        imported_investments = extractor.extract(upload_sheets, valid_anlv_codes=valid_anlv_codes)

        # 3b) Region / sector split extraction.
        #     Both paths are opt-in: callers that don't pass the
        #     region / sector repositories skip the splits entirely.
        region_weights_path_active = (
            region_repository is not None and region_weights_repository is not None
        )
        sector_weights_path_active = (
            sector_repository is not None and sector_weights_repository is not None
        )
        # Security-identifier reconciliation (ADR-0090). Opt-in; the
        # identifiers themselves already ride ``imp.identifiers`` from the
        # extractor, so no pre-extraction step is needed here.
        identifier_path_active = investment_identifier_repository is not None
        # The unitised-opening reconcile (ADR-0097 §7) is gated on the
        # constructor-injected ledger repository, not a transform parameter:
        # creation flows through :meth:`add_position_transaction`, the single
        # sanctioned write seam, which reads the same constructor fields.
        position_path_active = self._position_transactions is not None

        region_weights_by_name: dict[str, list] = {}
        sector_weights_by_name: dict[str, list] = {}
        if region_weights_path_active:
            assert region_repository is not None  # for type narrowing
            regions = await region_repository.list_all()
            regions_by_display_name: dict[str, UUID] = {
                r.display_name.lower(): r.id for r in regions
            }
            region_weights_by_name = extractor.extract_region_weights(
                upload_sheets, regions_by_display_name
            )
        if sector_weights_path_active:
            assert sector_repository is not None  # for type narrowing
            sectors_by_label = await self._ensure_sectors_for_labels(
                extractor=extractor,
                upload_sheets=upload_sheets,
                sector_repository=sector_repository,
                user_id=user_id,
            )
            sector_weights_by_name = extractor.extract_sector_weights(
                upload_sheets, sectors_by_label
            )

        # 3c) Liquid-archetype reference-data extraction (ADR-0081).
        #     Each path is opt-in: callers that don't pass the matching
        #     repository skip that domain entirely. Unlike the
        #     region/sector single-snapshot anchor, these are genuine
        #     time-series — every (investment, date[, bucket]) row is
        #     written, keyed on the investment name (no lookup map).
        bond_analytics_path_active = bond_analytics_repository is not None
        rating_weights_path_active = rating_weights_repository is not None
        maturity_weights_path_active = maturity_weights_repository is not None

        bond_analytics_by_name: dict[str, list] = {}
        rating_weights_by_name: dict[str, list] = {}
        maturity_weights_by_name: dict[str, list] = {}
        if bond_analytics_path_active:
            bond_analytics_by_name = extractor.extract_bond_analytics(upload_sheets)
        if rating_weights_path_active:
            rating_weights_by_name = extractor.extract_rating_weights(upload_sheets)
        if maturity_weights_path_active:
            maturity_weights_by_name = extractor.extract_maturity_weights(upload_sheets)

        # 4) Resolve asset-class codes once. The "unclassified"
        #    fallback is bootstrap-installed (ADR-0043 §1) — its
        #    absence is a deployment fault that we surface loud.
        asset_class_cache: dict[str, AssetClassDTO] = {}
        unclassified = await asset_class_repository.get_by_code(_UNCLASSIFIED_CODE)
        if unclassified is None:
            raise ValueError(
                "The 'unclassified' asset class is missing in the active "
                "tenant. This tenant's default seeds are incomplete; "
                "re-run `portfoliflow create-tenant --subdomain "
                "<subdomain> ...` to reinstall the per-tenant default "
                "seeds (idempotent), then retry the import."
            )
        asset_class_cache[_UNCLASSIFIED_CODE] = unclassified

        async def resolve_asset_class(code: str) -> AssetClassDTO:
            # Empty / sentinel — keep falling back to the bootstrap
            # "unclassified" bucket. The extractor marks blank cells
            # with this sentinel; an empty user-supplied string is
            # equivalent.
            if code == _UNCLASSIFIED_CODE:
                return unclassified

            cached = asset_class_cache.get(code.lower())
            if cached is not None:
                return cached

            normalised_code = _normalise_asset_class_code(code)
            looked_up = await asset_class_repository.get_by_code(normalised_code)
            if looked_up is None and normalised_code != code.lower():
                looked_up = await asset_class_repository.get_by_code(code)

            if looked_up is not None:
                asset_class_cache[normalised_code] = looked_up
                asset_class_cache[code.lower()] = looked_up
                return looked_up

            # Auto-create the asset class from the Excel value. Excel
            # is the source of truth for the asset-class vocabulary at
            # this layer; requiring the operator to pre-create every
            # asset class via the SAA UI would defeat the Strangler-
            # Pattern goal of letting Excel drive the data model
            # additively (ADR-0043 §1).
            _LOG.info(
                "transform: auto-creating asset class %r (normalised code %r) from Excel value.",
                code,
                normalised_code,
            )
            try:
                async with asset_class_repository._session.begin_nested():
                    created = await asset_class_repository.create(
                        code=normalised_code,
                        display_name=code.strip(),
                        description="Auto-created from Excel import.",
                    )
            except IntegrityError:
                # Race: a parallel transaction created the same code
                # first. Re-fetch and use the existing row.
                created = await asset_class_repository.get_by_code(normalised_code)
                if created is None:
                    raise

            asset_class_cache[normalised_code] = created
            asset_class_cache[code.lower()] = created
            return created

        # 5) Replace-by-investment loop.
        if dry_run:
            # Compute counts as if we were going to write — but issue
            # no DB mutations. The active investments list is read for
            # the soft-delete count; everything else is derived from
            # the extracted set.
            existing_investments = await self._investments.list_all()
            existing_by_name: dict[str, InvestmentDTO] = {
                inv.name: inv for inv in existing_investments
            }
            imported_names = {imp.name for imp in imported_investments}

            created = sum(1 for imp in imported_investments if imp.name not in existing_by_name)
            updated = sum(1 for imp in imported_investments if imp.name in existing_by_name)
            reactivated = sum(
                1
                for imp in imported_investments
                if (e := existing_by_name.get(imp.name)) is not None and not e.is_active
            )
            deactivated = sum(
                1
                for inv in existing_investments
                if inv.name not in imported_names and inv.is_active
            )
            navs_replaced = sum(len(imp.navs) for imp in imported_investments)
            cashflows_replaced = sum(len(imp.cashflows) for imp in imported_investments)
            region_weights_replaced = sum(
                len(weights) for weights in region_weights_by_name.values()
            )
            sector_weights_replaced = sum(
                len(weights) for weights in sector_weights_by_name.values()
            )
            bond_analytics_replaced = sum(len(rows) for rows in bond_analytics_by_name.values())
            rating_weights_replaced = sum(len(rows) for rows in rating_weights_by_name.values())
            maturity_weights_replaced = sum(len(rows) for rows in maturity_weights_by_name.values())
            # Statement cells read (ADR-0103 §3). The ledger / price deltas
            # the derivation would produce are deliberately *not* projected:
            # they depend on the stored ledger, and computing them would mean
            # running the classify half of a write path the dry-run exists to
            # avoid. The operator sees what the sheet carries; the write
            # branch reports what it changed.
            cash_statements_read = sum(len(imp.cash_statements) for imp in imported_investments)
            return InvestmentExtractionResult(
                investments_created=created,
                investments_updated=updated,
                investments_deactivated=deactivated,
                investments_reactivated=reactivated,
                navs_replaced=navs_replaced,
                cashflows_replaced=cashflows_replaced,
                region_weights_replaced=region_weights_replaced,
                sector_weights_replaced=sector_weights_replaced,
                bond_analytics_replaced=bond_analytics_replaced,
                rating_weights_replaced=rating_weights_replaced,
                maturity_weights_replaced=maturity_weights_replaced,
                cash_statement_rows=cash_statements_read,
                errors=tuple(extractor.errors),
                warnings=tuple(extractor.warnings),
            )

        created_count = 0
        updated_count = 0
        reactivated_count = 0
        navs_inserted = 0
        cashflows_inserted = 0
        region_weights_inserted = 0
        sector_weights_inserted = 0
        bond_analytics_inserted = 0
        rating_weights_inserted = 0
        maturity_weights_inserted = 0
        # (target_id, investment_type, identifiers) tuples for the post-loop
        # identifier reconciliation pass (ADR-0090); only populated when the
        # path is active. The ``investment_type`` is carried so Phase 4 can
        # apply the type-aware primary-promotion rule without a per-investment
        # re-read.
        identifier_targets: list[tuple[UUID, str, tuple[ImportedIdentifier, ...]]] = []
        # Openings to reconcile after the write loop (ADR-0097 §7); only
        # populated when the path is active and the investment carries a
        # units row. Tuple: (investment_id, units, units_as_of, currency).
        opening_targets: list[tuple[UUID, Decimal, _date, str]] = []
        # Cash positions to derive a ledger for after the write loop
        # (ADR-0103 §4). Tuple: (investment_id, statements).
        cash_targets: list[tuple[UUID, tuple[ImportedCashStatement, ...]]] = []
        cash_statement_rows = 0

        for imp in imported_investments:
            asset_class = await resolve_asset_class(imp.asset_class_code)
            existing = await self._investments.get_by_name(imp.name)
            # A cash position fed by the Cash sheet is the degenerate
            # unitised case (ADR-0103 §1): its balance *is* its holdings.
            # Creation is the only moment the import may set the mode — it
            # never flips an existing row (ADR-0097 §6). The path is gated
            # on the ledger repository, since a unitised row without a
            # ledger would have no balance at all.
            cash_statement_path = bool(imp.cash_statements) and (position_path_active)
            target_id: UUID
            if existing is None:
                created = await self._investments.create(
                    name=imp.name,
                    investment_type=imp.investment_type,
                    asset_class_id=asset_class.id,
                    currency=imp.currency,
                    created_by=user_id,
                    manager_name=imp.manager_name,
                    region=imp.region,
                    vintage_year=imp.vintage_year,
                    commitment_amount=imp.commitment_amount,
                    is_active=True,
                    anlv_code=imp.anlv_code,
                    valuation_mode=("unitised" if cash_statement_path else "reported"),
                )
                target_id = created.id
                created_count += 1
            else:
                target_id = existing.id
                updated_count += 1
                if not existing.is_active:
                    reactivated_count += 1
                # Update mutable fields. All Phase-4 fields except
                # the natural key (name) are sent through update_*.
                # Name is already the lookup key, so we don't rename
                # in transform — renaming is a manual CRUD operation.
                await self._investments.update(
                    target_id,
                    investment_type=imp.investment_type,
                    asset_class_id=asset_class.id,
                    manager_name=imp.manager_name,
                    region=imp.region,
                    currency=imp.currency,
                    vintage_year=imp.vintage_year,
                    commitment_amount=imp.commitment_amount,
                    anlv_code=imp.anlv_code,
                )
                # set_active also bumps updated_at; safe even if the
                # row was already active (idempotent).
                await self._investments.set_active(target_id, True)

            # Record the target for the post-loop identifier
            # reconciliation pass (ADR-0090). Reconciliation is deferred
            # so all deletions run before any insertion across the whole
            # import.
            if identifier_path_active:
                identifier_targets.append((target_id, imp.investment_type, imp.identifiers))

            # Record the opening for the post-loop reconciliation pass
            # (ADR-0097 §7). Only investments with a units row participate;
            # the extractor guarantees ``units_as_of`` is concrete whenever
            # ``units`` is set.
            if position_path_active and imp.units is not None:
                assert imp.units_as_of is not None  # extractor invariant
                opening_targets.append((target_id, imp.units, imp.units_as_of, imp.currency))

            # Record the cash position for the post-loop statement-to-ledger
            # derivation (ADR-0103 §4), for the same reason: the pass needs
            # every ``target_id`` to exist first.
            if cash_statement_path:
                cash_targets.append((target_id, imp.cash_statements))
                cash_statement_rows += len(imp.cash_statements)

            # Replace NAVs. The Cash sheet takes precedence over the NAV
            # sheets for a cash position (ADR-0103 §3), so ``imp.navs`` is
            # empty for one — its NAV series is materialised from the ledger
            # instead, and this delete is what clears the v31 NAV rows the
            # column used to carry.
            #
            # That only holds when the position will *actually* materialise:
            # it needs the ledger path wired **and** the row unitised. A
            # cash row still in 'reported' mode (an ADR-0100 row whose §9
            # migration has not run) materialises nothing — ADR-0098 makes
            # that a deliberate no-op — and neither does anything at all when
            # the caller did not supply the ledger repositories. In either
            # case the delete above would otherwise leave the position with
            # *no* NAV series, silently dropping it out of every aggregate.
            #
            # So when the balances will not be computed, they are written the
            # v31 way instead: the statement levels as ordinary 'excel' NAV
            # rows, which is exactly what the moved NAV column used to hold.
            # Once the migration flips the row, the next import writes none of
            # these and the series becomes computed — value-identical by
            # construction (``balance × 1.0000``, ADR-0103 §9).
            materialises = cash_statement_path and (
                existing is None or existing.valuation_mode == "unitised"
            )
            navs_to_write = imp.navs
            if imp.cash_statements and not materialises:
                navs_to_write = tuple(
                    ImportedNav(
                        as_of_date=statement.statement_date,
                        nav_value=statement.balance,
                        currency=imp.currency,
                        nav_kind="actual",
                    )
                    for statement in imp.cash_statements
                )

            await self._navs.delete_by_investment(target_id)
            for nav in navs_to_write:
                await self._navs.upsert(
                    investment_id=target_id,
                    as_of_date=nav.as_of_date,
                    nav_kind=nav.nav_kind,
                    nav_value=nav.nav_value,
                    currency=nav.currency,
                    source="excel-import",
                    created_by=user_id,
                    ingest_origin="excel",
                )
                navs_inserted += 1

            # Replace cashflows.
            await self._cashflows.delete_by_investment(target_id)
            for cf in imp.cashflows:
                await self._cashflows.create(
                    investment_id=target_id,
                    flow_timestamp=cf.flow_timestamp,
                    flow_type=cf.flow_type,
                    flow_kind=cf.flow_kind,
                    amount=cf.amount,
                    currency=cf.currency,
                    description=None,
                    created_by=user_id,
                    ingest_origin="excel",
                )
                cashflows_inserted += 1

            # Replace composition weights (Phase-6 region / Phase-5a
            # sector), both opt-in. Per ADR-0080 the composition tables
            # are historised: each import lays down a single snapshot
            # anchored to the investment's latest *actual* NAV date —
            # the honest "this is what last held" date. An investment
            # with no actual NAV has no date to anchor on, so its
            # composition is skipped with a warning rather than aborting
            # the whole import (ADR-0080 §2, write half).
            if region_weights_path_active or sector_weights_path_active:
                actual_nav_dates = [n.as_of_date for n in imp.navs if n.nav_kind == "actual"]
                composition_as_of = max(actual_nav_dates) if actual_nav_dates else None
                if composition_as_of is None:
                    extractor._warnings.append(
                        ExtractionWarning(
                            investment_name=imp.name,
                            field="composition",
                            raw_value="",
                            action="skipped_no_anchor",
                            message=(
                                f"composition skipped for {imp.name!r}: "
                                "no actual NAV to anchor as_of_date "
                                "(ADR-0080)"
                            ),
                        )
                    )
                else:
                    if region_weights_path_active:
                        assert region_weights_repository is not None
                        region_weights = region_weights_by_name.get(imp.name, [])
                        region_inputs = [
                            RegionWeightInput(
                                region_id=w.region_id,
                                weight_pct=w.weight_pct,
                            )
                            for w in region_weights
                        ]
                        await region_weights_repository.replace_snapshot_for_investment(
                            target_id,
                            composition_as_of,
                            region_inputs,
                            basis="reported",
                            created_by=user_id,
                            ingest_origin="excel",
                        )
                        region_weights_inserted += len(region_inputs)

                    if sector_weights_path_active:
                        assert sector_weights_repository is not None
                        sector_weights = sector_weights_by_name.get(imp.name, [])
                        sector_inputs = [
                            SectorWeightInput(
                                sector_id=w.sector_id,
                                weight_pct=w.weight_pct,
                            )
                            for w in sector_weights
                        ]
                        await sector_weights_repository.replace_snapshot_for_investment(
                            target_id,
                            composition_as_of,
                            sector_inputs,
                            basis="reported",
                            created_by=user_id,
                            ingest_origin="excel",
                        )
                        sector_weights_inserted += len(sector_inputs)

            # Replace liquid-archetype reference data (ADR-0081), each
            # opt-in. Unlike the region/sector single-snapshot anchor
            # above these are genuine time-series — every date (and
            # bucket) is written — so the block lives outside the
            # composition-anchor guard: delete-for-investment, then a
            # per-row ``upsert`` tagged ``basis="reported"``.
            if bond_analytics_path_active:
                assert bond_analytics_repository is not None
                await bond_analytics_repository.delete_for_investment(target_id)
                for ba in bond_analytics_by_name.get(imp.name, []):
                    await bond_analytics_repository.upsert(
                        investment_id=target_id,
                        as_of_date=ba.as_of_date,
                        ytm=ba.ytm,
                        eff_duration=ba.eff_duration,
                        oas=ba.oas,
                        convexity=ba.convexity,
                        basis="reported",
                        created_by=user_id,
                    )
                    bond_analytics_inserted += 1

            if rating_weights_path_active:
                assert rating_weights_repository is not None
                await rating_weights_repository.delete_for_investment(target_id)
                for rw in rating_weights_by_name.get(imp.name, []):
                    await rating_weights_repository.upsert(
                        investment_id=target_id,
                        as_of_date=rw.as_of_date,
                        rating_bucket=rw.rating_bucket,
                        weight_pct=rw.weight_pct,
                        basis="reported",
                        created_by=user_id,
                        ingest_origin="excel",
                    )
                    rating_weights_inserted += 1

            if maturity_weights_path_active:
                assert maturity_weights_repository is not None
                await maturity_weights_repository.delete_for_investment(target_id)
                for mw in maturity_weights_by_name.get(imp.name, []):
                    await maturity_weights_repository.upsert(
                        investment_id=target_id,
                        as_of_date=mw.as_of_date,
                        maturity_bucket=mw.maturity_bucket,
                        weight_pct=mw.weight_pct,
                        basis="reported",
                        created_by=user_id,
                        ingest_origin="excel",
                    )
                    maturity_weights_inserted += 1

        # 6) Soft-delete: investments not in the import → is_active=FALSE.
        deactivated_count = 0
        existing_investments = await self._investments.list_all()
        imported_names = {imp.name for imp in imported_investments}
        for inv in existing_investments:
            if inv.name in imported_names:
                continue
            if not inv.is_active:
                continue
            await self._investments.set_active(inv.id, False)
            deactivated_count += 1

        # 6b) Security identifiers (ADR-0090). Opt-in; skipped when no
        #     identifier repository was supplied. Runs after the whole
        #     per-investment write loop so every ``target_id`` is known,
        #     and applies all deletions before any insertion across the
        #     import so an identifier that migrates between investments
        #     in a new workbook version cannot trip the per-tenant unique
        #     index mid-flight. Investments absent from the workbook were
        #     soft-deleted above and are intentionally not reconciled —
        #     their identifiers are preserved alongside their history.
        if identifier_path_active:
            assert investment_identifier_repository is not None
            await self._reconcile_excel_identifiers(
                targets=identifier_targets,
                repository=investment_identifier_repository,
                user_id=user_id,
            )

        # 6c) Unitised openings (ADR-0097 §7). Opt-in; skipped when no
        #     position-transaction repository was supplied. Runs after the
        #     write loop so every ``target_id`` is known. Each investment
        #     with a units row gets its single ``excel``-origin opening
        #     reconciled in place — created when absent, updated when the
        #     count or date changed, left untouched when equal — never a
        #     second opening. Investments absent from the workbook were
        #     soft-deleted above and are intentionally not reconciled;
        #     their ledger history is preserved.
        if position_path_active:
            await self._reconcile_excel_openings(
                targets=opening_targets,
                user_id=user_id,
            )

        # 6d) Cash statement-to-ledger derivation (ADR-0103 §4). Same
        #     post-loop placement and same gate as the openings reconcile.
        #     Each cash position's statement series becomes an opening plus
        #     signed transfer deltas, one unity price per statement date,
        #     and — for a unitised row — a materialised NAV series.
        cash_counts = _CashReconcileCounts()
        if position_path_active and cash_targets:
            cash_counts = await self._reconcile_cash_statements(
                targets=cash_targets,
                user_id=user_id,
                extractor=extractor,
            )

        # 6e) Cash plan path (ADR-0103 §6). The cashflow replace in the write
        #     loop above is a delete-and-reinsert across *every* imported
        #     investment, so the workbook's plan flows — investor flows on the
        #     cash columns included — have all just been rewritten and no
        #     per-currency bound would exclude anything. Hence one full,
        #     unbounded recompute across the active cash positions; plan events
        #     are sparse and the projection is a cumulative sum, so this is
        #     cheap.
        #
        #     It runs *after* 6d deliberately: the projection anchors on each
        #     cash position's last **actual** NAV, and 6d is what materialises
        #     those from the statement ledger. Running it earlier would anchor
        #     on NAV rows the write loop had just deleted.
        #
        #     Ungated, unlike 6c/6d: the plan path needs no ledger and no price
        #     repository (plan rows stay value-based, annex §B.1), so it works
        #     on every construction site — and must still run for a tenant
        #     whose cash positions exist but whose workbook carries no Cash
        #     sheet this time.
        cash_plan = await self._cash_plan_materialiser().materialise_all(
            acting_user=user_id, since=None
        )

        # 7) The ``AUM`` sheet persists nothing (ADR-0103 §3): the residual
        #    retired with it, and AUM is defined uniformly as
        #    ``Σ nav_functional`` over every investment, cash rows included
        #    (:mod:`services.investments.aum` — the one formulation). The
        #    sheet survives as an optional **reconciliation control** —
        #    :meth:`reconcile_aum_sheet`, which the import route runs after
        #    the FX sheet has landed so the control can see this workbook's
        #    own rates.
        #
        #    ADR-0103 §7 (strand S1.7, migration b030) dropped the
        #    ``portfolio_aum`` table, model and repository outright; there is
        #    no AUM write path left anywhere to gate.

        # 8) Limit sets (ADR-0056). Same opt-in pattern. Validates
        #    each set's sum-to-100 invariant and the class-key
        #    resolution against the appropriate catalogue before
        #    calling the transactional writer.
        if limits_repository is not None:
            await self._persist_limit_sets(
                upload_sheets=upload_sheets,
                limits_repository=limits_repository,
                asset_class_repository=asset_class_repository,
                anlv_category_repository=anlv_category_repository,
                user_id=user_id,
            )

        _LOG.info(
            "transform_upload_to_investments: upload=%s user=%s "
            "created=%d updated=%d reactivated=%d deactivated=%d "
            "navs=%d cashflows=%d region_weights=%d sector_weights=%d "
            "bond_analytics=%d rating_weights=%d maturity_weights=%d "
            "cash_statements=%d cash_ledger=+%d/~%d/-%d cash_prices=+%d/-%d "
            "cash_plan=+%d/~%d/-%d "
            "errors=%d warnings=%d",
            upload_id,
            user_id,
            created_count,
            updated_count,
            reactivated_count,
            deactivated_count,
            navs_inserted,
            cashflows_inserted,
            region_weights_inserted,
            sector_weights_inserted,
            bond_analytics_inserted,
            rating_weights_inserted,
            maturity_weights_inserted,
            cash_statement_rows,
            cash_counts.ledger_inserted,
            cash_counts.ledger_updated,
            cash_counts.ledger_deleted,
            cash_counts.prices_written,
            cash_counts.prices_deleted,
            cash_plan.inserted,
            cash_plan.updated,
            cash_plan.deleted,
            len(extractor.errors),
            len(extractor.warnings),
        )
        return InvestmentExtractionResult(
            investments_created=created_count,
            investments_updated=updated_count,
            investments_deactivated=deactivated_count,
            investments_reactivated=reactivated_count,
            navs_replaced=navs_inserted,
            cashflows_replaced=cashflows_inserted,
            region_weights_replaced=region_weights_inserted,
            sector_weights_replaced=sector_weights_inserted,
            bond_analytics_replaced=bond_analytics_inserted,
            rating_weights_replaced=rating_weights_inserted,
            maturity_weights_replaced=maturity_weights_inserted,
            cash_statement_rows=cash_statement_rows,
            cash_ledger_inserted=cash_counts.ledger_inserted,
            cash_ledger_updated=cash_counts.ledger_updated,
            cash_ledger_deleted=cash_counts.ledger_deleted,
            cash_prices_written=cash_counts.prices_written,
            cash_prices_deleted=cash_counts.prices_deleted,
            errors=tuple(extractor.errors),
            warnings=tuple(extractor.warnings),
        )

    # ------------------------------------------------------------------
    # Phase-7 Benchmarks & Attribution transformation (ADR-0061)
    # ------------------------------------------------------------------

    async def transform_benchmarks_from_upload(
        self,
        upload_id: UUID,
        *,
        user_id: UUID,
        data_upload_repository: DataUploadRepository,
        asset_class_repository: AssetClassRepository,
        benchmark_repository: BenchmarkRepository,
        benchmark_observation_repository: BenchmarkObservationRepository,
        mapping_repository: AssetClassBenchmarkMappingRepository,
    ) -> BenchmarkImportResult:
        """Transform benchmark sheets from a workbook snapshot to DB rows.

        Per ADR-0061 §Decision the transformation is **idempotent**:
        re-importing the same workbook is safe and produces the same
        final DB state.

        - Benchmarks: upsert by code; ``display_name``,
          ``description``, ``provider_hint`` are refreshed from the
          workbook.
        - Observations: per benchmark, the entire previous generation
          is deleted and the new one inserted (atomic per benchmark).
        - Mappings: per asset class affected, all existing mappings
          are deleted and the workbook's mappings are inserted.

        Hard failures (raise :class:`ValidationError`):

        - Mapping row references an unknown ``asset_class.code``:
          "Asset class 'X' in Benchmark Mapping does not exist in the
          tenant catalogue. Add it under Back Office → SAA → Asset
          Classes, or remove the mapping row."
        - Mapping row references a ``benchmark_code`` not in
          ``Benchmarks actual`` — this is enforced at the extractor
          (raises :class:`ImportFormatError`), which the caller may
          surface to the operator before reaching this method.

        Args:
            upload_id: UUID of a row in ``data_uploads`` in the
                active tenant. RLS hides cross-tenant rows; the
                service surfaces an absent upload as
                :class:`UploadNotFoundError`.
            user_id: The acting user; ``created_by`` on new
                benchmarks.
            data_upload_repository: For reading the upload's JSONB
                sheets.
            asset_class_repository: For ``asset_class.code`` →
                :class:`AssetClassDTO` resolution.
            benchmark_repository: For benchmark upserts and
                code → :class:`BenchmarkDTO` resolution.
            benchmark_observation_repository: For replace-by-benchmark
                observation writes.
            mapping_repository: For replace-by-asset-class mapping
                writes.

        Returns:
            :class:`BenchmarkImportResult` with counts and any
            collected :class:`ImportRowError` warnings.

        Raises:
            UploadNotFoundError: If ``upload_id`` does not resolve.
            ImportFormatError: If the JSONB snapshot is structurally
                invalid (e.g. ``benchmark_mapping`` references a
                ``benchmark_id`` not present in ``benchmarks_actual``).
            ValidationError: On unknown asset-class codes — the
                message is operator-actionable.
        """
        # 1) Resolve and read the upload.
        upload = await data_upload_repository.get_by_id(upload_id)
        if upload is None:
            raise UploadNotFoundError(
                f"Data upload {upload_id} is not visible in the active "
                "tenant context (no row, or RLS hid a foreign-tenant "
                "row)."
            )
        sheet_dtos = await data_upload_repository.get_sheets(upload_id)
        upload_sheets: dict[str, dict] = {sheet.sheet_name: sheet.data for sheet in sheet_dtos}

        # 2) Pure extraction. ImportFormatError bubbles up untouched
        #    so the caller can route it to the upload UI.
        (
            benchmarks,
            observations,
            mappings,
            warnings,
        ) = extract_benchmarks_from_snapshot(upload_sheets)

        if not benchmarks and not observations and not mappings:
            return BenchmarkImportResult(
                n_benchmarks=0,
                n_observations=0,
                n_mappings=0,
                warnings=list(warnings),
            )

        # 3) Upsert benchmark catalogue.
        n_benchmarks = 0
        code_to_id: dict[str, UUID] = {}
        for bm in benchmarks:
            dto = await benchmark_repository.upsert_by_code(
                code=bm.code,
                display_name=bm.display_name,
                description=bm.description,
                provider_hint=bm.provider_hint,
                created_by=user_id,
            )
            code_to_id[bm.code] = dto.id
            n_benchmarks += 1

        # 4) Replace observations per benchmark.
        observations_by_code: dict[str, list[tuple[_date, Decimal]]] = {}
        for obs in observations:
            observations_by_code.setdefault(obs.benchmark_code, []).append(
                (obs.as_of_date, obs.period_return)
            )

        n_observations = 0
        for code, points in observations_by_code.items():
            benchmark_id = code_to_id.get(code)
            if benchmark_id is None:
                # Defensive — the extractor only emits observations
                # for benchmarks present in benchmarks_actual, and
                # those are upserted above. A miss here would imply
                # an extractor invariant violation.
                continue
            inserted = await benchmark_observation_repository.replace_observations_for_benchmark(
                benchmark_id=benchmark_id, observations=points
            )
            n_observations += inserted

        # 5) Replace mappings per affected asset class.
        #    The extractor allows weight==0 + empty benchmark code
        #    rows as deliberate non-mappings (e.g. a ``Cash`` row: cash is
        #    an investment, but it carries no benchmark to compare against).
        #    Such rows are annotations only — they neither produce DB rows
        #    nor require the asset class to exist in the catalogue.
        mappings_by_ac: dict[str, list[tuple[str, Decimal]]] = {}
        touched_asset_classes: set[str] = set()
        annotation_only_codes: set[str] = set()
        for m in mappings:
            is_annotation = m.benchmark_code == "" and m.weight == Decimal("0")
            if is_annotation:
                annotation_only_codes.add(m.asset_class_code)
                continue
            touched_asset_classes.add(m.asset_class_code)
            mappings_by_ac.setdefault(m.asset_class_code, []).append((m.benchmark_code, m.weight))

        # Resolve every touched asset class code to its DB id.
        # Excel strings are normalised to canonical snake-codes via
        # the same helper used by ``transform_upload_to_investments``
        # (ADR-0043). Unknown codes (after normalisation, and as the
        # original spelling as a fallback) are a hard failure with an
        # actionable message.
        ac_code_to_id: dict[str, UUID] = {}
        for code in sorted(touched_asset_classes):
            normalised = _normalise_asset_class_code(code)
            dto = await asset_class_repository.get_by_code(normalised)
            if dto is None and normalised != code:
                # Fallback: try the original spelling in case the
                # workbook already uses the canonical snake-code.
                dto = await asset_class_repository.get_by_code(code)
            if dto is None:
                raise ValidationError(
                    f"Asset class {code!r} (normalised to "
                    f"{normalised!r}) in Benchmark Mapping does not "
                    "exist in the tenant catalogue. Add it under "
                    "Back Office → SAA → Asset Classes, or remove "
                    "the mapping row."
                )
            # Key the lookup by the original code (used downstream in
            # the per-asset-class delete/insert loop, which iterates
            # ``mappings_by_ac`` whose keys are the original codes).
            ac_code_to_id[code] = dto.id

        # Annotation-only codes (weight=0 + empty benchmark) are
        # treated as touched too IF they resolve in the catalogue —
        # so a re-import that downgrades a previously-mapped asset
        # class to "no benchmark" wipes out the prior mapping rows.
        # Codes that don't resolve produce a warning rather than an
        # error (an unbenchmarked ``Cash`` row is the canonical case).
        for code in sorted(annotation_only_codes - touched_asset_classes):
            normalised = _normalise_asset_class_code(code)
            dto = await asset_class_repository.get_by_code(normalised)
            if dto is None and normalised != code:
                dto = await asset_class_repository.get_by_code(code)
            if dto is None:
                warnings.append(
                    ImportRowError(
                        investment_name=None,
                        sheet="benchmark_mapping",
                        row_index=None,
                        column="asset_class",
                        message=(
                            f"Benchmark Mapping row for asset class "
                            f"{code!r} is annotation-only (weight=0, "
                            "no benchmark) and the asset class is not "
                            "in the tenant catalogue; row skipped "
                            "without persistence."
                        ),
                    )
                )
                continue
            ac_code_to_id[code] = dto.id

        n_mappings = 0
        for ac_code, ac_id in ac_code_to_id.items():
            await mapping_repository.delete_mappings_for_asset_class(asset_class_id=ac_id)
            for bm_code, weight in mappings_by_ac.get(ac_code, []):
                benchmark_id = code_to_id.get(bm_code)
                if benchmark_id is None:
                    # Should have been caught at the extractor; raise
                    # a defensive ImportFormatError to surface the
                    # invariant break loudly rather than silently
                    # dropping the mapping.
                    raise ValidationError(
                        f"Benchmark {bm_code!r} in Benchmark Mapping "
                        "is not defined in the 'Benchmarks actual' "
                        "sheet. Check spelling or add a column in "
                        "the Benchmarks actual sheet."
                    )
                await mapping_repository.upsert_mapping(
                    asset_class_id=ac_id,
                    benchmark_id=benchmark_id,
                    weight=weight,
                )
                n_mappings += 1

        _LOG.info(
            "transform_benchmarks_from_upload: upload=%s user=%s "
            "benchmarks=%d observations=%d mappings=%d warnings=%d",
            upload_id,
            user_id,
            n_benchmarks,
            n_observations,
            n_mappings,
            len(warnings),
        )
        return BenchmarkImportResult(
            n_benchmarks=n_benchmarks,
            n_observations=n_observations,
            n_mappings=n_mappings,
            warnings=list(warnings),
        )

    async def transform_fx_rates_from_upload(
        self,
        upload_id: UUID,
        *,
        user_id: UUID,
        data_upload_repository: DataUploadRepository,
        fx_rate_repository: FxRateRepository,
    ) -> FxRateImportResult:
        """Transform the ``FX rates`` sheet from a snapshot to DB rows.

        Per ADR-0099 §5 the ``FX rates`` sheet is the v1 supply path for
        the ``fx_rates`` table. The transformation is **idempotent**:
        re-importing the same workbook is safe and produces the same
        final DB state, and a re-import that changes a rate overwrites
        the prior value in place. Both follow from
        :meth:`FxRateRepository.upsert` keying on
        ``(tenant_id, currency, as_of_date)`` — the unconditional Excel
        (book-of-record) write path, whose ADR-0092 semantics let the
        ``'excel'`` producer overwrite its own prior rows.

        The sheet is optional. A workbook without it (or with an empty
        one) yields a zero-result and writes nothing — the extractor
        returns no rates and this method short-circuits before touching
        the repository, so a benchmarks/investments-only import is
        unaffected.

        Args:
            upload_id: UUID of a row in ``data_uploads`` in the active
                tenant. RLS hides cross-tenant rows; an absent upload
                surfaces as :class:`UploadNotFoundError`.
            user_id: The acting user; ``created_by`` on new rate rows.
            data_upload_repository: For reading the upload's JSONB
                sheets.
            fx_rate_repository: The tenant-scoped ``fx_rates`` writer.

        Returns:
            :class:`FxRateImportResult` with the currencies seen, the
            number of rate rows written, and any collected
            :class:`ImportRowError` warnings.

        Raises:
            UploadNotFoundError: If ``upload_id`` does not resolve.
            ValidationError: On an operator-actionable header problem in
                the ``FX rates`` sheet (malformed pair, mixed reference
                currencies, or an identity pair) — surfaced by the
                extractor and left to bubble up to the upload UI.
            ImportFormatError: If the JSONB snapshot is structurally
                invalid.
        """
        # 1) Resolve and read the upload.
        upload = await data_upload_repository.get_by_id(upload_id)
        if upload is None:
            raise UploadNotFoundError(
                f"Data upload {upload_id} is not visible in the active "
                "tenant context (no row, or RLS hid a foreign-tenant "
                "row)."
            )
        sheet_dtos = await data_upload_repository.get_sheets(upload_id)
        upload_sheets: dict[str, dict] = {sheet.sheet_name: sheet.data for sheet in sheet_dtos}

        # 2) Pure extraction. A ValidationError (bad header) or
        #    ImportFormatError (broken JSONB) bubbles up untouched so the
        #    caller can route it to the upload UI.
        rates, warnings = extract_fx_rates_from_snapshot(upload_sheets)

        if not rates:
            # Optional sheet absent (or every cell was dropped as a row
            # error): nothing to persist. Warnings still surface.
            return FxRateImportResult(
                currencies=[],
                n_rates=0,
                warnings=list(warnings),
            )

        # 3) Upsert each rate via the unconditional Excel write path.
        currencies_seen: set[str] = set()
        n_rates = 0
        for rate in rates:
            await fx_rate_repository.upsert(
                currency=rate.currency,
                as_of_date=rate.as_of_date,
                rate_to_reference=rate.rate_to_reference,
                reference_currency=rate.reference_currency,
                source="excel-import",
                created_by=user_id,
                ingest_origin="excel",
            )
            currencies_seen.add(rate.currency)
            n_rates += 1

        currencies = sorted(currencies_seen)
        _LOG.info(
            "transform_fx_rates_from_upload: upload=%s user=%s currencies=%s rates=%d warnings=%d",
            upload_id,
            user_id,
            ",".join(currencies),
            n_rates,
            len(warnings),
        )
        return FxRateImportResult(
            currencies=currencies,
            n_rates=n_rates,
            warnings=list(warnings),
        )

    # ------------------------------------------------------------------
    # Phase-7 Anlagegrenzen-Überwachung (ADR-0055 / ADR-0056) — plus the
    # AUM sheet's afterlife as a reconciliation control (ADR-0103 §3).
    # ------------------------------------------------------------------

    async def reconcile_aum_sheet(
        self,
        upload_id: UUID,
        *,
        data_upload_repository: DataUploadRepository,
        tenant_repository: TenantRepository,
        fx_rate_repository: FxRateRepository,
    ) -> tuple[ExtractionWarning, ...]:
        """Check the ``AUM`` sheet against Σ NAV — a control, not a write.

        ADR-0103 §3 demotes the ``AUM`` sheet from a persisted parallel data
        model to an **optional reconciliation input**. The residual is
        retired and AUM is now defined uniformly as ``Σ nav_functional(t)``
        over every investment, cash rows included — so a stated AUM figure
        is no longer *data*, it is the custodian's independent count, and
        the honest thing to do with it is compare.

        For each ``(date, stated)`` pair on the sheet this sums every active
        investment's actual NAV on that date — carried forward from the last
        observation at or before it, the ADR-0060 convention every other
        consumer uses — converted into the tenant's functional currency
        through the ADR-0099 §4 seam, and emits one warning per date whose
        deviation exceeds the ``Numeric(20, 4)`` quantum of the NAV column.

        **Nothing is persisted and nothing raises.** A deviation is a
        control finding, not an import failure: the treasurer's custodian
        reconciliation (the ADR-0055 institutional finding) survives as the
        control it always was, without a parallel data model behind it. A
        workbook with no ``AUM`` sheet produces no warnings and reads
        nothing at all.

        **Zero-read property (ADR-0102).** The conversion goes through
        :func:`~services.fx.functional_currency.build_portfolio_fx_converter`,
        so on a single-currency book no FX row is read — the property holds
        by construction rather than by assertion.

        Args:
            upload_id: UUID of a row in ``data_uploads`` in the active
                tenant.
            data_upload_repository: For reading the upload's JSONB sheets.
            tenant_repository: Supplies the functional currency.
            fx_rate_repository: Supplies the rate frame — consulted only
                when a position currency differs from the functional one.

        Returns:
            One :class:`ExtractionWarning` per deviating date, ordered by
            date. Empty when the sheet is absent, carries no usable rows,
            or every figure reconciles within the quantum.

        Raises:
            UploadNotFoundError: If ``upload_id`` does not resolve.
            MissingFxRateError: If a required FX rate is absent for a date
                that must be converted — never a silent 1:1 fallback
                (ADR-0099).
        """
        upload = await data_upload_repository.get_by_id(upload_id)
        if upload is None:
            raise UploadNotFoundError(
                f"Data upload {upload_id} is not visible in the active "
                "tenant context (no row, or RLS hid a foreign-tenant row)."
            )
        sheet_dtos = await data_upload_repository.get_sheets(upload_id)
        payload = next(
            (s.data for s in sheet_dtos if s.sheet_name == _AUM_SHEET_KEY),
            None,
        )
        if payload is None:
            return ()

        stated = self._parse_aum_sheet(payload)
        if not stated:
            return ()

        investments = await self._investments.list_active()
        if not investments:
            return ()

        fx = await build_portfolio_fx_converter(
            tenants=tenant_repository,
            fx_rates=fx_rate_repository,
            position_currencies=[inv.currency for inv in investments],
        )
        navs_by_investment = await self._navs.list_by_investments_and_kind(
            [inv.id for inv in investments], "actual"
        )
        series = build_nav_series(investments, navs_by_investment)

        deviations: list[tuple[_date, Decimal, Decimal]] = []
        unpriced: list[_date] = []
        for as_of_date in sorted(stated):
            try:
                computed = compute_aum(series, as_of_date, fx).total
            except MissingFxRateError:
                # The book cannot be valued in the functional currency on
                # this date — the FX history starts later than the AUM sheet
                # does, which a v31-derived sheet routinely does. The control
                # declines to compare rather than defaulting the rate to 1:1
                # (ADR-0099 §3 forbids that outright) — and, per ADR-0103 §3,
                # it never fails the import over it.
                unpriced.append(as_of_date)
                continue
            deviation = stated[as_of_date] - computed
            if abs(deviation) > _NAV_QUANTUM:
                deviations.append((as_of_date, stated[as_of_date], computed))

        warnings = self._aum_findings(
            deviations=deviations,
            unpriced=unpriced,
            stated_dates=len(stated),
            investments=len(investments),
            functional_currency=fx.functional_currency,
        )
        _LOG.info(
            "reconcile_aum_sheet: upload=%s stated_dates=%d deviations=%d "
            "unpriced=%d warnings=%d fx_identity=%s",
            upload_id,
            len(stated),
            len(deviations),
            len(unpriced),
            len(warnings),
            fx.is_identity,
        )
        return warnings

    @staticmethod
    def _aum_findings(
        *,
        deviations: list[tuple[_date, Decimal, Decimal]],
        unpriced: list[_date],
        stated_dates: int,
        investments: int,
        functional_currency: str,
    ) -> tuple[ExtractionWarning, ...]:
        """Render the reconciliation outcome as bounded operator findings.

        One warning per deviating date, naming stated against computed —
        but **capped**, with a summary standing in for the remainder. The
        cap is not cosmetic: a v31-derived AUM sheet carries a daily figure
        (thousands of rows) while NAVs arrive at statement frequency, so
        nearly every day can deviate structurally, and an uncapped control
        would bury the import result under its own output. The count in the
        summary is the finding that matters in that case — it says the sheet
        is not a reconciliation-grade input at daily frequency, and the fix
        is to trim it to the dates a custodian statement actually covers.
        """
        findings: list[ExtractionWarning] = []
        for as_of_date, stated, computed in deviations[:_MAX_AUM_FINDINGS]:
            findings.append(
                ExtractionWarning(
                    investment_name=None,
                    field="aum_reconciliation",
                    raw_value=str(stated),
                    action="aum_deviation",
                    message=(
                        f"AUM reconciliation {as_of_date.isoformat()}: the "
                        f"sheet states {stated} {functional_currency}, the "
                        f"book computes {computed} {functional_currency} "
                        f"(Σ NAV over {investments} active investments; "
                        f"deviation {stated - computed}). Nothing was "
                        "written — the AUM sheet is a reconciliation control "
                        "only (ADR-0103 §3)."
                    ),
                )
            )
        if len(deviations) > _MAX_AUM_FINDINGS:
            findings.append(
                ExtractionWarning(
                    investment_name=None,
                    field="aum_reconciliation",
                    raw_value=str(len(deviations)),
                    action="aum_deviation_summary",
                    message=(
                        f"AUM reconciliation: {len(deviations)} of "
                        f"{stated_dates} stated dates deviate from Σ NAV "
                        f"beyond the {_NAV_QUANTUM} quantum; the first "
                        f"{_MAX_AUM_FINDINGS} are listed above. A daily AUM "
                        "sheet deviates on most days by construction, since "
                        "NAVs arrive at statement frequency and carry "
                        "forward between statements — trim the sheet to the "
                        "dates a custodian statement covers, or drop it "
                        "(it is optional, ADR-0103 §3)."
                    ),
                )
            )
        if unpriced:
            findings.append(
                ExtractionWarning(
                    investment_name=None,
                    field="aum_reconciliation",
                    raw_value=str(len(unpriced)),
                    action="aum_not_reconcilable",
                    message=(
                        f"AUM reconciliation: {len(unpriced)} of "
                        f"{stated_dates} stated dates could not be compared "
                        f"— the book holds a position whose currency has no "
                        f"FX rate at or before that date (earliest affected: "
                        f"{min(unpriced).isoformat()}). Rates are never "
                        "defaulted to 1:1 (ADR-0099 §3), so these dates are "
                        "skipped rather than reconciled against a fabricated "
                        "figure. Extend the 'FX rates' sheet backwards, or "
                        "start the AUM sheet where the rates do."
                    ),
                )
            )
        return tuple(findings)

    @classmethod
    def _parse_aum_sheet(cls, payload: dict[str, Any]) -> dict[_date, Decimal]:
        """Unpack the JSONB ``aum`` sheet into ``{date: stated_aum}``.

        The shape mirrors every other market-reference sheet
        (``DataFrame.to_json(orient="split", date_format="iso")``): a single
        ``"AUM total"`` column over an ISO date index. Unparseable dates and
        non-numeric cells are skipped — the sheet is a control input, so a
        malformed cell silently drops out of the comparison rather than
        failing an import that has already written its data.
        """
        index = list(payload.get("index", []))
        data = list(payload.get("data", []))
        stated: dict[_date, Decimal] = {}
        for idx, raw_value in zip(index, data, strict=False):
            cell = (
                (raw_value[0] if raw_value else None) if isinstance(raw_value, list) else raw_value
            )
            if cell is None:
                continue
            as_of = cls._parse_iso_date(idx)
            if as_of is None:
                continue
            try:
                stated[as_of] = Decimal(str(cell))
            except (TypeError, ValueError, InvalidOperation):
                continue
        return stated

    async def _persist_limit_sets(
        self,
        *,
        upload_sheets: dict[str, dict],
        limits_repository: LimitsRepository,
        asset_class_repository: AssetClassRepository,
        anlv_category_repository: AnlVCategoryRepository | None,
        user_id: UUID,
    ) -> dict[str, int]:
        """Validate and persist every limit set found in the snapshot.

        Per ADR-0056 §Decision the two sheets carry pre-defined
        families:

        - ``limit_set_saa`` → ``family = 'saa'`` (class keys resolve
          against :class:`AssetClassRepository`).
        - ``limit_set_2``   → ``family = 'anlv'`` (class keys resolve
          against :class:`AnlVCategoryRepository`).

        For each set the helper enforces:

        - ``abs(sum - 100) < 0.01`` (importer-level sum-to-100 rule).
        - Every ``class_key`` resolves against its catalogue.
        - The UNIQUE ``(tenant_id, family, effective_from)`` constraint
          — an attempted re-import of an already-persisted set raises
          :class:`LimitValidationError` referencing the immutability
          invariant.

        Args:
            upload_sheets: JSONB dict containing the limit-set sheets.
            limits_repository: Bound to the active tenant context.
            asset_class_repository: For SAA class-key resolution.
            anlv_category_repository: For AnlV class-key resolution.
                Required when the AnlV sheet is present in the
                snapshot — pass ``None`` only if the snapshot is
                AnlV-free (a sum-validation negative test path uses
                that for the SAA sheet alone).
            user_id: Becomes ``limit_sets.created_by``.

        Returns:
            Dict ``{family: number_of_sets_inserted}``.

        Raises:
            LimitValidationError: On sum-to-100 violation, unknown
                class key, or re-import of an existing set.
        """
        family_by_sheet: dict[str, str] = {
            "limit_set_saa": "saa",
            "limit_set_2": "anlv",
        }
        counts: dict[str, int] = {"saa": 0, "anlv": 0}

        for sheet_key, family in family_by_sheet.items():
            payload = upload_sheets.get(sheet_key)
            if payload is None:
                continue
            counts[family] += await self._persist_one_limit_sheet(
                sheet_key=sheet_key,
                family=family,
                payload=payload,
                limits_repository=limits_repository,
                asset_class_repository=asset_class_repository,
                anlv_category_repository=anlv_category_repository,
                user_id=user_id,
            )
        return counts

    async def _persist_one_limit_sheet(
        self,
        *,
        sheet_key: str,
        family: str,
        payload: dict,
        limits_repository: LimitsRepository,
        asset_class_repository: AssetClassRepository,
        anlv_category_repository: AnlVCategoryRepository | None,
        user_id: UUID,
    ) -> int:
        """Validate and persist every set inside one limit-set sheet."""
        index = list(payload.get("index", []))
        data = list(payload.get("data", []))
        if not index or not data:
            return 0

        try:
            df = pd.DataFrame(
                data=data,
                index=index,
                columns=list(payload.get("columns", [])),
            )
        except Exception as exc:  # noqa: BLE001 — surface as validation
            raise LimitValidationError(
                f"Limit-set sheet {sheet_key!r} payload could not be "
                f"reconstructed as a DataFrame: {exc}"
            ) from exc

        required = {"effective_from", "label", "notes"}
        missing = required - set(df.index)
        if missing:
            raise LimitValidationError(
                f"Limit-set sheet {sheet_key!r} is missing required rows: {sorted(missing)}."
            )

        class_keys = [idx for idx in df.index if idx not in required]
        if not class_keys:
            raise LimitValidationError(f"Limit-set sheet {sheet_key!r} has no class-key rows.")

        # Resolve valid class keys once per family.
        valid_class_keys: set[str]
        if family == "saa":
            classes = await asset_class_repository.list_all()
            valid_class_keys = {c.code for c in classes}
        elif family == "anlv":
            if anlv_category_repository is None:
                raise LimitValidationError(
                    f"Limit-set sheet {sheet_key!r} has family 'anlv' "
                    "but no anlv_category_repository was supplied."
                )
            valid_class_keys = set(await anlv_category_repository.list_codes())
        else:
            raise LimitValidationError(
                f"Limit-set sheet {sheet_key!r} maps to unknown family {family!r}."
            )

        inserted = 0
        for col in df.columns:
            effective_from = self._parse_iso_date(df.at["effective_from", col])
            if effective_from is None:
                raise LimitValidationError(
                    f"Limit-set sheet {sheet_key!r} column {col!r} has "
                    "no usable effective_from date."
                )
            label_val = df.at["label", col]
            label = str(label_val).strip() if label_val is not None else ""
            if not label:
                raise LimitValidationError(
                    f"Limit-set sheet {sheet_key!r} column {col!r} has no label."
                )
            notes_val = df.at["notes", col]
            notes: str | None
            if notes_val is None or (isinstance(notes_val, str) and not notes_val.strip()):
                notes = None
            else:
                notes = str(notes_val).strip()

            limits: dict[str, Decimal] = {}
            for class_key in class_keys:
                raw = df.at[class_key, col]
                if raw is None:
                    continue
                try:
                    pct = Decimal(str(raw))
                except (TypeError, ValueError) as exc:
                    raise LimitValidationError(
                        f"Limit-set sheet {sheet_key!r} column {col!r} "
                        f"class {class_key!r}: value {raw!r} is not "
                        "numeric."
                    ) from exc
                if pct == 0:
                    # 0 % is "no allocation" and is excluded — the DB
                    # CHECK refuses zero; the importer does the same.
                    continue
                if class_key in limits:
                    raise LimitValidationError(
                        f"Limit-set sheet {sheet_key!r} column {col!r} "
                        f"has duplicate class_key {class_key!r}."
                    )
                if class_key not in valid_class_keys:
                    raise LimitValidationError(
                        f"Limit-set sheet {sheet_key!r} family "
                        f"{family!r} references unknown class_key "
                        f"{class_key!r}. Either correct the Excel cell "
                        "or extend the catalogue."
                    )
                limits[class_key] = pct

            if not limits:
                raise LimitValidationError(
                    f"Limit-set sheet {sheet_key!r} column {col!r} has no non-zero class entries."
                )

            total = sum(limits.values(), Decimal("0"))
            if abs(total - Decimal("100")) >= Decimal("0.01"):
                raise LimitValidationError(
                    f"Limit-set sheet {sheet_key!r} column {col!r} "
                    f"({label!r}): class weights sum to {total}, "
                    "expected 100 (±0.01)."
                )

            try:
                await limits_repository.create_set_with_limits(
                    family=family,
                    effective_from=effective_from,
                    label=label,
                    notes=notes,
                    limits=limits,
                    created_by=user_id,
                )
            except IntegrityError as exc:
                raise LimitValidationError(
                    f"Limit set already exists for {family!r} effective "
                    f"{effective_from.isoformat()}; existing sets are "
                    "immutable per ADR-0056. Use a later effective_from "
                    "to amend."
                ) from exc
            inserted += 1

        return inserted

    @staticmethod
    def _parse_iso_date(raw) -> _date | None:
        """Best-effort ISO-date parser shared by AUM and limit-set helpers.

        Mirrors the convention used by the extractor: ISO 8601 strings,
        plus a forgiving path for ``datetime`` / ``date`` objects that
        round-trip through pandas without re-serialisation.
        """
        if raw is None:
            return None
        if isinstance(raw, datetime):
            return raw.date()
        if isinstance(raw, _date):
            return raw
        if isinstance(raw, str):
            candidate = raw.strip()
            if not candidate:
                return None
            if candidate.endswith("Z"):
                candidate = candidate[:-1] + "+00:00"
            try:
                return datetime.fromisoformat(candidate).date()
            except ValueError:
                return None
        return None
