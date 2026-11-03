# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Where we stand — the live block of a case, composed at render time (ADR-0138 §6).

Where we stand is what the book says about a case **now**. It is composed
on every request from read paths that already exist and is never persisted,
except inside a milestone's ``frozen`` snapshot (ADR-0138 §2, D-CS-B-8). It
holds four readings:

1. **The finding's subject** — its live reading now beside its reading at
   opening (:class:`SubjectStand`).
2. **The book around the case** — the linked investments with their live
   figures (:class:`InvestmentReading`), the counterparty's funds in the
   book (:class:`CounterpartyReading`) and, per linked foreign currency, the
   FX rate at opening and now (:class:`FxReading`).
3. **The case's tickets** with their state (:class:`TicketReading`).
4. **The system lines** written to the record since opening
   (:class:`SystemLineReading`, ADR-0138 §7).

Two outputs feed the freeze of a milestone (D-CS-B-8): :func:`to_snapshot`
is its ``stand`` argument and :func:`book_values_for` its ``book_values``.
The list of cases prints one line per case, :func:`headline_for`, which is
the same text :func:`compose` sets on :attr:`WhereWeStand.headline`.

**Read-only, and no metric of its own** (D-CS-M-14). Nothing here writes,
and nothing here imports the pure calculation layer: the multiples and the
IRR arrive through :class:`~services.investments.investment_service.InvestmentService`,
the subject's status through
:func:`~services.watch_desk.family_status.read_family_status` on the one
resolution :func:`~services.watch_desk.overlay.resolve_watch_desk` returns.
No threshold is restated and no coverage is computed here (ADR-0116 §1).
``tests/regression/test_cases_stand_reads_only.py`` pins both rules.

**One context per request, one composition per case** (D-CS-B-11).
:meth:`StandContext.load` resolves the Watch Desk once and reads the
functional currency once; :func:`compose` and :func:`headline_for` read
one case against it. Nothing outlives the context object, which lives for
one request — the block must be live.

**"At opening" has two sources** (D-CS-B-13). The ``opened`` milestone's
frozen snapshot carries ``where_we_stand.subject.now`` once a case is opened
with a ``stand``; it becomes :attr:`SubjectStand.at_opening`. The ``opened``
entry's ``materiality_at_opening.lines`` (ADR-0107 C4) are the reading at
opening for every case, exposed as :attr:`SubjectStand.at_opening_lines`.
For the investments, the snapshot's raw ``where_we_stand`` block is exposed
as :attr:`WhereWeStand.at_opening`; no value is reconstructed from history.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import (
    CaseStateInvalid,
    CoverageInputMissing,
    CoverageInputOutOfRange,
    LimitSetNotEffective,
    MissingFxRateError,
)
from core.repositories.case_repository import CaseDTO, CaseEntryDTO, CaseRepository
from core.repositories.case_working_file_repository import (
    CaseFigureDTO,
    CaseInvestmentRepository,
)
from core.repositories.fx_rate_repository import FxRateRepository
from core.repositories.investment_cashflow_repository import InvestmentCashflowRepository
from core.repositories.investment_nav_repository import InvestmentNavRepository
from core.repositories.investment_repository import InvestmentDTO, InvestmentRepository
from core.repositories.irene_finding_repository import IreneFindingRepository
from core.repositories.tenant_repository import TenantRepository
from core.repositories.trade_ticket_repository import TradeTicketRepository
from services.investments.investment_service import InvestmentService
from services.money_format import format_money_compact
from services.watch_desk.family_status import (
    FIGURE_UNIT_DAYS,
    FIGURE_UNIT_PCT,
    FIGURE_UNIT_RATIO,
    FamilyStatus,
    read_family_status,
)
from services.watch_desk.overlay import WatchDeskResolution, resolve_watch_desk

logger = logging.getLogger(__name__)

#: The functional currency when the tenant row carries none (the column's
#: own default; a guard, not a data assumption).
_DEFAULT_FUNCTIONAL_CURRENCY = "EUR"

#: The limit families: a coverage reading against a ceiling.
_LIMIT_FAMILIES: frozenset[str] = frozenset({"saa", "anlv"})
#: The signal families: a figure against a threshold.
_SIGNAL_FAMILIES: frozenset[str] = frozenset({"price", "fx", "freshness", "liquidity"})
#: The press family: watched or muted, no figure.
_PRESS_FAMILY = "rss"

