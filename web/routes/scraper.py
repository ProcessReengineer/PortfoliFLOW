# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Reading Desk web surface under ``/assistants#reading-desk``.

The Report Scraper service (``services/scraper/``) is Qt-free and
synchronous. This module lifts it into the web layer with a multipart
upload form, a keyword editor, a read-only line naming the model the next
run will use, and an SSE stream that drives the synchronous service via
``asyncio.to_thread`` so the uvicorn loop stays responsive. The
result is rendered inline — each file as the body of its reading, through
the projection of ``web/routes/reading_desk.py`` (D-RD-2-10) — and every
successfully read file becomes a stored reading (ADR-0139 §5) through
``services/reading_desk/``. The
writer runs when the extraction service returns, independent of the
client connection (RD-D-23): a closed tab does not lose a paid reading.
Without a database engine the run view works as before and nothing is
written. A second upload of a PDF already read in the tenant waits for
the operator's confirmation before it is read again (RD-D-9).

Since ADR-0123 the endpoint, credential and model are resolved **per run,
per tenant** through the one credential façade, exactly as the chat surface
resolves them per turn (ADR-0112 §4b) — see the resolution block below. The
page therefore has no model picker: the choice lives in Admin → Providers &
Credentials (``openrouter.scraper_model``), where every other model choice
lives.

Endpoints:

* ``GET  /scraper/section``              — the Section's frame (tabs,
                                            Read pane, Library pane, the
                                            slot of a reading); HTMX lazy
                                            load.
* ``POST /scraper/runs``                 — accept multipart upload, stash
                                            a pending run, return the
                                            SSE-mount fragment, or the
                                            already-read notice when a
                                            file was read before.
* ``POST /scraper/runs/<run_id>/confirm``— *Read all again* or *Read only
                                            the new files* (``scope``):
                                            release a run held by the
                                            dedup gate.
* ``POST /scraper/runs/<run_id>/discard``— *Don't read*: drop it.
* ``GET  /scraper/runs/<run_id>/stream`` — SSE stream emitting
                                            ``progress`` / ``result`` /
                                            ``error`` / ``cancelled``.
* ``POST /scraper/runs/<run_id>/cancel`` — set the cancel flag.

