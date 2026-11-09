# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""System lines — the program's mechanical record on a case (ADR-0138 §7).

A system line is a ``case_entries`` row of kind ``system_line`` written by
the program, never by a PM and never by a model: ``actor = 'system'``, no
acting user, a machine-readable payload and one fixed template text.

**The event vocabulary.** ``subject_within_ceiling`` · ``nav_restated`` ·
``press_cluster`` · ``ticket_booked`` · ``job_requested`` · ``job_landed``.
All but ``press_cluster`` have their template and writer here (P-CS-2b,
P-CS-2c); ``press_cluster`` stays a reserved constant until MC rules on its
subject (Q-CS-B-2). ``waiting_lifted`` is a milestone, not a line.

**The one writer.** Every line goes through
:meth:`core.repositories.case_repository.CaseRepository.add_system_line`,
called from this module and from nowhere else. A closed case receives
nothing; a closed case in a target set is skipped, never raised on.

**The write-and-wake rule** (ADR-0138 §4, D-CS-M-4, D-CS-B-22). Every
writer goes through one private path, ``_write_line``: it writes the line
and, when a line was written **and** the case is waiting, lifts the wait in
the same transaction —
:meth:`~core.repositories.case_repository.CaseRepository.lift_wait` with
``reason='event'`` and the line's event, subject and entry id. Line first,
lift second, so the lift's frozen snapshot shows the line. A deduplicated
write writes nothing and lifts nothing. ``_write_line`` hands back the case
as it stands after the write, and a writer that writes several lines to
one case carries that forward, so a second line on a just-lifted case never
acts on a stale ``waiting`` state. Wake events are not configurable: every
event of the vocabulary wakes. (The date wake is the tick runner's, not a
line: :func:`services.scheduler.tick_runner.run_case_wake_tick`.)

**The dedup rule.** One line per ``(event, subject)`` per case, checked by
the repository before the insert. A restated NAV for the same investment
and as-of is written once — the first report of an as-of is the event.

**The template rule** (D-CS-B-20). Each event has a fixed English sentence
filled with the event's own figures, composed in this module and nowhere
else. Irene's prose never reaches a case: ``payload['text']`` only ever
holds one of the templates below.

**The seams.**

* ``subject_within_ceiling`` — Irene's beat (``services.irene.beat``) after
  both deltas, for every falling edge, on the open or waiting cases opened
  from a finding on that subject (resolution ``opened_case``). A signal
  subject reads "back within its threshold".
* ``nav_restated`` — the two NAV routes and the Excel import
  (``web/routes/investments.py``, ``web/routes/data_import.py``) capture the
  linked investments' reported ``actual`` series before the write and diff
  it after (D-CS-B-17). A ticket booking's NAV row never passes a seam, so
  it never produces a ``nav_restated`` line (D-CS-M-10); its event is
  ``ticket_booked``. Plan rows and computed rows (``basis = 'computed'``)
  are not reported NAVs and are never captured.
* ``ticket_booked`` — :meth:`services.transactions.ticket_service.TicketService.book`,
  once the ticket is ``booked``, when the ticket carries a ``case_id``
  (D-CS-B-23).
* ``job_requested`` / ``job_landed`` — *Summarise into the file*
  (``post_proposals_summarise`` in ``web/routes/chat.py``), when a Shirley
  job for the case is started and when its proposal package is stored in
  ``case_proposals`` (ADR-0138 §7, §8; D-CS-M-12).
* ``press_cluster`` — reserved; no template and no writer until Q-CS-B-2 is
  ruled.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import CaseClosedError, CaseStateInvalid
from core.repositories.case_repository import CaseDTO, CaseEntryDTO, CaseRepository
from core.repositories.case_working_file_repository import CaseInvestmentRepository
from core.repositories.investment_nav_repository import InvestmentNavRepository
from core.repositories.investment_repository import InvestmentRepository
from core.repositories.trade_ticket_repository import TradeTicketDTO
from services.money_format import format_money_compact

logger = logging.getLogger(__name__)

EVENT_SUBJECT_WITHIN_CEILING = "subject_within_ceiling"
EVENT_NAV_RESTATED = "nav_restated"
EVENT_TICKET_BOOKED = "ticket_booked"
EVENT_JOB_REQUESTED = "job_requested"
EVENT_JOB_LANDED = "job_landed"
# Reserved: no template and no writer until MC rules on its subject (Q-CS-B-2).
EVENT_PRESS_CLUSTER = "press_cluster"

