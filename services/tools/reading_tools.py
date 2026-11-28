# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Shirley's reading tools: three untrusted reads over the Reading Desk (ADR-0139 §8).

Three read-only AI-callable tools over the readings the Reading Desk holds
for the tenant, so Shirley can name what was read, read one, and find a word
across all of them — in the web chat and on a voice call:

- ``list_readings`` — the live readings, newest read first, one line each,
  filtered by document type, investment (exact name), manager name,
  assignment and document-date range; through
  :meth:`DocumentReadingRepository.list_readings`.
- ``get_reading`` — one reading by its number: the head as read, the
  assignment, every finding with its stored value, page, quote and
  confidence, the fields not found, and the reading note; through
  :meth:`DocumentReadingRepository.get_by_number`.
- ``search_readings`` — a word or phrase over the stored search text of the
  live readings, with up to three matching lines per reading; through
  :meth:`DocumentReadingRepository.search`.

Untrusted, and why (ADR-0139 §8, ADR-0022)
------------------------------------------
Everything a reading holds was read by a model from an outside document. So
all three register with :class:`~services.tool_classes.ToolClass`
``READ_EXTERNAL_UNTRUSTED`` with ``wraps_result_as_untrusted`` set: the tool
registry wraps each result in ``<external_content … trust="untrusted">`` and
locks ``case_add`` for the rest of the turn. The way a reading reaches a case
is the PM's *Keep* on the reading's page, never the model.

Every return path — a result, a calm refusal, an unset context — is the
``{"source", "fetched_at", "body"}`` envelope ``web_research`` returns, so the
registry never falls back to raw-string wrapping. ``source`` is fixed text —
``reading-desk:<tool>`` or ``reading-desk:R-nnnn`` — and never carries text
from a reading or from an argument.

The registry writes the body between its delimiters without escaping it.
So every string taken from a reading (and an investment's or a user's name)
passes :func:`_inert`, which breaks the registry's own delimiter wherever it
appears, and single-line fields are collapsed to one line so a value cannot
forge a line of the listing. A tombstone is never shown: an unknown number
and a removed reading answer the same line (D-RD-3-1).

The descriptions carry the behaviour: ``prompts/Soul_Shirley.md`` is
byte-identical (the Soul-hash rule).

Pattern reuse (ADR-0047)
------------------------
Each tool mirrors ``case_tools.py``: a synchronous ``Callable[..., str]``
that reads the tool-execution context first, then runs its async workflow
through :func:`services.tools._async_bridge.run_async_in_fresh_loop` in a
loop-local, tenant-scoped session (:func:`services.tools._tool_session.tool_session`).
Users and investments appear by name, resolved inside that session; an id
never reaches the text.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.repositories.document_reading_repository import (
    DocumentReadingDTO,
    DocumentReadingRepository,
)
from core.repositories.investment_repository import InvestmentRepository
from core.repositories.user_repository import UserRepository
from services.scraper.profiles import PROFILE_IDS, ProfileError, load_profiles
from services.tool_classes import ToolClass
from services.tool_registry import get_tool_registry
from services.tools._async_bridge import run_async_in_fresh_loop
from services.tools._tool_context import get_tool_context
from services.tools._tool_session import tool_session

logger = logging.getLogger(__name__)

#: The three tools this module registers, in registration order.
_TOOL_NAMES: tuple[str, ...] = ("list_readings", "get_reading", "search_readings")

#: At most this many readings per ``list_readings`` and ``search_readings`` call.
_LIST_LIMIT = 50

#: A search needs at least this many characters.
_MIN_QUERY_CHARS = 2

#: Up to this many matching lines of a reading's search text (D-RD-3-3).
_MATCH_LINES = 3

#: The envelope's ``source`` (D-RD-3-4): fixed text, never a reading's.
_SOURCE_PREFIX = "reading-desk:"

#: The ``assigned`` filter's vocabulary; ``any`` is no filter.
_ASSIGNED: tuple[str, ...] = ("any", "assigned", "unassigned")

