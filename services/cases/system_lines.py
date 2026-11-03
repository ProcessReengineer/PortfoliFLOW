# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""System lines — the program's mechanical record on a case (ADR-0138 §7).

A system line is a ``case_entries`` row of kind ``system_line`` written by
the program, never by a PM and never by a model: ``actor = 'system'``, no
acting user, a machine-readable payload and one fixed template text.

**The event vocabulary.** ``subject_within_ceiling`` · ``nav_restated`` ·
``press_cluster`` · ``ticket_booked`` · ``job_requested`` · ``job_landed``.
The first two are written here (P-CS-2b); the other four are reserved as
constants and written by P-CS-2c. ``waiting_lifted`` is a milestone, not a
line.

**The one writer.** Every line goes through
:meth:`core.repositories.case_repository.CaseRepository.add_system_line`,
called from this module and from nowhere else. A closed case receives
nothing; a closed case in a target set is skipped, never raised on.

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
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import CaseClosedError
from core.repositories.case_repository import CaseEntryDTO, CaseRepository
from core.repositories.case_working_file_repository import CaseInvestmentRepository
from core.repositories.investment_nav_repository import InvestmentNavRepository
from core.repositories.investment_repository import InvestmentRepository
from services.money_format import format_money_compact

logger = logging.getLogger(__name__)

EVENT_SUBJECT_WITHIN_CEILING = "subject_within_ceiling"
EVENT_NAV_RESTATED = "nav_restated"
# Reserved here; their writers land in P-CS-2c.
EVENT_PRESS_CLUSTER = "press_cluster"
EVENT_TICKET_BOOKED = "ticket_booked"
EVENT_JOB_REQUESTED = "job_requested"
EVENT_JOB_LANDED = "job_landed"

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


async def _write(
    repo: CaseRepository, case_id: UUID, *, event: str, line: Line, now: datetime
) -> CaseEntryDTO | None:
    """Write one composed line; a case closed meanwhile is skipped."""
    subject, text, payload = line
    try:
        return await repo.add_system_line(
            case_id, event=event, subject=subject, text=text, payload=payload, now=now
        )
    except CaseClosedError:
        logger.info("system-lines: case %s is closed — %s line skipped.", case_id, event)
        return None


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
    threshold template. The state of a waiting case is left alone (the
    event-driven lift is P-CS-2c's).

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
        entry = await _write(repo, case.id, event=EVENT_SUBJECT_WITHIN_CEILING, line=line, now=now)
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
        for as_of, previous_value, nav_value in changes:
            line = nav_restated_line(
                investment_id=investment_id,
                investment_name=name,
                currency=currency,
                as_of=as_of,
                previous_value=previous_value,
                nav_value=nav_value,
            )
            entry = await _write(repo, case_id, event=EVENT_NAV_RESTATED, line=line, now=now)
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
    "NavSeries",
    "limit_within_ceiling_line",
    "linked_investment_ids",
    "nav_changes",
    "nav_restated",
    "nav_restated_after",
    "nav_restated_line",
    "signal_within_threshold_line",
    "snapshot_actual_series",
    "subject_within_ceiling",
]
