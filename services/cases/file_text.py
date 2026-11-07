# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The working file as text — Shirley's brief and ``get_case`` (ADR-0138 §10).

One composition serves both readers of a case's file as text: the case brief
appended to Shirley's system prompt while she is docked beside a case
(``web/routes/chat.py``) and the ``get_case`` read tool
(``services/tools/case_tools.py``). One function, one truth (D-CS-B-106): the
brief and the tool cannot drift, and both state what the page renders — the
sections in the ADR's order (§3), each headed and **omitted when empty**;
The question, Where we stand and the Record are always present, as on the
page.

What it reads
-------------
The working file through its repositories — threads, kept rows, figures,
conversations, open points and decisions; the record through
:meth:`~core.repositories.case_repository.CaseRepository.list_entries` and,
on a closed case,
:meth:`~core.repositories.case_repository.CaseRepository.latest_frozen`; the
origin finding's subject and band through
:class:`~core.repositories.irene_finding_repository.IreneFindingRepository`;
the investments a ``book`` figure names through
:class:`~core.repositories.investment_repository.InvestmentRepository`.
Where we stand on an open or waiting case is
:func:`~services.cases.stand.compose` on one
:class:`~services.cases.stand.StandContext` — the investment links, the
investments, the FX rates and the tickets (``TradeTicketRepository``) arrive
through it, and the ``book`` figures' values through
:func:`~services.cases.stand.book_values_for`. On a closed case it is the
block frozen at close, read by key name (JSONB keeps no order); nothing on a
closed case reads the book (ADR-0138 §6).

