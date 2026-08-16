# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Cash plan-path materialisation service (ADR-0103 §6, strand S1.5).

Projects each cash position's **forward** balance from its last actual
statement and the plan flows that settle against it, and materialises the
projection as ordinary ``investment_navs`` rows —

```
cash_plan(d) = balance(t₀) + Σ_{t₀ < t ≤ d} signed plan flows(t)
```

— so the Planning Desk (ADR-0104) and every other NAV-series consumer read
the plan path through the unchanged contract, with no new branch anywhere.

This is a **DB-writing** service, the sibling of
:mod:`services.investments.nav_materialisation` and deliberately **not** an
extension of it: that service materialises *actuals* from ``holdings ×
price`` and pins ``nav_kind='actual'`` (ADR-0098 §2, an immutable contract);
this one is a different computation with a different anchor and writes
``nav_kind='plan'``. The two share the idiom — classify-then-write
idempotency, ``'system'``-rows-only mutation, stranded-row deletion, a
per-outcome report — and nothing else (ADR-0103 §6, explicit).

**Semantics (ADR-0103 §6).** Per *active* cash position:

* **Anchor (t₀).** The latest ``nav_kind='actual'`` NAV row, of any origin —
  the ``'system'`` row the ADR-0098 service materialises from the unitised
  statement ledger and the ``'excel'`` fallback of a not-yet-migrated row are
  value-identical by construction (``balance × 1.0000``, ADR-0103 §9). Its
  date is ``t₀``, its value the anchor balance. A cash position with no
  actual NAV row has **no anchor** and is skipped (counted).
* **Event set.** The ``flow_kind='plan'`` flows — *every* flow type,
  ``investor_flow`` included — of **all active investments** whose flow
  ``currency`` equals the position's currency, with event date strictly
  **after** ``t₀``. Settlement is by the **flow's own currency** (ADR-0104:
  "the cash path of its settlement currency"), not the owning investment's:
  a EUR-denominated flow on a USD investment settles into the EUR cash path.
  A plan flow on or before the last actual statement is stale history and
  contributes nothing.
* **Result.** One row per distinct event date, carrying the cumulative
  balance: ``nav_kind='plan'``, ``basis='computed'``,
  ``ingest_origin='system'``, ``source='computed:cash-plan'``, currency = the
  position's currency.

**Signs need no per-type branch.** The importer validates cashflow signs at
the boundary rather than coercing them (ADR-0043 §3): a ``capital_call`` is
negative (the Cash-Flow-Out guard), a ``distribution`` or income flow
positive (the Cash-Flow-In guard), an ``investor_flow`` signed both ways (a
contribution positive, a withdrawal negative — ADR-0103 §5). So the §6
formula's "signed plan flows" simply sum, and decision ex-D6 — *a plan call
debits cash, a plan distribution credits it* — holds by construction.

**Negative balances are legal and expected.** A projected funding gap is the
single most decision-relevant signal the Planning Desk shows (ADR-0103 §6),
so this path carries **no** non-negativity guard of any kind. The
non-negativity rule of ADR-0100 §5 binds *actual* balances only.

**No FX (ADR-0103 §6, decision N2).** The path materialises per currency, in
**position currency**. It reads no rate, converts nothing, and knows nothing
of the functional currency; conversion happens at the ordinary ADR-0099 §4
read seam, under the plan-world FX convention ADR-0104 pins. Nothing in this
module may acquire an FX dependency.