#: The Watch Desk read failures a case page tolerates (coverage unavailable).
_TOLERATED_READ_FAILURES = (
    LimitSetNotEffective,
    CoverageInputMissing,
    CoverageInputOutOfRange,
    MissingFxRateError,
)

#: The ``book`` figure metrics :func:`book_values_for` resolves (D-CS-B-14).
_BOOK_METRICS: frozenset[str] = frozenset(
    {"nav", "nav_as_of", "commitment", "tvpi", "dpi", "rvpi", "irr", "currency"}
)
#: The metrics whose ``as_of`` is the NAV date; the rest carry none.
_NAV_DATED_METRICS: frozenset[str] = frozenset({"nav", "nav_as_of", "tvpi", "dpi", "rvpi", "irr"})

_NOTE_NOT_WATCHED = "not watched"
_NOTE_SWITCHED_OFF = "switched off"
_NOTE_COVERAGE_UNAVAILABLE = "coverage unavailable"


# ---------------------------------------------------------------------------
# The readings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubjectReading:
    """One reading of a finding's subject.

    Attributes:
        subject_key: ``{family}:{class_key}`` as the finding names it.
        family: The part before the colon (empty for an unkeyed subject).
        class_key: The part after the colon, or ``None``.
        status: ``OK`` / ``WARN`` / ``BREACH``, or ``None`` without one.
        status_label: What the list prints for the status.
        value: The measured figure in ``unit``, or ``None``.
        ceiling: The ceiling (limit families) or threshold (signal
            families) in ``unit``, or ``None``.
        unit: ``pct`` / ``days`` / ``ratio``, or ``None`` without a figure.
        as_of: The date the figure was observed at, or ``None``.
        muted: Whether the subject is muted.
        note: Why there is no figure — the producer's reason, or
            ``switched off`` / ``not watched`` / ``coverage unavailable``.
    """

    subject_key: str
    family: str
    class_key: str | None
    status: str | None
    status_label: str
    value: Decimal | None
    ceiling: Decimal | None
    unit: str | None
    as_of: date | None
    muted: bool
    note: str | None


@dataclass(frozen=True)
class SubjectStand:
    """The finding's subject now, and at opening (D-CS-B-13).

    Attributes:
        now: The live reading, or ``None`` when the Watch Desk could not be
            read today.
        at_opening: The structured reading of the ``opened`` snapshot, or
            ``None`` when the case was opened without a ``stand``.
        at_opening_lines: The materiality lines frozen at opening (ADR-0107
            C4); possibly empty.
        unavailable_reason: Why ``now`` is ``None``, else ``None``.
    """

    now: SubjectReading | None
    at_opening: SubjectReading | None
    at_opening_lines: tuple[str, ...]
    unavailable_reason: str | None


@dataclass(frozen=True)
class InvestmentReading:
    """One investment with its live book figures.

    Every figure is the book's own: the latest actual NAV, and the last
    point of the multiples and IRR series the investment detail charts.

    Attributes:
        investment_id: The investment.
        name: Its name.
        manager_name: Its manager, or ``None``.
        currency: Its position currency.
        commitment_amount: The commitment, or ``None``.
        nav_value: The latest actual NAV, or ``None``.
        nav_as_of: That NAV's date, or ``None``.
        tvpi: The latest TVPI, or ``None``.
        dpi: The latest DPI, or ``None``.
        rvpi: The latest RVPI, or ``None``.
        irr: The latest IRR since inception as a fraction, or ``None``.
        is_active: Whether the investment is active.
        linked: Whether the case links it (``False`` for counterparty-only
            and figure-only rows).
    """

    investment_id: UUID
    name: str
    manager_name: str | None
    currency: str
    commitment_amount: Decimal | None
    nav_value: Decimal | None
    nav_as_of: date | None
    tvpi: Decimal | None
    dpi: Decimal | None
    rvpi: Decimal | None
    irr: Decimal | None
    is_active: bool
    linked: bool


@dataclass(frozen=True)
class CounterpartyReading:
    """The counterparty's active funds in the book — a listing, never a sum (D-CS-B-12).

    Attributes:
        name: The counterparty as matched against ``manager_name``.
        funds: Its active investments, each with its own figures; the
            count is ``len(funds)``.
    """

    name: str
    funds: tuple[InvestmentReading, ...]


