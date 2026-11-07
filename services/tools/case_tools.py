# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Shirley's three case read tools (ADR-0138 §10).

Read-only AI-callable tools over the Cases area, so Shirley sees what the PM
sees — across cases and across time:

- ``list_cases`` — the cases of the book, one line each, filtered by state
  (open · waiting · closed · all) and, with ``mine``, to the cases the
  signed-in user owns; through :meth:`CaseRepository.list_cases`.
- ``get_case`` — one case's working file as text, in any state, by its
  number; through :func:`services.cases.file_text.render_case_file_text`,
  the composition Shirley's case brief uses too (one function, one truth).
- ``search_cases`` — a word or phrase over what the PM wrote or kept as
  text, **closed cases included** (the archive is the institution's memory);
  through :meth:`CaseRepository.search_working_file`, the helper the Cases
  list's search uses (D-CS-M-6). Never over an attachment's contents or its
  file name (ADR-0138 §11).

All three register with :class:`~services.tool_classes.ToolClass`
``READ_INTERNAL`` at import time; their results are trusted content — the
PM's own file (ADR-0022, D-CS-B-112). The descriptions carry the behaviour:
``prompts/Soul_Shirley.md`` is byte-identical (the Soul-hash rule).

Pattern reuse (ADR-0047)
------------------------
Each tool mirrors ``analysis_tools.py``: a synchronous ``Callable[..., str]``
that reads the tool-execution context first — without one it returns the
graceful *not available in this context* string — then runs its async
workflow through
:func:`services.tools._async_bridge.run_async_in_fresh_loop` in a
loop-local, tenant-scoped session (:func:`services.tools._tool_session.tool_session`,
which carries the turn's user, ADR-0132). Users appear by name, resolved
through :class:`~core.repositories.user_repository.UserRepository` inside
that session in one batch per call; an id never reaches the text.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.repositories.case_repository import CaseDTO, CaseRepository
from core.repositories.user_repository import UserRepository
from services.cases.file_text import file_user_ids, render_case_file_text
from services.tool_classes import ToolClass
from services.tool_registry import get_tool_registry
from services.tools._async_bridge import run_async_in_fresh_loop
from services.tools._tool_context import get_tool_context
from services.tools._tool_session import tool_session

#: The three tools this module registers, in registration order.
_TOOL_NAMES: tuple[str, ...] = ("list_cases", "get_case", "search_cases")

# Returned by every tool when no consumer has populated the tool-execution
# context — the graceful-degradation path of the analysis tools.
_CONTEXT_NOT_SET_MSG = (
    "Case data is not available in this context. (The tool-execution context "
    "was not set — the case tools read the persistent database and are only "
    "available from the web chat surface.)"
)

#: The state filter's vocabulary; ``all`` reads every state.
_STATES: tuple[str, ...] = ("open", "waiting", "closed", "all")
_ALL = "all"

#: At most this many cases per ``list_cases`` call, and lines per search.
_LIST_LIMIT = 50

#: A search needs at least this many characters.
_MIN_QUERY_CHARS = 2

# The tool results' calm lines (D-5a-3).
_UNKNOWN_STATE_MSG = "Unknown state — use open, waiting, closed or all."
_SHORT_QUERY_MSG = "Give at least two characters to search for."
_UNKNOWN_CASE_MSG = "No case {badge} in this book. `list_cases` names the open ones."
_BAD_NUMBER_MSG = "Give the case number as a whole number, e.g. 31 for CASE-0031."
_NO_USER_MSG = (
    "There is no signed-in user in this context, so mine cannot be applied — "
    "call list_cases without mine."
)
_NONE = "none"
_UNKNOWN_USER = "unknown user"


# ---------------------------------------------------------------------------
# Lines
# ---------------------------------------------------------------------------


def _badge(case_number: int) -> str:
    return f"CASE-{case_number:04d}"


def _day(value: date) -> str:
    """``dd Mon YYYY``."""
    return value.strftime("%d %b %Y")


def _short(moment: datetime) -> str:
    """``dd Mon``, in UTC."""
    return f"{moment.astimezone(timezone.utc):%d %b}"


def _case_line(case: CaseDTO, names: dict[UUID, str]) -> str:
    """One case: *CASE-0031 · open · title · owner Name · updated dd Mon · due d*.

    A waiting case adds the date it returns to open, a closed case when it
    closed.
    """
    parts = [
        _badge(case.case_number),
        case.state,
        " ".join(case.title.split()),
        f"owner {names.get(case.owner_user_id) or _UNKNOWN_USER}",
        f"updated {_short(case.updated_at)}",
    ]
    if case.state == "waiting" and case.wait_until is not None:
        parts.append(f"waiting until {_day(case.wait_until)}")
    if case.state == "closed" and case.closed_at is not None:
        parts.append(f"closed {_short(case.closed_at)}")
    elif case.due_on is not None:
        parts.append(f"due {_day(case.due_on)}")
    return " · ".join(parts)


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 'es' if noun.endswith('ch') else 's'}"


def _listing(head: str, cases: list[CaseDTO], names: dict[UUID, str]) -> str:
    """The head line, then one line per case (at most :data:`_LIST_LIMIT`), or *none*."""
    if not cases:
        return f"{head}\n{_NONE}"
    lines = [head, *(_case_line(case, names) for case in cases[:_LIST_LIMIT])]
    if len(cases) > _LIST_LIMIT:
        lines.append(f"… {len(cases) - _LIST_LIMIT} more")
    return "\n".join(lines)


def _parse_case_number(raw: object) -> int | None:
    """A case number ≥ 1 from the argument: an integer, or ``"31"`` / ``"CASE-0031"``.

    The schema asks for an integer; a model that sends the badge or a numeric
    string is read the same. Anything else — a bool, a fraction, zero — is
    ``None``.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)
    if isinstance(raw, str):
        digits = raw.strip().upper().removeprefix("CASE-").strip()
        raw = int(digits) if digits.isdigit() else None
    return raw if isinstance(raw, int) and raw >= 1 else None


async def _resolve_names(session: AsyncSession, ids: Iterable[UUID]) -> dict[UUID, str]:
    """Display names by user id, one batch — the Journal idiom (display name, else email)."""
    users = UserRepository(session)
    names: dict[UUID, str] = {}
    for user_id in set(ids):
        user = await users.get_by_id(user_id)
        if user is not None:
            names[user_id] = user.display_name or user.email
    return names


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------


def list_cases(state: str = "open", mine: bool = False) -> str:
    """List the book's cases, one line each, filtered by state and owner.

    Args:
        state: ``open`` (the default), ``waiting``, ``closed`` or ``all``.
        mine: Keep only the cases the turn's signed-in user owns.

    Returns:
        A head line *n cases (state, mine)* and one line per case (at most
        fifty), or *none*; a calm line for an unknown state, a missing user
        with ``mine``, or an unset context.
    """
    ctx = get_tool_context()
    if ctx is None:
        return _CONTEXT_NOT_SET_MSG
    chosen = (state or "open").strip().lower()
    if chosen not in _STATES:
        return _UNKNOWN_STATE_MSG
    if mine and ctx.user_id is None:
        return _NO_USER_MSG
    owner = ctx.user_id if mine else None

    async def _workflow() -> tuple[list[CaseDTO], dict[UUID, str]]:
        async with tool_session(ctx) as db:
            cases = await CaseRepository(db).list_cases(
                state=None if chosen == _ALL else chosen,
                owner_user_id=owner,
                limit=_LIST_LIMIT,
            )
            return cases, await _resolve_names(db, (case.owner_user_id for case in cases))

    cases, names = run_async_in_fresh_loop(_workflow)
    scope = f"{chosen}, mine" if mine else chosen
    return _listing(f"{_count(len(cases), 'case')} ({scope})", cases, names)


def get_case(case_number: int) -> str:
    """Return one case's working file as text, in any state.

    Args:
        case_number: The case's number — ``31`` for ``CASE-0031``.

    Returns:
        The file as text (:func:`render_case_file_text`), or a calm line for
        an unknown number, a number that is no whole number, or an unset
        context.
    """
    ctx = get_tool_context()
    if ctx is None:
        return _CONTEXT_NOT_SET_MSG
    number = _parse_case_number(case_number)
    if number is None:
        return _BAD_NUMBER_MSG

    async def _workflow() -> str | None:
        async with tool_session(ctx) as db:
            case = await CaseRepository(db).get_by_number(number)
            if case is None:
                return None
            names = await _resolve_names(db, await file_user_ids(db, case))
            return await render_case_file_text(
                db, case, now=datetime.now(timezone.utc), user_names=names
            )

    text = run_async_in_fresh_loop(_workflow)
    if text is None:
        return _UNKNOWN_CASE_MSG.format(badge=_badge(number))
    return text


def search_cases(query: str, state: str = "all") -> str:
    """Search every case — closed ones included — over what the PM wrote or kept as text.

    Args:
        query: The word or phrase, at least two characters once stripped.
        state: ``all`` (the default), ``open``, ``waiting`` or ``closed``.

    Returns:
        A head line *n matches for "q"* and one line per matching case (at
        most fifty), or *none*; a calm line for a short query, an unknown
        state, or an unset context.
    """
    ctx = get_tool_context()
    if ctx is None:
        return _CONTEXT_NOT_SET_MSG
    stripped = " ".join((query or "").split())
    if len(stripped) < _MIN_QUERY_CHARS:
        return _SHORT_QUERY_MSG
    chosen = (state or _ALL).strip().lower()
    if chosen not in _STATES:
        return _UNKNOWN_STATE_MSG

    async def _workflow() -> tuple[list[CaseDTO], dict[UUID, str]]:
        async with tool_session(ctx) as db:
            cases = await CaseRepository(db).search_working_file(
                stripped, state=None if chosen == _ALL else chosen
            )
            shown = cases[:_LIST_LIMIT]
            return cases, await _resolve_names(db, (case.owner_user_id for case in shown))

    cases, names = run_async_in_fresh_loop(_workflow)
    return _listing(f'{_count(len(cases), "match")} for "{stripped}"', cases, names)


# ---------------------------------------------------------------------------
# Register tools at import time
# ---------------------------------------------------------------------------

_STATE_ENUM = list(_STATES)

_registry = get_tool_registry()

_registry.register_tool(
    name="list_cases",
    function=list_cases,
    description=(
        "List the cases of the active tenant's book, one line per case: its number "
        "(CASE-nnnn), state, title, owner, when its file was last updated, and its due "
        "date — a waiting case also the date it returns to open, a closed case when it "
        "closed. state filters to open (the default), waiting, closed or all; mine=true "
        "keeps only the cases the signed-in user owns. Use it for 'what is open / waiting "
        "/ what did we close' and to find a case's number for get_case. At most 50 cases "
        "per call. Read-only."
    ),
    parameters={
        "type": "object",
        "properties": {
            "state": {
                "type": "string",
                "enum": _STATE_ENUM,
                "description": ("Which cases: open (default), waiting, closed, or all of them."),
            },
            "mine": {
                "type": "boolean",
                "description": (
                    "When true, only the cases the signed-in user owns. Defaults to false."
                ),
            },
        },
        "required": [],
    },
    tool_class=ToolClass.READ_INTERNAL,
)

_registry.register_tool(
    name="get_case",
    function=get_case,
    description=(
        "Read one case's working file as text, by its number (31 for CASE-0031), in any "
        "state — open, waiting or closed. The text holds the question or task, where the "
        "case stands (on a closed case: as frozen at close), its threads with status, "
        "conclusion and what was kept in them (captions and quoted passages only), its "
        "figures, conversations, open points or questions to ask, the decision draft and "
        "the recorded decisions, and the record's milestones and system lines. It is "
        "digest-bounded: a long section ends with '… n more'. It is the PM's own file — "
        "cite its figures and threads where they bear on the question. Attachment contents "
        "are never read. Read-only."
    ),
    parameters={
        "type": "object",
        "properties": {
            "case_number": {
                "type": "integer",
                "minimum": 1,
                "description": "The case's number: 31 for CASE-0031.",
            },
        },
        "required": ["case_number"],
    },
    tool_class=ToolClass.READ_INTERNAL,
)

_registry.register_tool(
    name="search_cases",
    function=search_cases,
    description=(
        "Search every case of the book for a word or phrase (case-insensitive, at least "
        "two characters) and list the matching cases, one line each. Searched is what the "
        "PM wrote or kept as text: titles and descriptions, threads (title, prose, "
        "conclusion), figures (label, value, note), conversations (with whom, what was said "
        "and promised), decisions (options, rationale, decided by), closing notes, and kept "
        "captions, comments and the quoted passages of document excerpts. Closed cases are "
        "included — the archive is the institution's memory: use it for 'have we had a "
        "gated fund before?'. Attachment contents and file names are never read. state "
        "narrows to open, waiting or closed (default all); read a match with get_case. "
        "Read-only."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The word or phrase to look for, at least two characters.",
            },
            "state": {
                "type": "string",
                "enum": _STATE_ENUM,
                "description": "Which cases to search: all (default), open, waiting or closed.",
            },
        },
        "required": ["query"],
    },
    tool_class=ToolClass.READ_INTERNAL,
)