**Not the overlay exemption.** This projection *reads* investor flows like
any other plan flow. :data:`~services.investments.flow_type_invariants
.OVERLAY_EXEMPT_FLOW_TYPES` governs the scenario/TA **overlay executors**
(ADR-0104, roadmap #049) — what may *rewrite* a flow — not what may read
one. The two must not be confused: an exempt flow still settles.

**Ownership (classify-then-write).** The service owns exactly the position's
``ingest_origin='system'`` **and** ``source='computed:cash-plan'`` *plan*
rows, and nothing else. Value-equal rows are left untouched (no ``updated_at``
bump), changed values refreshed, missing dates inserted, and rows whose date
left the event set — or fell to or below a moved ``t₀`` — deleted. A plan
row of any other origin (an ``'excel'`` plan column, a ``'manual'`` edit) is
precedence-protected: never written, never deleted, counted as skipped. The
unique key ``(investment_id, as_of_date, nav_kind)`` then makes a collision
structurally impossible once a date is skipped.

**Trigger (ADR-0103 §6, the ADR-0098 §3 pattern).** The service runs
synchronously inside the caller's tenant-scoped transaction — it takes the
repositories the caller already holds, hence the same session — so flows,
statements and the projection can never be observed disagreeing. Callers that
know the earliest affected date pass ``since`` to bound the classify window;
an anchor move recomputes in full.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date as _date
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from core.repositories.investment_cashflow_repository import (
    InvestmentCashflowRepository,
)
from core.repositories.investment_nav_repository import (
    InvestmentNavDTO,
    InvestmentNavRepository,
)
from core.repositories.investment_repository import (
    InvestmentDTO,
    InvestmentRepository,
)

_LOG = logging.getLogger("portfoliflow.services.investments.cash_plan_materialisation")

#: The ``nav_kind`` this service writes. Its sibling (ADR-0098) writes
#: ``'actual'`` and only ``'actual'``; this one writes ``'plan'`` and only
#: ``'plan'``. Neither ever touches the other's kind.
_NAV_KIND: str = "plan"

#: The ``flow_kind`` the projection reads. Actual flows are informational on
#: cash (ADR-0103 §5) — actual balances come from statement levels — so they
#: are never read here and never trigger a recompute.
_FLOW_KIND: str = "plan"

#: The ``investment_type`` a cash position carries (ADR-0100 §1).
_CASH_TYPE: str = "cash"

#: The deterministic provenance marker of a projected row (ADR-0103 §6). It
#: is the service's **ownership key**, alongside ``ingest_origin='system'``:
#: a ``'system'`` plan row of any other source belongs to another producer
#: (a future ADR-0104 overlay) and is never written or deleted here.
#:
#: Public because it is also the marker the *reader* selects by:
#: :func:`services.investments.plan_world.assemble_plan_frames` takes each
#: cash position's plan path from the rows carrying it (ADR-0104 §1). Writer
#: and reader name the same rows through one formulation — a second copy of
#: the string in the reader would drift from this one the moment either
#: moves.
CASH_PLAN_SOURCE: str = "computed:cash-plan"

#: The ``Numeric(20, 4)`` scale of ``investment_navs.nav_value``. Projected
#: balances are quantised to it (half-away-from-zero, matching Postgres
#: ``numeric`` rounding) so a stored row and a freshly recomputed value
#: compare equal — the load-bearing condition for the value-equal no-op.
_NAV_SCALE: Decimal = Decimal("0.0001")


# ---------------------------------------------------------------------------
# The pure projection (DB-free)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanFlowEvent:
    """One signed plan flow, reduced to what the projection needs.

    The DB shell maps an ``InvestmentCashflowDTO`` onto this; the projection
    itself never sees a repository DTO, a currency, or a flow type — the
    currency has already selected the cash path, and the flow type is
    irrelevant to a signed sum (see the module docstring).

    Attributes:
        as_of_date: The event date — the date part of the flow's 12:00-UTC
            ``flow_timestamp`` (ADR-0043 §1).
        amount: The signed flow amount, in the settling cash position's
            currency. Negative debits the cash path (a plan capital call),
            positive credits it (a plan distribution).
    """

    as_of_date: _date
    amount: Decimal


@dataclass(frozen=True)
class CashPlanPoint:
    """One projected balance on one event date.

    Attributes:
        as_of_date: The event date this balance is projected for.
        balance: The cumulative projected balance — anchor plus every signed
            plan flow strictly after ``t₀`` up to and including this date.
            **May be negative**: a projected funding gap (ADR-0103 §6).
    """

    as_of_date: _date
    balance: Decimal


def project_cash_plan(
    *,
    anchor_date: _date,
    anchor_balance: Decimal,
    flows: Iterable[PlanFlowEvent],
    scale: Decimal = _NAV_SCALE,
) -> list[CashPlanPoint]:
    """Project the forward cash path from an anchor and its plan flows.

    The pure core of ADR-0103 §6 — the whole formula and nothing else, kept
    DB-free and directly unit-testable in the spirit of
    :func:`services.investments.holdings.derive_holdings`:

    ```
    cash_plan(d) = anchor_balance + Σ signed flows with anchor_date < t ≤ d
    ```

    Flows on or before ``anchor_date`` are **stale history** — the statement
    that set the anchor already contains their effect — and contribute
    nothing. Multiple flows on one date collapse into one point carrying
    their summed effect: the projection publishes one balance per *distinct*
    event date, never one per flow.

    Args:
        anchor_date: ``t₀`` — the last actual statement date.
        anchor_balance: The actual balance on ``t₀``, in position currency.
        flows: The signed plan flows settling against this cash path, in any
            order. Flows outside ``(anchor_date, ∞)`` are ignored.
        scale: The quantum each projected balance is rounded to — the
            ``Numeric(20, 4)`` scale of the target column, so a re-run
            compares equal to what is stored.

    Returns:
        One :class:`CashPlanPoint` per distinct event date after
        ``anchor_date``, ascending. Empty when no plan flow lies ahead of the
        anchor — the correct answer, not an error: a fully-funded portfolio
        with no plan events simply has no forward path to draw.
    """
    by_date: dict[_date, Decimal] = {}
    for flow in flows:
        if flow.as_of_date <= anchor_date:
            continue
        by_date[flow.as_of_date] = by_date.get(flow.as_of_date, Decimal(0)) + flow.amount

    balance = anchor_balance
    points: list[CashPlanPoint] = []
    for as_of_date in sorted(by_date):
        balance += by_date[as_of_date]
        points.append(
            CashPlanPoint(
                as_of_date=as_of_date,
                balance=balance.quantize(scale, rounding=ROUND_HALF_UP),
            )
        )
    return points


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CashPlanReport:
    """Per-outcome counts for one cash-plan materialisation run.

    Summed across cash positions with ``+``. Idempotency is a property, not a
    mode: a re-run with unchanged flows and statements produces all-zero
    change counts — only :attr:`noop` and the ``skipped_*`` counters move, and
    no ``updated_at`` is bumped.

    Attributes:
        inserted: Event dates with no existing plan row — a fresh ``'system'``
            row written.
        updated: Own rows whose projected balance changed, refreshed in place.
        noop: Event dates already carrying an identical own row — no write
            issued (the idempotency signal).
        skipped_excel: Event dates whose plan row is ``'excel'`` — a workbook
            plan column. Left byte-identical; the book of record wins.
        skipped_manual: Event dates whose plan row is ``'manual'`` — an
            operator edit. Left byte-identical.
        skipped_foreign: Event dates whose plan row belongs to another writer
            — a ``'live'`` row (which cannot legitimately exist on a cash
            position: cash is permanently live-ineligible, ADR-0103 §1) or a
            ``'system'`` row of a different ``source`` (a future ADR-0104
            overlay producer). Left byte-identical and logged.
        deleted: Own rows whose date left the event set — a plan flow was
            removed or re-dated, or a new statement moved ``t₀`` past them.
            Only own rows are ever deletion candidates.
        positions: Active cash positions projected in this run.
        skipped_no_anchor: Active cash positions with no actual NAV row —
            nothing to anchor on, so nothing is projected, written, or
            deleted (ADR-0103 §6: the anchor *is* the last actual statement).
        ignored_positions: Active cash positions passed over because an
            earlier-created active position already settles their currency
            (the multi-position rule; ADR-0103 §10 puts multi-custodian
            sub-balances out of scope). Their own stranded rows, if any, are
            still cleaned up.
        skipped_flows_no_position: Plan flows whose settlement currency has no
            active cash position at all — there is nothing to anchor them to,
            so they project nowhere. Counted and logged rather than raised: an
            import must not fail on a data shape the ADR declines to model.
    """

    inserted: int = 0
    updated: int = 0
    noop: int = 0
    skipped_excel: int = 0
    skipped_manual: int = 0
    skipped_foreign: int = 0
    deleted: int = 0
    positions: int = 0
    skipped_no_anchor: int = 0
    ignored_positions: int = 0
    skipped_flows_no_position: int = 0

    def __add__(self, other: CashPlanReport) -> CashPlanReport:
        return CashPlanReport(
            inserted=self.inserted + other.inserted,
            updated=self.updated + other.updated,
            noop=self.noop + other.noop,
            skipped_excel=self.skipped_excel + other.skipped_excel,
            skipped_manual=self.skipped_manual + other.skipped_manual,
            skipped_foreign=self.skipped_foreign + other.skipped_foreign,
            deleted=self.deleted + other.deleted,
            positions=self.positions + other.positions,
            skipped_no_anchor=(self.skipped_no_anchor + other.skipped_no_anchor),
            ignored_positions=(self.ignored_positions + other.ignored_positions),
            skipped_flows_no_position=(
                self.skipped_flows_no_position + other.skipped_flows_no_position
            ),
        )

    @property
    def total_written(self) -> int:
        """Rows the run created or refreshed (``inserted + updated``)."""
        return self.inserted + self.updated


# ---------------------------------------------------------------------------
# The DB shell
# ---------------------------------------------------------------------------


class CashPlanMaterialisationService:
    """Materialise the forward cash path onto each cash position.

    Constructed from the three tenant-scoped repositories it needs; they must
    share one session so the whole run is a single in-transaction unit
    (ADR-0103 §6, ADR-0098 §3). The service holds no session itself.

    All three dependencies are **mandatory** — they are exactly the three
    repositories every :class:`~services.investments.InvestmentService`
    construction site already supplies. The plan path therefore has no
    optional dependency to go silently unwired (contrast
    :meth:`~services.investments.InvestmentService._require_instrument_prices`,
    whose loud failure exists precisely because the price repository *is*
    optional): the ADR-0098 loud-failure posture is satisfied structurally
    here, by there being nothing left to fail on.

    Note it needs **no** ledger and **no** price repository: plan rows stay
    value-based (compatibility annex §B.1) — the projection writes NAV values
    directly, never a ``position_transactions`` row and never an
    ``instrument_prices`` row. The plan world has no units decomposition.
    """

    def __init__(
        self,
        investments: InvestmentRepository,
        navs: InvestmentNavRepository,
        cashflows: InvestmentCashflowRepository,
    ) -> None:
        self._investments = investments
        self._navs = navs
        self._cashflows = cashflows

    async def materialise_all(
        self,
        *,
        acting_user: UUID,
        since: _date | None = None,
    ) -> CashPlanReport:
        """Recompute the plan path of every active cash position.

        The unbounded entry point, for the triggers that cannot name the
        affected currencies: the Excel transform's cashflow replace phase
        deletes and re-inserts flows across every imported investment, so
        per-currency bounding buys nothing there.

        Args:
            acting_user: ``created_by`` on inserted rows (preserved on
                update).
            since: The earliest affected date, if the caller knows it. See
                :meth:`materialise_currencies`.

        Returns:
            The summed :class:`CashPlanReport` over every currency in play.
        """
        return await self._run(currencies=None, acting_user=acting_user, since=since)

    async def materialise_currencies(
        self,
        currencies: Iterable[str],
        *,
        acting_user: UUID,
        since: _date | None = None,
    ) -> CashPlanReport:
        """Recompute the plan path of the cash positions of given currencies.

        The bounded entry point, for the triggers that know exactly which cash
        path moved: a statement import (the anchor moved) and a plan-flow
        edit (the events moved).

        Args:
            currencies: The settlement currencies to recompute. Case is
                normalised; an empty iterable is a no-op returning an empty
                report.
            acting_user: ``created_by`` on inserted rows (preserved on
                update).
            since: The earliest affected date, if the caller knows it (a
                flow's event date). When given, the classify window is bounded
                to dates on or after it: no change at ``since`` can move the
                projection on an earlier date, so earlier own rows are left
                untouched — and, being outside the window, are never stranded.
                Balances are still summed over the **whole** flow history, so
                a bounded run and a full run agree on every value they both
                write. ``None`` recomputes the whole path — which is what an
                anchor move requires, since a new ``t₀`` re-bases every date.

        Returns:
            The summed :class:`CashPlanReport` over the given currencies.
        """
        selected = {c.strip().upper() for c in currencies if c and c.strip()}
        if not selected:
            return CashPlanReport()
        return await self._run(currencies=selected, acting_user=acting_user, since=since)

    async def _run(
        self,
        *,
        currencies: set[str] | None,
        acting_user: UUID,
        since: _date | None,
    ) -> CashPlanReport:
        """Resolve the settling positions, then project each one.

        One pass over the active universe: the cash positions are grouped by
        currency and the settling one chosen per the multi-position rule; the
        plan flows of *all* active investments are grouped by their **own**
        currency. Inactive investments are excluded wholesale — a deactivated
        investment's plan flows do not settle.
        """
        active = await self._investments.list_active()
        settling, ignored = self._settling_positions(active)
        flows_by_currency = await self._plan_flows_by_currency(active)

        in_play = set(settling) | set(flows_by_currency)
        if currencies is not None:
            in_play &= currencies

        report = CashPlanReport()
        for currency in sorted(in_play):
            position = settling.get(currency)
            flows = flows_by_currency.get(currency, [])
            if position is None:
                # Nothing to anchor these flows to. Counted and logged, never
                # raised: multi-custodian and unmodelled-currency shapes must
                # not fail an import (ADR-0103 §10).
                _LOG.warning(
                    "cash_plan: %d plan flow(s) settle in %s, which has no "
                    "active cash position — not projected (ADR-0103 §6).",
                    len(flows),
                    currency,
                )
                report += CashPlanReport(skipped_flows_no_position=len(flows))
                continue
            report += await self._materialise_position(
                position=position,
                flows=flows,
                acting_user=acting_user,
                since=since,
            )

        # A non-settling duplicate projects nothing, so any own row it still
        # carries is stranded — clean it up rather than leave a path nobody
        # recomputes. (Normally there is none; this is the self-healing edge
        # when a newly imported, earlier-created position takes over a
        # currency.)
        for position in ignored:
            if currencies is not None and position.currency not in currencies:
                continue
            report += await self._materialise_position(
                position=position,
                flows=[],
                acting_user=acting_user,
                since=since,
                project=False,
            )

        _LOG.info(
            "cash_plan: currencies=%d positions=%d inserted=%d updated=%d "
            "noop=%d skipped_excel=%d skipped_manual=%d skipped_foreign=%d "
            "deleted=%d no_anchor=%d ignored_positions=%d "
            "flows_without_position=%d since=%s",
            len(in_play),
            report.positions,
            report.inserted,
            report.updated,
            report.noop,
            report.skipped_excel,
            report.skipped_manual,
            report.skipped_foreign,
            report.deleted,
            report.skipped_no_anchor,
            report.ignored_positions,
            report.skipped_flows_no_position,
            since,
        )
        return report

    @staticmethod
    def _settling_positions(
        active: list[InvestmentDTO],
    ) -> tuple[dict[str, InvestmentDTO], list[InvestmentDTO]]:
        """Choose the cash position each currency settles against.

        **The multi-position rule.** ADR-0103 §10 puts multi-custodian
        sub-balances out of scope, so the ADR fixes no meaning for two cash
        positions in one currency — yet a workbook may legitimately contain
        them, and an import must not fail on a data shape the specification
        declines to model. The **earliest-created** active position settles;
        the others are named in one warning per run and projected onto not at
        all. Deterministic, self-healing, and loud enough that the operator
        sees the shape rather than a silently split path.

        Returns:
            ``(settling_by_currency, ignored)`` — the chosen position per
            currency, and every active cash position passed over.
        """
        by_currency: dict[str, list[InvestmentDTO]] = {}
        for investment in active:
            if investment.investment_type != _CASH_TYPE:
                continue
            by_currency.setdefault(investment.currency.upper(), []).append(investment)

        settling: dict[str, InvestmentDTO] = {}
        ignored: list[InvestmentDTO] = []
        for currency, positions in by_currency.items():
            # (created_at, id): the id breaks a same-instant tie so the choice
            # is stable across runs rather than dependent on row order.
            ordered = sorted(positions, key=lambda i: (i.created_at, i.id))
            settling[currency] = ordered[0]
            if len(ordered) > 1:
                ignored.extend(ordered[1:])
                _LOG.warning(
                    "cash_plan: %s has %d active cash positions; the plan path "
                    "settles against the earliest-created %r and ignores %s. "
                    "Multi-custodian sub-balances per currency are out of "
                    "scope (ADR-0103 §10).",
                    currency,
                    len(ordered),
                    ordered[0].name,
                    ", ".join(repr(i.name) for i in ordered[1:]),
                )
        return settling, ignored

    async def _plan_flows_by_currency(
        self, active: list[InvestmentDTO]
    ) -> dict[str, list[PlanFlowEvent]]:
        """Group every active investment's plan flows by settlement currency.

        Settlement is by the **flow's own** ``currency`` column, not the
        owning investment's (ADR-0104: "the cash path of its settlement
        currency"): a EUR-denominated flow booked on a USD investment settles
        into the EUR cash path. Every flow type participates —
        ``investor_flow`` included, which is what makes the mandate's own
        contributions and withdrawals part of the projection (ADR-0103 §5).
        """
        by_investment = await self._cashflows.list_by_investments_and_kind(
            [i.id for i in active], _FLOW_KIND
        )
        grouped: dict[str, list[PlanFlowEvent]] = {}
        for flows in by_investment.values():
            for flow in flows:
                grouped.setdefault(flow.currency.upper(), []).append(
                    PlanFlowEvent(
                        as_of_date=flow.flow_timestamp.date(),
                        amount=flow.amount,
                    )
                )
        return grouped

    async def _materialise_position(
        self,
        *,
        position: InvestmentDTO,
        flows: list[PlanFlowEvent],
        acting_user: UUID,
        since: _date | None,
        project: bool = True,
    ) -> CashPlanReport:
        """Classify and write one cash position's projected path.

        Args:
            position: The cash position to project onto.
            flows: The plan flows settling against it.
            acting_user: ``created_by`` on inserted rows.
            since: Lower bound of the classify window, or ``None`` for the
                whole path.
            project: ``False`` for a non-settling duplicate — the target set
                is empty by definition, so the pass only cleans up its own
                stranded rows.

        Returns:
            The :class:`CashPlanReport` for this position alone.
        """
        anchor = await self._navs.get_latest_actual(position.id)
        if project and anchor is None:
            # No statement, no anchor, no projection — and nothing written or
            # deleted (ADR-0103 §6: the anchor is the last actual statement).
            _LOG.info(
                "cash_plan: cash position %r (%s) has no actual NAV row — no anchor, skipped.",
                position.name,
                position.currency,
            )
            return CashPlanReport(skipped_no_anchor=1)

        target: dict[_date, Decimal] = {}
        if project and anchor is not None:
            target = {
                point.as_of_date: point.balance
                for point in project_cash_plan(
                    anchor_date=anchor.as_of_date,
                    anchor_balance=anchor.nav_value,
                    flows=flows,
                )
                if since is None or point.as_of_date >= since
            }

        currency = position.currency
        existing = await self._navs.list_by_investment_and_kind(position.id, _NAV_KIND)
        existing_by_date = {n.as_of_date: n for n in existing}

        inserted = updated = noop = 0
        skipped_excel = skipped_manual = skipped_foreign = 0

        for as_of_date, balance in target.items():
            current = existing_by_date.get(as_of_date)
            if current is None:
                await self._navs.upsert_computed(
                    investment_id=position.id,
                    as_of_date=as_of_date,
                    nav_kind=_NAV_KIND,
                    nav_value=balance,
                    currency=currency,
                    source=CASH_PLAN_SOURCE,
                    created_by=acting_user,
                )
                inserted += 1
            elif self._is_own(current):
                if current.nav_value == balance and current.currency == currency:
                    # Identical projected row already present — no write, so
                    # updated_at is untouched (idempotency).
                    noop += 1
                else:
                    await self._navs.upsert_computed(
                        investment_id=position.id,
                        as_of_date=as_of_date,
                        nav_kind=_NAV_KIND,
                        nav_value=balance,
                        currency=currency,
                        source=CASH_PLAN_SOURCE,
                        created_by=acting_user,
                    )
                    updated += 1
            elif current.ingest_origin == "excel":
                # A workbook plan column on this date. The book of record
                # wins; the projection yields (ADR-0098 §1 precedence).
                skipped_excel += 1
            elif current.ingest_origin == "manual":
                skipped_manual += 1
            else:
                # 'live' (impossible on cash — permanently live-ineligible,
                # ADR-0103 §1) or a 'system' row of a foreign source (a future
                # ADR-0104 overlay producer). Never overwrite what is not
                # ours; skip loudly so the collision surfaces.
                _LOG.warning(
                    "cash_plan: plan NAV row on %s for cash position %r "
                    "belongs to another writer (origin=%s source=%r) — "
                    "skipped, not overwritten.",
                    as_of_date,
                    position.name,
                    current.ingest_origin,
                    current.source,
                )
                skipped_foreign += 1

        stranded = [
            n.as_of_date
            for n in existing
            if self._is_own(n)
            and n.as_of_date not in target
            and (since is None or n.as_of_date >= since)
        ]
        deleted = 0
        if stranded:
            deleted = await self._navs.delete_system_plan_navs(
                position.id, stranded, source=CASH_PLAN_SOURCE
            )

        return CashPlanReport(
            inserted=inserted,
            updated=updated,
            noop=noop,
            skipped_excel=skipped_excel,
            skipped_manual=skipped_manual,
            skipped_foreign=skipped_foreign,
            deleted=deleted,
            positions=1 if project else 0,
            ignored_positions=0 if project else 1,
        )

    @staticmethod
    def _is_own(nav: InvestmentNavDTO) -> bool:
        """Whether this plan row is one this service wrote and owns.

        Ownership is the pair ``ingest_origin='system'`` **and**
        ``source='computed:cash-plan'`` — not the origin alone. ADR-0104's
        overlay producers will write ``'system'`` plan rows of their own; the
        source marker is what keeps the two write sets disjoint before they
        ever meet.
        """
        return nav.ingest_origin == "system" and nav.source == CASH_PLAN_SOURCE


__all__ = [
    "CASH_PLAN_SOURCE",
    "CashPlanMaterialisationService",
    "CashPlanPoint",
    "CashPlanReport",
    "PlanFlowEvent",
    "project_cash_plan",
]
