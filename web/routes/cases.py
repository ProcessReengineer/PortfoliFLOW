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
embed or the description, and the record. P-CS-3b makes the working file
editable in place: The question (prose, counterparty, due date, framing), the
investment links and documents, and Where we stand composed live (or *At
close* from the frozen snapshot). P-CS-3b2 adds the title (the crumb follows),
Threads and Figures — both hidden while empty and revealed from the outline's
``+``; a ``book`` figure is read live through ``book_values_for``. P-CS-3c
adds Conversations, Open points (*Questions to ask* on a task) with *Create
ticket →* into the Transactions composer, and the Decision draft — all three
hidden when empty. P-CS-3c2 takes every milestone from the file through the one
milestone path, with the file frozen as it stands (Where we stand and the
``book`` values injected, :func:`_freeze_args`): *Record decision* under the
draft, *Wait until…*, *Resume now*, *Hand over to…* and *Close case…* (lift,
then close, on a waiting case). The Record block lists the milestones, the
system lines and the legacy entries, each milestone paired with its frozen
snapshot, which opens read-only under *File as …*. The note and decision
composers are retired. P-CS-4a retires the pin composer — Keep (ADR-0138 §8)
writes kept rows from the dock and the Planning Desk — and renders the kept
rows inside their threads, the file-level ones after them; the legacy pin rows
keep their anatomy in the Record (D-CS-M-7), from the same class partial
(``_partials/cases_artifact.html``). P-CS-4b renders the six kept classes —
``document_excerpt`` (the quoted passage and its page) and
``generated_document`` (a memo, the one class edited in place) besides the
four — with Remove (``removed_at``) on a mutable case, addresses every
attached document through a ``document`` kept row, and renders the frozen
file's kept rows with the same anatomy. The attachment download pipeline and
its caps are unchanged.

The module never mutates an Irene finding and never writes a Journal row.
The file *reads* a case's originating finding through the finding
repository's read-only ``get``; resolving a finding as ``opened_case`` lives
on the Watch Desk. A closed case carries the Journal deep link (ADR-0107 C4,
binding decision 2) on its list row and in its facts.

Pin payload contract (binding decision 4) — legacy rows, read-only
-------------------------------------------------------------------
No route writes a ``pin`` entry since P-CS-4a (ADR-0138 §9; D-CS-B-1 closed).
The kept classes carry the same contracts minus ``comment`` in
``case_kept.payload`` (D-CS-B-85), and both render through
:func:`_project_kept_payload` and ``_partials/cases_artifact.html``.

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
fidelity (ADR-0042 §4). The pin's write lived in ``web/routes/chat.py``;
Keep writes the kept ``chart_snapshot`` there now.

Attachments
-----------
Every attach writes the attachment **and** a kept row in one transaction
(ADR-0138 §5, D-CS-B-96): a ``document`` row in the file, or with a passage a
``document_excerpt`` row in the file or a live thread (D-CS-B-97) — still no
record entry (D-CS-B-47). An attachment with no kept row is an upload in
progress (the ADR's object model); the question block lists the file-level
``document`` rows, not the attachments table. A passage may also be kept from
an attachment already on the case. No standalone manager, no delete, no
re-upload (ADR-0107 §7, the DMS boundary).

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
* ``GET  /api/cases/{case_id}/sections/{name}`` — one block of the file alone
  (``question`` · ``stand`` · ``threads`` · ``figures`` · ``conversations`` ·
  ``points`` · ``decision`` · ``record``; ``reveal=1`` for the outline's ``+``;
  ``oob=1`` appends the OOB bundle, so the outline follows a keep, D-CS-B-104).
* ``POST /api/cases/{case_id}/question`` — the question's autosave
  (D-CS-B-44): the title, prose, counterparty, due date and framing, last
  write wins with a note (D-CS-B-45); answers the OOB bundle, ``rerender=1``
  the block too.
* ``POST /api/cases/{case_id}/link-investment`` · ``…/unlink-investment`` —
  the case's investment links; answer Where we stand and the bundle.
* ``POST /api/cases/{case_id}/attachments`` — attach a document, or keep a
  passage of one already attached, as a ``document`` / ``document_excerpt``
  kept row without a record entry (D-CS-B-96, D-CS-B-97); answers the
  question block, the Threads block out of band and the bundle.
* ``POST /api/cases/{case_id}/kept/{kept_id}/remove`` — a kept row leaves the
  file (``removed_at``, D-CS-B-99). ``GET …/kept/{kept_id}/generated/edit`` ·
  ``POST …/kept/{kept_id}/generated`` — the in-place edit of a
  ``generated_document``, the one kept class whose payload is mutable
  (ADR-0138 §9, D-CS-B-98).
* ``POST /api/cases/{case_id}/threads`` · ``…/threads/{thread_id}`` ·
  ``…/threads/{thread_id}/remove`` — add, update (title · prose · status ·
  conclusion, partial by presence) and remove a thread (D-CS-B-53).
* ``POST /api/cases/{case_id}/figures`` · ``…/figures/{figure_id}`` ·
  ``…/figures/{figure_id}/remove`` — add a typed or a ``book`` figure, update
  one (a ``book`` row its label and note only) and delete one (D-CS-B-54).
* ``POST /api/cases/{case_id}/conversations`` · ``…/conversations/{id}`` ·
  ``…/conversations/{id}/remove`` — add, update (by presence) and delete a
  conversation (D-CS-B-62).
* ``POST /api/cases/{case_id}/points`` · ``…/points/{id}`` ·
  ``…/points/{id}/remove`` — add, update (text · owner · due; ``done`` on the
  re-render path) and delete an open point while no ticket is linked
  (D-CS-B-63).
* ``POST /api/cases/{case_id}/decision-draft`` — the decision draft's one
  upsert: options, the chosen one, rationale, decided by and at; ``add_option``
  / ``remove_option`` as structural intents (D-CS-B-66).
* ``POST /api/cases/{case_id}/record-decision`` — record the draft: the
  ``decision_record`` milestone with the file frozen; answers the Decision
  block, the Record block and the bundle (D-CS-B-70).
* ``POST /api/cases/{case_id}/wait`` · ``…/resume`` · ``…/hand-over`` ·
  ``…/close`` — the state and owner milestones, each with the file frozen;
  each answers ``HX-Redirect: /cases/{id}`` (D-CS-B-70).
* ``GET  /api/cases/{case_id}/record/{entry_id}/file`` — the file as a
  milestone froze it, read-only, into the record row's slot (D-CS-B-74).
* The composer and download routes below. Everything writes on an open
  **or waiting** case (D-CS-B-76); a closed case is read-only.

The OOB bundle (``_partials/cases_file_oob.html``) is what every save of the
working file answers with: the crumb ``#cases-title``, ``#cs-saved``,
``#cs-facts``, ``#cs-outline``, the section stamps and ``#cs-note-<section>``.
A text autosave swaps nothing in place (``hx-swap="none"``) and receives the
bundle alone; a structural change (title, framing, link, attach, a thread's
title or status, a figure's source, a conversation's date, channel or
duration, a point's done box, a decision option added, removed or chosen, any
add or remove) re-renders its block, followed by the bundle.

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
import re
from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, cast
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from starlette.datastructures import UploadFile

from core.config import get_config
from core.exceptions import (
    CaseClosedError,
    CaseKeptImmutable,
    CasePointLinked,
    CaseStateInvalid,
    CaseWorkingFileInvalid,
)
from core.repositories._session import tenant_context
from core.repositories.case_attachment_repository import (
    CaseAttachmentDTO,
    CaseAttachmentRepository,
)
from core.repositories.case_repository import (
    CaseDTO,
    CaseEntryDTO,
    CaseRepository,
)
from core.repositories.case_working_file_repository import (
    CaseConversationDTO,
    CaseConversationRepository,
    CaseDecisionDTO,
    CaseDecisionRepository,
    CaseFigureDTO,
    CaseFigureRepository,
    CaseInvestmentRepository,
    CaseKeptDTO,
    CaseKeptRepository,
    CasePointDTO,
    CasePointRepository,
    CaseThreadDTO,
    CaseThreadRepository,
)
from core.repositories.investment_repository import InvestmentRepository
from core.repositories.irene_finding_repository import (
    IreneFindingDTO,
    IreneFindingRepository,
)
from core.repositories.trade_ticket_repository import TradeTicketRepository
from core.repositories.user_repository import UserRepository
from services.auth.session import SessionDTO
from services.cases.stand import (
    StandContext,
    WhereWeStand,
    book_values_for,
    compose,
    headline_for,
    to_snapshot,
)
from services.money_format import format_money_compact
from services.transactions.constants import STATUS_BOOKED, STATUS_CANCELLED
from services.watch_desk.family_status import FIGURE_UNIT_DAYS, FIGURE_UNIT_RATIO
from web.auth import require_session, verify_csrf
from web.case_keep import consultation_caption, live_thread
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
    """Build the attach composer's visible caps line from configuration."""
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


def _parse_uuid(raw: Any) -> UUID | None:
    """A UUID from a payload's string (or a UUID); ``None`` when absent or unreadable."""
    if raw is None or raw == "":
        return None
    try:
        return raw if isinstance(raw, UUID) else UUID(str(raw))
    except ValueError:
        return None


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
    "handed_over": "Handed over",
    "waiting_set": "Waiting set",
    "waiting_lifted": "Waiting lifted",
    "system_line": "System line",
}

#: A milestone kind → its word in *File as …* and in the frozen head (D-CS-B-73, D-CS-B-78).
_FILE_AS_WORDS: dict[str, str] = {
    "opened": "opened",
    "handed_over": "handed over",
    "waiting_set": "waiting",
    "waiting_lifted": "lifted",
    "decision_record": "decided",
    "closed": "closed",
}

