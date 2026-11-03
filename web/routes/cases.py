# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Cases area web surface — one Section, the list and the case (ADR-0138 §12).

The Cases area is one Section, ``cases``: the list with the filter Open ·
Waiting · Closed · All, *Mine* and search (``GET /api/cases/list``), and the
case opened as the Section's **sub-surface** (``GET /api/cases/{case_id}``),
which writes the crumb ``‹ Cases › CASE-nnnn`` into the Section head out of
band. The page URLs ``/cases`` and ``/cases/{case_id}`` are the shell's
(``web/routes/areas.py``); the shell's host loads one of the two partials. In
P-CS-3a the sub-surface is the page move of the retired stand-alone detail
page: the facts, the actions on an open case, the composer slot, the origin
embed or the description, and the record. The four composers (Add note / Pin
document / Record decision / Close case), the attachment upload/download
pipeline with its caps and the pin timeline anatomy are unchanged.

The module never mutates an Irene finding and never writes a Journal row.
The file *reads* a case's originating finding through the finding
repository's read-only ``get``; resolving a finding as ``opened_case`` lives
on the Watch Desk. A closed case carries the Journal deep link (ADR-0107 C4,
binding decision 2) on its list row and in its facts.

Pin payload contract (binding decision 4)
-----------------------------------------
A ``pin`` entry of artifact class ``document`` carries, in its opaque
``payload`` JSONB, exactly::

    {
      "artifact": "document",
      "comment": "<mandatory curation comment>",
      "attachment_id": "<uuid>",
      "filename": "<as uploaded, route-sanitised>",
      "mime_type": "<declared>",
      "size_bytes": <int>
    }

The timeline renders ``artifact == "document"`` fully (filename linking to the
download endpoint, human-legible size, curation comment); any other artifact
value falls to the calm generic fallback C3a established. C5 (Planning Desk,
``scenario_snapshot``) and C6 (Shirley, ``consultation``) extend this contract
with their own artifact classes and write exactly what the timeline reads.

A ``pin`` entry of artifact class ``scenario_snapshot`` (ADR-0107 C5, the
Planning Desk write) carries, in its opaque ``payload``, exactly::

    {
      "artifact": "scenario_snapshot",
      "comment": "<mandatory curation comment>",
      "snapshot": {
        "chips":         [{"label", "css_class"}, ...],
        "kpis":          [{"label", "base", "scen", "delta", "tone"}, ...],
        "headroom":      [{"family", "rows": [...]}, ...],
        "baseline_foot": {"nav", "ret"},
        "scenario_foot": {"nav", "nav_delta", "nav_tone", "ret", ...},
        "query":         "<canonical query string, no case marker>"
      }
    }

The snapshot is the presentation the Planning Desk results region *renders*,
frozen at capture: the parameter chips (label + tone only, never a live remove
link), the four KPI pairs, the headroom families, the two horizon feet, and the
canonical query string. **No charts.** The timeline renders it verbatim — a
frozen record, no recomputation and no live links into the Desk: the chips are a
parameter line, the KPI pairs read base → scenario with the frozen delta tone,
the headroom families collapse to a count line, the query is monospace text (not
an anchor — re-entering the parameters is the path back, not a rehydration
link). The Planning Desk writes exactly what this view reads.

A ``pin`` entry of artifact class ``consultation`` (ADR-0107 C6, the Shirley
write) carries, in its opaque ``payload``, exactly::

    {
      "artifact": "consultation",
      "comment": "<mandatory curation comment>",
      "excerpt": "<the curated Shirley text>"
    }

The excerpt is the part of a Shirley answer the PM *curated* — trimmed
server-prefilled text, never scraped bubble HTML (binding decision 2). Its entry
``actor`` is ``"pm"`` with the session user id: pinning is the PM's curation
act; the timeline anatomy attributes the *words* to Shirley (the quoted block),
the *decision to keep them* to the PM (the actor chip + curation comment). The
Shirley write lives in ``web/routes/chat.py``, which writes exactly what the
timeline reads.

A ``pin`` entry of artifact class ``chart_snapshot`` (ADR-0114, the second
Shirley write) carries, in its opaque ``payload``, exactly::

    {
      "artifact": "chart_snapshot",
      "comment": "<mandatory curation comment>",
      "caption": "<the chart's caption at render time>",
      "spec":    {"data": [...], "layout": {...}, "config": {...}}
    }

The ``spec`` is the **frozen Plotly figure** the PM saw — embedded, so the case
record is self-contained: no reference into the ephemeral session store the
chat surface resolves it from, and no reference into a shared results store
whose retention would then govern case integrity (ADR-0114 §Alternatives). The
timeline renders it verbatim through the shared render helper — nothing
recomputed and no live re-query, the same discipline ``scenario_snapshot``
keeps. The theme is baked into the spec at render time, so a later theme change
does not alter an archived chart; only the pinned Plotly.js version bounds the
fidelity (ADR-0042 §4). The write lives in ``web/routes/chat.py``
(``POST /api/chat/pin-chart``), which writes exactly what this view reads.

Attachment atomicity
--------------------
The upload lives *inside* the pin composer precisely so the attachment and its
pin entry are written in one transaction: ``create`` the attachment and
``append_entry`` the pin together, and if the entry append fails the whole
transaction — attachment included — rolls back, leaving no orphaned bytes.
Attachments are addressed only through their pin entry (ADR-0107 §7, the DMS
boundary): no standalone manager, no delete, no re-upload.

Caps (ADR-0107 §7) are configuration, read from ``core.config`` and enforced
here at the route — never hard-coded at the call site, never in the repository.

**Materiality-at-opening rendering contract (binding decision 2).** The
``opened`` entry's payload MAY carry
``{"materiality_at_opening": {"lines": ["<string>", ...]}}`` — presentation
strings frozen at case opening. The detail view renders them verbatim under
"At case opening" inside the origin embed **when present**, and nothing (no
placeholder) when absent. Today every case is manually opened with an empty
payload; C4's open-from-finding endpoint will write these strings via the
coverage service — so it must write exactly what this view reads.

Endpoints
---------
* ``GET  /api/cases/list`` — the list partial (D-CS-B-32): ``state`` one of
  ``open`` (open cases, then the Waiting group) · ``waiting`` · ``closed``
  (paged, ``before=``) · ``all``; ``mine=1`` filters on the owner
  (``owner_user_id``) — a filter, never a data boundary; a non-blank ``q``
  searches the working file (``search_working_file``, never attachments).
  The repository orders; the route never re-sorts.
* ``GET  /api/cases/new`` — the manual-creation form.
* ``POST /api/cases/new`` — validate the title, create the case (exactly one
  ``opened`` entry, ``actor='pm'``), and land the PM in its file
  (``HX-Redirect: /cases/{id}``). A blank title re-renders the form with an
  inline error and creates nothing.
* ``GET  /api/cases/{case_id}`` — the sub-surface partial (the case file).
  Unknown or foreign ids resolve to ``None`` under RLS and are mapped to 404,
  the entity-detail idiom (``web/routes/investments.py``).
* The composer, write, download and close routes below, unchanged.