# Returned by every tool when no consumer has populated the tool-execution
# context — the graceful-degradation path of the case tools.
_CONTEXT_NOT_SET_MSG = (
    "Readings are not available in this context. (The tool-execution context "
    "was not set — the reading tools read the persistent database and are only "
    "available from the web chat surface.)"
)

# The calm lines.
_UNKNOWN_TYPE_MSG = (
    "Unknown document type — use quarterly_report, capital_notice, pitch_deck or other."
)
_BAD_ASSIGNED_MSG = "assigned must be any, assigned or unassigned."
_BAD_DATE_MSG = "Dates are YYYY-MM-DD."
_UNKNOWN_INVESTMENT_MSG = (
    "No investment named «{name}» in this book. `list_investments` names them."
)
_BAD_NUMBER_MSG = "Give the reading number as a whole number, e.g. 47 for R-0047."
_NO_READING_MSG = (
    "There is no reading {label} to read — the number is unknown or the reading was "
    "removed. `list_readings` names the readings held."
)
_SHORT_QUERY_MSG = "Give at least two characters to search for."
_MORE_LISTED_MSG = "… more — narrow the filters."
_MORE_FOUND_MSG = "… more — search for something narrower."
_NOT_CHECKED_MSG = (
    "Values and quotes are the model's reading of the document; they are not checked against it."
)
_NONE = "none"
_UNKNOWN_USER = "unknown user"
_GONE_INVESTMENT = "an investment no longer in the book"

#: A confidence that means the field was not found (the Reading Desk's rule).
_NOT_FOUND = "Not_Found"
#: The field type whose value is one entry per line.
_LIST_TYPE = "List"
#: A finding the PM asked for on the run, beside the profile's fields.
_ADHOC_ORIGIN = "adhoc"

_ORIGIN_LINES: dict[str, str] = {
    "upload": "came in by upload",
    "case_attachment": "came in from a case attachment",
}

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

