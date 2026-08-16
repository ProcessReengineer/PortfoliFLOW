# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the Cases web surface — the eighth Area (ADR-0107, C2).

ASGI-level tests over a live Postgres, mirroring the fixture pattern in
``tests/web/test_watch_desk.py`` (login helper, superuser-seeded
tenant/user, HTMX header simulation). They cover the C2 list experience:

* Area/nav — ``/cases`` renders the page and the HTMX branch the partial.
* Open cases — rows newest-first, owner display name, finding-origin badge.
* "Mine" filter — narrows to the current user's cases and back.
* Recently closed — at most five, newest first, closing-note excerpt.
* Archive — title / closing-note search, idle state on whitespace, the
  collapsed panel on the bare (no-``q``) load.
* Manual creation — ``POST /api/cases/new`` writes exactly one ``opened``
  entry (``actor='pm'``, ``actor_user_id`` set), the new row appears in the
  re-rendered list, and a blank title re-renders the form with an error and
  creates nothing.

C3a arms the read side: the detail-view tests below cover the full-page
``GET /cases/{case_id}`` — head, origin embed, timeline and rail — the 404
idiom for unknown / foreign ids, and the list rows now linking to it.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.config import get_config
from core.repositories._session import tenant_context
from core.repositories.case_attachment_repository import (
    CaseAttachmentRepository,
)
from core.repositories.case_repository import CaseDTO, CaseRepository
from core.repositories.irene_finding_repository import IreneFindingRepository
from core.tenant_constants import SENTINEL_TENANT_ID
from services.password_hashing import hash_password
from web.main import create_app
from web.settings import WebSettings

# A tiny, valid-enough PDF byte payload for the pin-document tests. The route
# trusts the declared content type (v1 does not sniff), so the bytes only need
# to be stable — sha256 and size are computed over exactly these bytes.
_SMALL_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

_T0 = datetime(2026, 7, 21, 9, 0, tzinfo=timezone.utc)


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; skipping live-DB Cases tests.",
            allow_module_level=False,
        )


# ---------------------------------------------------------------------------
# Fixtures (live DB)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fresh_superuser_engine() -> AsyncGenerator[AsyncEngine, None]:
    _require_db()
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


# Case tables are truncated explicitly (they would cascade off ``tenants``
# too, but naming them keeps the intent obvious). ``irene_finding`` is here
# because a from-finding case references it.
_TRUNCATE = text(
    "TRUNCATE TABLE case_attachments, case_entries, cases, "
    "irene_finding, irene_schedule, irene_watch_state, "
    "login_audit, sessions, audit_log, "
    "data_store_entries, users, tenants RESTART IDENTITY CASCADE"
)


@pytest_asyncio.fixture
async def reset_schema(
    fresh_superuser_engine: AsyncEngine,
) -> AsyncGenerator[None, None]:
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(_TRUNCATE)
    try:
        yield
    finally:
        async with fresh_superuser_engine.begin() as conn:
            await conn.execute(_TRUNCATE)


@pytest_asyncio.fixture
async def seeded_user(
    fresh_superuser_engine: AsyncEngine,
    reset_schema: None,
) -> tuple[UUID, str, str]:
    """Seed the primary tenant and its owner (PM1, display "S. Behrens")."""
    plaintext = "correct-horse-battery-staple"
    user_id = uuid4()
    email = "cases-owner@example.com"
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, 'minathena-capital')"
            ),
            {"id": str(SENTINEL_TENANT_ID), "name": "Sentinel Tenant"},
        )
        await conn.execute(
            text(
                """
                INSERT INTO users
                    (id, tenant_id, email, password_hash, display_name,
                     roles, is_active)
                VALUES
                    (:id, :tid, :email, :hash, :dn,
                     ARRAY['owner']::text[], TRUE)
                """
            ),
            {
                "id": str(user_id),
                "tid": str(SENTINEL_TENANT_ID),
                "email": email,
                "hash": hash_password(plaintext),
                "dn": "S. Behrens",
            },
        )
    return user_id, email, plaintext


@pytest_asyncio.fixture
async def app_engine() -> AsyncGenerator[AsyncEngine, None]:
    """App-role engine for seeding cases through the repository under RLS."""
    _require_db()
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def web_client(
    seeded_user: tuple[UUID, str, str],
) -> AsyncGenerator[AsyncClient, None]:
    settings = WebSettings(
        web_host="127.0.0.1",
        web_port=8000,
        session_cookie_name="portfoliflow_session",
        csrf_cookie_name="portfoliflow_csrf_pre_session",
        database_url=DATABASE_URL,
        database_url_superuser=DATABASE_URL_SUPERUSER,
        session_cookie_secure=False,
    )
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://testserver") as client,
        app.router.lifespan_context(app),
    ):
        yield client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _login(client: AsyncClient, email: str, password: str) -> None:
    get_response = await client.get("/login")
    csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
    assert csrf is not None
    await client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf},
        follow_redirects=False,
    )


async def _session_csrf(client: AsyncClient, engine: AsyncEngine) -> str:
    """Read the logged-in session's CSRF token straight from the DB."""
    cookie = client.cookies.get("portfoliflow_session")
    assert cookie is not None, "not logged in"
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT csrf_token FROM sessions WHERE session_token = :t"),
                {"t": cookie},
            )
        ).first()
    assert row is not None
    return str(row.csrf_token)