The stand never fails the text (5a F-4, D-CS-B-120): a chat turn with a case
brief must not break on it. Its load and composition run in a SAVEPOINT, so a
database error rolls back to it and the session stays usable for the reads
that follow; on any exception the section reads *Where we stand: not
available — {the exception's first line}*, a warning is logged, and the rest
of the file is composed as usual.

What it never does
------------------
It writes nothing, imports nothing of the pure calculation layer and reaches
the Watch Desk only through :class:`~services.cases.stand.StandContext` — the
package guards (``tests/regression/test_cases_stand_reads_only.py``,
``tests/regression/test_watch_desk_single_resolution.py``) hold the package
to that. It carries no kept payload beyond captions and an excerpt's passage
— never a chart spec, never a document's bytes, a generated document as its
title and first line — no user id and no tenant id (users appear by name,
resolved by the caller through :func:`file_user_ids`), and no finding
payload beyond the subject and the band (D-CS-B-112).

Bounds (D-CS-B-107)
-------------------
Per section the first or the most recent *N* (the ``_MAX_*`` constants); a
cut section says *… n more* where the omitted rows belong. Every line is
clipped to :data:`LINE_CHARS` with *…*, and the whole text to
:data:`MAX_CHARS` with a hard tail cut marked *… [file truncated]*. Dates and
times are UTC, in the file's idiom (``%d %b %Y``, ``%H:%M``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, TypeVar
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.repositories.case_repository import CaseDTO, CaseEntryDTO, CaseRepository
from core.repositories.case_working_file_repository import (
    CaseConversationDTO,
    CaseConversationRepository,
    CaseDecisionDTO,
    CaseDecisionRepository,
    CaseFigureRepository,
    CaseKeptDTO,
    CaseKeptRepository,
    CasePointDTO,
    CasePointRepository,
    CaseThreadDTO,
    CaseThreadRepository,
)
from core.repositories.investment_repository import InvestmentRepository
from core.repositories.irene_finding_repository import IreneFindingDTO, IreneFindingRepository
from services.cases.stand import StandContext, WhereWeStand, book_values_for, compose
from services.money_format import format_money_compact
from services.watch_desk.family_status import FIGURE_UNIT_DAYS, FIGURE_UNIT_RATIO

logger = logging.getLogger(__name__)

#: Every line of the text is clipped to this many characters (D-CS-B-107).
LINE_CHARS: int = 160

#: The whole text is cut to this many characters (D-CS-B-107).
MAX_CHARS: int = 8_000

# The per-section bounds (D-CS-B-107): the first or the most recent N.
_MAX_THREADS = 12
_MAX_KEPT_PER_THREAD = 4
_MAX_FIGURES = 20
_MAX_CONVERSATIONS = 8
_MAX_POINTS = 20
_MAX_RECORD = 12

_INDENT = "  "
_ELLIPSIS = "…"
_TRUNCATED = "… [file truncated]"
_UNKNOWN_USER = "unknown user"

# The headings (D-5a-4), as the page titles its blocks.
_QUESTION_HEADINGS: dict[str, str] = {"question": "The question", "task": "The task"}
_POINTS_HEADINGS: dict[str, str] = {"question": "Open points", "task": "Questions to ask"}
_STAND_HEADING = "Where we stand"
_AT_CLOSE_HEADING = "At close"
_THREADS_HEADING = "Threads"
_FIGURES_HEADING = "Figures"
_CONVERSATIONS_HEADING = "Conversations"
_DECISION_HEADING = "Decision"
_RECORD_HEADING = "Record"

# The page's own wording for a closed case without a frozen block.
_NO_FROZEN_TEXT = "No frozen state — this case was closed before the working file existed."
_NOT_FROZEN_TEXT = "Where we stand was not frozen when this case closed."

# The one line Where we stand becomes when it cannot be composed (D-CS-B-120).
_STAND_UNAVAILABLE_TEXT = "Where we stand: not available — {reason}"

# The page's labels: a thread's status, a conversation's channel, a book metric.
_THREAD_STATUS_LABELS: dict[str, str] = {
    "open": "Open",
    "confirmed": "Confirmed",
    "parked": "Parked",
    "refuted": "Refuted",
}
_CHANNEL_LABELS: dict[str, str] = {"call": "Call", "meeting": "Meeting", "other": "Other"}
_BOOK_METRIC_LABELS: dict[str, str] = {
    "nav": "NAV",
    "nav_as_of": "NAV date",
    "commitment": "Commitment",
    "tvpi": "TVPI",
    "dpi": "DPI",
    "rvpi": "RVPI",
    "irr": "Net IRR",
    "currency": "Currency",
}

# The record's kind labels and the lift texts, as the Record block reads them.
_ENTRY_KIND_LABELS: dict[str, str] = {
    "opened": "Case opened",
    "note": "Note",
    "decision_record": "Decision record",
    "closed": "Case closed",
    "pin": "Pinned",
    "handed_over": "Handed over",
    "waiting_set": "Waiting set",
    "waiting_lifted": "Waiting lifted",
    "system_line": "System line",
}
_PIN_KIND_LABELS: dict[str, str] = {
    "document": "Document pinned",
    "scenario_snapshot": "Scenario snapshot pinned",
    "consultation": "Consultation pinned",
    "chart_snapshot": "Chart snapshot pinned",
}
_LIFT_TEXTS: dict[str, str] = {
    "manual": "Waiting lifted — by hand",
    "date": "Waiting lifted — on the date",
}

_T = TypeVar("_T")


# ---------------------------------------------------------------------------
# Lines, sections and bounds
# ---------------------------------------------------------------------------


def _one_line(value: Any) -> str:
    """Return ``value`` as text on one line, its whitespace collapsed."""
    return " ".join(str(value or "").split())


def _first_line(exc: BaseException) -> str:
    """The exception's first line, collapsed — its type's name when it says nothing."""
    lines = str(exc).strip().splitlines()
    return _one_line(lines[0]) if lines else type(exc).__name__


def _clip(text: str) -> str:
    """Clip one line to :data:`LINE_CHARS`, marking the cut with *…*."""
    if len(text) <= LINE_CHARS:
        return text
    return text[: LINE_CHARS - 1].rstrip() + _ELLIPSIS


def _prose_lines(value: Any) -> list[str]:
    """Return a prose field's non-empty lines, each collapsed to one line."""
    return [_one_line(line) for line in str(value or "").splitlines() if line.strip()]


def _section(heading: str, lines: Sequence[str]) -> list[str]:
    """Return the heading and its lines, indented and clipped; ``[]`` when empty.

    A line may carry indentation of its own (a thread's prose under its
    title); the section indents every line once more.
    """
    if not lines:
        return []
    return [heading, *(_clip(f"{_INDENT}{line}") for line in lines)]


def _bounded(items: Sequence[_T], limit: int) -> tuple[Sequence[_T], int]:
    """Return the first ``limit`` items and how many were left out."""
    return items[:limit], max(0, len(items) - limit)


def _more(omitted: int) -> list[str]:
    """The *… n more* line of a cut section, or nothing."""
    return [f"{_ELLIPSIS} {omitted} more"] if omitted else []


def _cap(text: str) -> str:
    """Cut the whole text to :data:`MAX_CHARS` with the truncation mark."""
    if len(text) <= MAX_CHARS:
        return text
    keep = MAX_CHARS - len(_TRUNCATED) - 1
    return text[:keep].rstrip() + "\n" + _TRUNCATED


def _name(user_id: UUID | None, names: Mapping[UUID, str]) -> str:
    """A user by name — never the id; *unknown user* when the caller resolved none."""
    if user_id is None:
        return _UNKNOWN_USER
    return names.get(user_id) or _UNKNOWN_USER


# ---------------------------------------------------------------------------
# Dates, numbers and readings (a dataclass or its snapshot, keys by name)
# ---------------------------------------------------------------------------


def _day(value: date) -> str:
    """``dd Mon YYYY``."""
    return value.strftime("%d %b %Y")


def _utc(moment: datetime) -> datetime:
    """The instant in UTC (a naive instant is taken as UTC)."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _field(source: Any, name: str) -> Any:
    """Read ``name`` off a stand dataclass or its snapshot dict; ``None`` when absent."""
    if source is None:
        return None
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def _as_decimal(value: Any) -> Decimal | None:
    """A ``Decimal`` from a reading (``Decimal``) or a snapshot (``str``)."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _as_date(value: Any) -> date | None:
    """A date from a reading (``date``) or a snapshot (ISO ``str``)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _utc(value).date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _as_datetime(value: Any) -> datetime | None:
    """An instant from a reading or a snapshot's ISO string."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _utc(value)
    try:
        return _utc(datetime.fromisoformat(str(value)))
    except ValueError:
        return None


def _figure_value(value: Decimal, unit: Any) -> str:
    """A subject figure in its unit — the Watch Desk monitor's idiom."""
    if unit == FIGURE_UNIT_DAYS:
        days = int(value)
        return f"{days} day{'' if days == 1 else 's'}"
    if unit == FIGURE_UNIT_RATIO:
        return f"{float(value):.2f}×"
    return f"{float(value):.1f} %"


def _reading_text(reading: Any) -> str:
    """One subject reading: *figure of ceiling (status) · as of date*."""
    value = _as_decimal(_field(reading, "value"))
    status_label = _one_line(_field(reading, "status_label"))
    if value is None:
        return status_label or _one_line(_field(reading, "note")) or "not available"
    unit = _field(reading, "unit")
    text = _figure_value(value, unit)
    ceiling = _as_decimal(_field(reading, "ceiling"))
    if ceiling is not None:
        text += f" of {_figure_value(ceiling, unit)}"
    if status_label:
        text += f" ({status_label})"
    as_of = _as_date(_field(reading, "as_of"))
    if as_of is not None:
        text += f" · as of {_day(as_of)}"
    return text


def _money(value: Decimal | None, currency: str) -> str | None:
    return format_money_compact(float(value), currency) if value is not None else None


def _multiple(value: Decimal | None) -> str | None:
    return f"{float(value):.2f}x" if value is not None else None


def _rate(value: Decimal | None) -> str:
    return f"{value:.4f}" if value is not None else "—"


# ---------------------------------------------------------------------------
# Where we stand — live (the dataclass) or at close (the snapshot dict)
# ---------------------------------------------------------------------------


def _subject_lines(source: Any, *, closed: bool, subject_key: str | None) -> list[str]:
    """The finding's subject: now (or at close) and at case opening."""
    subject = _field(source, "subject")
    if subject is None:
        return []
    now_reading = _field(subject, "now")
    at_opening = _field(subject, "at_opening")
    key = (
        subject_key
        or _field(now_reading, "subject_key")
        or _field(at_opening, "subject_key")
        or "the finding's subject"
    )
    reason = _one_line(_field(subject, "unavailable_reason"))
    reading = _reading_text(now_reading) if now_reading is not None else reason or "not available"
    lines = [f"Subject {key} {'at close' if closed else 'now'}: {reading}"]
    if at_opening is not None:
        lines.append(f"Subject at case opening: {_reading_text(at_opening)}")
    else:
        lines += [
            f"At case opening: {_one_line(line)}"
            for line in _field(subject, "at_opening_lines") or []
            if _one_line(line)
        ]
    return lines


def _investment_line(reading: Any) -> str:
    """One linked investment with the figures the page's table carries."""
    currency = _one_line(_field(reading, "currency"))
    parts = [_one_line(_field(reading, "name")) or "an investment", currency]
    commitment = _money(_as_decimal(_field(reading, "commitment_amount")), currency)
    if commitment is not None:
        parts.append(f"commitment {commitment}")
    nav = _money(_as_decimal(_field(reading, "nav_value")), currency)
    nav_as_of = _as_date(_field(reading, "nav_as_of"))
    if nav is None:
        parts.append("NAV not available")
    else:
        parts.append(f"NAV {nav}" + (f" as of {_day(nav_as_of)}" if nav_as_of else ""))
    for label in ("tvpi", "dpi"):
        multiple = _multiple(_as_decimal(_field(reading, label)))
        if multiple is not None:
            parts.append(f"{label.upper()} {multiple}")
    irr = _as_decimal(_field(reading, "irr"))
    if irr is not None:
        parts.append(f"IRR {float(irr) * 100.0:.1f}%")
    return "Investment: " + " · ".join(part for part in parts if part)


def _stand_body(source: Any, *, closed: bool, subject_key: str | None) -> list[str]:
    """Where we stand as lines, read by field name off either source."""
    lines: list[str] = []
    headline = _one_line(_field(source, "headline"))
    if headline:
        lines.append(f"Headline: {headline}")
    lines += _subject_lines(source, closed=closed, subject_key=subject_key)

    investments = list(_field(source, "investments") or [])
    lines += [_investment_line(reading) for reading in investments]
    if not investments:
        lines.append("Investments: none linked.")

    counterparty = _field(source, "counterparty")
    if counterparty is not None:
        funds = [_one_line(_field(fund, "name")) for fund in _field(counterparty, "funds") or []]
        count = len(funds)
        listing = f": {', '.join(funds)}" if funds else ""
        lines.append(
            f"Counterparty {_one_line(_field(counterparty, 'name'))}: "
            f"{count} fund{'' if count == 1 else 's'} in the book{listing}"
        )

    for fx in _field(source, "fx") or []:
        lines.append(
            f"FX {_one_line(_field(fx, 'currency'))}/{_one_line(_field(fx, 'functional_currency'))}: "
            f"at opening {_rate(_as_decimal(_field(fx, 'rate_at_opening')))} → now "
            f"{_rate(_as_decimal(_field(fx, 'rate_now')))}"
        )

    tickets = list(_field(source, "tickets") or [])
    for ticket in tickets:
        trade_date = _as_date(_field(ticket, "trade_date"))
        parts = [
            f"Ticket #{_field(ticket, 'ticket_number')}",
            _one_line(_field(ticket, "kind")),
            _one_line(_field(ticket, "direction")),
            _one_line(_field(ticket, "status")),
            _one_line(_field(ticket, "investment_name")) or "—",
            _day(trade_date) if trade_date is not None else "—",
        ]
        lines.append(" · ".join(part for part in parts if part))
    if not tickets:
        lines.append("Tickets: none.")

    system_lines = list(_field(source, "system_lines") or [])
    for line in system_lines:
        created = _as_datetime(_field(line, "created_at"))
        text = _one_line(_field(line, "text")) or _one_line(_field(line, "event"))
        lines.append(f"Since opening: {text}" + (f" · {_day(created.date())}" if created else ""))
    if not system_lines:
        lines.append("Since opening: nothing happened on this case's subject.")
    return lines


def _stand_lines(stand: WhereWeStand, *, subject_key: str | None) -> list[str]:
    """Where we stand on an open or waiting case, as composed now."""
    return _stand_body(stand, closed=False, subject_key=subject_key)


def _frozen_stand_lines(block: Mapping[str, Any], *, subject_key: str | None) -> list[str]:
    """Where we stand as frozen at close — the snapshot dict, keys by name."""
    return _stand_body(block, closed=True, subject_key=subject_key)


# ---------------------------------------------------------------------------
# The sections
# ---------------------------------------------------------------------------


def _framing(case: CaseDTO) -> str:
    """The case's framing, ``question`` when unset."""
    return case.framing if case.framing in _QUESTION_HEADINGS else "question"


def _badge(case: CaseDTO) -> str:
    return f"CASE-{case.case_number:04d}"


def _at_file_level(row: CaseKeptDTO, live_thread_ids: set[UUID]) -> bool:
    """A kept row no live thread holds — kept in the file, or in a thread removed since."""
    return row.thread_id is None or row.thread_id not in live_thread_ids


def _render_question(
    case: CaseDTO,
    finding: IreneFindingDTO | None,
    documents: Sequence[CaseKeptDTO],
    names: Mapping[UUID, str],
) -> list[str]:
    """The question (or The task): title, state, owner, origin, the prose, the documents."""
    lines = [f"{_badge(case)} — {_one_line(case.title)}"]
    if case.state == "waiting":
        state = (
            f"waiting until {_day(case.wait_until)}" if case.wait_until is not None else "waiting"
        )
    elif case.state == "closed":
        state = "closed"
        if case.closed_at is not None:
            state += f" {_day(_utc(case.closed_at).date())}"
        if case.closed_by is not None:
            state += f" by {_name(case.closed_by, names)}"
    else:
        state = case.state
    lines.append(f"State: {state}")
    origin = "from a finding" if case.finding_id is not None else "manually"
    lines.append(
        f"Owner: {_name(case.owner_user_id, names)} · opened {origin} "
        f"{_day(_utc(case.opened_at).date())} by {_name(case.opened_by, names)}"
    )
    if case.finding_id is not None:
        lines.append(
            f"Origin: finding on {finding.subject_key} (band {finding.band})"
            if finding is not None
            else "Origin: a finding"
        )
    facts: list[str] = []
    if case.counterparty and case.counterparty.strip():
        facts.append(f"Counterparty: {_one_line(case.counterparty)}")
    if case.due_on is not None:
        facts.append(f"Due: {_day(case.due_on)}")
    if facts:
        lines.append(" · ".join(facts))
    lines += _prose_lines(case.description)
    if case.state == "closed" and case.closing_note:
        lines.append(f"Closing note: {_one_line(case.closing_note)}")
    lines += [f"Document: {_one_line(row.caption) or 'a document'}" for row in documents]
    return _section(_QUESTION_HEADINGS[_framing(case)], lines)


def _kept_line(row: CaseKeptDTO) -> str:
    """One kept row as its caption line — never its payload beyond the caption.

    An excerpt adds its quoted passage; a generated document reads as its
    title and the first line of its body. A chart's spec, a document's bytes
    and every other payload field stay out (D-CS-B-112).
    """
    payload = row.payload if isinstance(row.payload, Mapping) else {}
    label = row.artifact.replace("_", " ")
    caption = _one_line(row.caption)
    if row.artifact == "generated_document":
        title = _one_line(payload.get("title")) or caption
        body = _prose_lines(payload.get("body"))
        parts = [label, title, body[0] if body else ""]
    elif row.artifact == "document_excerpt":
        passage = _one_line(payload.get("passage"))
        parts = [label, caption, f"“{passage}”" if passage else ""]
    else:
        parts = [label, caption]
    return "kept: " + " · ".join(part for part in parts if part)


def _kept_lines(rows: Sequence[CaseKeptDTO], indent: str) -> list[str]:
    shown, omitted = _bounded(rows, _MAX_KEPT_PER_THREAD)
    return [f"{indent}{_kept_line(row)}" for row in shown] + [
        f"{indent}{line}" for line in _more(omitted)
    ]


def _render_threads(
    threads: Sequence[CaseThreadDTO], kept_rows: Sequence[CaseKeptDTO]
) -> list[str]:
    """Threads with status, conclusion, prose and kept captions; then *Kept in the file*."""
    live_thread_ids = {thread.id for thread in threads}
    by_thread: dict[UUID, list[CaseKeptDTO]] = {}
    file_kept: list[CaseKeptDTO] = []
    for row in kept_rows:
        if row.thread_id is not None and row.thread_id in live_thread_ids:
            by_thread.setdefault(row.thread_id, []).append(row)
        elif row.artifact != "document" and _at_file_level(row, live_thread_ids):
            file_kept.append(row)

    lines: list[str] = []
    shown, omitted = _bounded(threads, _MAX_THREADS)
    for thread in shown:
        head = [
            _one_line(thread.title),
            _THREAD_STATUS_LABELS.get(thread.status, thread.status.capitalize()),
        ]
        if thread.conclusion and thread.conclusion.strip():
            head.append(f"conclusion: {_one_line(thread.conclusion)}")
        lines.append(" · ".join(head))
        lines += [f"{_INDENT}{line}" for line in _prose_lines(thread.prose)]
        lines += _kept_lines(by_thread.get(thread.id, []), _INDENT)
    lines += _more(omitted)
    if file_kept:
        lines.append("Kept in the file")
        lines += _kept_lines(file_kept, _INDENT)
    return _section(_THREADS_HEADING, lines)


def _book_ref(source_ref: Any) -> tuple[UUID | None, str]:
    """A ``book`` figure's read key ``(investment_id, metric)``."""
    ref = source_ref if isinstance(source_ref, Mapping) else {}
    raw = ref.get("investment_id")
    try:
        investment_id = UUID(str(raw)) if raw is not None else None
    except ValueError:
        investment_id = None
    return investment_id, str(ref.get("metric") or "")


async def _investment_names(session: AsyncSession, rows: Iterable[Any]) -> dict[UUID, str]:
    """The names of the investments the ``book`` figures read, one read per id."""
    names: dict[UUID, str] = {}
    repo = InvestmentRepository(session)
    for row in rows:
        if _field(row, "source") != "book":
            continue
        investment_id, _ = _book_ref(_field(row, "source_ref"))
        if investment_id is None or investment_id in names:
            continue
        investment = await repo.get_by_id(investment_id)
        names[investment_id] = investment.name if investment is not None else "unknown investment"
    return names


def _figure_line(row: Any, *, value_text: Any, as_of: Any, investments: Mapping[UUID, str]) -> str:
    """One figure: label · value · source (a book row with its investment and metric) · as of."""
    source = _one_line(_field(row, "source"))
    is_book = source == "book"
    value = _one_line(value_text)
    parts = [_one_line(_field(row, "label")), value or ("not available" if is_book else "pending")]
    if is_book:
        investment_id, metric = _book_ref(_field(row, "source_ref"))
        investment = investments.get(investment_id) if investment_id else None
        metric_label = _BOOK_METRIC_LABELS.get(metric, metric or "—")
        parts.append(f"book ({investment or 'unknown investment'} · {metric_label})")
        book_as_of = _as_date(as_of)
        if book_as_of is not None:
            parts.append(f"as of {_day(book_as_of)}")
    else:
        parts.append(source)
        if as_of:
            parts.append(f"as of {_one_line(as_of)}")
    note = _one_line(_field(row, "note"))
    if note:
        parts.append(f"note: {note}")
    return " · ".join(part for part in parts if part)


async def _render_figures(
    session: AsyncSession, rows: Sequence[Any], values: Mapping[str, Mapping[str, Any]] | None
) -> list[str]:
    """Figures — ``values`` resolves the live ``book`` rows; ``None`` reads rows as frozen."""
    shown, omitted = _bounded(rows, _MAX_FIGURES)
    investments = await _investment_names(session, shown)
    lines: list[str] = []
    for row in shown:
        if values is not None and _field(row, "source") == "book":
            resolved = values.get(str(_field(row, "id"))) or {}
            value_text, as_of = resolved.get("value_text"), resolved.get("as_of")
        else:
            value_text, as_of = _field(row, "value_text"), _field(row, "as_of")
        lines.append(_figure_line(row, value_text=value_text, as_of=as_of, investments=investments))
    return _section(_FIGURES_HEADING, lines + _more(omitted))


def _render_conversations(conversations: Sequence[CaseConversationDTO]) -> list[str]:
    """Conversations, newest first: date · with whom · channel, then said and promised."""
    shown, omitted = _bounded(conversations, _MAX_CONVERSATIONS)
    lines: list[str] = []
    for conversation in shown:
        head = [
            _day(conversation.held_on),
            _one_line(conversation.with_whom) or "—",
            _CHANNEL_LABELS.get(conversation.channel, conversation.channel.capitalize()),
        ]
        if conversation.duration_minutes is not None:
            head.append(f"{conversation.duration_minutes} min")
        lines.append(" · ".join(head))
        said = _one_line(conversation.what_was_said)
        if said:
            lines.append(f"{_INDENT}said: {said}")
        promised = _one_line(conversation.what_was_promised)
        if promised:
            lines.append(f"{_INDENT}promised: {promised}")
    return _section(_CONVERSATIONS_HEADING, lines + _more(omitted))


def _render_points(
    case: CaseDTO, points: Sequence[CasePointDTO], names: Mapping[UUID, str]
) -> list[str]:
    """Open points (Questions to ask on a task), the open ones first: text · owner · due · done."""
    shown, omitted = _bounded(points, _MAX_POINTS)
    lines: list[str] = []
    for point in shown:
        parts = [_one_line(point.text)]
        if point.owner_user_id is not None:
            parts.append(f"owner {_name(point.owner_user_id, names)}")
        if point.due_on is not None:
            parts.append(f"due {_day(point.due_on)}")
        parts.append(
            f"done {_day(_utc(point.done_at).date())}" if point.done_at is not None else "open"
        )
        lines.append(" · ".join(parts))
    return _section(_POINTS_HEADINGS[_framing(case)], lines + _more(omitted))


def _decision_lines(decision: CaseDecisionDTO, head: str) -> list[str]:
    """One decision: its head, the options with the chosen one marked, the rationale."""
    if decision.decided_by:
        head += f" · decided by {_one_line(decision.decided_by)}"
    if decision.decided_at is not None:
        head += f" on {_day(decision.decided_at)}"
    lines = [head]
    for option in decision.options:
        if not isinstance(option, Mapping):
            continue
        key = str(option.get("key") or "")
        chosen = bool(key) and key == decision.chosen_key
        lines.append(
            f"{_INDENT}{'chosen' if chosen else 'option'}: {_one_line(option.get('text'))}"
        )
    if decision.rationale and decision.rationale.strip():
        lines.append(f"{_INDENT}rationale: {_one_line(decision.rationale)}")
    return lines


def _render_decision(
    recorded: Sequence[CaseDecisionDTO], draft: CaseDecisionDTO | None
) -> list[str]:
    """The recorded decisions, newest first, then the draft."""
    lines: list[str] = []
    for decision in reversed(recorded):
        stamp = (
            f" {_utc(decision.recorded_at):%d %b %Y %H:%M}"
            if decision.recorded_at is not None
            else ""
        )
        lines += _decision_lines(decision, f"Recorded{stamp}")
    if draft is not None:
        lines += _decision_lines(draft, "Draft")
    return _section(_DECISION_HEADING, lines)


def _parse_uuid(raw: Any) -> UUID | None:
    if raw is None or raw == "":
        return None
    try:
        return raw if isinstance(raw, UUID) else UUID(str(raw))
    except ValueError:
        return None


def _opened_text(case: CaseDTO, finding: IreneFindingDTO | None) -> str:
    """The ``opened`` entry's phrasing, as the Record block reads it."""
    if case.finding_id is not None:
        subject = finding.subject_key if finding is not None else None
        return f"Opened from finding {subject}." if subject else "Opened from a finding."
    return "Opened manually."


def _entry_line(
    entry: CaseEntryDTO,
    *,
    opened_text: str,
    waking: Mapping[str, str],
    names: Mapping[UUID, str],
) -> str:
    """One record row: *dd Mon HH:MM · kind · text* — presentation, never a payload."""
    payload = entry.payload if isinstance(entry.payload, Mapping) else {}
    kind = entry.kind
    label = _ENTRY_KIND_LABELS.get(kind, kind.replace("_", " ").capitalize())
    text = ""
    if kind == "opened":
        text = opened_text
    elif kind in ("note", "system_line"):
        text = _one_line(payload.get("text"))
    elif kind == "decision_record" and payload.get("decision_id"):
        text = (
            f"Decision recorded: {_one_line(payload.get('chosen_text'))} — "
            f"{_one_line(payload.get('rationale'))}"
        )
    elif kind == "decision_record":
        text = f"{_one_line(payload.get('decision'))} — {_one_line(payload.get('rationale'))}"
    elif kind == "handed_over":
        text = f"Handed over to {_name(_parse_uuid(payload.get('to_user_id')), names)}"
    elif kind == "waiting_set":
        until = _as_date(payload.get("wait_until"))
        text = f"Waiting until {_day(until)}" if until is not None else "Waiting"
    elif kind == "waiting_lifted":
        reason = str(payload.get("reason") or "")
        if reason == "event":
            event = payload.get("event")
            fallback = event.get("event") if isinstance(event, Mapping) else None
            woken = (
                waking.get(str(event.get("entry_id"))) if isinstance(event, Mapping) else None
            ) or str(fallback or "an event").replace("_", " ")
            text = f"Waiting lifted — woken by: {woken}"
        else:
            text = _LIFT_TEXTS.get(reason, "Waiting lifted")
    elif kind == "closed":
        text = _one_line(payload.get("closing_note"))
    elif kind == "pin":
        label = _PIN_KIND_LABELS.get(str(payload.get("artifact")), label)
        text = _one_line(payload.get("comment"))
    if kind in ("handed_over", "waiting_set") and _one_line(payload.get("note")):
        text += f" — {_one_line(payload.get('note'))}"
    stamp = f"{_utc(entry.created_at):%d %b %H:%M}"
    return " · ".join(part for part in (stamp, label, text) if part)


def _render_record(
    case: CaseDTO,
    entries: Sequence[CaseEntryDTO],
    finding: IreneFindingDTO | None,
    names: Mapping[UUID, str],
) -> list[str]:
    """The record's newest rows, oldest first; the cut stands where the older rows were."""
    waking = {
        str(entry.id): _one_line(entry.payload.get("text"))
        for entry in entries
        if entry.kind == "system_line" and isinstance(entry.payload, Mapping)
    }
    omitted = max(0, len(entries) - _MAX_RECORD)
    opened_text = _opened_text(case, finding)
    lines = _more(omitted) + [
        _entry_line(entry, opened_text=opened_text, waking=waking, names=names)
        for entry in entries[omitted:]
    ]
    return _section(_RECORD_HEADING, lines)


# ---------------------------------------------------------------------------
# The public surface
# ---------------------------------------------------------------------------


async def file_user_ids(session: AsyncSession, case: CaseDTO) -> set[UUID]:
    """Return the ids of the users :func:`render_case_file_text` names.

    The caller resolves them to names in one batch and passes the mapping
    back (``user_names``): the owner, who opened and who closed the case,
    the open points' owners and the users the record hands the case over to.

    Args:
        session: A tenant-scoped session.
        case: The case.

    Returns:
        The user ids, possibly the owner alone.
    """
    ids: set[UUID] = {case.owner_user_id, case.opened_by}
    if case.closed_by is not None:
        ids.add(case.closed_by)
    for point in await CasePointRepository(session).list_for_case(case.id):
        if point.owner_user_id is not None:
            ids.add(point.owner_user_id)
    for entry in await CaseRepository(session).list_entries(case.id):
        if entry.kind == "handed_over" and isinstance(entry.payload, Mapping):
            to_user = _parse_uuid(entry.payload.get("to_user_id"))
            if to_user is not None:
                ids.add(to_user)
    return ids


async def render_case_file_text(
    session: AsyncSession,
    case: CaseDTO,
    *,
    now: datetime,
    user_names: Mapping[UUID, str] | None = None,
) -> str:
    """Compose a case's working file as plain text, digest-bounded (ADR-0138 §10).

    The sections in the ADR's order, each headed and omitted when empty: The
    question (The task), Where we stand (At close on a closed case),
    Threads, Figures, Conversations, Open points (Questions to ask),
    Decision and Record. Reads only (see the module docstring); on an open
    or waiting case one :class:`~services.cases.stand.StandContext` is
    loaded for the composition, on a closed case none.

    Args:
        session: A tenant-scoped session.
        case: The case, in any state.
        now: The instant Where we stand is composed at (timezone-aware).
        user_names: Display names by user id for the users the text names
            (:func:`file_user_ids`); an id without a name reads *unknown
            user* — never the id.

    Returns:
        The file as text, at most :data:`MAX_CHARS` characters.
    """
    names: Mapping[UUID, str] = user_names or {}
    finding = (
        await IreneFindingRepository(session).get(case.finding_id)
        if case.finding_id is not None
        else None
    )
    subject_key = finding.subject_key if finding is not None else None
    cases = CaseRepository(session)
    threads = await CaseThreadRepository(session).list_for_case(case.id)
    kept_rows = await CaseKeptRepository(session).list_for_case(case.id)
    figures = await CaseFigureRepository(session).list_for_case(case.id)
    conversations = await CaseConversationRepository(session).list_for_case(case.id)
    points = await CasePointRepository(session).list_for_case(case.id)
    decisions = CaseDecisionRepository(session)
    recorded = await decisions.list_recorded(case.id)
    draft = await decisions.get_draft(case.id)
    entries = await cases.list_entries(case.id)

    figure_rows: Sequence[Any] = figures
    book_values: dict[str, dict] | None
    if case.state == "closed":
        # ADR-0138 §6: a closed case renders its last frozen snapshot — the
        # block At close and the figures as frozen; nothing reads the book.
        frozen = await cases.latest_frozen(case.id)
        file_block = frozen.payload.get("file") if frozen is not None else None
        block = file_block.get("where_we_stand") if isinstance(file_block, Mapping) else None
        if frozen is None:
            stand_lines = [_NO_FROZEN_TEXT]
        elif not isinstance(block, Mapping):
            stand_lines = [_NOT_FROZEN_TEXT]
        else:
            stand_lines = [
                f"Frozen {_utc(frozen.created_at):%d %b %Y %H:%M}.",
                *_frozen_stand_lines(block, subject_key=subject_key),
            ]
        stand_section = _section(_AT_CLOSE_HEADING, stand_lines)
        frozen_figures = file_block.get("figures") if isinstance(file_block, Mapping) else None
        if isinstance(frozen_figures, list):
            figure_rows = [row for row in frozen_figures if isinstance(row, Mapping)]
        book_values = None
    else:
        try:
            # A SAVEPOINT: a database error inside rolls back to it, and the
            # reads below still run on the same transaction (D-CS-B-120).
            async with session.begin_nested():
                ctx = await StandContext.load(session, now=now)
                stand = await compose(ctx, case, figures=figures)
        except Exception as exc:  # noqa: BLE001 — the brief must never fail on the stand
            logger.warning(
                "file text: Where we stand is unavailable for case %s (%s: %s).",
                case.id,
                type(exc).__name__,
                _first_line(exc),
            )
            stand_section = [_clip(_STAND_UNAVAILABLE_TEXT.format(reason=_first_line(exc)))]
            book_values = {}
        else:
            stand_section = _section(_STAND_HEADING, _stand_lines(stand, subject_key=subject_key))
            book_values = book_values_for(stand, figures)

    live_thread_ids = {thread.id for thread in threads}
    documents = [
        row
        for row in kept_rows
        if row.artifact == "document" and _at_file_level(row, live_thread_ids)
    ]
    sections = [
        _render_question(case, finding, documents, names),
        stand_section,
        _render_threads(threads, kept_rows),
        await _render_figures(session, figure_rows, book_values),
        _render_conversations(conversations),
        _render_points(case, points, names),
        _render_decision(recorded, draft),
        _render_record(case, entries, finding, names),
    ]
    return _cap("\n\n".join("\n".join(section) for section in sections if section))


__all__ = ["LINE_CHARS", "MAX_CHARS", "file_user_ids", "render_case_file_text"]