Reads use ``session = Depends(require_session)``; the write also takes
``_ = Depends(verify_csrf)`` and opens a ``tenant_context`` so it is
RLS-policed. Passing ``user_id`` into the context is consistent with the
other write routes; the case tables install no audit trigger (b031 follows
the b019 idiom), so RLS — not a trigger — is what polices the write.
"""

from __future__ import annotations

import hashlib
import logging
import pathlib
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any, cast
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from starlette.datastructures import UploadFile

from core.config import get_config
from core.exceptions import CaseClosedError, CaseStateInvalid
from core.repositories._session import tenant_context
from core.repositories.case_attachment_repository import (
    CaseAttachmentRepository,
)
from core.repositories.case_repository import (
    CaseDTO,
    CaseEntryDTO,
    CaseRepository,
)
from core.repositories.case_working_file_repository import (
    CasePointDTO,
    CasePointRepository,
    CaseThreadDTO,
    CaseThreadRepository,
)
from core.repositories.irene_finding_repository import (
    IreneFindingDTO,
    IreneFindingRepository,
)
from core.repositories.user_repository import UserRepository
from services.auth.session import SessionDTO
from services.cases.stand import StandContext, headline_for
from web.auth import require_session, verify_csrf
from web.routes.watch_desk import JOURNAL_DEEP_LINK, resolution_label
from web.routes.planning_desk import AREA_URL as PLANNING_DESK_AREA_URL

logger = logging.getLogger(__name__)
router = APIRouter()

#: The assistants surface a case sends the PM to, "consulting for" it (ADR-0107
#: C6). Defined locally rather than imported from ``chat.py``: ``chat.py`` reuses
#: this module's projections for the case brief, so it imports *this* module —
#: importing the URL back would close an import cycle. The ``?case=<id>`` marker
#: is appended by the detail view, the same cross-module URL idiom C5 uses for
#: "Capture scenario".
CONSULT_SHIRLEY_URL: str = "/assistants"


# ---------------------------------------------------------------------------
# Wiring helpers
# ---------------------------------------------------------------------------


def _templates(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)


def _engine(request: Request) -> AsyncEngine:
    return cast(AsyncEngine, request.app.state.engine)


def _now() -> datetime:
    """Return the current instant as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# File / attachment helpers
# ---------------------------------------------------------------------------

_FILENAME_MAX_LEN: int = 255

# Chunk size for the bounded attachment read (the codebase's 64 KiB idiom).
_UPLOAD_CHUNK_BYTES: int = 64 * 1024


def _human_size(num_bytes: int) -> str:
    """Format a byte count as a short human-legible string (e.g. ``1.8 MB``).

    Whole bytes below 1 KB read as ``N B``; everything else as one decimal
    place in the largest unit under 1024. A small local helper — the data
    import surface formats KB inline in its template, so there is nothing
    shared to reuse.
    """
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _headroom_row_count(headroom: Any) -> int:
    """Count the frozen headroom rows across every family in a snapshot payload.

    The mock's scenario-snapshot entry shows compact key figures, not a headroom
    table, so the timeline states a **count** — "n headroom rows frozen" — over
    the frozen families rather than re-rendering a table the mock never promised
    (ADR-0107 C5, Step 4). Defensive over the opaque payload: a missing or
    malformed block counts zero.
    """
    if not isinstance(headroom, list):
        return 0
    total = 0
    for family in headroom:
        if isinstance(family, dict) and isinstance(family.get("rows"), list):
            total += len(family["rows"])
    return total


def _sanitise_filename(raw: str | None) -> str:
    """Return a safe basename for ``raw`` — the value we persist on the row.

    Strips path components (POSIX and Windows separators), trims whitespace and
    caps length, mirroring ``web/routes/data_import.py``'s upload sanitiser.
    Returns ``""`` when nothing usable remains.
    """
    if not raw:
        return ""
    candidate = raw.replace("\\", "/").strip()
    base = pathlib.PurePosixPath(candidate).name.strip()
    if not base:
        return ""
    return base[:_FILENAME_MAX_LEN]


def _file_extension(filename: str) -> str:
    """Return the lower-cased extension of ``filename`` without the dot."""
    return pathlib.PurePosixPath(filename).suffix.lower().lstrip(".")


def _sanitise_header_filename(name: str) -> str:
    """Sanitise a filename for a ``Content-Disposition`` header value.

    Strips quotes, control characters and path separators so the header cannot
    be broken out of or made to carry a path — the stored DB value is left
    untouched (this is a presentation concern only). Falls back to
    ``"attachment"`` when nothing usable remains.
    """
    cleaned = name.replace("\\", "_").replace("/", "_").replace('"', "")
    cleaned = "".join(ch for ch in cleaned if ch >= " ")  # drop control chars
    cleaned = cleaned.strip()
    return cleaned or "attachment"


def _caps_hint(cfg: Any) -> str:
    """Build the pin composer's visible caps line from configuration."""
    max_mb = cfg.case_attachment_max_bytes / (1024 * 1024)
    types = ", ".join(sorted(ext.upper() for ext in cfg.case_attachment_allowed_types))
    return (
        f"Up to {max_mb:.0f} MB per file · {types} · "
        f"max {cfg.case_attachment_max_count} attachments per case."
    )


# ---------------------------------------------------------------------------
# Owner-name resolution — the Journal's batch idiom
# ---------------------------------------------------------------------------


async def _resolve_owner_names(user_repo: UserRepository, ids: Iterable[UUID]) -> dict[UUID, str]:
    """Resolve user ids to display names, one batch (the Journal idiom).

    Mirrors ``web/routes/watch_desk.py``'s Journal actor resolution:
    look each distinct id up through :class:`UserRepository` and build a
    dict. Prefers ``display_name`` and falls back to ``email`` so the row
    reads as an owner name (the mock shows names), never a raw UUID. Ids
    that resolve to nothing are simply absent — the projection falls back to
    the stringified id, exactly as the Journal does for an unknown actor.
    """
    names: dict[UUID, str] = {}
    for user_id in {uid for uid in ids}:
        user = await user_repo.get_by_id(user_id)
        if user is not None:
            names[user_id] = user.display_name or user.email
    return names


def _owner_label(owner_id: UUID, owner_names: dict[UUID, str]) -> str:
    """Return the resolved owner name, or the stringified id as a fallback."""
    return owner_names.get(owner_id, str(owner_id))


# ---------------------------------------------------------------------------
# Projections — DTO → template-friendly dicts
# ---------------------------------------------------------------------------


_CLOSED_PAGE_SIZE: int = 50
_SOON_DAYS: int = 7
_LIST_STATES: tuple[str, ...] = ("open", "waiting", "closed", "all")

# The empty-state sentence per filter (D-CS-B-41); a search has its own.
_EMPTY_TEXT: dict[str, str] = {
    "open": "No open cases — nothing needs working right now.",
    "waiting": "Nothing is waiting.",
    "closed": "No cases closed yet.",
    "all": 'No cases yet — "New case" opens one.',
}


def _badge(case: CaseDTO) -> str:
    """Return the case's ``CASE-nnnn`` badge."""
    return f"CASE-{case.case_number:04d}"


