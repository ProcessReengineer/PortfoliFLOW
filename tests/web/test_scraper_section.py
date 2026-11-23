# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the Report Scraper web surface under
``/assistants#report-scraper`` (ADR-0053, ADR-0123).

Coverage:

1. GET /scraper/section renders the form with defaults and the
   read-only resolved-model line — never a model picker; the notice
   replaces the form when the resolved model fails the capability gate.
2. Model resolution, driven through the environment the way
   ``test_chat_llm_resolution.py`` drives the chat chain: ``SCRAPER_MODEL``
   outranks ``SHIRLEY_MODEL``, and with neither set the built-in default
   applies.
3. POST /scraper/runs validates inputs (no PDFs, malformed JSON), refuses
   with an inline 400 when no credential resolves or the resolved model
   cannot read PDFs, and stashes a pending run on success — with no
   ``model`` form field involved at all.
4. SSE happy path — events arrive as ``progress`` × N then
   ``result``; the ``result`` payload contains the rendered results
   partial, and the service receives the run's ``ResolvedLLM``.
5. SSE cancel — POST /scraper/runs/<id>/cancel returns 204 and the
   stream then emits ``cancelled`` and closes.
6. SSE service error — an UnsupportedModelError surfaces as an
   ``error`` event.
7. Per-file error — an extraction with ``error`` set renders the
   error block in the result partial.
8. Run-store LRU eviction — inserting beyond the cap evicts the
   oldest entry.
9. Logout drops the session's in-flight runs.
10. Session isolation — session A cannot cancel or stream session
    B's run.
11. Reading Desk (ADR-0139 §5, RD-D-9, RD-D-23): one reading per
    successful file, a cancelled run keeps its prefix, the dedup gate with
    *Read again* / *Don't read*, per tenant, the bank-keyword refusal, the
    empty keyword list, the document-type preselect, the escaped form
    error, a profile error at run time, no write without an engine, the
    proposal line, and the worker that outlives a closed or cancelled
    stream.

The :class:`ScraperService` is mocked at the boundary so no real
PDFs or LLM endpoints are involved.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import AsyncExitStack, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from starlette.requests import Request

from core.repositories import (
    AssetClassRepository,
    DocumentReadingDTO,
    DocumentReadingRepository,
    InvestmentRepository,
    UserRepository,
    tenant_context,
)
from core.tenant_constants import SENTINEL_TENANT_ID
from services.ai_service_core import ResolvedLLM
from services.password_hashing import hash_password
from services.reading_desk import compose_reading, describe_upload
from services.scraper.capabilities import UnsupportedModelError
from services.scraper.models import (
    FINDING_ORIGIN_PROFILE,
    Attachment,
    Confidence,
    Finding,
    Keyword,
    KeywordType,
    ReportExtraction,
    ScraperResult,
)
from services.scraper.profiles import ProfileError
from services.scraper.service import ScraperService
from web.main import create_app
from web.routes.scraper import (
    _DEFAULT_SCRAPER_MODEL,
    _drive_run,
    _PendingRun,
    _scraper_runs,
)
from web.settings import WebSettings

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

#: A PDF-capable model per the shipped capability map, and one that is not.
_SUPPORTED_MODEL = "anthropic/claude-opus-4-7"
_UNSUPPORTED_MODEL = "openai/gpt-4o"


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; skipping live-DB scraper tests.",
            allow_module_level=False,
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def env_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """The application scope, as a deployment's ``.env`` would provide it.

    Autouse so every test starts from one known chain state (ADR-0123): a
    resolvable credential and a PDF-capable model, both from the environment.
    Tests that need a different chain override these vars themselves — which
    is exactly how an operator would.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env-application-scope")
    monkeypatch.setenv("SCRAPER_MODEL", _SUPPORTED_MODEL)
    monkeypatch.delenv("SHIRLEY_MODEL", raising=False)


@pytest_asyncio.fixture
async def fresh_superuser_engine() -> AsyncGenerator[AsyncEngine, None]:
    _require_db()
    assert DATABASE_URL_SUPERUSER is not None  # narrowed for pyright; _require_db skipped otherwise
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def reset_schema(
    fresh_superuser_engine: AsyncEngine,
) -> AsyncGenerator[None, None]:
    # ``scoped_settings`` joins the list since ADR-0123: the section's model
    # now resolves through the vault-then-environment chain, so a leftover
    # tenant row from another suite would silently outrank the environment
    # these tests configure.
    truncate_sql = text(
        "TRUNCATE TABLE scoped_settings, data_upload_sheets, data_uploads, "
        "login_audit, sessions, audit_log, "
        "data_store_entries, document_readings, reading_purges, investments, "
        "users, tenants "
        "RESTART IDENTITY CASCADE"
    )
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(truncate_sql)
    try:
        yield
    finally:
        async with fresh_superuser_engine.begin() as conn:
            await conn.execute(truncate_sql)


@pytest_asyncio.fixture
async def seeded_user(
    fresh_superuser_engine: AsyncEngine,
    reset_schema: None,
) -> tuple[UUID, str, str]:
    plaintext = "correct-horse-battery-staple"
    user_id = uuid4()
    email = "scraper@example.com"
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, 'minathena-capital') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": str(SENTINEL_TENANT_ID), "name": "Sentinel Tenant"},
        )
        await conn.execute(
            text(
                """
                INSERT INTO users
                    (id, tenant_id, email, password_hash,
                     roles, is_active)
                VALUES
                    (:id, :tid, :email, :hash, ARRAY['owner']::text[], TRUE)
                """
            ),
            {
                "id": str(user_id),
                "tid": str(SENTINEL_TENANT_ID),
                "email": email,
                "hash": hash_password(plaintext),
            },
        )
    return user_id, email, plaintext


@pytest_asyncio.fixture
async def seeded_other_user(
    fresh_superuser_engine: AsyncEngine,
    seeded_user: tuple[UUID, str, str],
) -> tuple[UUID, str, str]:
    plaintext = "another-horse-battery-staple"
    user_id = uuid4()
    email = "scraper-other@example.com"
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO users
                    (id, tenant_id, email, password_hash,
                     roles, is_active)
                VALUES
                    (:id, :tid, :email, :hash, ARRAY['owner']::text[], TRUE)
                """
            ),
            {
                "id": str(user_id),
                "tid": str(SENTINEL_TENANT_ID),
                "email": email,
                "hash": hash_password(plaintext),
            },
        )
    return user_id, email, plaintext


