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

Assertions bind to ``data-pf-rd`` hooks, ids and copy, and to a class only
where the class is the point. All dates are fixed.
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
        '<span class="pf-hint">investment · Olivia Owner, 2026-11-21</span></dd>',
    ):
        assert fact in body, fact

    # The findings.
    assert "4 of 6 fields found · values and quotes are the model's reading" in html.unescape(body)
    assert body.count('data-pf-rd="finding"') == 4
    assert (
        '<span style="white-space:nowrap">41236000<span class="pf-table__unit">EUR</span></span>'
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
        '<dt>Assigned to</dt><dd><span class="pf-state pf-state--unassigned">'
        "Unassigned</span></dd>",
    ):
        assert fact in sparse, fact
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
    for absent in ("Remove reading", "Change…", "Proposed assignment", "Keep"):
        assert absent not in body, absent


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
