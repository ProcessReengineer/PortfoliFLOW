# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The Reading Desk's Library routes (ADR-0139 §5, §6, §7, §11).

What every signed-in user of the tenant may see of the Reading Desk — the
Library with its filters and search, one reading as the Section's
sub-surface, the Section's plain head — what owner and member may do to
one reading: assign it and remove it — and the owner's retention run. The
run routes, and the Section frame itself (``GET /scraper/section``), stay
in ``web/routes/scraper.py``.

Endpoints:

* ``GET /api/reading-desk/library``                 — the Library pane, or
                                                       its rows alone.
* ``GET /api/reading-desk/readings/{reading_number}`` — one reading, live
                                                       or removed, with the
                                                       crumb out of band.
* ``GET /api/reading-desk/head``                    — the plain Section head,
                                                       out of band.
* ``POST /api/reading-desk/readings/{reading_number}/assign`` — assign a
                                                       reading or clear its
                                                       assignment.
* ``POST /api/reading-desk/readings/{reading_number}/remove`` — remove one
                                                       reading, with a reason.
* ``GET /api/reading-desk/retention``               — the Retention pane;
                                                       with ``years`` the
                                                       preview of a run.
* ``POST /api/reading-desk/retention/run``          — the retention run.

The GET routes have no role gate: every role reads, ``auditor`` included
(RD-D-16, RD-D-19), and row-level security keeps other tenants out — a
reading of another tenant is simply not found. What a role cannot *use* is
not rendered (D-RD-2-2); ``can_read`` (owner or member — the roles that
start a run, assign and remove, RD-D-19) decides it. The two POST routes
are gated to those roles and verify the CSRF token; everyone else gets 403.

A write answers with the reading as it now stands. A refused write does
too, with the reason first in the panel that caused it: 400 for an input
the route cannot use, 409 when the reading was removed meanwhile.

The two retention routes are the owner's alone (RD-D-17, RD-D-19) — the
preview too, although it writes nothing: nobody else has the tab. The run
verifies the CSRF token and removes only what the owner confirmed: it
carries the previewed age, cutoff and count, and a preview that no longer
holds is refused with 409 and shown again.

A reading is untrusted text. Everything here projects it into plain values
and the templates render them through Jinja's autoescape; no HTML is built
in Python. A removed reading's context is built by its own function and
holds only its identity and removal fields (ADR-0140, D-RD-2-14).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, cast
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from core.exceptions import ReadingInvalid, ReadingRemoved
from core.repositories import (
    DocumentReadingDTO,
    DocumentReadingRepository,
    InvestmentDTO,
    InvestmentRepository,
    ReadingPurgeDTO,
    ReadingPurgePreview,
    UserRepository,
)
from core.repositories._session import tenant_context
from core.repositories.user_repository import UserDTO
from services.auth.session import SessionDTO
from services.reading_desk import propose_for_names
from services.scraper.profiles import PROFILE_IDS, ProfileError, load_profiles
from web.auth import require_session, verify_csrf
from web.permissions import get_authenticated_user, require_role

logger = logging.getLogger(__name__)
router = APIRouter()

#: Rows per Library page (Q-RD-2-1).
PAGE_SIZE = 50

_UNKNOWN_FILTER = "Unknown filter value."
_NOT_FOUND = "Reading not found."
_MISSING = "—"

#: What ``to`` may name on an assignment, and where its answer lands.
_ASSIGN_TARGETS = frozenset({"proposal", "investment", "manager", "none"})
_ASSIGN_VIEWS = frozenset({"reading", "result"})
_UNKNOWN_ASSIGNMENT = "Unknown assignment."
_MANAGER_NAME_MAX = 200
_REASON_MAX = 2000

_CHOOSE_INVESTMENT = "Choose an investment from the list."
_ENTER_MANAGER = "Enter a manager name."
_MANAGER_TOO_LONG = "A manager name has at most 200 characters."
_NO_PROPOSAL = "The proposal no longer applies."
_REASON_REQUIRED = "A reason is required."
_REASON_TOO_LONG = "A reason has at most 2,000 characters."

#: The oldest age a retention run accepts, in calendar years.
_RETENTION_YEARS_MAX = 100
_BAD_YEARS = "Enter a whole number of years, from 1 to 100."
_STALE_PREVIEW = "The preview is out of date. Check it again before you confirm."

#: The stored ISO 639-1 code, shown by name; any other code is shown as stored.
_LANGUAGES: dict[str, str] = {
    "en": "English",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "nl": "Dutch",
    "sv": "Swedish",
    "da": "Danish",
    "no": "Norwegian",
    "fi": "Finnish",
    "pt": "Portuguese",
    "pl": "Polish",
}

_ORIGINS: dict[str, str] = {"upload": "Upload", "case_attachment": "Case attachment"}

_CONFIDENCES: dict[str, tuple[str, str]] = {
    "High": ("high", "High"),
    "Medium": ("medium", "Medium"),
    "Low": ("low", "Low"),
}

_NUMERIC_TYPES = frozenset({"Number", "Percentage"})

#: A number as the model writes it: digits, at most one decimal point.
_PLAIN_NUMBER = re.compile(r"([+-]?)(\d+)(\.\d+)?")
_CURRENCY_CODE = re.compile(r"[A-Z]{3}")


# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------


def _templates(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)


def _utc_date(moment: datetime) -> str:
    """The UTC calendar date of ``moment``, ISO."""
    return moment.astimezone(timezone.utc).date().isoformat()