@dataclass(frozen=True)
class FxReading:
    """One linked foreign currency's rate at opening and now.

    Attributes:
        currency: The position currency.
        functional_currency: The tenant's functional currency.
        rate_now: The latest rate at or before today, or ``None``.
        rate_now_as_of: Its date, or ``None``.
        rate_at_opening: The latest rate at or before the opening date, or
            ``None``.
        rate_at_opening_as_of: Its date, or ``None``.
    """

    currency: str
    functional_currency: str
    rate_now: Decimal | None
    rate_now_as_of: date | None
    rate_at_opening: Decimal | None
    rate_at_opening_as_of: date | None


@dataclass(frozen=True)
class TicketReading:
    """One trade ticket carrying the case, with its state.

    Attributes:
        ticket_id: The ticket.
        ticket_number: Its tenant-sequential number.
        kind: Its object kind.
        direction: Its direction.
        status: Its lifecycle status.
        investment_id: The traded investment, or ``None``.
        investment_name: That investment's name, or ``None``.
        trade_date: The trade date.
        net_amount: The net amount, or ``None``.
        currency: The ticket currency.
        booked_at: When it was booked, or ``None``.
    """

    ticket_id: UUID
    ticket_number: int
    kind: str
    direction: str
    status: str
    investment_id: UUID | None
    investment_name: str | None
    trade_date: date
    net_amount: Decimal | None
    currency: str
    booked_at: datetime | None


@dataclass(frozen=True)
class SystemLineReading:
    """One system line of the record (ADR-0138 §7).

    ``event`` and ``text`` are the writers' payload contract (P-CS-2b); an
    absent key reads as an empty string.

    Attributes:
        entry_id: The record entry.
        event: ``payload["event"]``.
        text: ``payload["text"]``.
        created_at: When it was written.
        payload: The whole payload.
    """

    entry_id: UUID
    event: str
    text: str
    created_at: datetime
    payload: dict


@dataclass(frozen=True)
class WhereWeStand:
    """Where we stand for one case, composed now (ADR-0138 §6).

    Attributes:
        case_id: The case.
        composed_at: The context's ``now``.
        subject: The finding's subject, or ``None`` for a case without a
            finding.
        investments: The linked investments, in link order.
        counterparty: The counterparty's funds, or ``None`` without a
            counterparty.
        fx: One reading per distinct linked currency other than the
            functional currency.
        tickets: The case's tickets, newest number first.
        system_lines: The system lines, oldest first.
        headline: The list's one line.
        at_opening: The ``opened`` snapshot's raw ``where_we_stand`` block,
            or ``None``.
    """

    case_id: UUID
    composed_at: datetime
    subject: SubjectStand | None
    investments: tuple[InvestmentReading, ...]
    counterparty: CounterpartyReading | None
    fx: tuple[FxReading, ...]
    tickets: tuple[TicketReading, ...]
    system_lines: tuple[SystemLineReading, ...]
    headline: str
    at_opening: dict | None = None
    # Every investment compose read (linked, counterparty and figure-only),
    # for book_values_for. Private: not part of the snapshot.
    _readings_by_id: dict[UUID, InvestmentReading] = field(
        default_factory=dict, repr=False, compare=False
    )


# ---------------------------------------------------------------------------
# The context — one per request (D-CS-B-11)
# ---------------------------------------------------------------------------


@dataclass
class StandContext:
    """What every case of one request is read against.

    Built once per request by :meth:`load`; never cached across requests.

    Attributes:
        session: The tenant-scoped session the per-case reads run on.
        now: The request instant.
        resolution: The tenant's Watch Desk resolution at ``now``.
        status: The family status at ``now``, or ``None`` when the Watch
            Desk could not be read.
        status_unavailable_reason: Why ``status`` is ``None``, else ``None``.
        functional_currency: The tenant's functional currency.
    """

    session: AsyncSession
    now: datetime
    resolution: WatchDeskResolution
    status: FamilyStatus | None
    status_unavailable_reason: str | None
    functional_currency: str

    @classmethod
    async def load(cls, session: AsyncSession, *, now: datetime) -> StandContext:
        """Resolve the Watch Desk and read the family status once for this request.

        A coverage failure the briefing material tolerates degrades to
        ``status = None`` with a reason, so a case page never fails because
        the Watch Desk cannot evaluate today. A calibration that no longer
        composes still raises: the resolution's loud failure is deliberate
        (ADR-0116 §5).

        Args:
            session: A tenant-scoped session.
            now: The request instant (timezone-aware).

        Returns:
            The context.
        """
        resolution = await resolve_watch_desk(session, as_of=now)
        status: FamilyStatus | None
        reason: str | None = None
        try:
            status = await read_family_status(session, resolution=resolution, now=now)
        except _TOLERATED_READ_FAILURES as exc:
            logger.info("where we stand: the Watch Desk status is unavailable (%s).", exc)
            status = None
            reason = f"The Watch Desk cannot evaluate today: {exc}"
        functional = await TenantRepository(session).get_current_functional_currency()
        return cls(
            session=session,
            now=now,
            resolution=resolution,
            status=status,
            status_unavailable_reason=reason,
            functional_currency=functional or _DEFAULT_FUNCTIONAL_CURRENCY,
        )


