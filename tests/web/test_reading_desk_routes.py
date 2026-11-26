# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the Reading Desk's read side (ADR-0139 §5–§7, §11; ADR-0140).

What every signed-in user of the tenant may see of the Reading Desk:

1. The Section frame returned by ``GET /scraper/section`` — the tabs, the
   Read pane for owner and member only, the Library pane, the reading slot;
   ``?reading=<n>`` opens it on a reading.
2. ``GET /api/reading-desk/library`` — the list, each filter, tombstones,
   the search, paging and the empty states.
3. ``GET /api/reading-desk/readings/<n>`` — a live reading (head, facts,
   findings, note, "How this was read"), a removed reading (identity and
   removal only), the roles, the escaping of untrusted text.
4. ``GET /api/reading-desk/head`` — the plain head, out of band.
5. ``GET /assistants/readings/<n>`` — the page address of a reading.
6. The write side of one reading (P-RD-2c1): ``POST
   /api/reading-desk/readings/<n>/assign`` — to an investment, to a manager
   name, unassign, the proposal, on the reading's page and from a result
   block — and ``POST /api/reading-desk/readings/<n>/remove`` with its
   reason; the refusals, the CSRF token the Section root carries, and the
   gates for the three roles.
7. The owner's Retention tab (P-RD-2c2): ``GET /api/reading-desk/retention``
   — the pane, and with ``years`` the preview of a run — and ``POST
   /api/reading-desk/retention/run``; the past runs, a preview that no
   longer holds, the refusals, the tenant, and the gate for the three roles.
8. The "Documents read" section of ``GET /investments/<id>`` (P-RD-2d) —
   the list, what "As read" shows per document type, the order, the roles,
   the tenant, the escaping.

Assertions bind to ``data-pf-rd`` hooks, ids and copy, and to a class only
where the class is the point. All dates are fixed, except the day a write
stamps, which is today's in UTC, and the retention cases', which count back
from today's UTC date.
"""

from __future__ import annotations

import html
import os
import re
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.repositories import (
    AssetClassRepository,
    DocumentReadingDTO,
    DocumentReadingRepository,
    InvestmentRepository,
    UserRepository,
    tenant_context,
)
from core.tenant_constants import SENTINEL_TENANT_ID
from services.password_hashing import hash_password
from web.main import create_app
from web.routes.reading_desk import project_findings
from web.settings import WebSettings

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

_PASSWORD = "correct-horse-battery-staple"
#: role -> (email, display name)
_USERS: dict[str, tuple[str, str]] = {
    "owner": ("owner@rd.example", "Olivia Owner"),
    "member": ("member@rd.example", "Mark Member"),
    "auditor": ("auditor@rd.example", "Audrey Auditor"),
}

_T0 = datetime(2026, 11, 20, 9, 15, tzinfo=timezone.utc)
_SCRIPT = "<script>alert(1)</script>"
_SCRIPT_ESCAPED = "&lt;script&gt;alert(1)&lt;/script&gt;"

_ROW = re.compile(
    r'data-pf-rd="row" data-pf-rd-state="(live|removed)" data-pf-rd-reading="(R-\d+)"'
)


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB reading desk tests.",
            allow_module_level=False,
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def env_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resolvable credential and a PDF-capable model, as for the scraper tests."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env-application-scope")
    monkeypatch.setenv("SCRAPER_MODEL", "anthropic/claude-opus-4-7")
    monkeypatch.delenv("SHIRLEY_MODEL", raising=False)


@pytest_asyncio.fixture
async def fresh_superuser_engine() -> AsyncGenerator[AsyncEngine, None]:
    _require_db()
    assert DATABASE_URL_SUPERUSER is not None  # narrowed for pyright
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def reset_schema(fresh_superuser_engine: AsyncEngine) -> AsyncGenerator[None, None]:
    truncate_sql = text(
        "TRUNCATE TABLE scoped_settings, data_upload_sheets, data_uploads, "
        "login_audit, sessions, audit_log, "
        "data_store_entries, document_readings, reading_purges, investments, "
        "asset_classes, users, tenants "
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
async def seeded_users(
    fresh_superuser_engine: AsyncEngine,
    reset_schema: None,
) -> dict[str, UUID]:
    """One user per role in the Primary Tenant, each with a display name."""
    ids: dict[str, UUID] = {}
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, 'minathena-capital') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": str(SENTINEL_TENANT_ID), "name": "Sentinel Tenant"},
        )
        for role, (email, display_name) in _USERS.items():
            user_id = uuid4()
            await conn.execute(
                text(
                    """
                    INSERT INTO users
                        (id, tenant_id, email, display_name, password_hash, roles, is_active)
                    VALUES
                        (:id, :tid, :email, :name, :hash, ARRAY[:role]::text[], TRUE)
                    """
                ),
                {
                    "id": str(user_id),
                    "tid": str(SENTINEL_TENANT_ID),
                    "email": email,
                    "name": display_name,
                    "hash": hash_password(_PASSWORD),
                    "role": role,
                },
            )
            ids[role] = user_id
    return ids


@pytest_asyncio.fixture
async def web_client_factory(seeded_users: dict[str, UUID]):
    """Yield a factory that returns ``(client, app)``, signed in as ``role`` if given."""
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

    async def _make(role: str | None = "owner") -> tuple[AsyncClient, Any]:
        app = create_app(settings)
        await stack.enter_async_context(app.router.lifespan_context(app))
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")
        await stack.enter_async_context(client)
        if role is not None:
            await _login(client, _USERS[role][0])
        return client, app

    try:
        yield _make
    finally:
        await stack.aclose()


async def _login(client: AsyncClient, email: str) -> None:
    page = await client.get("/login")
    csrf = page.cookies.get("portfoliflow_csrf_pre_session")
    assert csrf is not None
    response = await client.post(
        "/login",
        data={"email": email, "password": _PASSWORD, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303), response.text


# ---------------------------------------------------------------------------
# Seeding through the repository
# ---------------------------------------------------------------------------


def _finding(**overrides: Any) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "key": "nav",
        "label": "NAV",
        "value": "41236000",
        "type": "Number",
        "unit": "EUR",
        "page": 7,
        "source": "Capital account",
        "quote": "Capital account balance EUR 41,236,000",
        "confidence": "High",
        "origin": "profile",
    }
    finding.update(overrides)
    return finding


def _reading_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "filename": "fund-x-q3.pdf",
        "sha256": "a" * 64,
        "size_bytes": 123_456,
        "page_count": 24,
        "document_type": "quarterly_report",
        "profile_version": 1,
        "language": "en",
        "fund_name_read": "Fund X",
        "manager_name_read": "Example Capital",
        "document_date": date(2026, 9, 30),
        "period_label": "Q3 2026",
        "currency": "EUR",
        "findings": [_finding()],
        "note": {"highlights": ["Two exits signed."]},
        "model": "vendor/model-a",
        "prompt_version": "0f1e2d3c",
        "adhoc_keywords": [],
        "search_text": "Fund X Q3 2026",
        "now": _T0,
    }
    kwargs.update(overrides)
    return kwargs


async def _create(
    engine: AsyncEngine, user_id: UUID, tenant_id: UUID = SENTINEL_TENANT_ID, **overrides: Any
) -> DocumentReadingDTO:
    async with tenant_context(engine, tenant_id, user_id=user_id) as session:
        return await DocumentReadingRepository(session).create(
            read_by=user_id, **_reading_kwargs(**overrides)
        )


async def _assign(
    engine: AsyncEngine, user_id: UUID, reading_id: UUID, now: datetime, **target: Any
) -> None:
    async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
        await DocumentReadingRepository(session).assign(
            reading_id, assigned_by=user_id, now=now, **target
        )


async def _remove(
    engine: AsyncEngine, user_id: UUID, reading_id: UUID, reason: str, now: datetime
) -> None:
    async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
        await DocumentReadingRepository(session).remove(
            reading_id, reason=reason, removed_by=user_id, now=now
        )


async def _purge(engine: AsyncEngine, user_id: UUID, *, years: int, today: date, now: datetime):
    async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
        return await DocumentReadingRepository(session).run_purge(
            years=years, today=today, run_by=user_id, now=now
        )


async def _investments(engine: AsyncEngine, user_id: UUID, *names: str) -> list[UUID]:
    async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
        asset_class = await AssetClassRepository(session).create(
            code="rd_class", display_name="RD Class"
        )
        repo = InvestmentRepository(session)
        return [
            (
                await repo.create(
                    name=name,
                    investment_type="private_equity",
                    asset_class_id=asset_class.id,
                    currency="EUR",
                    created_by=user_id,
                )
            ).id
            for name in names
        ]


async def _other_tenant_reading(
    fresh_superuser_engine: AsyncEngine, engine: AsyncEngine, count: int = 1
) -> None:
    """A second tenant with readings of its own (R-0001 … R-<count>)."""
    other_tenant = uuid4()
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (id, name, subdomain) VALUES (:id, 'Other', 'other-rd')"),
            {"id": str(other_tenant)},
        )
    async with tenant_context(engine, other_tenant) as session:
        other_user = await UserRepository(session).create(
            email="pm@other-rd.example", password_hash="x" * 8
        )
    for i in range(count):
        await _create(
            engine,
            other_user.id,
            tenant_id=other_tenant,
            filename=f"foreign-{i}.pdf",
            fund_name_read="Foreign Fund",
            search_text="Foreign Fund orbital",
        )


def _rows(body: str) -> list[str]:
    return [label for _state, label in _ROW.findall(body)]


def _row_html(body: str, label: str) -> str:
    match = re.search(
        rf'<tr class="pf-table__row" data-pf-rd="row"[^>]*data-pf-rd-reading="{label}">.*?</tr>',
        body,
        re.S,
    )
    assert match is not None, label
    return match.group(0)


def _element(body: str, pattern: str) -> str:
    """The first opening tag matching ``pattern``."""
    match = re.search(pattern, body)
    assert match is not None, pattern
    return match.group(0)


async def _library(client: AsyncClient, **params: str) -> str:
    response = await client.get("/api/reading-desk/library", params=params)
    assert response.status_code == 200, response.text
    return response.text


# ---------------------------------------------------------------------------
# The frame and the roles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["owner", "member"])
async def test_frame_for_a_reader_opens_on_read(web_client_factory: Any, role: str) -> None:
    client, _app = await web_client_factory(role)
    response = await client.get("/scraper/section")
    assert response.status_code == 200
    body = response.text

    assert 'data-pf-rd="root"' in body
    read_tab = _element(body, r'<button[^>]*data-pf-rd-tab="read"[^>]*>')
    library_tab = _element(body, r'<button[^>]*data-pf-rd-tab="library"[^>]*>')
    assert 'aria-selected="true"' in read_tab
    assert 'aria-selected="false"' in library_tab
    read_pane = _element(body, r'<div id="rd-pane-read"[^>]*>')
    assert " hidden" not in read_pane
    # The run form sits inside the Read pane.
    assert body.index('id="rd-pane-read"') < body.index('id="scraper-form"')
    assert body.index('id="scraper-form"') < body.index('id="rd-pane-library"')
    assert " hidden" in _element(body, r'<div id="rd-pane-library"[^>]*>')
    assert 'hx-trigger="rd:load"' in _element(body, r'<div id="rd-library"[^>]*>')
    assert " hidden" in _element(body, r'<div id="rd-reading"[^>]*>')
    if role == "owner":
        _assert_retention_tab(body)
    else:
        assert "Retention" not in body


@pytest.mark.parametrize("role", ["owner", "member"])
async def test_refusal_offers_the_admin_link_to_an_owner_only(
    web_client_factory: Any, role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model that cannot read PDFs replaces the form, inside the frame.

    The way to Admin › Providers & Credentials is offered to an owner only:
    a member cannot open it.
    """
    monkeypatch.setenv("SCRAPER_MODEL", "openai/gpt-4o")
    client, _app = await web_client_factory(role)
    response = await client.get("/scraper/section")
    assert response.status_code == 200
    body = response.text

    assert 'id="rd-tab-read"' in body
    assert "scraper-form" not in body
    assert "The model openai/gpt-4o cannot read PDF documents." in body
    if role == "owner":
        assert 'href="/admin#providers-credentials"' in body
    else:
        assert 'href="/admin#providers-credentials"' not in body