@pytest_asyncio.fixture
async def web_client_factory(
    seeded_user: tuple[UUID, str, str],
):
    """Yield a factory that returns ``(client, app)`` per test."""
    settings = WebSettings(
        web_host="127.0.0.1",
        web_port=8000,
        session_cookie_name="portfoliflow_session",
        csrf_cookie_name="portfoliflow_csrf_pre_session",
        database_url=DATABASE_URL,
        database_url_superuser=DATABASE_URL_SUPERUSER,
        session_cookie_secure=False,
    )

    stack = AsyncExitStack()
    await stack.__aenter__()

    async def _make() -> tuple[AsyncClient, Any]:
        app = create_app(settings)
        await stack.enter_async_context(app.router.lifespan_context(app))
        transport = ASGITransport(app=app)
        client = AsyncClient(transport=transport, base_url="http://testserver")
        await stack.enter_async_context(client)
        return client, app

    try:
        yield _make
    finally:
        await stack.aclose()


async def _login_and_get_csrf(client: AsyncClient, email: str, password: str) -> str:
    get_response = await client.get("/login")
    csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
    assert csrf is not None
    await client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf},
        follow_redirects=False,
    )
    page = await client.get("/scraper/section", follow_redirects=False)
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
    assert match is not None, page.text
    return match.group(1)