def _utc_minute(moment: datetime) -> str:
    """``moment`` in UTC to the minute, with the zone named."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def format_size(size_bytes: int) -> str:
    """A file size for a reader: bytes below 1 KB, else one decimal in KB, MB or GB.

    Args:
        size_bytes: The size in bytes.

    Returns:
        The size, in steps of 1024.
    """
    if size_bytes < 1024:
        return f"{size_bytes} B"
    value = size_bytes / 1024
    for unit in ("KB", "MB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _short_sha(sha256: str) -> str:
    return f"{sha256[:6]}…{sha256[-4:]}"


def _joined(*parts: str | None) -> str:
    """The present parts joined by `` · ``."""
    return " · ".join(part for part in parts if part)


def language_name(code: str | None) -> str | None:
    """The language of a stored ISO 639-1 code, by name; another code as stored.

    Args:
        code: The stored code, or ``None``.

    Returns:
        The name, the code itself, or ``None`` when no language was read.
    """
    if not code:
        return None
    return _LANGUAGES.get(code, code)


def document_type_labels() -> dict[str, str]:
    """The profile labels by id; the ids themselves when the profiles do not load."""
    try:
        loaded = load_profiles()
    except ProfileError as exc:
        # The exception's text carries a server path; name the type only.
        logger.error("reading desk: document profiles not loaded (%s)", type(exc).__name__)
        return {profile_id: profile_id for profile_id in PROFILE_IDS}
    return {profile_id: loaded[profile_id].label for profile_id in PROFILE_IDS}


def _person(people: dict[UUID, str], user_id: UUID | None) -> str:
    if user_id is None:
        return _MISSING
    return people.get(user_id, _MISSING)


async def _people(repo: UserRepository) -> dict[UUID, str]:
    """Every user of the tenant as shown: display name, else email."""
    return {user.id: user.display_name or user.email for user in await repo.list_all()}


def _fold(value: str | None) -> str:
    return (value or "").strip().casefold()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _name_pairs(investments: Sequence[InvestmentDTO]) -> list[tuple[str, str | None]]:
    """``(name, manager_name)`` of the investments, the shape the proposal rule reads."""
    return [(investment.name, investment.manager_name) for investment in investments]


def _manager_suggestions(investments: Sequence[InvestmentDTO], used: Iterable[str]) -> list[str]:
    """The manager names to suggest: those of the investments and those already used.

    One spelling per name — the first met, the investments' first — sorted
    without regard to case.
    """
    seen: dict[str, str] = {}
    for name in [*(investment.manager_name for investment in investments), *used]:
        key = _fold(name)
        if name is not None and key and key not in seen:
            seen[key] = name.strip()
    return sorted(seen.values(), key=str.casefold)


# ---------------------------------------------------------------------------
# The Library's filters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Filters:
    """The Library's filters, parsed; the raw values are what the controls show."""

    type: str
    assigned: str
    date_from: str
    date_to: str
    removed: bool
    q: str
    offset: int
    document_type: str | None
    unassigned: bool
    investment_id: UUID | None
    manager_name: str | None
    from_date: date | None
    to_date: date | None

    @property
    def any_set(self) -> bool:
        """Whether a filter, the search or *Show removed* is set."""
        return bool(
            self.type or self.assigned or self.date_from or self.date_to or self.removed or self.q
        )

    def older_href(self) -> str:
        """The next page's rows: every filter that is set, the next offset, rows alone."""
        params: list[tuple[str, str]] = [
            (name, value)
            for name, value in (
                ("type", self.type),
                ("assigned", self.assigned),
                ("date_from", self.date_from),
                ("date_to", self.date_to),
                ("removed", "1" if self.removed else ""),
            )
            if value
        ]
        params.append(("offset", str(self.offset + PAGE_SIZE)))
        params.append(("rows", "1"))
        return f"/api/reading-desk/library?{urlencode(params)}"

    def keeps(self, reading: DocumentReadingDTO) -> bool:
        """Whether ``reading`` passes the filters, applied in Python.

        The manager name means the name the reading is *assigned* to; the
        repository's filter also matches the name as read.
        """
        if self.document_type is not None and reading.document_type != self.document_type:
            return False
        if self.unassigned and (
            reading.investment_id is not None or reading.manager_name is not None
        ):
            return False
        if self.investment_id is not None and reading.investment_id != self.investment_id:
            return False
        if self.manager_name is not None and _fold(reading.manager_name) != _fold(
            self.manager_name
        ):
            return False
        if self.from_date is None and self.to_date is None:
            return True
        # A reading without a document date drops out under either bound.
        dated = reading.document_date
        if dated is None:
            return False
        return (self.from_date is None or dated >= self.from_date) and (
            self.to_date is None or dated <= self.to_date
        )


def _bad_filter() -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, _UNKNOWN_FILTER)


def _parse_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise _bad_filter() from None


def _parse_flag(value: str) -> bool:
    if value not in ("", "1"):
        raise _bad_filter()
    return value == "1"


def _parse_filters(
    *,
    document_type: str,
    assigned: str,
    date_from: str,
    date_to: str,
    removed: str,
    q: str,
    offset: str,
) -> _Filters:
    """Parse the Library's query parameters; a value outside the table is a 400."""
    if document_type and document_type not in PROFILE_IDS:
        raise _bad_filter()
    unassigned = False
    investment_id: UUID | None = None
    manager_name: str | None = None
    if assigned == "none":
        unassigned = True
    elif assigned.startswith("inv:"):
        try:
            investment_id = UUID(assigned[len("inv:") :])
        except ValueError:
            raise _bad_filter() from None
    elif assigned.startswith("mgr:"):
        manager_name = assigned[len("mgr:") :].strip()
        if not manager_name:
            raise _bad_filter()
    elif assigned:
        raise _bad_filter()
    if offset == "":
        offset_value = 0
    elif offset.isascii() and offset.isdigit():
        offset_value = int(offset)
    else:
        raise _bad_filter()
    return _Filters(
        type=document_type,
        assigned=assigned,
        date_from=date_from,
        date_to=date_to,
        removed=_parse_flag(removed),
        q=q.strip(),
        offset=offset_value,
        document_type=document_type or None,
        unassigned=unassigned,
        investment_id=investment_id,
        manager_name=manager_name,
        from_date=_parse_date(date_from),
        to_date=_parse_date(date_to),
    )