async def test_frame_for_an_auditor_opens_on_the_library(web_client_factory: Any) -> None:
    client, _app = await web_client_factory("auditor")
    response = await client.get("/scraper/section")
    assert response.status_code == 200
    body = response.text

    assert 'data-pf-rd-tab="read"' not in body
    assert "rd-pane-read" not in body
    assert "scraper-form" not in body
    assert 'aria-selected="true"' in _element(body, r'<button[^>]*data-pf-rd-tab="library"[^>]*>')
    assert " hidden" not in _element(body, r'<div id="rd-pane-library"[^>]*>')
    assert 'hx-trigger="load, rd:load"' in _element(body, r'<div id="rd-library"[^>]*>')
    assert "Retention" not in body


async def test_frame_opened_on_a_reading(web_client_factory: Any) -> None:
    client, _app = await web_client_factory("owner")
    body = (await client.get("/scraper/section", params={"reading": "7"})).text

    assert " hidden" in _element(body, r'<div class="pf-tabs"[^>]*>')
    assert " hidden" in _element(body, r'<div id="rd-pane-read"[^>]*>')
    assert " hidden" in _element(body, r'<div id="rd-pane-library"[^>]*>')
    assert 'hx-trigger="rd:load"' in _element(body, r'<div id="rd-library"[^>]*>')
    slot = _element(body, r'<div id="rd-reading"[^>]*>')
    assert " hidden" not in slot
    assert 'hx-get="/api/reading-desk/readings/7"' in slot
    assert 'hx-trigger="load"' in slot
    assert "Loading the reading&hellip;" in body


async def test_routes_without_a_session_answer_as_the_section_does(
    web_client_factory: Any, seeded_users: dict[str, UUID]
) -> None:
    client, app = await web_client_factory(None)
    await _create(app.state.engine, seeded_users["owner"])
    reference = await client.get("/scraper/section", follow_redirects=False)
    assert reference.status_code in (303, 401)
    for path in (
        "/api/reading-desk/library",
        "/api/reading-desk/library?rows=1",
        "/api/reading-desk/readings/1",
        "/api/reading-desk/head",
    ):
        response = await client.get(path, follow_redirects=False)
        assert response.status_code == reference.status_code, path
        assert response.headers.get("location") == reference.headers.get("location"), path


# ---------------------------------------------------------------------------
# The Library
# ---------------------------------------------------------------------------


async def test_library_empty_per_role(web_client_factory: Any) -> None:
    owner, _app = await web_client_factory("owner")
    body = await _library(owner)
    assert 'data-pf-rd="empty"' in body
    assert "Nothing has been read yet." in body
    assert 'data-pf-rd="goto" data-pf-rd-tab="read"' in body
    assert "what is read from it is kept here, the document itself is not." in body
    assert 'data-pf-rd="table"' not in body

    auditor, _app = await web_client_factory("auditor")
    body = await _library(auditor)
    assert "Nothing has been read yet." in body
    assert "What the team reads from documents is kept here" in body
    assert 'data-pf-rd="goto"' not in body


async def test_library_lists_newest_read_first(
    web_client_factory: Any, seeded_users: dict[str, UUID]
) -> None:
    client, app = await web_client_factory("auditor")
    engine = app.state.engine
    owner, member = seeded_users["owner"], seeded_users["member"]
    [fund] = await _investments(engine, owner, "Nordlicht Infrastructure II")

    undated = await _create(
        engine, owner, document_date=None, period_label=None, filename="undated.pdf", now=_T0
    )
    held = await _create(
        engine, member, filename="held.pdf", document_type="pitch_deck", now=_T0 + timedelta(1)
    )
    await _assign(engine, owner, held.id, _T0 + timedelta(1), investment_id=fund)
    named = await _create(
        engine, owner, filename="named.pdf", fund_name_read=None, now=_T0 + timedelta(2)
    )
    await _assign(engine, owner, named.id, _T0 + timedelta(2), manager_name="Gamma Capital")

    body = await _library(client)
    assert _rows(body) == ["R-0003", "R-0002", "R-0001"]
    assert 'data-pf-rd="empty"' not in body
    assert "hx-push-url" not in body

    first = _row_html(body, "R-0001")
    assert '<span class="pf-table__id">R-0001</span>' in first
    assert 'href="/assistants/readings/1"' in first
    assert 'hx-get="/api/reading-desk/readings/1"' in first
    assert 'hx-target="#rd-reading"' in first
    assert ">undated.pdf</a>" in first
    assert '<span class="pf-table__sub">Fund X</span>' in first
    assert "<td>Quarterly report</td>" in first
    assert '<span class="pf-hint">—</span>' in first
    assert '<span class="pf-state pf-state--unassigned">Unassigned</span>' in first
    assert "2026-11-20 · Olivia Owner" in first

    second = _row_html(body, "R-0002")
    assert '<span class="pf-table__sub">Fund X · Q3 2026</span>' in second
    assert "<td>Pitch deck / PPM</td>" in second
    assert "<td>2026-09-30</td>" in second
    assert 'Nordlicht Infrastructure II<span class="pf-table__sub">investment</span>' in second
    assert "2026-11-21 · Mark Member" in second

    third = _row_html(body, "R-0003")
    assert '<span class="pf-table__sub">Q3 2026</span>' in third
    assert 'Gamma Capital<span class="pf-table__sub">manager name</span>' in third
    assert undated.reading_number == 1


async def test_library_filters(web_client_factory: Any, seeded_users: dict[str, UUID]) -> None:
    client, app = await web_client_factory("owner")
    engine = app.state.engine
    owner = seeded_users["owner"]
    fund_one, fund_two = await _investments(engine, owner, "Alpha Fund", "Delta Fund")

    a = await _create(engine, owner, document_date=date(2026, 3, 31), now=_T0)
    await _assign(engine, owner, a.id, _T0, investment_id=fund_one)
    b = await _create(
        engine,
        owner,
        document_type="pitch_deck",
        manager_name_read="Beta LLP",
        document_date=date(2026, 6, 30),
        now=_T0 + timedelta(1),
    )
    await _assign(engine, owner, b.id, _T0, manager_name="Gamma Capital")
    await _create(
        engine, owner, manager_name_read="Gamma Capital", document_date=None, now=_T0 + timedelta(2)
    )
    d = await _create(
        engine,
        owner,
        document_type="capital_notice",
        manager_name_read="gamma capital",
        document_date=date(2026, 9, 30),
        now=_T0 + timedelta(3),
    )
    await _assign(engine, owner, d.id, _T0, investment_id=fund_two)

    assert _rows(await _library(client)) == ["R-0004", "R-0003", "R-0002", "R-0001"]
    assert _rows(await _library(client, type="pitch_deck")) == ["R-0002"]
    assert _rows(await _library(client, assigned="none")) == ["R-0003"]
    assert _rows(await _library(client, assigned=f"inv:{fund_one}")) == ["R-0001"]
    # Assigned to the name — not R-0003 (only read), not R-0004 (an investment).
    assert _rows(await _library(client, assigned="mgr:Gamma Capital")) == ["R-0002"]
    assert _rows(await _library(client, date_from="2026-06-30")) == ["R-0004", "R-0002"]
    assert _rows(await _library(client, date_to="2026-06-30")) == ["R-0002", "R-0001"]
    assert _rows(await _library(client, type="quarterly_report", date_to="2026-06-30")) == [
        "R-0001"
    ]

    body = await _library(
        client, type="pitch_deck", assigned="mgr:Gamma Capital", date_from="2026-06-01", q=""
    )
    assert '<option value="pitch_deck" selected>Pitch deck / PPM</option>' in body
    assert '<option value="mgr:Gamma Capital" selected>Gamma Capital</option>' in body
    assert 'name="date_from" value="2026-06-01"' in body
    assert '<optgroup label="Investments">' in body
    assert f'<option value="inv:{fund_one}">Alpha Fund</option>' in body
    assert '<optgroup label="Manager names">' in body
    body = await _library(client, assigned=f"inv:{fund_two}", date_to="2026-12-31", removed="1")
    assert f'<option value="inv:{fund_two}" selected>Delta Fund</option>' in body
    assert 'name="date_to" value="2026-12-31"' in body
    assert 'name="removed" value="1" checked' in body

    for params in (
        {"type": "agm_papers"},
        {"assigned": "someone"},
        {"assigned": "inv:not-a-uuid"},
        {"assigned": "mgr:  "},
        {"date_from": "2026-13-01"},
        {"date_to": "yesterday"},
        {"offset": "-1"},
        {"offset": "ten"},
        {"removed": "yes"},
    ):
        response = await client.get("/api/reading-desk/library", params=params)
        assert response.status_code == 400, params
        assert response.json() == {"detail": "Unknown filter value."}, params


async def test_library_without_an_assigned_name_hides_the_group(
    web_client_factory: Any, seeded_users: dict[str, UUID]
) -> None:
    client, app = await web_client_factory("owner")
    await _create(app.state.engine, seeded_users["owner"])
    body = await _library(client)
    assert "<optgroup" not in body


async def test_library_removed_readings(
    web_client_factory: Any, seeded_users: dict[str, UUID]
) -> None:
    client, app = await web_client_factory("owner")
    engine = app.state.engine
    owner, member = seeded_users["owner"], seeded_users["member"]

    await _create(engine, owner, now=_T0)
    draft = await _create(
        engine,
        owner,
        filename="draft.pdf",
        document_type="pitch_deck",
        fund_name_read="Tombstone Fund",
        period_label="Q9 2099",
        now=_T0 + timedelta(1),
    )
    await _assign(engine, owner, draft.id, _T0, manager_name="Hidden Manager")
    await _remove(
        engine,
        member,
        draft.id,
        "Draft superseded by R-0001.",
        datetime(2026, 11, 22, 8, 0, tzinfo=timezone.utc),
    )
    await _create(
        engine,
        owner,
        filename="old.pdf",
        document_date=date(2015, 6, 30),
        now=_T0 + timedelta(2),
    )
    await _purge(
        engine,
        owner,
        years=7,
        today=date(2026, 11, 20),
        now=datetime(2026, 11, 23, 8, 0, tzinfo=timezone.utc),
    )

    assert _rows(await _library(client)) == ["R-0001"]

    body = await _library(client, removed="1")
    assert _ROW.findall(body) == [
        ("removed", "R-0003"),
        ("removed", "R-0002"),
        ("live", "R-0001"),
    ]
    draft_row = _row_html(body, "R-0002")
    assert (
        '<td colspan="3"><span class="pf-state pf-state--removed">'
        "Removed 2026-11-22 · Mark Member</span>"
        '<span class="pf-table__sub">Draft superseded by R-0001.</span></td>'
    ) in draft_row
    assert ">draft.pdf</a>" in draft_row
    assert 'hx-get="/api/reading-desk/readings/2"' in draft_row
    assert "2026-11-21 · Olivia Owner" in draft_row
    for hidden in ("Pitch deck / PPM", "Tombstone Fund", "Q9 2099", "Hidden Manager"):
        assert hidden not in draft_row, hidden
    for hidden in ("Tombstone Fund", "Q9 2099", "Hidden Manager"):
        assert hidden not in body, hidden

    retention_row = _row_html(body, "R-0003")
    assert "Removed 2026-11-23 · Olivia Owner" in retention_row
    assert "Retention run: documents older than 7 years (before 2019-11-20)." in retention_row


async def test_library_search(web_client_factory: Any, seeded_users: dict[str, UUID]) -> None:
    client, app = await web_client_factory("owner")
    engine = app.state.engine
    owner = seeded_users["owner"]

    await _create(engine, owner, search_text="Orbital widget pipeline", now=_T0)
    await _create(engine, owner, search_text="nothing to see", now=_T0 + timedelta(1))
    await _create(
        engine,
        owner,
        document_type="pitch_deck",
        search_text="an orbital teaser",
        now=_T0 + timedelta(2),
    )
    gone = await _create(engine, owner, search_text="orbital draft", now=_T0 + timedelta(3))
    await _remove(engine, owner, gone.id, "Wrong file.", _T0 + timedelta(4))

    assert _rows(await _library(client, q="  ORBITAL ")) == ["R-0003", "R-0001"]
    assert _rows(await _library(client, q="orbital", type="pitch_deck")) == ["R-0003"]
    # A removed reading never matches; "Show removed" adds nothing to a search.
    assert _rows(await _library(client, q="orbital", removed="1")) == ["R-0003", "R-0001"]
    assert _rows(await _library(client, q="orbital", assigned="none")) == ["R-0003", "R-0001"]

    body = await _library(client, q="orbital", type="other")
    assert _rows(body) == []
    assert "No reading matches." in body
    assert "Nothing has been read yet." not in body
    assert 'value="orbital"' in body
    assert 'data-pf-rd="older"' not in body


