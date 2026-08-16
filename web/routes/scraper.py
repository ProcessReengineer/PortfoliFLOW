# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Report Scraper web surface under ``/assistants#report-scraper``.

The Report Scraper service (``services/scraper/``) is PyQt-free and
synchronous. This module lifts it into the web layer with a multipart
upload form, a keyword editor, a read-only line naming the model the next
run will use, and an SSE stream that drives the synchronous service via
``asyncio.to_thread`` so the uvicorn loop stays responsive. The
result is rendered inline; **persistence is explicitly deferred** —
the ScraperResult lives in memory for the duration of the run and is
shown to the operator on completion.

Since ADR-0123 the endpoint, credential and model are resolved **per run,
per tenant** through the one credential façade, exactly as the chat surface
resolves them per turn (ADR-0112 §4b) — see the resolution block below. The
page therefore has no model picker: the choice lives in Admin → Providers &
Credentials (``openrouter.scraper_model``), where every other model choice
lives.

Endpoints:

* ``GET  /scraper/section``              — section body (HTMX lazy load).
* ``POST /scraper/runs``                 — accept multipart upload, stash
                                            a pending run, return the
                                            SSE-mount fragment.
* ``GET  /scraper/runs/<run_id>/stream`` — SSE stream emitting
                                            ``progress`` / ``result`` /
                                            ``error`` / ``cancelled``.
* ``POST /scraper/runs/<run_id>/cancel`` — set the cancel flag.