A bounded LRU on ``app.state.scraper_runs`` holds pending and just-
completed runs keyed by ``run_id``; ``app.state.scraper_workers`` holds a
strong reference to every running worker task, so a worker outlives a
closed stream. Single-worker only; multi-worker deployments need Redis or
equivalent (same migration trigger as ``chat_histories`` /
``pending_turns`` in ``web/routes/chat.py``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import timezone
from typing import Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import DocumentReadingDTO
from core.repositories._session import tenant_context
from core.repositories.user_repository import UserDTO
from services.ai_service_core import ResolvedLLM
from services.auth.session import SessionDTO
from services.investments.credential_resolver import (
    CredentialResolver,
    CredentialUnavailableError,
    ProviderCredential,
)
from services.reading_desk import (
    ReadingOutcome,
    UploadedDocument,
    compose_findings,
    compose_note,
    describe_upload,
    find_earlier_readings,
    write_readings,
)
from services.scraper.bank_filter import asks_for_bank_details
from services.scraper.capabilities import (
    UnsupportedModelError,
    lookup_capability,
)
from services.scraper.models import (
    Attachment,
    Keyword,
    KeywordType,
    ReportExtraction,
    ScraperResult,
)
from services.scraper.profiles import PROFILE_IDS, ProfileError, load_profiles
from services.scraper.service import ScraperService
from web.auth import require_session, verify_csrf
from web.permissions import get_authenticated_user, require_role
from web.routes.reading_desk import (
    document_type_labels,
    language_name,
    project_findings,
    project_note,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# In-memory run store
# ---------------------------------------------------------------------------

_RUNS_LIMIT = 32
# Heartbeat for proxies with short idle timeouts; it also lets
# ``scraper_run_stream`` notice a closed tab while a file is read.
_KEEPALIVE_S = 15.0
_MAX_UPLOAD_BYTES = 64 * 1024 * 1024  # Defence-in-depth cap on a single
# multipart submission. The
# capability map's per-file MB limit
# then applies inside the service.


@dataclass
class _PendingRun:
    """One in-flight or just-completed scraper run.

    Held on ``app.state.scraper_runs`` keyed by ``run_id``. The
    ``cancel_flag`` is set by ``POST /scraper/runs/<id>/cancel`` and
    polled by the service via its ``cancel_check`` hook. The
    ``result`` is set by the SSE handler when the run completes; the
    SSE handler then renders the results partial as the payload of
    the ``result`` event.

    ``model`` is the resolved model **id** the POST saw — kept for the log
    line, not to drive the run. The :class:`ResolvedLLM` it came from is
    deliberately *not* stored (ADR-0123): a plain key must never sit in a
    process-wide store, so the SSE handler resolves again and that
    resolution is the authoritative one.

    The Reading Desk fields all carry defaults. ``documents`` describes the
    uploads by position (file name, hash, size); each attachment carries the
    same file name, so the run view and the reading show one name.
    ``tenant_id`` and ``user_id`` are the session's, for the writer.
    ``profile_id`` is the document-type preselect, ``None`` for Auto.
    ``awaiting_confirmation`` is set while the dedup gate holds the run; the
    stream refuses such a run. ``known_sha256s`` holds the hashes the gate
    found a live reading for, so *Read only the new files* can drop them.
    ``outcomes`` is set by the worker after the write, one per extraction,
    and stays ``None`` when nothing was written.
    """

    run_id: str
    session_id: str
    attachments: list[Attachment]
    keywords: list[Keyword]
    model: str
    cancel_flag: threading.Event
    result: ScraperResult | None = None
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    documents: list[UploadedDocument] = field(default_factory=list)
    tenant_id: UUID | None = None
    user_id: UUID | None = None
    profile_id: str | None = None
    awaiting_confirmation: bool = False
    known_sha256s: frozenset[str] = frozenset()
    outcomes: list[ReadingOutcome] | None = None


def _scraper_runs(request: Request) -> OrderedDict[str, _PendingRun]:
    """Return (and lazily initialise) the per-app scraper-run store.

    Bounded LRU at ``_RUNS_LIMIT`` entries. Single-worker only; a
    multi-worker deployment needs Redis or equivalent — same
    migration trigger as ``chat_histories`` / ``pending_turns``.
    """
    store = getattr(request.app.state, "scraper_runs", None)
    if store is None:
        store = OrderedDict()
        request.app.state.scraper_runs = store
    return cast("OrderedDict[str, _PendingRun]", store)


def _stash_run(request: Request, run: _PendingRun) -> None:
    """Insert ``run`` into the store, evicting the LRU entry on
    overflow."""
    store = _scraper_runs(request)
    while len(store) >= _RUNS_LIMIT:
        evicted_id, evicted_run = store.popitem(last=False)
        evicted_run.cancel_flag.set()
        logger.warning(
            "scraper: evicting pending run %s (LRU cap %d)",
            evicted_id,
            _RUNS_LIMIT,
        )
    store[run.run_id] = run


def drop_scraper_runs_for_session(request: Request, session_id: str) -> None:
    """Cancel and remove every run owned by ``session_id``.

    Called from the logout handler so a logged-out session leaves
    no in-flight runs or stale result memory behind. Safe on empty
    stores; safe on runs that were never started.
    """
    store = _scraper_runs(request)
    ids = [k for k, v in store.items() if v.session_id == session_id]
    for run_id in ids:
        run = store.pop(run_id)
        run.cancel_flag.set()


def _scraper_workers(request: Request) -> set[asyncio.Task[None]]:
    """Return (and lazily initialise) the set of running worker tasks.

    The set holds the strong reference that keeps a worker alive after its
    stream is gone (RD-D-23); each task removes itself when it is done.
    """
    workers = getattr(request.app.state, "scraper_workers", None)
    if workers is None:
        workers = set()
        request.app.state.scraper_workers = workers
    return cast("set[asyncio.Task[None]]", workers)


def _reading_engine(request: Request) -> AsyncEngine | None:
    """Return the engine the Reading Desk paths use, or ``None`` without one.

    Every reading path — the dedup lookup and the writer — goes through
    this one helper, so readings can be switched off without touching
    authentication.
    """
    return getattr(request.app.state, "engine", None)


#: Shown when the document profiles do not load; the server path stays in the log.
_PROFILES_UNAVAILABLE_LEAD = "The document profiles could not be loaded."
_PROFILES_UNAVAILABLE_SUB = "Ask an administrator to check the server log."
_PROFILES_UNAVAILABLE_MESSAGE = f"{_PROFILES_UNAVAILABLE_LEAD} {_PROFILES_UNAVAILABLE_SUB}"


def _templates(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)


# ---------------------------------------------------------------------------
# Per-run LLM resolution (ADR-0112 §4b, ADR-0123)
# ---------------------------------------------------------------------------
#
# The Scraper's credential and model are resolved **per run**, inside the
# requesting session's tenant context, through the one credential façade
# (``CredentialResolver``) — the same seam the chat surface resolves per turn
# and the tick per beat. There is no process-global "configured" state to
# consult: a tenant that writes an OpenRouter key in Admin → Providers &
# Credentials can extract on its very next run, with no restart, and its
# extractions are billed to its own key rather than to the operator's.
#
# The chains, scope-major per ADR-0112 §1:
#
# * credential — vault user → vault tenant → env ``OPENROUTER_API_KEY``;
# * model      — tenant ``scraper_model`` → tenant ``model`` → env
#   ``SCRAPER_MODEL`` → env ``SHIRLEY_MODEL`` → :data:`_DEFAULT_SCRAPER_MODEL`,
#   the exact shape of the Irene chain with the Scraper's field in Irene's
#   place, so an operator who has configured nothing keeps the pre-ADR-0123
#   behaviour;
# * base_url   — vault tenant → env ``OPENROUTER_BASE_URL`` → the
#   ``WebSettings`` default (the one field with a sane default, so it never
#   fails a run).
#
# Resolution is **never stashed** (ADR-0112 §4b, chat's D3): the POST resolves
# to fail fast, the SSE GET resolves again and *that* resolution drives the
# run. ``_PendingRun`` keeps the model **id** for its log line and its progress
# display — an id is not a secret — but never the ``ResolvedLLM``.
#
# The capability gate is unchanged (ADR-0027): ``lookup_capability`` runs on
# the resolved model before any file is touched, and a model outside the map
# is a loud refusal rather than a silent downgrade.

#: Fallback extraction model when no scope sets one — the same built-in
#: default the Irene tick carries. The capability gate below turns any
#: unsuitable resolution into a loud, actionable error, so this default is
#: never silent about *whether* it can extract.
_DEFAULT_SCRAPER_MODEL = "anthropic/claude-sonnet-4.5"

#: One operator-facing message for every "this run has no LLM" outcome. Points
#: at both scopes it can be fixed in, and deliberately says nothing about
#: restarting: tenant and user rows apply on the next run.
_NO_SCRAPER_LLM_LEAD = "The Reading Desk has no API credential for this tenant."
_NO_SCRAPER_LLM_SUB = (
    "Set an OpenRouter API key in Admin › Providers & Credentials, and a Reading "
    "Desk model there if you want one other than the default (both apply "
    "on your next run), or set OPENROUTER_API_KEY and SCRAPER_MODEL in .env "
    "for the whole application."
)
_NO_SCRAPER_LLM_MESSAGE = f"{_NO_SCRAPER_LLM_LEAD} {_NO_SCRAPER_LLM_SUB}"

#: The second line of the refusal for a model outside the capability map
#: (D-RD-2-4): the one place the Section names the supported models.
_UNSUPPORTED_MODEL_SUB = (
    "Only Anthropic models currently support PDF extraction. Set one under "
    "Admin › Providers & Credentials › Reading Desk model."
)


# ---------------------------------------------------------------------------
# Refusals (M-RD-1 lines 3166–3174, ui-standards §2.9.4)
# ---------------------------------------------------------------------------


def _refusal(lead: str, sub: str = "", *, admin_link: bool = False) -> dict[str, Any]:
    """The context of ``reading_desk_refusal.html``.

    Args:
        lead: The refusal in one sentence.
        sub: What to do about it, or ``""``.
        admin_link: Whether the way to Admin › Providers & Credentials is
            offered; the caller sets it for an owner only.
    """
    return {"lead": lead, "sub": sub, "admin_link": admin_link}


def _model_refusal(model: str, *, is_owner: bool) -> dict[str, Any]:
    """The refusal for a resolved model outside the capability map."""
    return _refusal(
        f"The model {model} cannot read PDF documents.",
        _UNSUPPORTED_MODEL_SUB,
        admin_link=is_owner,
    )


def _render_refusal(
    request: Request, refusal: dict[str, Any], *, status_code: int = 400
) -> HTMLResponse:
    """Render a refusal for the run-mount target; autoescape guards its text."""
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/reading_desk_refusal.html",
            {"refusal": refusal},
            status_code=status_code,
        ),
    )