async def test_library_pages_by_fifty(
    web_client_factory: Any, seeded_users: dict[str, UUID]
) -> None:
    client, app = await web_client_factory("owner")
    owner = seeded_users["owner"]
    async with tenant_context(app.state.engine, SENTINEL_TENANT_ID, user_id=owner) as session:
        repo = DocumentReadingRepository(session)
        for i in range(51):
            await repo.create(read_by=owner, **_reading_kwargs(now=_T0 + timedelta(minutes=i)))

    body = await _library(client, type="quarterly_report", date_from="2000-01-01")
    rows = _rows(body)
    assert len(rows) == 50
    assert rows[0] == "R-0051" and rows[-1] == "R-0002"
    older = _element(
        body, r'<tr class="pf-table__older" data-pf-rd="older">\s*<td[^>]*><button[^>]*>'
    )
    found = re.search(r'hx-get="([^"]+)"', older)
    assert found is not None
    href = html.unescape(found.group(1))
    assert href == (
        "/api/reading-desk/library?type=quarterly_report&date_from=2000-01-01&offset=50&rows=1"
    )
    assert 'hx-target="closest tr"' in older
    assert 'hx-swap="outerHTML"' in older
    assert ">Show older</button>" in body

    page = await client.get(href)
    assert page.status_code == 200
    assert _rows(page.text) == ["R-0001"]
    assert "<table" not in page.text
    assert "<form" not in page.text
    assert 'data-pf-rd="older"' not in page.text