# ---------------------------------------------------------------------------
# The subject (ADR-0116 §1: read off the family status, never recomputed)
# ---------------------------------------------------------------------------


def _split_subject(subject_key: str) -> tuple[str, str | None]:
    """Return ``(family, class_key)``; an unkeyed subject has no class key."""
    family, sep, class_key = subject_key.partition(":")
    if not sep or not class_key:
        return (family if sep else ""), None
    return family, class_key


def _absent(subject_key: str, family: str, class_key: str | None, note: str) -> SubjectReading:
    """Return a reading with no figure, carrying why."""
    return SubjectReading(
        subject_key=subject_key,
        family=family,
        class_key=class_key,
        status=None,
        status_label=note,
        value=None,
        ceiling=None,
        unit=None,
        as_of=None,
        muted=False,
        note=note,
    )


def _subject_now(ctx: StandContext, subject_key: str) -> SubjectReading | None:
    """Return the subject's live reading off the context's family status.

    The lookup is by ``subject_key`` in the typed records the Watch Desk
    monitor renders from; the family decides which records to search.

    Returns:
        The reading, or ``None`` when the context holds no status.
    """
    status = ctx.status
    if status is None:
        return None
    family, class_key = _split_subject(subject_key)

    if family in _LIMIT_FAMILIES:
        group = next((g for g in status.limits if g.family == family), None)
        if group is None:
            return _absent(subject_key, family, class_key, _NOTE_SWITCHED_OFF)
        if status.coverage_as_of is None:
            return _absent(subject_key, family, class_key, _NOTE_COVERAGE_UNAVAILABLE)
        limit = next((s for s in group.subjects if s.subject_key == subject_key), None)
        if limit is None:
            return _absent(subject_key, family, class_key, _NOTE_NOT_WATCHED)
        return SubjectReading(
            subject_key=subject_key,
            family=family,
            class_key=limit.class_key,
            status=limit.status,
            status_label=limit.status,
            value=limit.coverage_pct,
            ceiling=limit.max_pct,
            unit=FIGURE_UNIT_PCT,
            as_of=status.coverage_as_of,
            muted=limit.muted,
            note=None,
        )

    if family in _SIGNAL_FAMILIES:
        if not ctx.resolution.is_family_enabled(family):
            return _absent(subject_key, family, class_key, _NOTE_SWITCHED_OFF)
        signal = next((s for s in status.signals if s.subject_key == subject_key), None)
        if signal is None:
            return _absent(subject_key, family, class_key, _NOTE_NOT_WATCHED)
        figure = signal.figure
        return SubjectReading(
            subject_key=subject_key,
            family=family,
            class_key=class_key,
            status=signal.status,
            status_label=signal.status_label,
            value=figure.value if figure is not None else None,
            ceiling=figure.threshold if figure is not None else None,
            unit=figure.unit if figure is not None else None,
            as_of=signal.as_of,
            muted=signal.muted,
            note=signal.no_data_reason,
        )

    if family == _PRESS_FAMILY:
        if status.press is None:
            return _absent(subject_key, family, class_key, _NOTE_SWITCHED_OFF)
        tag = next((p for p in status.press if p.subject_key == subject_key), None)
        if tag is None:
            return _absent(subject_key, family, class_key, _NOTE_NOT_WATCHED)
        return SubjectReading(
            subject_key=subject_key,
            family=family,
            class_key=class_key,
            status=None,
            status_label="muted" if tag.muted else "watched",
            value=None,
            ceiling=None,
            unit=None,
            as_of=None,
            muted=tag.muted,
            note=None,
        )

    return _absent(subject_key, family, class_key, _NOTE_NOT_WATCHED)


def _parse_decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _parse_date(value: Any) -> date | None:
    return None if value is None else date.fromisoformat(str(value))