class _ScraperUnconfiguredError(Exception):
    """This run's OpenRouter credential resolved to nothing.

    The Scraper twin of ``web.routes.chat._LLMUnconfiguredError``, translated
    the same way at both entry points — the POST into an inline 400 form
    error, the SSE stream into a single ``error`` frame.

    Only the *credential* half raises it: the model chain always terminates in
    :data:`_DEFAULT_SCRAPER_MODEL`, so "no model" is not an outcome here. An
    unsuitable model is the capability gate's refusal, which reads for itself.

    A :class:`~services.credential_vault.VaultDecryptError` is deliberately
    **not** wrapped: a vault that will not decrypt is an operator emergency,
    not a "configure me" nudge, and it must not read as a missing key.
    """


async def _resolve_scraper_model_through(resolver: CredentialResolver) -> str:
    """Walk the model chain alone — no credential, no vault secret read.

    Split out because the section render needs the *model* without needing a
    credential: showing which model the next run would use is a legitimate
    question for a tenant that has not configured a key yet, and answering it
    must not depend on one.

    Args:
        resolver: The façade, with or without a bound vault session.

    Returns:
        The resolved model id; :data:`_DEFAULT_SCRAPER_MODEL` when no scope
        sets one.
    """
    return (
        await resolver.resolve_config("openrouter", "scraper_model", scopes=("tenant",))
        or await resolver.resolve_config("openrouter", "model", scopes=("tenant",))
        or await resolver.resolve_config("openrouter", "scraper_model", scopes=("env",))
        or await resolver.resolve_config("openrouter", "model", scopes=("env",))
        or _DEFAULT_SCRAPER_MODEL
    )


async def _resolve_scraper_llm_through(
    resolver: CredentialResolver,
    request: Request,
    *,
    tenant_id: UUID | None,
    user_id: UUID | None,
) -> ResolvedLLM:
    """Walk the three chains on ``resolver`` and assemble the resolution.

    Split from :func:`_resolve_scraper_llm` so the chain reads in one place,
    independent of whether a vault-backed session was available.

    Raises:
        _ScraperUnconfiguredError: If no source holds a credential.
    """
    try:
        credential = await resolver.resolve("openrouter", tenant_id=tenant_id, user_id=user_id)
    except CredentialUnavailableError as exc:
        raise _ScraperUnconfiguredError(_NO_SCRAPER_LLM_MESSAGE) from exc
    if not isinstance(credential, ProviderCredential):
        # openrouter declares a secret field and is not optional, so the
        # resolver raises rather than returning NoCredential. Defensive.
        raise _ScraperUnconfiguredError(_NO_SCRAPER_LLM_MESSAGE)

    model = await _resolve_scraper_model_through(resolver)
    settings = request.app.state.settings
    base_url = (
        await resolver.resolve_config("openrouter", "base_url") or settings.openrouter_base_url
    )
    return ResolvedLLM(base_url=base_url, api_key=credential.payload["api_key"], model=model)


async def _resolve_scraper_model(request: Request, session: SessionDTO) -> str:
    """Resolve the model the next run will use, inside the tenant's context.

    Without a database engine (a DB-less test rig, a contributor laptop) the
    resolver is built without a session and the environment is the only
    source — the same graceful degradation ``chat.py``'s ``_resolve_llm``
    takes.

    Args:
        request: The active request (carries ``app.state``).
        session: The authenticated session, supplying the tenant.

    Returns:
        The resolved model id.
    """
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        return await _resolve_scraper_model_through(CredentialResolver())
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        return await _resolve_scraper_model_through(CredentialResolver(session=db))


async def _resolve_scraper_llm(request: Request, session: SessionDTO) -> ResolvedLLM:
    """Resolve this run's endpoint, credential and model (ADR-0123).

    Runs inside the session's ``tenant_context`` so the vault sources see
    exactly the rows RLS allows this tenant, with the user axis carried for
    the user-scope credential rows.

    Args:
        request: The active request (carries ``app.state`` and settings).
        session: The authenticated session, supplying tenant and user.

    Returns:
        The :class:`~services.ai_service_core.ResolvedLLM` for this run.

    Raises:
        _ScraperUnconfiguredError: If no source holds a credential.
        VaultDecryptError: Propagated untouched — a wrong or rotated master
            key must never look like an absent credential.
    """
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        return await _resolve_scraper_llm_through(
            CredentialResolver(), request, tenant_id=None, user_id=None
        )
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        return await _resolve_scraper_llm_through(
            CredentialResolver(session=db),
            request,
            tenant_id=session.tenant_id,
            user_id=session.user_id,
        )


# ---------------------------------------------------------------------------
# GET /scraper/section
# ---------------------------------------------------------------------------