# ---------------------------------------------------------------------------
# Projections
# ---------------------------------------------------------------------------


def _retention_reason(purge: ReadingPurgeDTO | None) -> str:
    if purge is None:
        return _MISSING
    return (
        f"Retention run: documents older than {_counted(purge.years, 'year')} "
        f"(before {purge.cutoff.isoformat()})."
    )


def _project_row(
    reading: DocumentReadingDTO,
    *,
    people: dict[UUID, str],
    investments: dict[UUID, str],
    purges: dict[UUID, ReadingPurgeDTO],
    type_labels: dict[str, str],
) -> dict[str, Any]:
    """One Library row as plain values; a tombstone carries its identity and removal only."""
    row: dict[str, Any] = {
        "label": reading.label,
        "href": f"/assistants/readings/{reading.reading_number}",
        "api_href": f"/api/reading-desk/readings/{reading.reading_number}",
        "filename": reading.filename,
        "read": f"{_utc_date(reading.read_at)} · {_person(people, reading.read_by)}",
    }
    if reading.removed_at is not None:
        if reading.purge_id is not None:
            reason = _retention_reason(purges.get(reading.purge_id))
        else:
            reason = reading.removal_reason or _MISSING
        row.update(
            state="removed",
            removed=(
                f"Removed {_utc_date(reading.removed_at)} · {_person(people, reading.removed_by)}"
            ),
            reason=reason,
        )
        return row
    assignment: dict[str, str] | None = None
    if reading.investment_id is not None:
        assignment = {
            "name": investments.get(reading.investment_id, _MISSING),
            "kind": "investment",
        }
    elif reading.manager_name is not None:
        assignment = {"name": reading.manager_name, "kind": "manager name"}
    row.update(
        state="live",
        sub=_joined(reading.fund_name_read, reading.period_label),
        type_label=type_labels.get(reading.document_type, reading.document_type),
        document_date=reading.document_date.isoformat() if reading.document_date else None,
        assignment=assignment,
    )
    return row


def _as_read(page: object, source: str) -> str:
    has_page = isinstance(page, int) and not isinstance(page, bool)
    if has_page and source:
        return f"as read, p. {page} · {source}"
    if has_page:
        return f"as read, p. {page}"
    if source:
        return f"as read · {source}"
    return "as read"


def _text(value: object) -> str:
    """A stored text value, or ``""`` for anything that is not a string."""
    return value if isinstance(value, str) else ""


def _grouped(value: str, unit: str) -> str:
    """A plain number with its thousands grouped by commas (D-RD-2-36).

    Five and more integer digits are always grouped; four only for an amount,
    which carries a currency code — a vintage year stays ``2024``. Anything
    that is not digits with at most one decimal point is returned as stored.
    """
    match = _PLAIN_NUMBER.fullmatch(value.strip())
    if match is None:
        return value
    sign, digits, fraction = match.group(1), match.group(2), match.group(3) or ""
    if len(digits) < (4 if _CURRENCY_CODE.fullmatch(unit) else 5):
        return value
    return f"{sign}{int(digits):,}{fraction}"


def project_findings(findings: Iterable[dict] | None) -> dict[str, Any]:
    """The findings of a reading as plain values for ``reading_desk_findings_table.html``.

    A finding counts as found unless its confidence is ``Not_Found`` or its
    value is blank. Values stay as stored (D-RD-2-12), except that a plain
    number's thousands are grouped for reading (D-RD-2-36). The projection
    also feeds a result block of a run (``web/routes/scraper.py``).

    Args:
        findings: The stored findings, in stored order, or ``None``.

    Returns:
        ``total``, ``found_count``, ``rows`` (the found findings, a list value
        split into ``lines``),
        ``not_found`` (their labels) and ``not_found_summary``.
    """
    rows: list[dict[str, Any]] = []
    not_found: list[str] = []
    total = 0
    for finding in findings or ():
        total += 1
        label = _text(finding.get("label")) or _text(finding.get("key"))
        value = _text(finding.get("value"))
        confidence = _text(finding.get("confidence"))
        if confidence == "Not_Found" or not value.strip():
            not_found.append(label)
            continue
        field_type = _text(finding.get("type"))
        unit = _text(finding.get("unit")).strip()
        if unit and value.rstrip().endswith(unit):
            unit = ""
        if field_type == "List":
            kind = "list"
        elif field_type in _NUMERIC_TYPES:
            kind = "number"
            value = _grouped(value, unit)
        else:
            kind = "text"
        state, state_label = _CONFIDENCES.get(confidence, ("low", "Low"))
        rows.append(
            {
                "label": label,
                "adhoc": finding.get("origin") == "adhoc",
                "kind": kind,
                "value": value,
                "lines": [line.strip() for line in value.splitlines() if line.strip()],
                "unit": unit,
                "quote": _text(finding.get("quote")).strip(),
                "as_read": _as_read(finding.get("page"), _text(finding.get("source")).strip()),
                "confidence": state,
                "confidence_label": state_label,
            }
        )
    count = len(not_found)
    summary = (
        "1 field was not found in the document"
        if count == 1
        else f"{count} fields were not found in the document"
    )
    return {
        "total": total,
        "found_count": len(rows),
        "rows": rows,
        "not_found": not_found,
        "not_found_summary": summary,
    }