#: ``<``, an optional ``/`` and optional whitespace before ``external_content``:
#: the registry's own delimiter, in any case (D-RD-3-5).
_DELIMITER = re.compile(r"<(?=/?\s*external_content)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Envelope and inert text
# ---------------------------------------------------------------------------


def _envelope(source: str, body: str) -> str:
    """Build the ``{source, fetched_at, body}`` JSON envelope (``web_research``'s shape).

    Consumed by the ToolRegistry's ``wraps_result_as_untrusted`` path, which
    unpacks these fields into ``<external_content>`` wrapper attributes.
    """
    return json.dumps(
        {
            "source": _SOURCE_PREFIX + source,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "body": body,
        }
    )


def _inert(text: str) -> str:
    """Break the registry's delimiter in ``text``: a space after each such ``<``."""
    return _DELIMITER.sub("< ", text)


def _one_line(value: object) -> str:
    """A stored text value on one line and inert; ``""`` for anything but a string."""
    if not isinstance(value, str):
        return ""
    return _inert(" ".join(value.split()))


# ---------------------------------------------------------------------------
# Lines
# ---------------------------------------------------------------------------


def _readings(n: int, *, more: bool = False) -> str:
    """``n readings``, ``1 reading``, or ``50+ readings`` when more came back."""
    if more:
        return f"{n}+ readings"
    return f"{n} reading{'' if n == 1 else 's'}"


def _clean(value: object) -> str:
    """An optional string argument, stripped; anything else is empty."""
    return value.strip() if isinstance(value, str) else ""


def _parse_iso_date(raw: object) -> date | None:
    """``YYYY-MM-DD`` as a date; ``None`` when absent.

    Raises:
        ValueError: On anything but a calendar date in that one form.
    """
    text = _clean(raw)
    if not text:
        return None
    if not _ISO_DATE.fullmatch(text):
        raise ValueError(text)
    return date.fromisoformat(text)


def _parse_reading_number(raw: object) -> int | None:
    """A reading number ≥ 1 from the argument: an integer, or ``"47"`` / ``"R-0047"``.

    The schema asks for an integer; a model that sends the label or a numeric
    string is read the same. Anything else — a bool, a fraction, zero — is
    ``None``.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)
    if isinstance(raw, str):
        digits = raw.strip().upper().removeprefix("R-").strip()
        raw = int(digits) if digits.isdigit() else None
    return raw if isinstance(raw, int) and raw >= 1 else None


def _label(reading_number: int) -> str:
    """``R-nnnn`` — the form :attr:`DocumentReadingDTO.label` makes."""
    return f"R-{reading_number:04d}"


def _type_labels() -> dict[str, str]:
    """The profile labels by id; the ids themselves when the profiles do not load."""
    try:
        loaded = load_profiles()
    except ProfileError as exc:
        # The exception's text carries a server path; name the type only.
        logger.error("reading tools: document profiles not loaded (%s)", type(exc).__name__)
        return {profile_id: profile_id for profile_id in PROFILE_IDS}
    return {profile_id: loaded[profile_id].label for profile_id in PROFILE_IDS}


def _is_found(finding: dict) -> bool:
    """A finding counts as found unless it is ``Not_Found`` or its value is blank."""
    value = finding.get("value")
    if finding.get("confidence") == _NOT_FOUND:
        return False
    return isinstance(value, str) and bool(value.strip())


def _finding_label(finding: dict) -> str:
    return _one_line(finding.get("label")) or _one_line(finding.get("key"))


def _found_count(reading: DocumentReadingDTO) -> str:
    findings = [f for f in reading.findings or () if isinstance(f, dict)]
    found = sum(1 for finding in findings if _is_found(finding))
    return f"{found} of {len(findings)} fields found"


def _assignment(reading: DocumentReadingDTO, investments: dict[UUID, str]) -> str:
    """``assigned to investment <name>`` / ``assigned to manager <name>`` / ``unassigned``."""
    if reading.investment_id is not None:
        name = investments.get(reading.investment_id)
        return f"assigned to investment {name}" if name else f"assigned to {_GONE_INVESTMENT}"
    if reading.manager_name is not None:
        return f"assigned to manager {_one_line(reading.manager_name)}"
    return "unassigned"


def _reading_line(
    reading: DocumentReadingDTO, type_labels: dict[str, str], investments: dict[UUID, str]
) -> str:
    """One reading: *R-0047 · Quarterly report · Fund X · Q3 2026 · document date … · …*."""
    parts = [
        reading.label,
        type_labels.get(reading.document_type, reading.document_type),
        _one_line(reading.fund_name_read),
        _one_line(reading.period_label),
        f"document date {reading.document_date.isoformat()}" if reading.document_date else "",
        f"read {reading.read_at.astimezone(timezone.utc).date().isoformat()}",
        _assignment(reading, investments),
        _found_count(reading),
        f"file {_one_line(reading.filename)}",
    ]
    return " · ".join(part for part in parts if part)


async def _investment_names(
    session: AsyncSession, readings: Iterable[DocumentReadingDTO]
) -> dict[UUID, str]:
    """Investment names by id, one lookup per distinct id; a vanished id is left out."""
    repo = InvestmentRepository(session)
    names: dict[UUID, str] = {}
    for investment_id in {r.investment_id for r in readings if r.investment_id is not None}:
        investment = await repo.get_by_id(investment_id)
        if investment is not None:
            names[investment_id] = _one_line(investment.name)
    return names


def _listing(
    head: str,
    readings: Sequence[DocumentReadingDTO],
    type_labels: dict[str, str],
    investments: dict[UUID, str],
    *,
    more_line: str,
    beneath: dict[UUID, list[str]] | None = None,
) -> str:
    """The head line, then one line per reading (at most :data:`_LIST_LIMIT`), or *none*."""
    if not readings:
        return f"{head}\n{_NONE}"
    lines = [head]
    for reading in readings[:_LIST_LIMIT]:
        lines.append(_reading_line(reading, type_labels, investments))
        lines.extend(f"    {line}" for line in (beneath or {}).get(reading.id, ()))
    if len(readings) > _LIST_LIMIT:
        lines.append(more_line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# get_reading — the body
# ---------------------------------------------------------------------------


def _page(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _finding_lines(finding: dict) -> list[str]:
    """One found finding: ``- label: value unit · as read, p. n · source · confidence · quote``.

    A ``List`` value prints its entries beneath the label line, one indented
    line per stored line (D-RD-3-9).
    """
    label = _finding_label(finding)
    if finding.get("origin") == _ADHOC_ORIGIN:
        label = f"{label} · ad hoc"
    raw_value = finding.get("value")
    value = raw_value if isinstance(raw_value, str) else ""
    page = _page(finding.get("page"))
    quote = _one_line(finding.get("quote"))
    meta = [
        f"as read, p. {page}" if page is not None else "",
        _one_line(finding.get("source")),
        f"confidence {_one_line(finding.get('confidence'))}",
        f"quote: “{quote}”" if quote else "",
    ]
    if finding.get("type") == _LIST_TYPE:
        entries = [_one_line(line) for line in value.splitlines() if line.strip()]
        head = " · ".join(part for part in (f"list of {len(entries)}", *meta) if part)
        return [f"- {label}: {head}", *(f"    {entry}" for entry in entries)]
    shown = _one_line(value)
    unit = _one_line(finding.get("unit"))
    if unit and not shown.endswith(unit):
        shown = f"{shown} {unit}"
    return [f"- {label}: " + " · ".join(part for part in (shown, *meta) if part)]


def _entries(value: object) -> list[str]:
    """A note list's entries, each on one line; blanks and non-strings dropped."""
    if not isinstance(value, list):
        return []
    return [line for line in (_one_line(item) for item in value) if line]


def _note_lines(note: dict | None) -> list[str]:
    """``Reading note:`` and its four parts, each only when it has content."""
    note = note or {}
    highlights = _entries(note.get("highlights"))
    quotes: list[str] = []
    raw_quotes = note.get("quotes")
    for quote in raw_quotes if isinstance(raw_quotes, list) else []:
        if not isinstance(quote, dict):
            continue
        text = _one_line(quote.get("text"))
        if not text:
            continue
        page = _page(quote.get("page"))
        cite = f"(as read, p. {page})" if page is not None else "(as read)"
        quotes.append(f"- “{text}” {cite}")
    events = _entries(note.get("events_and_risks"))
    outlook = _one_line(note.get("outlook"))
    if not (highlights or quotes or events or outlook):
        return [f"Reading note: {_NONE}"]
    lines = ["Reading note:"]
    if highlights:
        lines += ["Highlights:", *(f"- {entry}" for entry in highlights)]
    if quotes:
        lines += ["Quotes:", *quotes]
    if events:
        lines += ["Events and risks:", *(f"- {entry}" for entry in events)]
    if outlook:
        lines.append(f"Outlook: {outlook}")
    return lines


def _reading_body(
    reading: DocumentReadingDTO,
    *,
    type_label: str,
    investment_name: str | None,
    reader: str,
) -> str:
    """The text of one live reading, in the order of the prompt's §1."""
    pages = reading.page_count
    first = [
        reading.label,
        f"{type_label} (profile version {reading.profile_version})",
        f"file {_one_line(reading.filename)}",
        (f"{pages} page{'' if pages == 1 else 's'}" if pages else ""),
    ]
    lines = [" · ".join(part for part in first if part)]

    head = [
        ("Fund as read", _one_line(reading.fund_name_read)),
        ("Manager as read", _one_line(reading.manager_name_read)),
        ("Period", _one_line(reading.period_label)),
        (
            "Document date",
            reading.document_date.isoformat() if reading.document_date else "",
        ),
        ("Currency", _one_line(reading.currency)),
        ("Language", _one_line(reading.language)),
    ]
    head_line = " · ".join(f"{name}: {value}" for name, value in head if value)
    if head_line:
        lines.append(head_line)

    if reading.investment_id is not None:
        lines.append(
            f"Assigned to investment {investment_name}"
            if investment_name
            else f"Assigned to {_GONE_INVESTMENT}"
        )
    elif reading.manager_name is not None:
        lines.append(f"Assigned to manager {_one_line(reading.manager_name)}")
    else:
        lines.append("Unassigned")

    read_on = reading.read_at.astimezone(timezone.utc).date().isoformat()
    origin = _ORIGIN_LINES.get(reading.origin, "")
    lines.append(" · ".join(part for part in (f"Read {read_on} by {reader}", origin) if part))
    lines.append(_NOT_CHECKED_MSG)

    findings = [f for f in reading.findings or () if isinstance(f, dict)]
    found = [f for f in findings if _is_found(f)]
    lines.append(f"Findings ({len(found)} of {len(findings)} found; values as stored):")
    for finding in found:
        lines.extend(_finding_lines(finding))
    missing = [_finding_label(f) for f in findings if not _is_found(f)]
    if missing:
        lines.append("Not found: " + "; ".join(missing))

    lines.extend(_note_lines(reading.note))
    return "\n".join(lines)


def _matching_lines(search_text: str | None, query: str) -> list[str]:
    """Up to three lines of ``search_text`` holding ``query`` (``str.casefold``)."""
    wanted = query.casefold()
    hits: list[str] = []
    for raw in (search_text or "").splitlines():
        line = " ".join(raw.split())
        if line and wanted in line.casefold():
            hits.append(_inert(line))
            if len(hits) == _MATCH_LINES:
                break
    return hits


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------


def list_readings(  # noqa: PLR0913 — the schema's flat arguments
    document_type: str = "",
    investment_name: str = "",
    manager_name: str = "",
    assigned: str = "any",
    date_from: str = "",
    date_to: str = "",
) -> str:
    """List the tenant's live readings, newest read first, one line each.

    Args:
        document_type: A profile id — ``quarterly_report``, ``capital_notice``,
            ``pitch_deck`` or ``other``; empty for all.
        investment_name: Only readings assigned to the investment of this
            exact name.
        manager_name: The assigned manager name or the manager name as read.
        assigned: ``any`` (the default), ``assigned`` or ``unassigned``.
        date_from: ``YYYY-MM-DD``, the inclusive lower bound on the document date.
        date_to: ``YYYY-MM-DD``, the inclusive upper bound on the document date.

    Returns:
        The JSON envelope; its body is a head line *n readings (filters)* and
        one line per reading (at most fifty), or *none* — or a calm line for
        a bad argument, an unknown investment, or an unset context.
    """
    source = "list_readings"
    ctx = get_tool_context()
    if ctx is None:
        return _envelope(source, _CONTEXT_NOT_SET_MSG)
    chosen_type = _clean(document_type).lower()
    if chosen_type and chosen_type not in PROFILE_IDS:
        return _envelope(source, _UNKNOWN_TYPE_MSG)
    chosen_assigned = _clean(assigned).lower() or "any"
    if chosen_assigned not in _ASSIGNED:
        return _envelope(source, _BAD_ASSIGNED_MSG)
    try:
        start = _parse_iso_date(date_from)
        end = _parse_iso_date(date_to)
    except ValueError:
        return _envelope(source, _BAD_DATE_MSG)
    wanted_investment = _clean(investment_name)
    wanted_manager = _clean(manager_name)
    type_labels = _type_labels()

    async def _workflow() -> str:
        async with tool_session(ctx) as db:
            investment_id: UUID | None = None
            if wanted_investment:
                investment = await InvestmentRepository(db).get_by_name(wanted_investment)
                if investment is None:
                    return _UNKNOWN_INVESTMENT_MSG.format(name=_one_line(wanted_investment))
                investment_id = investment.id
            readings = await DocumentReadingRepository(db).list_readings(
                document_type=chosen_type or None,
                investment_id=investment_id,
                manager_name=wanted_manager or None,
                assigned=None if chosen_assigned == "any" else chosen_assigned == "assigned",
                document_date_from=start,
                document_date_to=end,
                limit=_LIST_LIMIT + 1,
            )
            investments = await _investment_names(db, readings[:_LIST_LIMIT])
        filters = [
            f"type {type_labels.get(chosen_type, chosen_type)}" if chosen_type else "",
            f"investment {_one_line(wanted_investment)}" if wanted_investment else "",
            f"manager {_one_line(wanted_manager)}" if wanted_manager else "",
            "" if chosen_assigned == "any" else chosen_assigned,
            f"document date from {start.isoformat()}" if start else "",
            f"document date to {end.isoformat()}" if end else "",
        ]
        scope = ", ".join(part for part in filters if part) or "all"
        more = len(readings) > _LIST_LIMIT
        head = f"{_readings(min(len(readings), _LIST_LIMIT), more=more)} ({scope})"
        return _listing(head, readings, type_labels, investments, more_line=_MORE_LISTED_MSG)

    return _envelope(source, run_async_in_fresh_loop(_workflow))


def get_reading(reading_number: int) -> str:
    """Return one live reading as text, by its number.

    Args:
        reading_number: The reading's number — ``47`` for ``R-0047``.

    Returns:
        The JSON envelope; its body is the reading as text, or a calm line
        for a number that is no whole number, an unknown or removed reading
        (one line for both, D-RD-3-1), or an unset context.
    """
    number = _parse_reading_number(reading_number)
    source = _label(number) if number is not None else "get_reading"
    ctx = get_tool_context()
    if ctx is None:
        return _envelope(source, _CONTEXT_NOT_SET_MSG)
    if number is None:
        return _envelope(source, _BAD_NUMBER_MSG)
    type_labels = _type_labels()

    async def _workflow() -> str | None:
        async with tool_session(ctx) as db:
            reading = await DocumentReadingRepository(db).get_by_number(number)
            # get_by_number returns tombstones; nothing of one is shown.
            if reading is None or reading.is_removed:
                return None
            investments = await _investment_names(db, (reading,))
            user = await UserRepository(db).get_by_id(reading.read_by)
        reader = (_one_line(user.display_name) or _one_line(user.email)) if user else ""
        investment_name = (
            investments.get(reading.investment_id) if reading.investment_id is not None else None
        )
        return _reading_body(
            reading,
            type_label=type_labels.get(reading.document_type, reading.document_type),
            investment_name=investment_name,
            reader=reader or _UNKNOWN_USER,
        )

    body = run_async_in_fresh_loop(_workflow)
    if body is None:
        body = _NO_READING_MSG.format(label=_label(number))
    return _envelope(source, body)


def search_readings(query: str) -> str:
    """Search the live readings' stored text for a word or phrase.

    Args:
        query: The word or phrase, at least two characters once collapsed.

    Returns:
        The JSON envelope; its body is a head line *n readings match "q"*,
        then per reading its line and up to three matching lines, or *none* —
        or a calm line for a short query or an unset context.
    """
    source = "search_readings"
    ctx = get_tool_context()
    if ctx is None:
        return _envelope(source, _CONTEXT_NOT_SET_MSG)
    stripped = " ".join(query.split()) if isinstance(query, str) else ""
    if len(stripped) < _MIN_QUERY_CHARS:
        return _envelope(source, _SHORT_QUERY_MSG)
    type_labels = _type_labels()

    async def _workflow() -> str:
        async with tool_session(ctx) as db:
            readings = await DocumentReadingRepository(db).search(stripped, limit=_LIST_LIMIT + 1)
            investments = await _investment_names(db, readings[:_LIST_LIMIT])
        beneath = {r.id: _matching_lines(r.search_text, stripped) for r in readings}
        more = len(readings) > _LIST_LIMIT
        shown = min(len(readings), _LIST_LIMIT)
        verb = "matches" if shown == 1 and not more else "match"
        head = f'{_readings(shown, more=more)} {verb} "{_inert(stripped)}"'
        return _listing(
            head,
            readings,
            type_labels,
            investments,
            more_line=_MORE_FOUND_MSG,
            beneath=beneath,
        )

    return _envelope(source, run_async_in_fresh_loop(_workflow))


# ---------------------------------------------------------------------------
# Register tools at import time
# ---------------------------------------------------------------------------

#: The closing sentences every description ends with.
_UNTRUSTED_CLOSING = (
    "A reading is what a model read from an outside document: the result is marked as "
    "untrusted external content and is cited as read, never treated as verified fact. "
    "After this tool runs, adding to a case (case_add) is locked for the rest of the turn "
    "(ADR-0022); the user puts a quote or a finding into a case with Keep on the reading's "
    "page. Read-only."
)

_registry = get_tool_registry()

_registry.register_tool(
    name="list_readings",
    function=list_readings,
    description=(
        "List the documents the Reading Desk has read for the active tenant, newest read "
        "first, one line per reading: its number (R-nnnn), the document type, the fund name "
        "and the period as read, the document date, the day it was read, what it is "
        "assigned to, how many of its fields were found, and the file name. Filters, all "
        "optional: document_type (quarterly_report, capital_notice, pitch_deck, other); "
        "investment_name (exact, as list_investments returns it); manager_name (the "
        "assigned manager or the manager as read); assigned (any, assigned, unassigned); "
        "date_from and date_to on the document date (YYYY-MM-DD). Use it for 'which reports "
        "have we read', 'did we see a pitch deck from X', and to find a reading's number for "
        "get_reading. At most 50 readings per call. " + _UNTRUSTED_CLOSING
    ),
    parameters={
        "type": "object",
        "properties": {
            "document_type": {
                "type": "string",
                "enum": list(PROFILE_IDS),
                "description": "Only readings of this document type.",
            },
            "investment_name": {
                "type": "string",
                "description": "Only readings assigned to this investment, its exact name.",
            },
            "manager_name": {
                "type": "string",
                "description": "The assigned manager name or the manager name as read.",
            },
            "assigned": {
                "type": "string",
                "enum": list(_ASSIGNED),
                "description": "any (default), assigned or unassigned.",
            },
            "date_from": {
                "type": "string",
                "description": "YYYY-MM-DD: the earliest document date.",
            },
            "date_to": {
                "type": "string",
                "description": "YYYY-MM-DD: the latest document date.",
            },
        },
        "required": [],
    },
    tool_class=ToolClass.READ_EXTERNAL_UNTRUSTED,
    wraps_result_as_untrusted=True,
)

_registry.register_tool(
    name="get_reading",
    function=get_reading,
    description=(
        "Read one reading by its number (47 for R-0047): the document's head as read (type, "
        "fund, manager, period, document date, currency, language), what it is assigned to, "
        "every finding with its value as stored, its unit, the page, the quote and the "
        "confidence, the fields that were not found, and the reading note (highlights, "
        "quotes with page, events and risks, outlook). Values are the stored ones, without "
        "thousands separators. Name the reading (R-nnnn) and the page whenever you use a "
        "finding, also in a comparison or an average across several readings. A removed "
        "reading cannot be read. " + _UNTRUSTED_CLOSING
    ),
    parameters={
        "type": "object",
        "properties": {
            "reading_number": {
                "type": "integer",
                "minimum": 1,
                "description": "The reading's number: 47 for R-0047.",
            },
        },
        "required": ["reading_number"],
    },
    tool_class=ToolClass.READ_EXTERNAL_UNTRUSTED,
    wraps_result_as_untrusted=True,
)

_registry.register_tool(
    name="search_readings",
    function=search_readings,
    description=(
        "Search every reading held for a word or phrase (case-insensitive substring, at "
        "least two characters) over what was read: file name, fund and manager name, "
        "period, the findings with their quotes, and the reading note. Returns the matching "
        "readings, newest read first, each with up to three of the lines that matched. It "
        "searches readings only — not the documents attached to cases and not the cases "
        "themselves (search_cases does that). At most 50 readings per call. " + _UNTRUSTED_CLOSING
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The word or phrase to look for, at least two characters.",
            },
        },
        "required": ["query"],
    },
    tool_class=ToolClass.READ_EXTERNAL_UNTRUSTED,
    wraps_result_as_untrusted=True,
)