@router.get("/scraper/section", response_class=HTMLResponse)
async def scraper_section(
    request: Request,
    reading: int | None = None,
    session: SessionDTO = Depends(require_session),
    user: UserDTO = Depends(get_authenticated_user),
) -> HTMLResponse:
    """Return the Reading Desk Section's frame for HTMX area-section loads.

    The frame (``reading_desk_frame.html``, ADR-0139 §7) holds the tabs,
    the Read pane with the run form, the Library pane, which loads through
    ``web/routes/reading_desk.py``, and the slot a reading opens in. Every
    role sees the frame; the Read tab and its pane are rendered for owner
    and member only (D-RD-2-2), so the Section opens on Read for them and
    on Library for everyone else. With ``reading`` set the frame opens on
    that reading — the page address ``/assistants/readings/<n>``.

    For the Read pane it shows the model the next run will use — resolved
    through the config chain only, so a tenant with no credential yet still
    sees which model it would extract with — and runs the capability gate
    on it. A model outside the map renders a refusal in place of the form:
    submitting would fail on the first file anyway, and saying so before the
    upload is the honest order. The credential is not consulted here
    (ADR-0123 §4); a tenant without one is refused on submit.

    The keyword editor starts empty (D-RD-1-1): the document type's fields
    are read anyway, and the editor holds only extra keywords. The document
    profiles feed the type preselect; when they do not load, a refusal
    replaces the form the same way, and the log names the exception type
    only (its text carries a server path). None of this is computed for a
    role without the Read pane.
    """
    templates = _templates(request)
    can_read = user.has_role("owner", "member")
    context: dict[str, Any] = {
        "can_read": can_read,
        "initial_tab": "read" if can_read else "library",
        "reading_number": reading,
    }
    if can_read:
        model = await _resolve_scraper_model(request, session)
        refusal: dict[str, Any] | None = None
        try:
            lookup_capability(model)
        except UnsupportedModelError:
            refusal = _model_refusal(model, is_owner=user.has_role("owner"))
        profiles: list[tuple[str, str]] = []
        try:
            loaded = load_profiles()
        except ProfileError as exc:
            logger.error("scraper: document profiles not loaded (%s)", type(exc).__name__)
            if refusal is None:
                refusal = _refusal(_PROFILES_UNAVAILABLE_LEAD, _PROFILES_UNAVAILABLE_SUB)
        else:
            profiles = [(profile_id, loaded[profile_id].label) for profile_id in PROFILE_IDS]
        context.update(
            {
                "csrf_token": session.csrf_token,
                "scraper_model": model,
                "profiles": profiles,
                "refusal": refusal,
            }
        )
    return cast(
        HTMLResponse,
        templates.TemplateResponse(request, "_partials/reading_desk_frame.html", context),
    )


# ---------------------------------------------------------------------------
# POST /scraper/runs
# ---------------------------------------------------------------------------
#
# Submission shape (Option B from the spec — JSON-encoded keywords):
#     - pdf: list[UploadFile], multipart, accept="application/pdf"
#     - keywords_json: str, JSON-serialised
#       [{"name": "...", "type": "Number|Percentage|Date|Text|List"}, ...]
#       — the extra keywords; an empty list is a run on the profile alone
#     - profile_id: str, "" for Auto or one of PROFILE_IDS
#     - csrf_token: str (validated by verify_csrf)
#
# The JSON payload is more natural for the dynamic keyword editor —
# the client-side serialisation happens once on submit, instead of
# round-tripping two parallel form-field lists and zipping by index
# on the server.


def _parse_keywords_json(payload: str) -> list[Keyword]:
    """Parse the submitted ``keywords_json`` field into ``Keyword`` records.

    An empty list is valid: the run reads the document type's fields and
    no extra keyword. A keyword that asks for a bank detail is refused
    (RD-D-5).

    Raises ``ValueError`` with an operator-readable message when the
    payload is malformed. The exception text is rendered (escaped)
    inline in the form's error panel.
    """
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Could not parse keywords ({exc.msg}).") from exc
    if not isinstance(raw, list):
        raise ValueError("Keywords must be a list.")
    out: list[Keyword] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"Keyword #{idx + 1} is not an object.")
        name = str(item.get("name", "") or "").strip()
        type_str = str(item.get("type", "") or "").strip()
        if not name:
            raise ValueError(f"Keyword #{idx + 1} has no name.")
        try:
            kw_type = KeywordType(type_str)
        except ValueError as exc:
            raise ValueError(
                f"Keyword #{idx + 1} ({name!r}) has an unknown type: {type_str!r}."
            ) from exc
        if asks_for_bank_details(name):
            raise ValueError(
                f'Keyword "{name}" asks for bank details. '
                "The Reading Desk never reads bank details."
            )
        out.append(Keyword(name=name, type=kw_type))
    return out


def _render_form_error(request: Request, message: str) -> HTMLResponse:
    """Render a refused submission for the run-mount target, as a 400.

    The message is escaped by the template: it can echo a submitted keyword
    name.
    """
    return _render_refusal(request, _refusal(message))


def _render_run_mount(
    request: Request,
    run: _PendingRun,
    csrf_token: str,
    *,
    repeated: list[str] | None = None,
) -> HTMLResponse:
    """Render the run-mount fragment whose stream URL ``scraper.js`` opens.

    The mount starts on the progress frame of the run's first file.
    ``repeated`` holds one sentence per file attached more than once; it is
    passed when the mount is the upload's own answer, not after a confirm,
    where the already-read notice has said it.
    """
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/scraper_run_mount.html",
            {
                "run_id": run.run_id,
                "csrf_token": csrf_token,
                "filename": run.attachments[0].filename if run.attachments else "",
                "total": len(run.attachments),
                "repeated": repeated or [],
            },
        ),
    )


def _dedup_rows(
    documents: list[UploadedDocument], earlier: dict[str, list[DocumentReadingDTO]]
) -> list[dict[str, Any]]:
    """One row per gated upload: its name, the newest live reading, the rest.

    ``earlier`` maps a hash to its live readings, newest first, as
    :func:`~services.reading_desk.find_earlier_readings` returns them.
    """
    rows: list[dict[str, Any]] = []
    for document in documents:
        readings = earlier.get(document.sha256)
        if not readings:
            continue
        newest = readings[0]
        rows.append(
            {
                "filename": document.filename,
                "read_on": newest.read_at.astimezone(timezone.utc).date().isoformat(),
                "label": newest.label,
                "number": newest.reading_number,
                "earlier": len(readings) - 1,
            }
        )
    return rows


