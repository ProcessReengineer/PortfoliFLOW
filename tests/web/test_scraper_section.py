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

The :class:`ScraperService` is mocked at the boundary so no real
PDFs or LLM endpoints are involved.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.tenant_constants import SENTINEL_TENANT_ID
from services.password_hashing import hash_password
from services.scraper.capabilities import UnsupportedModelError
from services.scraper.models import (
    Confidence,
    Finding,
    Keyword,
    KeywordType,
    ReportExtraction,
    ScraperResult,
)
from web.main import create_app
from web.routes.scraper import _DEFAULT_SCRAPER_MODEL, _PendingRun, _scraper_runs
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
        "data_store_entries, users, tenants "
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

    # The eight default keyword names are present.
    for default_name in (
        "Fund Name",
        "Reporting Period",
        "NAV",
        "TVPI",
        "DPI",
        "Net IRR",
        "Capital Called",
        "Capital Distributed",
    ):
        assert default_name in body, f"missing {default_name!r}"

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

    # Empty keyword list.
    response = await client.post(
        "/scraper/runs",
        data={
            "csrf_token": csrf,
            "keywords_json": "[]",
        },
        files={"pdf": ("report.pdf", b"%PDF-1.4\n", "application/pdf")},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "At least one keyword is required." in response.text

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
    ) -> ScraperResult:
        seen["llm"] = llm
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