async def _seed_extra_user(superuser_engine: AsyncEngine, *, email: str, display_name: str) -> UUID:
    """Insert a second user (PM2) in the primary tenant; return its id.

    Seeded through the superuser engine (RLS-bypassing), the same path
    ``seeded_user`` uses — the app role cannot INSERT a user without an
    ``app.tenant_id`` session context.
    """
    user_id = uuid4()
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO users
                    (id, tenant_id, email, password_hash, display_name,
                     roles, is_active)
                VALUES
                    (:id, :tid, :email, :hash, :dn,
                     ARRAY['member']::text[], TRUE)
                """
            ),
            {
                "id": str(user_id),
                "tid": str(SENTINEL_TENANT_ID),
                "email": email,
                "hash": hash_password("irrelevant-passphrase-here"),
                "dn": display_name,
            },
        )
    return user_id


async def _seed_case(
    app_engine: AsyncEngine,
    *,
    opened_by: UUID,
    title: str,
    opened_at: datetime,
    description: str | None = None,
    finding_id: UUID | None = None,
    opened_payload: dict | None = None,
    close_note: str | None = None,
    closed_at: datetime | None = None,
) -> CaseDTO:
    """Seed one case (optionally closed) through the repository under RLS."""
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=opened_by) as session:
        repo = CaseRepository(session)
        case = await repo.create(
            title=title,
            opened_by=opened_by,
            description=description,
            finding_id=finding_id,
            opened_payload=opened_payload,
            opened_actor="pm",
            now=opened_at,
        )
        if close_note is not None:
            await repo.close(
                case.id,
                closed_by=opened_by,
                closing_note=close_note,
                now=closed_at or (opened_at + timedelta(hours=1)),
            )
    return case


async def _append_entry(
    app_engine: AsyncEngine,
    *,
    session_user: UUID,
    case_id: UUID,
    kind: str,
    actor: str,
    actor_user_id: UUID | None,
    payload: dict,
    now: datetime,
) -> None:
    """Append one timeline entry to an open case through the repository."""
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=session_user) as session:
        await CaseRepository(session).append_entry(
            case_id,
            kind=kind,
            actor=actor,
            actor_user_id=actor_user_id,
            payload=payload,
            now=now,
        )


async def _seed_attachment(
    app_engine: AsyncEngine,
    *,
    case_id: UUID,
    uploaded_by: UUID,
    filename: str = "seed.pdf",
    content: bytes = b"%PDF-1.4 seed",
    mime_type: str = "application/pdf",
) -> UUID:
    """Seed one attachment against a case through the repository (under RLS)."""
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=uploaded_by) as session:
        attachment = await CaseAttachmentRepository(session).create(
            case_id,
            filename=filename,
            mime_type=mime_type,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            content=content,
            uploaded_by=uploaded_by,
            now=_T0,
        )
    return attachment.id


async def _post_pin(
    client: AsyncClient,
    case_id: UUID,
    csrf: str,
    *,
    comment: str,
    filename: str = "terms.pdf",
    content: bytes = _SMALL_PDF,
    content_type: str = "application/pdf",
):
    """POST a multipart pin-document request (csrf field + one file part)."""
    files = {"file": (filename, content, content_type)}
    return await client.post(
        f"/api/cases/{case_id}/pin-document",
        data={"csrf_token": csrf, "comment": comment},
        files=files,
        follow_redirects=False,
    )


async def _seed_finding(app_engine: AsyncEngine, *, opened_by: UUID) -> UUID:
    """Seed an open Irene finding a from-finding case can reference."""
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=opened_by) as session:
        finding = await IreneFindingRepository(session).append(
            subject_key="saa:private_equity",
            payload={"trigger": "PE approaching its SAA ceiling"},
            urgency=4,
            band="noteworthy",
        )
    return finding.id


async def _seed_finding_full(
    app_engine: AsyncEngine,
    *,
    opened_by: UUID,
    subject_key: str,
    band: str,
    payload: dict,
) -> UUID:
    """Seed a finding with a full payload for the origin-embed tests."""
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=opened_by) as session:
        finding = await IreneFindingRepository(session).append(
            subject_key=subject_key,
            payload=payload,
            urgency=8,
            band=band,
        )
    return finding.id


async def _seed_foreign_tenant_case_and_attachment(
    superuser_engine: AsyncEngine, app_engine: AsyncEngine
) -> tuple[UUID, UUID]:
    """Seed a foreign-tenant case + attachment; return ``(case_id, att_id)``.

    The primary-tenant session must not be able to download the attachment —
    RLS hides the row, so the download route sees absence and 404s. Mirrors
    :func:`_seed_foreign_tenant_case`.
    """
    tenant_b = uuid4()
    user_b = uuid4()
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, :sub)"),
            {"id": str(tenant_b), "name": "Foreign Tenant", "sub": "foreign-a"},
        )
        await conn.execute(
            text(
                """
                INSERT INTO users
                    (id, tenant_id, email, password_hash, display_name,
                     roles, is_active)
                VALUES
                    (:id, :tid, :email, :hash, :dn,
                     ARRAY['owner']::text[], TRUE)
                """
            ),
            {
                "id": str(user_b),
                "tid": str(tenant_b),
                "email": "owner@foreign-a.example",
                "hash": hash_password("irrelevant-passphrase-here"),
                "dn": "F. Oreign",
            },
        )
    async with tenant_context(app_engine, tenant_b, user_id=user_b) as session:
        case = await CaseRepository(session).create(
            title="Foreign tenant case",
            opened_by=user_b,
            opened_actor="pm",
            now=_T0,
        )
        att = await CaseAttachmentRepository(session).create(
            case.id,
            filename="foreign.pdf",
            mime_type="application/pdf",
            size_bytes=len(b"foreign"),
            sha256=hashlib.sha256(b"foreign").hexdigest(),
            content=b"foreign",
            uploaded_by=user_b,
            now=_T0,
        )
    return case.id, att.id


async def _seed_foreign_tenant_case(superuser_engine: AsyncEngine, app_engine: AsyncEngine) -> UUID:
    """Seed a case under a *second* tenant; return its id (foreign to primary).

    The primary-tenant session must not be able to load it — RLS hides the
    row, so the detail route sees absence and renders the 404 idiom.
    """
    tenant_b = uuid4()
    user_b = uuid4()
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, :sub)"),
            {"id": str(tenant_b), "name": "Foreign Tenant", "sub": "foreign-t"},
        )
        await conn.execute(
            text(
                """
                INSERT INTO users
                    (id, tenant_id, email, password_hash, display_name,
                     roles, is_active)
                VALUES
                    (:id, :tid, :email, :hash, :dn,
                     ARRAY['owner']::text[], TRUE)
                """
            ),
            {
                "id": str(user_b),
                "tid": str(tenant_b),
                "email": "owner@foreign.example",
                "hash": hash_password("irrelevant-passphrase-here"),
                "dn": "F. Oreign",
            },
        )
    async with tenant_context(app_engine, tenant_b, user_id=user_b) as session:
        case = await CaseRepository(session).create(
            title="Foreign tenant case",
            opened_by=user_b,
            opened_actor="pm",
            now=_T0,
        )
    return case.id


# ---------------------------------------------------------------------------
# Area / nav
# ---------------------------------------------------------------------------


async def test_cases_page_renders_three_sections(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """``GET /cases`` renders the area with its three Sections."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get("/cases", follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    assert 'data-area="cases"' in body
    assert "<html" in body.lower()
    for slug in ("open-cases", "recently-closed", "archive"):
        assert f'id="{slug}"' in body, f'missing section anchor id="{slug}"'
        assert f'data-section="{slug}"' in body, f"missing section-indicator dot for {slug}"