def _dedup_lead(known: int, total: int) -> str:
    """How many of an upload's files have been read before, as one sentence."""
    if total == 1:
        return "This file has been read before."
    if known == total:
        return f"All {total} files have been read before."
    verb = "has" if known == 1 else "have"
    return f"{known} of these {total} files {verb} been read before."


def _repeated_lines(repeats: dict[str, int], documents: list[UploadedDocument]) -> list[str]:
    """One sentence per file attached more than once, in upload order.

    ``repeats`` maps a hash to the number of times its bytes were attached;
    the sentence names the file by the name it is read under, the first one.
    """
    lines: list[str] = []
    for document in documents:
        times = repeats.get(document.sha256, 1)
        if times < 2:
            continue
        count = "twice" if times == 2 else f"{times} times"
        lines.append(f"{document.filename} is attached {count}; it is read once.")
    return lines


@router.post(
    "/scraper/runs",
    response_class=HTMLResponse,
    dependencies=[Depends(require_role("owner", "member"))],
)
async def scraper_run_start(
    request: Request,
    pdf: list[UploadFile] = File(...),
    keywords_json: str = Form(...),
    profile_id: str = Form(""),
    session: SessionDTO = Depends(require_session),
    user: UserDTO = Depends(get_authenticated_user),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Accept the upload, validate, stash a pending run, return the
    SSE-mount fragment — or the already-read notice.

    The fragment carries ``data-pf-scraper-sse-url`` and
    ``data-pf-scraper-run-id`` so ``scraper.js`` can open the
    EventSource and wire the cancel button.

    The run's LLM is resolved here to **fail fast** (ADR-0123): an
    unconfigured tenant, or one whose resolved model cannot read PDFs, learns
    it from an inline 400 rather than from a half-open SSE stream. Only the
    model *id* is stashed; the SSE handler resolves again and that second
    resolution is what drives the run.

    Dedup gate (RD-D-9): when a file of the upload already has a live
    reading in the tenant, the run is stashed awaiting confirmation and the
    response asks what to read (D-RD-2-5); the files stay in the run store,
    so neither answer needs a second upload. The gate is a courtesy: a failed
    lookup is logged and the run proceeds without it.

    A file attached more than once in one upload — the same bytes, under any
    name — is read once, under its first name, and the answer says so
    (D-RD-2-5, superseding D-RD-1-3).
    """
    if not pdf:
        return _render_form_error(request, "Please attach at least one PDF.")

    # Parse keywords first — cheap and lets us reject malformed input
    # before reading the upload bodies into memory.
    try:
        keywords = _parse_keywords_json(keywords_json)
    except ValueError as exc:
        return _render_form_error(request, str(exc))

    selected_profile: str | None = profile_id or None
    if selected_profile is not None and selected_profile not in PROFILE_IDS:
        return _render_form_error(request, "Unknown document type.")

    # Resolve before the bodies are read: a tenant with no credential, or one
    # whose model cannot take PDF input, should not pay for the upload first.
    is_owner = user.has_role("owner")
    try:
        llm = await _resolve_scraper_llm(request, session)
    except _ScraperUnconfiguredError:
        return _render_refusal(
            request,
            _refusal(_NO_SCRAPER_LLM_LEAD, _NO_SCRAPER_LLM_SUB, admin_link=is_owner),
        )
    try:
        lookup_capability(llm.model)
    except UnsupportedModelError:
        return _render_refusal(request, _model_refusal(llm.model, is_owner=is_owner))

    # Read every upload body into memory. The scraper service is
    # synchronous and consumes bytes; we hold each file in a single
    # bytes object so the async loop is not blocked during the read.
    attachments: list[Attachment] = []
    documents: list[UploadedDocument] = []
    repeats: dict[str, int] = {}
    total_bytes = 0
    for upload in pdf:
        data = await upload.read()
        total_bytes += len(data)
        if total_bytes > _MAX_UPLOAD_BYTES:
            return _render_form_error(
                request,
                f"Combined upload exceeds {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
            )
        # One name for the run view and the reading: the sanitised one,
        # ``untitled.pdf`` when nothing is left of it.
        document = describe_upload(upload.filename or "", data)
        repeats[document.sha256] = repeats.get(document.sha256, 0) + 1
        if repeats[document.sha256] > 1:
            # The same bytes again: read once, under the first name.
            continue
        documents.append(document)
        attachments.append(
            Attachment(
                filename=document.filename,
                mime_type=upload.content_type or "application/pdf",
                data=data,
            )
        )

    run_id = uuid.uuid4().hex
    run = _PendingRun(
        run_id=run_id,
        session_id=str(session.id),
        attachments=attachments,
        keywords=keywords,
        model=llm.model,
        cancel_flag=threading.Event(),
        documents=documents,
        tenant_id=session.tenant_id,
        user_id=session.user_id,
        profile_id=selected_profile,
    )
    _stash_run(request, run)
    repeated = _repeated_lines(repeats, documents)

    engine = _reading_engine(request)
    if engine is not None:
        try:
            earlier = await find_earlier_readings(
                engine,
                tenant_id=session.tenant_id,
                user_id=session.user_id,
                sha256s=[document.sha256 for document in documents],
            )
        except Exception as exc:  # noqa: BLE001 - the gate is a courtesy; the run proceeds
            logger.warning(
                "scraper: earlier readings of run %s not looked up (%s)",
                run_id,
                type(exc).__name__,
            )
            earlier = {}
        if earlier:
            run.awaiting_confirmation = True
            run.known_sha256s = frozenset(earlier)
            rows = _dedup_rows(documents, earlier)
            return cast(
                HTMLResponse,
                _templates(request).TemplateResponse(
                    request,
                    "_partials/scraper_dedup.html",
                    {
                        "run_id": run_id,
                        "csrf_token": session.csrf_token,
                        "lead": _dedup_lead(len(rows), len(documents)),
                        "rows": rows,
                        "repeated": repeated,
                        "offer_new": len(rows) < len(documents),
                    },
                ),
            )

    return _render_run_mount(request, run, session.csrf_token, repeated=repeated)


# ---------------------------------------------------------------------------
# POST /scraper/runs/{run_id}/confirm and /discard  (the dedup gate)
# ---------------------------------------------------------------------------


@router.post(
    "/scraper/runs/{run_id}/confirm",
    response_class=HTMLResponse,
    dependencies=[Depends(require_role("owner", "member"))],
)
async def scraper_run_confirm(
    run_id: str,
    request: Request,
    scope: str = Form("all"),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Release a run the dedup gate holds (RD-D-9, D-RD-2-5).

    ``scope`` is ``all`` — *Read all again*: every file of the upload is
    read and gets a new reading — or ``new`` — *Read only the new files*:
    the files with a live reading are dropped from the run first. Any other
    value, and ``new`` when every file is known, answer 400 and leave the
    run held.

    Returns the run-mount fragment, on whose swap ``scraper.js`` opens the
    stream. Session-isolated: another session's run, a missing run and a
    run not awaiting confirmation all answer 404 with a refusal fragment,
    which ``scraper.js`` swaps into ``#scraper-run`` (a confirm from a
    second tab, an evicted run).
    """
    run = _scraper_runs(request).get(run_id)
    if run is None or run.session_id != str(session.id) or not run.awaiting_confirmation:
        return _render_refusal(
            request,
            _refusal("This upload is no longer waiting to be read.", "Upload the files again."),
            status_code=status.HTTP_404_NOT_FOUND,
        )
    if scope not in ("all", "new"):
        return _render_form_error(request, "Unknown choice.")
    if scope == "new":
        kept = [
            index
            for index, document in enumerate(run.documents)
            if document.sha256 not in run.known_sha256s
        ]
        if not kept:
            return _render_form_error(request, "Every file of this upload has been read before.")
        run.attachments = [run.attachments[index] for index in kept]
        run.documents = [run.documents[index] for index in kept]
    run.awaiting_confirmation = False
    return _render_run_mount(request, run, session.csrf_token)


@router.post(
    "/scraper/runs/{run_id}/discard",
    response_class=HTMLResponse,
    dependencies=[Depends(require_role("owner", "member"))],
)
async def scraper_run_discard(
    run_id: str,
    request: Request,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """*Don't read*: drop the session's run; nothing is read or written.

    A no-op for a missing run or another session's run; the answer is the
    same either way.
    """
    store = _scraper_runs(request)
    run = store.get(run_id)
    if run is not None and run.session_id == str(session.id):
        del store[run_id]
        run.cancel_flag.set()
    return HTMLResponse('<p class="pf-hint">Nothing was read.</p>')


# ---------------------------------------------------------------------------
# POST /scraper/runs/{run_id}/cancel
# ---------------------------------------------------------------------------


@router.post(
    "/scraper/runs/{run_id}/cancel",
    status_code=204,
    dependencies=[Depends(require_role("owner", "member"))],
)
async def scraper_run_cancel(
    run_id: str,
    request: Request,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> Response:
    """Set the cancel flag for an in-flight run.

    Session-isolated: a request for someone else's run is treated as
    a no-op (the lookup misses), so cross-session cancel attempts
    cannot disrupt another operator's run.
    """
    run = _scraper_runs(request).get(run_id)
    if run is not None and run.session_id == str(session.id):
        run.cancel_flag.set()
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# GET /scraper/runs/{run_id}/stream  (SSE)
# ---------------------------------------------------------------------------


def _format_sse(event_name: str, data: str) -> str:
    """Encode one SSE frame.

    Multi-line data is split across multiple ``data:`` lines per the
    SSE specification — mirrors ``web.routes.chat._format_sse``.
    """
    lines = [f"event: {event_name}"]
    for piece in data.splitlines() or [""]:
        lines.append(f"data: {piece}")
    return "\n".join(lines) + "\n\n"


_READING_LABEL = re.compile(r"R-(\d+)")

_VALUES_NOT_CHECKED = (
    "Values and quotes are the model's reading of the documents and are not checked against them."
)


def _format_elapsed(seconds: float) -> str:
    """``m:ss`` — minutes unpadded, seconds two digits, as ``scraper.js`` ticks it."""
    whole = max(0, int(seconds))
    return f"{whole // 60}:{whole % 60:02d}"


def _files(count: int) -> str:
    return "1 file" if count == 1 else f"{count} files"


def _project_result(
    extraction: ReportExtraction,
    outcome: ReadingOutcome | None,
    *,
    stored: bool,
    labels: dict[str, str],
) -> dict[str, Any]:
    """One result block of ``scraper_results.html`` (D-RD-2-10).

    Args:
        extraction: What the service returned for the file.
        outcome: What the writer made of it, or ``None``.
        stored: Whether the run wrote readings at all (an engine exists).
        labels: The document-type labels by profile id.

    Returns:
        ``state`` — ``failed`` (not read), ``saved``, ``unsaved`` (read, the
        write failed) or ``read`` (nothing is written without an engine) —
        with ``filename`` and ``sub``, and by state ``error``, or ``label``,
        ``number``, ``proposal``, ``findings`` and ``note``.
    """
    if extraction.error:
        return {
            "state": "failed",
            "filename": extraction.filename,
            "sub": "Not saved",
            "error": extraction.error,
        }
    label = outcome.label if outcome is not None else None
    head = [
        labels.get(extraction.document_type, extraction.document_type),
        extraction.fund_name,
        extraction.period,
    ]
    if label:
        state = "saved"
        head.insert(0, f"Saved as {label}")
    elif stored:
        state = "unsaved"
        head.insert(0, "Not saved")
    else:
        state = "read"
    number = _READING_LABEL.fullmatch(label) if label else None
    proposal = outcome.proposal if outcome is not None and label else None
    return {
        "state": state,
        "filename": extraction.filename,
        "sub": " · ".join(part.strip() for part in head if part and part.strip()),
        "label": label,
        "number": int(number.group(1)) if number else None,
        "proposal": {"kind": proposal.kind, "name": proposal.name} if proposal else None,
        "findings": project_findings(compose_findings(extraction)),
        "note": project_note(compose_note(extraction), language_name(extraction.language or None)),
    }


def _closing(
    results: list[dict[str, Any]],
    *,
    total: int,
    cancelled: bool,
    elapsed: str,
    stored: bool,
) -> dict[str, Any]:
    """The closing line of a run (D-RD-2-6): what was read, in what time, what became of it."""
    states = [result["state"] for result in results]
    failed = states.count("failed")
    unsaved = states.count("unsaved")
    parts: list[str] = []
    if stored:
        parts.append(f"{states.count('saved')} saved")
    if failed:
        parts.append(f"{failed} not read")
    if unsaved:
        parts.append(f"{unsaved} not saved")
    if cancelled and not results:
        lead = "Run cancelled. No file was read."
    else:
        counted = f"{len(results)} of {_files(total)}" if cancelled else _files(len(results))
        lead = f"{counted} read in {elapsed}"
        if parts:
            lead += " — " + ", ".join(parts)
        lead += "."
        if cancelled:
            lead = f"Run cancelled. {lead}"
    return {
        "lead": lead,
        "sub": _VALUES_NOT_CHECKED if len(results) > failed else "",
        "cancelled": cancelled,
    }


def _render_results_fragment(
    request: Request,
    result: ScraperResult | None,
    outcomes: list[ReadingOutcome] | None,
    *,
    total: int,
    elapsed_s: float,
) -> str:
    """Render the results partial as a self-contained HTML fragment.

    ``outcomes`` is one :class:`~services.reading_desk.ReadingOutcome` per
    extraction, aligned by position, or ``None`` when nothing was written.
    ``total`` is the number of files in the run and ``elapsed_s`` the time
    since its worker started, for the closing line.
    """
    extractions: list[ReportExtraction] = list(result.extractions) if result is not None else []
    cancelled = bool(result.cancelled) if result is not None else False
    stored = outcomes is not None
    labels = document_type_labels()
    results = [
        _project_result(
            extraction,
            outcomes[index] if outcomes is not None and index < len(outcomes) else None,
            stored=stored,
            labels=labels,
        )
        for index, extraction in enumerate(extractions)
    ]
    return (
        _templates(request)
        .get_template("_partials/scraper_results.html")
        .render(
            closing=_closing(
                results,
                total=total,
                cancelled=cancelled,
                elapsed=_format_elapsed(elapsed_s),
                stored=stored,
            ),
            results=results,
        )
    )


def _render_progress_fragment(
    request: Request,
    *,
    phase: Literal["reading", "finishing"],
    current: int,
    filename: str,
    done: int,
    total: int,
    csrf_token: str,
    run_id: str,
) -> str:
    """Render the progress partial for a single SSE progress frame.

    Args:
        request: The request whose app holds the templates.
        phase: ``"reading"`` while a file is being read — the frame names
            it and carries the Cancel button; ``"finishing"`` once every
            file is read and the readings are being written — no Cancel.
        current: The 1-based index of the file being read.
        filename: The name of the file being read.
        done: Files read so far; the bar stands at ``done / total``.
        total: Files in the run.
        csrf_token: For the Cancel button.
        run_id: For the Cancel URL.

    Returns:
        The rendered ``scraper_progress.html`` fragment.
    """
    templates = _templates(request)
    return templates.get_template("_partials/scraper_progress.html").render(
        phase=phase,
        current=current,
        filename=filename,
        done=done,
        total=total,
        percent=round(done / total * 100) if total else 0,
        csrf_token=csrf_token,
        run_id=run_id,
    )


async def _drive_run(
    request: Request,
    run: _PendingRun,
    csrf_token: str,
    llm: ResolvedLLM,
) -> AsyncGenerator[str, None]:
    """Run the scraper in a worker thread, yield SSE frames.

    The service's ``progress_callback`` is invoked from the worker
    thread; ``call_soon_threadsafe`` re-enters the event loop with a
    ``queue.put_nowait`` so the SSE generator can pick the events up
    in order. ``cancel_check`` polls the run's flag.

    ``llm`` is the caller's resolution for this run (ADR-0123) — it travels
    as an argument rather than off ``run`` so nothing of it outlives the
    stream.

    The worker writes the run's readings when the service returns and
    before ``done`` is queued (RD-D-23). It is registered on
    ``app.state.scraper_workers`` and awaited through :func:`asyncio.shield`,
    so a stream that closes or is cancelled while the service still reads
    neither cancels the worker nor loses the readings already paid for: the
    cancel flag stops the run before its next file, and the completed prefix
    is written.

    The frames name the file being read, not the one just finished: the
    service reports a file after reading it, so the route takes the order
    from ``run.attachments``. The first frame, sent once the worker is
    registered, names the first file; each reported file names the next
    one, and the report of the last file turns the frame to *finishing*
    (the readings are being written).

    While no event is queued, an SSE comment ``: keep-alive`` is sent every
    ``_KEEPALIVE_S`` seconds. One pending ``queue.get()`` is held across the
    heartbeats and never cancelled between them, so no event is lost; it is
    cancelled when the stream closes, before the worker is awaited.
    """
    queue: asyncio.Queue[dict[str, str]] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def progress(done: int, total: int, filename: str) -> None:
        loop.call_soon_threadsafe(
            queue.put_nowait,
            {
                "event": "progress",
                "data": json.dumps({"done": done, "total": total, "filename": filename}),
            },
        )

    def cancel_check() -> bool:
        return run.cancel_flag.is_set()

    async def _run_in_thread() -> None:
        try:
            result = await asyncio.to_thread(
                ScraperService().scrape_reports,
                attachments=run.attachments,
                keywords=run.keywords,
                llm=llm,
                progress_callback=progress,
                cancel_check=cancel_check,
                profile_id=run.profile_id,
            )
            run.result = result
            engine = _reading_engine(request)
            if engine is not None and run.tenant_id is not None and run.user_id is not None:
                try:
                    run.outcomes = await write_readings(
                        engine,
                        tenant_id=run.tenant_id,
                        read_by=run.user_id,
                        documents=run.documents,
                        result=result,
                        keywords=run.keywords,
                        model=llm.model,
                    )
                except Exception as exc:  # noqa: BLE001 - the run still shows its result
                    logger.error(
                        "scraper: readings of run %s not written (%s)",
                        run.run_id,
                        type(exc).__name__,
                    )
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {"event": "done", "data": ""},
            )
        except UnsupportedModelError as exc:
            run.error = str(exc)
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {
                    "event": "error",
                    "data": json.dumps({"message": run.error}),
                },
            )
        except ProfileError as exc:
            # The text names a server path: the log gets the type, the
            # operator a plain sentence.
            run.error = _PROFILES_UNAVAILABLE_MESSAGE
            logger.error(
                "scraper: run %s stopped, document profiles not loaded (%s)",
                run.run_id,
                type(exc).__name__,
            )
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {
                    "event": "error",
                    "data": json.dumps({"message": run.error}),
                },
            )
        except Exception as exc:  # noqa: BLE001 — surface as SSE error
            run.error = f"{type(exc).__name__}: {exc}"
            logger.exception("scraper: run %s failed", run.run_id)
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {
                    "event": "error",
                    "data": json.dumps({"message": run.error}),
                },
            )

    started = time.monotonic()
    worker = asyncio.create_task(_run_in_thread())
    workers = _scraper_workers(request)
    workers.add(worker)
    worker.add_done_callback(workers.discard)

    getter: asyncio.Task[dict[str, str]] | None = None
    try:
        if run.attachments:
            yield _format_sse(
                "progress",
                _render_progress_fragment(
                    request,
                    phase="reading",
                    current=1,
                    filename=run.attachments[0].filename,
                    done=0,
                    total=len(run.attachments),
                    csrf_token=csrf_token,
                    run_id=run.run_id,
                ),
            )
        while True:
            if getter is None:
                getter = asyncio.create_task(queue.get())
            finished, _pending = await asyncio.wait({getter}, timeout=_KEEPALIVE_S)
            if not finished:
                yield ": keep-alive\n\n"
                continue
            event = getter.result()
            getter = None
            kind = event["event"]
            if kind == "progress":
                payload = json.loads(event["data"])
                done = int(payload["done"])
                total = int(payload["total"])
                if done < total and done < len(run.attachments):
                    fragment = _render_progress_fragment(
                        request,
                        phase="reading",
                        current=done + 1,
                        filename=run.attachments[done].filename,
                        done=done,
                        total=total,
                        csrf_token=csrf_token,
                        run_id=run.run_id,
                    )
                else:
                    fragment = _render_progress_fragment(
                        request,
                        phase="finishing",
                        current=done,
                        filename=str(payload["filename"]),
                        done=done,
                        total=total,
                        csrf_token=csrf_token,
                        run_id=run.run_id,
                    )
                yield _format_sse("progress", fragment)
                continue
            if kind == "done":
                # The service ran to completion. Distinguish between
                # "user pressed Cancel mid-run" (the service sets
                # ``result.cancelled`` and returns early) and an
                # ordinary completion.
                fragment = _render_results_fragment(
                    request,
                    run.result,
                    run.outcomes,
                    total=len(run.attachments),
                    elapsed_s=time.monotonic() - started,
                )
                if run.result is not None and run.result.cancelled:
                    yield _format_sse("cancelled", fragment)
                else:
                    yield _format_sse("result", fragment)
                return
            if kind == "error":
                payload = json.loads(event["data"])
                yield _format_sse("error", str(payload.get("message", "Unknown error.")))
                return
            # Defensive: unknown event kinds are dropped silently.
    finally:
        if getter is not None and not getter.done():
            getter.cancel()
        if not worker.done():
            run.cancel_flag.set()
            # Shielded: cancelling this stream must not cancel the worker,
            # which still writes the readings of the completed files.
            await asyncio.shield(worker)