def _subject_from_snapshot(frozen_payload: dict) -> SubjectReading | None:
    """Rebuild the structured reading at opening from a ``frozen`` payload, if any."""
    stand = (frozen_payload.get("file") or {}).get("where_we_stand")
    if not isinstance(stand, dict):
        return None
    subject = stand.get("subject")
    now = subject.get("now") if isinstance(subject, dict) else None
    if not isinstance(now, dict):
        return None
    return SubjectReading(
        subject_key=str(now.get("subject_key", "")),
        family=str(now.get("family", "")),
        class_key=now.get("class_key"),
        status=now.get("status"),
        status_label=str(now.get("status_label", "")),
        value=_parse_decimal(now.get("value")),
        ceiling=_parse_decimal(now.get("ceiling")),
        unit=now.get("unit"),
        as_of=_parse_date(now.get("as_of")),
        muted=bool(now.get("muted", False)),
        note=now.get("note"),
    )


# ---------------------------------------------------------------------------
# The headline — one formatter for the list and the block
# ---------------------------------------------------------------------------


def _figure_text(value: Decimal, unit: str | None) -> str:
    if unit == FIGURE_UNIT_DAYS:
        days = int(value)
        return f"{days} day{'' if days == 1 else 's'}"
    if unit == FIGURE_UNIT_RATIO:
        return f"{float(value):.2f}×"
    return f"{float(value):.1f} %"


def _subject_line(
    subject_key: str,
    reading: SubjectReading | None,
    reason: str | None,
    *,
    label: str | None = None,
) -> str:
    """Return the subject's one line, naming it ``label`` when given, else the key."""
    name = label or subject_key
    if reading is None:
        return f"{name}: {reason or 'not available'}"
    if reading.value is None:
        return f"{name}: {reading.status_label}"
    value = _figure_text(reading.value, reading.unit)
    if reading.ceiling is None:
        return f"{name}: {value} ({reading.status_label})"
    ceiling = _figure_text(reading.ceiling, reading.unit)
    if reading.family in _LIMIT_FAMILIES:
        return f"{name} at {value} of the {ceiling} ceiling ({reading.status_label})"
    return f"{name}: {value} against {ceiling} ({reading.status_label})"


def _investment_line(
    name: str, nav_value: Decimal | None, currency: str, nav_as_of: date | None
) -> str:
    """Return ``name · NAV · as-of`` for the first linked investment."""
    if nav_value is None:
        return f"{name} · NAV not available"
    nav = format_money_compact(float(nav_value), currency)
    as_of = nav_as_of.isoformat() if nav_as_of is not None else "no date"
    return f"{name} · NAV {nav} · {as_of}"


async def headline_for(
    ctx: StandContext,
    case: CaseDTO,
    *,
    subject_label: Callable[[str], str] | None = None,
) -> str:
    """Return the list's one line for ``case`` — cheap by construction.

    A finding case reads its finding and states the subject off the
    context; otherwise the first linked investment's latest NAV; else
    ``""``. A closed case returns ``""``: its row reads the frozen headline.

    ``subject_label`` is a presentation mapping the list supplies (the Watch
    Desk monitor's subject label, P-CS-3a); by default the stand prints the
    raw subject key, as :func:`compose` does.

    Args:
        ctx: The request's context.
        case: The case.
        subject_label: Maps the finding's subject key to the name the line
            prints; ``None`` prints the key.

    Returns:
        The headline, possibly empty.
    """
    if case.state == "closed":
        return ""
    session = ctx.session
    if case.finding_id is not None:
        finding = await IreneFindingRepository(session).get(case.finding_id)
        if finding is not None:
            reading = _subject_now(ctx, finding.subject_key)
            return _subject_line(
                finding.subject_key,
                reading,
                ctx.status_unavailable_reason,
                label=subject_label(finding.subject_key) if subject_label is not None else None,
            )
    links = await CaseInvestmentRepository(session).list_for_case(case.id)
    if not links:
        return ""
    first = await InvestmentRepository(session).get_by_id(links[0].investment_id)
    if first is None:
        return ""
    nav = await InvestmentNavRepository(session).get_latest_actual(first.id)
    return _investment_line(
        first.name,
        nav.nav_value if nav is not None else None,
        first.currency,
        nav.as_of_date if nav is not None else None,
    )


# ---------------------------------------------------------------------------
# The composition
# ---------------------------------------------------------------------------


def _last_decimal(value: Any) -> Decimal | None:
    """Return a series cell as ``Decimal``; ``None`` for a null or NaN."""
    if value is None:
        return None
    number = float(value)
    if math.isnan(number):
        return None
    return Decimal(str(number))