def _day(moment: date) -> str:
    """Render a date as ``dd Mon YYYY`` (the list's long date)."""
    return moment.strftime("%d %b %Y")


def subject_display(subject_key: str) -> str:
    """Label a finding's subject the way the Watch Desk monitor does.

    The part after the colon with underscores as spaces, capitalised
    (``saa:listed_equity`` → ``Listed equity``) — the idiom of
    ``_partials/watch_desk_monitor.html``; an unkeyed subject is shown as it is.

    Args:
        subject_key: The finding's subject key.

    Returns:
        The display label.
    """
    if ":" not in subject_key:
        return subject_key
    tail = subject_key.split(":", 1)[1]
    if not tail:
        return subject_key
    return tail.replace("_", " ").capitalize()


def _when_label(moment: datetime, *, now: datetime) -> str:
    """``today HH:MM`` / ``yesterday`` / ``dd Mon`` / ``dd Mon YYYY`` in UTC (D-CS-B-41)."""
    stamp = moment.astimezone(timezone.utc)
    today = now.astimezone(timezone.utc).date()
    if stamp.date() == today:
        return f"today {stamp:%H:%M}"
    if stamp.date() == today - timedelta(days=1):
        return "yesterday"
    if stamp.year == today.year:
        return stamp.strftime("%d %b")
    return stamp.strftime("%d %b %Y")


def _next_due(case: CaseDTO, point: CasePointDTO | None) -> tuple[str, date | None]:
    """The row's Next · due text and the date it reads — the earlier of the two (D-CS-M-2).

    The candidates are the case's own ``due_on`` and the next undone dated
    point; on a tie the point wins (it names what is due). Neither → ``("", None)``.
    """
    point_due = point.due_on if point is not None else None
    if (
        point is not None
        and point_due is not None
        and (case.due_on is None or point_due <= case.due_on)
    ):
        return f"{point.text} · {point_due:%d %b}", point_due
    if case.due_on is not None:
        return f"Due {_day(case.due_on)}", case.due_on
    return "", None


def _threads_summary(threads: Sequence[CaseThreadDTO]) -> str:
    """``n · c confirmed`` / ``n · o open`` / ``n`` / ``—`` (D-CS-B-41)."""
    total = len(threads)
    if total == 0:
        return "—"
    confirmed = sum(1 for thread in threads if thread.status == "confirmed")
    if confirmed > 0:
        return f"{total} · {confirmed} confirmed"
    still_open = sum(1 for thread in threads if thread.status == "open")
    if still_open > 0:
        return f"{total} · {still_open} open"
    return str(total)


def _frozen_headline(frozen: CaseEntryDTO | None) -> str:
    """Read ``file.where_we_stand.headline`` off a frozen snapshot, by name.

    JSONB keeps no key order and an older snapshot may lack a level, so every
    level is read with ``dict.get``; anything absent or empty is ``""``.
    """
    if frozen is None:
        return ""
    file_block = (frozen.payload or {}).get("file")
    stand = file_block.get("where_we_stand") if isinstance(file_block, dict) else None
    headline = stand.get("headline") if isinstance(stand, dict) else None
    return headline if isinstance(headline, str) else ""


async def _project_row(
    db_session: AsyncSession,
    ctx: StandContext,
    case: CaseDTO,
    owner_names: dict[UUID, str],
    *,
    now: datetime,
) -> dict[str, Any]:
    """One list row: badge, title, href, api_href, state, framing, sub, stand, threads,
    next, next_soon, next_empty, wait_until, owner, updated (D-CS-B-32 / D-CS-B-41).

    Reads per row: the finding (a finding case's sub line), the headline (an
    open or waiting case, through :func:`headline_for` with
    :func:`subject_display`) or the newest frozen snapshot (a closed case),
    the threads and the next due point.
    """
    is_closed = case.state == "closed"

    sub_parts: list[str] = []
    if case.finding_id is not None:
        finding = await IreneFindingRepository(db_session).get(case.finding_id)
        sub_parts.append("From finding")
        if finding is not None:
            sub_parts.extend([subject_display(finding.subject_key), finding.band])
    else:
        sub_parts.append("Manual")
    if case.counterparty:
        sub_parts.append(f"counterparty {case.counterparty}")
    sub_parts.append(f"opened {_day(case.opened_at)}")
    if is_closed and case.closed_at is not None:
        sub_parts.append(f"closed {_day(case.closed_at)}")

    if is_closed:
        stand = _frozen_headline(await CaseRepository(db_session).latest_frozen(case.id))
    else:
        stand = await headline_for(ctx, case, subject_label=subject_display)

    threads = await CaseThreadRepository(db_session).list_for_case(case.id)
    point = await CasePointRepository(db_session).next_due(case.id)
    next_text, next_date = ("", None) if is_closed else _next_due(case, point)
    today = now.astimezone(timezone.utc).date()

    return {
        "badge": _badge(case),
        "title": case.title,
        "href": f"/cases/{case.id}",
        "api_href": f"/api/cases/{case.id}",
        "state": case.state,
        "framing": case.framing,
        "sub": " · ".join(sub_parts),
        "stand": stand or "—",
        "threads": _threads_summary(threads),
        "next": next_text,
        "next_soon": next_date is not None and next_date - today <= timedelta(days=_SOON_DAYS),
        "next_empty": "—" if is_closed else "no point yet",
        "wait_until": _day(case.wait_until) if case.wait_until is not None else "",
        "owner": _owner_label(case.owner_user_id, owner_names),
        "updated": _when_label(case.updated_at, now=now),
    }


# ---------------------------------------------------------------------------
# The list — Open · Waiting · Closed · All, Mine and search (ADR-0138 §12)
# ---------------------------------------------------------------------------