A bounded LRU on ``app.state.scraper_runs`` holds pending and just-
completed runs keyed by ``run_id``. Single-worker only; multi-worker
deployments need Redis or equivalent (same migration trigger as
``chat_histories`` / ``pending_turns`` in ``web/routes/chat.py``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates

from core.repositories._session import tenant_context
from services.ai_service_core import ResolvedLLM
from services.auth.session import SessionDTO
from services.investments.credential_resolver import (
    CredentialResolver,
    CredentialUnavailableError,
    ProviderCredential,
)
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
from services.scraper.service import ScraperService
from web.auth import require_session, verify_csrf
from web.permissions import require_role

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# In-memory run store
# ---------------------------------------------------------------------------

_RUNS_LIMIT = 32
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


# ---------------------------------------------------------------------------
# Default keyword set
# ---------------------------------------------------------------------------


def _default_keyword_set() -> list[Keyword]:
    """Return the starting keyword set rendered on first section load.

    Chosen for institutional fund-of-funds use: the four standard
    return-multiple metrics, the two principal cashflow figures, and
    NAV. Reasonable defaults — not authoritative. The operator can
    add, edit, or remove rows before submitting the form.
    """
    return [
        Keyword(name="Fund Name", type=KeywordType.TEXT),
        Keyword(name="Reporting Period", type=KeywordType.DATE),
        Keyword(name="NAV", type=KeywordType.NUMBER),
        Keyword(name="TVPI", type=KeywordType.NUMBER),
        Keyword(name="DPI", type=KeywordType.NUMBER),
        Keyword(name="Net IRR", type=KeywordType.PERCENTAGE),
        Keyword(name="Capital Called", type=KeywordType.NUMBER),
        Keyword(name="Capital Distributed", type=KeywordType.NUMBER),
    ]


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
_NO_SCRAPER_LLM_MESSAGE = (
    "The Report Scraper has no API credential for this tenant. Set an "
    "OpenRouter API key in Admin → Providers & Credentials, and a Report "
    "Scraper model there if you want one other than the default (both apply "
    "on your next run), or set OPENROUTER_API_KEY and SCRAPER_MODEL in .env "
    "for the whole application."
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
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Return the Scraper section body for HTMX area-section loads.

    Shows the model the next run will use — resolved through the config
    chain only, so a tenant with no credential yet still sees which model it
    would extract with — and runs the capability gate on it. A model outside
    the map renders the notice in place of the form: submitting would fail on
    the first file anyway, and saying so before the upload is the honest
    order.
    """
    templates = _templates(request)
    model = await _resolve_scraper_model(request, session)
    model_error: str | None = None
    try:
        lookup_capability(model)
    except UnsupportedModelError as exc:
        model_error = str(exc)
    return cast(
        HTMLResponse,
        templates.TemplateResponse(
            request,
            "_partials/scraper_section.html",
            {
                "csrf_token": session.csrf_token,
                "keywords": _default_keyword_set(),
                "scraper_model": model,
                "model_supported": model_error is None,
                "model_error": model_error,
            },
        ),
    )


# ---------------------------------------------------------------------------
# POST /scraper/runs
# ---------------------------------------------------------------------------
#
# Submission shape (Option B from the spec — JSON-encoded keywords):
#     - pdf: list[UploadFile], multipart, accept="application/pdf"
#     - keywords_json: str, JSON-serialised
#       [{"name": "...", "type": "Number|Percentage|Date|Text|List"}, ...]
#     - csrf_token: str (validated by verify_csrf)
#
# The JSON payload is more natural for the dynamic keyword editor —
# the client-side serialisation happens once on submit, instead of
# round-tripping two parallel form-field lists and zipping by index
# on the server.


def _parse_keywords_json(payload: str) -> list[Keyword]:
    """Parse the submitted ``keywords_json`` field into ``Keyword`` records.

    Raises ``ValueError`` with an operator-readable message when the
    payload is malformed or empty. The exception text is rendered
    inline in the form's error panel.
    """
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Could not parse keywords ({exc.msg}).") from exc
    if not isinstance(raw, list) or not raw:
        raise ValueError("At least one keyword is required.")
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
        out.append(Keyword(name=name, type=kw_type))
    return out


def _render_form_error(request: Request, message: str) -> HTMLResponse:
    """Render a small inline error fragment for the run-mount target."""
    return HTMLResponse(
        f'<p class="scraper__form-error">{message}</p>',
        status_code=400,
    )


@router.post(
    "/scraper/runs",
    response_class=HTMLResponse,
    dependencies=[Depends(require_role("owner", "member"))],
)
async def scraper_run_start(
    request: Request,
    pdf: list[UploadFile] = File(...),
    keywords_json: str = Form(...),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Accept the upload, validate, stash a pending run, return the
    SSE-mount fragment.

    The fragment carries ``data-pf-scraper-sse-url`` and
    ``data-pf-scraper-run-id`` so ``scraper.js`` can open the
    EventSource and wire the cancel button.

    The run's LLM is resolved here to **fail fast** (ADR-0123): an
    unconfigured tenant, or one whose resolved model cannot read PDFs, learns
    it from an inline 400 rather than from a half-open SSE stream. Only the
    model *id* is stashed; the SSE handler resolves again and that second
    resolution is what drives the run.
    """
    if not pdf:
        return _render_form_error(request, "Please attach at least one PDF.")

    # Parse keywords first — cheap and lets us reject malformed input
    # before reading the upload bodies into memory.
    try:
        keywords = _parse_keywords_json(keywords_json)
    except ValueError as exc:
        return _render_form_error(request, str(exc))

    # Resolve before the bodies are read: a tenant with no credential, or one
    # whose model cannot take PDF input, should not pay for the upload first.
    try:
        llm = await _resolve_scraper_llm(request, session)
    except _ScraperUnconfiguredError as exc:
        return _render_form_error(request, str(exc))
    try:
        lookup_capability(llm.model)
    except UnsupportedModelError as exc:
        return _render_form_error(request, str(exc))

    # Read every upload body into memory. The scraper service is
    # synchronous and consumes bytes; we hold each file in a single
    # bytes object so the async loop is not blocked during the read.
    attachments: list[Attachment] = []
    total_bytes = 0
    for upload in pdf:
        data = await upload.read()
        total_bytes += len(data)
        if total_bytes > _MAX_UPLOAD_BYTES:
            return _render_form_error(
                request,
                f"Combined upload exceeds {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
            )
        filename = upload.filename or "untitled.pdf"
        attachments.append(
            Attachment(
                filename=filename,
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
    )
    _stash_run(request, run)

    templates = _templates(request)
    return cast(
        HTMLResponse,
        templates.TemplateResponse(
            request,
            "_partials/scraper_run_mount.html",
            {
                "run_id": run_id,
                "csrf_token": session.csrf_token,
                "total_files": len(attachments),
            },
        ),
    )


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


def _render_results_fragment(request: Request, result: ScraperResult | None) -> str:
    """Render the results partial as a self-contained HTML fragment."""
    templates = _templates(request)
    extractions: list[ReportExtraction] = list(result.extractions) if result is not None else []
    cancelled = bool(result.cancelled) if result is not None else False
    return templates.get_template("_partials/scraper_results.html").render(
        extractions=extractions, cancelled=cancelled
    )


def _render_progress_fragment(
    request: Request, done: int, total: int, filename: str, csrf_token: str, run_id: str
) -> str:
    """Render the progress partial for a single SSE progress frame."""
    templates = _templates(request)
    return templates.get_template("_partials/scraper_progress.html").render(
        done=done,
        total=total,
        filename=filename,
        percent=round((done / total) * 100) if total else 0,
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
            )
            run.result = result
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

    worker = asyncio.create_task(_run_in_thread())

    try:
        while True:
            event = await queue.get()
            kind = event["event"]
            if kind == "progress":
                payload = json.loads(event["data"])
                fragment = _render_progress_fragment(
                    request,
                    done=int(payload["done"]),
                    total=int(payload["total"]),
                    filename=str(payload["filename"]),
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
                if run.result is not None and run.result.cancelled:
                    yield _format_sse(
                        "cancelled",
                        _render_results_fragment(request, run.result),
                    )
                else:
                    yield _format_sse(
                        "result",
                        _render_results_fragment(request, run.result),
                    )
                return
            if kind == "error":
                payload = json.loads(event["data"])
                yield _format_sse("error", str(payload.get("message", "Unknown error.")))
                return
            # Defensive: unknown event kinds are dropped silently.
    finally:
        if not worker.done():
            run.cancel_flag.set()
            await worker


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
    closes after the ``result`` / ``error`` / ``cancelled`` event.

    Resolves the run's LLM **again** here (ADR-0123): this second
    resolution is the authoritative one, and resolving it inside the
    handler is what keeps a plain key out of the run store entirely.
    """
    run = _scraper_runs(request).get(run_id)
    if run is None or run.session_id != str(session.id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Unknown scraper run.",
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