async def test_library_hides_another_tenants_readings(
    web_client_factory: Any,
    seeded_users: dict[str, UUID],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    client, app = await web_client_factory("owner")
    await _other_tenant_reading(fresh_superuser_engine, app.state.engine)
    await _create(app.state.engine, seeded_users["owner"], filename="ours.pdf")

    body = await _library(client)
    assert _rows(body) == ["R-0001"]
    assert "ours.pdf" in body
    assert "foreign-0.pdf" not in body
    assert "foreign-0.pdf" not in await _library(client, q="orbital")


# ---------------------------------------------------------------------------
# A reading
# ---------------------------------------------------------------------------


async def _seed_full_reading(engine: AsyncEngine, owner: UUID) -> None:
    """R-0001 rich and assigned; R-0002 sparse; R-0003 without findings."""
    [fund] = await _investments(engine, owner, "Nordlicht Infrastructure II")
    full = await _create(
        engine,
        owner,
        filename="Nordlicht_Q3.pdf",
        language="de",
        manager_name_read="Nordlicht Capital Partners",
        fund_name_read="Nordlicht Infrastructure Fund II",
        findings=[
            _finding(),
            _finding(
                key="irr",
                label="Net IRR",
                value="9.4%",
                type="Percentage",
                unit="%",
                page=None,
                source="Performance table",
                quote="Net IRR since inception 9.4%",
                confidence="Medium",
            ),
            _finding(
                key="new",
                label="New investments",
                value="Fjordkraft Wind\n\n  Baltic Fibre  \n",
                type="List",
                unit="",
                page=12,
                source="",
                quote="",
                confidence="Low",
            ),
            _finding(
                key="esg",
                label="ESG incidents",
                value="One reportable incident",
                type="Text",
                unit="",
                page=None,
                source="",
                quote="One incident occurred",
                confidence="High",
                origin="adhoc",
            ),
            _finding(key="exits", label="Exits", value="", confidence="Not_Found"),
            _finding(key="team", label="Team changes", value="   ", confidence="Low"),
        ],
        note={
            "highlights": ["NAV rose 3.1%.", "  "],
            "quotes": [{"text": "We expect two calls.", "page": 4}, {"text": "Volumes fell."}],
            "events_and_risks": ["Grid delay."],
            "outlook": "Stable distributions.",
        },
        adhoc_keywords=[{"name": "ESG incidents", "type": "Text"}],
        now=_T0,
    )
    await _assign(
        engine,
        owner,
        full.id,
        datetime(2026, 11, 21, 10, 0, tzinfo=timezone.utc),
        investment_id=fund,
    )
    await _create(
        engine,
        owner,
        filename="sparse.pdf",
        fund_name_read=None,
        period_label=None,
        manager_name_read=None,
        currency=None,
        language="xx",
        page_count=None,
        document_date=None,
        findings=[
            _finding(),
            _finding(key="exits", label="Exits", value="", confidence="Not_Found"),
        ],
        note={"highlights": [], "quotes": [], "events_and_risks": [], "outlook": ""},
        now=_T0 + timedelta(1),
    )
    bare = await _create(
        engine, owner, filename="bare.pdf", findings=[], language=None, now=_T0 + timedelta(2)
    )
    await _assign(engine, owner, bare.id, _T0 + timedelta(2), manager_name="Gamma Capital")


async def test_live_reading(web_client_factory: Any, seeded_users: dict[str, UUID]) -> None:
    client, app = await web_client_factory("owner")
    await _seed_full_reading(app.state.engine, seeded_users["owner"])

    response = await client.get("/api/reading-desk/readings/1")
    assert response.status_code == 200
    body = response.text

    # The head, out of band.
    crumb = _element(body, r'<h2 class="pf-view__title pf-crumb"[^>]*>')
    assert 'id="reading-desk-title"' in crumb and 'hx-swap-oob="outerHTML"' in crumb
    back = _element(body, r'<button[^>]*data-pf-rd="back"[^>]*>')
    assert 'hx-get="/api/reading-desk/head"' in back and 'hx-swap="none"' in back
    assert '<span class="pf-crumb__here">R-0001 · Quarterly report</span>' in body
    assert (
        '<span class="pf-view__asof" id="reading-desk-asof" hx-swap-oob="outerHTML">'
        "Read 2026-11-20 09:15 UTC</span>"
    ) in body

    # The head panel.
    assert 'data-pf-rd="file" data-pf-rd-state="live" data-pf-rd-reading="R-0001"' in body
    assert "Nordlicht Infrastructure Fund II · Q3 2026</h3>" in body
    assert "Quarterly report · as read from Nordlicht_Q3.pdf</p>" in body
    for fact in (
        "<dt>Document date</dt><dd>2026-09-30</dd>",
        "<dt>Manager as read</dt><dd>Nordlicht Capital Partners</dd>",
        "<dt>Currency</dt><dd>EUR</dd>",
        "<dt>Language</dt><dd>German</dd>",
        '<dt>Pages</dt><dd>24 <span class="pf-hint">as reported by the model</span></dd>',
        "<dt>Read</dt><dd>2026-11-20 · Olivia Owner</dd>",
        "<dt>Assigned to</dt><dd>Nordlicht Infrastructure II "
        '<span class="pf-hint">investment · Olivia Owner, 2026-11-21</span>',
    ):
        assert fact in body, fact
    # The owner's page continues the fact with the opener of the assign panel.
    opener = _element(body, r'<button[^>]*data-pf-rd-toggle="rd-assign"[^>]*>Change…</button>')
    assert 'aria-expanded="false"' in opener

    # The findings.
    assert "4 of 6 fields found · values and quotes are the model's reading" in html.unescape(body)
    assert body.count('data-pf-rd="finding"') == 4
    assert (
        '<span style="white-space:nowrap">41,236,000<span class="pf-table__unit">EUR</span></span>'
    ) in body
    assert '<span style="white-space:nowrap">9.4%</span>' in body
    assert '<span class="pf-table__unit">%</span>' not in body
    assert "<ul><li>Fjordkraft Wind</li><li>Baltic Fibre</li></ul>" in body
    assert "<q>Capital account balance EUR 41,236,000</q>" in body
    assert '<span class="pf-table__sub">as read, p. 7 · Capital account</span>' in body
    assert '<span class="pf-table__sub">as read · Performance table</span>' in body
    assert '<span class="pf-table__sub">as read, p. 12</span>' in body
    assert '<span class="pf-table__sub">as read</span>' in body
    assert '<span class="pf-state pf-state--high">High</span>' in body
    assert '<span class="pf-state pf-state--medium">Medium</span>' in body
    assert '<span class="pf-state pf-state--low">Low</span>' in body
    assert 'ESG incidents<span class="pf-kind">extra keyword</span>' in body
    assert "<span>One reportable incident</span>" in body
    assert "<summary>2 fields were not found in the document</summary>" in body
    assert "<p>Exits · Team changes</p>" in body

    # The note.
    note = body[body.index('data-pf-rd="note"') :]
    assert "in the document&#39;s language (German) · quotes are as read" in note
    assert "<li>NAV rose 3.1%.</li>" in note
    assert note.count("<li>") == 2
    assert "We expect two calls.<cite>as read, p. 4</cite>" in note
    assert "Volumes fell.<cite>as read</cite>" in note
    assert "<li>Grid delay.</li>" in note
    assert "<p>Stable distributions.</p>" in note
    for title in ("Highlights", "Quotes", "Events and risks", "Outlook"):
        assert f'<h4 class="pf-block__title">{title}</h4>' in note

    # How this was read.
    assert 'data-pf-rd="how"' in body
    for fact in (
        "<dt>Model</dt><dd>vendor/model-a</dd>",
        "<dt>Document type</dt><dd>Quarterly report, profile version 1</dd>",
        "<dt>Prompt version</dt><dd>0f1e2d3c</dd>",
        "<dt>Extra keywords</dt><dd>ESG incidents (Text)</dd>",
        "<dt>Came in as</dt><dd>Upload</dd>",
        "<dt>File</dt><dd>120.6 KB · SHA-256 aaaaaa…aaaa</dd>",
    ):
        assert fact in body, fact
    assert "decided by the model" not in body


def test_plain_numbers_are_grouped_for_reading() -> None:
    """A plain number's thousands are grouped for reading; nothing stored changes (D-RD-2-36).

    Five and more integer digits always; four only for an amount, which
    carries a currency code — a vintage year stays as read.
    """
    cases = [
        ("Number", "41236000", "EUR", "41,236,000"),
        ("Number", "9600", "EUR", "9,600"),
        ("Number", "9600", "", "9600"),
        ("Number", "2024", "", "2024"),
        ("Number", "12500", "", "12,500"),
        ("Number", "-1200000.50", "USD", "-1,200,000.50"),
        ("Number", "1.25", "x", "1.25"),
        ("Number", "41,236,000", "EUR", "41,236,000"),
        ("Text", "41236000", "EUR", "41236000"),
    ]
    for field_type, value, unit, shown in cases:
        finding = _finding(type=field_type, value=value, unit=unit)
        [row] = project_findings([finding])["rows"]
        assert row["value"] == shown, (field_type, value, unit)
        assert finding["value"] == value


async def test_sparse_readings(web_client_factory: Any, seeded_users: dict[str, UUID]) -> None:
    client, app = await web_client_factory("owner")
    await _seed_full_reading(app.state.engine, seeded_users["owner"])

    sparse = (await client.get("/api/reading-desk/readings/2")).text
    assert ">sparse.pdf</h3>" in sparse
    for fact in (
        "<dt>Document date</dt><dd>—</dd>",
        "<dt>Manager as read</dt><dd>—</dd>",
        "<dt>Currency</dt><dd>—</dd>",
        "<dt>Language</dt><dd>xx</dd>",
        "<dt>Pages</dt><dd>—</dd>",
        '<dt>Assigned to</dt><dd><span class="pf-state pf-state--unassigned">Unassigned</span>',
    ):
        assert fact in sparse, fact
    _element(sparse, r'<button[^>]*data-pf-rd-toggle="rd-assign"[^>]*>Assign…</button>')
    assert "1 of 2 fields found" in sparse
    assert "<summary>1 field was not found in the document</summary>" in sparse
    assert 'data-pf-rd="note"' not in sparse
    assert "Extra keywords" not in sparse

    bare = (await client.get("/api/reading-desk/readings/3")).text
    assert 'data-pf-rd="findings"' not in bare
    assert "<dt>Language</dt><dd>—</dd>" in bare
    assert "in the document&#39;s language · quotes are as read" in bare
    assert 'Gamma Capital <span class="pf-hint">manager name · Olivia Owner, 2026-11-22</span>' in (
        bare
    )


@pytest.mark.parametrize("role", ["owner", "member", "auditor"])
async def test_reading_actions_per_role(
    web_client_factory: Any, seeded_users: dict[str, UUID], role: str
) -> None:
    client, app = await web_client_factory(role)
    await _seed_full_reading(app.state.engine, seeded_users["owner"])
    response = await client.get("/api/reading-desk/readings/1")
    assert response.status_code == 200
    body = response.text

    if role == "auditor":
        assert "Read again…" not in body
        assert 'id="rd-again"' not in body
        assert "Go to Read" not in body
    else:
        toggle = _element(body, r'<button[^>]*data-pf-rd-toggle="rd-again"[^>]*>')
        assert 'aria-expanded="false"' in toggle and 'aria-controls="rd-again"' in toggle
        again = _element(body, r'<div class="pf-note pf-note--info" id="rd-again"[^>]*>')
        assert " hidden" in again and 'data-pf-rd="again"' in again
        assert "The document is not kept, so reading it again needs the file." in body
        go = _element(body, r'<button[^>]*data-pf-rd-tab="read"[^>]*>')
        assert 'data-pf-rd="back"' in go and 'hx-get="/api/reading-desk/head"' in go

    if role == "auditor":
        for absent in (
            "Remove reading",
            "Change…",
            "Assign…",
            "Proposed assignment",
            "Keep",
            'id="rd-assign"',
            'id="rd-remove"',
            'data-pf-rd="menu"',
            "hx-post",
        ):
            assert absent not in body, absent
        return

    # The menu in the head panel and its one item.
    assert 'data-pf-rd="menu"' in _element(body, r'<details class="pf-menu"[^>]*>')
    assert 'aria-label="More actions for R-0001"' in body
    item = _element(
        body, r'<button[^>]*data-pf-rd-toggle="rd-remove"[^>]*>Remove reading…</button>'
    )
    assert 'aria-expanded="false"' in item and "pf-menu__item--danger" in item
    # The remove panel, closed.
    remove = _element(body, r'<div class="pf-panel" id="rd-remove"[^>]*>')
    assert " hidden" in remove
    panel = body[body.index('id="rd-remove"') :]
    assert "Remove reading R-0001?</h3>" in panel
    assert "Required. It is shown with the removed entry and recorded in the audit log." in panel
    textarea = _element(panel, r'<textarea[^>]*name="reason"[^>]*>')
    assert " required" in textarea
    assert 'hx-post="/api/reading-desk/readings/1/remove"' in panel
    assert ">Remove reading</button>" in panel and ">Keep</button>" in panel
    # The assign panel, closed; R-0001 is assigned to its investment.
    assign = _element(body, r'<div class="pf-panel" id="rd-assign"[^>]*>')
    assert " hidden" in assign
    panel = body[body.index('id="rd-assign"') : body.index('id="rd-remove"')]
    assert '<h3 class="pf-panel__title">Assign this reading</h3>' in panel
    assert ">Unassign</button>" in panel and ">Close</button>" in panel
    assert panel.count('hx-post="/api/reading-desk/readings/1/assign"') == 3
    assert re.search(r'<option value="[0-9a-f-]{36}" selected>Nordlicht Infrastructure II<', panel)
    assert "Proposed assignment" not in body


async def test_untrusted_text_is_escaped(
    web_client_factory: Any, seeded_users: dict[str, UUID]
) -> None:
    client, app = await web_client_factory("owner")
    await _create(
        app.state.engine,
        seeded_users["owner"],
        filename=f"{_SCRIPT}.pdf",
        findings=[_finding(value=_SCRIPT, type="Text", quote=_SCRIPT)],
        note={"highlights": [_SCRIPT], "outlook": _SCRIPT},
    )

    reading = (await client.get("/api/reading-desk/readings/1")).text
    library = await _library(client)
    for body in (reading, library):
        assert _SCRIPT_ESCAPED in body
        assert _SCRIPT not in body
    assert reading.count(_SCRIPT_ESCAPED) >= 5


async def test_removed_reading(web_client_factory: Any, seeded_users: dict[str, UUID]) -> None:
    client, app = await web_client_factory("auditor")
    engine = app.state.engine
    owner, member = seeded_users["owner"], seeded_users["member"]
    draft = await _create(
        engine,
        owner,
        filename="draft.pdf",
        fund_name_read="Tombstone Fund",
        period_label="Q9 2099",
        manager_name_read="Shadow Partners",
        now=_T0,
    )
    await _remove(
        engine, member, draft.id, "Draft superseded.", datetime(2026, 11, 22, tzinfo=timezone.utc)
    )
    await _create(
        engine, owner, filename="old.pdf", document_date=date(2015, 6, 30), page_count=1, now=_T0
    )
    await _purge(
        engine,
        owner,
        years=7,
        today=date(2026, 11, 20),
        now=datetime(2026, 11, 23, 8, 0, tzinfo=timezone.utc),
    )

    response = await client.get("/api/reading-desk/readings/1")
    assert response.status_code == 200
    body = response.text
    assert '<span class="pf-crumb__here">R-0001</span>' in body
    assert '<span class="pf-state pf-state--removed">Removed</span>' in body
    assert 'data-pf-rd="file" data-pf-rd-state="removed" data-pf-rd-reading="R-0001"' in body
    assert "This reading was removed." in body
    assert '<h3 class="pf-panel__title">Removal</h3>' in body
    assert '<h3 class="pf-panel__title">What was read</h3>' in body
    for fact in (
        "<dt>Removed</dt><dd>2026-11-22 · Mark Member</dd>",
        "<dt>Reason</dt><dd>Draft superseded.</dd>",
        "<dt>Reading</dt><dd>R-0001</dd>",
        "<dt>File</dt><dd>draft.pdf</dd>",
        "<dt>Size</dt><dd>120.6 KB · 24 pages</dd>",
        "<dt>Read</dt><dd>2026-11-20 · Olivia Owner</dd>",
        "<dt>Came in as</dt><dd>Upload</dd>",
    ):
        assert fact in body, fact
    for hidden in (
        "Tombstone Fund",
        "Q9 2099",
        "Shadow Partners",
        "Quarterly report",
        "EUR",
        "vendor/model-a",
        "English",
    ):
        assert hidden not in body, hidden

    retention = (await client.get("/api/reading-desk/readings/2")).text
    assert (
        "<dt>Reason</dt><dd>Retention run of 2026-11-23 — documents older than 7 years "
        "(before 2019-11-20)</dd>"
    ) in retention
    assert "<dt>Size</dt><dd>120.6 KB · 1 page</dd>" in retention


async def test_one_year_retention_reads_singular(
    web_client_factory: Any, seeded_users: dict[str, UUID]
) -> None:
    """F-RD-2-9: a run of one year says "1 year" in the Library row and on the reading."""
    client, app = await web_client_factory("owner")
    engine = app.state.engine
    owner = seeded_users["owner"]
    await _create(engine, owner, filename="last-year.pdf", document_date=date(2024, 6, 30), now=_T0)
    await _purge(
        engine,
        owner,
        years=1,
        today=date(2026, 11, 20),
        now=datetime(2026, 11, 23, 8, 0, tzinfo=timezone.utc),
    )

    row = _row_html(await _library(client, removed="1"), "R-0001")
    assert "Retention run: documents older than 1 year (before 2025-11-20)." in row

    reading = (await client.get("/api/reading-desk/readings/1")).text
    assert (
        "<dt>Reason</dt><dd>Retention run of 2026-11-23 — documents older than 1 year "
        "(before 2025-11-20)</dd>"
    ) in reading
    for body in (row, reading):
        assert "1 years" not in body


async def test_reading_not_found(
    web_client_factory: Any,
    seeded_users: dict[str, UUID],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    client, app = await web_client_factory("auditor")
    await _other_tenant_reading(fresh_superuser_engine, app.state.engine, count=2)
    await _create(app.state.engine, seeded_users["owner"])

    assert (await client.get("/api/reading-desk/readings/1")).status_code == 200
    for number in ("2", "99"):
        response = await client.get(f"/api/reading-desk/readings/{number}")
        assert response.status_code == 404
        assert response.json() == {"detail": "Reading not found."}


async def test_head(web_client_factory: Any) -> None:
    client, _app = await web_client_factory("auditor")
    response = await client.get("/api/reading-desk/head")
    assert response.status_code == 200
    body = response.text
    assert (
        '<h2 class="pf-view__title" id="reading-desk-title" hx-swap-oob="outerHTML">'
        "Reading Desk</h2>"
    ) in body
    assert (
        '<span class="pf-view__asof" id="reading-desk-asof" hx-swap-oob="outerHTML"></span>'
    ) in body
    assert "pf-crumb" not in body


# ---------------------------------------------------------------------------
# The page address
# ---------------------------------------------------------------------------


async def test_reading_page_address(
    web_client_factory: Any,
    seeded_users: dict[str, UUID],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    client, app = await web_client_factory("owner")
    engine = app.state.engine
    owner = seeded_users["owner"]
    await _other_tenant_reading(fresh_superuser_engine, engine, count=3)
    await _create(engine, owner)
    gone = await _create(engine, owner, now=_T0 + timedelta(1))
    await _remove(engine, owner, gone.id, "Wrong file.", _T0 + timedelta(2))

    response = await client.get("/assistants/readings/1")
    assert response.status_code == 200
    body = response.text
    assert " hidden" not in _element(body, r'<section class="pf-section" id="reading-desk"[^>]*>')
    assert " hidden" in _element(body, r'<section class="pf-section" id="shirley"[^>]*>')
    assert 'hx-get="/scraper/section?reading=1"' in body

    assert (await client.get("/assistants/readings/2")).status_code == 200
    for number in ("3", "99"):
        missing = await client.get(f"/assistants/readings/{number}")
        assert missing.status_code == 404
        assert missing.json() == {"detail": "Reading not found."}

    plain = (await client.get("/assistants")).text
    assert 'hx-get="/scraper/section"' in plain
    assert "/scraper/section?reading" not in plain


# ---------------------------------------------------------------------------
# The write side of one reading (P-RD-2c1)
# ---------------------------------------------------------------------------

_ROOT_TAG = re.compile(r'<div class="pf-reading-desk" data-pf-rd="root"[^>]*>')
_ROOT_TOKEN = re.compile(r"""hx-headers='\{"X-CSRF-Token": "([^"]+)"\}'""")
_META_TOKEN = re.compile(r'<meta name="csrf-token" content="([^"]+)">')


async def _section_token(client: AsyncClient) -> str:
    """The token the Section root carries for owner and member."""
    body = (await client.get("/scraper/section")).text
    match = _ROOT_TOKEN.search(_element(body, _ROOT_TAG.pattern))
    assert match is not None, "the Section root carries no token"
    return match.group(1)


async def _page_token(client: AsyncClient) -> str:
    """The session's token as any signed-in page gives it, auditor included."""
    body = (await client.get("/assistants")).text
    match = _META_TOKEN.search(body)
    assert match is not None, "the page carries no csrf-token meta"
    return match.group(1)


def _today() -> str:
    """The day a write made now is stamped with, in UTC."""
    return datetime.now(timezone.utc).date().isoformat()


async def _post(
    client: AsyncClient, number: int, action: str, token: str | None, **form: str
) -> Any:
    headers = {"X-CSRF-Token": token} if token is not None else {}
    return await client.post(
        f"/api/reading-desk/readings/{number}/{action}",
        data=form,
        headers=headers,
        follow_redirects=False,
    )


async def _row(
    engine: AsyncEngine, user_id: UUID, number: int, tenant_id: UUID = SENTINEL_TENANT_ID
) -> DocumentReadingDTO:
    async with tenant_context(engine, tenant_id, user_id=user_id) as session:
        reading = await DocumentReadingRepository(session).get_by_number(number)
    assert reading is not None, number
    return reading


async def _held(
    engine: AsyncEngine,
    user_id: UUID,
    pairs: list[tuple[str, str | None]],
    tenant_id: UUID = SENTINEL_TENANT_ID,
) -> list[UUID]:
    """Investments ``(name, manager_name)`` under one asset class."""
    async with tenant_context(engine, tenant_id, user_id=user_id) as session:
        asset_class = await AssetClassRepository(session).create(
            code="rd_held", display_name="RD Held"
        )
        repo = InvestmentRepository(session)
        return [
            (
                await repo.create(
                    name=name,
                    investment_type="private_equity",
                    asset_class_id=asset_class.id,
                    currency="EUR",
                    created_by=user_id,
                    manager_name=manager,
                )
            ).id
            for name, manager in pairs
        ]


async def _other_tenant(
    fresh_superuser_engine: AsyncEngine, engine: AsyncEngine
) -> tuple[UUID, UUID]:
    """A second tenant and a user of it: ``(tenant_id, user_id)``."""
    other_tenant = uuid4()
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (id, name, subdomain) VALUES (:id, 'Other', 'other-rd')"),
            {"id": str(other_tenant)},
        )
    async with tenant_context(engine, other_tenant) as session:
        other_user = await UserRepository(session).create(
            email="pm@other-rd.example", password_hash="x" * 8
        )
    return other_tenant, other_user.id


def _assign_panel(body: str) -> str:
    start = body.index('id="rd-assign"')
    return body[start : body.index('id="rd-remove"', start)]


def _unassigned(reading: DocumentReadingDTO) -> bool:
    return (
        reading.investment_id is None
        and reading.manager_name is None
        and reading.assigned_by is None
        and reading.assigned_at is None
    )


@pytest.mark.parametrize("role", ["owner", "member", "auditor"])
async def test_frame_root_carries_the_csrf_token_for_writers_only(
    web_client_factory: Any, role: str
) -> None:
    client, _app = await web_client_factory(role)
    body = (await client.get("/scraper/section")).text
    root = _element(body, _ROOT_TAG.pattern)
    if role == "auditor":
        assert "hx-headers" not in root
        assert "X-CSRF-Token" not in body
        return
    match = _ROOT_TOKEN.search(root)
    assert match is not None, root
    assert match.group(1) == await _page_token(client)


async def test_assign_to_an_investment(
    web_client_factory: Any, seeded_users: dict[str, UUID]
) -> None:
    client, app = await web_client_factory("owner")
    engine = app.state.engine
    owner = seeded_users["owner"]
    _alpha, fund = await _held(engine, owner, [("Alpha Fund", None), ("Nordlicht II", None)])
    await _create(engine, owner)
    token = await _section_token(client)

    response = await _post(client, 1, "assign", token, to="investment", investment_id=str(fund))
    assert response.status_code == 200, response.text
    body = response.text
    assert 'data-pf-rd="file" data-pf-rd-state="live" data-pf-rd-reading="R-0001"' in body
    assert (
        "<dt>Assigned to</dt><dd>Nordlicht II "
        f'<span class="pf-hint">investment · Olivia Owner, {_today()}</span>'
    ) in body
    assert f'<option value="{fund}" selected>Nordlicht II</option>' in body
    assert ">Unassign</button>" in body

    row = await _row(engine, owner, 1)
    assert row.investment_id == fund
    assert row.manager_name is None
    assert row.assigned_by == owner


async def test_assign_to_a_manager_name(
    web_client_factory: Any, seeded_users: dict[str, UUID]
) -> None:
    client, app = await web_client_factory("member")
    engine = app.state.engine
    member = seeded_users["member"]
    await _create(engine, seeded_users["owner"], fund_name_read="Successor Fund")
    token = await _section_token(client)

    response = await _post(client, 1, "assign", token, to="manager", manager_name="  Gamma LLP ")
    assert response.status_code == 200, response.text
    body = response.text
    assert (
        "<dt>Assigned to</dt><dd>Gamma LLP "
        f'<span class="pf-hint">manager name · Mark Member, {_today()}</span>'
    ) in body
    manager_field = _element(body, r'<input[^>]*id="rd-assign-manager"[^>]*>')
    assert 'value="Gamma LLP"' in manager_field

    row = await _row(engine, member, 1)
    assert row.manager_name == "Gamma LLP"
    assert row.investment_id is None
    assert row.assigned_by == member


async def test_unassign(web_client_factory: Any, seeded_users: dict[str, UUID]) -> None:
    client, app = await web_client_factory("owner")
    engine = app.state.engine
    owner = seeded_users["owner"]
    reading = await _create(engine, owner)
    await _assign(engine, owner, reading.id, _T0, manager_name="Gamma Capital")
    token = await _section_token(client)

    response = await _post(client, 1, "assign", token, to="none")
    assert response.status_code == 200, response.text
    body = response.text
    assert (
        '<dt>Assigned to</dt><dd><span class="pf-state pf-state--unassigned">Unassigned</span>'
    ) in body
    _element(body, r'<button[^>]*data-pf-rd-toggle="rd-assign"[^>]*>Assign…</button>')
    assert ">Unassign</button>" not in body
    assert 'data-pf-rd="unassign"' not in body

    row = await _row(engine, owner, 1)
    assert row.investment_id is None
    assert row.manager_name is None


@pytest.mark.parametrize("kind", ["investment", "manager"])
async def test_assign_the_proposal(
    web_client_factory: Any, seeded_users: dict[str, UUID], kind: str
) -> None:
    client, app = await web_client_factory("owner")
    engine = app.state.engine
    owner = seeded_users["owner"]
    [fund] = await _held(
        engine, owner, [("Nordlicht Infrastructure II", "Nordlicht Capital Partners")]
    )
    if kind == "investment":
        await _create(engine, owner, fund_name_read="  nordlicht INFRASTRUCTURE ii ")
        lead = "Proposed assignment: investment “Nordlicht Infrastructure II”"
        why = "— the fund name as read matches this investment."
    else:
        await _create(
            engine,
            owner,
            fund_name_read="Unheld Fund",
            manager_name_read="NORDLICHT capital partners",
        )
        lead = "Proposed assignment: manager “Nordlicht Capital Partners”"
        why = "— the manager as read is the manager of one of your investments."

    page = (await client.get("/api/reading-desk/readings/1")).text
    assert 'data-pf-rd="proposal"' in page
    assert lead in page and why in page
    button = _element(page, r'<button[^>]*data-pf-rd="proposal-assign"[^>]*>')
    assert 'hx-post="/api/reading-desk/readings/1/assign"' in button
    assert 'hx-target="#rd-reading"' in button
    assert 'data-pf-rd="proposal-other"' not in page
    assert " hidden" not in _element(page, r'<div class="pf-panel" id="rd-assign"[^>]*>')
    panel = _assign_panel(page)
    assert '<h3 class="pf-panel__title">Or assign it yourself</h3>' in panel
    assert ">Close</button>" not in panel
    assert ">Unassign</button>" not in panel
    assert 'data-pf-rd-toggle="rd-assign"' not in page

    token = await _section_token(client)
    response = await _post(client, 1, "assign", token, to="proposal")
    assert response.status_code == 200, response.text
    body = response.text
    assert 'data-pf-rd="proposal"' not in body
    assert "Proposed assignment" not in body
    row = await _row(engine, owner, 1)
    if kind == "investment":
        assert row.investment_id == fund and row.manager_name is None
        assert "<dt>Assigned to</dt><dd>Nordlicht Infrastructure II " in body
    else:
        assert row.manager_name == "Nordlicht Capital Partners" and row.investment_id is None
        assert "<dt>Assigned to</dt><dd>Nordlicht Capital Partners " in body


async def test_assign_from_a_result_block(
    web_client_factory: Any, seeded_users: dict[str, UUID]
) -> None:
    client, app = await web_client_factory("owner")
    engine = app.state.engine
    owner = seeded_users["owner"]
    [fund] = await _held(engine, owner, [("Test Fund", None)])
    await _create(engine, owner, fund_name_read="Test Fund")
    await _create(engine, owner, fund_name_read="Nobody's Fund", manager_name_read="Nobody LLP")
    gone = await _create(engine, owner, fund_name_read="Test Fund")
    await _remove(engine, owner, gone.id, "Wrong file.", _T0 + timedelta(1))
    token = await _section_token(client)

    done = await _post(client, 1, "assign", token, to="proposal", view="result")
    assert done.status_code == 200, done.text
    assert 'data-pf-rd="assigned" data-pf-rd-state="done"' in done.text
    assert "Assigned to investment “Test Fund”." in done.text
    assert done.text.count('data-pf-rd="assigned"') == 1
    assert 'data-pf-rd="file"' not in done.text
    assert (await _row(engine, owner, 1)).investment_id == fund

    stale = await _post(client, 2, "assign", token, to="proposal", view="result")
    assert stale.status_code == 400
    assert 'data-pf-rd="assigned" data-pf-rd-state="refused"' in stale.text
    assert "The proposal no longer applies." in stale.text
    assert "Open R-0002 to assign it yourself." in stale.text
    assert 'data-pf-rd="file"' not in stale.text
    assert _unassigned(await _row(engine, owner, 2))

    removed = await _post(client, 3, "assign", token, to="proposal", view="result")
    assert removed.status_code == 409
    assert 'data-pf-rd="assigned" data-pf-rd-state="refused"' in removed.text
    assert "R-0003 was removed." in removed.text
    assert "Nothing was assigned." in removed.text
    assert 'data-pf-rd="file"' not in removed.text


async def test_assign_refusals_come_back_in_the_panel(
    web_client_factory: Any, seeded_users: dict[str, UUID]
) -> None:
    client, app = await web_client_factory("owner")
    engine = app.state.engine
    owner = seeded_users["owner"]
    await _held(engine, owner, [("Alpha Fund", None)])
    await _create(engine, owner)
    token = await _section_token(client)

    refusals: list[tuple[dict[str, str], str]] = [
        ({"to": "investment"}, "Choose an investment from the list."),
        (
            {"to": "investment", "investment_id": "not-a-uuid"},
            "Choose an investment from the list.",
        ),
        (
            {"to": "investment", "investment_id": str(uuid4())},
            "Choose an investment from the list.",
        ),
        ({"to": "manager", "manager_name": "   "}, "Enter a manager name."),
        (
            {"to": "manager", "manager_name": "M" * 201},
            "A manager name has at most 200 characters.",
        ),
        ({"to": "proposal"}, "The proposal no longer applies."),
    ]
    for form, refusal in refusals:
        response = await _post(client, 1, "assign", token, **form)
        assert response.status_code == 400, form
        body = response.text
        assert 'data-pf-rd="file" data-pf-rd-state="live"' in body, form
        error = body[body.index('data-pf-rd="assign-error"') :]
        assert f'<p class="pf-note__lead">{refusal}</p>' in error, form
        assert " hidden" not in _element(body, r'<div class="pf-panel" id="rd-assign"[^>]*>'), form
        assert _unassigned(await _row(engine, owner, 1)), form

    accepted = await _post(client, 1, "assign", token, to="manager", manager_name="M" * 200)
    assert accepted.status_code == 200
    assert 'data-pf-rd="assign-error"' not in accepted.text
    assert (await _row(engine, owner, 1)).manager_name == "M" * 200


async def test_assign_keeps_to_the_tenant(
    web_client_factory: Any,
    seeded_users: dict[str, UUID],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    client, app = await web_client_factory("owner")
    engine = app.state.engine
    owner = seeded_users["owner"]
    other_tenant, other_user = await _other_tenant(fresh_superuser_engine, engine)
    [foreign_fund] = await _held(engine, other_user, [("Foreign Fund", None)], other_tenant)
    for i in range(2):
        await _create(engine, other_user, tenant_id=other_tenant, filename=f"foreign-{i}.pdf")
    await _create(engine, owner)
    token = await _section_token(client)

    response = await _post(
        client, 1, "assign", token, to="investment", investment_id=str(foreign_fund)
    )
    assert response.status_code == 400
    assert "Choose an investment from the list." in response.text
    assert "Foreign Fund" not in response.text
    assert _unassigned(await _row(engine, owner, 1))

    for action, form in (
        ("assign", {"to": "manager", "manager_name": "Gamma"}),
        ("remove", {"reason": "Wrong file."}),
    ):
        missing = await _post(client, 2, action, token, **form)
        assert missing.status_code == 404, action
        assert missing.json() == {"detail": "Reading not found."}, action

    for number in (1, 2):
        foreign = await _row(engine, other_user, number, other_tenant)
        assert _unassigned(foreign) and not foreign.is_removed, number


async def test_assign_unknown_vocabulary(
    web_client_factory: Any, seeded_users: dict[str, UUID]
) -> None:
    client, app = await web_client_factory("owner")
    engine = app.state.engine
    owner = seeded_users["owner"]
    reading = await _create(engine, owner)
    await _assign(engine, owner, reading.id, _T0, manager_name="Gamma Capital")
    before = await _row(engine, owner, 1)
    token = await _section_token(client)

    for form in ({"to": "everything"}, {"to": "none", "view": "page"}):
        response = await _post(client, 1, "assign", token, **form)
        assert response.status_code == 400, form
        assert response.json() == {"detail": "Unknown assignment."}, form

    after = await _row(engine, owner, 1)
    assert after.manager_name == "Gamma Capital"
    assert after.assigned_at == before.assigned_at


async def test_remove_a_reading(web_client_factory: Any, seeded_users: dict[str, UUID]) -> None:
    client, app = await web_client_factory("member")
    engine = app.state.engine
    member = seeded_users["member"]
    await _create(engine, seeded_users["owner"])
    token = await _section_token(client)

    response = await _post(client, 1, "remove", token, reason="  Wrong file, superseded.  ")
    assert response.status_code == 200, response.text
    body = response.text
    assert 'data-pf-rd="file" data-pf-rd-state="removed" data-pf-rd-reading="R-0001"' in body
    assert "This reading was removed." in body
    assert "<dt>Reason</dt><dd>Wrong file, superseded.</dd>" in body
    assert f"<dt>Removed</dt><dd>{_today()} · Mark Member</dd>" in body
    assert "Fund X" not in body
    assert "Quarterly report" not in body

    row = await _row(engine, member, 1)
    assert row.is_removed
    assert row.removed_by == member
    assert row.removal_reason == "Wrong file, superseded."
    assert row.findings is None and row.note is None and row.search_text is None

    library = await _library(client, removed="1")
    assert _ROW.findall(library) == [("removed", "R-0001")]


async def test_remove_needs_a_reason(
    web_client_factory: Any, seeded_users: dict[str, UUID]
) -> None:
    client, app = await web_client_factory("owner")
    engine = app.state.engine
    owner = seeded_users["owner"]
    await _create(engine, owner)
    token = await _section_token(client)

    too_long = "x" * 2001
    for reason, refusal in (
        ("", "A reason is required."),
        ("   ", "A reason is required."),
        (too_long, "A reason has at most 2,000 characters."),
    ):
        response = await _post(client, 1, "remove", token, reason=reason)
        assert response.status_code == 400, refusal
        body = response.text
        assert 'data-pf-rd="file" data-pf-rd-state="live"' in body
        error = body[body.index('data-pf-rd="remove-error"') :]
        assert f'<p class="pf-note__lead">{refusal}</p>' in error
        assert " hidden" not in _element(body, r'<div class="pf-panel" id="rd-remove"[^>]*>')
        textarea = _element(body, r'<textarea[^>]*name="reason"[^>]*>')
        assert 'aria-invalid="true"' in textarea
        if reason == too_long:
            assert f">{too_long}</textarea>" in body
        assert not (await _row(engine, owner, 1)).is_removed


async def test_writes_on_a_removed_reading_answer_409(
    web_client_factory: Any, seeded_users: dict[str, UUID]
) -> None:
    client, app = await web_client_factory("owner")
    engine = app.state.engine
    owner = seeded_users["owner"]
    gone = await _create(engine, owner)
    await _remove(engine, owner, gone.id, "Draft superseded.", _T0 + timedelta(1))
    before = await _row(engine, owner, 1)
    token = await _section_token(client)

    for action, form in (
        ("assign", {"to": "manager", "manager_name": "Gamma Capital"}),
        ("remove", {"reason": "Again."}),
    ):
        response = await _post(client, 1, action, token, **form)
        assert response.status_code == 409, action
        assert 'data-pf-rd="file" data-pf-rd-state="removed"' in response.text, action
        assert "<dt>Reason</dt><dd>Draft superseded.</dd>" in response.text, action

    assert await _row(engine, owner, 1) == before


@pytest.mark.parametrize("role", ["owner", "member", "auditor"])
async def test_write_gates_per_role(
    web_client_factory: Any, seeded_users: dict[str, UUID], role: str
) -> None:
    client, app = await web_client_factory(role)
    engine = app.state.engine
    owner = seeded_users["owner"]
    await _create(engine, owner)
    await _create(engine, owner, now=_T0 + timedelta(1))
    token = await _page_token(client)

    assign = await _post(client, 1, "assign", token, to="manager", manager_name="Gamma Capital")
    remove = await _post(client, 2, "remove", token, reason="Wrong file.")
    if role == "auditor":
        for response in (assign, remove):
            assert response.status_code == 403
            assert response.json() == {"detail": "insufficient role"}
        assert _unassigned(await _row(engine, owner, 1))
        assert not (await _row(engine, owner, 2)).is_removed
    else:
        assert assign.status_code == 200, assign.text
        assert remove.status_code == 200, remove.text
        assert (await _row(engine, owner, 1)).manager_name == "Gamma Capital"
        assert (await _row(engine, owner, 2)).is_removed


async def test_writes_need_the_csrf_token(
    web_client_factory: Any, seeded_users: dict[str, UUID]
) -> None:
    client, app = await web_client_factory("owner")
    engine = app.state.engine
    owner = seeded_users["owner"]
    await _create(engine, owner)

    assign = await _post(client, 1, "assign", None, to="manager", manager_name="Gamma Capital")
    remove = await _post(client, 1, "remove", None, reason="Wrong file.")
    for response in (assign, remove):
        assert response.status_code == 403
        assert response.json() == {"detail": "CSRF token missing or invalid."}
    row = await _row(engine, owner, 1)
    assert _unassigned(row) and not row.is_removed


async def test_writes_without_a_session(
    web_client_factory: Any, seeded_users: dict[str, UUID]
) -> None:
    client, app = await web_client_factory(None)
    engine = app.state.engine
    owner = seeded_users["owner"]
    await _create(engine, owner)
    reference = await client.get("/scraper/section", follow_redirects=False)
    assert reference.status_code in (303, 401)

    for action, form in (
        ("assign", {"to": "manager", "manager_name": "Gamma Capital"}),
        ("remove", {"reason": "Wrong file."}),
    ):
        response = await _post(client, 1, action, "forged-token", **form)
        assert response.status_code == reference.status_code, action
        assert response.headers.get("location") == reference.headers.get("location"), action
    row = await _row(engine, owner, 1)
    assert _unassigned(row) and not row.is_removed


async def test_write_side_text_is_escaped(
    web_client_factory: Any, seeded_users: dict[str, UUID]
) -> None:
    client, app = await web_client_factory("owner")
    engine = app.state.engine
    owner = seeded_users["owner"]
    await _held(engine, owner, [(_SCRIPT, f"{_SCRIPT} Partners")])
    await _create(engine, owner, fund_name_read=_SCRIPT, manager_name_read=_SCRIPT)
    await _create(engine, owner, fund_name_read="Plain Fund", manager_name_read=None)
    token = await _section_token(client)

    # An assigned manager name, through the route.
    assigned = await _post(client, 2, "assign", token, to="manager", manager_name=_SCRIPT)
    assert assigned.status_code == 200
    # The reading with the proposal: picker option, suggestions, field value, proposal.
    page = (await client.get("/api/reading-desk/readings/1")).text
    assert "Proposed assignment: investment “" + _SCRIPT_ESCAPED + "”" in page
    assert f">{_SCRIPT_ESCAPED}</option>" in page
    assert f'<option value="{_SCRIPT_ESCAPED} Partners"></option>' in page
    assert f'<option value="{_SCRIPT_ESCAPED}"></option>' in page
    assert f'value="{_SCRIPT_ESCAPED}" placeholder' in page
    answer = await _post(client, 1, "assign", token, to="proposal", view="result")
    assert answer.status_code == 200
    assert f"Assigned to investment “{_SCRIPT_ESCAPED}”." in answer.text
    removed = await _post(client, 2, "remove", token, reason=_SCRIPT)
    assert removed.status_code == 200
    assert f"<dt>Reason</dt><dd>{_SCRIPT_ESCAPED}</dd>" in removed.text

    for body in (assigned.text, page, answer.text, removed.text):
        assert _SCRIPT_ESCAPED in body
        assert _SCRIPT not in body


async def test_assign_panel_states(web_client_factory: Any, seeded_users: dict[str, UUID]) -> None:
    client, app = await web_client_factory("owner")
    engine = app.state.engine
    owner = seeded_users["owner"]
    await _create(engine, owner, fund_name_read="Nothing Fund", manager_name_read="Nobody LLP")

    # A tenant without investments gets the manager name only.
    bare = (await client.get("/api/reading-desk/readings/1")).text
    assert 'data-pf-rd="assign-investment"' not in bare
    assert 'data-pf-rd="assign-manager"' in bare

    await _held(
        engine,
        owner,
        [
            ("Alpha Fund", "beta partners"),
            ("Gamma Fund", "Beta Partners"),
            ("Delta Fund", None),
            ("Epsilon Fund", "alpha capital"),
        ],
    )
    used = await _create(engine, owner, fund_name_read=None, now=_T0 + timedelta(1))
    await _assign(engine, owner, used.id, _T0, manager_name="Zeta Advisors")
    again = await _create(engine, owner, fund_name_read=None, now=_T0 + timedelta(2))
    await _assign(engine, owner, again.id, _T0, manager_name="ALPHA CAPITAL")

    body = (await client.get("/api/reading-desk/readings/1")).text
    assert "Proposed assignment" not in body
    opener = _element(body, r'<button[^>]*data-pf-rd-toggle="rd-assign"[^>]*>Assign…</button>')
    assert 'aria-expanded="false"' in opener
    assert " hidden" in _element(body, r'<div class="pf-panel" id="rd-assign"[^>]*>')
    panel = _assign_panel(body)
    assert '<h3 class="pf-panel__title">Assign this reading</h3>' in panel
    assert ">Close</button>" in panel
    assert ">Unassign</button>" not in panel
    assert 'data-pf-rd="assign-investment"' in panel
    assert 'value="Nobody LLP"' in _element(panel, r'<input[^>]*id="rd-assign-manager"[^>]*>')
    datalist = panel[panel.index('<datalist id="rd-assign-managers">') : panel.index("</datalist>")]
    assert re.findall(r'<option value="([^"]*)">', datalist) == [
        "alpha capital",
        "beta partners",
        "Zeta Advisors",
    ]


# ---------------------------------------------------------------------------
# The Retention tab (P-RD-2c2)
# ---------------------------------------------------------------------------

_HIDDEN_INPUT = re.compile(r'<input type="hidden" name="(years|cutoff|count)" value="([^"]*)">')
_RUN_ROW = re.compile(r'<tr data-pf-rd="retention-run">(.*?)</tr>', re.S)
_STALE = "The preview is out of date. Check it again before you confirm."
_BAD_YEARS = "Enter a whole number of years, from 1 to 100."


def _assert_retention_tab(body: str) -> None:
    """The owner's Retention tab and its hidden pane, as the frame renders them."""
    tab = _element(body, r'<button[^>]*data-pf-rd-tab="retention"[^>]*>Retention</button>')
    assert 'id="rd-tab-retention"' in tab
    assert 'aria-selected="false"' in tab
    assert 'aria-controls="rd-pane-retention"' in tab
    pane = _element(body, r'<div id="rd-pane-retention"[^>]*>')
    assert " hidden" in pane and 'data-pf-rd-pane="retention"' in pane
    loader = _element(body, r'<div id="rd-retention"[^>]*>')
    assert 'data-pf-rd="retention"' in loader
    assert 'hx-get="/api/reading-desk/retention"' in loader
    assert 'hx-trigger="rd:load"' in loader


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _cutoff(years: int, today: date | None = None) -> date:
    """Today (UTC) minus ``years`` calendar years, 29 February falling to the 28th."""
    day = today or _today_utc()
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def _noon(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 12, 0, tzinfo=timezone.utc)


async def _seed_retention(engine: AsyncEngine, owner: UUID, member: UUID) -> None:
    """Five readings around the 7-year cutoff; R-0001 and R-0002 are what it matches.

    R-0001 is dated the day before the cutoff; R-0002 has no document date
    and was read two years before it (so an 8-year cutoff still matches
    it); R-0003 is dated on the cutoff day; R-0004 is recent; R-0005 is old
    but already removed.
    """
    cutoff = _cutoff(7)
    await _create(engine, owner, filename="before.pdf", document_date=cutoff - timedelta(1))
    await _create(
        engine,
        owner,
        filename="undated.pdf",
        document_date=None,
        now=_noon(cutoff - timedelta(730)),
    )
    await _create(engine, owner, filename="on-the-day.pdf", document_date=cutoff)
    await _create(engine, owner, filename="recent.pdf", document_date=_today_utc() - timedelta(10))
    removed = await _create(
        engine, owner, filename="removed.pdf", document_date=cutoff - timedelta(3650)
    )
    await _remove(engine, member, removed.id, "Wrong file.", _T0)


async def _readings(
    engine: AsyncEngine, user_id: UUID, count: int, tenant_id: UUID = SENTINEL_TENANT_ID
) -> list[DocumentReadingDTO]:
    return [await _row(engine, user_id, number, tenant_id) for number in range(1, count + 1)]


async def _purges(engine: AsyncEngine, user_id: UUID, tenant_id: UUID = SENTINEL_TENANT_ID):
    async with tenant_context(engine, tenant_id, user_id=user_id) as session:
        return await DocumentReadingRepository(session).list_purges()


async def _retention(client: AsyncClient, **params: str) -> Any:
    return await client.get("/api/reading-desk/retention", params=params, follow_redirects=False)


async def _run(client: AsyncClient, token: str | None, **form: str) -> Any:
    headers = {"X-CSRF-Token": token} if token is not None else {}
    return await client.post(
        "/api/reading-desk/retention/run", data=form, headers=headers, follow_redirects=False
    )


def _confirmation(body: str) -> dict[str, str]:
    """The three values the preview's confirmation carries."""
    values = dict(_HIDDEN_INPUT.findall(body))
    assert set(values) == {"years", "cutoff", "count"}, values
    return values


def _state(body: str) -> str:
    match = re.search(r'data-pf-rd="retention-body" data-pf-rd-state="([a-z]+)"', body)
    assert match is not None, "no retention body"
    return match.group(1)


def _years_field(body: str) -> str:
    return _element(body, r'<input[^>]*name="years"[^>]*>')


@pytest.mark.parametrize("role", ["owner", "member", "auditor"])
async def test_retention_tab_is_the_owners(web_client_factory: Any, role: str) -> None:
    client, _app = await web_client_factory(role)
    response = await client.get("/scraper/section")
    assert response.status_code == 200
    body = response.text

    if role == "owner":
        _assert_retention_tab(body)
    else:
        assert "rd-tab-retention" not in body
        assert "rd-pane-retention" not in body
        assert "Retention" not in body


@pytest.mark.parametrize("role", ["owner", "member", "auditor"])
async def test_retention_routes_are_owner_only(
    web_client_factory: Any, seeded_users: dict[str, UUID], role: str
) -> None:
    client, app = await web_client_factory(role)
    engine = app.state.engine
    owner = seeded_users["owner"]
    await _create(engine, owner, document_date=_cutoff(7) - timedelta(1))
    token = await _page_token(client)

    plain = await _retention(client)
    preview = await _retention(client, years="7")
    run = await _run(client, token, years="7", cutoff=_cutoff(7).isoformat(), count="1")
    if role == "owner":
        for response in (plain, preview, run):
            assert response.status_code == 200, response.text
        assert (await _row(engine, owner, 1)).is_removed
        assert len(await _purges(engine, owner)) == 1
    else:
        for response in (plain, preview, run):
            assert response.status_code == 403
            assert response.json() == {"detail": "insufficient role"}
        assert not (await _row(engine, owner, 1)).is_removed
        assert await _purges(engine, owner) == []


async def test_retention_pane_idle(web_client_factory: Any) -> None:
    client, _app = await web_client_factory("owner")
    response = await _retention(client)
    assert response.status_code == 200
    body = response.text

    assert _state(body) == "idle"
    assert (
        "A retention run removes every reading whose document is older than the age you set. "
        "The findings and notes of those readings are deleted; the entries stay in the Library "
        "as removed."
    ) in body
    field = _years_field(body)
    assert 'value=""' in field
    assert 'inputmode="numeric"' in field
    assert " required" in field
    assert "aria-invalid" not in field
    assert (
        "The document date counts; where a document has none, the day it was read. "
        "Nothing is removed before you confirm."
    ) in body
    assert 'data-pf-rd="retention-none"' in body
    assert "No retention run so far." in body
    assert "hx-post" not in body
    assert 'data-pf-rd="retention-error"' not in body


async def test_retention_preview_counts_by_document_date(
    web_client_factory: Any, seeded_users: dict[str, UUID]
) -> None:
    client, app = await web_client_factory("owner")
    engine = app.state.engine
    owner = seeded_users["owner"]
    await _seed_retention(engine, owner, seeded_users["member"])
    before = await _readings(engine, owner, 5)
    cutoff = _cutoff(7).isoformat()

    response = await _retention(client, years="7")
    assert response.status_code == 200, response.text
    body = response.text
    assert _state(body) == "preview"
    assert 'data-pf-rd="retention-preview"' in body
    assert f"2 readings have a document date before {cutoff}." in body
    assert "The 2 entries stay in the Library as removed and name this run." in body
    assert ">Remove 2 readings</button>" in body
    assert _confirmation(body) == {"years": "7", "cutoff": cutoff, "count": "2"}
    keep = _element(
        body, r'<button[^>]*data-pf-rd-toggle="rd-retention-preview"[^>]*>Keep</button>'
    )
    assert 'type="button"' in keep
    assert 'hx-post="/api/reading-desk/retention/run"' in body
    assert 'value="7"' in _years_field(body)

    narrowed = (await _retention(client, years="8")).text
    assert _state(narrowed) == "preview"
    assert f"1 reading has a document date before {_cutoff(8).isoformat()}." in narrowed
    assert "Its findings and reading note will be deleted and cannot be restored." in narrowed
    assert ">Remove 1 reading</button>" in narrowed
    assert _confirmation(narrowed)["count"] == "1"

    assert await _readings(engine, owner, 5) == before
    assert await _purges(engine, owner) == []


async def test_retention_preview_of_nothing_offers_no_confirmation(
    web_client_factory: Any, seeded_users: dict[str, UUID]
) -> None:
    client, app = await web_client_factory("owner")
    engine = app.state.engine
    owner = seeded_users["owner"]
    await _create(engine, owner, document_date=_cutoff(7))

    response = await _retention(client, years="7")
    assert response.status_code == 200, response.text
    body = response.text
    assert _state(body) == "zero"
    assert 'data-pf-rd="retention-zero"' in body
    assert f"No reading has a document date before {_cutoff(7).isoformat()}." in body
    assert "There is nothing to remove." in body
    assert "hx-post" not in body
    assert "Keep" not in body
    assert 'data-pf-rd="retention-preview"' not in body
    assert await _purges(engine, owner) == []


async def test_retention_run(web_client_factory: Any, seeded_users: dict[str, UUID]) -> None:
    client, app = await web_client_factory("owner")
    engine = app.state.engine
    owner = seeded_users["owner"]
    await _seed_retention(engine, owner, seeded_users["member"])
    before = await _readings(engine, owner, 5)
    token = await _section_token(client)
    cutoff = _cutoff(7).isoformat()

    confirmation = _confirmation((await _retention(client, years="7")).text)
    response = await _run(client, token, **confirmation)
    assert response.status_code == 200, response.text
    body = response.text
    assert _state(body) == "done"
    assert 'data-pf-rd="retention-done"' in body
    assert "2 readings removed." in body
    assert "The run is recorded below." in body
    assert "hx-post" not in body

    [run] = await _purges(engine, owner)
    assert run.reading_count == 2 and run.years == 7 and run.cutoff.isoformat() == cutoff
    rows = _RUN_ROW.findall(body)
    assert rows == [
        f"<td>{run.run_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</td>"
        f"<td>Olivia Owner</td><td>Older than 7 years</td><td>{cutoff}</td>"
        '<td class="pf-num">2</td>'
    ]
    assert "No retention run so far." not in body

    after = await _readings(engine, owner, 5)
    for reading in after[:2]:
        assert reading.is_removed
        assert reading.purge_id == run.id
        assert reading.removal_reason is None
        assert reading.findings is None and reading.note is None
        assert reading.search_text is None
    assert after[2:] == before[2:]

    run_day = run.run_at.astimezone(timezone.utc).date().isoformat()
    removed_page = (await client.get("/api/reading-desk/readings/1")).text
    assert (
        f"Retention run of {run_day} — documents older than 7 years (before {cutoff})"
    ) in removed_page
    library = await _library(client, removed="1")
    states = dict((label, state) for state, label in _ROW.findall(library))
    assert states["R-0001"] == "removed" and states["R-0002"] == "removed"

    now = datetime.now(timezone.utc)
    await _purge(engine, owner, years=1, today=now.date(), now=now)
    runs = _RUN_ROW.findall((await _retention(client)).text)
    assert len(runs) == 2
    assert "<td>Older than 1 year</td>" in runs[0]
    assert "<td>Older than 7 years</td>" in runs[1]


async def test_retention_run_refuses_a_preview_that_no_longer_holds(
    web_client_factory: Any, seeded_users: dict[str, UUID]
) -> None:
    client, app = await web_client_factory("owner")
    engine = app.state.engine
    owner = seeded_users["owner"]
    await _seed_retention(engine, owner, seeded_users["member"])
    before = await _readings(engine, owner, 5)
    token = await _section_token(client)
    cutoff = _cutoff(7)

    for form in (
        {"years": "7", "cutoff": cutoff.isoformat(), "count": "1"},
        {"years": "7", "cutoff": (cutoff - timedelta(1)).isoformat(), "count": "2"},
    ):
        response = await _run(client, token, **form)
        assert response.status_code == 409, form
        body = response.text
        assert 'data-pf-rd="retention-error"' in body
        assert _STALE in body
        # The preview as it now stands.
        assert _state(body) == "preview"
        assert ">Remove 2 readings</button>" in body
        assert _confirmation(body) == {"years": "7", "cutoff": cutoff.isoformat(), "count": "2"}

    nothing = await _run(client, token, years="100", cutoff=_cutoff(100).isoformat(), count="0")
    assert nothing.status_code == 409
    assert _STALE in nothing.text
    assert _state(nothing.text) == "zero"
    assert "hx-post" not in nothing.text

    assert await _readings(engine, owner, 5) == before
    assert await _purges(engine, owner) == []


async def test_retention_refuses_an_age_it_cannot_use(
    web_client_factory: Any, seeded_users: dict[str, UUID]
) -> None:
    client, app = await web_client_factory("owner")
    engine = app.state.engine
    owner = seeded_users["owner"]
    await _create(engine, owner, document_date=_cutoff(7) - timedelta(1))
    token = await _section_token(client)

    for years in ("", "abc", "0", "-1", "101", "7.5", "0007"):
        response = await _retention(client, years=years)
        assert response.status_code == 400, repr(years)
        body = response.text
        assert _BAD_YEARS in body, repr(years)
        assert 'data-pf-rd="retention-error"' in body
        assert 'aria-invalid="true"' in _years_field(body), repr(years)
        assert 'data-pf-rd="retention-preview"' not in body
        assert "hx-post" not in body

    for years in ("100", " 7 "):
        response = await _retention(client, years=years)
        assert response.status_code == 200, repr(years)
        assert _BAD_YEARS not in response.text
    assert ">Remove 1 reading</button>" in (await _retention(client, years=" 7 ")).text

    refused = await _run(client, token, years="abc", cutoff=_cutoff(7).isoformat(), count="1")
    assert refused.status_code == 400
    assert _BAD_YEARS in refused.text
    assert not (await _row(engine, owner, 1)).is_removed
    assert await _purges(engine, owner) == []


async def test_retention_run_needs_the_csrf_token(
    web_client_factory: Any, seeded_users: dict[str, UUID]
) -> None:
    client, app = await web_client_factory("owner")
    engine = app.state.engine
    owner = seeded_users["owner"]
    await _create(engine, owner, document_date=_cutoff(7) - timedelta(1))

    response = await _run(client, None, years="7", cutoff=_cutoff(7).isoformat(), count="1")
    assert response.status_code == 403
    assert response.json() == {"detail": "CSRF token missing or invalid."}
    assert not (await _row(engine, owner, 1)).is_removed
    assert await _purges(engine, owner) == []


async def test_retention_keeps_to_the_tenant(
    web_client_factory: Any,
    seeded_users: dict[str, UUID],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    client, app = await web_client_factory("owner")
    engine = app.state.engine
    owner = seeded_users["owner"]
    old = _cutoff(7) - timedelta(1)

    other_tenant = uuid4()
    other_owner = uuid4()
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (id, name, subdomain) VALUES (:id, 'Other', 'other-rd')"),
            {"id": str(other_tenant)},
        )
        await conn.execute(
            text(
                """
                INSERT INTO users
                    (id, tenant_id, email, display_name, password_hash, roles, is_active)
                VALUES
                    (:id, :tid, 'owner@other-rd.example', 'Otto Other', :hash,
                     ARRAY['owner']::text[], TRUE)
                """
            ),
            {"id": str(other_owner), "tid": str(other_tenant), "hash": hash_password(_PASSWORD)},
        )
    await _create(engine, other_owner, tenant_id=other_tenant, document_date=old)
    await _create(engine, owner, document_date=old)
    token = await _section_token(client)

    preview = (await _retention(client, years="7")).text
    assert "1 reading has a document date before" in preview
    run = await _run(client, token, **_confirmation(preview))
    assert run.status_code == 200, run.text
    assert "1 reading removed." in run.text
    assert (await _row(engine, owner, 1)).is_removed
    assert not (await _row(engine, other_owner, 1, other_tenant)).is_removed
    assert await _purges(engine, other_owner, other_tenant) == []

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://other-rd.localhost"
    ) as other:
        await _login(other, "owner@other-rd.example")
        pane = await _retention(other)
        assert pane.status_code == 200, pane.text
        assert "No retention run so far." in pane.text
        assert 'data-pf-rd="retention-run"' not in pane.text
        assert "Olivia Owner" not in pane.text


async def test_retention_without_a_session(
    web_client_factory: Any, seeded_users: dict[str, UUID]
) -> None:
    client, app = await web_client_factory(None)
    engine = app.state.engine
    owner = seeded_users["owner"]
    await _create(engine, owner, document_date=_cutoff(7) - timedelta(1))
    reference = await client.get("/scraper/section", follow_redirects=False)
    assert reference.status_code in (303, 401)

    for response in (
        await _retention(client),
        await _retention(client, years="7"),
        await _run(client, "forged-token", years="7", cutoff=_cutoff(7).isoformat(), count="1"),
    ):
        assert response.status_code == reference.status_code
        assert response.headers.get("location") == reference.headers.get("location")
    assert not (await _row(engine, owner, 1)).is_removed
    assert await _purges(engine, owner) == []


# ---------------------------------------------------------------------------
# The investment page (P-RD-2d)
# ---------------------------------------------------------------------------

_INV_ROW = re.compile(r'<tr data-inv-reading="(R-\d+)">(.*?)</tr>', re.S)
_DASH = '<span class="pf-hint">—</span>'
_FIGURES_SENTENCE = (
    "Figures are as read from the documents by the Reading Desk; they are not book data "
    "and are not checked against the documents."
)


async def _investment_page(client: AsyncClient, investment_id: UUID) -> str:
    response = await client.get(f"/investments/{investment_id}")
    assert response.status_code == 200, response.text
    return response.text


def _readings_section(body: str) -> str:
    """The "Documents read" section of an investment page."""
    start = body.index('<section class="inv-section" id="inv-readings"')
    return body[start : body.index("</section>", start) + len("</section>")]


def _inv_rows(section: str) -> list[str]:
    return [label for label, _cells in _INV_ROW.findall(section)]


def _inv_row(section: str, label: str) -> str:
    rows = dict(_INV_ROW.findall(section))
    assert label in rows, label
    return rows[label]


@pytest.mark.parametrize("role", ["owner", "member", "auditor"])
async def test_investment_page_lists_its_readings(
    web_client_factory: Any, seeded_users: dict[str, UUID], role: str
) -> None:
    client, app = await web_client_factory(role)
    engine = app.state.engine
    owner = seeded_users["owner"]
    fund_x, fund_y = await _investments(engine, owner, "Fund X", "Fund Y")
    assigned = [
        await _create(
            engine,
            owner,
            filename="fund-x-q2.pdf",
            document_type="quarterly_report",
            document_date=date(2026, 6, 30),
        ),
        await _create(
            engine,
            owner,
            filename="fund-x-call-7.pdf",
            document_type="capital_notice",
            document_date=date(2026, 5, 6),
        ),
        await _create(
            engine,
            owner,
            filename="fund-x-q3.pdf",
            document_type="quarterly_report",
            document_date=date(2026, 9, 30),
            findings=[
                _finding(key="nav_lp", label="NAV, investor's share", value="41236000", unit="EUR"),
                _finding(key="tvpi", label="TVPI", value="1.25", unit="x"),
                _finding(key="net_irr", label="Net IRR", value="9.4", type="Percentage", unit="%"),
            ],
        ),
        await _create(
            engine,
            owner,
            filename="fund-x-letter.pdf",
            document_type="other",
            document_date=date(2026, 10, 22),
            findings=[],
        ),
    ]
    for reading in assigned:
        await _assign(engine, owner, reading.id, _T0, investment_id=fund_x)
    to_fund_y = await _create(engine, owner, filename="fund-y-q3.pdf")
    await _assign(engine, owner, to_fund_y.id, _T0, investment_id=fund_y)
    await _create(engine, owner, filename="unassigned-q3.pdf")
    to_manager = await _create(engine, owner, filename="manager-letter.pdf")
    await _assign(engine, owner, to_manager.id, _T0, manager_name="Example Capital")
    removed = await _create(engine, owner, filename="removed-q3.pdf")
    await _assign(engine, owner, removed.id, _T0, investment_id=fund_x)
    await _remove(engine, owner, removed.id, "Wrong file.", _T0 + timedelta(hours=1))

    page = await _investment_page(client, fund_x)
    section = _readings_section(page)
    assert _inv_rows(section) == ["R-0004", "R-0003", "R-0001", "R-0002"]
    assert (
        page.index('id="cashflows-title"')
        < page.index('id="readings-title"')
        < page.index('id="identifiers-title"')
    )
    assert '<h2 id="readings-title" class="inv-section__title">Documents read</h2>' in section
    assert (
        '<a class="btn btn--ghost" href="/assistants#reading-desk">Open the Reading Desk</a>'
    ) in section
    assert (
        "<th>Document date</th><th>Type</th><th>Document</th><th>As read</th><th>Reading</th>"
    ) in section
    assert _FIGURES_SENTENCE in section

    q3 = _inv_row(section, "R-0003")
    assert "<td>2026-09-30</td>" in q3
    assert "<td>Quarterly report</td>" in q3
    assert "<td>fund-x-q3.pdf</td>" in q3
    assert (
        "<td>NAV, investor's share 41,236,000 EUR · TVPI 1.25 x · Net IRR 9.4%</td>"
    ) in html.unescape(q3)
    assert '<a class="pf-table__link" href="/assistants/readings/3">R-0003</a>' in q3

    letter = _inv_row(section, "R-0004")
    assert "<td>Other document</td>" in letter
    assert f"<td>{_DASH}</td>" in letter

    for filename in (
        "fund-y-q3.pdf",
        "unassigned-q3.pdf",
        "manager-letter.pdf",
        "removed-q3.pdf",
    ):
        assert filename not in page, filename


async def test_investment_page_without_readings_shows_no_section(
    web_client_factory: Any, seeded_users: dict[str, UUID]
) -> None:
    client, app = await web_client_factory("owner")
    engine = app.state.engine
    owner = seeded_users["owner"]
    fund_x, fund_y = await _investments(engine, owner, "Fund X", "Fund Y")
    removed = await _create(engine, owner, filename="fund-y-q3.pdf")
    await _assign(engine, owner, removed.id, _T0, investment_id=fund_y)
    await _remove(engine, owner, removed.id, "Wrong file.", _T0 + timedelta(hours=1))
    await _create(engine, owner, filename="unassigned-q3.pdf")

    for fund in (fund_x, fund_y):
        page = await _investment_page(client, fund)
        for absent in (
            "inv-readings",
            "readings-title",
            "Documents read",
            "Open the Reading Desk",
        ):
            assert absent not in page, (fund, absent)


async def test_investment_page_as_read_by_document_type(
    web_client_factory: Any, seeded_users: dict[str, UUID]
) -> None:
    client, app = await web_client_factory("owner")
    engine = app.state.engine
    owner = seeded_users["owner"]
    [fund] = await _investments(engine, owner, "Fund X")
    seeds: list[tuple[str, date, list[dict[str, Any]]]] = [
        (
            "pitch_deck",
            date(2026, 1, 15),
            [
                _finding(
                    key="target_return",
                    label="Target return",
                    value="15% net IRR",
                    type="Text",
                    unit="",
                ),
                _finding(
                    key="fundraising_status",
                    label="Fundraising status",
                    value="first close held",
                    type="Text",
                    unit="",
                ),
                _finding(key="target_size", label="Target size", value="750000000", unit="USD"),
            ],
        ),
        (
            "capital_notice",
            date(2026, 5, 6),
            [
                _finding(
                    key="notice_kind", label="Kind of notice", value="call", type="Text", unit=""
                ),
                _finding(
                    key="amount_lp",
                    label="Amount, investor's share",
                    value="1920000",
                    unit="EUR",
                ),
                _finding(
                    key="value_date",
                    label="Value date",
                    value="2026-05-20",
                    type="Date",
                    unit="",
                ),
            ],
        ),
        (
            "quarterly_report",
            date(2026, 6, 30),
            [
                _finding(
                    key="nav_lp",
                    label="NAV, investor's share",
                    value="",
                    confidence="Not_Found",
                ),
                _finding(key="tvpi", label="TVPI", value="   ", unit="x"),
                _finding(key="net_irr", label="Net IRR", value="9.4%", type="Percentage", unit="%"),
                _finding(key="net_irr", label="Net IRR", value="1.0", type="Percentage", unit="%"),
                _finding(key="dpi", label="DPI", value="0.40", unit="x"),
                _finding(
                    key="adhoc_nav_lp",
                    label="Ad-hoc NAV",
                    value="41236000",
                    origin="adhoc",
                ),
            ],
        ),
        (
            "quarterly_report",
            date(2026, 9, 30),
            [_finding(key="dpi", label="DPI", value="0.40", unit="x")],
        ),
        (
            "other",
            date(2026, 10, 22),
            [_finding(key="nav_lp", label="NAV, investor's share", value="41236000")],
        ),
    ]
    for document_type, document_date, findings in seeds:
        reading = await _create(
            engine,
            owner,
            document_type=document_type,
            document_date=document_date,
            findings=findings,
        )
        await _assign(engine, owner, reading.id, _T0, investment_id=fund)

    section = _readings_section(await _investment_page(client, fund))
    assert _inv_rows(section) == ["R-0005", "R-0004", "R-0003", "R-0002", "R-0001"]

    def as_read(label: str) -> str:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", _inv_row(section, label), re.S)
        return html.unescape(cells[3])

    assert as_read("R-0001") == (
        "Target size 750,000,000 USD · Fundraising status first close held"
        " · Target return 15% net IRR"
    )
    assert as_read("R-0002") == (
        "Kind of notice call · Amount, investor's share 1,920,000 EUR · Value date 2026-05-20"
    )
    assert as_read("R-0003") == "Net IRR 9.4%"
    quarter = _inv_row(section, "R-0003")
    assert "DPI" not in quarter
    assert "Ad-hoc NAV" not in quarter
    assert as_read("R-0004") == _DASH
    assert as_read("R-0005") == _DASH


async def test_investment_page_orders_by_document_date(
    web_client_factory: Any, seeded_users: dict[str, UUID]
) -> None:
    client, app = await web_client_factory("owner")
    engine = app.state.engine
    owner = seeded_users["owner"]
    [fund] = await _investments(engine, owner, "Fund X")
    for document_date, now in (
        (date(2026, 3, 31), _T0),
        (None, datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)),
        (date(2026, 9, 30), _T0),
        (date(2026, 9, 30), _T0),
    ):
        reading = await _create(engine, owner, document_date=document_date, now=now)
        await _assign(engine, owner, reading.id, _T0, investment_id=fund)

    section = _readings_section(await _investment_page(client, fund))
    assert _inv_rows(section) == ["R-0004", "R-0003", "R-0002", "R-0001"]
    undated = _inv_row(section, "R-0002")
    assert undated.lstrip().startswith(f"<td>{_DASH}</td>")
    assert "2026-07-01" not in undated


async def test_investment_page_escapes_what_a_document_says(
    web_client_factory: Any, seeded_users: dict[str, UUID]
) -> None:
    client, app = await web_client_factory("owner")
    engine = app.state.engine
    owner = seeded_users["owner"]
    [fund] = await _investments(engine, owner, "Fund X")
    reading = await _create(
        engine,
        owner,
        filename=f"{_SCRIPT}.pdf",
        findings=[
            _finding(
                key="nav_lp", label=_SCRIPT, value="<b>value</b>", type="Text", unit="<i>unit</i>"
            )
        ],
    )
    await _assign(engine, owner, reading.id, _T0, investment_id=fund)

    section = _readings_section(await _investment_page(client, fund))
    assert _SCRIPT not in section
    assert "<script" not in section
    assert "<b>value" not in section
    assert "<i>unit" not in section
    assert section.count(_SCRIPT_ESCAPED) == 2
    assert "&lt;b&gt;value&lt;/b&gt; &lt;i&gt;unit&lt;/i&gt;" in section


async def test_investment_page_keeps_to_the_tenant(
    web_client_factory: Any,
    seeded_users: dict[str, UUID],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    client, app = await web_client_factory("owner")
    engine = app.state.engine
    owner = seeded_users["owner"]
    [fund] = await _investments(engine, owner, "Fund X")
    reading = await _create(engine, owner, filename="fund-x-q3.pdf")
    await _assign(engine, owner, reading.id, _T0, investment_id=fund)
    await _other_tenant_reading(fresh_superuser_engine, engine)
    async with fresh_superuser_engine.begin() as conn:
        result = await conn.execute(
            text(
                "UPDATE document_readings SET investment_id = :investment_id "
                "WHERE filename = 'foreign-0.pdf'"
            ),
            {"investment_id": str(fund)},
        )
    assert result.rowcount == 1

    page = await _investment_page(client, fund)
    assert _inv_rows(_readings_section(page)) == ["R-0001"]
    assert "foreign-0.pdf" not in page