@router.get("/api/cases/list", response_class=HTMLResponse)
async def get_case_list(
    request: Request,
    state: str = "open",
    mine: bool = False,
    q: str = "",
    before: datetime | None = None,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """The list partial (D-CS-B-32). ``state`` outside the four values → 400; ``before``
    with any state but ``closed`` → 400; ``before`` renders the next Closed page as rows only
    (``cases_list_rows.html``).

    *Open* is the open cases by ``updated_at`` followed by the Waiting group;
    *Waiting* the waiting cases by ``wait_until``; *Closed* one page of closed
    cases, newest first; *All* every case by ``updated_at``. *Mine* filters on
    ``owner_user_id`` — never ``opened_by``. A non-blank ``q`` searches the
    working file (``search_working_file``) under the same filter, *Mine*
    applied here. The repository orders; the route never re-sorts. The counts
    in the head are the tenant's, whatever the filter.
    """
    if state not in _LIST_STATES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown filter {state!r}; expected one of {', '.join(_LIST_STATES)}.",
        )
    if before is not None and state != "closed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="before pages the Closed filter only.",
        )
    query = q.strip()
    owner = session.user_id if mine else None
    now = _now()
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        repo = CaseRepository(db_session)
        ctx = await StandContext.load(db_session, now=now)

        waiting_cases: list[CaseDTO] = []
        if query:
            if state == "open":
                cases = await repo.search_working_file(query, state="open")
                waiting_cases = await repo.search_working_file(query, state="waiting")
            else:
                cases = await repo.search_working_file(
                    query, state=None if state == "all" else state
                )
            if owner is not None:
                cases = [c for c in cases if c.owner_user_id == owner]
                waiting_cases = [c for c in waiting_cases if c.owner_user_id == owner]
        elif state == "open":
            cases = await repo.list_cases(state="open", owner_user_id=owner)
            waiting_cases = await repo.list_cases(state="waiting", owner_user_id=owner)
        elif state == "closed":
            cases = await repo.list_cases(
                state="closed", owner_user_id=owner, before=before, limit=_CLOSED_PAGE_SIZE
            )
        else:
            cases = await repo.list_cases(
                state=None if state == "all" else state, owner_user_id=owner
            )

        owner_names = await _resolve_owner_names(
            UserRepository(db_session), (c.owner_user_id for c in [*cases, *waiting_cases])
        )
        rows = [await _project_row(db_session, ctx, c, owner_names, now=now) for c in cases]
        waiting = [
            await _project_row(db_session, ctx, c, owner_names, now=now) for c in waiting_cases
        ]
        counts = await repo.count_by_state() if before is None else {}

    older_href: str | None = None
    if state == "closed" and not query and len(cases) == _CLOSED_PAGE_SIZE:
        last_closed = cases[-1].closed_at
        if last_closed is not None:
            params = {"state": "closed", "before": last_closed.isoformat()}
            if mine:
                params["mine"] = "1"
            older_href = f"/api/cases/list?{urlencode(params)}"

    if before is not None:
        return _render(
            request,
            "_partials/cases_list_rows.html",
            {"rows": rows, "older_href": older_href, "journal_href": JOURNAL_DEEP_LINK},
        )

    empty_text = f'No cases match "{query}".' if query else _EMPTY_TEXT[state]
    if mine:
        empty_text += " (Mine is on.)"
    return _render(
        request,
        "_partials/cases_list.html",
        {
            "state": state,
            "mine": mine,
            "q": query,
            "rows": rows,
            "waiting": waiting,
            "counts": counts,
            "older_href": older_href,
            "empty_text": empty_text,
            "journal_href": JOURNAL_DEEP_LINK,
        },
    )


# ---------------------------------------------------------------------------
# Manual creation — the "New case" form
# ---------------------------------------------------------------------------


