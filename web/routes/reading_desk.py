# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The Reading Desk's Library routes (ADR-0139 §7, §11).

What every signed-in user of the tenant may see of the Reading Desk: the
Library with its filters and search, one reading as the Section's
sub-surface, and the Section's plain head. The run routes, and the Section
frame itself (``GET /scraper/section``), stay in ``web/routes/scraper.py``.

Endpoints:

* ``GET /api/reading-desk/library``                 — the Library pane, or
                                                       its rows alone.
* ``GET /api/reading-desk/readings/{reading_number}`` — one reading, live
                                                       or removed, with the
                                                       crumb out of band.
* ``GET /api/reading-desk/head``                    — the plain Section head,
                                                       out of band.

There is no role gate: every role reads, ``auditor`` included (RD-D-16,
RD-D-19), and row-level security keeps other tenants out — a reading of
another tenant is simply not found. What a role cannot *use* is not
rendered (D-RD-2-2); ``can_read`` (owner or member) decides the Read tab's
pointers.

A reading is untrusted text. Everything here projects it into plain values
and the templates render them through Jinja's autoescape; no HTML is built
in Python. A removed reading's context is built by its own function and
holds only its identity and removal fields (ADR-0140, D-RD-2-14).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, cast
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from core.repositories import (
    DocumentReadingDTO,
    DocumentReadingRepository,
    InvestmentRepository,
    ReadingPurgeDTO,
    UserRepository,
)
from core.repositories._session import tenant_context
from core.repositories.user_repository import UserDTO
from services.auth.session import SessionDTO
from services.scraper.profiles import PROFILE_IDS, ProfileError, load_profiles
from web.auth import require_session
from web.permissions import get_authenticated_user

logger = logging.getLogger(__name__)
router = APIRouter()

#: Rows per Library page (Q-RD-2-1).
PAGE_SIZE = 50

_UNKNOWN_FILTER = "Unknown filter value."
_NOT_FOUND = "Reading not found."
_MISSING = "—"

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


def _type_labels() -> dict[str, str]:
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
        f"Retention run: documents older than {purge.years} years "
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


def project_findings(findings: Iterable[dict] | None) -> dict[str, Any]:
    """The findings of a reading as plain values for ``reading_desk_findings.html``.

    A finding counts as found unless its confidence is ``Not_Found`` or its
    value is blank. Values stay as stored (D-RD-2-12).

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


def _reading_context(
    reading: DocumentReadingDTO,
    *,
    people: dict[UUID, str],
    investments: dict[UUID, str],
    type_labels: dict[str, str],
    can_read: bool,
) -> dict[str, Any]:
    """The context of a live reading."""
    type_label = type_labels.get(reading.document_type, reading.document_type)
    language = language_name(reading.language)
    assignment: dict[str, str] | None = None
    if reading.investment_id is not None or reading.manager_name is not None:
        if reading.investment_id is not None:
            name = investments.get(reading.investment_id, _MISSING)
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
    return {
        "head": {
            "crumb": f"{reading.label} · {type_label}",
            "asof": f"Read {_utc_minute(reading.read_at)}",
        },
        "reading": {
            "label": reading.label,
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
            f"{purge.years} years (before {purge.cutoff.isoformat()})"
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
    type_labels = _type_labels()
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
    engine = request.app.state.engine
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
        investments = (
            {
                investment.id: investment.name
                for investment in await InvestmentRepository(db).list_all()
            }
            if reading.investment_id is not None and not reading.is_removed
            else {}
        )
    templates = _templates(request)
    if reading.is_removed:
        return cast(
            HTMLResponse,
            templates.TemplateResponse(
                request,
                "_partials/reading_desk_reading_removed.html",
                _removed_context(reading, people=people, purges=purges),
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
                type_labels=_type_labels(),
                can_read=user.has_role("owner", "member"),
            ),
        ),
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