async def test_cases_htmx_branch_returns_partial(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """An HTMX swap returns the body partial plus the OOB sidebar."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get(
        "/cases", headers={"HX-Request": "true"}, follow_redirects=False
    )
    assert response.status_code == 200
    body = response.text
    assert "<html" not in body.lower()
    assert 'hx-swap-oob="outerHTML"' in body
    assert 'data-area="cases"' in body


# ---------------------------------------------------------------------------
# Open cases — order, owner name, finding-origin badge
# ---------------------------------------------------------------------------


async def test_open_list_rows_newest_first_owner_and_finding_badge(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    pm1, email, password = seeded_user
    finding_id = await _seed_finding(app_engine, opened_by=pm1)
    # Older manual case, then a newer from-finding case.
    await _seed_case(
        app_engine,
        opened_by=pm1,
        title="Board-meeting preparation",
        opened_at=_T0,
    )
    await _seed_case(
        app_engine,
        opened_by=pm1,
        title="Private equity approaching its SAA ceiling",
        opened_at=_T0 + timedelta(hours=1),
        finding_id=finding_id,
    )

    await _login(web_client, email, password)
    response = await web_client.get("/api/cases/open", follow_redirects=False)
    assert response.status_code == 200
    body = response.text

    # Both cases, newest (CASE-0002, from-finding) before oldest (CASE-0001).
    assert "CASE-0001" in body and "CASE-0002" in body
    assert body.index("CASE-0002") < body.index("CASE-0001")
    # Owner display name resolved (the Journal idiom), not a raw UUID.
    assert "S. Behrens" in body
    assert str(pm1) not in body
    # The finding-origin badge shows on the from-finding row; the manual row
    # reads "opened manually".
    assert "From finding" in body
    assert "opened manually" in body


# ---------------------------------------------------------------------------
# "Mine" filter — narrows to the current user and back
# ---------------------------------------------------------------------------


async def test_mine_filter_narrows_and_restores(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
    fresh_superuser_engine: AsyncEngine,
) -> None:
    pm1, email, password = seeded_user
    pm2 = await _seed_extra_user(
        fresh_superuser_engine, email="pm2@example.com", display_name="M. Otten"
    )
    await _seed_case(
        app_engine,
        opened_by=pm1,
        title="Mine — PM1 case",
        opened_at=_T0,
    )
    await _seed_case(
        app_engine,
        opened_by=pm2,
        title="Theirs — PM2 case",
        opened_at=_T0 + timedelta(minutes=1),
    )

    await _login(web_client, email, password)

    # All open — both cases, both owners visible (Mine is a filter, never a
    # data boundary: PM1 sees PM2's case).
    all_open = await web_client.get("/api/cases/open", follow_redirects=False)
    assert all_open.status_code == 200
    assert "Mine — PM1 case" in all_open.text
    assert "Theirs — PM2 case" in all_open.text
    assert "M. Otten" in all_open.text

    # Mine — only PM1's case; PM2's is filtered out. The Mine chip is active.
    mine = await web_client.get("/api/cases/open?mine=1", follow_redirects=False)
    assert mine.status_code == 200
    assert "Mine — PM1 case" in mine.text
    assert "Theirs — PM2 case" not in mine.text
    assert "pf-chip--active" in mine.text

    # Back to all open — PM2's case returns.
    back = await web_client.get("/api/cases/open", follow_redirects=False)
    assert "Theirs — PM2 case" in back.text


# ---------------------------------------------------------------------------
# Recently closed — at most five, newest first, excerpt
# ---------------------------------------------------------------------------


async def test_recently_closed_caps_at_five_with_excerpt(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    pm1, email, password = seeded_user
    for i in range(6):
        await _seed_case(
            app_engine,
            opened_by=pm1,
            title=f"Closed case {i}",
            opened_at=_T0 + timedelta(hours=i),
            close_note=f"Resolution note number {i} for the archive.",
            closed_at=_T0 + timedelta(hours=i, minutes=30),
        )

    await _login(web_client, email, password)
    response = await web_client.get("/api/cases/recently-closed", follow_redirects=False)
    assert response.status_code == 200
    body = response.text

    # Six closed, only five shown; the oldest (case 0) is dropped. Count by
    # the once-per-row id span (``pf-case-row`` is a substring of
    # ``pf-case-row--closed`` and would double-count).
    assert body.count("pf-case-row__id") == 5
    assert "Closed case 0" not in body
    assert "Closed case 5" in body
    # The closing-note excerpt is rendered.
    assert "Resolution note number 5" in body


# ---------------------------------------------------------------------------
# Archive — title / note search, idle whitespace, collapsed panel
# ---------------------------------------------------------------------------


async def test_archive_search_title_note_and_idle(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    pm1, email, password = seeded_user
    await _seed_case(
        app_engine,
        opened_by=pm1,
        title="Alpha Rebalancing Review",
        opened_at=_T0,
        close_note="closed uneventfully",
    )
    await _seed_case(
        app_engine,
        opened_by=pm1,
        title="Ordinary title",
        opened_at=_T0 + timedelta(hours=1),
        close_note="watched the zzbetazz closely",
    )

    await _login(web_client, email, password)

    # Bare load (no q) — the collapsed search panel.
    panel = await web_client.get("/api/cases/archive", follow_redirects=False)
    assert panel.status_code == 200
    assert "Search all closed cases" in panel.text

    # Title match.
    by_title = await web_client.get("/api/cases/archive?q=Alpha", follow_redirects=False)
    assert "Alpha Rebalancing Review" in by_title.text
    assert "Ordinary title" not in by_title.text

    # Closing-note match.
    by_note = await web_client.get("/api/cases/archive?q=zzbetazz", follow_redirects=False)
    assert "Ordinary title" in by_note.text
    assert "Alpha Rebalancing Review" not in by_note.text

    # Whitespace query — the idle state, never "no results".
    idle = await web_client.get("/api/cases/archive?q=%20%20", follow_redirects=False)
    assert idle.status_code == 200
    assert "Type a title or closing-note fragment" in idle.text
    assert "No closed cases match" not in idle.text


# ---------------------------------------------------------------------------
# Manual creation — success, entry shape, and validation
# ---------------------------------------------------------------------------


async def test_new_case_form_renders(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get("/api/cases/new", follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    assert 'name="title"' in body
    assert 'name="csrf_token"' in body
    assert 'hx-post="/api/cases/new"' in body


async def test_create_case_writes_one_opened_entry_and_appears(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
    fresh_superuser_engine: AsyncEngine,
) -> None:
    pm1, email, password = seeded_user
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    response = await web_client.post(
        "/api/cases/new",
        data={
            "csrf_token": csrf,
            "title": "Prepare advisory-board position",
            "description": "Fund II continuation vehicle.",
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    # The re-rendered open list carries the new row (CASE-0001) out of band.
    assert 'id="cases-open-list"' in body
    assert "hx-swap-oob" in body
    assert "CASE-0001" in body
    assert "Prepare advisory-board position" in body

    # Exactly one case, with exactly one 'opened' entry attributed to the PM.
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=pm1) as session:
        repo = CaseRepository(session)
        cases = await repo.list_open()
        assert len(cases) == 1
        created = cases[0]
        assert created.title == "Prepare advisory-board position"
        assert created.description == "Fund II continuation vehicle."
        assert created.opened_by == pm1
        entries = await repo.list_entries(created.id)

    assert [e.kind for e in entries] == ["opened"]
    assert entries[0].actor == "pm"
    assert entries[0].actor_user_id == pm1


async def test_create_case_blank_title_rerenders_form_and_writes_nothing(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
    fresh_superuser_engine: AsyncEngine,
) -> None:
    pm1, email, password = seeded_user
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    response = await web_client.post(
        "/api/cases/new",
        data={"csrf_token": csrf, "title": "   ", "description": "x"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    # The form is re-rendered carrying the inline error.
    assert 'hx-post="/api/cases/new"' in body
    assert "A title is required" in body

    # Nothing was written.
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=pm1) as session:
        cases = await CaseRepository(session).list_open()
    assert cases == []


# ---------------------------------------------------------------------------
# Detail view (C3a) — head, description, origin embed, timeline, rail
# ---------------------------------------------------------------------------


async def test_detail_manual_open_case_head_description_origin_and_actions(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """A manual open case: head, description block, no origin embed, four actions.

    C3b arms the Actions block on an open case (this flips the C3a assertion
    that there were none). All four actions and the composer slot are present.
    """
    pm1, email, password = seeded_user
    case = await _seed_case(
        app_engine,
        opened_by=pm1,
        title="Prepare advisory-board position",
        description="Fund II continuation vehicle — reinvestment question.",
        opened_at=_T0,
    )

    await _login(web_client, email, password)
    response = await web_client.get(f"/cases/{case.id}", follow_redirects=False)
    assert response.status_code == 200
    body = response.text

    # Head: badge, title, open state, Cases nav active.
    assert "CASE-0001" in body
    assert "Prepare advisory-board position" in body
    assert "state--open" in body
    assert 'data-area="cases"' in body
    # The manual description stands in for the origin embed (no finding).
    assert "Fund II continuation vehicle" in body
    assert "Originating finding" not in body
    assert "At case opening" not in body
    # C3b: the Actions block and composer slot are present on an open case.
    assert 'id="case-composer-slot"' in body
    assert "pf-case-actions" in body
    assert "Add note" in body
    assert "Pin document" in body
    assert "Record decision" in body
    assert "Close case" in body


async def test_detail_from_finding_origin_embed_with_and_without_materiality(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """The origin embed reads the immutable finding; materiality is opt-in."""
    pm1, email, password = seeded_user
    finding_id = await _seed_finding_full(
        app_engine,
        opened_by=pm1,
        subject_key="anlv:17",
        band="critical",
        payload={
            "trigger": "AnlV §17 high-yield quota breached",
            "finding": "The high-yield quota crossed its 5.00% ceiling.",
            "basis": "coverage 5.14% against a 5.00% ceiling",
        },
    )
    # Case A carries frozen materiality-at-opening lines on the opened entry.
    with_mat = await _seed_case(
        app_engine,
        opened_by=pm1,
        title="AnlV quota — with materiality",
        finding_id=finding_id,
        opened_payload={
            "materiality_at_opening": {
                "lines": [
                    "coverage 5.14% against a 5.00% ceiling",
                    "headroom -118,400 EUR",
                ]
            }
        },
        opened_at=_T0,
    )
    # Case B references a finding but carries no materiality payload.
    without_mat = await _seed_case(
        app_engine,
        opened_by=pm1,
        title="AnlV quota — no materiality",
        finding_id=finding_id,
        opened_at=_T0 + timedelta(hours=1),
    )

    await _login(web_client, email, password)

    a_body = (await web_client.get(f"/cases/{with_mat.id}", follow_redirects=False)).text
    # Origin embed: band chip, subject_key, finding text, basis.
    assert "Originating finding" in a_body
    assert "Critical" in a_body
    assert "anlv:17" in a_body
    assert "crossed its 5.00% ceiling" in a_body
    assert "coverage 5.14% against a 5.00% ceiling" in a_body
    # Materiality-at-opening lines render verbatim under "At case opening".
    assert "At case opening" in a_body
    assert "headroom -118,400 EUR" in a_body

    b_body = (await web_client.get(f"/cases/{without_mat.id}", follow_redirects=False)).text
    assert "Originating finding" in b_body
    assert "anlv:17" in b_body
    # Without a materiality payload the block is absent — no placeholder.
    assert "At case opening" not in b_body


async def test_detail_timeline_entries_render_ascending_with_actor_chips(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """note / decision_record render their contracts; unknown kind falls back."""
    pm1, email, password = seeded_user
    case = await _seed_case(
        app_engine,
        opened_by=pm1,
        title="Timeline coverage case",
        opened_at=_T0,
    )
    await _append_entry(
        app_engine,
        session_user=pm1,
        case_id=case.id,
        kind="note",
        actor="pm",
        actor_user_id=pm1,
        payload={"text": "Called the depot bank for the final terms."},
        now=_T0 + timedelta(minutes=30),
    )
    await _append_entry(
        app_engine,
        session_user=pm1,
        case_id=case.id,
        kind="decision_record",
        actor="pm",
        actor_user_id=pm1,
        payload={
            "decision": "Reclassify DE000NB47531 from anlv:17 to anlv:15.",
            "rationale": "A classification error, not an exposure decision.",
        },
        now=_T0 + timedelta(hours=1),
    )
    # A ``pin`` entry: valid in the repository but not renderable until C3b, so
    # the template must show the calm generic fallback and never dump payload.
    await _append_entry(
        app_engine,
        session_user=pm1,
        case_id=case.id,
        kind="pin",
        actor="system",
        actor_user_id=None,
        payload={"hidden": "PIN-PAYLOAD-SHOULD-NOT-RENDER"},
        now=_T0 + timedelta(hours=2),
    )

    await _login(web_client, email, password)
    body = (await web_client.get(f"/cases/{case.id}", follow_redirects=False)).text

    # note + decision_record payload contracts.
    assert "Called the depot bank for the final terms." in body
    assert "Reclassify DE000NB47531 from anlv:17 to anlv:15." in body
    assert "A classification error, not an exposure decision." in body
    # Actor chips resolve: pm → display name, system → its label.
    assert "S. Behrens" in body
    assert "System" in body
    # Unknown kind (pin) → calm fallback: humanised label, payload untouched.
    assert 'data-kind="pin"' in body
    assert "PIN-PAYLOAD-SHOULD-NOT-RENDER" not in body
    # Ascending: opened → note → decision_record → pin.
    assert (
        body.index("Case opened")
        < body.index("Called the depot bank")
        < body.index("Reclassify DE000NB47531")
        < body.index('data-kind="pin"')
    )


async def test_a_pinned_scenario_snapshot_renders_the_anatomy(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """The scenario-snapshot pin renders its frozen anatomy (ADR-0107 C5, test 6).

    The Planning Desk writes exactly what this view reads: the curation comment,
    the parameter chips as a line, the KPI pairs with their frozen delta tones,
    the two feet, a headroom *count* (the mock shows no table), and the stored
    query as monospace text — never a link. A ``consultation`` pin (C6) still
    falls to the calm generic fallback, its payload untouched.
    """
    pm1, email, password = seeded_user
    case = await _seed_case(app_engine, opened_by=pm1, title="Snapshot case", opened_at=_T0)
    await _append_entry(
        app_engine,
        session_user=pm1,
        case_id=case.id,
        kind="pin",
        actor="pm",
        actor_user_id=pm1,
        payload={
            "artifact": "scenario_snapshot",
            "comment": "the alternative path, quantified before deciding",
            "snapshot": {
                "chips": [{"label": "FX shock: USD +10 %", "css_class": "pd-chip--shock"}],
                "kpis": [
                    {
                        "label": "AUM (Σ NAV incl. cash)",
                        "base": "212.4m EUR",
                        "scen": "198.7m EUR",
                        "delta": "−13.7m EUR",
                        "tone": "neg",
                    }
                ],
                "headroom": [{"family": "AnlV", "rows": [{"label": "a"}, {"label": "b"}]}],
                "baseline_foot": {"nav": "212.4m EUR", "ret": "+14.8%"},
                "scenario_foot": {
                    "nav": "198.7m EUR",
                    "nav_delta": "−13.7m EUR",
                    "nav_tone": "neg",
                    "ret": "+7.9%",
                    "ret_delta": "−6.9pp",
                    "ret_tone": "neg",
                },
                "query": "periodisation=quarterly&horizon=8&t0_kind=fx_shock",
            },
        },
        now=_T0 + timedelta(hours=1),
    )
    # A consultation pin (C6) — still the calm fallback; payload never dumped.
    await _append_entry(
        app_engine,
        session_user=pm1,
        case_id=case.id,
        kind="pin",
        actor="shirley",
        actor_user_id=None,
        payload={"artifact": "consultation", "secret": "SHOULD-NOT-RENDER"},
        now=_T0 + timedelta(hours=2),
    )

    await _login(web_client, email, password)
    body = (await web_client.get(f"/cases/{case.id}", follow_redirects=False)).text
    collapsed = " ".join(body.split())

    assert "Scenario snapshot pinned" in body
    assert "the alternative path, quantified before deciding" in body
    # The parameter chip line and the KPI pair.
    assert "FX shock: USD +10 %" in body
    assert "212.4m EUR" in body
    assert "198.7m EUR" in body
    assert "−13.7m EUR" in body
    # A headroom count, not a table (the mock never promised a table).
    assert "2 headroom rows frozen" in collapsed
    assert "pd-deltatable" not in body
    # The stored query is monospace text, never a link (decision 2).
    assert (
        '<code class="mono">periodisation=quarterly&amp;horizon=8'
        "&amp;t0_kind=fx_shock</code>" in body
    )
    # The consultation pin still falls to the calm generic fallback.
    assert "SHOULD-NOT-RENDER" not in body


async def test_a_pinned_chart_snapshot_renders_the_frozen_figure(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """The chart-snapshot pin renders comment + figure from the payload (ADR-0114).

    The fourth pin artifact class: the curation comment plus the caption and the
    frozen Plotly spec, handed to the client in an inert ``application/json``
    block for the shared render helper. Nothing is recomputed and nothing is
    re-queried — the ``scenario_snapshot`` discipline applied to a figure. An
    unknown artifact class still falls to the calm generic fallback, and a
    closed case renders identically (reading is never gated).
    """
    pm1, email, password = seeded_user
    spec = {
        "data": [{"type": "scatter", "x": ["2026-01-31"], "y": [1.0], "name": "NAV"}],
        "layout": {"title": {"text": "Nordbank 2031 — NAV"}},
        "config": {"responsive": True},
    }
    await _login(web_client, email, password)
    for closed in (False, True):
        case = await _seed_case(
            app_engine,
            opened_by=pm1,
            title="Chart snapshot case" + (" (closed)" if closed else ""),
            opened_at=_T0,
        )
        await _append_entry(
            app_engine,
            session_user=pm1,
            case_id=case.id,
            kind="pin",
            actor="pm",
            actor_user_id=pm1,
            payload={
                "artifact": "chart_snapshot",
                "comment": "the trajectory the committee asked about",
                "caption": "Nordbank 2031 — NAV trajectory",
                "spec": spec,
            },
            now=_T0 + timedelta(hours=1),
        )
        # An unrendered artifact class stays calm — its payload never leaks.
        await _append_entry(
            app_engine,
            session_user=pm1,
            case_id=case.id,
            kind="pin",
            actor="pm",
            actor_user_id=pm1,
            payload={"artifact": "mystery", "secret": "SHOULD-NOT-RENDER"},
            now=_T0 + timedelta(hours=2),
        )
        if closed:
            async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=pm1) as session:
                await CaseRepository(session).close(
                    case.id,
                    closed_by=pm1,
                    closing_note="filed",
                    now=_T0 + timedelta(hours=3),
                )

        body = (await web_client.get(f"/cases/{case.id}", follow_redirects=False)).text

        assert "Chart snapshot pinned" in body
        assert "the trajectory the committee asked about" in body
        assert "Nordbank 2031 — NAV trajectory" in body  # the caption
        assert "pf-artifact--chart" in body
        # The figure container plus the frozen spec, verbatim from the payload.
        assert "data-pf-chart-plot" in body
        assert '<script type="application/json" data-pf-chart-spec>' in body
        assert '"scatter"' in body
        assert '"responsive": true' in body
        assert "SHOULD-NOT-RENDER" not in body


async def test_the_open_detail_offers_capture_scenario_the_closed_does_not(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """ "Capture scenario" is an open-case action only (ADR-0107 C5, test 7).

    The open-case Actions block gains a plain link to the Planning Desk carrying
    this case as the capture marker; a closed case renders no Actions block, so
    the affordance is absent.
    """
    pm1, email, password = seeded_user
    open_case = await _seed_case(
        app_engine, opened_by=pm1, title="Open capture target", opened_at=_T0
    )
    closed_case = await _seed_case(
        app_engine,
        opened_by=pm1,
        title="Closed one",
        opened_at=_T0,
        close_note="done",
        closed_at=_T0 + timedelta(hours=1),
    )
    await _login(web_client, email, password)

    open_body = (await web_client.get(f"/cases/{open_case.id}", follow_redirects=False)).text
    assert "Capture scenario" in open_body
    assert f'href="/planning-desk?case={open_case.id}"' in open_body

    closed_body = (await web_client.get(f"/cases/{closed_case.id}", follow_redirects=False)).text
    assert "Capture scenario" not in closed_body


async def test_detail_closed_case_shows_closing_note_and_no_composers(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """A closed case reads fully; the page carries no composer/action markup."""
    pm1, email, password = seeded_user
    case = await _seed_case(
        app_engine,
        opened_by=pm1,
        title="Resolved breach",
        opened_at=_T0,
        close_note="Breach resolved by reclassification, no transaction required.",
        closed_at=_T0 + timedelta(hours=2),
    )

    await _login(web_client, email, password)
    body = (await web_client.get(f"/cases/{case.id}", follow_redirects=False)).text

    # Closed state visible in the head and the rail; the closed entry carries
    # the closing note (mandatory close artifact, written by close()).
    assert "state--closed" in body
    assert "Case closed" in body
    assert "Breach resolved by reclassification, no transaction required." in body
    # Still nothing interactive — no composer slot, no actions, no composers.
    assert 'id="case-composer-slot"' not in body
    assert "pf-case-actions" not in body
    assert "Add note" not in body
    assert "Close case" not in body


async def test_detail_unknown_and_foreign_ids_are_404(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """Unknown id and a foreign-tenant id both resolve to the 404 idiom."""
    _pm1, email, password = seeded_user
    foreign_id = await _seed_foreign_tenant_case(fresh_superuser_engine, app_engine)

    await _login(web_client, email, password)

    unknown = await web_client.get(f"/cases/{uuid4()}", follow_redirects=False)
    assert unknown.status_code == 404

    foreign = await web_client.get(f"/cases/{foreign_id}", follow_redirects=False)
    assert foreign.status_code == 404


async def test_list_surfaces_link_rows_to_detail(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """Open, recently-closed and archive rows all link to /cases/{id}."""
    pm1, email, password = seeded_user
    open_case = await _seed_case(
        app_engine,
        opened_by=pm1,
        title="Open one",
        opened_at=_T0,
    )
    closed_case = await _seed_case(
        app_engine,
        opened_by=pm1,
        title="Closed one",
        opened_at=_T0 + timedelta(hours=1),
        close_note="Closed for the archive search.",
        closed_at=_T0 + timedelta(hours=2),
    )

    await _login(web_client, email, password)

    open_body = (await web_client.get("/api/cases/open", follow_redirects=False)).text
    assert f'href="/cases/{open_case.id}"' in open_body

    closed_body = (await web_client.get("/api/cases/recently-closed", follow_redirects=False)).text
    assert f'href="/cases/{closed_case.id}"' in closed_body

    archive_body = (
        await web_client.get("/api/cases/archive?q=Closed", follow_redirects=False)
    ).text
    assert f'href="/cases/{closed_case.id}"' in archive_body


# ---------------------------------------------------------------------------
# C3b — composers, attachment pipeline, pin anatomy
# ---------------------------------------------------------------------------


async def test_note_appends_one_entry_and_blank_writes_nothing(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """POST /note appends one note by the session user; blank text writes nothing."""
    pm1, email, password = seeded_user
    case = await _seed_case(app_engine, opened_by=pm1, title="Note case", opened_at=_T0)
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    # Blank text → inline error, nothing written.
    blank = await web_client.post(
        f"/api/cases/{case.id}/note",
        data={"csrf_token": csrf, "text": "   "},
        follow_redirects=False,
    )
    assert blank.status_code == 200
    assert "A note cannot be empty" in blank.text
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=pm1) as session:
        entries = await CaseRepository(session).list_entries(case.id)
    assert [e.kind for e in entries] == ["opened"]

    # Success → one note entry; the timeline refreshes out of band.
    ok = await web_client.post(
        f"/api/cases/{case.id}/note",
        data={"csrf_token": csrf, "text": "Called the depot bank."},
        follow_redirects=False,
    )
    assert ok.status_code == 200
    assert 'id="case-timeline"' in ok.text
    assert "hx-swap-oob" in ok.text
    assert "Called the depot bank." in ok.text
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=pm1) as session:
        entries = await CaseRepository(session).list_entries(case.id)
    notes = [e for e in entries if e.kind == "note"]
    assert len(notes) == 1
    assert notes[0].actor == "pm"
    assert notes[0].actor_user_id == pm1
    assert notes[0].payload == {"text": "Called the depot bank."}


async def test_decision_requires_both_fields_then_renders_anatomy(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """POST /decision requires both fields; success renders the C3a anatomy."""
    pm1, email, password = seeded_user
    case = await _seed_case(app_engine, opened_by=pm1, title="Decision case", opened_at=_T0)
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    # Missing rationale, then missing decision — each rejected, nothing written.
    for payload in (
        {"decision": "Reclassify", "rationale": "  "},
        {"decision": "  ", "rationale": "because documented"},
    ):
        r = await web_client.post(
            f"/api/cases/{case.id}/decision",
            data={"csrf_token": csrf, **payload},
            follow_redirects=False,
        )
        assert r.status_code == 200
        assert "Both a decision and its rationale are required" in r.text
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=pm1) as session:
        entries = await CaseRepository(session).list_entries(case.id)
    assert [e.kind for e in entries] == ["opened"]

    # Success → one decision_record entry; the C3a anatomy renders.
    ok = await web_client.post(
        f"/api/cases/{case.id}/decision",
        data={
            "csrf_token": csrf,
            "decision": "Reclassify DE000NB47531 to anlv:15.",
            "rationale": "A classification error, not an exposure decision.",
        },
        follow_redirects=False,
    )
    assert ok.status_code == 200
    body = ok.text
    assert "Decision of record" in body
    assert "Reclassify DE000NB47531 to anlv:15." in body
    assert "A classification error, not an exposure decision." in body
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=pm1) as session:
        entries = await CaseRepository(session).list_entries(case.id)
    recs = [e for e in entries if e.kind == "decision_record"]
    assert len(recs) == 1
    assert recs[0].payload == {
        "decision": "Reclassify DE000NB47531 to anlv:15.",
        "rationale": "A classification error, not an exposure decision.",
    }


async def test_pin_happy_path_stores_downloads_and_renders(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """A small PDF uploads: attachment stored, pin payload correct, download exact."""
    pm1, email, password = seeded_user
    case = await _seed_case(app_engine, opened_by=pm1, title="Pin happy", opened_at=_T0)
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    comment = "Ranking terms from the depot bank — basis for the review."
    resp = await _post_pin(web_client, case.id, csrf, comment=comment, filename="terms.pdf")
    assert resp.status_code == 200
    body = resp.text
    # Timeline + attachments count refresh out of band; filename + comment show.
    assert 'id="case-timeline"' in body
    assert "hx-swap-oob" in body
    assert "terms.pdf" in body
    assert comment in body
    assert "1 of" in body  # the rail attachments count

    # The attachment row exists with correct sha256 + size; the pin entry
    # carries the binding-decision-4 payload.
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=pm1) as session:
        atts = await CaseAttachmentRepository(session).list_for_case(case.id)
        entries = await CaseRepository(session).list_entries(case.id)
    assert len(atts) == 1
    att = atts[0]
    assert att.sha256 == hashlib.sha256(_SMALL_PDF).hexdigest()
    assert att.size_bytes == len(_SMALL_PDF)
    assert att.filename == "terms.pdf"
    assert att.mime_type == "application/pdf"
    pins = [e for e in entries if e.kind == "pin"]
    assert len(pins) == 1
    assert pins[0].payload == {
        "artifact": "document",
        "comment": comment,
        "attachment_id": str(att.id),
        "filename": "terms.pdf",
        "mime_type": "application/pdf",
        "size_bytes": len(_SMALL_PDF),
    }

    # The download URL returns the exact bytes with the declared mime and an
    # attachment Content-Disposition header.
    dl = await web_client.get(f"/api/cases/{case.id}/attachments/{att.id}", follow_redirects=False)
    assert dl.status_code == 200
    assert dl.content == _SMALL_PDF
    assert dl.headers["content-type"].startswith("application/pdf")
    assert 'attachment; filename="terms.pdf"' in dl.headers["content-disposition"]


async def test_pin_gates_reject_with_inline_error_and_zero_writes(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
    fresh_superuser_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every pin gate rejects with its inline error and writes nothing."""
    pm1, email, password = seeded_user
    case = await _seed_case(app_engine, opened_by=pm1, title="Pin gates", opened_at=_T0)
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    async def _count() -> int:
        async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=pm1) as session:
            return await CaseAttachmentRepository(session).count_for_case(case.id)

    # Empty comment → rejected.
    r = await _post_pin(web_client, case.id, csrf, comment="   ")
    assert r.status_code == 200
    assert "curation comment is required" in r.text
    assert await _count() == 0

    # Disallowed extension → rejected.
    r = await _post_pin(
        web_client,
        case.id,
        csrf,
        comment="ok",
        filename="notes.txt",
        content=b"hello there",
        content_type="text/plain",
    )
    assert "not allowed" in r.text
    assert await _count() == 0

    # Disallowed declared content type (valid extension) → rejected.
    r = await _post_pin(
        web_client,
        case.id,
        csrf,
        comment="ok",
        filename="terms.pdf",
        content_type="application/x-evil",
    )
    assert "declared content type" in r.text
    assert await _count() == 0

    # Oversized → rejected (cap lowered below the payload size).
    cfg = get_config()
    monkeypatch.setattr(cfg, "case_attachment_max_bytes", len(_SMALL_PDF) - 1)
    r = await _post_pin(web_client, case.id, csrf, comment="ok")
    assert "per-file" in r.text
    assert await _count() == 0
    monkeypatch.undo()  # restore the size cap before the count-cap check

    # Cap reached → rejected (seed count == cap).
    cfg = get_config()
    monkeypatch.setattr(cfg, "case_attachment_max_count", 1)
    await _seed_attachment(app_engine, case_id=case.id, uploaded_by=pm1)
    assert await _count() == 1
    r = await _post_pin(web_client, case.id, csrf, comment="ok")
    assert "maximum of 1" in r.text
    assert await _count() == 1