@router.get("/api/cases/new", response_class=HTMLResponse)
async def get_new_case_form(
    request: Request,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Return the manual case-creation form (no DB access)."""
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/cases_new_form.html",
            {"csrf_token": session.csrf_token},
        ),
    )


@router.post("/api/cases/new", response_class=HTMLResponse)
async def create_case(
    request: Request,
    title: str = Form(""),
    description: str = Form(""),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Create a manually opened case and land the PM in its file (D-CS-B-33).

    The title is validated non-empty at the route (the repository does not
    enforce it); a blank title re-renders the form with an inline error and
    writes nothing. On success the case is created with exactly one
    ``opened`` entry (``actor='pm'``, ``actor_user_id`` = the session user),
    and the response carries ``HX-Redirect: /cases/{id}`` — the full-refresh
    idiom :func:`post_case_close` uses — so the first thing after "New case"
    is the file.
    """
    clean_title = title.strip()
    if not clean_title:
        # Validation failure: re-render the form carrying the inline error
        # and the attempted title. Returned 200 so HTMX swaps it into the
        # slot reliably (the swap is the whole point of the re-render).
        return cast(
            HTMLResponse,
            _templates(request).TemplateResponse(
                request,
                "_partials/cases_new_form.html",
                {
                    "csrf_token": session.csrf_token,
                    "error": "A title is required to open a case.",
                    "title": title,
                },
            ),
        )

    clean_description = description.strip() or None
    engine = _engine(request)
    now = _now()
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        repo = CaseRepository(db_session)
        created = await repo.create(
            title=clean_title,
            description=clean_description,
            opened_by=session.user_id,
            opened_actor="pm",
            now=now,
        )

    logger.info(
        "cases create: tenant=%s user=%s case=%s number=%s",
        session.tenant_id,
        session.user_id,
        created.id,
        created.case_number,
    )

    return HTMLResponse("", headers={"HX-Redirect": f"/cases/{created.id}"})


# ---------------------------------------------------------------------------
# The case file — the Section's sub-surface (the page move of C3a's detail view)
# ---------------------------------------------------------------------------

# Kind → human label for the timeline entries the detail view knows how to
# render. A ``pin`` defaults to "Pinned" and is refined to "Document pinned"
# for the ``document`` artifact class (below); C5/C6 refine it for theirs. Any
# kind still absent here falls through to a calm generic fallback in the
# template (humanised kind + timestamp), so a seeded row of an as-yet-unrendered
# kind never breaks the page.
_ENTRY_KIND_LABELS: dict[str, str] = {
    "opened": "Case opened",
    "note": "Note",
    "decision_record": "Decision record",
    "closed": "Case closed",
    "pin": "Pinned",
}


def _stamp(moment: datetime | None) -> str:
    """Render a moment in the Journal's ``%Y-%m-%d %H:%M`` date+time idiom.

    Mirrors ``_partials/watch_desk_journal.html`` so the case timeline,
    head and rail read in the same date+time idiom as the monitoring surface.
    ``None`` renders an em dash, as the Journal does for a missing stamp.
    """
    return moment.strftime("%Y-%m-%d %H:%M") if moment is not None else "—"


def _actor_chip(entry: CaseEntryDTO, owner_names: dict[UUID, str]) -> dict[str, str]:
    """Project an entry's actor into a chip ``{css, label}``.

    ``pm`` resolves to the acting user's display name (the Journal batch
    idiom), falling back to the ``PM`` label when no user id is attached;
    ``shirley`` and ``system`` render their fixed labels. ``css`` keys the
    mock's ``actor--{pm,shirley,system}`` styling.
    """
    actor = entry.actor
    if actor == "pm":
        label = (
            _owner_label(entry.actor_user_id, owner_names)
            if entry.actor_user_id is not None
            else "PM"
        )
    elif actor == "shirley":
        label = "Shirley"
    elif actor == "system":
        label = "System"
    else:  # An unknown actor degrades to its raw value rather than vanishing.
        label = actor
    return {"css": actor, "label": label}


def _materiality_lines(entries: Iterable[CaseEntryDTO]) -> list[str]:
    """Return the frozen materiality-at-opening lines, when present.

    Binding decision 2 (ADR-0107, C3a): the ``opened`` entry's payload MAY
    carry ``materiality_at_opening.lines`` — presentation strings frozen at
    case opening, which C4's open-from-finding endpoint writes via the
    coverage service. Returns the strings verbatim when present; ``[]`` when
    absent (today's manually opened cases carry an empty payload), so the
    template renders the block only when there is something to show.
    """
    for entry in entries:
        if entry.kind != "opened":
            continue
        block = (entry.payload or {}).get("materiality_at_opening")
        if isinstance(block, dict) and isinstance(block.get("lines"), list):
            return [str(line) for line in block["lines"]]
        return []
    return []


def _project_head(case: CaseDTO, owner_names: dict[UUID, str]) -> dict[str, Any]:
    """Project the case head: badge, title, state, opened/closed metadata.

    Also the owner (``owner_user_id`` — the hand-over changes it, ``opened_by``
    stays as history), the counterparty, the case's due date and, for a
    waiting case, the date it returns to open.
    """
    return {
        "badge": _badge(case),
        "title": case.title,
        "state": case.state,
        "opened_stamp": _stamp(case.opened_at),
        "opened_by": _owner_label(case.opened_by, owner_names),
        "closed_stamp": (_stamp(case.closed_at) if case.closed_at is not None else None),
        "closed_by": (
            _owner_label(case.closed_by, owner_names) if case.closed_by is not None else None
        ),
        "owner": _owner_label(case.owner_user_id, owner_names),
        "counterparty": case.counterparty,
        "due": _day(case.due_on) if case.due_on is not None else None,
        "wait_until": _day(case.wait_until) if case.wait_until is not None else None,
    }


def _project_origin(finding: IreneFindingDTO, entries: Iterable[CaseEntryDTO]) -> dict[str, Any]:
    """Project the read-only origin embed from the immutable finding.

    A record, not a control (ADR-0107 §3): band and resolution use the
    Watch Desk tag classes; the finding text and ``basis`` (materiality
    at finding time) come from the immutable payload; the "At case opening"
    lines come from the ``opened`` entry (binding decision 2). No options, no
    resolve affordances, no Watch Desk navigation.

    ``resolution_label`` is the Watch Desk's display label (ADR-0107, C4 rider 2)
    — so the fifth resolution reads "Opened case", not the ``|capitalize``
    default "Opened_case", and phrases identically to the Journal rows.
    """
    payload = finding.payload or {}
    return {
        "band": finding.band,
        "subject_key": finding.subject_key,
        "surfaced": (
            finding.created_at.strftime("%d %b %Y") if finding.created_at is not None else ""
        ),
        "trigger": payload.get("trigger", ""),
        "finding": payload.get("finding", ""),
        "basis": payload.get("basis", ""),
        "resolution": finding.resolution,
        "resolution_label": resolution_label(finding.resolution),
        "materiality_lines": _materiality_lines(entries),
    }


def _project_entry(
    entry: CaseEntryDTO,
    owner_names: dict[UUID, str],
    *,
    opened_text: str,
) -> dict[str, Any]:
    """Project one timeline entry per the mock's per-kind anatomy.

    ``opened`` carries the origin phrasing (materiality lives in the origin
    embed, not repeated here); ``note`` the note text; ``decision_record`` the
    two-key ``decision`` / ``rationale`` contract C3b's composer writes;
    ``closed`` the closing note C1 already writes; ``pin`` of artifact class
    ``document`` the pin anatomy (binding decision 4 — the comment, the
    download link and the size); ``pin`` of artifact class ``scenario_snapshot``
    the frozen Planning Desk snapshot (ADR-0107 C5 — the parameter chips, the
    four KPI pairs, the headroom count, the two feet, the stored query and the
    curation comment, all read verbatim from the payload); ``pin`` of artifact
    class ``consultation`` the curated Shirley excerpt (ADR-0107 C6 — the quoted
    text attributed to Shirley plus the PM's curation comment); ``pin`` of
    artifact class ``chart_snapshot`` the frozen Plotly figure (ADR-0114 — the
    caption, the stored spec passed through untouched, and the curation
    comment). A ``pin`` of any still-unrendered artifact class projects only its
    raw ``artifact`` value, so the template falls to the calm generic fallback;
    the payload is never leaked. Every other kind likewise carries no projected
    body.
    """
    payload = entry.payload or {}
    kind = entry.kind
    projected: dict[str, Any] = {
        "kind": kind,
        "kind_label": _ENTRY_KIND_LABELS.get(kind, kind.replace("_", " ").capitalize()),
        "when": _stamp(entry.created_at),
        "actor": _actor_chip(entry, owner_names),
    }
    if kind == "opened":
        projected["text"] = opened_text
    elif kind == "note":
        projected["text"] = payload.get("text", "")
    elif kind == "decision_record":
        projected["decision"] = payload.get("decision", "")
        projected["rationale"] = payload.get("rationale", "")
    elif kind == "closed":
        projected["closing_note"] = payload.get("closing_note", "")
    elif kind == "pin":
        artifact = payload.get("artifact")
        projected["artifact"] = artifact
        if artifact == "document":
            projected["kind_label"] = "Document pinned"
            projected["comment"] = payload.get("comment", "")
            projected["filename"] = payload.get("filename", "")
            projected["size_human"] = _human_size(int(payload.get("size_bytes") or 0))
            projected["download_href"] = (
                f"/api/cases/{entry.case_id}/attachments/{payload.get('attachment_id')}"
            )
        elif artifact == "scenario_snapshot":
            # The frozen Planning Desk snapshot, read verbatim (ADR-0107 C5):
            # chips + KPI pairs + headroom count + feet + the stored query, none
            # of it recomputed. The query is text, never an anchor (decision 2).
            snapshot = payload.get("snapshot") or {}
            projected["kind_label"] = "Scenario snapshot pinned"
            projected["comment"] = payload.get("comment", "")
            projected["chips"] = [
                {
                    "label": str(chip.get("label", "")),
                    "css_class": str(chip.get("css_class", "")),
                }
                for chip in (snapshot.get("chips") or [])
                if isinstance(chip, dict)
            ]
            projected["kpis"] = [
                kpi for kpi in (snapshot.get("kpis") or []) if isinstance(kpi, dict)
            ]
            projected["headroom_count"] = _headroom_row_count(snapshot.get("headroom"))
            projected["baseline_foot"] = snapshot.get("baseline_foot") or {}
            projected["scenario_foot"] = snapshot.get("scenario_foot") or {}
            projected["query"] = str(snapshot.get("query", ""))
        elif artifact == "consultation":
            # The curated Shirley excerpt (ADR-0107 C6): the quoted text, read
            # verbatim from the payload and attributed to Shirley in the
            # anatomy, plus the PM's curation comment. The words are Shirley's;
            # the actor chip stays ``pm`` (the curator, binding decision 3).
            projected["kind_label"] = "Consultation pinned"
            projected["comment"] = payload.get("comment", "")
            projected["excerpt"] = payload.get("excerpt", "")
        elif artifact == "chart_snapshot":
            # The frozen Plotly figure (ADR-0114). The spec is handed to the
            # template untouched — the projection labels, it never
            # reinterprets — and re-plotted verbatim: what the PM saw at pin
            # time, not a fresh query (the C5 snapshot discipline).
            projected["kind_label"] = "Chart snapshot pinned"
            projected["comment"] = payload.get("comment", "")
            projected["caption"] = payload.get("caption", "")
            projected["spec"] = payload.get("spec") or {}
    return projected


def _opened_text(case: CaseDTO, finding: IreneFindingDTO | None) -> str:
    """Return the ``opened`` entry's phrasing for the timeline.

    A from-finding case names its finding's subject; a manual case reads
    "Opened manually." Factored so the detail view and the composer refresh
    paths (which re-project the timeline) phrase it identically.
    """
    if case.finding_id is not None:
        subject = finding.subject_key if finding is not None else None
        return f"Opened from finding {subject}." if subject else "Opened from a finding."
    return "Opened manually."


async def _project_timeline(db_session: AsyncSession, case: CaseDTO) -> list[dict[str, Any]]:
    """Re-read and project a case's timeline for an OOB refresh.

    Loads the entries (and the originating finding, for the ``opened``
    phrasing), resolves actor names in one batch, and projects each entry
    exactly as :func:`load_case_file_context` does — the single source of the
    timeline shape the note / decision / pin composers refresh into
    ``#case-timeline``.
    """
    case_repo = CaseRepository(db_session)
    entries = await case_repo.list_entries(case.id)

    finding: IreneFindingDTO | None = None
    if case.finding_id is not None:
        finding = await IreneFindingRepository(db_session).get(case.finding_id)

    actor_ids: set[UUID] = {case.opened_by}
    if case.closed_by is not None:
        actor_ids.add(case.closed_by)
    actor_ids.update(e.actor_user_id for e in entries if e.actor_user_id is not None)
    owner_names = await _resolve_owner_names(UserRepository(db_session), actor_ids)
    opened_text = _opened_text(case, finding)
    return [_project_entry(entry, owner_names, opened_text=opened_text) for entry in entries]


async def load_case_file_context(
    db_session: AsyncSession, case: CaseDTO, session: SessionDTO
) -> dict[str, Any]:
    """The sub-surface context — what ``case_detail_view`` built, as one function the
    partial route renders and tests exercise: head (badge, title, state, opened/closed
    stamps and names, owner, counterparty, due, wait_until), is_open, origin / description,
    entries, linked_finding, attachments_count / attachments_cap / show_attachments,
    journal_href, capture_scenario_href, consult_shirley_href, csrf_token, case_id.

    Loads the case's append-only timeline, its originating finding through the
    read-only finding lookup when it has one, the actor and owner names in one
    batch, and the attachments count. The actions — and with them the composer
    slot, "Capture scenario" and "Consult Shirley" — are offered on an *open*
    case only (``is_open``), so the UI never offers what the write routes'
    ``state == "open"`` gate refuses; a waiting case reads like a closed one
    until P-CS-3c re-gates the composers (D-CS-B-34).

    Args:
        db_session: A tenant-scoped session.
        case: The case, already read (and 404-checked) by the caller.
        session: The PM's session (for the CSRF token).

    Returns:
        The template context of ``_partials/cases_file.html``.
    """
    entries = await CaseRepository(db_session).list_entries(case.id)

    finding: IreneFindingDTO | None = None
    if case.finding_id is not None:
        finding = await IreneFindingRepository(db_session).get(case.finding_id)

    actor_ids: set[UUID] = {case.opened_by, case.owner_user_id}
    if case.closed_by is not None:
        actor_ids.add(case.closed_by)
    actor_ids.update(e.actor_user_id for e in entries if e.actor_user_id is not None)
    owner_names = await _resolve_owner_names(UserRepository(db_session), actor_ids)
    attachments_count = await CaseAttachmentRepository(db_session).count_for_case(case.id)

    head = _project_head(case, owner_names)
    origin = _project_origin(finding, entries) if finding is not None else None
    opened_text = _opened_text(case, finding)
    is_open = case.state == "open"
    is_closed = case.state == "closed"
    cfg = get_config()

    return {
        "csrf_token": session.csrf_token,
        "case_id": str(case.id),
        "head": head,
        "is_open": is_open,
        "origin": origin,
        # The manual-case substitute for the origin embed: rendered only when
        # there is no finding to embed.
        "description": case.description if origin is None else None,
        "entries": [
            _project_entry(entry, owner_names, opened_text=opened_text) for entry in entries
        ],
        # The origin finding fact (text only), when present. No Investments
        # row (binding decision 1).
        "linked_finding": (
            {"subject_key": origin["subject_key"], "band": origin["band"]}
            if origin is not None
            else None
        ),
        # Attachments · N of {cap}: shown when N > 0 or the case is open.
        "attachments_count": attachments_count,
        "attachments_cap": cfg.case_attachment_max_count,
        "show_attachments": attachments_count > 0 or is_open,
        # Journal deep link — closed cases only (a closed case is in the
        # Journal as a closed-case row; an open or waiting one is not).
        # Binding decision 2.
        "journal_href": JOURNAL_DEEP_LINK if is_closed else None,
        # "Capture scenario" (ADR-0107 C5) — open cases only: a plain link to the
        # Planning Desk with this case as the capture marker, so the Desk arrives
        # "capturing for" it. The C4 cross-module URL idiom — one imported target,
        # not a duplicated path string.
        "capture_scenario_href": (f"{PLANNING_DESK_AREA_URL}?case={case.id}" if is_open else None),
        # "Consult Shirley" (ADR-0107 C6) — open cases only: a plain link to the
        # assistants surface carrying this case as the ``?case=`` brief marker,
        # so Shirley arrives "consulting for" it. The in-place hand-off is
        # P-CS-5a's (D-CS-B-40).
        "consult_shirley_href": (f"{CONSULT_SHIRLEY_URL}?case={case.id}" if is_open else None),
    }


@router.get("/api/cases/{case_id}", response_class=HTMLResponse)
async def get_case_file(
    request: Request, case_id: UUID, session: SessionDTO = Depends(require_session)
) -> HTMLResponse:
    """The sub-surface partial (D-CS-B-28); an unknown or foreign id → 404.

    Swapped into the Section's ``#cs-host`` by a list row, or by the host's
    own load when the page was opened on ``/cases/{case_id}``. Unknown or
    foreign-tenant ids resolve to ``None`` under RLS and are mapped to 404 —
    the entity-detail idiom (``web/routes/investments.py``), which deliberately
    does not disclose whether the id exists in another tenant.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        case = await CaseRepository(db_session).get(case_id)
        if case is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Case not found.",
            )
        context = await load_case_file_context(db_session, case, session)
    return _render(request, "_partials/cases_file.html", context)


# ---------------------------------------------------------------------------
# Composers + writes (C3b) — everything that mutates a case from the detail
# view. Every write endpoint takes ``verify_csrf`` and gates on the open state
# at the route (the repository already raises on a closed case; the route gate
# exists so the UI never offers what the layer below refuses, and so a
# defensive request against a closed case degrades to a calm error, logged
# because the UI should not have offered it).
# ---------------------------------------------------------------------------

# The closed-case calm error, shared by every composer's closed-state branch.
_CLOSED_CASE_ERROR = "This case is closed — closed cases are read-only and cannot be changed."

_COMPOSER_TEMPLATES: dict[str, str] = {
    "note": "_partials/cases_composer_note.html",
    "decision": "_partials/cases_composer_decision.html",
    "pin": "_partials/cases_composer_pin.html",
    "close": "_partials/cases_composer_close.html",
}


def _render(
    request: Request,
    template: str,
    context: dict[str, Any],
    *,
    status_code: int = 200,
) -> HTMLResponse:
    """Render ``template`` with ``context`` as an :class:`HTMLResponse`."""
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(request, template, context, status_code=status_code),
    )


def _closed_composer(
    request: Request, session: SessionDTO, case_id: UUID, template: str
) -> HTMLResponse:
    """Re-render a composer carrying the calm closed-case error."""
    return _render(
        request,
        template,
        {
            "case_id": str(case_id),
            "csrf_token": session.csrf_token,
            "error": _CLOSED_CASE_ERROR,
        },
    )


@router.get("/api/cases/{case_id}/composer/{action}", response_class=HTMLResponse)
async def get_case_composer(
    request: Request,
    case_id: UUID,
    action: str,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Return one composer form into ``#case-composer-slot`` (the C2 slot idiom).

    ``action='cancel'`` clears the slot (an empty body). The four real actions
    render their form — DB-free, exactly like ``GET /api/cases/new`` — because
    the gate that matters lives on the write endpoints the composers post to.
    An unknown action is a 404.
    """
    if action == "cancel":
        return HTMLResponse("")
    template = _COMPOSER_TEMPLATES.get(action)
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown composer.")
    context: dict[str, Any] = {
        "case_id": str(case_id),
        "csrf_token": session.csrf_token,
    }
    if action == "pin":
        context["caps_hint"] = _caps_hint(get_config())
    return _render(request, template, context)


def _timeline_refresh(request: Request, entries: list[dict[str, Any]]) -> HTMLResponse:
    """Return the OOB timeline fragment that clears the composer slot.

    The response is only the timeline (carrying ``hx-swap-oob``), so HTMX
    swaps it by id and the main ``#case-composer-slot`` target receives the
    empty remainder — the note / decision success shape.
    """
    return _render(
        request,
        "_partials/cases_detail_timeline.html",
        {"entries": entries, "timeline_oob": True},
    )


@router.post("/api/cases/{case_id}/note", response_class=HTMLResponse)
async def post_case_note(
    request: Request,
    case_id: UUID,
    text: str = Form(""),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Append a ``note`` entry, then OOB-refresh the timeline (Step 3).

    Route-level open-state gate first (missing → 404; closed → calm error,
    logged); then the text is stripped and required. On success one ``note``
    entry is appended (``actor='pm'``, ``actor_user_id`` = the session user)
    and the timeline refreshes out of band while the composer slot clears.
    """
    clean = text.strip()
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        case = await CaseRepository(db).get(case_id)
        if case is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Case not found.",
            )
        if case.state != "open":
            logger.warning(
                "cases note: closed-case write blocked tenant=%s user=%s case=%s",
                session.tenant_id,
                session.user_id,
                case_id,
            )
            return _closed_composer(request, session, case_id, _COMPOSER_TEMPLATES["note"])
        if not clean:
            return _render(
                request,
                _COMPOSER_TEMPLATES["note"],
                {
                    "case_id": str(case_id),
                    "csrf_token": session.csrf_token,
                    "error": "A note cannot be empty.",
                    "text": text,
                },
            )
        await CaseRepository(db).append_entry(
            case_id,
            kind="note",
            actor="pm",
            actor_user_id=session.user_id,
            payload={"text": clean},
            now=_now(),
        )
        entries = await _project_timeline(db, case)

    logger.info(
        "cases note: tenant=%s user=%s case=%s",
        session.tenant_id,
        session.user_id,
        case_id,
    )
    return _timeline_refresh(request, entries)


@router.post("/api/cases/{case_id}/decision", response_class=HTMLResponse)
async def post_case_decision(
    request: Request,
    case_id: UUID,
    decision: str = Form(""),
    rationale: str = Form(""),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Append a ``decision_record`` entry, then OOB-refresh the timeline.

    Both fields are required (each missing → inline error, nothing written) —
    the ``decision`` / ``rationale`` pair is the C3a rendering contract. Same
    open-state gate and refresh shape as :func:`post_case_note`.
    """
    clean_decision = decision.strip()
    clean_rationale = rationale.strip()
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        case = await CaseRepository(db).get(case_id)
        if case is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Case not found.",
            )
        if case.state != "open":
            logger.warning(
                "cases decision: closed-case write blocked tenant=%s user=%s case=%s",
                session.tenant_id,
                session.user_id,
                case_id,
            )
            return _closed_composer(request, session, case_id, _COMPOSER_TEMPLATES["decision"])
        if not clean_decision or not clean_rationale:
            return _render(
                request,
                _COMPOSER_TEMPLATES["decision"],
                {
                    "case_id": str(case_id),
                    "csrf_token": session.csrf_token,
                    "error": "Both a decision and its rationale are required.",
                    "decision": decision,
                    "rationale": rationale,
                },
            )
        await CaseRepository(db).append_entry(
            case_id,
            kind="decision_record",
            actor="pm",
            actor_user_id=session.user_id,
            payload={
                "decision": clean_decision,
                "rationale": clean_rationale,
            },
            now=_now(),
        )
        entries = await _project_timeline(db, case)

    logger.info(
        "cases decision: tenant=%s user=%s case=%s",
        session.tenant_id,
        session.user_id,
        case_id,
    )
    return _timeline_refresh(request, entries)


@router.post("/api/cases/{case_id}/pin-document", response_class=HTMLResponse)
async def post_case_pin_document(
    request: Request,
    case_id: UUID,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Pin a document: validate, store the attachment, append the pin (Step 4).

    Gates, in order, each with its own inline error: case exists (404) → case
    open → comment non-empty → file present → extension AND declared content
    type in the whitelist → size ≤ cap (the real byte length, never the header)
    → count < cap. The size gate reads the body in bounded 64 KiB chunks and
    stops at cap + 1 bytes, so an oversized upload is rejected without ever
    being fully buffered as one in-memory object (single-instance memory
    safety). Then, in **one** tenant-context transaction, the attachment is
    created and the pin entry appended together — if the append fails the
    attachment rolls back too, so no orphaned bytes survive. On success the
    timeline and the rail's attachments count refresh out of band.

    The multipart body is read through ``request.state.form`` (cached by
    ``verify_csrf``), the codebase's multipart idiom (``data_import.py``) —
    reading it a second time via ``request.form()`` is avoided.
    """
    cfg = get_config()
    engine = _engine(request)

    def _pin_error(message: str, *, comment: str = "", status_code: int = 200) -> HTMLResponse:
        return _render(
            request,
            _COMPOSER_TEMPLATES["pin"],
            {
                "case_id": str(case_id),
                "csrf_token": session.csrf_token,
                "caps_hint": _caps_hint(cfg),
                "error": message,
                "comment": comment,
            },
            status_code=status_code,
        )

    form = getattr(request.state, "form", None)
    if form is None:
        form = await request.form()
    comment_raw = form.get("comment")
    comment = str(comment_raw).strip() if comment_raw is not None else ""
    file_field = form.get("file")

    # Gates 1-2 + the count read (one tenant-context read).
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        case = await CaseRepository(db).get(case_id)
        if case is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Case not found.",
            )
        if case.state != "open":
            logger.warning(
                "cases pin: closed-case write blocked tenant=%s user=%s case=%s",
                session.tenant_id,
                session.user_id,
                case_id,
            )
            return _pin_error(_CLOSED_CASE_ERROR)
        current_count = await CaseAttachmentRepository(db).count_for_case(case_id)

    # Gate 3: comment non-empty. The file input cannot be preserved across a
    # re-render, so the copy asks the PM to reselect it.
    if not comment:
        return _pin_error(
            "A curation comment is required — say why the document is "
            "decision-relevant. Please reselect the file too; browsers cannot "
            "keep it across this error."
        )
    # Gate 4: file present.
    if not isinstance(file_field, UploadFile) or not file_field.filename:
        return _pin_error("Choose a file to pin.", comment=comment)
    # Gate 5: extension AND declared content type in the whitelist.
    sanitised_name = _sanitise_filename(file_field.filename)
    ext = _file_extension(sanitised_name)
    allowed = cfg.case_attachment_allowed_types
    accepted = ", ".join(sorted(e.upper() for e in allowed))
    if not sanitised_name or ext not in allowed:
        return _pin_error(
            f"That file type is not allowed. Accepted: {accepted}.",
            comment=comment,
        )
    declared_mime = (file_field.content_type or "").split(";")[0].strip().lower()
    if declared_mime not in allowed[ext]:
        return _pin_error(
            "The file's declared content type does not match its extension "
            f"— accepted types are {accepted}.",
            comment=comment,
        )
    # Gate 6: size ≤ cap — read the bytes in bounded 64 KiB chunks, stopping
    # at cap + 1 so an oversized body is rejected without ever being fully
    # buffered as one in-memory object (single-instance memory safety).
    max_bytes = cfg.case_attachment_max_bytes
    chunks: list[bytes] = []
    total = 0
    while total <= max_bytes:
        chunk = await file_field.read(min(_UPLOAD_CHUNK_BYTES, max_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > max_bytes:
        return _pin_error(
            f"The file exceeds the {max_bytes // (1024 * 1024)} MB per-file limit.",
            comment=comment,
        )
    content = b"".join(chunks)
    if len(content) == 0:
        return _pin_error("The file is empty.", comment=comment)
    # Gate 7: count < cap.
    if current_count >= cfg.case_attachment_max_count:
        return _pin_error(
            f"This case already has the maximum of {cfg.case_attachment_max_count} attachments.",
            comment=comment,
        )

    # Write: one transaction — attachment + pin entry together (atomic).
    sha256 = hashlib.sha256(content).hexdigest()
    now = _now()
    try:
        async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
            attachment = await CaseAttachmentRepository(db).create(
                case_id,
                filename=sanitised_name,
                mime_type=declared_mime,
                size_bytes=len(content),
                sha256=sha256,
                content=content,
                uploaded_by=session.user_id,
                now=now,
            )
            await CaseRepository(db).append_entry(
                case_id,
                kind="pin",
                actor="pm",
                actor_user_id=session.user_id,
                payload={
                    "artifact": "document",
                    "comment": comment,
                    "attachment_id": str(attachment.id),
                    "filename": attachment.filename,
                    "mime_type": attachment.mime_type,
                    "size_bytes": attachment.size_bytes,
                },
                now=now,
            )
            entries = await _project_timeline(db, case)
            new_count = await CaseAttachmentRepository(db).count_for_case(case_id)
    except HTTPException:
        raise
    except (CaseClosedError, CaseStateInvalid):
        # Raced to closed (or the case vanished) between gate and write.
        logger.warning(
            "cases pin: case no longer writable under the write case=%s",
            case_id,
        )
        return _pin_error(_CLOSED_CASE_ERROR)
    except Exception:
        # The pin entry append failed after the attachment insert: the
        # tenant_context rolled the whole transaction back, so the attachment
        # did not survive. Surface a calm error rather than a 500 page.
        logger.exception(
            "cases pin: atomic write failed, attachment rolled back case=%s",
            case_id,
        )
        return _pin_error(
            "The document could not be pinned. Please reselect the file and try again.",
            comment=comment,
            status_code=500,
        )

    logger.info(
        "cases pin: tenant=%s user=%s case=%s attachment=%s bytes=%d",
        session.tenant_id,
        session.user_id,
        case_id,
        attachment.id,
        len(content),
    )
    return _render(
        request,
        "_partials/cases_pin_result.html",
        {
            "entries": entries,
            "attachments_count": new_count,
            "attachments_cap": cfg.case_attachment_max_count,
        },
    )


@router.get("/api/cases/{case_id}/attachments/{attachment_id}")
async def download_case_attachment(
    request: Request,
    case_id: UUID,
    attachment_id: UUID,
    session: SessionDTO = Depends(require_session),
) -> Response:
    """Stream one pinned document back to the browser (Step 4).

    RLS scopes the read to the active tenant (a foreign-tenant attachment is
    simply not found); the attachment must also belong to ``case_id`` — a
    mismatch is a 404 with no disclosure. Reading is never gated on state, so
    downloads work on closed cases. The stored ``mime_type`` is the media type;
    the filename is sanitised for the ``Content-Disposition`` header only (the
    stored value is left untouched in the DB).
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        result = await CaseAttachmentRepository(db).get_with_content(attachment_id)

    if result is None or result[0].case_id != case_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Attachment not found.",
        )
    meta, content = result
    header_name = _sanitise_header_filename(meta.filename)
    return Response(
        content=content,
        media_type=meta.mime_type,
        headers={"Content-Disposition": f'attachment; filename="{header_name}"'},
    )


@router.post("/api/cases/{case_id}/close", response_class=HTMLResponse)
async def post_case_close(
    request: Request,
    case_id: UUID,
    closing_note: str = Form(""),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Close a case, then trigger a full-page refresh (Step 5).

    The closing note is stripped and required (missing → inline error);
    missing case → 404; an already-closed case → the calm error idiom. On
    success :meth:`CaseRepository.close` writes the state transition and the
    ``closed`` entry atomically (C1), and the response carries an
    ``HX-Redirect`` back to the detail URL so the browser re-renders the whole
    page closed — state chip, closed metadata, closing note, no actions, no
    composers. **No Journal write and no Journal link** — the Journal's
    closed-case projection and the deep link are C4.
    """
    clean_note = closing_note.strip()
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        case = await CaseRepository(db).get(case_id)
        if case is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Case not found.",
            )
        if case.state != "open":
            logger.warning(
                "cases close: already-closed close blocked tenant=%s user=%s case=%s",
                session.tenant_id,
                session.user_id,
                case_id,
            )
            return _closed_composer(request, session, case_id, _COMPOSER_TEMPLATES["close"])
        if not clean_note:
            return _render(
                request,
                _COMPOSER_TEMPLATES["close"],
                {
                    "case_id": str(case_id),
                    "csrf_token": session.csrf_token,
                    "error": "A closing note is required to close a case.",
                    "closing_note": closing_note,
                },
            )
        await CaseRepository(db).close(
            case_id,
            closed_by=session.user_id,
            closing_note=clean_note,
            now=_now(),
        )

    logger.info(
        "cases close: tenant=%s user=%s case=%s",
        session.tenant_id,
        session.user_id,
        case_id,
    )
    # Full-page refresh: the whole page changes (head chip, closed metadata,
    # actions and composers vanish). HX-Redirect is the codebase's full
    # client-side navigation idiom (web/routes/investments.py).
    return HTMLResponse("", headers={"HX-Redirect": f"/cases/{case_id}"})