@router.get("/scraper/runs/{run_id}/stream")
async def scraper_run_stream(
    run_id: str,
    request: Request,
    session: SessionDTO = Depends(require_session),
) -> StreamingResponse:
    """SSE stream for a scraper run.

    Drives ``ScraperService.scrape_reports`` via ``asyncio.to_thread``
    so the uvicorn loop stays responsive (same hazard the chat
    surface's ``_TURN_LOCK`` work navigated — ADR-0031). The stream
    closes after the ``result`` / ``error`` / ``cancelled`` event. While a
    file is read, :func:`_drive_run` sends a ``: keep-alive`` comment every
    ``_KEEPALIVE_S`` seconds, so the disconnect check below also runs during
    a long read.

    Resolves the run's LLM **again** here (ADR-0123): this second
    resolution is the authoritative one, and resolving it inside the
    handler is what keeps a plain key out of the run store entirely.

    A run the dedup gate holds answers 409 until it is confirmed.
    """
    run = _scraper_runs(request).get(run_id)
    if run is None or run.session_id != str(session.id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Unknown scraper run.",
        )
    if run.awaiting_confirmation:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Scraper run awaits confirmation.",
        )

    csrf_token = session.csrf_token

    async def event_stream() -> AsyncGenerator[str, None]:
        try:
            llm = await _resolve_scraper_llm(request, session)
        except _ScraperUnconfiguredError as exc:
            # The credential vanished between the POST and the stream (a row
            # deleted, a key rotated out). One frame, the same message the
            # POST would have shown, and the stream closes.
            run.error = str(exc)
            yield _format_sse("error", str(exc))
            return
        try:
            async for frame in _drive_run(request, run, csrf_token, llm):
                if await request.is_disconnected():
                    logger.info("scraper-stream: client disconnected for run %s", run_id)
                    run.cancel_flag.set()
                    return
                yield frame
        except asyncio.CancelledError:
            run.cancel_flag.set()
            raise

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