async def test_pin_atomicity_rolls_back_attachment_when_append_fails(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
    fresh_superuser_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the pin entry append fails after create, the attachment does not survive."""
    pm1, email, password = seeded_user
    case = await _seed_case(app_engine, opened_by=pm1, title="Atomic pin", opened_at=_T0)
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    async def _boom(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("append failed after the attachment insert")

    monkeypatch.setattr(CaseRepository, "append_entry", _boom)

    resp = await _post_pin(web_client, case.id, csrf, comment="ranking terms")
    # The route surfaced a calm error rather than a half-write.
    assert resp.status_code == 500

    # The attachment did not survive — the transaction rolled it back.
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=pm1) as session:
        count = await CaseAttachmentRepository(session).count_for_case(case.id)
    assert count == 0


async def test_close_requires_note_renders_closed_and_is_immutable(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """Close needs a note; success closes the page; the closed case rejects writes."""
    pm1, email, password = seeded_user
    case = await _seed_case(app_engine, opened_by=pm1, title="To close", opened_at=_T0)
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    # Pin a document first so we can prove downloads still work after close.
    pin = await _post_pin(web_client, case.id, csrf, comment="evidence", filename="evidence.pdf")
    assert pin.status_code == 200
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=pm1) as session:
        att = (await CaseAttachmentRepository(session).list_for_case(case.id))[0]

    # Missing note → inline error, still open.
    blank = await web_client.post(
        f"/api/cases/{case.id}/close",
        data={"csrf_token": csrf, "closing_note": "  ", "confirm": "yes"},
        follow_redirects=False,
    )
    assert blank.status_code == 200
    assert "A closing note is required" in blank.text
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=pm1) as session:
        assert (await CaseRepository(session).get(case.id)).state == "open"

    # Success → full-page refresh via HX-Redirect; the case is closed.
    ok = await web_client.post(
        f"/api/cases/{case.id}/close",
        data={
            "csrf_token": csrf,
            "closing_note": "Resolved by reclassification; no action.",
            "confirm": "yes",
        },
        follow_redirects=False,
    )
    assert ok.status_code == 200
    assert ok.headers.get("HX-Redirect") == f"/cases/{case.id}"

    # The reloaded page renders closed: chip, closing note, no composers/actions.
    page = await web_client.get(f"/cases/{case.id}", follow_redirects=False)
    pbody = page.text
    assert "state--closed" in pbody
    assert "Resolved by reclassification; no action." in pbody
    assert 'id="case-composer-slot"' not in pbody
    assert "pf-case-actions" not in pbody

    # Closing again → calm error idiom.
    again = await web_client.post(
        f"/api/cases/{case.id}/close",
        data={"csrf_token": csrf, "closing_note": "again", "confirm": "yes"},
        follow_redirects=False,
    )
    assert again.status_code == 200
    assert "closed cases are read-only" in again.text

    # Note + pin against the closed case → calm error, zero writes.
    late_note = await web_client.post(
        f"/api/cases/{case.id}/note",
        data={"csrf_token": csrf, "text": "late note"},
        follow_redirects=False,
    )
    assert "closed cases are read-only" in late_note.text
    late_pin = await _post_pin(web_client, case.id, csrf, comment="late pin", filename="late.pdf")
    assert "closed cases are read-only" in late_pin.text

    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=pm1) as session:
        entries = await CaseRepository(session).list_entries(case.id)
        att_count = await CaseAttachmentRepository(session).count_for_case(case.id)
    # Only opened → pin → closed; the late note and late pin wrote nothing.
    assert [e.kind for e in entries] == ["opened", "pin", "closed"]
    assert att_count == 1

    # Reading is never gated — the download still works on the closed case.
    dl = await web_client.get(f"/api/cases/{case.id}/attachments/{att.id}", follow_redirects=False)
    assert dl.status_code == 200
    assert dl.content == _SMALL_PDF


async def test_download_scoping_mismatched_case_and_foreign_tenant_404(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """A download is scoped to (case, tenant): a mismatch or foreign row is 404."""
    pm1, email, password = seeded_user
    case_a = await _seed_case(app_engine, opened_by=pm1, title="Case A", opened_at=_T0)
    case_b = await _seed_case(
        app_engine,
        opened_by=pm1,
        title="Case B",
        opened_at=_T0 + timedelta(hours=1),
    )
    att_a = await _seed_attachment(app_engine, case_id=case_a.id, uploaded_by=pm1)
    foreign_case, foreign_att = await _seed_foreign_tenant_case_and_attachment(
        fresh_superuser_engine, app_engine
    )

    await _login(web_client, email, password)

    # Correct (case, attachment) → 200.
    ok = await web_client.get(f"/api/cases/{case_a.id}/attachments/{att_a}", follow_redirects=False)
    assert ok.status_code == 200

    # att_a addressed under case_b's id → 404 (belongs to a different case).
    mismatch = await web_client.get(
        f"/api/cases/{case_b.id}/attachments/{att_a}", follow_redirects=False
    )
    assert mismatch.status_code == 404

    # A foreign-tenant attachment is invisible under RLS → 404.
    foreign = await web_client.get(
        f"/api/cases/{foreign_case}/attachments/{foreign_att}",
        follow_redirects=False,
    )
    assert foreign.status_code == 404


async def test_pin_strips_path_and_download_header_is_sanitised(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """The pin strips the stored filename to a basename; the download header is safe.

    Two properties (Step 6.9): the upload sanitiser reduces a path-prefixed
    filename to its basename on the *stored* row, and the download route
    sanitises whatever is stored for the ``Content-Disposition`` header —
    quotes, control characters and path separators removed — while leaving the
    stored DB value untouched. The header part is exercised through a
    repository-seeded nasty filename (multipart transport mangles quotes before
    they reach the server, so the pin path cannot produce one).
    """
    pm1, email, password = seeded_user
    case = await _seed_case(app_engine, opened_by=pm1, title="Sanitise", opened_at=_T0)
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    # (a) Upload with a path-prefixed filename → the stored row is the basename.
    resp = await _post_pin(
        web_client,
        case.id,
        csrf,
        comment="terms",
        filename="../../etc/statement.pdf",
    )
    assert resp.status_code == 200
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=pm1) as session:
        atts = await CaseAttachmentRepository(session).list_for_case(case.id)
    assert len(atts) == 1
    assert atts[0].filename == "statement.pdf"

    # (b) A stored filename carrying a quote, CRLF and a path separator: the
    # download header strips all three, while the DB value stays untouched.
    nasty = 'pa"ss\r\n/evil.pdf'
    att_id = await _seed_attachment(app_engine, case_id=case.id, uploaded_by=pm1, filename=nasty)
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=pm1) as session:
        stored = {
            a.id: a.filename for a in await CaseAttachmentRepository(session).list_for_case(case.id)
        }
    assert stored[att_id] == nasty  # DB value unchanged

    dl = await web_client.get(f"/api/cases/{case.id}/attachments/{att_id}", follow_redirects=False)
    assert dl.status_code == 200
    cd = dl.headers["content-disposition"]
    assert cd == 'attachment; filename="pass_evil.pdf"'


# ---------------------------------------------------------------------------
# C4 — the fifth resolution's origin chip, Cases→Journal deep links, and the
# chunked upload ceiling (rider 1)
# ---------------------------------------------------------------------------


async def _resolve_finding(
    app_engine: AsyncEngine, *, finding_id: UUID, resolution: str, by: UUID
) -> None:
    """Resolve a finding through the repository (for the opened_case chip)."""
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=by) as session:
        await IreneFindingRepository(session).resolve(
            finding_id=finding_id,
            resolution=resolution,
            resolved_by=by,
            resolved_at=_T0,
        )


async def test_origin_chip_renders_opened_case_label_and_tag(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """An opened_case finding's origin embed reads "Opened case", not the
    ``|capitalize`` default "Opened_case", and carries the new tag class."""
    pm1, email, password = seeded_user
    finding_id = await _seed_finding_full(
        app_engine,
        opened_by=pm1,
        subject_key="saa:private_equity",
        band="critical",
        payload={"trigger": "PE near its SAA ceiling", "finding": "f"},
    )
    # The finding is handed over to a case, so its resolution is opened_case.
    await _resolve_finding(app_engine, finding_id=finding_id, resolution="opened_case", by=pm1)
    case = await _seed_case(
        app_engine,
        opened_by=pm1,
        title="PE near its SAA ceiling",
        finding_id=finding_id,
        opened_at=_T0,
    )

    await _login(web_client, email, password)
    body = (await web_client.get(f"/cases/{case.id}", follow_redirects=False)).text

    assert "Opened case" in body
    assert "Opened_case" not in body  # not the |capitalize default
    assert "pf-dc-res-tag--opened_case" in body


async def test_cases_side_journal_deep_links(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """Recently-closed rows and the closed detail rail carry the Journal link;
    an open case's rail does not."""
    pm1, email, password = seeded_user
    closed = await _seed_case(
        app_engine,
        opened_by=pm1,
        title="Closed for the journal link",
        opened_at=_T0,
        close_note="Documented and closed.",
        closed_at=_T0 + timedelta(hours=1),
    )
    open_case = await _seed_case(app_engine, opened_by=pm1, title="Still open", opened_at=_T0)

    await _login(web_client, email, password)

    # Recently-closed rows carry the deep link.
    recent = (await web_client.get("/api/cases/recently-closed", follow_redirects=False)).text
    assert "/watch-desk#journal" in recent
    assert "View in Journal" in recent
    # The detail link is still present (the row remains navigable).
    assert f'href="/cases/{closed.id}"' in recent

    # The closed-case detail rail carries it too.
    closed_body = (await web_client.get(f"/cases/{closed.id}", follow_redirects=False)).text
    assert "/watch-desk#journal" in closed_body

    # An open case is not in the Journal yet — its rail carries no deep link.
    open_body = (await web_client.get(f"/cases/{open_case.id}", follow_redirects=False)).text
    assert "/watch-desk#journal" not in open_body


async def test_pin_chunked_read_rejects_oversize_without_full_buffer(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
    fresh_superuser_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A multi-chunk oversize body is rejected; a within-cap one round-trips.

    Exercises the bounded 64 KiB read across several iterations (rider 1): a
    payload larger than one chunk and larger than the cap is rejected, and one
    within the cap is stored and downloads byte-identical.
    """
    pm1, email, password = seeded_user
    case = await _seed_case(app_engine, opened_by=pm1, title="Chunked pin", opened_at=_T0)
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    cfg = get_config()
    # A 128 KiB cap: exercises two-plus 64 KiB chunks in the loop.
    monkeypatch.setattr(cfg, "case_attachment_max_bytes", 128 * 1024)

    # 200 KiB payload → over the cap → rejected on the multi-chunk path.
    big = b"%PDF-1.4\n" + b"0" * (200 * 1024)
    over = await _post_pin(web_client, case.id, csrf, comment="too big", content=big)
    assert "per-file" in over.text
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=pm1) as session:
        assert await CaseAttachmentRepository(session).count_for_case(case.id) == 0

    # 100 KiB payload → within the cap → stored, and downloads identically.
    ok_bytes = b"%PDF-1.4\n" + b"1" * (100 * 1024)
    ok = await _post_pin(
        web_client,
        case.id,
        csrf,
        comment="within cap",
        content=ok_bytes,
        filename="within.pdf",
    )
    assert ok.status_code == 200
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=pm1) as session:
        atts = await CaseAttachmentRepository(session).list_for_case(case.id)
    assert len(atts) == 1
    assert atts[0].size_bytes == len(ok_bytes)

    dl = await web_client.get(
        f"/api/cases/{case.id}/attachments/{atts[0].id}",
        follow_redirects=False,
    )
    assert dl.status_code == 200
    assert dl.content == ok_bytes