#: How a lift reads in the record, by its reason; ``event`` names the waking line (D-CS-B-73).
_LIFT_TEXTS: dict[str, str] = {
    "manual": "Waiting lifted — by hand",
    "date": "Waiting lifted — on the date",
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


#: A legacy pin's record label per artifact class (ADR-0107 C3b–C6, ADR-0114).
_PIN_KIND_LABELS: dict[str, str] = {
    "document": "Document pinned",
    "scenario_snapshot": "Scenario snapshot pinned",
    "consultation": "Consultation pinned",
    "chart_snapshot": "Chart snapshot pinned",
}


def _attachment_href(case_id: UUID, attachment_id: Any) -> str:
    """The download endpoint of one attachment of ``case_id``."""
    return f"/api/cases/{case_id}/attachments/{attachment_id}"


def _in_app_href(raw: Any) -> str | None:
    """A generated document's source link, only when it is an in-app path (D-CS-B-98).

    A path starting with one ``/`` — a case, an attachment download, an
    investment. A protocol-relative ``//host`` (or ``/\\host``, which browsers
    read the same), every absolute URL and any href carrying a control
    character render as the plain label — a browser drops tabs and newlines
    from a URL, so ``/<tab>/host`` would otherwise reach another host.
    """
    if not isinstance(raw, str) or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return None
    if not raw.startswith("/") or raw.startswith(("//", "/\\")):
        return None
    return raw


def _paragraphs(body: Any) -> list[str]:
    """A plain-text body as paragraphs, split on blank lines (D-CS-B-98) — no Markdown."""
    text = str(body or "").replace("\r\n", "\n")
    return [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]


def _project_kept_payload(
    artifact: str,
    payload: dict[str, Any],
    *,
    case_id: UUID,
    attachment_id: UUID | None = None,
) -> dict[str, Any]:
    """Project one artefact class's payload for ``_partials/cases_artifact.html``.

    The one projection of the six classes, shared by a legacy pin in the
    Record and a kept row in the file (D-CS-B-90): ``document`` — the
    filename linking to the download endpoint and the human-legible size;
    ``scenario_snapshot`` — the frozen Planning Desk snapshot (ADR-0107 C5:
    the parameter chips, the four KPI pairs, the headroom *count*, the two
    feet and the stored query, all read verbatim, the query text and never a
    link); ``consultation`` — the Shirley excerpt (ADR-0107 C6);
    ``chart_snapshot`` — the caption and the frozen Plotly spec, passed
    through untouched (ADR-0114); ``document_excerpt`` — the filename, the
    passage, the page and the download link (D-CS-B-97); and
    ``generated_document`` — the title, the body as paragraphs and the sources,
    a source linked only when it is an in-app path (D-CS-B-98). Any other
    class projects nothing, so the partial falls to its calm one-liner and the
    payload never leaks.

    Args:
        artifact: The artefact class.
        payload: The opaque payload (a pin's entry payload or a kept row's).
        case_id: The case, for a document's download link.
        attachment_id: The kept row's attachment — an excerpt's payload
            carries none, and a document's falls back to it.

    Returns:
        The class's template keys.
    """
    if artifact == "document":
        return {
            "filename": payload.get("filename", ""),
            "size_human": _human_size(int(payload.get("size_bytes") or 0)),
            "download_href": _attachment_href(
                case_id, payload.get("attachment_id") or attachment_id
            ),
        }
    if artifact == "document_excerpt":
        page = payload.get("page")
        return {
            "filename": str(payload.get("filename") or ""),
            "passage": str(payload.get("passage") or ""),
            "page": page if isinstance(page, int) and not isinstance(page, bool) else None,
            "download_href": _attachment_href(case_id, attachment_id),
        }
    if artifact == "generated_document":
        sources = payload.get("sources")
        return {
            "title": str(payload.get("title") or ""),
            "paragraphs": _paragraphs(payload.get("body")),
            "sources": [
                {"label": str(source.get("label") or ""), "href": _in_app_href(source.get("href"))}
                for source in (sources if isinstance(sources, list) else [])
                if isinstance(source, dict)
            ],
        }
    if artifact == "scenario_snapshot":
        snapshot = payload.get("snapshot") or {}
        return {
            "chips": [
                {"label": str(chip.get("label", "")), "css_class": str(chip.get("css_class", ""))}
                for chip in (snapshot.get("chips") or [])
                if isinstance(chip, dict)
            ],
            "kpis": [kpi for kpi in (snapshot.get("kpis") or []) if isinstance(kpi, dict)],
            "headroom_count": _headroom_row_count(snapshot.get("headroom")),
            "baseline_foot": snapshot.get("baseline_foot") or {},
            "scenario_foot": snapshot.get("scenario_foot") or {},
            "query": str(snapshot.get("query", "")),
        }
    if artifact == "consultation":
        return {"excerpt": payload.get("excerpt", "")}
    if artifact == "chart_snapshot":
        return {"caption": payload.get("caption", ""), "spec": payload.get("spec") or {}}
    return {}


def _project_entry(
    entry: CaseEntryDTO,
    owner_names: dict[UUID, str],
    *,
    opened_text: str,
    file_href: str | None = None,
    waking_text: str | None = None,
) -> dict[str, Any]:
    """Project one timeline entry per the mock's per-kind anatomy.

    ``opened`` carries the origin phrasing (materiality lives in the origin
    embed, not repeated here); ``note`` the note text; ``decision_record`` the
    milestone (P-CS-1b — the payload names its ``decision_id``) as
    ``text`` *Decision recorded: {chosen} — {rationale}*, or, without a
    ``decision_id``, the legacy two-key ``decision`` / ``rationale`` contract
    C3b's composer wrote; ``handed_over`` *Handed over to {name}*, ``waiting_set``
    *Waiting until {date}* (each with its optional ``note``),
    ``waiting_lifted`` *Waiting lifted — by hand* / *— on the date* / *— woken
    by: {waking_text}* (the text of the system line that woke the case, looked
    up by the caller), ``system_line`` its ``payload.text`` verbatim (D-CS-B-20,
    D-CS-B-73); ``closed`` the closing note C1 already writes; ``pin`` of artifact class
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

    ``file_href`` is the milestone's *File as …* link, given by the caller
    when the milestone has a frozen pair (D-CS-B-6); an orphan (a legacy
    milestone) and every non-milestone carry ``None`` and render no button.
    Every new kind also carries ``text``, which the Shirley case brief's digest
    reads (``web/routes/chat.py``).
    """
    payload = entry.payload or {}
    kind = entry.kind
    projected: dict[str, Any] = {
        "id": str(entry.id),
        "kind": kind,
        "kind_label": _ENTRY_KIND_LABELS.get(kind, kind.replace("_", " ").capitalize()),
        "when": _stamp(entry.created_at),
        "actor": _actor_chip(entry, owner_names),
        "file_href": file_href,
        "file_word": _FILE_AS_WORDS.get(kind, kind.replace("_", " ")) if file_href else "",
    }
    if kind == "opened":
        projected["text"] = opened_text
    elif kind == "note":
        projected["text"] = payload.get("text", "")
    elif kind == "decision_record" and payload.get("decision_id"):
        chosen = str(payload.get("chosen_text") or "")
        rationale = str(payload.get("rationale") or "")
        projected["recorded"] = True
        projected["decision"] = chosen
        projected["rationale"] = rationale
        projected["text"] = f"Decision recorded: {chosen} — {rationale}"
    elif kind == "decision_record":
        projected["decision"] = payload.get("decision", "")
        projected["rationale"] = payload.get("rationale", "")
    elif kind == "handed_over":
        to_user = _parse_uuid(payload.get("to_user_id"))
        to_name = (
            _owner_label(to_user, owner_names)
            if to_user is not None
            else str(payload.get("to_user_id") or "—")
        )
        projected["text"] = f"Handed over to {to_name}"
        projected["note"] = str(payload.get("note") or "")
    elif kind == "waiting_set":
        until = _as_date(payload.get("wait_until"))
        projected["text"] = f"Waiting until {_day(until)}" if until is not None else "Waiting"
        projected["note"] = str(payload.get("note") or "")
    elif kind == "waiting_lifted":
        reason = str(payload.get("reason") or "")
        if reason == "event":
            event = payload.get("event")
            fallback = event.get("event") if isinstance(event, dict) else None
            woken = waking_text or str(fallback or "an event").replace("_", " ")
            projected["text"] = f"Waiting lifted — woken by: {woken}"
        else:
            projected["text"] = _LIFT_TEXTS.get(reason, "Waiting lifted")
    elif kind == "system_line":
        projected["text"] = str(payload.get("text") or "")
    elif kind == "closed":
        projected["closing_note"] = payload.get("closing_note", "")
    elif kind == "pin":
        # A legacy pin (D-CS-M-7): the class body is the kept classes' own
        # projection, so a pin in the Record and a kept row in a thread render
        # one anatomy; the pin adds its label and its curation comment. Any
        # other artifact projects only its raw value — the calm fallback, the
        # payload never leaked.
        artifact = payload.get("artifact")
        projected["artifact"] = artifact
        label = _PIN_KIND_LABELS.get(str(artifact))
        if label is not None:
            projected["kind_label"] = label
            projected["comment"] = payload.get("comment", "")
            projected.update(_project_kept_payload(str(artifact), payload, case_id=entry.case_id))
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


def _pair_frozen(entries: Iterable[CaseEntryDTO]) -> dict[str, CaseEntryDTO]:
    """Each milestone's ``frozen`` snapshot, keyed by the milestone's id as text (D-CS-B-6).

    Paired by ``payload.milestone_entry_id``, never by adjacency: rows that
    share an instant need not be a pair, and a legacy milestone has none.
    """
    pairs: dict[str, CaseEntryDTO] = {}
    for entry in entries:
        if entry.kind != "frozen":
            continue
        milestone_id = (entry.payload or {}).get("milestone_entry_id")
        if milestone_id:
            pairs[str(milestone_id)] = entry
    return pairs


def _record_file_href(case_id: UUID, entry_id: Any) -> str:
    """The *File as …* read of one milestone (D-CS-B-74)."""
    return f"/api/cases/{case_id}/record/{entry_id}/file"


async def _project_timeline(db_session: AsyncSession, case: CaseDTO) -> list[dict[str, Any]]:
    """The Record block's rows, oldest first (D-CS-B-73).

    One read of the record with its ``frozen`` rows
    (``list_entries(include_frozen=True)``): a snapshot is never a row of its
    own; it is attached to its milestone by ``payload.milestone_entry_id``
    (:func:`_pair_frozen`) as the row's *File as …* link. A lift woken by an
    event names the system line that woke it, found by
    ``payload.event.entry_id`` in the same list. The actors, the owner and a
    hand-over's two users are resolved in one batch; the originating finding
    is read for the ``opened`` phrasing. The single source of the record's
    shape: the file's first render and the Record block's re-render after a
    milestone (the pin composer's refresh into ``#case-timeline`` retired with
    P-CS-4a).
    """
    entries = await CaseRepository(db_session).list_entries(case.id, include_frozen=True)
    pairs = _pair_frozen(entries)
    listed = [entry for entry in entries if entry.kind != "frozen"]

    finding: IreneFindingDTO | None = None
    if case.finding_id is not None:
        finding = await IreneFindingRepository(db_session).get(case.finding_id)

    actor_ids: set[UUID] = {case.opened_by, case.owner_user_id}
    if case.closed_by is not None:
        actor_ids.add(case.closed_by)
    for entry in listed:
        if entry.actor_user_id is not None:
            actor_ids.add(entry.actor_user_id)
        if entry.kind == "handed_over":
            for key in ("from_user_id", "to_user_id"):
                user_id = _parse_uuid((entry.payload or {}).get(key))
                if user_id is not None:
                    actor_ids.add(user_id)
    owner_names = await _resolve_owner_names(UserRepository(db_session), actor_ids)
    opened_text = _opened_text(case, finding)
    line_texts = {
        str(entry.id): str((entry.payload or {}).get("text") or "")
        for entry in listed
        if entry.kind == "system_line"
    }

    rows: list[dict[str, Any]] = []
    for entry in listed:
        waking_text: str | None = None
        event = (entry.payload or {}).get("event") if entry.kind == "waiting_lifted" else None
        if isinstance(event, dict):
            waking_text = line_texts.get(str(event.get("entry_id"))) or None
        rows.append(
            _project_entry(
                entry,
                owner_names,
                opened_text=opened_text,
                file_href=(
                    _record_file_href(case.id, entry.id) if str(entry.id) in pairs else None
                ),
                waking_text=waking_text,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# The editable file (P-CS-3b) — the frame, the sections, Where we stand
# ---------------------------------------------------------------------------

#: The autosaving blocks — the ones the stamps partial carries a stamp for — in
#: ADR order (D-CS-B-49, D-CS-B-59, D-CS-B-67). Record follows "decision" in
#: the file and the outline; it is written only by milestones, so it carries
#: no stamp and is not one of these.
_SECTION_NAMES: tuple[str, ...] = (
    "question",
    "stand",
    "threads",
    "figures",
    "conversations",
    "points",
    "decision",
)

#: One block partial per renderable block: the seven sections and the Record,
#: which re-renders after a milestone (D-CS-B-73).
_SECTION_TEMPLATES: dict[str, str] = {
    **{name: f"_partials/cases_file_{name}.html" for name in _SECTION_NAMES},
    "record": "_partials/cases_file_record.html",
}

#: The out-of-band bundle every save answers with (D-CS-B-44).
_OOB_TEMPLATE = "_partials/cases_file_oob.html"

#: The states the working file accepts writes in (ADR-0138 §4, D-CS-B-52) —
#: the repositories' own gate, restated so the route answers calmly first.
_MUTABLE_STATES: frozenset[str] = frozenset({"open", "waiting"})

#: ``framing`` → the question block's heading (D-CS-M-1); ``None`` reads as a question.
_FRAMING_TITLES: dict[str, str] = {"question": "The question", "task": "The task"}

_STALE_NOTE = "Saved over a newer version from {time} — read the text once."
_NO_FROZEN_TEXT = "No frozen state — this case was closed before the working file existed."
_NOT_FROZEN_TEXT = "Where we stand was not frozen when this case closed."

#: A thread's status → its chip label (D-CS-B-53), in the order the switch shows them.
_THREAD_STATUS_LABELS: dict[str, str] = {
    "open": "Open",
    "confirmed": "Confirmed",
    "parked": "Parked",
    "refuted": "Refuted",
}

#: The sources a PM types a figure under (D-CS-B-54). ``book`` and ``kept``
#: figures come from their own paths and are never typed.
_FIGURE_EDITABLE_SOURCES: tuple[str, ...] = ("typed", "document", "conversation", "external")

#: A ``book`` figure's metric → its label, in the picker's order (D-CS-B-54).
#: The keys are the metrics ``book_values_for`` resolves (D-CS-B-14).
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

#: A figure's source → ``(pf-src tone, chip label)`` (D-CS-B-54).
_SOURCE_CHIPS: dict[str, tuple[str, str]] = {
    "book": ("live", "book"),
    "typed": ("typed", "typed"),
    "document": ("doc", "document"),
    "conversation": ("call", "conversation"),
    "kept": ("kept", "kept"),
    "external": ("ext", "external"),
}

#: The fields a figure row's form may carry; a ``book`` row takes the first two.
_FIGURE_FIELDS: tuple[str, ...] = (
    "label",
    "note",
    "value_text",
    "source",
    "as_of",
    "investment_id",
    "metric",
)
_BOOK_ROW_FIELDS: frozenset[str] = frozenset({"label", "note"})

_UNKNOWN_INVESTMENT = "unknown investment"
_CHOOSE_INVESTMENT_TEXT = "Choose an investment."
_CHOOSE_METRIC_TEXT = "Choose a metric."
_FIGURE_SOURCE_TEXT = (
    "Choose typed, document, conversation or external — "
    "book and kept figures come from their own paths."
)
_BOOK_ROW_TEXT = "A figure from the book takes only its label and note — its value is read live."

#: A conversation's channel → its label, in the select's order (D-CS-B-62).
_CHANNEL_LABELS: dict[str, str] = {"call": "Call", "meeting": "Meeting", "other": "Other"}

#: ``framing`` → the Open points heading and its sub (D-CS-B-63, D-CS-M-1): a
#: task's points are the questions to ask.
_POINTS_TITLES: dict[str, str] = {"question": "Open points", "task": "Questions to ask"}
_POINTS_SUBS: dict[str, str] = {
    "question": "what has to happen, who, by when",
    "task": "what to ask, whom, by when",
}

#: The fields a point row's form may carry besides ``done`` (D-CS-B-63).
_POINT_FIELDS: tuple[str, ...] = ("text", "owner_user_id", "due_on")

#: A linked ticket's statuses that list it under History, not the Blotter.
_TERMINAL_TICKET_STATUSES: frozenset[str] = frozenset({STATUS_BOOKED, STATUS_CANCELLED})

_HELD_ON_TEXT = "{raw!r} is not a date — write the day it was held as YYYY-MM-DD."
_DURATION_TEXT = "{raw!r} is not a duration — write whole minutes, or leave it empty."
_POINT_DUE_TEXT = "{raw!r} is not a date — write the due date as YYYY-MM-DD."
_DECIDED_AT_TEXT = "{raw!r} is not a date — write the decision date as YYYY-MM-DD."
_OWNER_TEXT = "Choose a user of this tenant."
_CHOSEN_REMOVED_TEXT = "The chosen option was removed — choose again."


def _framing(case: CaseDTO) -> str:
    """The case's framing, ``question`` when unset."""
    return case.framing if case.framing in _FRAMING_TITLES else "question"


def _clock(moment: datetime) -> str:
    """``HH:MM`` in UTC (D-CS-B-41)."""
    return f"{moment.astimezone(timezone.utc):%H:%M}"


def _is_stale(stamp: Any, case: CaseDTO) -> bool:
    """Whether the client's stamp names another version than ``case`` (D-CS-B-45).

    No stamp is no claim, so never stale; an unreadable stamp is stale.
    """
    raw = str(stamp).strip() if stamp is not None else ""
    if not raw:
        return False
    try:
        sent = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=timezone.utc)
    return sent != case.updated_at


def _note(text: str, tone: str = "info") -> dict[str, str]:
    """A ``pf-note`` for a section's note slot: its text and tone."""
    return {"text": text, "tone": tone}


def _decision_count_label(recorded: int, *, has_draft: bool) -> str:
    """The Decision entry's count: ``n recorded`` · ``draft`` (either, both, or ``""``)."""
    parts: list[str] = []
    if recorded:
        parts.append(f"{recorded} recorded")
    if has_draft:
        parts.append("draft")
    return " · ".join(parts)


def _outline(
    case: CaseDTO,
    *,
    record_count: int,
    threads_count: int,
    figures_count: int,
    file_kept_count: int = 0,
    conversations_count: int = 0,
    points_open: int = 0,
    points_total: int = 0,
    decision_label: str = "",
) -> list[dict[str, Any]]:
    """The outline's entries, in ADR order, for the sections that exist (D-CS-B-50, D-CS-B-67).

    The question · Where we stand · Threads · Figures · Conversations · Open
    points (*Questions to ask* on a task) · Decision · Record. Threads,
    Figures, Conversations, Open points and Decision are hidden when empty
    (D-CS-B-57): their entry has ``empty`` set and renders grey. On an open or
    waiting case it carries ``+`` and reveals the block through
    ``reveal_href``; a closed case has nothing to add, so its empty entry is
    grey alone (``reveal_href`` is ``None``). A non-empty entry reads its
    count — Open points the **open** count (a done point still lists it),
    Decision ``draft`` / ``n recorded`` (:func:`_decision_count_label`).
    Threads counts the threads; the rows of *Kept in the file* append
    ``· kept m`` and keep the entry from reading empty (D-CS-B-90) — a row of
    a removed thread among them, a ``document`` row not (it is the question
    block's, D-CS-B-100).
    """
    is_mutable = case.state in _MUTABLE_STATES

    def entry(
        name: str, label: str, count_label: str, *, revealable: bool, empty: bool = False
    ) -> dict[str, Any]:
        return {
            "name": name,
            "label": label,
            "count_label": count_label,
            "empty": empty,
            "href": f"#cs-{name}",
            "reveal_href": (
                f"/api/cases/{case.id}/sections/{name}?reveal=1" if revealable else None
            ),
        }

    return [
        entry("question", _FRAMING_TITLES[_framing(case)], "", revealable=True),
        entry(
            "stand",
            "Where we stand",
            "at close" if case.state == "closed" else "live",
            revealable=True,
        ),
        entry(
            "threads",
            "Threads",
            str(threads_count) + (f" · kept {file_kept_count}" if file_kept_count else ""),
            revealable=is_mutable,
            empty=threads_count == 0 and file_kept_count == 0,
        ),
        entry(
            "figures",
            "Figures",
            str(figures_count),
            revealable=is_mutable,
            empty=figures_count == 0,
        ),
        entry(
            "conversations",
            "Conversations",
            str(conversations_count),
            revealable=is_mutable,
            empty=conversations_count == 0,
        ),
        entry(
            "points",
            _POINTS_TITLES[_framing(case)],
            str(points_open),
            revealable=is_mutable,
            empty=points_total == 0,
        ),
        entry(
            "decision",
            "Decision",
            decision_label,
            revealable=is_mutable,
            empty=not decision_label,
        ),
        entry("record", "Record", str(record_count), revealable=False),
    ]


async def _frame_context(
    db_session: AsyncSession, case: CaseDTO, session: SessionDTO
) -> dict[str, Any]:
    """What the file renders around its blocks, and what every save refreshes.

    The head (the crumb reads its badge and title), the facts (with the
    linked count and the attachments row), the outline (with the Threads,
    Figures, Conversations, Open points and Decision counts, and the Record
    count — the listed rows, ``frozen`` snapshots excluded), the stamp and
    the action hrefs. The OOB bundle (``cases_file_oob.html``) renders from
    exactly this, so the first render and a save's refresh cannot drift. The
    record's rows are the Record block's own (:func:`_project_timeline`).
    """
    entries = await CaseRepository(db_session).list_entries(case.id)

    finding: IreneFindingDTO | None = None
    if case.finding_id is not None:
        finding = await IreneFindingRepository(db_session).get(case.finding_id)

    actor_ids: set[UUID] = {case.opened_by, case.owner_user_id}
    if case.closed_by is not None:
        actor_ids.add(case.closed_by)
    owner_names = await _resolve_owner_names(UserRepository(db_session), actor_ids)
    attachments_count = await CaseAttachmentRepository(db_session).count_for_case(case.id)
    links_count = len(await CaseInvestmentRepository(db_session).list_for_case(case.id))
    threads = await CaseThreadRepository(db_session).list_for_case(case.id)
    threads_count = len(threads)
    live_thread_ids = {thread.id for thread in threads}
    file_kept_count = sum(
        1
        for row in await CaseKeptRepository(db_session).list_for_case(case.id)
        if _in_file_group(row, live_thread_ids)
    )
    figures_count = len(await CaseFigureRepository(db_session).list_for_case(case.id))
    conversations_count = len(await CaseConversationRepository(db_session).list_for_case(case.id))
    points = await CasePointRepository(db_session).list_for_case(case.id)
    decisions = CaseDecisionRepository(db_session)
    decision_label = _decision_count_label(
        len(await decisions.list_recorded(case.id)),
        has_draft=await decisions.get_draft(case.id) is not None,
    )

    is_open = case.state == "open"
    is_closed = case.state == "closed"
    is_mutable = case.state in _MUTABLE_STATES
    cfg = get_config()

    return {
        "csrf_token": session.csrf_token,
        "case_id": str(case.id),
        "head": _project_head(case, owner_names),
        "is_open": is_open,
        "is_mutable": is_mutable,
        "stamp": case.updated_at.isoformat(),
        "section_names": _SECTION_NAMES,
        "saved_label": "",
        "outline": _outline(
            case,
            record_count=len(entries),
            threads_count=threads_count,
            figures_count=figures_count,
            file_kept_count=file_kept_count,
            conversations_count=conversations_count,
            points_open=sum(1 for point in points if point.done_at is None),
            points_total=len(points),
            decision_label=decision_label,
        ),
        # The origin finding fact (text only), when present.
        "linked_finding": (
            {"subject_key": finding.subject_key, "band": finding.band}
            if finding is not None
            else None
        ),
        "linked_label": f"{links_count} investments · {attachments_count} documents",
        # Attachments · N of {cap}: shown when N > 0 or the case is mutable.
        "attachments_count": attachments_count,
        "attachments_cap": cfg.case_attachment_max_count,
        "show_attachments": attachments_count > 0 or is_mutable,
        # Journal deep link — closed cases only (binding decision 2).
        "journal_href": JOURNAL_DEEP_LINK if is_closed else None,
        # "Capture scenario" (ADR-0107 C5) and "Consult Shirley" (ADR-0107 C6) —
        # open and waiting cases (D-CS-B-76), plain links carrying the case as
        # their marker.
        "capture_scenario_href": (
            f"{PLANNING_DESK_AREA_URL}?case={case.id}" if is_mutable else None
        ),
        "consult_shirley_href": (f"{CONSULT_SHIRLEY_URL}?case={case.id}" if is_mutable else None),
    }


async def _question_context(db_session: AsyncSession, case: CaseDTO) -> dict[str, Any]:
    """The question block (D-CS-B-46, D-CS-B-56): heading, title, origin, prose, meta, documents.

    ``title`` is the block's heading (The question / The task); ``case_title``
    is the case's own title, the form's first field. ``note`` is set by the
    autosave route when it re-renders the block (the 3c report's F-3c-5).

    ``documents`` are the live file-level ``document`` kept rows (D-CS-B-96)
    — in the file, or in a thread removed since (D-CS-B-100) — not the
    attachments table: an attachment no kept row addresses is not listed.
    Each is ``{filename, size, kept, kept_by, href, kept_id, remove_href}``
    (*kept dd Mon by Name*, D-4b-9); ``remove_href`` on a mutable case only.
    """
    finding: IreneFindingDTO | None = None
    entries: list[CaseEntryDTO] = []
    if case.finding_id is not None:
        finding = await IreneFindingRepository(db_session).get(case.finding_id)
        entries = await CaseRepository(db_session).list_entries(case.id)
    origin = _project_origin(finding, entries) if finding is not None else None

    if case.finding_id is not None:
        surfaced = (
            f" · surfaced {finding.created_at:%d %b}"
            if finding is not None and finding.created_at is not None
            else ""
        )
        sub = f"from finding{surfaced}"
    else:
        sub = f"manual · opened {case.opened_at:%d %b}"

    managers: list[str] = []
    if case.state in _MUTABLE_STATES:
        active = await InvestmentRepository(db_session).list_active()
        managers = sorted({i.manager_name for i in active if i.manager_name})

    live_thread_ids = {
        thread.id for thread in await CaseThreadRepository(db_session).list_for_case(case.id)
    }
    document_rows = [
        row
        for row in await CaseKeptRepository(db_session).list_for_case(case.id)
        if row.artifact == "document" and _at_file_level(row, live_thread_ids)
    ]
    kept_names = await _resolve_owner_names(
        UserRepository(db_session), {row.kept_by for row in document_rows}
    )
    is_mutable = case.state in _MUTABLE_STATES
    documents: list[dict[str, Any]] = []
    for row in document_rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        documents.append(
            {
                "filename": str(payload.get("filename") or row.caption or "attachment"),
                "size": _human_size(int(payload.get("size_bytes") or 0)),
                "kept": f"{row.kept_at.astimezone(timezone.utc):%d %b}",
                "kept_by": _owner_label(row.kept_by, kept_names),
                "href": _attachment_href(
                    case.id, payload.get("attachment_id") or row.attachment_id
                ),
                "kept_id": str(row.id),
                "remove_href": (
                    f"/api/cases/{case.id}/kept/{row.id}/remove" if is_mutable else None
                ),
            }
        )
    framing = _framing(case)
    return {
        "title": _FRAMING_TITLES[framing],
        "case_title": case.title,
        "framing": framing,
        "sub": sub,
        "origin": origin,
        "description": case.description or "",
        "counterparty": case.counterparty or "",
        "due_on": case.due_on.isoformat() if case.due_on is not None else "",
        "due_label": _day(case.due_on) if case.due_on is not None else "",
        "managers": managers,
        "documents": documents,
        "note": None,
        "oob": False,
    }


# --- Where we stand: one projection over the dataclass or its snapshot -----


def _field(source: Any, name: str) -> Any:
    """Read ``name`` off a stand dataclass or its snapshot dict; ``None`` when absent."""
    if source is None:
        return None
    if isinstance(source, dict):
        return source.get(name)
    return getattr(source, name, None)


def _as_decimal(value: Any) -> Decimal | None:
    """A ``Decimal`` from a reading (``Decimal``) or a snapshot (``str``); ``None`` otherwise."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _as_date(value: Any) -> date | None:
    """A date from a reading (``date``) or a snapshot (ISO ``str``); ``None`` otherwise."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _as_datetime(value: Any) -> datetime | None:
    """An aware datetime from a reading or a snapshot's ISO string; ``None`` otherwise."""
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _figure_text(value: Decimal, unit: Any) -> str:
    """A subject figure in its unit — the Watch Desk monitor's idiom."""
    if unit == FIGURE_UNIT_DAYS:
        days = int(value)
        return f"{days} day{'' if days == 1 else 's'}"
    if unit == FIGURE_UNIT_RATIO:
        return f"{float(value):.2f}×"
    return f"{float(value):.1f} %"


def _reading_text(reading: Any) -> str:
    """One subject reading as text: figure of ceiling (status) · as of date."""
    value = _as_decimal(_field(reading, "value"))
    status_label = str(_field(reading, "status_label") or "")
    if value is None:
        return status_label or str(_field(reading, "note") or "not available")
    unit = _field(reading, "unit")
    text = _figure_text(value, unit)
    ceiling = _as_decimal(_field(reading, "ceiling"))
    if ceiling is not None:
        text += f" of {_figure_text(ceiling, unit)}"
    if status_label:
        text += f" ({status_label})"
    as_of = _as_date(_field(reading, "as_of"))
    if as_of is not None:
        text += f" · as of {_day(as_of)}"
    return text


def _money(value: Decimal | None, currency: str) -> str:
    return format_money_compact(float(value), currency) if value is not None else "—"


def _multiple(value: Decimal | None) -> str:
    return f"{float(value):.2f}x" if value is not None else "—"


def _rate(value: Decimal | None) -> str:
    return f"{value:.4f}" if value is not None else "—"


def _project_stand(
    source: Any, *, closed: bool, now: datetime, subject_key: str | None = None
) -> dict[str, Any]:
    """One display dict from a WhereWeStand or its snapshot dict (keys by name).

    Every level is read by field name — ``getattr`` on the dataclass,
    ``dict.get`` on the snapshot, whose JSONB keeps no order and may lack a
    level — and every ``Decimal`` or ISO string becomes display text here, so
    the live block and the block *At close* render from one template.

    Args:
        source: A :class:`~services.cases.stand.WhereWeStand` or its
            :func:`~services.cases.stand.to_snapshot` dict.
        closed: Whether the block is the frozen state at close (no unlink).
        now: The request instant, the fallback for a missing ``composed_at``.
        subject_key: The finding's subject key, so the subject is labelled
            even when the Watch Desk could not be read (no reading carries it).

    Returns:
        ``closed``, ``composed_at``, ``subject``, ``investments``,
        ``counterparty``, ``fx``, ``tickets`` and ``system_lines``.
    """
    subject: dict[str, Any] | None = None
    subject_block = _field(source, "subject")
    if subject_block is not None:
        now_reading = _field(subject_block, "now")
        at_opening = _field(subject_block, "at_opening")
        key = (
            subject_key
            or _field(now_reading, "subject_key")
            or _field(at_opening, "subject_key")
            or ""
        )
        reason = _field(subject_block, "unavailable_reason")
        subject = {
            "label": subject_display(str(key)) if key else "The finding's subject",
            "now": (
                _reading_text(now_reading)
                if now_reading is not None
                else str(reason or "not available")
            ),
            "at_opening": _reading_text(at_opening) if at_opening is not None else "",
            "at_opening_lines": [
                str(line) for line in (_field(subject_block, "at_opening_lines") or [])
            ],
        }

    investments: list[dict[str, Any]] = []
    for reading in _field(source, "investments") or []:
        currency = str(_field(reading, "currency") or "")
        nav_as_of = _as_date(_field(reading, "nav_as_of"))
        irr = _as_decimal(_field(reading, "irr"))
        investments.append(
            {
                "id": str(_field(reading, "investment_id") or ""),
                "name": str(_field(reading, "name") or ""),
                "currency": currency,
                "commitment": _money(_as_decimal(_field(reading, "commitment_amount")), currency),
                "nav": _money(_as_decimal(_field(reading, "nav_value")), currency),
                "as_of": _day(nav_as_of) if nav_as_of is not None else "—",
                "tvpi": _multiple(_as_decimal(_field(reading, "tvpi"))),
                "dpi": _multiple(_as_decimal(_field(reading, "dpi"))),
                "irr": f"{float(irr) * 100.0:.1f}%" if irr is not None else "—",
            }
        )

    counterparty: str | None = None
    counterparty_block = _field(source, "counterparty")
    if counterparty_block is not None:
        funds = [str(_field(f, "name") or "") for f in (_field(counterparty_block, "funds") or [])]
        count = len(funds)
        counterparty = (
            f"{_field(counterparty_block, 'name')} · {count} fund{'' if count == 1 else 's'} "
            "in the book" + (f": {', '.join(funds)}" if funds else "")
        )

    fx = [
        (
            f"{_field(r, 'currency')}/{_field(r, 'functional_currency')} at opening "
            f"{_rate(_as_decimal(_field(r, 'rate_at_opening')))} → now "
            f"{_rate(_as_decimal(_field(r, 'rate_now')))}"
        )
        for r in _field(source, "fx") or []
    ]

    tickets: list[str] = []
    for ticket in _field(source, "tickets") or []:
        trade_date = _as_date(_field(ticket, "trade_date"))
        parts = [
            f"#{_field(ticket, 'ticket_number')}",
            str(_field(ticket, "kind") or ""),
            str(_field(ticket, "direction") or ""),
            str(_field(ticket, "status") or ""),
            str(_field(ticket, "investment_name") or "—"),
            _day(trade_date) if trade_date is not None else "—",
        ]
        tickets.append(" · ".join(parts))

    system_lines: list[dict[str, str]] = []
    for line in _field(source, "system_lines") or []:
        created = _as_datetime(_field(line, "created_at"))
        system_lines.append(
            {
                "text": str(_field(line, "text") or _field(line, "event") or ""),
                "date": _day(created) if created is not None else "",
            }
        )

    return {
        "closed": closed,
        "composed_at": _as_datetime(_field(source, "composed_at")) or now,
        "subject": subject,
        "investments": investments,
        "counterparty": counterparty,
        "fx": fx,
        "tickets": tickets,
        "system_lines": system_lines,
    }


async def _compose_stand(
    db_session: AsyncSession, case: CaseDTO, figures: Sequence[CaseFigureDTO]
) -> WhereWeStand:
    """Compose Where we stand for an open or waiting case (D-CS-B-11, D-CS-B-55).

    One :class:`StandContext` per call. ``figures`` are handed to ``compose``
    so a ``book`` figure's investment is read too, for :func:`book_values_for`.
    A request calls this once: :func:`load_case_file_context` hands the result
    to the stand and the figures blocks alike.
    """
    ctx = await StandContext.load(db_session, now=_now())
    return await compose(ctx, case, figures=figures)


async def _freeze_args(
    db_session: AsyncSession, case: CaseDTO, figures: Sequence[CaseFigureDTO] | None = None
) -> dict[str, Any]:
    """The two inputs a milestone's freeze cannot read itself, composed once (D-CS-B-69).

    Where we stand is composed for the open or waiting case as it stands
    (:func:`_compose_stand`) and returned as ``stand`` — its
    :func:`~services.cases.stand.to_snapshot` dict — and ``book_values`` —
    the live values of the ``book`` figures (:func:`book_values_for`), so a
    ``book`` figure is frozen with its value (D-CS-B-8; the 3b/3b2 reports'
    F-3 / F-5). Every milestone route passes both into its repository call;
    the close of a waiting case passes the one freeze to both of its
    milestones. ``figures`` are read when not given. The caller gates the
    state first: ``compose`` refuses a closed case.
    """
    if figures is None:
        figures = await CaseFigureRepository(db_session).list_for_case(case.id)
    stand = await _compose_stand(db_session, case, figures)
    return {"stand": to_snapshot(stand), "book_values": book_values_for(stand, figures)}


async def _stand_context(
    db_session: AsyncSession, case: CaseDTO, *, stand: WhereWeStand | None = None
) -> dict[str, Any]:
    """Where we stand for the block: live via compose() on an open/waiting case, the
    frozen where_we_stand of latest_frozen on a closed one; both through _project_stand.

    ``compose`` refuses a closed case, so the state is branched on first. A
    ``stand`` already composed for this request is used as it is; without one
    the block composes its own (D-CS-B-11, D-CS-B-55). Nothing here writes
    (D-CS-M-14). A closed case without a frozen ``where_we_stand`` renders a
    calm line instead of the block's readings (``missing``).
    """
    now = _now()
    subject_key: str | None = None
    if case.finding_id is not None:
        finding = await IreneFindingRepository(db_session).get(case.finding_id)
        subject_key = finding.subject_key if finding is not None else None

    if case.state == "closed":
        frozen = await CaseRepository(db_session).latest_frozen(case.id)
        file_block = (frozen.payload or {}).get("file") if frozen is not None else None
        source = file_block.get("where_we_stand") if isinstance(file_block, dict) else None
        return {
            "closed": True,
            "title": "At close",
            "chip": (
                f"frozen · {frozen.created_at.astimezone(timezone.utc):%H:%M %d %b %Y}"
                if frozen is not None
                else ""
            ),
            "view": (
                _project_stand(source, closed=True, now=now, subject_key=subject_key)
                if isinstance(source, dict)
                else None
            ),
            "missing": _NO_FROZEN_TEXT if frozen is None else _NOT_FROZEN_TEXT,
        }

    if stand is None:
        figures = await CaseFigureRepository(db_session).list_for_case(case.id)
        stand = await _compose_stand(db_session, case, figures)
    view = _project_stand(stand, closed=False, now=now, subject_key=subject_key)
    return {
        "closed": False,
        "title": "Where we stand",
        "chip": f"live · {_clock(view['composed_at'])}",
        "view": view,
        "missing": "",
    }


# --- Threads and Figures (P-CS-3b2) ----------------------------------------


def _thread_dates(thread: CaseThreadDTO) -> str:
    """``dd Mon`` (opened) or ``dd Mon → dd Mon`` (concluded), in UTC (D-CS-B-53)."""
    opened = f"{thread.created_at.astimezone(timezone.utc):%d %b}"
    if thread.concluded_at is None:
        return opened
    return f"{opened} → {thread.concluded_at.astimezone(timezone.utc):%d %b}"


#: Where a kept artefact came from, by class — the kept row's chip (D-4a-7).
_KEPT_FROM: dict[str, str] = {
    "consultation": "Shirley",
    "chart_snapshot": "Shirley",
    "generated_document": "Shirley",
    "scenario_snapshot": "the Planning Desk",
}

#: The kept classes that address an attachment; their chip names the document.
_KEPT_DOCUMENT_CLASSES: frozenset[str] = frozenset({"document", "document_excerpt"})

#: The one kept class whose payload is edited in place (ADR-0138 §9, D-CS-B-98).
_GENERATED_DOCUMENT: str = "generated_document"


def _at_file_level(row: CaseKeptDTO, live_thread_ids: set[UUID]) -> bool:
    """Whether a live kept row sits at file level: no live thread holds it (D-CS-B-100).

    A row kept in the file (``thread_id`` null) and a row of a thread removed
    since — the thread no longer renders, so its rows surface in the file.
    """
    return row.thread_id is None or row.thread_id not in live_thread_ids


def _in_file_group(row: CaseKeptDTO, live_thread_ids: set[UUID]) -> bool:
    """Whether a live kept row renders under *Kept in the file* (D-CS-B-100).

    File level and not a ``document``: the file's documents are the question
    block's list (D-CS-B-96).
    """
    return row.artifact != "document" and _at_file_level(row, live_thread_ids)


def _project_kept_row(
    row: CaseKeptDTO,
    owner_names: dict[UUID, str],
    *,
    case_id: UUID | None = None,
    mutable: bool = False,
    readonly: bool = False,
) -> dict[str, Any]:
    """One kept row for ``_partials/cases_kept_row.html`` (D-CS-B-90, D-CS-B-97…99, D-CS-B-105).

    The head: a ``pf-src`` chip — *kept from Shirley · dd Mon* / *kept from
    the Planning Desk · dd Mon*, *document · filename* for a document,
    *document · filename, p. n* for an excerpt, *generated · Shirley · dd Mon*
    for a generated document — the caption (a generated document's title)
    and *kept by <name>*, then the actions right-aligned (D-CS-B-105):
    ``open_href`` (a document's or an excerpt's download), ``edit_href`` and
    ``sources`` (the count) for a generated document, ``remove_href``. Edit
    and Remove only on a mutable case and never ``readonly`` (the frozen
    file); Open and Sources always. The body is the class's projection
    (:func:`_project_kept_payload`) under ``art``, an excerpt's ``attached``
    (*attached dd Mon by Name* — the keep, which is the attach) added; a class
    it does not render falls to the calm *{class} · caption* line.

    Args:
        row: The kept row (a frozen file passes the row with its frozen
            caption and, for a generated document, its frozen payload).
        owner_names: Display names by user id, ``kept_by`` among them.
        case_id: The case, for the hrefs; the row's own when ``None``.
        mutable: Whether the case takes writes (open or waiting).
        readonly: A frozen file's row — no Remove, no Edit, its hook
            ``frozen-kept`` and no element id (the live row owns it).

    Returns:
        ``{id, artifact, caption, chip, kept_by, readonly, actions, art}``.
    """
    payload = row.payload if isinstance(row.payload, dict) else {}
    owner_case = case_id or row.case_id
    label = row.artifact.replace("_", " ").capitalize()
    day = f"{row.kept_at.astimezone(timezone.utc):%d %b}"
    kept_by = _owner_label(row.kept_by, owner_names)
    art: dict[str, Any] = {
        "artifact": row.artifact,
        "label": label,
        "kept_caption": row.caption or "",
        **_project_kept_payload(
            row.artifact, payload, case_id=owner_case, attachment_id=row.attachment_id
        ),
    }
    caption = row.caption or ""
    is_generated = row.artifact == _GENERATED_DOCUMENT
    if row.artifact in _KEPT_DOCUMENT_CLASSES:
        name = str(payload.get("filename") or row.caption or "attachment")
        page = art.get("page")
        chip = {
            "tone": "doc",
            "text": f"document · {name}" + (f", p. {page}" if page is not None else ""),
        }
        if row.artifact == "document_excerpt":
            art["attached"] = f"attached {day} by {kept_by}"
    elif is_generated:
        chip = {"tone": "kept", "text": f"generated · Shirley · {day}"}
        caption = art["title"] or caption
        art["sources_id"] = None if readonly else f"cs-kept-{row.id}-sources"
    else:
        chip = {
            "tone": "kept",
            "text": f"kept from {_KEPT_FROM.get(row.artifact, 'Shirley')} · {day}",
        }
    writable = mutable and not readonly
    base = f"/api/cases/{owner_case}/kept/{row.id}"
    return {
        "id": str(row.id),
        "artifact": row.artifact,
        "caption": caption,
        "chip": chip,
        "kept_by": kept_by,
        "readonly": readonly,
        "actions": {
            "open_href": (
                art.get("download_href") if row.artifact in _KEPT_DOCUMENT_CLASSES else None
            ),
            "edit_href": f"{base}/generated/edit" if writable and is_generated else None,
            "sources": len(art["sources"]) if is_generated else 0,
            "remove_href": f"{base}/remove" if writable else None,
        },
        "art": art,
    }


async def _threads_context(
    db_session: AsyncSession, case: CaseDTO, *, reveal: bool
) -> dict[str, Any]:
    """Threads (D-CS-B-53, D-CS-B-90): ``{threads[{id, title, prose, status,
    status_label, conclusion, dates, kept}], file_kept, empty, reveal, focus_id,
    open_id, note, oob, statuses}``.

    The case's threads in ``position`` order; removed threads never render.
    A closed case reads the same rows — its working file is read-only, so they
    are the threads as they stood at close. ``focus_id`` (a thread just added:
    open, its prose focused) and ``open_id`` (a thread whose title or status
    was just changed: open) are ``None`` here; the write routes set them on
    the block they answer with.

    The kept rows are read **once** (``list_for_case``, removed rows left
    out) and grouped by ``thread_id``: each live thread carries its own under
    ``kept``, the rows of *Kept in the file* are ``file_kept`` — every live
    row no live thread holds, a removed thread's among them (D-CS-B-100), but
    no ``document`` row: the file's documents are the question block's list
    (D-CS-B-96). The ``kept_by`` names resolve in one batch. ``empty`` — the
    block hidden — is no thread *and* no row in the file group. ``oob`` is set
    when the block rides out of band (the attach route, D-CS-B-96).
    """
    threads = await CaseThreadRepository(db_session).list_for_case(case.id)
    kept_rows = await CaseKeptRepository(db_session).list_for_case(case.id)
    owner_names = await _resolve_owner_names(
        UserRepository(db_session), {row.kept_by for row in kept_rows}
    )
    mutable = case.state in _MUTABLE_STATES
    live_thread_ids = {thread.id for thread in threads}
    kept_by_thread: dict[UUID, list[dict[str, Any]]] = {}
    file_kept: list[dict[str, Any]] = []
    for row in kept_rows:
        projected = _project_kept_row(row, owner_names, case_id=case.id, mutable=mutable)
        if row.thread_id is not None and row.thread_id in live_thread_ids:
            kept_by_thread.setdefault(row.thread_id, []).append(projected)
        elif _in_file_group(row, live_thread_ids):
            file_kept.append(projected)
    return {
        "threads": [
            {
                "id": str(thread.id),
                "title": thread.title,
                "prose": thread.prose,
                "status": thread.status,
                "status_label": _THREAD_STATUS_LABELS.get(
                    thread.status, thread.status.capitalize()
                ),
                "conclusion": thread.conclusion or "",
                "dates": _thread_dates(thread),
                "kept": kept_by_thread.get(thread.id, []),
            }
            for thread in threads
        ],
        "file_kept": file_kept,
        "empty": not threads and not file_kept,
        "reveal": reveal,
        "focus_id": None,
        "open_id": None,
        "note": None,
        "oob": False,
        "statuses": [
            {"value": value, "label": label} for value, label in _THREAD_STATUS_LABELS.items()
        ],
    }


def _book_ref(source_ref: Any) -> tuple[UUID | None, str]:
    """A ``book`` figure's read key ``(investment_id, metric)``; ``None`` / ``""`` if unreadable."""
    ref = source_ref if isinstance(source_ref, dict) else {}
    try:
        investment_id: UUID | None = UUID(str(ref.get("investment_id")))
    except ValueError:
        investment_id = None
    return investment_id, str(ref.get("metric") or "")


async def _investment_names(
    db_session: AsyncSession, rows: Iterable[Any], known: dict[UUID, str]
) -> dict[UUID, str]:
    """The names of the investments the ``book`` rows read: ``known`` first, the rest by id.

    ``rows`` are figure DTOs or frozen figure dicts. An id the book does not
    hold stays absent (the row reads *unknown investment*).
    """
    names = dict(known)
    missing: set[UUID] = set()
    repo = InvestmentRepository(db_session)
    for row in rows:
        if _field(row, "source") != "book":
            continue
        investment_id, _ = _book_ref(_field(row, "source_ref"))
        if investment_id is None or investment_id in names or investment_id in missing:
            continue
        investment = await repo.get_by_id(investment_id)
        if investment is None:
            missing.add(investment_id)
        else:
            names[investment_id] = investment.name
    return names


def _figure_as_of(raw: Any, *, is_book: bool) -> str:
    """A figure's as-of for display: a book date as ``dd Mon YYYY``, a typed one verbatim."""
    if raw is None or raw == "":
        return ""
    if is_book:
        moment = _as_date(raw)
        return _day(moment) if moment is not None else str(raw)
    return str(raw)


def _project_figure(
    row: Any, *, value_text: Any, as_of: Any, names: dict[UUID, str], live: bool
) -> dict[str, Any]:
    """One figure row of the block, from a :class:`CaseFigureDTO` or a frozen row dict.

    Read by field name (``_field``), so the live rows and the rows frozen at
    close render from one template. ``value_text`` and ``as_of`` arrive
    resolved: a typed figure's own; a live ``book`` figure's from
    :func:`book_values_for`; a frozen figure's as frozen (a ``book`` value was
    applied at the freeze). An empty value is *pending* on a typed row and
    *not available* on a ``book`` row (the template words it). A ``book`` row
    of a closed case is frozen, not live, so its chip takes the stand block's
    frozen tone.
    """
    source = str(_field(row, "source") or "")
    is_book = source == "book"
    tone, chip = _SOURCE_CHIPS.get(source, ("typed", source))
    if is_book and not live:
        tone = "frozen"
    ref_label = ""
    if is_book:
        investment_id, metric = _book_ref(_field(row, "source_ref"))
        name = names.get(investment_id) if investment_id is not None else None
        metric_label = _BOOK_METRIC_LABELS.get(metric, metric or "—")
        ref_label = f"{name or _UNKNOWN_INVESTMENT} · {metric_label}"
    value = str(value_text).strip() if value_text is not None else ""
    return {
        "id": str(_field(row, "id") or ""),
        "label": str(_field(row, "label") or ""),
        "value": value,
        "pending": not value,
        "source": source,
        "editable_source": source in _FIGURE_EDITABLE_SOURCES,
        "chip_css": tone,
        "chip_label": chip,
        "ref_label": ref_label,
        "note": str(_field(row, "note") or ""),
        "as_of": _figure_as_of(as_of, is_book=is_book),
        "is_book": is_book,
        "live": is_book and live,
    }


async def _closed_figures_context(
    db_session: AsyncSession, case: CaseDTO, *, reveal: bool
) -> dict[str, Any]:
    """Figures of a closed case, read-only, from the newest frozen snapshot.

    ``latest_frozen(...).payload["file"]["figures"]`` is read by name; its
    ``book`` rows carry the value frozen at close. A case without a frozen
    file (closed before the working file existed) falls back to the table's
    rows, whose ``book`` values then read *not available*.
    """
    frozen = await CaseRepository(db_session).latest_frozen(case.id)
    file_block = (frozen.payload or {}).get("file") if frozen is not None else None
    frozen_rows = file_block.get("figures") if isinstance(file_block, dict) else None
    rows: list[Any] = (
        [row for row in frozen_rows if isinstance(row, dict)]
        if isinstance(frozen_rows, list)
        else list(await CaseFigureRepository(db_session).list_for_case(case.id))
    )
    names = await _investment_names(db_session, rows, {})
    figures = [
        _project_figure(
            row,
            value_text=_field(row, "value_text"),
            as_of=_field(row, "as_of"),
            names=names,
            live=False,
        )
        for row in rows
    ]
    return {
        "figures": figures,
        "empty": not figures,
        "reveal": reveal,
        "investments": [],
        "metrics": [],
        "sources": [],
        "note": None,
    }


async def _figures_context(
    db_session: AsyncSession, case: CaseDTO, *, stand: WhereWeStand | None, reveal: bool
) -> dict[str, Any]:
    """Figures (D-CS-B-54, D-CS-B-55): ``{figures[{id, label, value, pending, source,
    editable_source, chip_css, chip_label, ref_label, note, as_of, is_book, live}], empty,
    reveal, investments[{id, name, linked}], metrics[{key, label}], sources[{value, label}],
    note}``.

    On an open or waiting case the rows are the figures table's, in
    ``position`` order; a ``book`` row's value and as-of are read live through
    :func:`book_values_for`. Where we stand is composed here only when
    ``stand`` is ``None`` **and** a ``book`` figure exists, so a case of typed
    figures reads neither the Watch Desk nor a NAV. ``investments`` are the
    active investments for *Add from the book*, the linked ones first, then by
    name. A closed case renders from its frozen snapshot
    (:func:`_closed_figures_context`).
    """
    if case.state not in _MUTABLE_STATES:
        return await _closed_figures_context(db_session, case, reveal=reveal)

    figures = await CaseFigureRepository(db_session).list_for_case(case.id)
    book_values: dict[str, dict] = {}
    if any(figure.source == "book" for figure in figures):
        if stand is None:
            stand = await _compose_stand(db_session, case, figures)
        book_values = book_values_for(stand, figures)

    active = await InvestmentRepository(db_session).list_active()
    links = await CaseInvestmentRepository(db_session).list_for_case(case.id)
    linked = {link.investment_id for link in links}
    names = await _investment_names(db_session, figures, {i.id: i.name for i in active})

    rows: list[dict[str, Any]] = []
    for figure in figures:
        if figure.source == "book":
            resolved = book_values.get(str(figure.id)) or {}
            value_text, as_of = resolved.get("value_text"), resolved.get("as_of")
        else:
            value_text, as_of = figure.value_text, figure.as_of
        rows.append(
            _project_figure(figure, value_text=value_text, as_of=as_of, names=names, live=True)
        )

    return {
        "figures": rows,
        "empty": not rows,
        "reveal": reveal,
        "investments": [
            {"id": str(investment.id), "name": investment.name, "linked": investment.id in linked}
            for investment in sorted(active, key=lambda i: (i.id not in linked, i.name.lower()))
        ],
        "metrics": [{"key": key, "label": label} for key, label in _BOOK_METRIC_LABELS.items()],
        "sources": [
            {"value": source, "label": _SOURCE_CHIPS[source][1]}
            for source in _FIGURE_EDITABLE_SOURCES
        ],
        "note": None,
    }


# --- Conversations, Open points, the Decision draft (P-CS-3c) --------------


def _conversation_when(conversation: CaseConversationDTO) -> str:
    """``dd Mon YYYY · Call · 20 min`` — a conversation's stamp column (D-CS-B-62)."""
    parts = [
        _day(conversation.held_on),
        _CHANNEL_LABELS.get(conversation.channel, conversation.channel.capitalize()),
    ]
    if conversation.duration_minutes is not None:
        parts.append(f"{conversation.duration_minutes} min")
    return " · ".join(parts)


async def _conversations_context(
    db_session: AsyncSession, case: CaseDTO, *, reveal: bool
) -> dict[str, Any]:
    """Conversations (D-CS-B-62): ``{conversations[{id, held_on, channel, when, with_whom,
    what_was_said, what_was_promised, duration}], empty, reveal, focus_id, note,
    channels[{value, label}], today}``.

    The case's conversations in ``position`` order — the repository lists the
    newest ``held_on`` first, the block lists them in the order they were
    added, so an added row lands above the add form at the foot. A closed case
    reads the same rows (its working file is read-only). ``focus_id`` (a
    conversation just added) and ``note`` (a note the block must carry itself
    when it is re-rendered, since the bundle's out-of-band note lands in the
    slot the re-render replaces) are set by the write routes. ``today`` (UTC)
    is the add form's default *held on*.
    """
    conversations = sorted(
        await CaseConversationRepository(db_session).list_for_case(case.id),
        key=lambda conversation: conversation.position,
    )
    return {
        "conversations": [
            {
                "id": str(conversation.id),
                "held_on": conversation.held_on.isoformat(),
                "channel": conversation.channel,
                "when": _conversation_when(conversation),
                "with_whom": conversation.with_whom,
                "what_was_said": conversation.what_was_said,
                "what_was_promised": conversation.what_was_promised or "",
                "duration": (
                    ""
                    if conversation.duration_minutes is None
                    else str(conversation.duration_minutes)
                ),
            }
            for conversation in conversations
        ],
        "empty": not conversations,
        "reveal": reveal,
        "focus_id": None,
        "note": None,
        "channels": [{"value": value, "label": label} for value, label in _CHANNEL_LABELS.items()],
        "today": _now().date().isoformat(),
    }


async def _points_context(
    db_session: AsyncSession, case: CaseDTO, *, reveal: bool
) -> dict[str, Any]:
    """Open points (D-CS-B-63): ``{title, sub, add_label, points[{id, text, done, state,
    owner_id, owner_label, due_on, due_label, is_soon, ticket, create_ticket_href}], empty,
    reveal, note, users[{id, name}]}``.

    The heading is *Open points*, or *Questions to ask* on a task (D-CS-M-1).
    The points in ``position`` order — a ticked point keeps its place, struck
    through, until it is removed. The tenant's users are read once
    (``list_all``), the active ones offered as owners by display name, an
    inactive owner kept so the select still says who it is. A linked ticket
    is read per point (``TradeTicketRepository.get``) and reads *Ticket #n ·
    status*, linking to the Blotter (or History, once booked or cancelled) —
    no row of either list has an address of its own. A point without a
    ticket on an open or waiting case offers *Create ticket →*
    (``create_ticket_href``, D-CS-B-64). ``is_soon``: undone and due within
    seven days of today (UTC), overdue included — the list's rule.
    """
    points = sorted(
        await CasePointRepository(db_session).list_for_case(case.id),
        key=lambda point: point.position,
    )
    is_mutable = case.state in _MUTABLE_STATES
    users = await UserRepository(db_session).list_all()
    names = {user.id: user.display_name or user.email for user in users}
    owners = {point.owner_user_id for point in points if point.owner_user_id is not None}
    tickets = TradeTicketRepository(db_session)
    today = _now().date()
    framing = _framing(case)

    rows: list[dict[str, Any]] = []
    for point in points:
        ticket: dict[str, str] | None = None
        if point.ticket_id is not None:
            linked = await tickets.get(point.ticket_id)
            ticket = {
                "label": (
                    f"Ticket #{linked.ticket_number} · {linked.status}"
                    if linked is not None
                    else "Ticket"
                ),
                "href": (
                    "/transactions#history"
                    if linked is not None and linked.status in _TERMINAL_TICKET_STATUSES
                    else "/transactions#blotter"
                ),
            }
        done = point.done_at is not None
        owner = point.owner_user_id
        rows.append(
            {
                "id": str(point.id),
                "text": point.text,
                "done": done,
                "state": "done" if done else "open",
                "owner_id": str(owner) if owner is not None else "",
                "owner_label": names.get(owner, str(owner)) if owner is not None else "",
                "due_on": point.due_on.isoformat() if point.due_on is not None else "",
                "due_label": _day(point.due_on) if point.due_on is not None else "",
                "is_soon": (
                    not done
                    and point.due_on is not None
                    and point.due_on - today <= timedelta(days=_SOON_DAYS)
                ),
                "ticket": ticket,
                "create_ticket_href": (
                    "/transactions?" + urlencode({"case": str(case.id), "point": str(point.id)})
                    if is_mutable and point.ticket_id is None
                    else None
                ),
            }
        )

    return {
        "title": _POINTS_TITLES[framing],
        "sub": _POINTS_SUBS[framing],
        "add_label": "New question" if framing == "task" else "New point",
        "points": rows,
        "empty": not rows,
        "reveal": reveal,
        "note": None,
        "users": [
            {"id": str(user.id), "name": names[user.id]}
            for user in sorted(users, key=lambda user: names[user.id].lower())
            if user.is_active or user.id in owners
        ],
    }


def _decision_options(decision: CaseDecisionDTO) -> list[dict[str, Any]]:
    """A decision's options as ``{key, text, chosen}``, read defensively off the JSONB."""
    options: list[dict[str, Any]] = []
    for option in decision.options:
        if not isinstance(option, dict):
            continue
        key = str(option.get("key") or "")
        options.append(
            {
                "key": key,
                "text": str(option.get("text") or ""),
                "chosen": bool(key) and key == decision.chosen_key,
            }
        )
    return options


def _project_recorded(decision: CaseDecisionDTO) -> dict[str, str]:
    """One recorded decision, read-only (D-CS-B-66)."""
    chosen = next((o["text"] for o in _decision_options(decision) if o["chosen"]), "")
    recorded_at = cast(datetime, decision.recorded_at).astimezone(timezone.utc)
    return {
        "recorded": f"Recorded {recorded_at:%d %b %Y %H:%M}",
        "chosen": chosen,
        "rationale": decision.rationale or "",
        "decided_by": decision.decided_by or "",
        "decided_at": _day(decision.decided_at) if decision.decided_at is not None else "",
    }


async def _decision_context(
    db_session: AsyncSession, case: CaseDTO, *, reveal: bool
) -> dict[str, Any]:
    """The Decision (D-CS-B-66): ``{recorded[{recorded, chosen, rationale, decided_by,
    decided_at}], draft {id, options[{key, text, chosen}], rationale, decided_by,
    decided_at} | None, empty, reveal, focus_key, note}``.

    The recorded decisions first, newest first (the repository lists them
    oldest first), read-only; then the case's draft, if there is one — the
    options, the chosen one, the rationale, decided by and at. A draft may be
    incomplete: the repository refuses only *recording* it without a chosen
    option and a rationale (D-CS-B-10), and recording is P-CS-3c2's. A case
    with neither is ``empty``. ``focus_key`` (an option just added) and
    ``note`` are set by the write route.
    """
    decisions = CaseDecisionRepository(db_session)
    draft = await decisions.get_draft(case.id)
    recorded = list(reversed(await decisions.list_recorded(case.id)))
    return {
        "recorded": [_project_recorded(decision) for decision in recorded],
        "draft": (
            {
                "id": str(draft.id),
                "options": _decision_options(draft),
                "rationale": draft.rationale or "",
                "decided_by": draft.decided_by or "",
                "decided_at": draft.decided_at.isoformat() if draft.decided_at else "",
            }
            if draft is not None
            else None
        ),
        "empty": draft is None and not recorded,
        "reveal": reveal,
        "focus_key": None,
        "note": None,
    }


async def load_section_context(
    db_session: AsyncSession,
    case: CaseDTO,
    session: SessionDTO,
    name: str,
    *,
    reveal: bool = False,
    stand: WhereWeStand | None = None,
) -> dict[str, Any]:
    """The context one block partial renders from (D-CS-B-49).

    The shared keys (``csrf_token``, ``case_id``, ``is_mutable``, ``stamp``,
    ``reveal``) plus one key named after the section: ``question`` (see
    :func:`_question_context`), ``stand`` (see :func:`_stand_context`),
    ``threads`` (see :func:`_threads_context`), ``figures`` (see
    :func:`_figures_context`), ``conversations`` (see
    :func:`_conversations_context`), ``points`` (see :func:`_points_context`),
    ``decision`` (see :func:`_decision_context`) or ``record`` —
    ``{entries, oob}``, the rows of :func:`_project_timeline` and whether the
    block rides out of band after a milestone (D-CS-B-73).
    :func:`load_case_file_context` composes the file from the same calls, so
    the first render and a block's re-render cannot drift.

    Args:
        db_session: A tenant-scoped session.
        case: The case, already read (and 404-checked) by the caller.
        session: The PM's session (for the CSRF token).
        name: One of ``_SECTION_NAMES``, or ``record``.
        reveal: Render a hidden-when-empty block visible, its add control
            focused (the outline's ``+``).
        stand: Where we stand already composed for this request, for the
            stand and figures blocks; ``None`` lets the block compose its own
            when it needs one (D-CS-B-55).

    Returns:
        The template context of ``_partials/cases_file_<name>.html``.

    Raises:
        ValueError: If ``name`` is not a section of the file.
    """
    context: dict[str, Any] = {
        "csrf_token": session.csrf_token,
        "case_id": str(case.id),
        "is_mutable": case.state in _MUTABLE_STATES,
        "stamp": case.updated_at.isoformat(),
        "reveal": reveal,
    }
    if name == "question":
        context["question"] = await _question_context(db_session, case)
    elif name == "stand":
        context["stand"] = await _stand_context(db_session, case, stand=stand)
    elif name == "threads":
        context["threads"] = await _threads_context(db_session, case, reveal=reveal)
    elif name == "figures":
        context["figures"] = await _figures_context(db_session, case, stand=stand, reveal=reveal)
    elif name == "conversations":
        context["conversations"] = await _conversations_context(db_session, case, reveal=reveal)
    elif name == "points":
        context["points"] = await _points_context(db_session, case, reveal=reveal)
    elif name == "decision":
        context["decision"] = await _decision_context(db_session, case, reveal=reveal)
    elif name == "record":
        context["record"] = {"entries": await _project_timeline(db_session, case), "oob": False}
    else:
        raise ValueError(
            f"Unknown section {name!r}; expected one of {', '.join(_SECTION_TEMPLATES)}."
        )
    return context


async def load_case_file_context(
    db_session: AsyncSession, case: CaseDTO, session: SessionDTO
) -> dict[str, Any]:
    """The sub-surface context: the frame plus every section's block context.

    The frame (:func:`_frame_context`): ``csrf_token``, ``case_id``, ``head``,
    ``is_open``, ``is_mutable``, ``stamp``, ``section_names``,
    ``saved_label`` (empty on the first render), ``outline``,
    ``linked_finding``, ``linked_label``, ``attachments_count`` /
    ``attachments_cap`` / ``show_attachments``, ``journal_href``,
    ``capture_scenario_href``, ``consult_shirley_href``. Then one key per
    section (``question``, ``stand``, ``threads``, ``figures``,
    ``conversations``, ``points``, ``decision``) and the ``record`` from
    :func:`load_section_context`.

    An open or waiting case is composed **once** here (D-CS-B-11, D-CS-B-55):
    the same :class:`WhereWeStand` feeds the stand block and the figures
    block's ``book`` values.

    The actions and the working file are offered on an open or waiting case
    (``is_mutable``, D-CS-B-52, D-CS-B-76); ``is_open`` only chooses *Wait
    until…* (open) or *Resume now* (waiting). A closed case renders read-only.

    Args:
        db_session: A tenant-scoped session.
        case: The case, already read (and 404-checked) by the caller.
        session: The PM's session (for the CSRF token).

    Returns:
        The template context of ``_partials/cases_file.html``.
    """
    context = await _frame_context(db_session, case, session)
    stand: WhereWeStand | None = None
    if case.state in _MUTABLE_STATES:
        figures = await CaseFigureRepository(db_session).list_for_case(case.id)
        stand = await _compose_stand(db_session, case, figures)
    for name in (*_SECTION_NAMES, "record"):
        context.update(await load_section_context(db_session, case, session, name, stand=stand))
    return context


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


@router.get("/api/cases/{case_id}/sections/{name}", response_class=HTMLResponse)
async def get_case_section(
    request: Request,
    case_id: UUID,
    name: str,
    reveal: bool = False,
    oob: bool = False,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """One block of the file, alone (D-CS-B-49): the stand's Refresh, the outline's ``+``.

    ``name`` is one of ``_SECTION_NAMES``; anything else, like an unknown or
    foreign case, is a 404. ``reveal=1`` renders a hidden-when-empty block
    visible with its add control focused. A render writes nothing, so it
    answers no OOB bundle — unless ``oob=1``: the Threads block's
    ``pf:case-kept`` listeners ask for it after a keep or an undo elsewhere
    wrote to the file, so the outline's counts, the facts and the stamps
    follow (D-CS-B-104). The block itself never carries ``hx-swap-oob``.
    """
    template = _SECTION_TEMPLATES.get(name)
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown section.")
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        case = await CaseRepository(db_session).get(case_id)
        if case is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found.")
        context = await load_section_context(db_session, case, session, name, reveal=reveal)
        if not oob:
            return _render(request, template, context)
        bundle = await _bundle_context(db_session, case, session, section=name, note=None)
    return _render_parts(request, [(template, context), (_OOB_TEMPLATE, bundle)])


# ---------------------------------------------------------------------------
# Composers + writes (C3b) — everything that mutates a case from the detail
# view. Every write endpoint takes ``verify_csrf`` and gates on the open or
# waiting state at the route (the repository already raises on a closed case;
# the route gate exists so the UI never offers what the layer below refuses,
# and so a defensive request against a closed case degrades to a calm error,
# logged because the UI should not have offered it).
# ---------------------------------------------------------------------------

# The closed-case calm error, shared by every composer's closed-state branch.
_CLOSED_CASE_ERROR = "This case is closed — closed cases are read-only and cannot be changed."

#: The composers of the actions row (D-CS-B-70, D-CS-B-75); the note and
#: decision composers are retired (the Decision block records a decision), and
#: the pin composer with P-CS-4a (Keep replaced it, ADR-0138 §9).
_COMPOSER_TEMPLATES: dict[str, str] = {
    "close": "_partials/cases_composer_close.html",
    "link-investment": "_partials/cases_composer_link_investment.html",
    "attach": "_partials/cases_composer_attach.html",
    "wait": "_partials/cases_composer_wait.html",
    "handover": "_partials/cases_composer_handover.html",
}

#: *Wait until…* proposes a date this many days ahead (D-CS-B-70).
_WAIT_DEFAULT_DAYS: int = 30

_WAIT_PAST_TEXT = "The date must be after today."
_WAIT_UNTIL_TEXT = "{raw!r} is not a date — write the date to wait until as YYYY-MM-DD."
_WAIT_NOT_OPEN_TEXT = "Only an open case can be set waiting — this one is waiting already."
_HANDOVER_TEXT = "Choose a user to hand over to."


def _wait_dates() -> dict[str, str]:
    """The *Wait until…* composer's default (today + 30 days) and earliest date (tomorrow), UTC."""
    today = _now().date()
    return {
        "wait_until": (today + timedelta(days=_WAIT_DEFAULT_DAYS)).isoformat(),
        "min_date": (today + timedelta(days=1)).isoformat(),
    }


async def _handover_candidates(db: AsyncSession, case: CaseDTO) -> list[dict[str, str]]:
    """The users a case can be handed to: the tenant's active users but the owner, by name.

    ``list_all`` is RLS-scoped, so another tenant's users never appear
    (ADR-0138 §13).
    """
    users = await UserRepository(db).list_all()
    candidates = [
        {"id": str(user.id), "name": user.display_name or user.email}
        for user in users
        if user.is_active and user.id != case.owner_user_id
    ]
    return sorted(candidates, key=lambda candidate: candidate["name"].lower())


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

    ``action='cancel'`` clears the slot (an empty body). The real actions
    render their form — DB-free, exactly like ``GET /api/cases/new`` — because
    the gate that matters lives on the write endpoints the composers post to.
    Three read: ``link-investment`` its candidates, the tenant's active
    investments not yet linked, by name (D-CS-B-47); ``handover`` the tenant's
    active users but the owner, by name; ``attach`` its two selects (for the
    last two an unknown or foreign case is then a 404). ``wait`` proposes today + 30 days (D-CS-B-70). An unknown action —
    the retired ``note``, ``decision`` and ``pin`` among them — is a 404.
    ``attach`` reads its two selects (:func:`_attach_options`): the case's
    attachments, so a passage is kept from one without a second upload, and
    its live threads for *Keep in* (D-CS-B-97).
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
    if action == "attach":
        context["caps_hint"] = _caps_hint(get_config())
        context["hint"] = _ATTACH_HINT
        engine = _engine(request)
        async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
            if await CaseRepository(db).get(case_id) is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found.")
            context.update(await _attach_options(db, case_id))
    if action == "wait":
        context.update(_wait_dates())
    if action == "handover":
        engine = _engine(request)
        async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
            case = await CaseRepository(db).get(case_id)
            if case is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found.")
            context["candidates"] = await _handover_candidates(db, case)
    if action == "link-investment":
        engine = _engine(request)
        async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
            links = await CaseInvestmentRepository(db).list_for_case(case_id)
            active = await InvestmentRepository(db).list_active()
        linked = {link.investment_id for link in links}
        context["candidates"] = [
            {
                "id": str(investment.id),
                "label": " · ".join(
                    part
                    for part in (investment.name, investment.manager_name, investment.currency)
                    if part
                ),
            }
            for investment in sorted(active, key=lambda i: i.name.lower())
            if investment.id not in linked
        ]
    return _render(request, template, context)


async def _read_upload(request: Request, cfg: Any) -> tuple[str, str, bytes, str] | str:
    """Validate and read the multipart ``file`` field — the attach route's gates.

    The attach route runs them only when a new file comes (D-CS-B-96); a
    passage kept from an attachment already on the case reads no upload.

    Returns ``(filename, mime_type, content, sha256)`` or the refusal text. Extension
    AND declared content type in the whitelist; the size gate reads bounded 64 KiB
    chunks and stops at cap + 1 bytes; the count cap is checked by the caller inside
    its transaction (it needs the repository).

    The gates, in order: a file is present → its sanitised name carries an
    allowed extension → the declared content type matches it → the real byte
    length (never the header) is within the cap → the file is not empty. The
    bounded read means an oversized upload is rejected without ever being
    buffered whole as one in-memory object (single-instance memory safety).
    """
    form = await _request_form(request)
    file_field = form.get("file")
    if not isinstance(file_field, UploadFile) or not file_field.filename:
        return "Choose a file."
    sanitised_name = _sanitise_filename(file_field.filename)
    ext = _file_extension(sanitised_name)
    allowed = cfg.case_attachment_allowed_types
    accepted = ", ".join(sorted(e.upper() for e in allowed))
    if not sanitised_name or ext not in allowed:
        return f"That file type is not allowed. Accepted: {accepted}."
    declared_mime = (file_field.content_type or "").split(";")[0].strip().lower()
    if declared_mime not in allowed[ext]:
        return (
            "The file's declared content type does not match its extension "
            f"— accepted types are {accepted}."
        )
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
        return f"The file exceeds the {max_bytes // (1024 * 1024)} MB per-file limit."
    content = b"".join(chunks)
    if len(content) == 0:
        return "The file is empty."
    return sanitised_name, declared_mime, content, hashlib.sha256(content).hexdigest()


# ---------------------------------------------------------------------------
# The editable file's writes (P-CS-3b): the autosave frame (D-CS-B-44), The
# question, links and documents. Each writes on an open or waiting case
# (D-CS-B-52) and answers the OOB bundle — a text autosave the bundle alone
# (``hx-swap="none"``), a structural change its block followed by the bundle.
# ---------------------------------------------------------------------------


async def _request_form(request: Request) -> Any:
    """The parsed form body — ``verify_csrf`` caches it on ``request.state.form``."""
    form = getattr(request.state, "form", None)
    if form is None:
        form = await request.form()
    return form


def _render_parts(
    request: Request,
    parts: Sequence[tuple[str, dict[str, Any]]],
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    """Render several partials into one response body, in order (a block + the bundle)."""
    templates = _templates(request)
    body = "".join(
        templates.get_template(template).render({**context, "request": request})
        for template, context in parts
    )
    return HTMLResponse(body, status_code=status_code, headers=headers)


async def _bundle_context(
    db_session: AsyncSession,
    case: CaseDTO,
    session: SessionDTO,
    *,
    section: str,
    note: dict[str, str] | None,
    clear_composer: bool = False,
) -> dict[str, Any]:
    """The OOB bundle's context for the reloaded ``case`` (D-CS-B-44, D-CS-B-51).

    ``#cs-saved`` (*Saved · HH:MM*), ``#cs-facts``, ``#cs-outline``, the
    stamps and ``#cs-note-<section>`` carrying ``note`` (or an empty slot);
    ``clear_composer`` empties ``#case-composer-slot`` after a composer saved.
    """
    context = await _frame_context(db_session, case, session)
    context.update(
        {
            "oob": True,
            "saved_label": f"Saved · {_clock(_now())}",
            "note_section": section,
            "note": note,
            "clear_composer": clear_composer,
        }
    )
    return context


def _note_refusal(request: Request, section: str, text: str, *, status_code: int) -> HTMLResponse:
    """A refusal: the section's note slot alone, nothing swapped in place, nothing saved.

    ``HX-Reswap: none`` keeps the block a structural change targets in place;
    the area's ``htmx:beforeSwap`` lets a 400 of this shape through, so the
    reason reaches the PM.
    """
    return _render_parts(
        request,
        [
            (
                _OOB_TEMPLATE,
                {
                    "oob": True,
                    "note_only": True,
                    "note_section": section,
                    "note": _note(text, "warn"),
                },
            )
        ],
        status_code=status_code,
        headers={"HX-Reswap": "none"},
    )


def _stale_note(case: CaseDTO, stamp: Any) -> dict[str, str] | None:
    """The last-write-wins note when ``stamp`` predates ``case`` (D-CS-B-45), else ``None``."""
    if not _is_stale(stamp, case):
        return None
    return _note(_STALE_NOTE.format(time=_clock(case.updated_at)))


def _parse_due(raw: str) -> date | None:
    """Parse ``YYYY-MM-DD`` strictly; ``ValueError`` on anything else."""
    if len(raw) != 10 or raw[4] != "-" or raw[7] != "-":
        raise ValueError(raw)
    return date.fromisoformat(raw)


@router.post("/api/cases/{case_id}/question", response_class=HTMLResponse)
async def post_case_question(
    request: Request,
    case_id: UUID,
    rerender: bool = False,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Autosave The question (D-CS-B-44 … D-CS-B-46, D-CS-B-56): title, prose, counterparty,
    due, framing.

    Only the fields the form sent are written (``update_working_file`` is
    partial): ``title`` as sent (the repository strips it and refuses an empty
    one → 400 with its message verbatim, nothing saved); ``description`` and
    ``counterparty`` stripped, ``None`` when empty; ``due_on`` ``YYYY-MM-DD``
    or empty for none (anything else → 400, nothing saved); ``framing``
    validated by the repository (its refusal → 400 with the message verbatim).
    The stamp is compared on the case read *before* the write, only for the
    note — the save always happens.

    Answers the OOB bundle alone — its crumb carries the title, so the head
    follows a rename; ``rerender=1`` (the title field, the framing switch)
    answers the question block (carrying its own note) followed by the
    bundle — and, when the framing changed, the points block out of band, so
    its heading follows at once (*Open points* ↔ *Questions to ask*). A
    closed case answers the calm closed error in the note slot and saves
    nothing.
    """
    form = await _request_form(request)
    values: dict[str, Any] = {}
    if "title" in form:
        values["title"] = str(form.get("title") or "")
    if "description" in form:
        values["description"] = str(form.get("description") or "").strip() or None
    if "counterparty" in form:
        values["counterparty"] = str(form.get("counterparty") or "").strip() or None
    if "due_on" in form:
        raw_due = str(form.get("due_on") or "").strip()
        try:
            values["due_on"] = _parse_due(raw_due) if raw_due else None
        except ValueError:
            return _note_refusal(
                request,
                "question",
                f"{raw_due!r} is not a date — write the due date as YYYY-MM-DD.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
    if "framing" in form:
        values["framing"] = str(form.get("framing") or "").strip() or None

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        repo = CaseRepository(db)
        case = await repo.get(case_id)
        if case is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found.")
        if case.state not in _MUTABLE_STATES:
            logger.warning(
                "cases question: closed-case write blocked tenant=%s user=%s case=%s",
                session.tenant_id,
                session.user_id,
                case_id,
            )
            return _note_refusal(request, "question", _CLOSED_CASE_ERROR, status_code=200)
        note = _stale_note(case, form.get("stamp"))
        try:
            updated = await repo.update_working_file(case_id, now=_now(), **values)
        except CaseWorkingFileInvalid as exc:
            return _note_refusal(
                request, "question", str(exc), status_code=status.HTTP_400_BAD_REQUEST
            )
        except CaseClosedError:
            return _note_refusal(request, "question", _CLOSED_CASE_ERROR, status_code=200)
        parts: list[tuple[str, dict[str, Any]]] = []
        if rerender:
            block = await load_section_context(db, updated, session, "question")
            block["question"]["note"] = note
            parts.append((_SECTION_TEMPLATES["question"], block))
            if _framing(updated) != _framing(case):
                # The points heading follows the framing (the 3c report's F-3c-4).
                points = await load_section_context(db, updated, session, "points")
                points["points"]["oob"] = True
                parts.append((_SECTION_TEMPLATES["points"], points))
        parts.append(
            (
                _OOB_TEMPLATE,
                await _bundle_context(db, updated, session, section="question", note=note),
            )
        )

    logger.info(
        "cases question: tenant=%s user=%s case=%s fields=%s",
        session.tenant_id,
        session.user_id,
        case_id,
        ",".join(sorted(values)),
    )
    return _render_parts(request, parts)


async def _write_link(
    request: Request, case_id: UUID, session: SessionDTO, *, link: bool
) -> HTMLResponse:
    """Link or unlink one investment, then answer the stand block and the bundle.

    ``investment_id`` must be a UUID (else 400). Linking needs a known (404)
    active (400) investment and is idempotent; unlinking a pair that is not
    linked is a no-op. A closed case answers the calm closed error.
    """
    form = await _request_form(request)
    action = "link" if link else "unlink"
    try:
        investment_id = UUID(str(form.get("investment_id") or "").strip())
    except ValueError:
        return _note_refusal(
            request,
            "stand",
            "Choose an investment.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        case = await CaseRepository(db).get(case_id)
        if case is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found.")
        if case.state not in _MUTABLE_STATES:
            logger.warning(
                "cases %s: closed-case write blocked tenant=%s user=%s case=%s",
                action,
                session.tenant_id,
                session.user_id,
                case_id,
            )
            return _note_refusal(request, "stand", _CLOSED_CASE_ERROR, status_code=200)
        links = CaseInvestmentRepository(db)
        if link:
            investment = await InvestmentRepository(db).get_by_id(investment_id)
            if investment is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Investment not found."
                )
            if not investment.is_active:
                return _note_refusal(
                    request,
                    "stand",
                    f"{investment.name} is not active — only an active investment can be linked.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
        note = _stale_note(case, form.get("stamp"))
        try:
            if link:
                await links.link(
                    case_id, investment_id=investment_id, linked_by=session.user_id, now=_now()
                )
            else:
                await links.unlink(case_id, investment_id=investment_id, now=_now())
        except (CaseClosedError, CaseStateInvalid):
            return _note_refusal(request, "stand", _CLOSED_CASE_ERROR, status_code=200)
        updated = await CaseRepository(db).get(case_id)
        assert updated is not None
        parts = [
            (
                _SECTION_TEMPLATES["stand"],
                await load_section_context(db, updated, session, "stand"),
            ),
            (
                _OOB_TEMPLATE,
                await _bundle_context(
                    db, updated, session, section="stand", note=note, clear_composer=link
                ),
            ),
        ]

    logger.info(
        "cases %s: tenant=%s user=%s case=%s investment=%s",
        action,
        session.tenant_id,
        session.user_id,
        case_id,
        investment_id,
    )
    return _render_parts(request, parts)


@router.post("/api/cases/{case_id}/link-investment", response_class=HTMLResponse)
async def post_case_link_investment(
    request: Request,
    case_id: UUID,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Link an investment (ADR-0138 §5, D-CS-B-47); answers Where we stand + the bundle."""
    return await _write_link(request, case_id, session, link=True)


@router.post("/api/cases/{case_id}/unlink-investment", response_class=HTMLResponse)
async def post_case_unlink_investment(
    request: Request,
    case_id: UUID,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Unlink an investment (ADR-0138 §5, D-CS-B-47); answers Where we stand + the bundle."""
    return await _write_link(request, case_id, session, link=False)


#: The attach composer's refusals and hint (D-4b-2).
_CHOOSE_DOCUMENT_TEXT = "Choose a document to upload or one already attached."
_ATTACH_THREAD_GONE_TEXT = "That thread is gone — pick another."
_ATTACH_HINT = (
    "A passage makes this an excerpt kept where you choose; "
    "without one the document itself is kept in the file."
)
_PAGE_TEXT = "{raw!r} is not a page — write a page number from 1, or leave it empty."


def _parse_page(raw: str) -> int | None:
    """An excerpt's page: ``None`` when empty, else a whole number from 1 (``ValueError`` otherwise)."""
    if not raw:
        return None
    if not raw.isdigit() or int(raw) < 1:
        raise ValueError(raw)
    return int(raw)


async def _attach_options(db: AsyncSession, case_id: UUID) -> dict[str, Any]:
    """The attach composer's two selects (D-CS-B-97).

    ``attachments`` — every attachment of the case (metadata only), so a
    second passage from the same document needs no second upload; ``threads``
    — the live threads, for *Keep in* after *The file*.
    """
    attachments = await CaseAttachmentRepository(db).list_for_case(case_id)
    threads = await CaseThreadRepository(db).list_for_case(case_id)
    return {
        "attachments": [
            {
                "id": str(attachment.id),
                "filename": attachment.filename,
                "size": _human_size(attachment.size_bytes),
            }
            for attachment in attachments
        ],
        "threads": [{"id": str(thread.id), "title": thread.title} for thread in threads],
    }


def _document_payload(attachment: CaseAttachmentDTO) -> dict[str, Any]:
    """A ``document`` kept row's payload: the ADR-0107 pin contract minus the comment."""
    return {
        "filename": attachment.filename,
        "mime_type": attachment.mime_type,
        "size_bytes": attachment.size_bytes,
        "sha256": attachment.sha256,
        "attachment_id": str(attachment.id),
    }


@router.post("/api/cases/{case_id}/attachments", response_class=HTMLResponse)
async def post_case_attachment(
    request: Request,
    case_id: UUID,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Attach a document and keep it in the file — or keep a passage of it (D-CS-B-96, D-CS-B-97).

    One multipart form: ``file`` (a new upload) **or** ``attachment_id`` (one
    already on the case), ``passage``, ``page``, ``caption`` and
    ``thread_id`` (*Keep in*; ``""`` is the file). The gates, in order, each
    re-rendering the attach composer with its ``error`` into the composer
    slot (``HX-Retarget`` / ``HX-Reswap``, the composer idiom) and writing
    nothing: the case is open or waiting → exactly one of the file and the
    attachment (a known attachment of this case) → the file gates of
    :func:`_read_upload` when a file comes → the count cap, for a new upload
    only → with a passage, the page a whole number from 1 or empty and the
    thread a live thread of the case.

    Then **one** transaction: the attachment created (a new upload) and one
    kept row, ``via="keep"`` — without a passage a ``document`` row in the
    file (caption the filename; a page is dropped), with one a
    ``document_excerpt`` row ``{filename, passage, page}`` where *Keep in*
    says, captioned by *Caption* or the passage's first line. A refusal of
    the keep under the write (the thread removed, the case closed in between)
    rolls the attachment back. An attachment already kept in the file and
    chosen again without a passage writes nothing (no second listing). No
    record entry (D-CS-B-47).

    Answers the question block (``#cs-question``, its documents list), the
    Threads block out of band (an excerpt shows at once) and the bundle (the
    attachments fact; the composer slot emptied).
    """
    cfg = get_config()
    engine = _engine(request)
    form = await _request_form(request)
    file_field = form.get("file")
    has_file = isinstance(file_field, UploadFile) and bool(file_field.filename)
    raw_attachment = str(form.get("attachment_id") or "").strip()
    passage = str(form.get("passage") or "").replace("\r\n", "\n").strip()
    raw_page = str(form.get("page") or "").strip()
    caption_text = str(form.get("caption") or "").strip()
    raw_thread = str(form.get("thread_id") or "").strip()
    posted = {
        "attachment_id": raw_attachment,
        "passage": passage,
        "page": raw_page,
        "caption": caption_text,
        "thread_id": raw_thread,
    }

    async def _attach_error(message: str) -> HTMLResponse:
        async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
            options = await _attach_options(db, case_id)
        response = _render(
            request,
            _COMPOSER_TEMPLATES["attach"],
            {
                "case_id": str(case_id),
                "csrf_token": session.csrf_token,
                "caps_hint": _caps_hint(cfg),
                "hint": _ATTACH_HINT,
                "error": message,
                "posted": posted,
                **options,
            },
        )
        response.headers["HX-Retarget"] = "#case-composer-slot"
        response.headers["HX-Reswap"] = "innerHTML"
        return response

    chosen: CaseAttachmentDTO | None = None
    thread: CaseThreadDTO | None = None
    thread_gone = False
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        case = await CaseRepository(db).get(case_id)
        if case is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found.")
        current_count = await CaseAttachmentRepository(db).count_for_case(case_id)
        attachment_uuid = _parse_uuid(raw_attachment)
        if attachment_uuid is not None:
            chosen = await CaseAttachmentRepository(db).get(attachment_uuid)
            if chosen is not None and chosen.case_id != case_id:
                chosen = None
        if passage and raw_thread:
            thread_uuid = _parse_uuid(raw_thread)
            thread = (
                await live_thread(db, case_id, thread_uuid) if thread_uuid is not None else None
            )
            thread_gone = thread is None
        already_kept = False
        if chosen is not None and not passage:
            live_ids = {t.id for t in await CaseThreadRepository(db).list_for_case(case_id)}
            already_kept = any(
                row.artifact == "document"
                and row.attachment_id == chosen.id
                and _at_file_level(row, live_ids)
                for row in await CaseKeptRepository(db).list_for_case(case_id)
            )

    if case.state not in _MUTABLE_STATES:
        logger.warning(
            "cases attach: closed-case write blocked tenant=%s user=%s case=%s",
            session.tenant_id,
            session.user_id,
            case_id,
        )
        return await _attach_error(_CLOSED_CASE_ERROR)
    if has_file == bool(raw_attachment) or (raw_attachment and chosen is None):
        return await _attach_error(_CHOOSE_DOCUMENT_TEXT)
    upload: tuple[str, str, bytes, str] | None = None
    if has_file:
        read = await _read_upload(request, cfg)
        if isinstance(read, str):
            return await _attach_error(read)
        if current_count >= cfg.case_attachment_max_count:
            return await _attach_error(
                f"This case already has the maximum of {cfg.case_attachment_max_count} attachments."
            )
        upload = read
    page: int | None = None
    if passage:
        try:
            page = _parse_page(raw_page)
        except ValueError:
            return await _attach_error(_PAGE_TEXT.format(raw=raw_page))
        if thread_gone:
            return await _attach_error(_ATTACH_THREAD_GONE_TEXT)

    now = _now()
    kept: CaseKeptDTO | None = None
    try:
        async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
            if upload is not None:
                filename, mime_type, content, sha256 = upload
                attachment = await CaseAttachmentRepository(db).create(
                    case_id,
                    filename=filename,
                    mime_type=mime_type,
                    size_bytes=len(content),
                    sha256=sha256,
                    content=content,
                    uploaded_by=session.user_id,
                    now=now,
                )
            else:
                assert chosen is not None
                attachment = chosen
            if passage:
                kept = await CaseKeptRepository(db).keep(
                    case_id,
                    artifact="document_excerpt",
                    payload={"filename": attachment.filename, "passage": passage, "page": page},
                    kept_by=session.user_id,
                    via="keep",
                    now=now,
                    thread_id=thread.id if thread is not None else None,
                    caption=caption_text or consultation_caption(passage),
                    attachment_id=attachment.id,
                )
            elif not already_kept:
                kept = await CaseKeptRepository(db).keep(
                    case_id,
                    artifact="document",
                    payload=_document_payload(attachment),
                    kept_by=session.user_id,
                    via="keep",
                    now=now,
                    caption=attachment.filename,
                    attachment_id=attachment.id,
                )
            parts = await _answer_parts(
                db,
                case_id,
                session,
                "question",
                rerender=True,
                note=None,
                also=("threads",),
                clear_composer=True,
            )
    except CaseWorkingFileInvalid as exc:
        # The keep refused under the write — the transaction rolled the upload back.
        logger.warning(
            "cases attach: keep refused under the write case=%s field=%s", case_id, exc.field
        )
        return await _attach_error(
            _ATTACH_THREAD_GONE_TEXT if exc.field == "thread_id" else _CHOOSE_DOCUMENT_TEXT
        )
    except (CaseClosedError, CaseStateInvalid):
        logger.warning("cases attach: case no longer writable under the write case=%s", case_id)
        return await _attach_error(_CLOSED_CASE_ERROR)

    logger.info(
        "cases attach: tenant=%s user=%s case=%s attachment=%s new=%s kept=%s artifact=%s",
        session.tenant_id,
        session.user_id,
        case_id,
        attachment.id,
        upload is not None,
        kept.id if kept is not None else None,
        kept.artifact if kept is not None else None,
    )
    return _render_parts(request, parts)


# ---------------------------------------------------------------------------
# Threads and Figures (P-CS-3b2): the two list sections of the working file.
# Each write runs on an open or waiting case (D-CS-B-58) and answers the
# 3b frame: a text autosave the bundle alone, a structural change (add,
# remove, a thread's title or status, a figure's source) its block followed
# by the bundle. Refusals go through ``_note_refusal`` into
# ``#cs-note-threads`` / ``#cs-note-figures``.
# ---------------------------------------------------------------------------


def _optional_text(form: Any, name: str) -> str | None:
    """A form field stripped, ``None`` when empty."""
    return str(form.get(name) or "").strip() or None


async def _writable_case(
    request: Request,
    db: AsyncSession,
    case_id: UUID,
    session: SessionDTO,
    *,
    section: str,
    action: str,
) -> CaseDTO | HTMLResponse:
    """The case a working-file write addresses, or the answer that refuses it.

    Unknown or foreign → 404 (raised). Closed → the calm closed error in the
    section's note slot (200, nothing written), logged because the UI does
    not offer the write (D-CS-B-58).
    """
    case = await CaseRepository(db).get(case_id)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found.")
    if case.state not in _MUTABLE_STATES:
        logger.warning(
            "cases %s: closed-case write blocked tenant=%s user=%s case=%s",
            action,
            session.tenant_id,
            session.user_id,
            case_id,
        )
        return _note_refusal(request, section, _CLOSED_CASE_ERROR, status_code=200)
    return case


async def _case_thread(db: AsyncSession, case_id: UUID, thread_id: UUID) -> CaseThreadDTO:
    """The live thread ``thread_id`` of ``case_id``.

    Another case's thread, a removed thread or an unknown id is a 404
    (D-CS-B-58): a removed thread never renders and refuses every write.
    """
    thread = await CaseThreadRepository(db).get(thread_id)
    if thread is None or thread.case_id != case_id or thread.removed_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found.")
    return thread


async def _case_kept_row(db: AsyncSession, case_id: UUID, kept_id: UUID) -> CaseKeptDTO:
    """The live kept row ``kept_id`` of ``case_id``.

    Another case's row, a removed one or an unknown id is a 404 — the
    ``_case_<row>`` idiom: a removed row never renders live and refuses every
    write (D-CS-B-99).
    """
    row = await CaseKeptRepository(db).get(kept_id)
    if row is None or row.case_id != case_id or row.removed_at is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Kept artefact not found."
        )
    return row


async def _case_figure(db: AsyncSession, case_id: UUID, figure_id: UUID) -> CaseFigureDTO:
    """The figure ``figure_id`` of ``case_id``; another case's or an unknown id is a 404."""
    figure = await CaseFigureRepository(db).get(figure_id)
    if figure is None or figure.case_id != case_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Figure not found.")
    return figure


async def _answer_parts(
    db: AsyncSession,
    case_id: UUID,
    session: SessionDTO,
    section: str,
    *,
    rerender: bool,
    note: dict[str, str] | None,
    block_keys: dict[str, Any] | None = None,
    also: Sequence[str] = (),
    clear_composer: bool = False,
) -> list[tuple[str, dict[str, Any]]]:
    """A write's answer: the section's block (``rerender``), then the bundle (D-CS-B-44).

    The case is re-read after the write, so the stamps, the outline counts
    and the crumb carry its new state. A re-rendered block carries ``note``
    itself: htmx swaps the bundle's out-of-band note first and the block's
    empty slot over it (the 3c report's D-7 and F-3c-5). ``block_keys`` are
    set on the block's own context (the thread to open or focus). ``also``
    names further blocks rendered out of band after it — the Record after a
    recorded decision (D-CS-B-70), the Threads block after an attach, the
    question block after a document's removal (D-CS-B-96, D-CS-B-99).
    ``clear_composer`` empties the composer slot (a composer saved). The
    figures block composes Where we stand once, after the write, when it
    holds a ``book`` figure (D-CS-B-55).
    """
    updated = await CaseRepository(db).get(case_id)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found.")
    parts: list[tuple[str, dict[str, Any]]] = []
    if rerender:
        block = await load_section_context(db, updated, session, section)
        block[section]["note"] = note
        if block_keys:
            block[section].update(block_keys)
        parts.append((_SECTION_TEMPLATES[section], block))
    for name in also:
        extra = await load_section_context(db, updated, session, name)
        extra[name]["oob"] = True
        parts.append((_SECTION_TEMPLATES[name], extra))
    parts.append(
        (
            _OOB_TEMPLATE,
            await _bundle_context(
                db, updated, session, section=section, note=note, clear_composer=clear_composer
            ),
        )
    )
    return parts


@router.post("/api/cases/{case_id}/threads", response_class=HTMLResponse)
async def post_case_thread_add(
    request: Request,
    case_id: UUID,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Add a thread (D-CS-B-53): the block re-rendered with it open, its prose focused.

    The title is required — the repository's refusal answers 400 with its
    message in ``#cs-note-threads`` and nothing is written. The new thread is
    ``open`` at the next position.
    """
    form = await _request_form(request)
    title = str(form.get("title") or "")
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        case = await _writable_case(
            request, db, case_id, session, section="threads", action="thread add"
        )
        if isinstance(case, HTMLResponse):
            return case
        try:
            thread = await CaseThreadRepository(db).add(case_id, title=title, now=_now())
        except CaseWorkingFileInvalid as exc:
            return _note_refusal(
                request, "threads", str(exc), status_code=status.HTTP_400_BAD_REQUEST
            )
        except (CaseClosedError, CaseStateInvalid):
            return _note_refusal(request, "threads", _CLOSED_CASE_ERROR, status_code=200)
        parts = await _answer_parts(
            db,
            case_id,
            session,
            "threads",
            rerender=True,
            note=None,
            block_keys={"focus_id": str(thread.id)},
        )

    logger.info(
        "cases thread add: tenant=%s user=%s case=%s thread=%s",
        session.tenant_id,
        session.user_id,
        case_id,
        thread.id,
    )
    return _render_parts(request, parts)


@router.post("/api/cases/{case_id}/threads/{thread_id}", response_class=HTMLResponse)
async def post_case_thread(
    request: Request,
    case_id: UUID,
    thread_id: UUID,
    rerender: bool = False,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Update a thread (D-CS-B-53): title · prose · status · conclusion, partial by presence.

    ``prose`` stripped; ``conclusion`` stripped, ``None`` when empty;
    ``title`` and ``status`` validated by the repository (an empty title or
    a status outside *open · confirmed · parked · refuted* → 400 with its
    message verbatim, nothing saved). A status leaving ``open`` sets
    ``concluded_at``; a return to ``open`` clears it (the repository's rule).

    Answers the bundle alone (the prose and conclusion autosave); ``rerender=1``
    (the title, the status) answers the block — the thread kept open, so the
    summary follows in place — followed by the bundle. Last write wins with a
    note (D-CS-B-45). Another case's, a removed or an unknown thread → 404.
    """
    form = await _request_form(request)
    values: dict[str, Any] = {}
    if "title" in form:
        values["title"] = str(form.get("title") or "")
    if "prose" in form:
        values["prose"] = str(form.get("prose") or "").strip()
    if "status" in form:
        values["status"] = str(form.get("status") or "").strip()
    if "conclusion" in form:
        values["conclusion"] = _optional_text(form, "conclusion")

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        case = await _writable_case(
            request, db, case_id, session, section="threads", action="thread update"
        )
        if isinstance(case, HTMLResponse):
            return case
        await _case_thread(db, case_id, thread_id)
        note = _stale_note(case, form.get("stamp"))
        try:
            await CaseThreadRepository(db).update(thread_id, now=_now(), **values)
        except CaseWorkingFileInvalid as exc:
            return _note_refusal(
                request, "threads", str(exc), status_code=status.HTTP_400_BAD_REQUEST
            )
        except CaseClosedError:
            return _note_refusal(request, "threads", _CLOSED_CASE_ERROR, status_code=200)
        parts = await _answer_parts(
            db,
            case_id,
            session,
            "threads",
            rerender=rerender,
            note=note,
            block_keys={"open_id": str(thread_id)},
        )

    logger.info(
        "cases thread update: tenant=%s user=%s case=%s thread=%s fields=%s",
        session.tenant_id,
        session.user_id,
        case_id,
        thread_id,
        ",".join(sorted(values)),
    )
    return _render_parts(request, parts)


@router.post("/api/cases/{case_id}/threads/{thread_id}/remove", response_class=HTMLResponse)
async def post_case_thread_remove(
    request: Request,
    case_id: UUID,
    thread_id: UUID,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Remove a thread from the file (D-CS-B-53): soft — ``removed_at`` is set, the row stays.

    The UI asks first (a native ``hx-confirm``). Answers the block without the
    thread — the hidden placeholder when it was the last — and the bundle.
    A thread already removed, another case's or an unknown one → 404.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        case = await _writable_case(
            request, db, case_id, session, section="threads", action="thread remove"
        )
        if isinstance(case, HTMLResponse):
            return case
        await _case_thread(db, case_id, thread_id)
        try:
            await CaseThreadRepository(db).remove(thread_id, now=_now())
        except (CaseClosedError, CaseStateInvalid):
            return _note_refusal(request, "threads", _CLOSED_CASE_ERROR, status_code=200)
        parts = await _answer_parts(db, case_id, session, "threads", rerender=True, note=None)

    logger.info(
        "cases thread remove: tenant=%s user=%s case=%s thread=%s",
        session.tenant_id,
        session.user_id,
        case_id,
        thread_id,
    )
    return _render_parts(request, parts)


# --- Kept rows (P-CS-4b): Remove, and the generated document's Edit --------

#: The 400 note a payload edit of any class but a generated document answers (D-4b-5).
_KEPT_IMMUTABLE_TEXT = (
    "Only a generated document is edited in place; everything else is immutable once kept."
)
_GENERATED_TITLE_TEXT = "A generated document needs a title."
_GENERATED_EDIT_TEMPLATE = "_partials/cases_kept_generated_edit.html"


def _thread_open_keys(row: CaseKeptDTO) -> dict[str, Any] | None:
    """Keep the row's thread open on the re-render (a file-level row has none)."""
    return {"open_id": str(row.thread_id)} if row.thread_id is not None else None


@router.post("/api/cases/{case_id}/kept/{kept_id}/remove", response_class=HTMLResponse)
async def post_case_kept_remove(
    request: Request,
    case_id: UUID,
    kept_id: UUID,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Remove a kept row from the file (ADR-0138 §9, D-CS-B-99): ``removed_at``, never a delete.

    The UI asks first (a native ``hx-confirm``, the walk-1 ruling). The row
    stays in the record and in every file frozen before (D-CS-M-5). Answers
    the Threads block re-rendered without the row (its thread kept open) —
    and the question block out of band when the row is a ``document`` (the
    documents list) — then the bundle. Another case's row, a removed or an
    unknown one → 404; a closed case → the calm closed note, nothing written.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        case = await _writable_case(
            request, db, case_id, session, section="threads", action="kept remove"
        )
        if isinstance(case, HTMLResponse):
            return case
        row = await _case_kept_row(db, case_id, kept_id)
        try:
            await CaseKeptRepository(db).remove(kept_id, now=_now())
        except (CaseClosedError, CaseStateInvalid):
            return _note_refusal(request, "threads", _CLOSED_CASE_ERROR, status_code=200)
        parts = await _answer_parts(
            db,
            case_id,
            session,
            "threads",
            rerender=True,
            note=None,
            block_keys=_thread_open_keys(row),
            also=("question",) if row.artifact == "document" else (),
        )

    logger.info(
        "cases kept remove: tenant=%s user=%s case=%s kept=%s artifact=%s",
        session.tenant_id,
        session.user_id,
        case_id,
        kept_id,
        row.artifact,
    )
    return _render_parts(request, parts)


@router.get("/api/cases/{case_id}/kept/{kept_id}/generated/edit", response_class=HTMLResponse)
async def get_case_kept_generated_edit(
    request: Request,
    case_id: UUID,
    kept_id: UUID,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """The generated document's in-place Edit form (D-CS-B-98), swapped over its body.

    Title and body on native controls, *Save* · *Cancel* (Cancel re-renders
    the Threads block). Only a ``generated_document`` opens one — any other
    class answers the 400 immutability note; a closed case the calm closed
    note; another case's, a removed or an unknown row → 404.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        case = await _writable_case(
            request, db, case_id, session, section="threads", action="generated edit"
        )
        if isinstance(case, HTMLResponse):
            return case
        row = await _case_kept_row(db, case_id, kept_id)
    if row.artifact != _GENERATED_DOCUMENT:
        return _note_refusal(
            request, "threads", _KEPT_IMMUTABLE_TEXT, status_code=status.HTTP_400_BAD_REQUEST
        )
    payload = row.payload if isinstance(row.payload, dict) else {}
    return _render(
        request,
        _GENERATED_EDIT_TEMPLATE,
        {
            "case_id": str(case_id),
            "csrf_token": session.csrf_token,
            "kept": {
                "id": str(row.id),
                "title": str(payload.get("title") or ""),
                "body": str(payload.get("body") or ""),
            },
        },
    )


@router.post("/api/cases/{case_id}/kept/{kept_id}/generated", response_class=HTMLResponse)
async def post_case_kept_generated(
    request: Request,
    case_id: UUID,
    kept_id: UUID,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Save a generated document's title and body in place (ADR-0138 §9, D-CS-B-98).

    :meth:`CaseKeptRepository.update_generated_document` rewrites the payload
    and its hash; the sources (and any other key) are carried over unchanged.
    The body is plain text — paragraphs split on blank lines when rendered.
    A file frozen before keeps the text it froze (the class is frozen by
    value). Any other class → :class:`CaseKeptImmutable` → the 400 note
    *Only a generated document is edited in place …*, nothing written; an
    empty title → 400; a closed case → the calm closed note. Answers the
    Threads block (the row's thread kept open) and the bundle.
    """
    form = await _request_form(request)
    title = str(form.get("title") or "").strip()
    body = str(form.get("body") or "").replace("\r\n", "\n").strip()
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        case = await _writable_case(
            request, db, case_id, session, section="threads", action="generated update"
        )
        if isinstance(case, HTMLResponse):
            return case
        row = await _case_kept_row(db, case_id, kept_id)
        payload = row.payload if isinstance(row.payload, dict) else {}
        if row.artifact == _GENERATED_DOCUMENT and not title:
            return _note_refusal(
                request, "threads", _GENERATED_TITLE_TEXT, status_code=status.HTTP_400_BAD_REQUEST
            )
        try:
            await CaseKeptRepository(db).update_generated_document(
                kept_id, payload={**payload, "title": title, "body": body}, now=_now()
            )
        except CaseKeptImmutable:
            return _note_refusal(
                request, "threads", _KEPT_IMMUTABLE_TEXT, status_code=status.HTTP_400_BAD_REQUEST
            )
        except (CaseClosedError, CaseStateInvalid):
            return _note_refusal(request, "threads", _CLOSED_CASE_ERROR, status_code=200)
        parts = await _answer_parts(
            db,
            case_id,
            session,
            "threads",
            rerender=True,
            note=None,
            block_keys=_thread_open_keys(row),
        )

    logger.info(
        "cases generated update: tenant=%s user=%s case=%s kept=%s",
        session.tenant_id,
        session.user_id,
        case_id,
        kept_id,
    )
    return _render_parts(request, parts)


@router.post("/api/cases/{case_id}/figures", response_class=HTMLResponse)
async def post_case_figure_add(
    request: Request,
    case_id: UUID,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Add a figure (D-CS-B-54): typed, or read from the book.

    ``source`` is ``typed`` when not sent. One of *typed · document ·
    conversation · external* adds the row as typed (``label`` required by the
    repository; ``value_text``, ``note``, ``as_of`` stripped, ``None`` when
    empty — an empty value reads *pending*). ``source=book`` needs an
    ``investment_id`` the book holds and a ``metric`` of the eight; it stores
    ``source_ref={investment_id, metric}`` with no ``value_text``, and an
    empty label defaults to *{investment} {metric}*. ``kept`` or anything
    else → 400. Answers the block (Where we stand composed once, after the
    write, when a ``book`` figure exists) and the bundle.
    """
    form = await _request_form(request)
    source = str(form.get("source") or "").strip() or "typed"
    label = str(form.get("label") or "").strip()
    book_ref: tuple[UUID, str] | None = None
    if source == "book":
        try:
            investment_id = UUID(str(form.get("investment_id") or "").strip())
        except ValueError:
            return _note_refusal(
                request,
                "figures",
                _CHOOSE_INVESTMENT_TEXT,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        metric = str(form.get("metric") or "").strip()
        if metric not in _BOOK_METRIC_LABELS:
            return _note_refusal(
                request, "figures", _CHOOSE_METRIC_TEXT, status_code=status.HTTP_400_BAD_REQUEST
            )
        book_ref = (investment_id, metric)
    elif source not in _FIGURE_EDITABLE_SOURCES:
        return _note_refusal(
            request, "figures", _FIGURE_SOURCE_TEXT, status_code=status.HTTP_400_BAD_REQUEST
        )

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        case = await _writable_case(
            request, db, case_id, session, section="figures", action="figure add"
        )
        if isinstance(case, HTMLResponse):
            return case
        figures = CaseFigureRepository(db)
        try:
            if book_ref is not None:
                investment_id, metric = book_ref
                investment = await InvestmentRepository(db).get_by_id(investment_id)
                if investment is None:
                    return _note_refusal(
                        request,
                        "figures",
                        _CHOOSE_INVESTMENT_TEXT,
                        status_code=status.HTTP_400_BAD_REQUEST,
                    )
                figure = await figures.add(
                    case_id,
                    label=label or f"{investment.name} {_BOOK_METRIC_LABELS[metric]}",
                    source="book",
                    source_ref={"investment_id": str(investment_id), "metric": metric},
                    now=_now(),
                )
            else:
                figure = await figures.add(
                    case_id,
                    label=label,
                    source=source,
                    value_text=_optional_text(form, "value_text"),
                    note=_optional_text(form, "note"),
                    as_of=_optional_text(form, "as_of"),
                    now=_now(),
                )
        except CaseWorkingFileInvalid as exc:
            return _note_refusal(
                request, "figures", str(exc), status_code=status.HTTP_400_BAD_REQUEST
            )
        except (CaseClosedError, CaseStateInvalid):
            return _note_refusal(request, "figures", _CLOSED_CASE_ERROR, status_code=200)
        parts = await _answer_parts(db, case_id, session, "figures", rerender=True, note=None)

    logger.info(
        "cases figure add: tenant=%s user=%s case=%s figure=%s source=%s",
        session.tenant_id,
        session.user_id,
        case_id,
        figure.id,
        source,
    )
    return _render_parts(request, parts)


@router.post("/api/cases/{case_id}/figures/{figure_id}", response_class=HTMLResponse)
async def post_case_figure(
    request: Request,
    case_id: UUID,
    figure_id: UUID,
    rerender: bool = False,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Update a figure row (D-CS-B-54): label · value_text · source · note · as_of, by presence.

    Text fields are stripped, ``None`` when empty; an empty label is the
    repository's refusal (400, verbatim). A ``book`` row takes its label and
    note only — any other field → 400, nothing saved. A typed row's source
    moves among *typed · document · conversation · external*; ``book``,
    ``kept`` or a move off a ``kept`` row → 400.

    Answers the bundle alone (the row autosave); ``rerender=1`` (the source
    select, so the chip follows) answers the block too. Last write wins with a
    note (D-CS-B-45). Another case's or an unknown figure → 404.
    """
    form = await _request_form(request)
    sent = {name for name in _FIGURE_FIELDS if name in form}

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        case = await _writable_case(
            request, db, case_id, session, section="figures", action="figure update"
        )
        if isinstance(case, HTMLResponse):
            return case
        figure = await _case_figure(db, case_id, figure_id)
        values: dict[str, Any] = {}
        if figure.source == "book":
            if sent - _BOOK_ROW_FIELDS:
                return _note_refusal(
                    request, "figures", _BOOK_ROW_TEXT, status_code=status.HTTP_400_BAD_REQUEST
                )
        else:
            if "value_text" in form:
                values["value_text"] = _optional_text(form, "value_text")
            if "as_of" in form:
                values["as_of"] = _optional_text(form, "as_of")
            if "source" in form:
                source = str(form.get("source") or "").strip()
                movable = (
                    source in _FIGURE_EDITABLE_SOURCES and figure.source in _FIGURE_EDITABLE_SOURCES
                )
                if source != figure.source and not movable:
                    return _note_refusal(
                        request,
                        "figures",
                        _FIGURE_SOURCE_TEXT,
                        status_code=status.HTTP_400_BAD_REQUEST,
                    )
                values["source"] = source
        if "label" in form:
            values["label"] = str(form.get("label") or "")
        if "note" in form:
            values["note"] = _optional_text(form, "note")
        note = _stale_note(case, form.get("stamp"))
        try:
            await CaseFigureRepository(db).update(figure_id, now=_now(), **values)
        except CaseWorkingFileInvalid as exc:
            return _note_refusal(
                request, "figures", str(exc), status_code=status.HTTP_400_BAD_REQUEST
            )
        except CaseClosedError:
            return _note_refusal(request, "figures", _CLOSED_CASE_ERROR, status_code=200)
        parts = await _answer_parts(db, case_id, session, "figures", rerender=rerender, note=note)

    logger.info(
        "cases figure update: tenant=%s user=%s case=%s figure=%s fields=%s",
        session.tenant_id,
        session.user_id,
        case_id,
        figure_id,
        ",".join(sorted(values)),
    )
    return _render_parts(request, parts)


@router.post("/api/cases/{case_id}/figures/{figure_id}/remove", response_class=HTMLResponse)
async def post_case_figure_remove(
    request: Request,
    case_id: UUID,
    figure_id: UUID,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Delete a figure (D-CS-B-54): no confirm — a figure is one line.

    A hard delete (snapshots embed figures by value). Answers the block
    without the row — the hidden placeholder when it was the last — and the
    bundle. Another case's or an unknown figure → 404.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        case = await _writable_case(
            request, db, case_id, session, section="figures", action="figure remove"
        )
        if isinstance(case, HTMLResponse):
            return case
        await _case_figure(db, case_id, figure_id)
        try:
            await CaseFigureRepository(db).delete(figure_id, now=_now())
        except (CaseClosedError, CaseStateInvalid):
            return _note_refusal(request, "figures", _CLOSED_CASE_ERROR, status_code=200)
        parts = await _answer_parts(db, case_id, session, "figures", rerender=True, note=None)

    logger.info(
        "cases figure remove: tenant=%s user=%s case=%s figure=%s",
        session.tenant_id,
        session.user_id,
        case_id,
        figure_id,
    )
    return _render_parts(request, parts)


# ---------------------------------------------------------------------------
# The editable file's writes (P-CS-3c): Conversations, Open points and the
# Decision draft, on the 3b2 helpers — ``_writable_case``, ``_case_<row>``,
# ``_answer_parts``, ``_note_refusal``, ``_stale_note`` on the update routes.
# A text autosave answers the bundle alone; a structural change (add, remove,
# a conversation's date, channel or duration, a point's done box, an option
# added, removed or chosen) re-renders its block, followed by the bundle. A
# re-rendered block carries its own note: the bundle's out-of-band note lands
# in the slot the re-render then replaces (htmx 1.9 swaps OOB content first).
# ---------------------------------------------------------------------------


async def _case_conversation(
    db: AsyncSession, case_id: UUID, conversation_id: UUID
) -> CaseConversationDTO:
    """The conversation ``conversation_id`` of ``case_id``; another case's or an unknown id → 404."""
    conversation = await CaseConversationRepository(db).get(conversation_id)
    if conversation is None or conversation.case_id != case_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
    return conversation


async def _case_point(db: AsyncSession, case_id: UUID, point_id: UUID) -> CasePointDTO:
    """The open point ``point_id`` of ``case_id``; another case's or an unknown id → 404."""
    point = await CasePointRepository(db).get(point_id)
    if point is None or point.case_id != case_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Point not found.")
    return point


def _parse_held_on(raw: str) -> date:
    """A conversation's day, ``YYYY-MM-DD`` strictly; ``ValueError`` otherwise, empty included."""
    held_on = _parse_due(raw)
    if held_on is None:
        raise ValueError(raw)
    return held_on


def _parse_minutes(raw: str) -> int | None:
    """Whole minutes (up to five ASCII digits), ``None`` when empty; ``ValueError`` otherwise."""
    if not raw:
        return None
    if not (raw.isascii() and raw.isdigit() and len(raw) <= 5):
        raise ValueError(raw)
    return int(raw)


async def _tenant_user(db: AsyncSession, raw: str) -> UUID | None:
    """A point's owner: a user of the active tenant, ``None`` when blank (D-CS-B-63).

    Raises:
        ValueError: If ``raw`` is not a user id this tenant can see — RLS
            hides another tenant's users, so they read as unknown.
    """
    text = raw.strip()
    if not text:
        return None
    user_id = UUID(text)
    if await UserRepository(db).get_by_id(user_id) is None:
        raise ValueError(text)
    return user_id


def _renumber_options(
    keys: list[str],
    texts: list[str],
    *,
    remove: str | None,
    add: bool,
    chosen: str | None,
) -> tuple[list[dict[str, str]], str | None, bool]:
    """The draft's options as posted, renumbered ``o1``, ``o2``, … in row order (D-CS-B-66).

    ``keys`` are the rows' stored keys (the form's hidden ``option_key``),
    paired with ``texts`` by position; when the two lists do not pair up the
    rows read positionally (``o1`` …). ``remove`` drops the row keyed so,
    ``add`` appends an empty row. The chosen key follows its option through
    the renumbering.

    Returns:
        ``(options, chosen_key, chosen_removed)``: the ``{key, text}`` list;
        the chosen key renumbered — ``None`` when nothing was chosen or the
        chosen option was the one removed (``chosen_removed`` is then
        ``True``), else as sent when it names no row, so the repository
        refuses it in its own words.
    """
    if len(keys) != len(texts) or not all(keys):
        keys = [f"o{index}" for index in range(1, len(texts) + 1)]
    rows: list[tuple[str | None, str]] = list(zip(keys, texts, strict=True))
    if remove is not None:
        rows = [row for row in rows if row[0] != remove]
    if add:
        rows.append((None, ""))
    options = [{"key": f"o{index}", "text": text} for index, (_, text) in enumerate(rows, 1)]
    renamed = {key: f"o{index}" for index, (key, _) in enumerate(rows, 1) if key is not None}
    if chosen is None:
        return options, None, False
    if chosen in renamed:
        return options, renamed[chosen], False
    if remove is not None and chosen == remove:
        return options, None, True
    return options, chosen, False


@router.post("/api/cases/{case_id}/conversations", response_class=HTMLResponse)
async def post_case_conversation_add(
    request: Request,
    case_id: UUID,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Add a conversation (D-CS-B-62): held on, channel, with whom, what was said.

    ``held_on`` is ``YYYY-MM-DD`` — empty is today (UTC), anything else →
    400 with ``_HELD_ON_TEXT``; ``channel`` *call* when not sent (the
    repository refuses one outside *call · meeting · other*, verbatim);
    ``what_was_promised`` and ``duration_minutes`` optional. Answers the
    block — the new row's *what was promised* focused — and the bundle.
    """
    form = await _request_form(request)
    raw_held = str(form.get("held_on") or "").strip()
    try:
        held_on = _parse_held_on(raw_held) if raw_held else _now().date()
    except ValueError:
        return _note_refusal(
            request,
            "conversations",
            _HELD_ON_TEXT.format(raw=raw_held),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    raw_minutes = str(form.get("duration_minutes") or "").strip()
    try:
        duration = _parse_minutes(raw_minutes)
    except ValueError:
        return _note_refusal(
            request,
            "conversations",
            _DURATION_TEXT.format(raw=raw_minutes),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        case = await _writable_case(
            request, db, case_id, session, section="conversations", action="conversation add"
        )
        if isinstance(case, HTMLResponse):
            return case
        try:
            conversation = await CaseConversationRepository(db).add(
                case_id,
                held_on=held_on,
                now=_now(),
                channel=str(form.get("channel") or "").strip() or "call",
                with_whom=str(form.get("with_whom") or "").strip(),
                what_was_said=str(form.get("what_was_said") or "").strip(),
                what_was_promised=_optional_text(form, "what_was_promised"),
                duration_minutes=duration,
            )
        except CaseWorkingFileInvalid as exc:
            return _note_refusal(
                request, "conversations", str(exc), status_code=status.HTTP_400_BAD_REQUEST
            )
        except (CaseClosedError, CaseStateInvalid):
            return _note_refusal(request, "conversations", _CLOSED_CASE_ERROR, status_code=200)
        parts = await _answer_parts(
            db,
            case_id,
            session,
            "conversations",
            rerender=True,
            note=None,
            block_keys={"focus_id": str(conversation.id)},
        )

    logger.info(
        "cases conversation add: tenant=%s user=%s case=%s conversation=%s",
        session.tenant_id,
        session.user_id,
        case_id,
        conversation.id,
    )
    return _render_parts(request, parts)


@router.post("/api/cases/{case_id}/conversations/{conversation_id}", response_class=HTMLResponse)
async def post_case_conversation(
    request: Request,
    case_id: UUID,
    conversation_id: UUID,
    rerender: bool = False,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Update a conversation (D-CS-B-62), partial by presence.

    ``held_on`` strict ``YYYY-MM-DD`` (anything else, empty included → 400,
    nothing saved); ``channel`` validated by the repository (its refusal →
    400 verbatim); ``with_whom`` and ``what_was_said`` stripped;
    ``what_was_promised`` stripped, ``None`` when empty; ``duration_minutes``
    whole minutes or empty for none (anything else → 400).

    Answers the bundle alone (the text autosave); ``rerender=1`` (held on,
    channel and duration — the row's stamp column reads them) answers the
    block too. Last write wins with a note (D-CS-B-45). Another case's or an
    unknown conversation → 404.
    """
    form = await _request_form(request)
    values: dict[str, Any] = {}
    if "held_on" in form:
        raw_held = str(form.get("held_on") or "").strip()
        try:
            values["held_on"] = _parse_held_on(raw_held)
        except ValueError:
            return _note_refusal(
                request,
                "conversations",
                _HELD_ON_TEXT.format(raw=raw_held),
                status_code=status.HTTP_400_BAD_REQUEST,
            )
    if "channel" in form:
        values["channel"] = str(form.get("channel") or "").strip()
    if "with_whom" in form:
        values["with_whom"] = str(form.get("with_whom") or "").strip()
    if "what_was_said" in form:
        values["what_was_said"] = str(form.get("what_was_said") or "").strip()
    if "what_was_promised" in form:
        values["what_was_promised"] = _optional_text(form, "what_was_promised")
    if "duration_minutes" in form:
        raw_minutes = str(form.get("duration_minutes") or "").strip()
        try:
            values["duration_minutes"] = _parse_minutes(raw_minutes)
        except ValueError:
            return _note_refusal(
                request,
                "conversations",
                _DURATION_TEXT.format(raw=raw_minutes),
                status_code=status.HTTP_400_BAD_REQUEST,
            )

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        case = await _writable_case(
            request, db, case_id, session, section="conversations", action="conversation update"
        )
        if isinstance(case, HTMLResponse):
            return case
        await _case_conversation(db, case_id, conversation_id)
        note = _stale_note(case, form.get("stamp"))
        try:
            await CaseConversationRepository(db).update(conversation_id, now=_now(), **values)
        except CaseWorkingFileInvalid as exc:
            return _note_refusal(
                request, "conversations", str(exc), status_code=status.HTTP_400_BAD_REQUEST
            )
        except CaseClosedError:
            return _note_refusal(request, "conversations", _CLOSED_CASE_ERROR, status_code=200)
        parts = await _answer_parts(
            db,
            case_id,
            session,
            "conversations",
            rerender=rerender,
            note=note,
            block_keys={"note": note},
        )

    logger.info(
        "cases conversation update: tenant=%s user=%s case=%s conversation=%s fields=%s",
        session.tenant_id,
        session.user_id,
        case_id,
        conversation_id,
        ",".join(sorted(values)),
    )
    return _render_parts(request, parts)


@router.post(
    "/api/cases/{case_id}/conversations/{conversation_id}/remove", response_class=HTMLResponse
)
async def post_case_conversation_remove(
    request: Request,
    case_id: UUID,
    conversation_id: UUID,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Delete a conversation (D-CS-B-62): no confirm, a hard delete (snapshots embed by value).

    Answers the block without the row — the hidden placeholder when it was
    the last — and the bundle. Another case's or an unknown conversation → 404.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        case = await _writable_case(
            request, db, case_id, session, section="conversations", action="conversation remove"
        )
        if isinstance(case, HTMLResponse):
            return case
        await _case_conversation(db, case_id, conversation_id)
        try:
            await CaseConversationRepository(db).delete(conversation_id, now=_now())
        except (CaseClosedError, CaseStateInvalid):
            return _note_refusal(request, "conversations", _CLOSED_CASE_ERROR, status_code=200)
        parts = await _answer_parts(db, case_id, session, "conversations", rerender=True, note=None)

    logger.info(
        "cases conversation remove: tenant=%s user=%s case=%s conversation=%s",
        session.tenant_id,
        session.user_id,
        case_id,
        conversation_id,
    )
    return _render_parts(request, parts)


@router.post("/api/cases/{case_id}/points", response_class=HTMLResponse)
async def post_case_point_add(
    request: Request,
    case_id: UUID,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Add an open point (D-CS-B-63): text · owner · due.

    The text is required — the repository's refusal answers 400 verbatim.
    ``owner_user_id`` blank or a user of this tenant (else 400 *Choose a user
    of this tenant.*); ``due_on`` ``YYYY-MM-DD`` or empty (else 400). Answers
    the block, the add line focused for the next point, and the bundle.
    """
    form = await _request_form(request)
    raw_due = str(form.get("due_on") or "").strip()
    try:
        due_on = _parse_due(raw_due) if raw_due else None
    except ValueError:
        return _note_refusal(
            request,
            "points",
            _POINT_DUE_TEXT.format(raw=raw_due),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        case = await _writable_case(
            request, db, case_id, session, section="points", action="point add"
        )
        if isinstance(case, HTMLResponse):
            return case
        try:
            owner = await _tenant_user(db, str(form.get("owner_user_id") or ""))
        except ValueError:
            return _note_refusal(
                request, "points", _OWNER_TEXT, status_code=status.HTTP_400_BAD_REQUEST
            )
        try:
            point = await CasePointRepository(db).add(
                case_id,
                text=str(form.get("text") or ""),
                now=_now(),
                owner_user_id=owner,
                due_on=due_on,
            )
        except CaseWorkingFileInvalid as exc:
            return _note_refusal(
                request, "points", str(exc), status_code=status.HTTP_400_BAD_REQUEST
            )
        except (CaseClosedError, CaseStateInvalid):
            return _note_refusal(request, "points", _CLOSED_CASE_ERROR, status_code=200)
        parts = await _answer_parts(
            db, case_id, session, "points", rerender=True, note=None, block_keys={"reveal": True}
        )

    logger.info(
        "cases point add: tenant=%s user=%s case=%s point=%s",
        session.tenant_id,
        session.user_id,
        case_id,
        point.id,
    )
    return _render_parts(request, parts)


@router.post("/api/cases/{case_id}/points/{point_id}", response_class=HTMLResponse)
async def post_case_point(
    request: Request,
    case_id: UUID,
    point_id: UUID,
    rerender: bool = False,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Update an open point (D-CS-B-63): text · owner · due by presence, and done.

    ``text`` as sent (the repository strips it and refuses an empty one →
    400 verbatim); ``owner_user_id`` blank or a user of this tenant (else 400
    *Choose a user of this tenant.*); ``due_on`` ``YYYY-MM-DD`` or empty for
    none (else 400). ``done`` is the checkbox and is read on the
    ``rerender=1`` path only, where the box posts: present → ``mark_done``,
    absent → ``mark_open`` — each only when it changes the point, so a
    re-tick keeps the first ``done_at``. The text autosave never touches it.

    Answers the bundle alone (text, owner and due autosave); ``rerender=1``
    answers the block too, the point struck through (``is-done``) or not.
    Last write wins with a note (D-CS-B-45). Another case's or an unknown
    point → 404.
    """
    form = await _request_form(request)
    values: dict[str, Any] = {}
    if "text" in form:
        values["text"] = str(form.get("text") or "")
    if "due_on" in form:
        raw_due = str(form.get("due_on") or "").strip()
        try:
            values["due_on"] = _parse_due(raw_due) if raw_due else None
        except ValueError:
            return _note_refusal(
                request,
                "points",
                _POINT_DUE_TEXT.format(raw=raw_due),
                status_code=status.HTTP_400_BAD_REQUEST,
            )

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        case = await _writable_case(
            request, db, case_id, session, section="points", action="point update"
        )
        if isinstance(case, HTMLResponse):
            return case
        point = await _case_point(db, case_id, point_id)
        if "owner_user_id" in form:
            try:
                values["owner_user_id"] = await _tenant_user(
                    db, str(form.get("owner_user_id") or "")
                )
            except ValueError:
                return _note_refusal(
                    request, "points", _OWNER_TEXT, status_code=status.HTTP_400_BAD_REQUEST
                )
        note = _stale_note(case, form.get("stamp"))
        points = CasePointRepository(db)
        done: bool | None = None
        try:
            if values:
                await points.update(point_id, now=_now(), **values)
            if rerender:
                if "done" in form and point.done_at is None:
                    done = True
                    await points.mark_done(point_id, now=_now())
                elif "done" not in form and point.done_at is not None:
                    done = False
                    await points.mark_open(point_id, now=_now())
        except CaseWorkingFileInvalid as exc:
            return _note_refusal(
                request, "points", str(exc), status_code=status.HTTP_400_BAD_REQUEST
            )
        except CaseClosedError:
            return _note_refusal(request, "points", _CLOSED_CASE_ERROR, status_code=200)
        parts = await _answer_parts(
            db, case_id, session, "points", rerender=rerender, note=note, block_keys={"note": note}
        )

    logger.info(
        "cases point update: tenant=%s user=%s case=%s point=%s fields=%s done=%s",
        session.tenant_id,
        session.user_id,
        case_id,
        point_id,
        ",".join(sorted(values)),
        done,
    )
    return _render_parts(request, parts)


@router.post("/api/cases/{case_id}/points/{point_id}/remove", response_class=HTMLResponse)
async def post_case_point_remove(
    request: Request,
    case_id: UUID,
    point_id: UUID,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Delete an open point (D-CS-B-63): no confirm, a hard delete while no ticket is linked.

    A point a ticket was created from is the forward half of the provenance
    chain (ADR-0138 §5): the repository's ``CasePointLinked`` refusal answers
    400 verbatim and nothing is deleted (the block offers no *Remove* on such
    a point). Answers the block — the hidden placeholder when it was the
    last — and the bundle. Another case's or an unknown point → 404.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        case = await _writable_case(
            request, db, case_id, session, section="points", action="point remove"
        )
        if isinstance(case, HTMLResponse):
            return case
        await _case_point(db, case_id, point_id)
        try:
            await CasePointRepository(db).delete(point_id, now=_now())
        except CasePointLinked as exc:
            return _note_refusal(
                request, "points", str(exc), status_code=status.HTTP_400_BAD_REQUEST
            )
        except (CaseClosedError, CaseStateInvalid):
            return _note_refusal(request, "points", _CLOSED_CASE_ERROR, status_code=200)
        parts = await _answer_parts(db, case_id, session, "points", rerender=True, note=None)

    logger.info(
        "cases point remove: tenant=%s user=%s case=%s point=%s",
        session.tenant_id,
        session.user_id,
        case_id,
        point_id,
    )
    return _render_parts(request, parts)


@router.post("/api/cases/{case_id}/decision-draft", response_class=HTMLResponse)
async def post_case_decision_draft(
    request: Request,
    case_id: UUID,
    rerender: bool = False,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Save the decision draft (D-CS-B-66) — the one upsert for the whole draft form.

    The options are the posted rows (``option_key`` + ``option_text``, in
    row order), renumbered ``o1``, ``o2``, … by :func:`_renumber_options`;
    ``add_option=1`` appends an empty row (focused on the re-render),
    ``remove_option=<key>`` drops one. ``chosen_key`` follows its option;
    when the chosen option is the one removed it is cleared, with the note
    *The chosen option was removed — choose again.* ``rationale`` and
    ``decided_by`` stripped, ``None`` when empty; ``decided_at``
    ``YYYY-MM-DD`` or empty (else 400). Everything is written by presence
    through ``upsert_draft``, which creates the case's draft when there is
    none — after a recorded decision that is a second decision (D-CS-M-3) —
    and refuses a malformed option list or a chosen key naming no option
    (400 verbatim). The draft may stay incomplete; recording it is P-CS-3c2's.

    Answers the bundle alone (option texts, rationale, decided by and at
    autosave); ``rerender=1`` (add, remove, the chosen radio) answers the
    block too. Last write wins with a note when a draft existed (D-CS-B-45).
    """
    form = await _request_form(request)
    add_option = bool(str(form.get("add_option") or "").strip())
    remove_key = str(form.get("remove_option") or "").strip() or None
    values: dict[str, Any] = {}
    if "rationale" in form:
        values["rationale"] = _optional_text(form, "rationale")
    if "decided_by" in form:
        values["decided_by"] = _optional_text(form, "decided_by")
    if "decided_at" in form:
        raw_decided = str(form.get("decided_at") or "").strip()
        try:
            values["decided_at"] = _parse_due(raw_decided) if raw_decided else None
        except ValueError:
            return _note_refusal(
                request,
                "decision",
                _DECIDED_AT_TEXT.format(raw=raw_decided),
                status_code=status.HTTP_400_BAD_REQUEST,
            )

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        case = await _writable_case(
            request, db, case_id, session, section="decision", action="decision draft"
        )
        if isinstance(case, HTMLResponse):
            return case
        decisions = CaseDecisionRepository(db)
        draft = await decisions.get_draft(case_id)
        note = _stale_note(case, form.get("stamp")) if draft is not None else None
        chosen: str | None = (
            str(form.get("chosen_key") or "").strip() or None
            if "chosen_key" in form
            else (draft.chosen_key if draft is not None else None)
        )
        focus_key: str | None = None
        if "option_text" in form or add_option or remove_key is not None:
            options, chosen, chosen_removed = _renumber_options(
                [str(key).strip() for key in form.getlist("option_key")],
                [str(text).strip() for text in form.getlist("option_text")],
                remove=remove_key,
                add=add_option,
                chosen=chosen,
            )
            values["options"] = options
            values["chosen_key"] = chosen
            if chosen_removed:
                note = _note(_CHOSEN_REMOVED_TEXT)
            if add_option and options:
                focus_key = options[-1]["key"]
        elif "chosen_key" in form:
            values["chosen_key"] = chosen
        try:
            await decisions.upsert_draft(case_id, now=_now(), **values)
        except CaseWorkingFileInvalid as exc:
            return _note_refusal(
                request, "decision", str(exc), status_code=status.HTTP_400_BAD_REQUEST
            )
        except (CaseClosedError, CaseStateInvalid):
            return _note_refusal(request, "decision", _CLOSED_CASE_ERROR, status_code=200)
        parts = await _answer_parts(
            db,
            case_id,
            session,
            "decision",
            rerender=rerender,
            note=note,
            block_keys={"note": note, "focus_key": focus_key},
        )

    logger.info(
        "cases decision draft: tenant=%s user=%s case=%s fields=%s created=%s",
        session.tenant_id,
        session.user_id,
        case_id,
        ",".join(sorted(values)),
        draft is None,
    )
    return _render_parts(request, parts)


# ---------------------------------------------------------------------------
# Milestones from the file (P-CS-3c2): Record decision, Wait until…, Resume
# now, Hand over to…, Close case…. Each goes through the one milestone path
# (D-CS-B-7) with the file frozen as it stands — ``stand`` and ``book_values``
# from :func:`_freeze_args`. The decision stays in place (the state does not
# change); the state and owner milestones answer ``HX-Redirect`` (the 3a
# close idiom), since the pill, the actions and the list all change.
# ---------------------------------------------------------------------------


def _redirect_to_case(case_id: UUID) -> HTMLResponse:
    """The full refresh of the case page after a state or owner milestone."""
    return HTMLResponse("", headers={"HX-Redirect": f"/cases/{case_id}"})


@router.post("/api/cases/{case_id}/record-decision", response_class=HTMLResponse)
async def post_case_record_decision(
    request: Request,
    case_id: UUID,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Record the draft — the ``decision_record`` milestone with the file frozen (D-CS-B-70).

    On an open or waiting case, in one transaction: Where we stand is
    composed once (:func:`_freeze_args`) and :meth:`CaseRepository.record_decision`
    writes the milestone, marks the draft recorded and freezes the file.
    Its refusals — no draft, no chosen option, no rationale (D-CS-B-10) —
    answer 400 with the message verbatim in ``#cs-note-decision``, and
    nothing is written. Answers the Decision block (the recorded card and
    *Start a new draft*), the Record block out of band (the new row and its
    *File as decided*) and the bundle. A closed case answers the calm closed
    error.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        case = await _writable_case(
            request, db, case_id, session, section="decision", action="record decision"
        )
        if isinstance(case, HTMLResponse):
            return case
        freeze = await _freeze_args(db, case)
        try:
            milestone = await CaseRepository(db).record_decision(
                case_id,
                recorded_by=session.user_id,
                now=_now(),
                stand=freeze["stand"],
                book_values=freeze["book_values"],
            )
        except CaseWorkingFileInvalid as exc:
            return _note_refusal(
                request, "decision", str(exc), status_code=status.HTTP_400_BAD_REQUEST
            )
        except (CaseClosedError, CaseStateInvalid):
            return _note_refusal(request, "decision", _CLOSED_CASE_ERROR, status_code=200)
        parts = await _answer_parts(
            db, case_id, session, "decision", rerender=True, note=None, also=("record",)
        )

    logger.info(
        "cases record decision: tenant=%s user=%s case=%s entry=%s",
        session.tenant_id,
        session.user_id,
        case_id,
        milestone.id,
    )
    return _render_parts(request, parts)


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


@router.post("/api/cases/{case_id}/wait", response_class=HTMLResponse)
async def post_case_wait(
    request: Request,
    case_id: UUID,
    wait_until: str = Form(""),
    note: str = Form(""),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Set an open case waiting until a date — the ``waiting_set`` milestone (D-CS-B-70).

    ``wait_until`` is ``YYYY-MM-DD`` and must be after today (UTC); ``note`` is
    optional (stripped, ``None`` when empty). A refusal re-renders the
    composer with the entered values and ``error`` — *The date must be after
    today.* — and writes nothing; a closed case answers the calm closed error,
    a waiting one a calm line. On success :meth:`CaseRepository.wait` moves
    the case to waiting and writes the milestone with the file frozen
    (:func:`_freeze_args`), and the answer is ``HX-Redirect: /cases/{id}``.
    """
    raw = wait_until.strip()
    clean_note = note.strip() or None

    def _wait_error(message: str) -> HTMLResponse:
        return _render(
            request,
            _COMPOSER_TEMPLATES["wait"],
            {
                "case_id": str(case_id),
                "csrf_token": session.csrf_token,
                **_wait_dates(),
                "wait_until": raw,
                "note": note,
                "error": message,
            },
        )

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        repo = CaseRepository(db)
        case = await repo.get(case_id)
        if case is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found.")
        if case.state not in _MUTABLE_STATES:
            logger.warning(
                "cases wait: closed-case write blocked tenant=%s user=%s case=%s",
                session.tenant_id,
                session.user_id,
                case_id,
            )
            return _closed_composer(request, session, case_id, _COMPOSER_TEMPLATES["wait"])
        if case.state != "open":
            return _wait_error(_WAIT_NOT_OPEN_TEXT)
        try:
            until = _parse_due(raw)
        except ValueError:
            return _wait_error(_WAIT_UNTIL_TEXT.format(raw=raw))
        if until is None or until <= _now().date():
            return _wait_error(_WAIT_PAST_TEXT)
        freeze = await _freeze_args(db, case)
        await repo.wait(
            case_id,
            wait_until=until,
            by=session.user_id,
            now=_now(),
            note=clean_note,
            stand=freeze["stand"],
            book_values=freeze["book_values"],
        )

    logger.info(
        "cases wait: tenant=%s user=%s case=%s until=%s",
        session.tenant_id,
        session.user_id,
        case_id,
        until,
    )
    return _redirect_to_case(case_id)


@router.post("/api/cases/{case_id}/resume", response_class=HTMLResponse)
async def post_case_resume(
    request: Request,
    case_id: UUID,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Lift the wait by hand — the ``waiting_lifted`` milestone, ``reason='manual'`` (D-CS-B-70).

    On a waiting case :meth:`CaseRepository.lift_wait` returns it to open with
    the file frozen (:func:`_freeze_args`), ``actor='pm'``. A case the program
    lifted meanwhile (the date, an event — the repository's race guard,
    D-CS-B-72) is open already: nothing is written and the page simply
    refreshes, as it does on success (``HX-Redirect: /cases/{id}``). A closed
    case answers the calm closed error in the Record block's note slot.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        repo = CaseRepository(db)
        case = await repo.get(case_id)
        if case is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found.")
        if case.state not in _MUTABLE_STATES:
            logger.warning(
                "cases resume: closed-case write blocked tenant=%s user=%s case=%s",
                session.tenant_id,
                session.user_id,
                case_id,
            )
            return _note_refusal(request, "record", _CLOSED_CASE_ERROR, status_code=200)
        lifted = False
        if case.state == "waiting":
            freeze = await _freeze_args(db, case)
            try:
                await repo.lift_wait(
                    case_id,
                    now=_now(),
                    by=session.user_id,
                    reason="manual",
                    stand=freeze["stand"],
                    book_values=freeze["book_values"],
                )
                lifted = True
            except CaseStateInvalid:
                logger.info("cases resume: case %s was no longer waiting — no lift.", case_id)

    logger.info(
        "cases resume: tenant=%s user=%s case=%s lifted=%s",
        session.tenant_id,
        session.user_id,
        case_id,
        lifted,
    )
    return _redirect_to_case(case_id)


@router.post("/api/cases/{case_id}/hand-over", response_class=HTMLResponse)
async def post_case_hand_over(
    request: Request,
    case_id: UUID,
    to_user_id: str = Form(""),
    note: str = Form(""),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Hand the case to another user — the ``handed_over`` milestone (ADR-0138 §13, D-CS-B-70).

    ``to_user_id`` must be one of the composer's candidates — an active user
    of this tenant who is not the owner (:func:`_handover_candidates`); any
    other value re-renders the composer with *Choose a user to hand over to.*
    and writes nothing. ``note`` is optional. On an open or waiting case
    :meth:`CaseRepository.hand_over` changes the owner (``opened_by`` stays
    as history) and writes the milestone with the file frozen; the answer is
    ``HX-Redirect: /cases/{id}``. A closed case answers the calm closed error.
    """
    clean_note = note.strip() or None
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        repo = CaseRepository(db)
        case = await repo.get(case_id)
        if case is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found.")
        if case.state not in _MUTABLE_STATES:
            logger.warning(
                "cases hand-over: closed-case write blocked tenant=%s user=%s case=%s",
                session.tenant_id,
                session.user_id,
                case_id,
            )
            return _closed_composer(request, session, case_id, _COMPOSER_TEMPLATES["handover"])
        candidates = await _handover_candidates(db, case)
        target = _parse_uuid(to_user_id.strip())
        if target is None or str(target) not in {candidate["id"] for candidate in candidates}:
            return _render(
                request,
                _COMPOSER_TEMPLATES["handover"],
                {
                    "case_id": str(case_id),
                    "csrf_token": session.csrf_token,
                    "candidates": candidates,
                    "note": note,
                    "error": _HANDOVER_TEXT,
                },
            )
        freeze = await _freeze_args(db, case)
        await repo.hand_over(
            case_id,
            to_user_id=target,
            by=session.user_id,
            now=_now(),
            note=clean_note,
            stand=freeze["stand"],
            book_values=freeze["book_values"],
        )

    logger.info(
        "cases hand-over: tenant=%s user=%s case=%s from=%s to=%s",
        session.tenant_id,
        session.user_id,
        case_id,
        case.owner_user_id,
        target,
    )
    return _redirect_to_case(case_id)


@router.post("/api/cases/{case_id}/close", response_class=HTMLResponse)
async def post_case_close(
    request: Request,
    case_id: UUID,
    closing_note: str = Form(""),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Close a case with the file frozen, then trigger a full-page refresh (D-CS-B-70).

    The closing note is stripped and required (missing → inline error);
    missing case → 404; an already-closed case → the calm error idiom. On an
    open case :meth:`CaseRepository.close` writes the state transition and
    the ``closed`` milestone with the file frozen — Where we stand and the
    ``book`` values (:func:`_freeze_args`; the 3b2 report's F-5). A waiting
    case is lifted first, in the same transaction (D-CS-B-2):
    ``lift_wait(reason='manual')`` at ``t1``, then ``close`` at
    ``t2 = max(now, t1 + 1 µs)`` — two milestones at two distinct instants,
    each frozen with the one freeze. A case the program lifted meanwhile is
    simply closed. The response carries an ``HX-Redirect`` back to the case
    so the page re-renders closed — state chip, closed metadata, closing
    note, no actions, no composers. **No Journal write** — the Journal's
    closed-case projection reads the closed case (ADR-0107 C4).
    """
    clean_note = closing_note.strip()
    engine = _engine(request)
    try:
        async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
            repo = CaseRepository(db)
            case = await repo.get(case_id)
            if case is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Case not found.",
                )
            if case.state not in _MUTABLE_STATES:
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
            freeze = await _freeze_args(db, case)
            now = _now()
            if case.state == "waiting":
                try:
                    await repo.lift_wait(
                        case_id,
                        now=now,
                        by=session.user_id,
                        reason="manual",
                        stand=freeze["stand"],
                        book_values=freeze["book_values"],
                    )
                except CaseStateInvalid:
                    logger.info("cases close: case %s was lifted meanwhile — closing.", case_id)
                now = max(_now(), now + timedelta(microseconds=1))
            await repo.close(
                case_id,
                closed_by=session.user_id,
                closing_note=clean_note,
                now=now,
                stand=freeze["stand"],
                book_values=freeze["book_values"],
            )
    except (CaseClosedError, CaseStateInvalid):
        # Closed (or gone) between the gate and the write: the transaction
        # rolled back, the lift included.
        logger.warning("cases close: case no longer closable under the write case=%s", case_id)
        return _closed_composer(request, session, case_id, _COMPOSER_TEMPLATES["close"])

    logger.info(
        "cases close: tenant=%s user=%s case=%s from=%s",
        session.tenant_id,
        session.user_id,
        case_id,
        case.state,
    )
    # Full-page refresh: the whole page changes (head chip, closed metadata,
    # actions and composers vanish). HX-Redirect is the codebase's full
    # client-side navigation idiom (web/routes/investments.py).
    return _redirect_to_case(case_id)


# ---------------------------------------------------------------------------
# File as … — the file as a milestone froze it (P-CS-3c2, D-CS-B-74)
# ---------------------------------------------------------------------------

_NOT_FROZEN_STAND_TEXT = "Where we stand was not frozen with this milestone."


def _snapshot_dict(source: Any, name: str) -> dict[str, Any]:
    """``source[name]`` when it is a dict, else ``{}`` — a snapshot read by name."""
    value = source.get(name) if isinstance(source, dict) else None
    return value if isinstance(value, dict) else {}


def _list_of(value: Any) -> list[Any]:
    """``value`` when it is a list, else ``[]``."""
    return value if isinstance(value, list) else []


def _snapshot_rows(source: Any, name: str) -> list[dict[str, Any]]:
    """The dict rows of ``source[name]`` in ``position`` order — a snapshot list read by name."""
    value = source.get(name) if isinstance(source, dict) else None
    rows = [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []
    return sorted(rows, key=lambda row: int(row.get("position") or 0))


def _snapshot_refs(refs: Any) -> list[dict[str, Any]]:
    """The dict kept-refs of a snapshot list, in stored order."""
    return [ref for ref in refs if isinstance(ref, dict)] if isinstance(refs, list) else []


async def _frozen_kept_rows(
    db_session: AsyncSession, refs: Any, owner_names: dict[UUID, str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """A snapshot's kept references rendered with their class anatomy (D-CS-B-101, D-CS-M-5).

    Each reference is resolved through :meth:`CaseKeptRepository.get`, which
    returns a removed row too, so a freeze made before a removal still
    renders its artefact. The row is projected by :func:`_project_kept_row`
    ``readonly`` — no Remove, no Edit — with the caption frozen in the
    reference; a ``generated_document`` renders the **payload frozen by
    value** in the reference, never the live row, so a later edit leaves the
    frozen text alone (ADR-0138 §9). One ``get`` per reference.

    Returns:
        ``(rows, missing)`` — the projected rows in stored order, and the
        *{class} · caption · not found* line of each reference that no
        longer resolves.
    """
    kept = CaseKeptRepository(db_session)
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for ref in _snapshot_refs(refs):
        kept_id = _parse_uuid(ref.get("kept_id"))
        resolved = await kept.get(kept_id) if kept_id is not None else None
        if resolved is None:
            label = str(ref.get("artifact") or "").replace("_", " ").capitalize() or "Kept"
            caption = str(ref.get("caption") or "")
            missing.append(
                f"{label} · {caption} · not found" if caption else f"{label} · not found"
            )
            continue
        frozen = replace(resolved, caption=ref.get("caption", resolved.caption))
        if frozen.artifact == _GENERATED_DOCUMENT and isinstance(ref.get("payload"), dict):
            frozen = replace(frozen, payload=ref["payload"])
        rows.append(_project_kept_row(frozen, owner_names, readonly=True))
    return rows, missing


def _frozen_recorded(decision: dict[str, Any]) -> dict[str, str]:
    """One recorded decision of a snapshot, read by name."""
    options = _list_of(decision.get("options"))
    chosen = next(
        (
            str(option.get("text") or "")
            for option in options
            if isinstance(option, dict) and option.get("key") == decision.get("chosen_key")
        ),
        "",
    )
    recorded_at = _as_datetime(decision.get("recorded_at"))
    return {
        "recorded": (
            f"Recorded {recorded_at.astimezone(timezone.utc):%d %b %Y %H:%M}"
            if recorded_at is not None
            else "Recorded"
        ),
        "chosen": chosen,
        "rationale": str(decision.get("rationale") or ""),
    }


async def _frozen_file_context(db_session: AsyncSession, frozen: CaseEntryDTO) -> dict[str, Any]:
    """The file as one milestone froze it, read-only (D-CS-B-74), keys read by name.

    Reads ``frozen.payload["file"]`` (:mod:`core.repositories.case_snapshot`):
    the case head as it stood (title, framing, state, counterparty, due, owner),
    the question's prose, Where we stand through :func:`_project_stand` under
    *as frozen HH:MM dd Mon YYYY*, the threads with their kept rows and the
    file-level ones in their class anatomy (:func:`_frozen_kept_rows`), the
    figures with the values frozen (a ``book`` value as applied at the
    freeze), the conversations, the open points and the decisions — recorded
    and the draft. Nothing here reads the book; the names (users — the
    keepers among them — and investments), the kept rows the references
    resolve to and the finding's subject label are the only live reads.

    Returns:
        ``{entry_id, head, close_href, case, question_title, description,
        stand_chip, stand, stand_missing, threads, kept, kept_missing,
        figures, conversations, points_title, points, recorded, draft}`` —
        each thread with its own ``kept`` and ``kept_missing``.
    """
    payload = frozen.payload or {}
    file_block = _snapshot_dict(payload, "file")
    snap_case = _snapshot_dict(file_block, "case")
    word = _FILE_AS_WORDS.get(str(payload.get("milestone_kind") or ""), "frozen")
    frozen_at = _as_datetime(payload.get("frozen_at")) or frozen.created_at
    stamp = f"{frozen_at.astimezone(timezone.utc):%H:%M %d %b %Y}"
    milestone_id = str(payload.get("milestone_entry_id") or "")
    framing = snap_case.get("framing") if snap_case.get("framing") in _FRAMING_TITLES else None
    framing = framing or "question"

    points = _snapshot_rows(file_block, "open_points")
    thread_rows = _snapshot_rows(file_block, "threads")
    all_refs = [
        ref for thread in thread_rows for ref in _snapshot_refs(thread.get("kept"))
    ] + _snapshot_refs(file_block.get("kept"))
    user_ids = {
        user_id
        for user_id in [_parse_uuid(snap_case.get("owner_user_id"))]
        + [_parse_uuid(point.get("owner_user_id")) for point in points]
        + [_parse_uuid(ref.get("kept_by")) for ref in all_refs]
        if user_id is not None
    }
    names = await _resolve_owner_names(UserRepository(db_session), user_ids)

    subject_key: str | None = None
    finding_id = _parse_uuid(snap_case.get("finding_id"))
    if finding_id is not None:
        finding = await IreneFindingRepository(db_session).get(finding_id)
        subject_key = finding.subject_key if finding is not None else None
    stand_source = file_block.get("where_we_stand")

    threads: list[dict[str, Any]] = []
    for thread in thread_rows:
        thread_kept, thread_missing = await _frozen_kept_rows(db_session, thread.get("kept"), names)
        threads.append(
            {
                "title": str(thread.get("title") or ""),
                "status_label": _THREAD_STATUS_LABELS.get(
                    str(thread.get("status") or ""), str(thread.get("status") or "").capitalize()
                ),
                "prose": str(thread.get("prose") or ""),
                "conclusion": str(thread.get("conclusion") or ""),
                "kept": thread_kept,
                "kept_missing": thread_missing,
            }
        )
    file_kept, file_missing = await _frozen_kept_rows(db_session, file_block.get("kept"), names)

    figure_rows = _snapshot_rows(file_block, "figures")
    investment_names = await _investment_names(db_session, figure_rows, {})
    figures = [
        _project_figure(
            row,
            value_text=row.get("value_text"),
            as_of=row.get("as_of"),
            names=investment_names,
            live=False,
        )
        for row in figure_rows
    ]

    conversations: list[dict[str, str]] = []
    for row in _snapshot_rows(file_block, "conversations"):
        held_on = _as_date(row.get("held_on"))
        channel = str(row.get("channel") or "")
        when = [
            _day(held_on) if held_on is not None else "—",
            _CHANNEL_LABELS.get(channel, channel.capitalize()),
        ]
        if row.get("duration_minutes") is not None:
            when.append(f"{row.get('duration_minutes')} min")
        conversations.append(
            {
                "when": " · ".join(part for part in when if part),
                "with_whom": str(row.get("with_whom") or ""),
                "what_was_said": str(row.get("what_was_said") or ""),
                "what_was_promised": str(row.get("what_was_promised") or ""),
            }
        )

    point_rows: list[dict[str, Any]] = []
    for point in points:
        owner = _parse_uuid(point.get("owner_user_id"))
        due_on = _as_date(point.get("due_on"))
        point_rows.append(
            {
                "text": str(point.get("text") or ""),
                "done": bool(point.get("done_at")),
                "owner_label": _owner_label(owner, names) if owner is not None else "",
                "due_label": _day(due_on) if due_on is not None else "",
            }
        )

    decisions = _snapshot_dict(file_block, "decisions")
    recorded_rows = _list_of(decisions.get("recorded"))
    draft = decisions.get("draft") if isinstance(decisions.get("draft"), dict) else None
    due_on = _as_date(snap_case.get("due_on"))
    wait_until = _as_date(snap_case.get("wait_until"))
    owner_id = _parse_uuid(snap_case.get("owner_user_id"))
    state = str(snap_case.get("state") or "")

    return {
        "entry_id": milestone_id,
        "head": f"File as {word} · frozen {stamp}",
        "close_href": f"{_record_file_href(frozen.case_id, milestone_id)}?close=1",
        "case": {
            "title": str(snap_case.get("title") or ""),
            "state": state,
            "state_label": state.capitalize() or "—",
            "wait_until": _day(wait_until) if wait_until is not None else "",
            "counterparty": str(snap_case.get("counterparty") or ""),
            "due": _day(due_on) if due_on is not None else "",
            "owner": _owner_label(owner_id, names) if owner_id is not None else "",
        },
        "question_title": _FRAMING_TITLES[framing],
        "description": str(snap_case.get("description") or ""),
        "stand_chip": f"as frozen {stamp}",
        "stand": (
            _project_stand(stand_source, closed=True, now=frozen_at, subject_key=subject_key)
            if isinstance(stand_source, dict)
            else None
        ),
        "stand_missing": _NOT_FROZEN_STAND_TEXT,
        "threads": threads,
        "kept": file_kept,
        "kept_missing": file_missing,
        "figures": figures,
        "conversations": conversations,
        "points_title": _POINTS_TITLES[framing],
        "points": point_rows,
        "recorded": [
            _frozen_recorded(decision) for decision in recorded_rows if isinstance(decision, dict)
        ],
        "draft": (
            {
                "options": [
                    {
                        "text": str(option.get("text") or ""),
                        "chosen": option.get("key") == draft.get("chosen_key"),
                    }
                    for option in (draft.get("options") or [])
                    if isinstance(option, dict)
                ],
                "rationale": str(draft.get("rationale") or ""),
            }
            if draft is not None
            else None
        ),
    }


@router.get("/api/cases/{case_id}/record/{entry_id}/file", response_class=HTMLResponse)
async def get_case_record_file(
    request: Request,
    case_id: UUID,
    entry_id: UUID,
    close: bool = False,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """*File as …*: the file as one milestone froze it, read-only (D-CS-B-74).

    Rendered into the record row's slot (``#cs-file-as-{entry_id}``,
    ``hx-swap="innerHTML"``). ``close=1`` — the view's *Close* — answers an
    empty body. Otherwise a 404 unless ``entry_id`` is a milestone of this
    case with a frozen pair, matched by ``payload.milestone_entry_id``
    (D-CS-B-6): another case's entry, a system line, a ``frozen`` row itself,
    a legacy milestone without a snapshot and an unknown id are all 404.
    Reading is never gated on the state, so a closed case's milestones open
    the same; nothing is written.
    """
    if close:
        return HTMLResponse("")
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        case = await CaseRepository(db).get(case_id)
        if case is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found.")
        entries = await CaseRepository(db).list_entries(case_id, include_frozen=True)
        milestone = next(
            (entry for entry in entries if entry.id == entry_id and entry.kind != "frozen"), None
        )
        frozen = _pair_frozen(entries).get(str(entry_id))
        if milestone is None or frozen is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No frozen file.")
        context = await _frozen_file_context(db, frozen)
    return _render(request, "_partials/cases_file_frozen.html", {"frozen": context})