async def _read_investment(
    session: AsyncSession, investment: InvestmentDTO, *, linked: bool
) -> InvestmentReading:
    """Read one investment's latest NAV and its latest multiples and IRR."""
    nav = await InvestmentNavRepository(session).get_latest_actual(investment.id)
    service = InvestmentService(
        InvestmentRepository(session),
        InvestmentNavRepository(session),
        InvestmentCashflowRepository(session),
    )
    bundle = await service.get_charts_data(investment.id, include_irr=True)
    tvpi = dpi = rvpi = irr = None
    if bundle is not None:
        multiples = bundle.rolling_multiples
        if len(multiples) > 0:
            last = multiples.iloc[-1]
            tvpi = _last_decimal(last["tvpi"])
            dpi = _last_decimal(last["dpi"])
            rvpi = _last_decimal(last["rvpi"])
        if len(bundle.rolling_irr) > 0:
            irr = _last_decimal(bundle.rolling_irr.iloc[-1])
    return InvestmentReading(
        investment_id=investment.id,
        name=investment.name,
        manager_name=investment.manager_name,
        currency=investment.currency,
        commitment_amount=investment.commitment_amount,
        nav_value=nav.nav_value if nav is not None else None,
        nav_as_of=nav.as_of_date if nav is not None else None,
        tvpi=tvpi,
        dpi=dpi,
        rvpi=rvpi,
        irr=irr,
        is_active=investment.is_active,
        linked=linked,
    )


def _figure_investment_id(figure: CaseFigureDTO) -> UUID | None:
    """Return the investment a ``book`` figure references, or ``None``."""
    if figure.source != "book":
        return None
    raw = (figure.source_ref or {}).get("investment_id")
    try:
        return UUID(str(raw)) if raw is not None else None
    except ValueError:
        return None


def _counterparty_name(case: CaseDTO, linked: Sequence[InvestmentDTO]) -> str | None:
    """Return the case's counterparty, or the one manager every linked investment shares."""
    if case.counterparty and case.counterparty.strip():
        return case.counterparty.strip()
    managers = {inv.manager_name for inv in linked}
    if len(managers) == 1:
        (manager,) = managers
        return manager or None
    return None


async def _fx_rates(
    session: AsyncSession, currencies: list[str], on: date
) -> dict[str, tuple[Decimal, date]]:
    """Return each currency's latest rate at or before ``on``.

    The frame's carry-forward anchor row is the rate at or before the
    window's lower bound; with a one-day window the last row per currency
    is that rate (or the day's own row).
    """
    if not currencies:
        return {}
    frame = await FxRateRepository(session).load_rates_frame(currencies, from_date=on, to_date=on)
    rates: dict[str, tuple[Decimal, date]] = {}
    for row in frame.to_dict("records"):
        as_of = row["as_of_date"]
        as_of_date = as_of.date() if isinstance(as_of, datetime) else as_of
        rates[str(row["currency"])] = (Decimal(str(row["rate_to_reference"])), as_of_date)
    return rates


def _opened_entry(entries: Sequence[CaseEntryDTO]) -> CaseEntryDTO | None:
    return next((e for e in entries if e.kind == "opened"), None)