def _lines(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def project_note(note: dict | None, language: str | None) -> dict[str, Any]:
    """A reading's note as plain values for ``reading_desk_note.html``.

    Args:
        note: The stored note, or ``None``.
        language: The document's language by name, or ``None`` when unknown.

    Returns:
        ``sub``, ``highlights``, ``quotes`` (``text`` and ``cite``),
        ``events``, ``outlook`` and ``empty``.
    """
    note = note or {}
    quotes: list[dict[str, str]] = []
    raw_quotes = note.get("quotes")
    for quote in raw_quotes if isinstance(raw_quotes, list) else []:
        if not isinstance(quote, dict):
            continue
        quote_text = _text(quote.get("text"))
        if not quote_text.strip():
            continue
        page = quote.get("page")
        has_page = isinstance(page, int) and not isinstance(page, bool)
        quotes.append(
            {"text": quote_text, "cite": f"as read, p. {page}" if has_page else "as read"}
        )
    highlights = _lines(note.get("highlights"))
    events = _lines(note.get("events_and_risks"))
    outlook = _text(note.get("outlook")).strip()
    in_language = f" ({language})" if language else ""
    return {
        "sub": (
            "written by the model from the document, in the document's language"
            f"{in_language} · quotes are as read and not checked"
        ),
        "highlights": highlights,
        "quotes": quotes,
        "events": events,
        "outlook": outlook,
        "empty": not (highlights or quotes or events or outlook),
    }


def _write_side(
    reading: DocumentReadingDTO,
    *,
    investments: Sequence[InvestmentDTO],
    manager_names: Sequence[str],
    assign_error: str | None,
    remove_error: str | None,
    remove_reason: str,
) -> dict[str, Any]:
    """What owner and member get on a live reading beyond its content.

    ``proposal`` is worked out again from the names the reading holds — it
    is not stored — and only while the reading is unassigned. Beneath a
    proposal the panel that assigns by hand stands open and has no foot;
    otherwise it is closed, unless a refused assignment comes back in it.
    """
    assigned = reading.investment_id is not None or reading.manager_name is not None
    proposed = (
        None
        if assigned
        else propose_for_names(
            reading.fund_name_read, reading.manager_name_read, _name_pairs(investments)
        )
    )
    if reading.manager_name is not None:
        manager_value = reading.manager_name
    elif assigned:
        manager_value = ""
    elif proposed is not None and proposed.kind == "manager":
        manager_value = proposed.name
    else:
        manager_value = reading.manager_name_read or ""
    return {
        "proposal": (
            {"kind": proposed.kind, "name": proposed.name} if proposed is not None else None
        ),
        "assign": {
            "title": "Or assign it yourself" if proposed is not None else "Assign this reading",
            "open": proposed is not None or assign_error is not None,
            "closable": proposed is None,
            "assigned": assigned,
            "error": assign_error,
            "investments": [
                {
                    "id": str(investment.id),
                    "name": investment.name,
                    "selected": investment.id == reading.investment_id,
                }
                for investment in investments
            ],
            "manager_value": manager_value,
            "manager_names": _manager_suggestions(investments, manager_names),
        },
        "remove": {
            "open": remove_error is not None,
            "error": remove_error,
            "reason": remove_reason,
        },
    }


def _reading_context(
    reading: DocumentReadingDTO,
    *,
    people: dict[UUID, str],
    investments: Sequence[InvestmentDTO],
    manager_names: Sequence[str],
    type_labels: dict[str, str],
    can_read: bool,
    assign_error: str | None = None,
    remove_error: str | None = None,
    remove_reason: str = "",
) -> dict[str, Any]:
    """The context of a live reading; for ``can_read`` with its write side."""
    type_label = type_labels.get(reading.document_type, reading.document_type)
    language = language_name(reading.language)
    investment_names = {investment.id: investment.name for investment in investments}
    assignment: dict[str, str] | None = None
    if reading.investment_id is not None or reading.manager_name is not None:
        if reading.investment_id is not None:
            name = investment_names.get(reading.investment_id, _MISSING)
            kind = "investment"
        else:
            name = reading.manager_name or _MISSING
            kind = "manager name"
        when = _utc_date(reading.assigned_at) if reading.assigned_at else _MISSING
        assignment = {
            "name": name,
            "hint": f"{kind} · {_person(people, reading.assigned_by)}, {when}",
        }
    extra = [
        f"{_text(keyword.get('name'))} ({_text(keyword.get('type'))})"
        for keyword in reading.adhoc_keywords
        if isinstance(keyword, dict) and _text(keyword.get("name"))
    ]
    context: dict[str, Any] = {
        "head": {
            "crumb": f"{reading.label} · {type_label}",
            "asof": f"Read {_utc_minute(reading.read_at)}",
        },
        "reading": {
            "label": reading.label,
            "number": reading.reading_number,
            "title": _joined(reading.fund_name_read, reading.period_label) or reading.filename,
            "sub": f"{type_label} · as read from {reading.filename}",
            "document_date": (
                reading.document_date.isoformat() if reading.document_date else _MISSING
            ),
            "manager_read": reading.manager_name_read or _MISSING,
            "currency": reading.currency or _MISSING,
            "language": language or _MISSING,
            "pages": reading.page_count,
            "read": f"{_utc_date(reading.read_at)} · {_person(people, reading.read_by)}",
            "assignment": assignment,
            "how": {
                "model": reading.model,
                "document_type": f"{type_label}, profile version {reading.profile_version}",
                "prompt_version": reading.prompt_version,
                "extra_keywords": ", ".join(extra),
                "came_in_as": _ORIGINS.get(reading.origin, reading.origin),
                "file": f"{format_size(reading.size_bytes)} · SHA-256 {_short_sha(reading.sha256)}",
            },
        },
        "findings": project_findings(reading.findings),
        "note": project_note(reading.note, language),
        "can_read": can_read,
    }
    if can_read:
        context.update(
            _write_side(
                reading,
                investments=investments,
                manager_names=manager_names,
                assign_error=assign_error,
                remove_error=remove_error,
                remove_reason=remove_reason,
            )
        )
    return context


def _removed_context(
    reading: DocumentReadingDTO,
    *,
    people: dict[UUID, str],
    purges: dict[UUID, ReadingPurgeDTO],
) -> dict[str, Any]:
    """The context of a removed reading: its identity and its removal, nothing else.

    Type, fund name, manager, period, currency, language, model and
    assignment are not passed, so the template cannot render them
    (ADR-0140, D-RD-2-14).
    """
    removed_at = cast(datetime, reading.removed_at)
    if reading.purge_id is not None:
        purge = purges.get(reading.purge_id)
        reason = (
            f"Retention run of {_utc_date(purge.run_at)} — documents older than "
            f"{_counted(purge.years, 'year')} (before {purge.cutoff.isoformat()})"
            if purge is not None
            else _MISSING
        )
    else:
        reason = reading.removal_reason or _MISSING
    size = format_size(reading.size_bytes)
    if reading.page_count is not None:
        pages = "1 page" if reading.page_count == 1 else f"{reading.page_count} pages"
        size = f"{size} · {pages}"
    return {
        "head": {"crumb": reading.label},
        "removed": {
            "label": reading.label,
            "removed": f"{_utc_date(removed_at)} · {_person(people, reading.removed_by)}",
            "reason": reason,
            "filename": reading.filename,
            "size": size,
            "read": f"{_utc_date(reading.read_at)} · {_person(people, reading.read_by)}",
            "came_in_as": _ORIGINS.get(reading.origin, reading.origin),
        },
    }


# ---------------------------------------------------------------------------
# GET /api/reading-desk/library
# ---------------------------------------------------------------------------


@router.get("/api/reading-desk/library", response_class=HTMLResponse)
async def reading_desk_library(
    request: Request,
    document_type: str = Query("", alias="type"),
    assigned: str = "",
    date_from: str = "",
    date_to: str = "",
    removed: str = "",
    q: str = "",
    offset: str = "",
    rows: str = "",
    session: SessionDTO = Depends(require_session),
    user: UserDTO = Depends(get_authenticated_user),
) -> HTMLResponse:
    """The Library pane, or with ``rows=1`` its rows alone (ADR-0139 §7).

    Without a search the list is paged newest read first, ``PAGE_SIZE``
    rows at a time; the repository is asked for one row more to know
    whether an older page exists. With a search the whole match is
    narrowed by the same filters in Python and not paged (Q-RD-2-2); a
    removed reading never matches a search.

    Raises:
        HTTPException: 400 for a value outside the filters' vocabulary.
    """
    filters = _parse_filters(
        document_type=document_type,
        assigned=assigned,
        date_from=date_from,
        date_to=date_to,
        removed=removed,
        q=q,
        offset=offset,
    )
    rows_only = _parse_flag(rows)
    engine = request.app.state.engine
    has_older = False
    manager_names: list[str] = []
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        repo = DocumentReadingRepository(db)
        if filters.q:
            found = [reading for reading in await repo.search(filters.q) if filters.keeps(reading)]
        else:
            fetched = await repo.list_readings(
                document_type=filters.document_type,
                investment_id=filters.investment_id,
                manager_name=filters.manager_name,
                assigned=False if filters.unassigned else None,
                document_date_from=filters.from_date,
                document_date_to=filters.to_date,
                include_removed=filters.removed,
                limit=PAGE_SIZE + 1,
                offset=filters.offset,
            )
            has_older = len(fetched) > PAGE_SIZE
            found = fetched[:PAGE_SIZE]
            if filters.manager_name is not None:
                found = [reading for reading in found if filters.keeps(reading)]
        people = await _people(UserRepository(db))
        investment_rows = await InvestmentRepository(db).list_all()
        purges = (
            {purge.id: purge for purge in await repo.list_purges()}
            if any(reading.purge_id is not None for reading in found)
            else {}
        )
        if not rows_only:
            manager_names = await repo.list_manager_names()
    investments = {investment.id: investment.name for investment in investment_rows}
    type_labels = document_type_labels()
    projected = [
        _project_row(
            reading,
            people=people,
            investments=investments,
            purges=purges,
            type_labels=type_labels,
        )
        for reading in found
    ]
    older_href = filters.older_href() if has_older else None
    templates = _templates(request)
    if rows_only:
        return cast(
            HTMLResponse,
            templates.TemplateResponse(
                request,
                "_partials/reading_desk_library_rows.html",
                {"rows": projected, "older_href": older_href},
            ),
        )
    return cast(
        HTMLResponse,
        templates.TemplateResponse(
            request,
            "_partials/reading_desk_library.html",
            {
                "filters": filters,
                "type_options": list(type_labels.items()),
                "investment_options": [
                    (str(investment.id), investment.name) for investment in investment_rows
                ],
                "manager_names": manager_names,
                "rows": projected,
                "older_href": older_href,
                "any_set": filters.any_set,
                "can_read": user.has_role("owner", "member"),
            },
        ),
    )


# ---------------------------------------------------------------------------
# GET /api/reading-desk/readings/{reading_number}
# ---------------------------------------------------------------------------


@router.get("/api/reading-desk/readings/{reading_number}", response_class=HTMLResponse)
async def reading_desk_reading(
    request: Request,
    reading_number: int,
    session: SessionDTO = Depends(require_session),
    user: UserDTO = Depends(get_authenticated_user),
) -> HTMLResponse:
    """One reading as the Section's sub-surface, the crumb written out of band.

    A live reading renders ``reading_desk_reading.html``; a tombstone
    renders ``reading_desk_reading_removed.html`` from a context that holds
    its identity and removal only.

    Raises:
        HTTPException: 404 for a number the tenant does not have.
    """
    return await _render_reading(request, session, user, reading_number)


async def _render_reading(
    request: Request,
    session: SessionDTO,
    user: UserDTO,
    reading_number: int,
    *,
    status_code: int = status.HTTP_200_OK,
    assign_error: str | None = None,
    remove_error: str | None = None,
    remove_reason: str = "",
) -> HTMLResponse:
    """A reading as it stands now — the answer to the GET and to every write.

    Reads in a transaction of its own, so after a write it shows what was
    committed. The investments are read for a live reading that is assigned
    to one, and for owner and member, whose panel lists them; the names
    already used on readings are read for owner and member only.

    Args:
        request: The request.
        session: The session; its tenant and user scope the read.
        user: The signed-in user; the role decides the write side.
        reading_number: The reading's number in the tenant.
        status_code: 200, or the status of the refused write this answers.
        assign_error: The refusal of an assignment, shown in its panel.
        remove_error: The refusal of a removal, shown in its panel.
        remove_reason: The reason that was sent with a refused removal.

    Raises:
        HTTPException: 404 for a number the tenant does not have.
    """
    can_read = user.has_role("owner", "member")
    engine = request.app.state.engine
    investments: list[InvestmentDTO] = []
    manager_names: list[str] = []
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        repo = DocumentReadingRepository(db)
        reading = await repo.get_by_number(reading_number)
        if reading is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
        people = await _people(UserRepository(db))
        purges = (
            {purge.id: purge for purge in await repo.list_purges()}
            if reading.purge_id is not None
            else {}
        )
        if not reading.is_removed:
            if can_read or reading.investment_id is not None:
                investments = await InvestmentRepository(db).list_all()
            if can_read:
                manager_names = await repo.list_manager_names()
    templates = _templates(request)
    if reading.is_removed:
        return cast(
            HTMLResponse,
            templates.TemplateResponse(
                request,
                "_partials/reading_desk_reading_removed.html",
                _removed_context(reading, people=people, purges=purges),
                status_code=status_code,
            ),
        )
    return cast(
        HTMLResponse,
        templates.TemplateResponse(
            request,
            "_partials/reading_desk_reading.html",
            _reading_context(
                reading,
                people=people,
                investments=investments,
                manager_names=manager_names,
                type_labels=document_type_labels(),
                can_read=can_read,
                assign_error=assign_error,
                remove_error=remove_error,
                remove_reason=remove_reason,
            ),
            status_code=status_code,
        ),
    )


# ---------------------------------------------------------------------------
# POST /api/reading-desk/readings/{reading_number}/assign
# ---------------------------------------------------------------------------


def _answer(state: str, lead: str, sub: str = "") -> dict[str, str]:
    """The context of ``reading_desk_assigned.html``."""
    return {"state": state, "lead": lead, "sub": sub}


def _chosen_investment(value: str, investments: Sequence[InvestmentDTO]) -> InvestmentDTO | None:
    """The tenant's investment a submitted id names, or ``None``."""
    try:
        chosen = UUID(value.strip())
    except ValueError:
        return None
    return next((investment for investment in investments if investment.id == chosen), None)


@router.post(
    "/api/reading-desk/readings/{reading_number}/assign",
    response_class=HTMLResponse,
    dependencies=[Depends(require_role("owner", "member"))],
)
async def reading_desk_assign(
    request: Request,
    reading_number: int,
    to: str = Form(""),
    investment_id: str = Form(""),
    manager_name: str = Form(""),
    view: str = Form("reading"),
    session: SessionDTO = Depends(require_session),
    user: UserDTO = Depends(get_authenticated_user),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Assign a reading, or clear its assignment (ADR-0139 §6, RD-D-7, RD-D-19).

    ``to`` names the target: ``investment`` (with ``investment_id``),
    ``manager`` (with ``manager_name``), ``none`` (unassign), or
    ``proposal`` — the proposal is not stored, so it is worked out again
    from the names the reading holds and assigned if it still applies.

    ``view`` names where the answer lands. ``reading`` (the reading's page)
    answers with the reading as it now stands; a refusal comes back in the
    panel, with 400. ``result`` (a result block of a run) answers with one
    note in the proposal's place. A reading removed meanwhile answers 409
    in either view.

    Raises:
        HTTPException: 400 for a ``to`` or ``view`` outside the vocabulary,
            404 for a number the tenant does not have.
    """
    if to not in _ASSIGN_TARGETS or view not in _ASSIGN_VIEWS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _UNKNOWN_ASSIGNMENT)
    engine = request.app.state.engine
    removed = False
    refusal: str | None = None
    target_investment: InvestmentDTO | None = None
    target_manager: str | None = None
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        repo = DocumentReadingRepository(db)
        reading = await repo.get_by_number(reading_number)
        if reading is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
        label = reading.label
        if reading.is_removed:
            removed = True
        else:
            investments = (
                await InvestmentRepository(db).list_all()
                if to in ("proposal", "investment")
                else []
            )
            if to == "proposal":
                proposed = propose_for_names(
                    reading.fund_name_read, reading.manager_name_read, _name_pairs(investments)
                )
                if proposed is None:
                    refusal = _NO_PROPOSAL
                elif proposed.kind == "investment":
                    target_investment = next(
                        investment for investment in investments if investment.name == proposed.name
                    )
                else:
                    target_manager = proposed.name
            elif to == "investment":
                target_investment = _chosen_investment(investment_id, investments)
                if target_investment is None:
                    refusal = _CHOOSE_INVESTMENT
            elif to == "manager":
                target_manager = manager_name.strip() or None
                if target_manager is None:
                    refusal = _ENTER_MANAGER
                elif len(target_manager) > _MANAGER_NAME_MAX:
                    refusal = _MANAGER_TOO_LONG
            if refusal is None:
                try:
                    await repo.assign(
                        reading.id,
                        investment_id=target_investment.id if target_investment else None,
                        manager_name=target_manager,
                        assigned_by=session.user_id,
                        now=_now(),
                    )
                except ReadingRemoved:
                    removed = True
                except ReadingInvalid:
                    # The route has checked what the repository checks; a
                    # refusal here is a target that vanished in between.
                    refusal = _CHOOSE_INVESTMENT if to == "investment" else _NO_PROPOSAL
    if view == "reading":
        if removed:
            return await _render_reading(
                request, session, user, reading_number, status_code=status.HTTP_409_CONFLICT
            )
        if refusal is not None:
            return await _render_reading(
                request,
                session,
                user,
                reading_number,
                status_code=status.HTTP_400_BAD_REQUEST,
                assign_error=refusal,
            )
        return await _render_reading(request, session, user, reading_number)
    if removed:
        answer = _answer("refused", f"{label} was removed.", "Nothing was assigned.")
        status_code = status.HTTP_409_CONFLICT
    elif refusal is not None:
        answer = _answer("refused", refusal, f"Open {label} to assign it yourself.")
        status_code = status.HTTP_400_BAD_REQUEST
    else:
        if target_investment is not None:
            lead = f"Assigned to investment “{target_investment.name}”."
        elif target_manager is not None:
            lead = f"Assigned to manager “{target_manager}”."
        else:
            lead = "Unassigned."
        answer = _answer("done", lead)
        status_code = status.HTTP_200_OK
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/reading_desk_assigned.html",
            {"answer": answer},
            status_code=status_code,
        ),
    )


# ---------------------------------------------------------------------------
# POST /api/reading-desk/readings/{reading_number}/remove
# ---------------------------------------------------------------------------


@router.post(
    "/api/reading-desk/readings/{reading_number}/remove",
    response_class=HTMLResponse,
    dependencies=[Depends(require_role("owner", "member"))],
)
async def reading_desk_remove(
    request: Request,
    reading_number: int,
    reason: str = Form(""),
    session: SessionDTO = Depends(require_session),
    user: UserDTO = Depends(get_authenticated_user),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Remove one reading, with a reason (RD-D-8, RD-D-17, RD-D-19).

    The reading becomes a tombstone: its findings, its note and its search
    text are deleted, its entry stays with the reason. The answer is the
    removed reading. Without a reason nothing is removed and the reading
    comes back with the refusal in the panel (400); a reading that already
    is removed answers 409 with the removed reading.

    Raises:
        HTTPException: 404 for a number the tenant does not have.
    """
    engine = request.app.state.engine
    removed_already = False
    refusal: str | None = None
    stripped = reason.strip()
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        repo = DocumentReadingRepository(db)
        reading = await repo.get_by_number(reading_number)
        if reading is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
        if reading.is_removed:
            removed_already = True
        elif not stripped:
            refusal = _REASON_REQUIRED
        elif len(stripped) > _REASON_MAX:
            refusal = _REASON_TOO_LONG
        else:
            try:
                await repo.remove(
                    reading.id, reason=stripped, removed_by=session.user_id, now=_now()
                )
            except ReadingRemoved:
                removed_already = True
    if removed_already:
        return await _render_reading(
            request, session, user, reading_number, status_code=status.HTTP_409_CONFLICT
        )
    if refusal is not None:
        return await _render_reading(
            request,
            session,
            user,
            reading_number,
            status_code=status.HTTP_400_BAD_REQUEST,
            remove_error=refusal,
            remove_reason=reason,
        )
    return await _render_reading(request, session, user, reading_number)


# ---------------------------------------------------------------------------
# GET /api/reading-desk/retention · POST /api/reading-desk/retention/run
# ---------------------------------------------------------------------------


def _parse_years(value: str) -> int | None:
    """The age a retention form names, or ``None`` for anything but 1 to 100."""
    text = value.strip()
    if not 1 <= len(text) <= 3 or not text.isascii() or not text.isdigit():
        return None
    years = int(text)
    return years if 1 <= years <= _RETENTION_YEARS_MAX else None


def _counted(count: int, noun: str) -> str:
    """``1 reading``, ``14 readings``."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _retention_context(
    *,
    years: str,
    runs: Sequence[ReadingPurgeDTO],
    people: dict[UUID, str],
    preview: ReadingPurgePreview | None = None,
    done: ReadingPurgeDTO | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """The context of the Retention pane.

    ``done`` (a run that was just recorded) comes before ``preview``; a
    preview of nothing is the state ``zero``, which offers no confirmation.
    """
    state = "idle"
    lead = sub = action = cutoff = ""
    count = 0
    if done is not None:
        state = "done"
        lead = f"{_counted(done.reading_count, 'reading')} removed."
    elif preview is not None:
        cutoff = preview.cutoff.isoformat()
        count = preview.reading_count
        if count == 0:
            state = "zero"
            lead = f"No reading has a document date before {cutoff}."
        elif count == 1:
            state = "preview"
            lead = f"1 reading has a document date before {cutoff}."
            sub = (
                "Its findings and reading note will be deleted and cannot be restored. "
                "The entry stays in the Library as removed and names this run. "
                "Anything already kept into a case, and anything entered in the book, "
                "is not touched."
            )
            action = "Remove 1 reading"
        else:
            state = "preview"
            lead = f"{count} readings have a document date before {cutoff}."
            sub = (
                "Their findings and reading notes will be deleted and cannot be restored. "
                f"The {count} entries stay in the Library as removed and name this run. "
                "Anything already kept into a case, and anything entered in the book, "
                "is not touched."
            )
            action = f"Remove {count} readings"
    return {
        "retention": {
            "state": state,
            "years": years,
            "error": error,
            "lead": lead,
            "sub": sub,
            "action": action,
            "cutoff": cutoff,
            "count": count,
            "runs": [
                {
                    "run": _utc_minute(run.run_at),
                    "by": people.get(run.run_by, _MISSING),
                    "rule": f"Older than {_counted(run.years, 'year')}",
                    "cutoff": run.cutoff.isoformat(),
                    "count": run.reading_count,
                }
                for run in runs
            ],
        }
    }


def _render_retention(
    request: Request, context: dict[str, Any], *, status_code: int = status.HTTP_200_OK
) -> HTMLResponse:
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/reading_desk_retention.html",
            context,
            status_code=status_code,
        ),
    )


@router.get(
    "/api/reading-desk/retention",
    response_class=HTMLResponse,
    dependencies=[Depends(require_role("owner"))],
)
async def reading_desk_retention(
    request: Request,
    years: str | None = Query(None),
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """The Retention pane; with ``years`` the preview of a run (ADR-0139 §5).

    A preview counts the live readings whose document date — the day they
    were read where a document has none — is before today (UTC) minus
    ``years`` calendar years. It writes nothing. An age that is not a whole
    number from 1 to 100 answers 400 with the pane and the refusal.
    """
    parsed = _parse_years(years) if years is not None else None
    refused = years is not None and parsed is None
    engine = request.app.state.engine
    preview: ReadingPurgePreview | None = None
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        repo = DocumentReadingRepository(db)
        if parsed is not None:
            preview = await repo.preview_purge(years=parsed, today=_now().date())
        runs = await repo.list_purges()
        people = await _people(UserRepository(db))
    return _render_retention(
        request,
        _retention_context(
            years=years or "",
            runs=runs,
            people=people,
            preview=preview,
            error=_BAD_YEARS if refused else None,
        ),
        status_code=status.HTTP_400_BAD_REQUEST if refused else status.HTTP_200_OK,
    )


@router.post(
    "/api/reading-desk/retention/run",
    response_class=HTMLResponse,
    dependencies=[Depends(require_role("owner"))],
)
async def reading_desk_retention_run(
    request: Request,
    years: str = Form(""),
    cutoff: str = Form(""),
    count: str = Form(""),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Run retention as the owner confirmed it (ADR-0139 §5, RD-D-8, RD-D-17).

    The confirmation carries what the preview showed: the age, the cutoff
    and the count. The route previews again in the run's own transaction
    and runs only if both still hold; otherwise nothing is removed and the
    pane comes back with the preview as it now stands (409). Nothing to
    remove is never a run — a run that matches nothing would still be
    recorded — so it is answered the same way.

    The run tombstones every matching reading and records one
    ``reading_purges`` row; the pane comes back with the run heading the
    past runs.
    """
    parsed = _parse_years(years)
    engine = request.app.state.engine
    preview: ReadingPurgePreview | None = None
    done: ReadingPurgeDTO | None = None
    error: str | None = None
    status_code = status.HTTP_200_OK
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        repo = DocumentReadingRepository(db)
        if parsed is None:
            error = _BAD_YEARS
            status_code = status.HTTP_400_BAD_REQUEST
        else:
            now = _now()
            preview = await repo.preview_purge(years=parsed, today=now.date())
            confirmed = cutoff.strip() == preview.cutoff.isoformat() and count.strip() == str(
                preview.reading_count
            )
            if preview.reading_count == 0 or not confirmed:
                error = _STALE_PREVIEW
                status_code = status.HTTP_409_CONFLICT
            else:
                done = await repo.run_purge(
                    years=parsed, today=now.date(), run_by=session.user_id, now=now
                )
        runs = await repo.list_purges()
        people = await _people(UserRepository(db))
    return _render_retention(
        request,
        _retention_context(
            years=years, runs=runs, people=people, preview=preview, done=done, error=error
        ),
        status_code=status_code,
    )


# ---------------------------------------------------------------------------
# GET /api/reading-desk/head
# ---------------------------------------------------------------------------


@router.get("/api/reading-desk/head", response_class=HTMLResponse)
async def reading_desk_head(
    request: Request,
    session: SessionDTO = Depends(require_session),
    user: UserDTO = Depends(get_authenticated_user),
) -> HTMLResponse:
    """The Section's plain head, both elements out of band.

    It reads nothing from the database; the two dependencies only
    authenticate the request, as on every route of the Reading Desk.
    """
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(request, "_partials/reading_desk_head.html", {}),
    )