def _parse_sse_frames(text: str) -> list[tuple[str, str]]:
    """Decode an SSE response body into ``(event, data)`` pairs."""
    frames: list[tuple[str, str]] = []
    for raw in text.split("\n\n"):
        raw = raw.strip("\n")
        if not raw:
            continue
        event = "message"
        data_lines: list[str] = []
        for line in raw.split("\n"):
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data_lines.append(line[len("data: ") :])
        frames.append((event, "\n".join(data_lines)))
    return frames


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_get_section_renders_form_with_defaults(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _id, email, password = seeded_user
    client, _app = await web_client_factory()
    await _login_and_get_csrf(client, email, password)

    response = await client.get("/scraper/section", follow_redirects=False)
    assert response.status_code == 200
    body = response.text

    # The keyword editor starts empty (D-RD-1-1): no row, no default name.
    assert 'class="scraper__keyword-row"' not in body
    assert "Capital Distributed" not in body
    assert "Extra keywords" in body

    # The document-type preselect: Auto first, then the four profile labels.
    assert re.search(r'<select name="profile_id" class="scraper__select">', body)
    assert '<option value="">Auto (the model decides)</option>' in body
    for label in (
        "Quarterly report",
        "Capital call / distribution notice",
        "Pitch deck / PPM",
        "Other document",
    ):
        assert label in body, f"missing {label!r}"

    # The resolved model is shown read-only, with the hint pointing at the
    # Admin field that sets it — and there is no picker any more (ADR-0123).
    assert _SUPPORTED_MODEL in body
    assert not re.search(r'<select[^>]*name="model"', body), "the model picker is gone"
    assert 'name="model"' not in body
    assert "Report Scraper model" in body
    assert re.search(r"Only Anthropic\s+models currently support PDF extraction\.", body), (
        "operator hint missing"
    )

    # The form posts to /scraper/runs as multipart.
    assert 'hx-post="/scraper/runs"' in body
    assert 'enctype="multipart/form-data"' in body


async def test_scraper_model_outranks_shirley_model(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Within the environment scope, the Scraper's own field wins."""
    monkeypatch.setenv("SCRAPER_MODEL", _SUPPORTED_MODEL)
    monkeypatch.setenv("SHIRLEY_MODEL", "anthropic/claude-haiku-4-5")

    _id, email, password = seeded_user
    client, _app = await web_client_factory()
    await _login_and_get_csrf(client, email, password)

    body = (await client.get("/scraper/section", follow_redirects=False)).text
    assert _SUPPORTED_MODEL in body
    assert "claude-haiku-4-5" not in body


async def test_shirley_model_serves_when_scraper_model_is_unset(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chain falls through to Shirley's model — the pre-ADR-0123 shape."""
    monkeypatch.delenv("SCRAPER_MODEL", raising=False)
    monkeypatch.setenv("SHIRLEY_MODEL", _SUPPORTED_MODEL)

    _id, email, password = seeded_user
    client, _app = await web_client_factory()
    await _login_and_get_csrf(client, email, password)

    body = (await client.get("/scraper/section", follow_redirects=False)).text
    assert _SUPPORTED_MODEL in body


async def test_built_in_default_applies_when_no_model_is_set(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With nothing configured anywhere, the built-in default is the answer.

    The default is PDF-capable, so an operator who configured nothing still
    gets a working form rather than a notice.
    """
    monkeypatch.delenv("SCRAPER_MODEL", raising=False)
    monkeypatch.delenv("SHIRLEY_MODEL", raising=False)

    _id, email, password = seeded_user
    client, _app = await web_client_factory()
    await _login_and_get_csrf(client, email, password)

    body = (await client.get("/scraper/section", follow_redirects=False)).text
    assert _DEFAULT_SCRAPER_MODEL in body
    assert "scraper-form" in body


async def test_get_section_renders_notice_when_model_is_unsupported(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolved model outside the capability map replaces the form.

    Submitting would fail on the first file anyway; the notice names the
    model and points at the Admin field that sets it.
    """
    monkeypatch.setenv("SCRAPER_MODEL", _UNSUPPORTED_MODEL)

    _id, email, password = seeded_user
    client, _app = await web_client_factory()
    # Inline login — _login_and_get_csrf extracts a CSRF token from
    # the rendered form, but the notice state has no form.
    get_response = await client.get("/login")
    pre_csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
    assert pre_csrf is not None
    await client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": pre_csrf},
        follow_redirects=False,
    )

    response = await client.get("/scraper/section", follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    assert _UNSUPPORTED_MODEL in body
    assert "does not support PDF extraction" in body
    assert "Report Scraper model" in body
    assert "scraper-form" not in body


async def test_post_runs_validates_inputs(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _id, email, password = seeded_user
    client, _app = await web_client_factory()
    csrf = await _login_and_get_csrf(client, email, password)

    # Malformed keywords_json.
    response = await client.post(
        "/scraper/runs",
        data={
            "csrf_token": csrf,
            "keywords_json": "{not-json",
        },
        files={"pdf": ("report.pdf", b"%PDF-1.4\n", "application/pdf")},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "Could not parse keywords" in response.text

    # Empty keyword list — accepted since P-RD-1c2: the profile's fields
    # are read anyway.
    response = await client.post(
        "/scraper/runs",
        data={
            "csrf_token": csrf,
            "keywords_json": "[]",
        },
        files={"pdf": ("report.pdf", b"%PDF-1.4\n", "application/pdf")},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert 'data-pf-scraper-sse-url="/scraper/runs/' in response.text

    # Valid submission — no ``model`` field is sent, and none is wanted.
    response = await client.post(
        "/scraper/runs",
        data={
            "csrf_token": csrf,
            "keywords_json": json.dumps([{"name": "NAV", "type": "Number"}]),
        },
        files={"pdf": ("report.pdf", b"%PDF-1.4\n", "application/pdf")},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert 'data-pf-scraper-sse-url="/scraper/runs/' in response.text


async def test_post_runs_ignores_a_submitted_model_field(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hand-crafted ``model`` field cannot choose the run's model.

    The picker is gone, and with it any client say over the model: the run
    uses what the tenant's chain resolves, whatever the form carries.
    """
    _id, email, password = seeded_user
    client, app = await web_client_factory()
    csrf = await _login_and_get_csrf(client, email, password)

    response = await client.post(
        "/scraper/runs",
        data={
            "csrf_token": csrf,
            "model": "openai/gpt-4o",
            "keywords_json": json.dumps([{"name": "NAV", "type": "Number"}]),
        },
        files={"pdf": ("report.pdf", b"%PDF-1.4\n", "application/pdf")},
        follow_redirects=False,
    )
    assert response.status_code == 200
    run_id = re.search(r'data-pf-scraper-run-id="([0-9a-f]+)"', response.text)
    assert run_id is not None
    stashed = app.state.scraper_runs[run_id.group(1)]
    assert stashed.model == _SUPPORTED_MODEL


async def test_post_runs_refuses_without_a_credential(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No credential in any scope answers the inline 400, not a broken run."""
    _id, email, password = seeded_user
    client, _app = await web_client_factory()
    csrf = await _login_and_get_csrf(client, email, password)

    # Removed *after* login so the section still rendered a form to read the
    # CSRF token from — the credential is only consulted on the POST.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    response = await client.post(
        "/scraper/runs",
        data={
            "csrf_token": csrf,
            "keywords_json": json.dumps([{"name": "NAV", "type": "Number"}]),
        },
        files={"pdf": ("report.pdf", b"%PDF-1.4\n", "application/pdf")},
        follow_redirects=False,
    )
    assert response.status_code == 400
    body = response.text
    assert "no API credential" in body
    assert "Admin → Providers &amp; Credentials" in body or "Providers" in body
    assert "OPENROUTER_API_KEY" in body
    # No restart is ever suggested: rows apply on the next run.
    assert "restart" not in body.lower()


async def test_post_runs_refuses_an_unsupported_model(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The capability gate refuses before a single upload body is read."""
    _id, email, password = seeded_user
    client, _app = await web_client_factory()
    csrf = await _login_and_get_csrf(client, email, password)

    monkeypatch.setenv("SCRAPER_MODEL", _UNSUPPORTED_MODEL)

    response = await client.post(
        "/scraper/runs",
        data={
            "csrf_token": csrf,
            "keywords_json": json.dumps([{"name": "NAV", "type": "Number"}]),
        },
        files={"pdf": ("report.pdf", b"%PDF-1.4\n", "application/pdf")},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert _UNSUPPORTED_MODEL in response.text
    assert "does not support PDF extraction" in response.text


async def test_sse_happy_path(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two-file run — events arrive as ``progress`` × 2 then ``result``.

    Also pins the seam ADR-0123 moved: the service is handed the run's
    ``ResolvedLLM``, carrying the tenant's key and the resolved model.
    """
    _id, email, password = seeded_user
    client, _app = await web_client_factory()
    csrf = await _login_and_get_csrf(client, email, password)

    seen: dict[str, Any] = {}

    def fake_scrape(
        self: Any,
        *,
        attachments: list[Any],
        keywords: list[Keyword],
        llm: Any,
        progress_callback: Any = None,
        cancel_check: Any = None,
        profile_id: str | None = None,
    ) -> ScraperResult:
        seen["llm"] = llm
        seen["profile_id"] = profile_id
        result = ScraperResult()
        for idx, att in enumerate(attachments):
            extraction = ReportExtraction(
                filename=att.filename,
                fund_name="Test Fund",
                period="Q3 2026",
                findings=[
                    Finding(
                        keyword=keywords[0],
                        value="123.45",
                        source="Page 1",
                        confidence=Confidence.HIGH,
                    )
                ],
            )
            result.extractions.append(extraction)
            if progress_callback:
                progress_callback(idx + 1, len(attachments), att.filename)
        return result

    from services.scraper.service import ScraperService

    monkeypatch.setattr(ScraperService, "scrape_reports", fake_scrape)

    post = await client.post(
        "/scraper/runs",
        data={
            "csrf_token": csrf,
            "keywords_json": json.dumps([{"name": "NAV", "type": "Number"}]),
        },
        files=[
            ("pdf", ("a.pdf", b"%PDF-1.4\nA", "application/pdf")),
            ("pdf", ("b.pdf", b"%PDF-1.4\nB", "application/pdf")),
        ],
        follow_redirects=False,
    )
    assert post.status_code == 200
    match = re.search(
        r'data-pf-scraper-sse-url="(/scraper/runs/[0-9a-f]+/stream)"',
        post.text,
    )
    assert match is not None
    stream_url = match.group(1)

    response = await client.get(stream_url)
    assert response.status_code == 200
    frames = _parse_sse_frames(response.text)
    events_seen = [e for e, _ in frames]
    assert events_seen.count("progress") == 2
    assert events_seen[-1] == "result"
    # The result payload contains the rendered results partial.
    result_data = frames[-1][1]
    assert "scraper-result__filename" in result_data
    assert "a.pdf" in result_data
    assert "b.pdf" in result_data

    # The service ran on the resolution, not on a model id.
    from services.ai_service_core import ResolvedLLM

    assert isinstance(seen["llm"], ResolvedLLM)
    assert seen["llm"].model == _SUPPORTED_MODEL
    assert seen["llm"].api_key == "sk-env-application-scope"
    # Auto: no document type was chosen, so none reaches the service.
    assert seen["profile_id"] is None


async def test_sse_service_error_emits_error_event(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _id, email, password = seeded_user
    client, _app = await web_client_factory()
    csrf = await _login_and_get_csrf(client, email, password)

    def boom(self: Any, **kwargs: Any) -> ScraperResult:
        raise UnsupportedModelError("Model 'x' does not support PDF.")

    from services.scraper.service import ScraperService

    monkeypatch.setattr(ScraperService, "scrape_reports", boom)

    post = await client.post(
        "/scraper/runs",
        data={
            "csrf_token": csrf,
            "keywords_json": json.dumps([{"name": "NAV", "type": "Number"}]),
        },
        files={"pdf": ("a.pdf", b"%PDF-1.4\nA", "application/pdf")},
        follow_redirects=False,
    )
    match = re.search(
        r'data-pf-scraper-sse-url="(/scraper/runs/[0-9a-f]+/stream)"',
        post.text,
    )
    assert match is not None
    response = await client.get(match.group(1))
    frames = _parse_sse_frames(response.text)
    events_seen = [e for e, _ in frames]
    assert "error" in events_seen
    assert "does not support PDF" in response.text


async def test_per_file_error_renders_in_result(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An extraction with .error set renders the error block."""
    _id, email, password = seeded_user
    client, _app = await web_client_factory()
    csrf = await _login_and_get_csrf(client, email, password)

    def fake_scrape(self: Any, **kwargs: Any) -> ScraperResult:
        result = ScraperResult()
        result.extractions.append(
            ReportExtraction(
                filename="good.pdf",
                findings=[
                    Finding(
                        keyword=Keyword(name="NAV", type=KeywordType.NUMBER),
                        value="100",
                        source="Page 1",
                        confidence=Confidence.HIGH,
                    )
                ],
            )
        )
        result.extractions.append(ReportExtraction(filename="bad.pdf", error="File too large"))
        return result

    from services.scraper.service import ScraperService

    monkeypatch.setattr(ScraperService, "scrape_reports", fake_scrape)

    post = await client.post(
        "/scraper/runs",
        data={
            "csrf_token": csrf,
            "keywords_json": json.dumps([{"name": "NAV", "type": "Number"}]),
        },
        files=[
            ("pdf", ("good.pdf", b"%PDF-1.4\nA", "application/pdf")),
            ("pdf", ("bad.pdf", b"%PDF-1.4\nB", "application/pdf")),
        ],
        follow_redirects=False,
    )
    match = re.search(
        r'data-pf-scraper-sse-url="(/scraper/runs/[0-9a-f]+/stream)"',
        post.text,
    )
    assert match is not None
    response = await client.get(match.group(1))
    body = response.text
    assert "Extraction failed:" in body
    assert "File too large" in body
    # The good file still renders normally.
    assert "scraper-result__findings" in body


async def test_sse_cancel_path(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancel POST flips the flag and the next iteration returns
    ``result.cancelled = True``; the stream emits ``cancelled``."""
    _id, email, password = seeded_user
    client, app = await web_client_factory()
    csrf = await _login_and_get_csrf(client, email, password)

    def fake_scrape(
        self: Any,
        *,
        attachments: list[Any],
        keywords: list[Keyword],
        llm: Any,
        progress_callback: Any = None,
        cancel_check: Any = None,
        profile_id: str | None = None,
    ) -> ScraperResult:
        result = ScraperResult()
        for idx, att in enumerate(attachments):
            if cancel_check and cancel_check():
                result.cancelled = True
                return result
            result.extractions.append(ReportExtraction(filename=att.filename))
            if progress_callback:
                progress_callback(idx + 1, len(attachments), att.filename)
            # Wait briefly so the test thread has a chance to push the
            # cancel flag before the next iteration is checked. The
            # synchronous worker thread sleeps here while the asyncio
            # loop runs the cancel POST.
            time.sleep(0.05)
        return result

    from services.scraper.service import ScraperService

    monkeypatch.setattr(ScraperService, "scrape_reports", fake_scrape)

    post = await client.post(
        "/scraper/runs",
        data={
            "csrf_token": csrf,
            "keywords_json": json.dumps([{"name": "NAV", "type": "Number"}]),
        },
        files=[
            ("pdf", ("a.pdf", b"%PDF-1.4\nA", "application/pdf")),
            ("pdf", ("b.pdf", b"%PDF-1.4\nB", "application/pdf")),
            ("pdf", ("c.pdf", b"%PDF-1.4\nC", "application/pdf")),
        ],
        follow_redirects=False,
    )
    match = re.search(
        r'data-pf-scraper-sse-url="/scraper/runs/([0-9a-f]+)/stream"',
        post.text,
    )
    assert match is not None
    run_id = match.group(1)

    # Pre-set the cancel flag — the worker thread polls it before
    # every file. This deterministic approach avoids racing the
    # background thread with the test's POST timing.
    run = app.state.scraper_runs.get(run_id)
    assert run is not None
    run.cancel_flag.set()

    response = await client.get(f"/scraper/runs/{run_id}/stream")
    frames = _parse_sse_frames(response.text)
    events_seen = [e for e, _ in frames]
    assert events_seen[-1] == "cancelled"

    # And the cancel POST itself returns 204 cleanly.
    cancel = await client.post(
        f"/scraper/runs/{run_id}/cancel",
        headers={"X-CSRF-Token": csrf},
    )
    assert cancel.status_code == 204


async def test_run_store_lru_eviction(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Insert 33 fake runs and verify the 33rd evicts the oldest."""
    _id, email, password = seeded_user
    client, app = await web_client_factory()
    await _login_and_get_csrf(client, email, password)

    from web.routes.scraper import _stash_run, _RUNS_LIMIT

    class _MockRequest:
        def __init__(self, app: Any) -> None:
            self.app = app

    request = _MockRequest(app)
    for i in range(_RUNS_LIMIT + 1):
        run = _PendingRun(
            run_id=f"run-{i:03d}",
            session_id="dummy-session",
            attachments=[],
            keywords=[],
            model=_SUPPORTED_MODEL,
            cancel_flag=threading.Event(),
        )
        _stash_run(request, run)  # type: ignore[arg-type]

    store = _scraper_runs(request)  # type: ignore[arg-type]
    assert len(store) == _RUNS_LIMIT
    assert "run-000" not in store  # oldest evicted
    assert f"run-{_RUNS_LIMIT:03d}" in store


async def test_logout_drops_inflight_runs(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _id, email, password = seeded_user
    client, app = await web_client_factory()
    csrf = await _login_and_get_csrf(client, email, password)

    from web.routes.scraper import _stash_run

    # Resolve the logged-in session id by looking at the session
    # cookie and reading it back through the auth helper.
    from web.auth import get_optional_session

    class _MockRequest:
        def __init__(self, app: Any, cookies: dict[str, str]) -> None:
            self.app = app
            self.cookies = cookies

    cookies = {"portfoliflow_session": client.cookies["portfoliflow_session"]}
    request = _MockRequest(app, cookies)
    session = await get_optional_session(request)  # type: ignore[arg-type]
    assert session is not None

    run = _PendingRun(
        run_id="run-logout",
        session_id=str(session.id),
        attachments=[],
        keywords=[],
        model="anthropic/claude-opus-4-7",
        cancel_flag=threading.Event(),
    )
    _stash_run(request, run)  # type: ignore[arg-type]
    assert "run-logout" in getattr(app.state, "scraper_runs", {})

    response = await client.post(
        "/logout",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "run-logout" not in getattr(app.state, "scraper_runs", {})
    assert run.cancel_flag.is_set()


async def test_session_isolation_blocks_cross_session_access(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    seeded_other_user: tuple[UUID, str, str],
) -> None:
    """A run owned by session A is not visible to session B."""
    _id_a, email_a, password_a = seeded_user
    _id_b, email_b, password_b = seeded_other_user

    client_a, app = await web_client_factory()
    csrf_a = await _login_and_get_csrf(client_a, email_a, password_a)
    del csrf_a  # we just need to be logged in for the POST

    # Sneak a run owned by A into the store directly.
    from web.routes.scraper import _stash_run
    from web.auth import get_optional_session

    class _MockRequest:
        def __init__(self, app: Any, cookies: dict[str, str]) -> None:
            self.app = app
            self.cookies = cookies

    cookies_a = {"portfoliflow_session": client_a.cookies["portfoliflow_session"]}
    request_a = _MockRequest(app, cookies_a)
    session_a = await get_optional_session(request_a)  # type: ignore[arg-type]
    assert session_a is not None

    run = _PendingRun(
        run_id="run-private-a",
        session_id=str(session_a.id),
        attachments=[],
        keywords=[],
        model="anthropic/claude-opus-4-7",
        cancel_flag=threading.Event(),
    )
    _stash_run(request_a, run)  # type: ignore[arg-type]

    # A second client logs in as user B (sharing the same app).
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client_b:
        get_response = await client_b.get("/login")
        pre_csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
        assert pre_csrf is not None
        await client_b.post(
            "/login",
            data={
                "email": email_b,
                "password": password_b,
                "csrf_token": pre_csrf,
            },
            follow_redirects=False,
        )
        page = await client_b.get("/scraper/section", follow_redirects=False)
        match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
        assert match is not None
        csrf_b = match.group(1)

        # B's stream attempt returns 404 — the lookup checks session id.
        stream = await client_b.get(
            "/scraper/runs/run-private-a/stream",
            follow_redirects=False,
        )
        assert stream.status_code == 404

        # B's cancel attempt is a silent no-op (204) — the flag
        # is not set because the session-id check fails.
        cancel = await client_b.post(
            "/scraper/runs/run-private-a/cancel",
            headers={"X-CSRF-Token": csrf_b},
        )
        assert cancel.status_code == 204
        assert not run.cancel_flag.is_set()


# ---------------------------------------------------------------------------
# Reading Desk (P-RD-1c2): writer, dedup gate, preselect, worker survival
# ---------------------------------------------------------------------------

_STREAM_URL = re.compile(r'data-pf-scraper-sse-url="(/scraper/runs/([0-9a-f]+)/stream)"')
_CONFIRM_URL = re.compile(r'hx-post="/scraper/runs/([0-9a-f]+)/confirm"')
_NAV = Keyword(name="NAV", type=KeywordType.NUMBER)


def _read_extraction(filename: str, *, fund_name: str = "Test Fund") -> ReportExtraction:
    """A successful extraction as the mapping builds it (profile ``other``, v1)."""
    return ReportExtraction(
        filename=filename,
        fund_name=fund_name,
        period="Q3 2026",
        findings=[
            Finding(
                keyword=_NAV,
                value="123.45",
                source="Page 1",
                confidence=Confidence.HIGH,
                key="nav",
                label="Net asset value",
                origin=FINDING_ORIGIN_PROFILE,
            )
        ],
        document_type="other",
        profile_version=1,
    )


def _install_reader(
    monkeypatch: pytest.MonkeyPatch,
    *,
    seen: dict[str, Any] | None = None,
    fail: frozenset[str] = frozenset(),
    after_file: Callable[[int], None] | None = None,
) -> None:
    """Replace the service with a fake that reads every file successfully.

    Files named in ``fail`` come back with ``error`` set; ``after_file`` is
    called with the index of each file read; ``seen`` receives the keywords
    and the profile id the service was handed.
    """

    def fake_scrape(
        self: Any,
        *,
        attachments: list[Attachment],
        keywords: list[Keyword],
        llm: Any,
        progress_callback: Any = None,
        cancel_check: Any = None,
        profile_id: str | None = None,
    ) -> ScraperResult:
        if seen is not None:
            seen["keywords"] = list(keywords)
            seen["profile_id"] = profile_id
        result = ScraperResult(prompt_version="0f1e2d3c")
        for idx, att in enumerate(attachments):
            if cancel_check and cancel_check():
                result.cancelled = True
                return result
            if att.filename in fail:
                result.extractions.append(
                    ReportExtraction(filename=att.filename, error="The model returned no JSON.")
                )
            else:
                result.extractions.append(_read_extraction(att.filename))
            if progress_callback:
                progress_callback(idx + 1, len(attachments), att.filename)
            if after_file is not None:
                after_file(idx)
        return result

    monkeypatch.setattr(ScraperService, "scrape_reports", fake_scrape)


async def _post_run(
    client: AsyncClient,
    csrf: str,
    files: list[tuple[str, bytes]],
    *,
    keywords: list[dict[str, str]] | None = None,
    profile_id: str | None = None,
) -> Any:
    data = {
        "csrf_token": csrf,
        "keywords_json": json.dumps(
            [{"name": "NAV", "type": "Number"}] if keywords is None else keywords
        ),
    }
    if profile_id is not None:
        data["profile_id"] = profile_id
    return await client.post(
        "/scraper/runs",
        data=data,
        files=[("pdf", (name, content, "application/pdf")) for name, content in files],
        follow_redirects=False,
    )


async def _run_to_end(
    client: AsyncClient, csrf: str, files: list[tuple[str, bytes]], **kwargs: Any
) -> list[tuple[str, str]]:
    """POST a run that the gate does not hold, stream it, return its frames."""
    post = await _post_run(client, csrf, files, **kwargs)
    assert post.status_code == 200, post.text
    match = _STREAM_URL.search(post.text)
    assert match is not None, post.text
    response = await client.get(match.group(1))
    assert response.status_code == 200
    return _parse_sse_frames(response.text)


async def _readings(app: Any, tenant_id: UUID = SENTINEL_TENANT_ID) -> list[DocumentReadingDTO]:
    async with tenant_context(app.state.engine, tenant_id) as session:
        readings = await DocumentReadingRepository(session).list_readings(include_removed=True)
    return sorted(readings, key=lambda reading: reading.reading_number)


async def test_one_reading_per_successful_file(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A good and a failed file: one reading, and the run view names it."""
    user_id, email, password = seeded_user
    client, app = await web_client_factory()
    csrf = await _login_and_get_csrf(client, email, password)
    _install_reader(monkeypatch, fail=frozenset({"bad.pdf"}))

    good = b"%PDF-1.4\ngood"
    frames = await _run_to_end(client, csrf, [("good.pdf", good), ("bad.pdf", b"%PDF-1.4\nbad")])

    assert frames[-1][0] == "result"
    body = frames[-1][1]
    assert body.count("Saved as R-0001") == 1
    assert body.count("Saved as") == 1
    assert "Not saved." not in body  # a failed file shows its error, no outcome line
    assert "Extraction failed:" in body
    readings = await _readings(app)
    assert len(readings) == 1
    assert readings[0].sha256 == hashlib.sha256(good).hexdigest()
    assert readings[0].read_by == user_id
    assert readings[0].origin == "upload"
    assert readings[0].filename == "good.pdf"


async def test_cancelled_run_keeps_its_prefix(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three files, cancelled after the first: the first file's reading stays."""
    _id, email, password = seeded_user
    client, app = await web_client_factory()
    csrf = await _login_and_get_csrf(client, email, password)

    def cancel_after_first(idx: int) -> None:
        if idx == 0:
            # What POST /scraper/runs/<id>/cancel does, at a fixed point.
            for run in app.state.scraper_runs.values():
                run.cancel_flag.set()

    _install_reader(monkeypatch, after_file=cancel_after_first)
    frames = await _run_to_end(
        client,
        csrf,
        [("a.pdf", b"%PDF-1.4\nA"), ("b.pdf", b"%PDF-1.4\nB"), ("c.pdf", b"%PDF-1.4\nC")],
    )

    assert frames[-1][0] == "cancelled"
    assert "Saved as R-0001" in frames[-1][1]
    readings = await _readings(app)
    assert [reading.filename for reading in readings] == ["a.pdf"]


async def _read_once_then_gate(
    client: AsyncClient, app: Any, csrf: str, content: bytes
) -> tuple[str, Any]:
    """Read ``content`` once, upload it again, and return the held run's id and answer."""
    await _run_to_end(client, csrf, [("fund.pdf", content)])
    post = await _post_run(client, csrf, [("fund-again.pdf", content)])
    match = _CONFIRM_URL.search(post.text)
    assert match is not None, post.text
    return match.group(1), post


async def test_dedup_gate_holds_a_second_upload(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _id, email, password = seeded_user
    client, app = await web_client_factory()
    csrf = await _login_and_get_csrf(client, email, password)
    _install_reader(monkeypatch)

    run_id, post = await _read_once_then_gate(client, app, csrf, b"%PDF-1.4\nsame")

    assert post.status_code == 200
    first = (await _readings(app))[0]
    read_on = first.read_at.astimezone(timezone.utc).date().isoformat()
    assert f"fund-again.pdf — already read on {read_on} as R-0001" in post.text
    assert "earlier)" not in post.text
    assert "data-pf-scraper-sse-url" not in post.text
    assert app.state.scraper_runs[run_id].awaiting_confirmation is True

    stream = await client.get(f"/scraper/runs/{run_id}/stream")
    assert stream.status_code == 409
    assert "Scraper run awaits confirmation." in stream.text


async def test_read_again_writes_a_new_reading(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _id, email, password = seeded_user
    client, app = await web_client_factory()
    csrf = await _login_and_get_csrf(client, email, password)
    _install_reader(monkeypatch)
    content = b"%PDF-1.4\nsame"

    run_id, _post = await _read_once_then_gate(client, app, csrf, content)
    confirm = await client.post(f"/scraper/runs/{run_id}/confirm", headers={"X-CSRF-Token": csrf})

    assert confirm.status_code == 200
    match = _STREAM_URL.search(confirm.text)
    assert match is not None, confirm.text
    assert match.group(2) == run_id
    response = await client.get(match.group(1))
    frames = _parse_sse_frames(response.text)
    assert frames[-1][0] == "result"
    assert "Saved as R-0002" in frames[-1][1]
    sha256 = hashlib.sha256(content).hexdigest()
    live = [r for r in await _readings(app) if r.sha256 == sha256 and r.removed_at is None]
    assert len(live) == 2

    # A confirmed run is no longer held: a second confirm is refused.
    again = await client.post(f"/scraper/runs/{run_id}/confirm", headers={"X-CSRF-Token": csrf})
    assert again.status_code == 404


async def test_dont_read_drops_the_run(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _id, email, password = seeded_user
    client, app = await web_client_factory()
    csrf = await _login_and_get_csrf(client, email, password)
    _install_reader(monkeypatch)

    run_id, _post = await _read_once_then_gate(client, app, csrf, b"%PDF-1.4\nsame")
    discard = await client.post(f"/scraper/runs/{run_id}/discard", headers={"X-CSRF-Token": csrf})

    assert discard.status_code == 200
    assert discard.text == '<p class="scraper__hint">Nothing was read.</p>'
    assert run_id not in app.state.scraper_runs
    assert len(await _readings(app)) == 1
    # The stream of a dropped run is gone with it.
    stream = await client.get(f"/scraper/runs/{run_id}/stream")
    assert stream.status_code == 404


async def test_dedup_is_per_tenant(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live reading of the same bytes in another tenant does not gate."""
    _id, email, password = seeded_user
    client, app = await web_client_factory()
    csrf = await _login_and_get_csrf(client, email, password)
    _install_reader(monkeypatch)
    content = b"%PDF-1.4\nshared"

    other_tenant = uuid4()
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (id, name, subdomain) VALUES (:id, 'Other', 'other-rd')"),
            {"id": str(other_tenant)},
        )
    async with tenant_context(app.state.engine, other_tenant) as session:
        other_user = await UserRepository(session).create(
            email="pm@other-rd.example", password_hash="x" * 8
        )
    kwargs = compose_reading(
        _read_extraction("fund.pdf"),
        describe_upload("fund.pdf", content),
        read_by=other_user.id,
        model=_SUPPORTED_MODEL,
        prompt_version="0f1e2d3c",
        keywords=[],
        now=datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc),
    )
    async with tenant_context(app.state.engine, other_tenant, user_id=other_user.id) as session:
        await DocumentReadingRepository(session).create(**kwargs)

    post = await _post_run(client, csrf, [("fund.pdf", content)])

    assert post.status_code == 200
    assert _STREAM_URL.search(post.text) is not None
    assert "already read on" not in post.text


async def test_bank_keyword_is_refused(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _id, email, password = seeded_user
    client, _app = await web_client_factory()
    csrf = await _login_and_get_csrf(client, email, password)

    post = await _post_run(
        client, csrf, [("a.pdf", b"%PDF-1.4\nA")], keywords=[{"name": "IBAN", "type": "Text"}]
    )

    assert post.status_code == 400
    assert "asks for bank details" in post.text
    assert "The Reading Desk never reads bank details." in post.text


async def test_empty_keyword_list_is_accepted(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _id, email, password = seeded_user
    client, _app = await web_client_factory()
    csrf = await _login_and_get_csrf(client, email, password)
    seen: dict[str, Any] = {}
    _install_reader(monkeypatch, seen=seen)

    frames = await _run_to_end(client, csrf, [("a.pdf", b"%PDF-1.4\nA")], keywords=[])

    assert frames[-1][0] == "result"
    assert seen["keywords"] == []


async def test_profile_preselect_reaches_the_service(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _id, email, password = seeded_user
    client, _app = await web_client_factory()
    csrf = await _login_and_get_csrf(client, email, password)
    seen: dict[str, Any] = {}
    _install_reader(monkeypatch, seen=seen)

    await _run_to_end(client, csrf, [("a.pdf", b"%PDF-1.4\nA")], profile_id="capital_notice")
    assert seen["profile_id"] == "capital_notice"

    refused = await _post_run(client, csrf, [("b.pdf", b"%PDF-1.4\nB")], profile_id="nonsense")
    assert refused.status_code == 400
    assert "Unknown document type." in refused.text


async def test_form_error_is_escaped(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _id, email, password = seeded_user
    client, _app = await web_client_factory()
    csrf = await _login_and_get_csrf(client, email, password)

    post = await _post_run(
        client,
        csrf,
        [("a.pdf", b"%PDF-1.4\nA")],
        keywords=[{"name": "<b>x</b>", "type": "Colour"}],
    )

    assert post.status_code == 400
    assert "&lt;b&gt;x&lt;/b&gt;" in post.text
    assert "<b>x</b>" not in post.text


async def test_profile_error_at_run_time_hides_the_path(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _id, email, password = seeded_user
    client, _app = await web_client_factory()
    csrf = await _login_and_get_csrf(client, email, password)

    def boom(self: Any, **kwargs: Any) -> ScraperResult:
        raise ProfileError("/srv/secret/path missing")

    monkeypatch.setattr(ScraperService, "scrape_reports", boom)
    frames = await _run_to_end(client, csrf, [("a.pdf", b"%PDF-1.4\nA")])

    assert frames[-1][0] == "error"
    assert frames[-1][1] == (
        "The document profiles could not be loaded. Ask an administrator to check the server log."
    )
    assert all("/srv/secret" not in data for _event, data in frames)


async def test_no_engine_writes_nothing_and_gates_nothing(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _id, email, password = seeded_user
    client, app = await web_client_factory()
    csrf = await _login_and_get_csrf(client, email, password)
    _install_reader(monkeypatch)
    monkeypatch.setattr("web.routes.scraper._reading_engine", lambda request: None)
    content = b"%PDF-1.4\nsame"

    first = await _run_to_end(client, csrf, [("a.pdf", content)])
    second = await _run_to_end(client, csrf, [("a.pdf", content)])  # no gate: it streams

    for frames in (first, second):
        assert frames[-1][0] == "result"
        assert "Saved as" not in frames[-1][1]
        assert "Not saved." not in frames[-1][1]
    assert await _readings(app) == []


async def test_proposal_is_shown_for_a_saved_reading(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, email, password = seeded_user
    client, app = await web_client_factory()
    csrf = await _login_and_get_csrf(client, email, password)
    _install_reader(monkeypatch)
    async with tenant_context(app.state.engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
        asset_class = await AssetClassRepository(session).create(
            code="rd_class", display_name="RD Class"
        )
        await InvestmentRepository(session).create(
            name="Test Fund",
            investment_type="private_equity",
            asset_class_id=asset_class.id,
            currency="EUR",
            created_by=user_id,
        )

    frames = await _run_to_end(client, csrf, [("a.pdf", b"%PDF-1.4\nA")])

    body = frames[-1][1]
    assert "Saved as R-0001" in body
    assert "Proposed assignment: investment “Test Fund”" in body


def _direct_run(app: Any, user_id: UUID, content: bytes) -> tuple[Request, _PendingRun]:
    """A request on the test app and a run, for driving ``_drive_run`` directly."""
    request = Request({"type": "http", "app": app, "method": "GET", "path": "/", "headers": []})
    run = _PendingRun(
        run_id=uuid4().hex,
        session_id="direct-session",
        attachments=[Attachment(filename="a.pdf", mime_type="application/pdf", data=content)],
        keywords=[],
        model=_SUPPORTED_MODEL,
        cancel_flag=threading.Event(),
        documents=[describe_upload("a.pdf", content)],
        tenant_id=SENTINEL_TENANT_ID,
        user_id=user_id,
    )
    return request, run


def _install_blocking_reader(
    monkeypatch: pytest.MonkeyPatch, started: threading.Event, release: threading.Event
) -> None:
    """A fake that reports its file, then blocks until ``release`` is set."""

    def fake_scrape(
        self: Any,
        *,
        attachments: list[Attachment],
        keywords: list[Keyword],
        llm: Any,
        progress_callback: Any = None,
        cancel_check: Any = None,
        profile_id: str | None = None,
    ) -> ScraperResult:
        result = ScraperResult(prompt_version="0f1e2d3c")
        result.extractions.append(_read_extraction(attachments[0].filename))
        if progress_callback:
            progress_callback(1, len(attachments), attachments[0].filename)
        started.set()
        release.wait(timeout=10)
        return result

    monkeypatch.setattr(ScraperService, "scrape_reports", fake_scrape)


_LLM = ResolvedLLM(base_url="http://llm.invalid", api_key="sk-test", model=_SUPPORTED_MODEL)


async def test_disconnect_after_the_service_returned_keeps_the_reading(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RD-D-23: the client goes away mid-read; the reading is still written."""
    user_id, _email, _password = seeded_user
    _client, app = await web_client_factory()
    started, release = threading.Event(), threading.Event()
    _install_blocking_reader(monkeypatch, started, release)
    request, run = _direct_run(app, user_id, b"%PDF-1.4\ndisconnect")

    generator = _drive_run(request, run, "csrf", _LLM)
    try:
        event, _data = _parse_sse_frames(await asyncio.wait_for(anext(generator), 10))[0]
        assert event == "progress"
        closing = asyncio.create_task(generator.aclose())
        await asyncio.sleep(0.05)
        assert not closing.done(), "the closing stream waits for the worker"
        release.set()
        await asyncio.wait_for(closing, 10)
    finally:
        release.set()

    readings = await _readings(app)
    assert [reading.filename for reading in readings] == ["a.pdf"]
    assert run.outcomes is not None
    assert run.outcomes[0].label == "R-0001"


async def test_cancelled_stream_does_not_cancel_the_writer(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling the stream task twice leaves the shielded worker running."""
    user_id, _email, _password = seeded_user
    _client, app = await web_client_factory()
    started, release = threading.Event(), threading.Event()
    _install_blocking_reader(monkeypatch, started, release)
    request, run = _direct_run(app, user_id, b"%PDF-1.4\ncancelled")

    async def consume() -> None:
        async for _frame in _drive_run(request, run, "csrf", _LLM):
            pass

    consumer = asyncio.create_task(consume())
    try:
        assert await asyncio.to_thread(started.wait, 10)
        await asyncio.sleep(0.05)
        consumer.cancel()
        await asyncio.sleep(0.05)
        consumer.cancel()
        await asyncio.sleep(0.05)
        workers = list(app.state.scraper_workers)
        assert workers, "the worker is still registered"
        release.set()
        await asyncio.wait(workers, timeout=10)
    finally:
        release.set()
        with suppress(asyncio.CancelledError):
            await consumer

    assert consumer.cancelled()
    readings = await _readings(app)
    assert [reading.filename for reading in readings] == ["a.pdf"]