async def compose(
    ctx: StandContext,
    case: CaseDTO,
    *,
    figures: Sequence[CaseFigureDTO] = (),
) -> WhereWeStand:
    """Compose Where we stand for one open or waiting case.

    Reads, in order: the finding, the ``opened`` entry and its frozen
    snapshot, the investment links, the investments (linked, referenced by a
    ``book`` figure, and the counterparty's active funds), per investment its
    latest NAV and its multiples and IRR, the FX rates at opening and now,
    the tickets and the system lines.

    Args:
        ctx: The request's context.
        case: The case.
        figures: The case's figures, so a ``book`` figure's investment is
            read too (:func:`book_values_for`).

    Returns:
        The composed block.

    Raises:
        CaseStateInvalid: If the case is closed — a closed case renders its
            last frozen snapshot (ADR-0138 §6).
    """
    if case.state == "closed":
        raise CaseStateInvalid(
            f"Case {case.id} is closed; a closed case renders its last frozen "
            "snapshot — ADR-0138 §6.",
            field="state",
        )
    session = ctx.session
    cases = CaseRepository(session)
    investments_repo = InvestmentRepository(session)

    # 1. The finding.
    finding = (
        await IreneFindingRepository(session).get(case.finding_id)
        if case.finding_id is not None
        else None
    )

    # 2. The opened entry and its frozen snapshot.
    entries = await cases.list_entries(case.id)
    opened = _opened_entry(entries)
    frozen = await cases.get_frozen_for(opened.id) if opened is not None else None
    frozen_stand = (frozen.payload.get("file") or {}).get("where_we_stand") if frozen else None

    # 3. The links, then 4. the investments.
    links = await CaseInvestmentRepository(session).list_for_case(case.id)
    linked_ids = [link.investment_id for link in links]
    dtos: dict[UUID, InvestmentDTO] = {}
    for investment_id in linked_ids:
        dto = await investments_repo.get_by_id(investment_id)
        if dto is not None:
            dtos[investment_id] = dto
    linked_dtos = [dtos[i] for i in linked_ids if i in dtos]

    for figure in figures:
        figure_id = _figure_investment_id(figure)
        if figure_id is not None and figure_id not in dtos:
            dto = await investments_repo.get_by_id(figure_id)
            if dto is not None:
                dtos[figure_id] = dto

    counterparty_name = _counterparty_name(case, linked_dtos)
    counterparty_ids: list[UUID] = []
    if counterparty_name is not None:
        for dto in await investments_repo.list_active():
            if dto.manager_name == counterparty_name:
                counterparty_ids.append(dto.id)
                dtos.setdefault(dto.id, dto)

    linked_set = set(linked_ids)
    readings: dict[UUID, InvestmentReading] = {}
    for investment_id, dto in dtos.items():
        readings[investment_id] = await _read_investment(
            session, dto, linked=investment_id in linked_set
        )

    # 5. FX at opening and now, per distinct linked foreign currency.
    foreign: list[str] = []
    for dto in linked_dtos:
        if dto.currency != ctx.functional_currency and dto.currency not in foreign:
            foreign.append(dto.currency)
    rates_now = await _fx_rates(session, foreign, ctx.now.date())
    rates_opening = await _fx_rates(session, foreign, case.opened_at.date())
    fx = tuple(
        FxReading(
            currency=currency,
            functional_currency=ctx.functional_currency,
            rate_now=rates_now[currency][0] if currency in rates_now else None,
            rate_now_as_of=rates_now[currency][1] if currency in rates_now else None,
            rate_at_opening=rates_opening[currency][0] if currency in rates_opening else None,
            rate_at_opening_as_of=rates_opening[currency][1] if currency in rates_opening else None,
        )
        for currency in foreign
    )

    # 6. The tickets.
    tickets: list[TicketReading] = []
    for ticket in await TradeTicketRepository(session).list_by_case(case.id):
        investment_name: str | None = None
        if ticket.investment_id is not None:
            known = dtos.get(ticket.investment_id)
            if known is None:
                known = await investments_repo.get_by_id(ticket.investment_id)
                if known is not None:
                    dtos[ticket.investment_id] = known
            investment_name = known.name if known is not None else None
        tickets.append(
            TicketReading(
                ticket_id=ticket.id,
                ticket_number=ticket.ticket_number,
                kind=ticket.kind,
                direction=ticket.direction,
                status=ticket.status,
                investment_id=ticket.investment_id,
                investment_name=investment_name,
                trade_date=ticket.trade_date,
                net_amount=ticket.net_amount,
                currency=ticket.currency,
                booked_at=ticket.booked_at,
            )
        )

    # 7. The system lines.
    system_lines = tuple(
        SystemLineReading(
            entry_id=entry.id,
            event=str(entry.payload.get("event", "")),
            text=str(entry.payload.get("text", "")),
            created_at=entry.created_at,
            payload=entry.payload,
        )
        for entry in entries
        if entry.kind == "system_line"
    )

    # The subject, then the headline.
    subject: SubjectStand | None = None
    headline = ""
    if finding is not None:
        now_reading = _subject_now(ctx, finding.subject_key)
        lines = (opened.payload if opened is not None else {}).get("materiality_at_opening", {})
        subject = SubjectStand(
            now=now_reading,
            at_opening=_subject_from_snapshot(frozen.payload) if frozen is not None else None,
            at_opening_lines=tuple(str(line) for line in lines.get("lines", [])),
            unavailable_reason=ctx.status_unavailable_reason if now_reading is None else None,
        )
        headline = _subject_line(finding.subject_key, now_reading, ctx.status_unavailable_reason)
    elif linked_dtos:
        first = readings[linked_dtos[0].id]
        headline = _investment_line(first.name, first.nav_value, first.currency, first.nav_as_of)

    counterparty = (
        CounterpartyReading(
            name=counterparty_name, funds=tuple(readings[i] for i in counterparty_ids)
        )
        if counterparty_name is not None
        else None
    )

    return WhereWeStand(
        case_id=case.id,
        composed_at=ctx.now,
        subject=subject,
        investments=tuple(readings[dto.id] for dto in linked_dtos),
        counterparty=counterparty,
        fx=fx,
        tickets=tuple(tickets),
        system_lines=system_lines,
        headline=headline,
        at_opening=frozen_stand if isinstance(frozen_stand, dict) else None,
        _readings_by_id=readings,
    )