#: The longest request text a ``job_requested`` line quotes, ellipsis included.
REQUEST_TEXT_CLIP = 120

#: The limit families — a subject with a ceiling. Every other family is a
#: signal with a threshold the tenant chose (D-CS-B-19).
LIMIT_FAMILIES: frozenset[str] = frozenset({"saa", "anlv"})

#: ``nav_restated`` change kinds.
CHANGE_NEW = "new"
CHANGE_RESTATED = "restated"

#: One investment's reported NAV series: as-of → value.
NavSeries = dict[date, Decimal]

#: A composed line: ``(subject, text, payload)``.
Line = tuple[str, str, dict[str, Any]]


def _str_or_none(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


# ---------------------------------------------------------------------------
# Templates (pure)
# ---------------------------------------------------------------------------


def limit_within_ceiling_line(
    *,
    subject_key: str,
    family: str,
    status: str,
    coverage_pct: Decimal,
    max_pct: Decimal | None,
    as_of: date,
    finding_id: UUID | None,
) -> Line:
    """Compose the ``subject_within_ceiling`` line of a limit subject.

    Args:
        subject_key: The limit subject (``saa:…`` / ``anlv:…``).
        family: The limit family.
        status: The coverage status now (``OK`` / ``WARN``).
        coverage_pct: The coverage now, in percent.
        max_pct: The ceiling, in percent, or ``None`` when the row has none.
        as_of: The date the reading is stated for.
        finding_id: The case's origin finding.

    Returns:
        ``(subject, text, payload)``.
    """
    if max_pct is None:
        text = (
            f"{subject_key} is back within its ceiling: {coverage_pct:.1f} % "
            f"({status}) as of {as_of.isoformat()}."
        )
    else:
        text = (
            f"{subject_key} is back within its ceiling: {coverage_pct:.1f} % of the "
            f"{max_pct:.1f} % ceiling ({status}) as of {as_of.isoformat()}."
        )
    payload: dict[str, Any] = {
        "family": family,
        "status": status,
        "coverage_pct": str(coverage_pct),
        "max_pct": _str_or_none(max_pct),
        "as_of": as_of.isoformat(),
        "finding_id": None if finding_id is None else str(finding_id),
    }
    return subject_key, text, payload


def signal_within_threshold_line(
    *,
    subject_key: str,
    family: str,
    display_name: str,
    status: str,
    as_of: date,
    finding_id: UUID | None,
    value: Decimal | None = None,
    threshold: Decimal | None = None,
    unit: str | None = None,
) -> Line:
    """Compose the ``subject_within_ceiling`` line of a signal subject.

    A signal has no ceiling — it has a threshold the tenant chose — so the
    text reads "back within its threshold" (D-CS-B-19).

    Args:
        subject_key: The signal subject (``price:…`` / ``fx:…`` / …).
        family: The signal family.
        display_name: The instrument or pair label.
        status: The human status word (Calm / Approaching / Triggered).
        as_of: The date the reading is stated for.
        finding_id: The case's origin finding.
        value: The observed move, or ``None``.
        threshold: The trigger threshold, or ``None``.
        unit: The unit of ``value`` / ``threshold``, or ``None``.

    Returns:
        ``(subject, text, payload)``.
    """
    text = f"{display_name} is back within its threshold ({status}) as of {as_of.isoformat()}."
    payload: dict[str, Any] = {
        "family": family,
        "status": status,
        "value": _str_or_none(value),
        "threshold": _str_or_none(threshold),
        "unit": unit,
        "as_of": as_of.isoformat(),
        "finding_id": None if finding_id is None else str(finding_id),
    }
    return subject_key, text, payload


def nav_restated_line(
    *,
    investment_id: UUID,
    investment_name: str,
    currency: str,
    as_of: date,
    previous_value: Decimal | None,
    nav_value: Decimal,
) -> Line:
    """Compose the ``nav_restated`` line for one investment and as-of.

    A new as-of reads "NAV reported for …"; a changed value at an existing
    as-of reads "NAV restated for …". Money goes through
    :func:`services.money_format.format_money_compact`.

    Args:
        investment_id: The investment.
        investment_name: Its name.
        currency: Its currency.
        as_of: The NAV's as-of date.
        previous_value: The value before the write, or ``None`` for a new
            as-of.
        nav_value: The value after the write.

    Returns:
        ``(subject, text, payload)``.
    """
    subject = f"{investment_id}:{as_of.isoformat()}"
    new = format_money_compact(float(nav_value), currency)
    if previous_value is None:
        change = CHANGE_NEW
        text = f"NAV reported for {investment_name}: {new} as of {as_of.isoformat()}."
    else:
        change = CHANGE_RESTATED
        old = format_money_compact(float(previous_value), currency)
        text = f"NAV restated for {investment_name} as of {as_of.isoformat()}: {old} → {new}."
    payload: dict[str, Any] = {
        "investment_id": str(investment_id),
        "investment_name": investment_name,
        "as_of": as_of.isoformat(),
        "nav_value": str(nav_value),
        "previous_value": _str_or_none(previous_value),
        "currency": currency,
        "change": change,
    }
    return subject, text, payload


def ticket_booked_line(ticket: TradeTicketDTO, *, investment_name: str | None) -> Line:
    """Compose the ``ticket_booked`` line of a booked ticket.

    The amount is the ticket's net amount through
    :func:`services.money_format.format_money_compact`, which prefixes the
    currency itself; a ticket without a net amount (a commitment) leaves the
    amount clause out.

    Args:
        ticket: The ticket as booking left it (``status == 'booked'``).
        investment_name: The traded investment's name, or ``None`` when it
            is unknown.

    Returns:
        ``(subject, text, payload)`` — the subject is the ticket id, so a
        ticket is booked onto a case once.
    """
    name = investment_name or "—"
    head = f"Ticket #{ticket.ticket_number} booked: {ticket.kind} {ticket.direction} {name}"
    trade_date = ticket.trade_date.isoformat()
    if ticket.net_amount is None:
        text = f"{head}, trade date {trade_date}."
    else:
        amount = format_money_compact(float(ticket.net_amount), ticket.currency)
        text = f"{head}, {amount}, trade date {trade_date}."
    payload: dict[str, Any] = {
        "ticket_id": str(ticket.id),
        "ticket_number": ticket.ticket_number,
        "kind": ticket.kind,
        "direction": ticket.direction,
        "status": ticket.status,
        "investment_id": None if ticket.investment_id is None else str(ticket.investment_id),
        "investment_name": investment_name,
        "trade_date": trade_date,
        "net_amount": _str_or_none(ticket.net_amount),
        "currency": ticket.currency,
        "booked_at": None if ticket.booked_at is None else ticket.booked_at.isoformat(),
    }
    return str(ticket.id), text, payload


def job_requested_line(*, proposal_id: UUID, request_text: str) -> Line:
    """Compose the ``job_requested`` line of a Shirley job.

    The quoted request is clipped to :data:`REQUEST_TEXT_CLIP` characters,
    ellipsis included; the payload keeps it whole.

    Args:
        proposal_id: The proposal the job will land in.
        request_text: What Shirley was asked.

    Returns:
        ``(subject, text, payload)`` — the subject is the proposal id.
    """
    asked = request_text.strip()
    if len(asked) > REQUEST_TEXT_CLIP:
        asked = asked[: REQUEST_TEXT_CLIP - 1].rstrip() + "…"
    text = f"Shirley was asked: “{asked}” — working on it."
    payload: dict[str, Any] = {"proposal_id": str(proposal_id), "request_text": request_text}
    return str(proposal_id), text, payload


def job_landed_line(*, proposal_id: UUID, item_count: int) -> Line:
    """Compose the ``job_landed`` line of a stored proposal package.

    Args:
        proposal_id: The proposal that landed.
        item_count: How many items it carries.

    Returns:
        ``(subject, text, payload)`` — the subject is the proposal id.
    """
    noun = "item" if item_count == 1 else "items"
    text = f"Shirley's proposal landed: {item_count} {noun} to review."
    payload: dict[str, Any] = {"proposal_id": str(proposal_id), "item_count": item_count}
    return str(proposal_id), text, payload


# ---------------------------------------------------------------------------
# The NAV diff (pure)
# ---------------------------------------------------------------------------


def nav_changes(before: NavSeries, after: NavSeries) -> list[tuple[date, Decimal | None, Decimal]]:
    """Return the reported-NAV events between two snapshots of one series.

    An as-of in ``after`` that is absent from ``before`` is a new report; an
    as-of whose value differs is a restatement. Values compare as
    ``Decimal`` (``Decimal("1.0") == Decimal("1.00")``, so a change of scale
    alone is no event). An as-of only in ``before`` was deleted — a deletion
    is not a report and yields nothing.

    Args:
        before: The series before the write.
        after: The series after the write.

    Returns:
        ``(as_of, previous_value, new_value)`` per event, by as-of
        ascending; ``previous_value`` is ``None`` for a new as-of.
    """
    changes: list[tuple[date, Decimal | None, Decimal]] = []
    for as_of in sorted(after):
        new_value = after[as_of]
        previous = before.get(as_of)
        if previous is None or previous != new_value:
            changes.append((as_of, previous, new_value))
    return changes


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


async def _write_line(
    cases: CaseRepository,
    case: CaseDTO,
    *,
    event: str,
    subject: str,
    text: str,
    payload: dict[str, Any],
    now: datetime,
) -> tuple[CaseEntryDTO | None, CaseDTO]:
    """Write one line and, if it woke a waiting case, lift the wait (D-CS-B-22).

    The one write-and-wake path of every writer. The line is written first;
    when a row was written and ``case`` is waiting, the wait is lifted with
    ``reason='event'`` at the same ``now``, in the caller's transaction, so
    the lift's snapshot shows the line. A deduplicated write (``None``)
    never lifts. A case closed meanwhile is skipped; a case that stopped
    waiting meanwhile (lifted by the wake-up tick or by hand since ``case``
    was read) keeps the line and is not lifted twice.

    Args:
        cases: The tenant-scoped case repository.
        case: The case as the writer last read it.
        event: The system-line event.
        subject: The dedup subject within ``event``.
        text: The template text.
        payload: The event's extras.
        now: The line's (and the lift's) time.

    Returns:
        ``(line, case_after)`` — the written line or ``None``, and the case
        as it stands after the write (the lifted case after a lift).
    """
    try:
        line = await cases.add_system_line(
            case.id, event=event, subject=subject, text=text, payload=payload, now=now
        )
    except CaseClosedError:
        logger.info("system-lines: case %s is closed — %s line skipped.", case.id, event)
        return None, case
    if line is None or case.state != "waiting":
        return line, case
    try:
        lifted = await cases.lift_wait(
            case.id,
            now=now,
            reason="event",
            event={"event": event, "subject": subject, "entry_id": str(line.id)},
        )
    except CaseStateInvalid:
        # Lifted since the writer read it (another transaction committed
        # first). The line stands; the case is open already.
        logger.info("system-lines: case %s was no longer waiting — no event lift.", case.id)
        current = await cases.get(case.id)
        return line, current if current is not None else case
    return line, lifted


async def subject_within_ceiling(
    session: AsyncSession,
    *,
    subject_key: str,
    family: str,
    status: str,
    as_of: date,
    now: datetime,
    coverage_pct: Decimal | None = None,
    max_pct: Decimal | None = None,
    display_name: str | None = None,
    value: Decimal | None = None,
    threshold: Decimal | None = None,
    unit: str | None = None,
) -> list[CaseEntryDTO]:
    """Write the falling edge of ``subject_key`` to the cases opened on it.

    Targets the open or waiting cases whose origin finding is on
    ``subject_key`` and resolved ``opened_case``. A limit family uses the
    ceiling template and needs ``coverage_pct``; any other family uses the
    threshold template. A waiting case is lifted by its line
    (``_write_line``, D-CS-M-4).

    Args:
        session: A tenant-scoped session.
        subject_key: The subject that fell back.
        family: The subject's family.
        status: The status now (limits: raw; signals: the human word).
        as_of: The date the reading is stated for.
        now: The lines' ``created_at``.
        coverage_pct: Limits — the coverage now.
        max_pct: Limits — the ceiling, or ``None``.
        display_name: Signals — the instrument or pair label.
        value: Signals — the observed move.
        threshold: Signals — the trigger threshold.
        unit: Signals — the unit of ``value`` / ``threshold``.

    Returns:
        The lines written (an already-written ``(event, subject)`` adds none).

    Raises:
        ValueError: If a limit family arrives without ``coverage_pct``.
    """
    is_limit = family in LIMIT_FAMILIES
    if is_limit and coverage_pct is None:
        raise ValueError(f"A limit subject's line needs its coverage ({subject_key}).")
    repo = CaseRepository(session)
    written: list[CaseEntryDTO] = []
    for case in await repo.list_open_by_finding_subject(subject_key):
        if is_limit:
            assert coverage_pct is not None
            line = limit_within_ceiling_line(
                subject_key=subject_key,
                family=family,
                status=status,
                coverage_pct=coverage_pct,
                max_pct=max_pct,
                as_of=as_of,
                finding_id=case.finding_id,
            )
        else:
            line = signal_within_threshold_line(
                subject_key=subject_key,
                family=family,
                display_name=display_name or subject_key,
                status=status,
                as_of=as_of,
                finding_id=case.finding_id,
                value=value,
                threshold=threshold,
                unit=unit,
            )
        subject, text, payload = line
        entry, _ = await _write_line(
            repo,
            case,
            event=EVENT_SUBJECT_WITHIN_CEILING,
            subject=subject,
            text=text,
            payload=payload,
            now=now,
        )
        if entry is not None:
            written.append(entry)
    return written


async def snapshot_actual_series(
    session: AsyncSession, investment_ids: Iterable[UUID]
) -> dict[UUID, NavSeries]:
    """Capture the reported ``actual`` NAV series of each investment.

    Computed rows (``basis = 'computed'``, holdings × price) are not
    reported NAVs and are left out, as are plan rows.

    Args:
        session: A tenant-scoped session.
        investment_ids: The investments to capture.

    Returns:
        ``{investment_id: {as_of: nav_value}}`` — an empty series for an
        investment with no reported NAV.
    """
    navs = InvestmentNavRepository(session)
    series: dict[UUID, NavSeries] = {}
    for investment_id in investment_ids:
        rows = await navs.list_by_investment_and_kind(investment_id, "actual")
        series[investment_id] = {
            row.as_of_date: row.nav_value for row in rows if row.basis != "computed"
        }
    return series


async def nav_restated(
    session: AsyncSession,
    *,
    investment_id: UUID,
    changes: list[tuple[date, Decimal | None, Decimal]],
    now: datetime,
) -> list[CaseEntryDTO]:
    """Write one ``nav_restated`` line per linking case and as-of.

    A waiting case is lifted by its first line; the lifted case is carried
    into the next as-of's write, so the later lines find it open
    (D-CS-B-22).

    Args:
        session: A tenant-scoped session.
        investment_id: The investment whose reported NAVs changed.
        changes: :func:`nav_changes` for it.
        now: The lines' ``created_at``.

    Returns:
        The lines written.
    """
    if not changes:
        return []
    case_ids = await CaseInvestmentRepository(session).list_case_ids_for_investment(investment_id)
    if not case_ids:
        return []
    investment = await InvestmentRepository(session).get_by_id(investment_id)
    name = investment.name if investment is not None else str(investment_id)
    currency = investment.currency if investment is not None else ""
    repo = CaseRepository(session)
    written: list[CaseEntryDTO] = []
    for case_id in case_ids:
        case = await repo.get(case_id)
        if case is None:
            continue
        for as_of, previous_value, nav_value in changes:
            subject, text, payload = nav_restated_line(
                investment_id=investment_id,
                investment_name=name,
                currency=currency,
                as_of=as_of,
                previous_value=previous_value,
                nav_value=nav_value,
            )
            entry, case = await _write_line(
                repo,
                case,
                event=EVENT_NAV_RESTATED,
                subject=subject,
                text=text,
                payload=payload,
                now=now,
            )
            if entry is not None:
                written.append(entry)
    return written


async def linked_investment_ids(session: AsyncSession) -> set[UUID]:
    """Return every investment linked to an open or waiting case (one query)."""
    return await CaseInvestmentRepository(session).list_linked_investment_ids()


async def nav_restated_after(
    session: AsyncSession, *, before: dict[UUID, NavSeries], now: datetime
) -> list[CaseEntryDTO]:
    """Re-capture the series of ``before``, diff them and write the lines.

    The second half of a NAV write seam: the seam captured ``before`` with
    :func:`snapshot_actual_series`, wrote, and calls this.

    Args:
        session: The tenant-scoped session the write ran in.
        before: The capture taken before the write.
        now: The lines' ``created_at``.

    Returns:
        Every line written, across the investments.
    """
    if not before:
        return []
    after = await snapshot_actual_series(session, before.keys())
    written: list[CaseEntryDTO] = []
    for investment_id, series in before.items():
        changes = nav_changes(series, after.get(investment_id, {}))
        written.extend(
            await nav_restated(session, investment_id=investment_id, changes=changes, now=now)
        )
    return written


async def ticket_booked(
    cases: CaseRepository,
    *,
    ticket: TradeTicketDTO,
    investment_name: str | None,
    now: datetime,
) -> CaseEntryDTO | None:
    """Write the ``ticket_booked`` line of a booked ticket to its case.

    A booking never fails because of its case: a ticket without a case, a
    case that is absent (another tenant's id, under RLS) or closed writes
    nothing. A waiting case is lifted by the line.

    Args:
        cases: The case repository on the booking's session.
        ticket: The ticket as booking left it.
        investment_name: The traded investment's name, or ``None``.
        now: The line's time — the booking's ``now``.

    Returns:
        The line written, or ``None``.
    """
    if ticket.case_id is None:
        return None
    case = await cases.get(ticket.case_id)
    if case is None or case.state == "closed":
        return None
    subject, text, payload = ticket_booked_line(ticket, investment_name=investment_name)
    line, _ = await _write_line(
        cases,
        case,
        event=EVENT_TICKET_BOOKED,
        subject=subject,
        text=text,
        payload=payload,
        now=now,
    )
    return line


async def job_requested(
    cases: CaseRepository,
    *,
    case: CaseDTO,
    proposal_id: UUID,
    request_text: str,
    now: datetime,
) -> CaseEntryDTO | None:
    """Write the ``job_requested`` line: Shirley was asked to work on the case.

    One line per proposal id. An asked job is an event on the case, so a
    waiting case is lifted by it. The caller is *Summarise into the file*
    (``post_proposals_summarise`` in ``web/routes/chat.py``, D-CS-M-12).

    Args:
        cases: The tenant-scoped case repository.
        case: The case the job is for.
        proposal_id: The proposal the job will land in.
        request_text: What Shirley was asked.
        now: The line's time.

    Returns:
        The line written, or ``None`` (already written, or the case closed).
    """
    subject, text, payload = job_requested_line(proposal_id=proposal_id, request_text=request_text)
    line, _ = await _write_line(
        cases,
        case,
        event=EVENT_JOB_REQUESTED,
        subject=subject,
        text=text,
        payload=payload,
        now=now,
    )
    return line


async def job_landed(
    cases: CaseRepository,
    *,
    case: CaseDTO,
    proposal_id: UUID,
    item_count: int,
    now: datetime,
) -> CaseEntryDTO | None:
    """Write the ``job_landed`` line: the proposal package is in ``case_proposals``.

    One line per proposal id; a waiting case is lifted by it. The caller is
    *Summarise into the file* (``post_proposals_summarise`` in
    ``web/routes/chat.py``, D-CS-M-12).

    Args:
        cases: The tenant-scoped case repository.
        case: The case the proposal belongs to.
        proposal_id: The proposal stored.
        item_count: How many items it carries.
        now: The line's time.

    Returns:
        The line written, or ``None`` (already written, or the case closed).
    """
    subject, text, payload = job_landed_line(proposal_id=proposal_id, item_count=item_count)
    line, _ = await _write_line(
        cases,
        case,
        event=EVENT_JOB_LANDED,
        subject=subject,
        text=text,
        payload=payload,
        now=now,
    )
    return line


__all__ = [
    "CHANGE_NEW",
    "CHANGE_RESTATED",
    "EVENT_JOB_LANDED",
    "EVENT_JOB_REQUESTED",
    "EVENT_NAV_RESTATED",
    "EVENT_PRESS_CLUSTER",
    "EVENT_SUBJECT_WITHIN_CEILING",
    "EVENT_TICKET_BOOKED",
    "LIMIT_FAMILIES",
    "REQUEST_TEXT_CLIP",
    "NavSeries",
    "job_landed",
    "job_landed_line",
    "job_requested",
    "job_requested_line",
    "limit_within_ceiling_line",
    "linked_investment_ids",
    "nav_changes",
    "nav_restated",
    "nav_restated_after",
    "nav_restated_line",
    "signal_within_threshold_line",
    "snapshot_actual_series",
    "subject_within_ceiling",
    "ticket_booked",
    "ticket_booked_line",
]