# ---------------------------------------------------------------------------
# The two freeze inputs (D-CS-B-8)
# ---------------------------------------------------------------------------


def _multiple_text(value: Decimal) -> str:
    return f"{float(value):.2f}x"


def _book_value(reading: InvestmentReading, metric: str) -> str | None:
    """Return one metric of a reading as display text, or ``None``."""
    if metric in ("nav", "nav_as_of") and reading.nav_value is None:
        return None
    if metric == "nav":
        assert reading.nav_value is not None
        return format_money_compact(float(reading.nav_value), reading.currency)
    if metric == "nav_as_of":
        return reading.nav_as_of.isoformat() if reading.nav_as_of is not None else None
    if metric == "commitment":
        if reading.commitment_amount is None:
            return None
        return format_money_compact(float(reading.commitment_amount), reading.currency)
    if metric == "currency":
        return reading.currency
    if metric == "irr":
        return f"{float(reading.irr) * 100.0:.1f}%" if reading.irr is not None else None
    value: Decimal | None = getattr(reading, metric)
    return _multiple_text(value) if value is not None else None


def book_values_for(stand: WhereWeStand, figures: Sequence[CaseFigureDTO]) -> dict[str, dict]:
    """Resolve each ``book`` figure against the readings :func:`compose` gathered (D-CS-B-14).

    Pure. An unknown metric, an investment :func:`compose` did not read, or
    a figure the book holds no value for resolves to
    ``{"value_text": None, "as_of": None}`` — "not available", never raised.
    Figures of any other source are absent from the result.

    Args:
        stand: The composed block (pass the case's figures to
            :func:`compose` so their investments are read).
        figures: The case's figures.

    Returns:
        ``str(figure_id) → {"value_text", "as_of"}`` for every ``book``
        figure — the freeze's ``book_values``.
    """
    resolved: dict[str, dict] = {}
    for figure in figures:
        if figure.source != "book":
            continue
        metric = str((figure.source_ref or {}).get("metric", ""))
        investment_id = _figure_investment_id(figure)
        reading = stand._readings_by_id.get(investment_id) if investment_id else None
        value_text: str | None = None
        as_of: str | None = None
        if reading is not None and metric in _BOOK_METRICS:
            value_text = _book_value(reading, metric)
            if value_text is not None and metric in _NAV_DATED_METRICS:
                as_of = reading.nav_as_of.isoformat() if reading.nav_as_of is not None else None
        resolved[str(figure.id)] = {"value_text": value_text, "as_of": as_of}
    return resolved


def _snapshot_value(value: Any) -> Any:
    """Return ``value`` JSON-ready, dataclasses as dicts of their public fields."""
    if is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _snapshot_value(getattr(value, f.name))
            for f in fields(value)
            if not f.name.startswith("_")
        }
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_snapshot_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _snapshot_value(item) for key, item in value.items()}
    return value


def to_snapshot(stand: WhereWeStand) -> dict:
    """Return the block as the freeze's ``stand`` argument (D-CS-B-8).

    The same shape as the dataclasses, keys named as the fields;
    ``Decimal`` as ``str``, dates and datetimes as ISO 8601, UUIDs as
    ``str``, tuples as lists. The private readings index is left out.

    Args:
        stand: The composed block.

    Returns:
        A JSON-serialisable dict.
    """
    snapshot = _snapshot_value(stand)
    assert isinstance(snapshot, dict)
    return snapshot


__all__ = [
    "CounterpartyReading",
    "FxReading",
    "InvestmentReading",
    "StandContext",
    "SubjectReading",
    "SubjectStand",
    "SystemLineReading",
    "TicketReading",
    "WhereWeStand",
    "book_values_for",
    "compose",
    "headline_for",
    "to_snapshot",
]
