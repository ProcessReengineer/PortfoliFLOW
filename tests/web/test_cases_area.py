# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the Cases web surface — one Section (ADR-0107, ADR-0138 §12).

ASGI-level tests over a live Postgres, mirroring the fixture pattern in
``tests/web/test_watch_desk.py`` (login helper, superuser-seeded
tenant/user, HTMX header simulation).

The Cases area is one Section, ``cases`` (P-CS-3a). ``GET /cases`` and
``GET /cases/{id}`` are the shell: the Section's host lazy-loads the list
(``GET /api/cases/list``) or the case's file (``GET /api/cases/{id}``), so
content assertions go against the two partials and the pages are asserted as
the shell with the host. New markup is named through the ``data-pf-cs`` seam
(``cs()`` / ``cs_block()`` / ``cs_text()`` in ``tests/web/conftest.py``), which
survives the P-CS-3d restyle.

* CS3a-01/02 — the one Section, the host, the case page, the 404s.
* CS3a-03…08 — the list: Open with the Waiting group, the filters, *Mine* on
  the owner, search over the working file (never attachments), the row
  columns, the Closed paging.
* CS3a-09 — creation lands in the file (``HX-Redirect``).
* CS3a-10 — the file: crumb, state, outline, facts, actions (the page move).
* CS3a-11 — the one registered module.
* CS3b-01…05 — The question: the block, the text autosave answering out of
  band only, last write wins with a note, the refusals, the framing switch.
* CS3b-06…08 — Link investment / unlink, Attach document and its gates.
* CS3b-09/10 — Where we stand live and *At close*; the section endpoint.

The file tests below (origin embed, timeline, pins, composers, close,
downloads) were the stand-alone detail page's; they now read the sub-surface
partial, whose markup moved verbatim.
"""

from __future__ import annotations

import hashlib
import html
import os
import re
from collections.abc import AsyncGenerator
from datetime import date, datetime, timedelta, timezone
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
from core.repositories.case_working_file_repository import (
    CaseInvestmentRepository,
    CasePointRepository,
    CaseThreadRepository,
)
from core.repositories.investment_repository import InvestmentRepository
from core.repositories.irene_finding_repository import IreneFindingRepository
from core.tenant_constants import SENTINEL_TENANT_ID

import modules  # noqa: F401 — importing the package populates the ModuleRegistry
import web.routes.cases as cases_module
from modules.module_registry import registry
from services.cases.stand import StandContext, compose, to_snapshot
from services.money_format import format_money_compact
from services.password_hashing import hash_password
from tests.services.irene._book_fixtures import CALM_NAV, seed_book
from tests.web.conftest import cs, cs_block, cs_blocks, cs_count, cs_text, wd_attr
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


async def test_cases_page_renders_one_section_with_the_host(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CS3a-01: ``GET /cases`` renders one Section whose host loads the list.

    The three retired Sections are gone; the second navigation level links
    the one Section, and "New case" is its primary action. The page reads no
    case: the host loads the list over HTMX. ``monkeypatch`` pins the page
    size the list would page by, to prove the shell does not consult it.
    """
    monkeypatch.setattr(cases_module, "_CLOSED_PAGE_SIZE", 1)
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get("/cases", follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    assert "<html" in body.lower()
    assert 'id="cases"' in body
    assert 'data-pf-section="cases"' in body
    assert 'data-pf-section-link="cases"' in body
    assert wd_attr(cs_block(body, "host"), "hx-get") == "/api/cases/list"
    for retired in ("open-cases", "recently-closed", "archive"):
        assert f'id="{retired}"' not in body
    assert cs("new-action") in body


async def test_case_page_is_the_shell_with_the_host_on_the_case(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """CS3a-02: ``GET /cases/{id}`` is the shell with the host on the case; 404s."""
    pm1, email, password = seeded_user
    case = await _seed_case(
        app_engine, opened_by=pm1, title="A title only the file shows", opened_at=_T0
    )
    foreign_id = await _seed_foreign_tenant_case(fresh_superuser_engine, app_engine)
    await _login(web_client, email, password)

    page = await web_client.get(f"/cases/{case.id}", follow_redirects=False)
    assert page.status_code == 200
    assert wd_attr(cs_block(page.text, "host"), "hx-get") == f"/api/cases/{case.id}"
    assert "A title only the file shows" not in page.text

    for missing in (uuid4(), foreign_id):
        for url in (f"/cases/{missing}", f"/api/cases/{missing}"):
            response = await web_client.get(url, follow_redirects=False)
            assert response.status_code == 404, url


# ---------------------------------------------------------------------------
# The list (P-CS-3a) — Open · Waiting · Closed · All, Mine and search
# ---------------------------------------------------------------------------


def _row_badges(body: str) -> list[str]:
    """The ``CASE-nnnn`` badges of the list rows, in document order."""
    return [wd_attr(row, "data-pf-cs-case") or "" for row in cs_blocks(body, "row")]


async def test_open_view_orders_by_updated_and_groups_the_waiting(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """CS3a-03: open rows by ``updated_at``, then the Waiting group; counts; head."""
    pm1, email, password = seeded_user
    first = await _seed_case(app_engine, opened_by=pm1, title="First", opened_at=_T0)
    await _seed_case(app_engine, opened_by=pm1, title="Second", opened_at=_T0 + timedelta(hours=1))
    third = await _seed_case(
        app_engine, opened_by=pm1, title="Third", opened_at=_T0 + timedelta(hours=2)
    )
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=pm1) as session:
        repo = CaseRepository(session)
        await repo.wait(
            third.id,
            wait_until=date.today() + timedelta(days=30),
            by=pm1,
            now=_T0 + timedelta(hours=3),
        )
        # The bump puts the oldest open case first.
        await repo.update_working_file(first.id, counterparty="GP X", now=_T0 + timedelta(hours=4))

    await _login(web_client, email, password)
    response = await web_client.get("/api/cases/list", follow_redirects=False)
    assert response.status_code == 200
    body = response.text

    assert _row_badges(body) == ["CASE-0001", "CASE-0002", "CASE-0003"]
    group = cs_block(body, "group", data_pf_cs_state="waiting", data_pf_cs_count="1")
    assert body.index(cs("row", state="open", case="CASE-0002")) < body.index(group)
    assert body.index(group) < body.index(cs("row", state="waiting", case="CASE-0003"))
    waiting_row = cs_block(body, "row", data_pf_cs_case="CASE-0003")
    assert cs_text(waiting_row, "next").startswith("Waiting until")
    assert cs_text(body, "counts") == "2 open · 1 waiting · 0 closed"
    assert '<h2 class="pf-view__title" id="cases-title" hx-swap-oob="outerHTML">' in body


async def test_waiting_closed_and_all_filters(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """CS3a-04: Waiting alone; Closed newest first with the Journal; All; a bad filter."""
    pm1, email, password = seeded_user
    await _seed_case(app_engine, opened_by=pm1, title="Open one", opened_at=_T0)
    waiting = await _seed_case(app_engine, opened_by=pm1, title="Waiting one", opened_at=_T0)
    await _seed_case(
        app_engine,
        opened_by=pm1,
        title="Closed early",
        opened_at=_T0,
        close_note="done early",
        closed_at=_T0 + timedelta(hours=1),
    )
    await _seed_case(
        app_engine,
        opened_by=pm1,
        title="Closed late",
        opened_at=_T0,
        close_note="done late",
        closed_at=_T0 + timedelta(hours=5),
    )
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=pm1) as session:
        await CaseRepository(session).wait(
            waiting.id, wait_until=date.today() + timedelta(days=10), by=pm1, now=_T0
        )

    await _login(web_client, email, password)

    only_waiting = (await web_client.get("/api/cases/list?state=waiting")).text
    assert _row_badges(only_waiting) == ["CASE-0002"]
    assert cs_count(only_waiting, "group") == 0

    closed = (await web_client.get("/api/cases/list?state=closed")).text
    assert _row_badges(closed) == ["CASE-0004", "CASE-0003"]
    assert cs_count(closed, "row", data_pf_cs_state="open") == 0
    for row in cs_blocks(closed, "row"):
        assert cs_text(row, "sub").endswith("View in Journal ↗")
    assert "/watch-desk#journal" in closed

    every = (await web_client.get("/api/cases/list?state=all")).text
    assert sorted(_row_badges(every)) == ["CASE-0001", "CASE-0002", "CASE-0003", "CASE-0004"]
    assert cs_count(every, "group") == 0

    bogus = await web_client.get("/api/cases/list?state=bogus")
    assert bogus.status_code == 400


async def test_mine_filters_on_the_owner_not_the_opener(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """CS3a-05: *Mine* reads ``owner_user_id`` — a handed-over case leaves it."""
    pm1, email, password = seeded_user
    pm2 = await _seed_extra_user(
        fresh_superuser_engine, email="pm2@example.com", display_name="M. Otten"
    )
    handed = await _seed_case(app_engine, opened_by=pm1, title="Handed over", opened_at=_T0)
    await _seed_case(app_engine, opened_by=pm1, title="Kept", opened_at=_T0)
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=pm1) as session:
        await CaseRepository(session).hand_over(
            handed.id, to_user_id=pm2, by=pm1, now=_T0 + timedelta(hours=1)
        )

    await _login(web_client, email, password)

    mine = (await web_client.get("/api/cases/list?mine=1")).text
    assert _row_badges(mine) == ["CASE-0002"]
    assert 'name="mine" value="1" checked' in mine

    everyone = (await web_client.get("/api/cases/list")).text
    assert sorted(_row_badges(everyone)) == ["CASE-0001", "CASE-0002"]
    handed_row = cs_block(everyone, "row", data_pf_cs_case="CASE-0001")
    assert cs_text(handed_row, "owner") == "M. Otten"


async def test_search_reads_the_file_and_never_attachment_names(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """CS3a-06: a thread title is found; an attachment's filename is not; blank is no search."""
    pm1, email, password = seeded_user
    with_thread = await _seed_case(app_engine, opened_by=pm1, title="Threaded", opened_at=_T0)
    with_file = await _seed_case(app_engine, opened_by=pm1, title="Filed", opened_at=_T0)
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=pm1) as session:
        await CaseThreadRepository(session).add(
            with_thread.id, title="zzkeypersonzz clause", now=_T0 + timedelta(minutes=5)
        )
    await _seed_attachment(
        app_engine, case_id=with_file.id, uploaded_by=pm1, filename="zzsecretsheetzz.pdf"
    )

    await _login(web_client, email, password)

    hit = (await web_client.get("/api/cases/list?q=zzkeypersonzz")).text
    assert _row_badges(hit) == ["CASE-0001"]

    miss = (await web_client.get("/api/cases/list?q=zzsecretsheetzz")).text
    assert cs_count(miss, "row") == 0
    assert cs_text(miss, "empty").startswith("No cases match")

    blank = (await web_client.get("/api/cases/list?q=%20%20")).text
    assert sorted(_row_badges(blank)) == ["CASE-0001", "CASE-0002"]


async def test_row_columns_read_the_file(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """CS3a-07: sub line, stand (labelled subject), threads, next · due, owner, updated."""
    pm1, email, password = seeded_user
    now = datetime.now(timezone.utc)
    today = now.date()
    finding_id = await _seed_finding(app_engine, opened_by=pm1)
    case = await _seed_case(
        app_engine, opened_by=pm1, title="From a finding", opened_at=now, finding_id=finding_id
    )
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=pm1) as session:
        await CaseRepository(session).update_working_file(
            case.id, due_on=today + timedelta(days=20), now=now
        )
        threads = CaseThreadRepository(session)
        first = await threads.add(case.id, title="Is the ceiling binding?", now=now)
        await threads.add(case.id, title="Can we rebalance?", now=now)
        await threads.update(first.id, status="confirmed", now=now)
        await CasePointRepository(session).add(
            case.id, text="Call the GP", due_on=today + timedelta(days=3), now=now
        )

    await _login(web_client, email, password)
    body = (await web_client.get("/api/cases/list", follow_redirects=False)).text
    row = cs_block(body, "row", data_pf_cs_case="CASE-0001")

    assert cs_text(row, "sub").startswith("From finding · Private equity · noteworthy ·")
    stand = cs_text(row, "stand")
    assert stand and stand != "—"
    assert stand.startswith("Private equity")
    assert "saa:" not in stand
    assert cs_text(row, "threads") == "2 · 1 confirmed"
    next_block = cs_block(row, "next")
    assert cs_text(next_block).startswith("Call the GP · ")
    assert "is-soon" in next_block
    assert cs_text(row, "owner") == "S. Behrens"
    assert cs_text(row, "updated").startswith("today")


async def test_closed_filter_pages_by_before(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CS3a-08: a full Closed page offers "Show older"; the next page completes it."""
    monkeypatch.setattr(cases_module, "_CLOSED_PAGE_SIZE", 2)
    pm1, email, password = seeded_user
    for i in range(3):
        await _seed_case(
            app_engine,
            opened_by=pm1,
            title=f"Closed {i}",
            opened_at=_T0,
            close_note="done",
            closed_at=_T0 + timedelta(hours=i + 1),
        )

    await _login(web_client, email, password)
    first = (await web_client.get("/api/cases/list?state=closed")).text
    assert _row_badges(first) == ["CASE-0003", "CASE-0002"]
    # The hook names the loader row; the button inside it carries the request.
    older = re.search(r'hx-get="([^"]+)"', cs_block(first, "older"))
    assert older is not None and "before=" in older.group(1)
    older_href = older.group(1)

    second = await web_client.get(html.unescape(older_href))
    assert second.status_code == 200
    assert _row_badges(second.text) == ["CASE-0001"]
    assert cs_count(second.text, "older") == 0

    refused = await web_client.get(
        "/api/cases/list", params={"state": "open", "before": _T0.isoformat()}
    )
    assert refused.status_code == 400


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


async def test_create_case_lands_in_its_file(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """CS3a-09: a created case answers ``HX-Redirect`` to its file; one ``opened`` entry."""
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

    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=pm1) as session:
        repo = CaseRepository(session)
        cases = await repo.list_cases()
        assert len(cases) == 1
        created = cases[0]
        assert created.title == "Prepare advisory-board position"
        assert created.description == "Fund II continuation vehicle."
        assert created.opened_by == pm1
        entries = await repo.list_entries(created.id)

    assert response.headers.get("HX-Redirect") == f"/cases/{created.id}"
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
    response = await web_client.get(f"/api/cases/{case.id}", follow_redirects=False)
    assert response.status_code == 200
    body = response.text

    # Head: badge, title, open state, Cases nav active.
    assert "CASE-0001" in body
    assert "Prepare advisory-board position" in body
    assert cs("state", state="open") in body
    # The Cases nav marker is the shell's, so it is on the page, not the file.
    page = await web_client.get(f"/cases/{case.id}", follow_redirects=False)
    assert 'data-area="cases"' in page.text
    # The manual description stands in for the origin embed (no finding).
    assert "Fund II continuation vehicle" in body
    assert "Originating finding" not in body
    assert "At case opening" not in body
    # C3b: the Actions block and composer slot are present on an open case.
    assert 'id="case-composer-slot"' in body
    assert cs("actions") in body
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

    a_body = (await web_client.get(f"/api/cases/{with_mat.id}", follow_redirects=False)).text
    # Origin embed: band chip, subject_key, finding text, basis.
    assert "Originating finding" in a_body
    assert "Critical" in a_body
    assert "anlv:17" in a_body
    assert "crossed its 5.00% ceiling" in a_body
    assert "coverage 5.14% against a 5.00% ceiling" in a_body
    # Materiality-at-opening lines render verbatim under "At case opening".
    assert "At case opening" in a_body
    assert "headroom -118,400 EUR" in a_body

    b_body = (await web_client.get(f"/api/cases/{without_mat.id}", follow_redirects=False)).text
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
    body = (await web_client.get(f"/api/cases/{case.id}", follow_redirects=False)).text

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
    body = (await web_client.get(f"/api/cases/{case.id}", follow_redirects=False)).text
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

        body = (await web_client.get(f"/api/cases/{case.id}", follow_redirects=False)).text

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

    open_body = (await web_client.get(f"/api/cases/{open_case.id}", follow_redirects=False)).text
    assert "Capture scenario" in open_body
    assert f'href="/planning-desk?case={open_case.id}"' in open_body

    closed_body = (
        await web_client.get(f"/api/cases/{closed_case.id}", follow_redirects=False)
    ).text
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
    body = (await web_client.get(f"/api/cases/{case.id}", follow_redirects=False)).text

    # Closed state visible in the head and the rail; the closed entry carries
    # the closing note (mandatory close artifact, written by close()).
    assert cs("state", state="closed") in body
    assert "Case closed" in body
    assert "Breach resolved by reclassification, no transaction required." in body
    # Still nothing interactive — no composer slot, no actions, no composers.
    assert 'id="case-composer-slot"' not in body
    assert cs("actions") not in body
    assert "Add note" not in body
    assert "Close case" not in body


async def test_file_partial_writes_the_crumb_and_moves_the_page(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """CS3a-10: the file writes the crumb and the state, and holds what the page held."""
    pm1, email, password = seeded_user
    open_case = await _seed_case(
        app_engine,
        opened_by=pm1,
        title="Manual open case",
        description="Fund II continuation vehicle — reinvestment question.",
        opened_at=_T0,
    )
    closed_case = await _seed_case(
        app_engine,
        opened_by=pm1,
        title="Closed case",
        opened_at=_T0,
        close_note="Documented and closed.",
        closed_at=_T0 + timedelta(hours=1),
    )
    await _login(web_client, email, password)

    body = (await web_client.get(f"/api/cases/{open_case.id}", follow_redirects=False)).text
    crumb = cs_block(body, "crumb", data_pf_cs_case="CASE-0001")
    assert 'hx-get="/api/cases/list"' in crumb
    assert 'hx-push-url="/cases"' in crumb
    assert 'hx-swap-oob="outerHTML"' in crumb
    assert cs("state", state="open") in body
    cs_block(body, "outline-item", data_pf_cs_name="question")
    cs_block(body, "outline-item", data_pf_cs_name="record")
    assert "Opened" in cs_text(body, "facts")
    assert cs_count(body, "action") == 8
    for kind in ("note", "pin", "link", "attach", "capture", "consult", "decision", "close"):
        cs_block(body, "action", data_pf_cs_kind=kind)
    assert cs("composer-slot") in body
    assert "Fund II continuation vehicle" in body
    assert "/watch-desk#journal" not in body

    closed = (await web_client.get(f"/api/cases/{closed_case.id}", follow_redirects=False)).text
    assert cs("actions") not in closed
    assert cs("state", state="closed") in closed
    facts = cs_text(closed, "facts")
    assert "Closed" in facts
    assert "View in Journal ↗" in facts


def test_cases_module_registered() -> None:
    """CS3a-11: the Area's one Module registers and constructs (ADR-0058)."""
    classes = registry.list_by_area("cases")
    assert {cls.module_name for cls in classes} == {"cases"}
    assert len(classes) == 1
    config = get_config()
    for cls in classes:
        cls(config)


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
    assert page.status_code == 200
    pbody = (await web_client.get(f"/api/cases/{case.id}", follow_redirects=False)).text
    assert cs("state", state="closed") in pbody
    assert "Resolved by reclassification; no action." in pbody
    assert 'id="case-composer-slot"' not in pbody
    assert cs("actions") not in pbody

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
# C4 — the fifth resolution's origin chip and the chunked upload ceiling
# (rider 1); the Cases→Journal deep links are CS3a-04 / CS3a-10 now
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
    body = (await web_client.get(f"/api/cases/{case.id}", follow_redirects=False)).text

    assert "Opened case" in body
    assert "Opened_case" not in body  # not the |capitalize default
    assert "pf-dc-res-tag--opened_case" in body


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


# ---------------------------------------------------------------------------
# P-CS-3b — the editable file I: the autosave frame, The question, links and
# documents, Where we stand (CS3b-01 … CS3b-10)
# ---------------------------------------------------------------------------


async def _case_now(app_engine: AsyncEngine, user_id: UUID, case_id: UUID) -> CaseDTO:
    """Re-read one case through the repository (its current ``updated_at``)."""
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
        case = await CaseRepository(session).get(case_id)
    assert case is not None
    return case


async def _post_question(
    client: AsyncClient, case_id: UUID, csrf: str, *, rerender: bool = False, **fields: str
):
    """POST the question form (urlencoded) with ``fields``."""
    suffix = "?rerender=1" if rerender else ""
    return await client.post(
        f"/api/cases/{case_id}/question{suffix}",
        data={"csrf_token": csrf, **fields},
        follow_redirects=False,
    )


async def _post_attach(
    client: AsyncClient,
    case_id: UUID,
    csrf: str,
    *,
    filename: str = "memo.pdf",
    content: bytes = _SMALL_PDF,
    content_type: str = "application/pdf",
):
    """POST a multipart attach request (csrf field + one file part, no comment)."""
    return await client.post(
        f"/api/cases/{case_id}/attachments",
        data={"csrf_token": csrf},
        files={"file": (filename, content, content_type)},
        follow_redirects=False,
    )


async def _seed_investment(
    app_engine: AsyncEngine,
    user_id: UUID,
    *,
    like: UUID,
    name: str,
    manager_name: str | None = None,
    is_active: bool = True,
) -> UUID:
    """Create an investment in the asset class of ``like`` (the stand tests' idiom)."""
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
        repo = InvestmentRepository(session)
        template = await repo.get_by_id(like)
        assert template is not None
        created = await repo.create(
            name=name,
            investment_type="private_equity",
            asset_class_id=template.asset_class_id,
            currency="EUR",
            created_by=user_id,
            manager_name=manager_name,
            is_active=is_active,
        )
    return created.id


def _radio(block: str, value: str) -> str:
    """The framing radio's start tag for ``value``."""
    match = re.search(rf'<input type="radio" name="framing" id="cs-framing-{value}"[^>]*>', block)
    assert match is not None, f"no framing radio {value!r}"
    return match.group(0)


async def test_question_block_renders_prose_meta_and_framing_heading(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """CS3b-01: the prose, the meta fields, the framing seg, the stamp; the task heading."""
    pm1, email, password = seeded_user
    case = await _seed_case(
        app_engine,
        opened_by=pm1,
        title="Continuation vehicle",
        description="Fund II continuation vehicle — reinvestment question.",
        opened_at=_T0,
    )
    await _login(web_client, email, password)

    body = (await web_client.get(f"/api/cases/{case.id}", follow_redirects=False)).text
    block = cs_block(body, "block", data_pf_cs_name="question")
    assert cs_text(block).startswith("The question")
    prose = cs_block(block, "prose")
    assert 'class="pf-textarea"' in prose
    assert "Fund II continuation vehicle — reinvestment question." in html.unescape(prose)
    assert 'name="counterparty"' in block
    assert 'list="cs-managers"' in block
    assert 'name="due_on"' in block and 'type="date"' in block
    assert "checked" in _radio(block, "question")
    assert "checked" not in _radio(block, "task")
    current = await _case_now(app_engine, pm1, case.id)
    assert f'id="cs-stamp-question" value="{current.updated_at.isoformat()}"' in block
    assert cs("note") not in block
    assert 'hx-swap="none"' in cs_block(block, "form", data_pf_cs_name="question")

    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=pm1) as session:
        await CaseRepository(session).update_working_file(
            case.id, now=_T0 + timedelta(hours=1), framing="task"
        )
    body = (await web_client.get(f"/api/cases/{case.id}", follow_redirects=False)).text
    block = cs_block(body, "block", data_pf_cs_name="question")
    assert cs_text(block).startswith("The task")
    assert "checked" in _radio(block, "task")
    assert "The task" in cs_text(body, "outline")


async def test_question_autosave_writes_and_answers_out_of_band_only(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """CS3b-02: a text autosave writes the fields and answers the OOB bundle alone."""
    pm1, email, password = seeded_user
    case = await _seed_case(app_engine, opened_by=pm1, title="Autosave", opened_at=_T0)
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)
    before = await _case_now(app_engine, pm1, case.id)

    response = await _post_question(
        web_client,
        case.id,
        csrf,
        description="  Should we roll into Fund III?  ",
        counterparty="Acme Partners",
        due_on="2026-12-15",
        framing="question",
        stamp=before.updated_at.isoformat(),
    )
    assert response.status_code == 200
    body = response.text
    after = await _case_now(app_engine, pm1, case.id)

    assert after.description == "Should we roll into Fund III?"
    assert after.counterparty == "Acme Partners"
    assert after.due_on == date(2026, 12, 15)
    assert after.framing == "question"
    assert after.updated_at > before.updated_at

    assert cs("block") not in body
    assert "Saved ·" in cs_text(body, "saved")
    assert 'id="cs-saved"' in body
    facts = cs_block(body, "facts")
    assert 'id="cs-facts"' in facts and 'hx-swap-oob="true"' in facts
    assert "Acme Partners" in cs_text(facts)
    assert "15 Dec 2026" in cs_text(facts)
    assert 'id="cs-outline"' in cs_block(body, "outline")
    assert f'id="cs-stamp-question" value="{after.updated_at.isoformat()}"' in body
    assert f'id="cs-stamp-stand" value="{after.updated_at.isoformat()}"' in body
    note_slot = cs_block(body, "note-slot")
    assert 'id="cs-note-question"' in note_slot
    assert cs_text(note_slot) == ""

    # An empty field clears its column.
    response = await _post_question(
        web_client, case.id, csrf, counterparty="", stamp=after.updated_at.isoformat()
    )
    assert response.status_code == 200
    cleared = await _case_now(app_engine, pm1, case.id)
    assert cleared.counterparty is None
    assert cleared.description == "Should we roll into Fund III?"


async def test_question_autosave_last_write_wins_with_a_note(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """CS3b-03: a stale stamp still saves and says so; the fresh stamp clears the note."""
    pm1, email, password = seeded_user
    case = await _seed_case(app_engine, opened_by=pm1, title="Two tabs", opened_at=_T0)
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)
    current = await _case_now(app_engine, pm1, case.id)
    stale = (current.updated_at - timedelta(minutes=5)).isoformat()

    response = await _post_question(
        web_client, case.id, csrf, description="From the older tab.", stamp=stale
    )
    assert response.status_code == 200
    assert (await _case_now(app_engine, pm1, case.id)).description == "From the older tab."
    note = cs_text(response.text, "note")
    assert note.startswith("Saved over a newer version from ")
    assert f"{current.updated_at.astimezone(timezone.utc):%H:%M}" in note
    assert "read the text once" in note

    fresh = await _case_now(app_engine, pm1, case.id)
    response = await _post_question(
        web_client,
        case.id,
        csrf,
        description="From the fresh tab.",
        stamp=fresh.updated_at.isoformat(),
    )
    assert response.status_code == 200
    assert cs("note") not in response.text
    assert cs_text(response.text, "note-slot") == ""


async def test_question_refusals_save_nothing(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """CS3b-04: a malformed date or framing → 400 in the note; a closed case → calm error."""
    pm1, email, password = seeded_user
    case = await _seed_case(
        app_engine, opened_by=pm1, title="Refusals", description="Kept.", opened_at=_T0
    )
    closed = await _seed_case(
        app_engine,
        opened_by=pm1,
        title="Closed",
        description="Frozen.",
        opened_at=_T0,
        close_note="done",
    )
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)
    before = await _case_now(app_engine, pm1, case.id)

    response = await _post_question(
        web_client, case.id, csrf, description="Changed.", due_on="31-02-2026"
    )
    assert response.status_code == 400
    assert response.headers.get("HX-Reswap") == "none"
    assert 'id="cs-note-question"' in response.text
    assert "'31-02-2026' is not a date" in cs_text(response.text, "note")
    after = await _case_now(app_engine, pm1, case.id)
    assert after.description == "Kept."
    assert after.updated_at == before.updated_at

    response = await _post_question(
        web_client, case.id, csrf, description="Changed.", framing="watch"
    )
    assert response.status_code == 400
    assert "Invalid framing 'watch'" in cs_text(response.text, "note")
    assert (await _case_now(app_engine, pm1, case.id)).description == "Kept."

    response = await _post_question(web_client, closed.id, csrf, description="Changed.")
    assert response.status_code == 200
    assert "closed cases are read-only" in cs_text(response.text, "note")
    assert (await _case_now(app_engine, pm1, closed.id)).description == "Frozen."

    # A closed case renders the question read-only: the prose as text, no form.
    body = (await web_client.get(f"/api/cases/{closed.id}", follow_redirects=False)).text
    block = cs_block(body, "block", data_pf_cs_name="question")
    assert cs("form") not in block
    assert '<p class="pf-prose" data-pf-cs="prose">Frozen.</p>' in block


async def test_framing_switch_rerenders_the_block(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """CS3b-05: ``rerender=1`` answers the question block (The task) and the bundle."""
    pm1, email, password = seeded_user
    case = await _seed_case(app_engine, opened_by=pm1, title="Reframe", opened_at=_T0)
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    response = await _post_question(web_client, case.id, csrf, rerender=True, framing="task")
    assert response.status_code == 200
    body = response.text
    block = cs_block(body, "block", data_pf_cs_name="question")
    assert cs_text(block).startswith("The task")
    assert "hx-swap-oob" not in block
    assert "Saved ·" in cs_text(body, "saved")
    assert "The task" in cs_text(body, "outline")
    assert (await _case_now(app_engine, pm1, case.id)).framing == "task"


async def test_link_and_unlink_investment_rerender_where_we_stand(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """CS3b-06: the composer lists the candidates; link/unlink re-render the stand."""
    pm1, email, password = seeded_user
    alpha = await seed_book(app_engine, SENTINEL_TENANT_ID, pm1, latest_nav=CALM_NAV)
    dormant = await _seed_investment(
        app_engine, pm1, like=alpha, name="Dormant Fund", is_active=False
    )
    case = await _seed_case(app_engine, opened_by=pm1, title="Link me", opened_at=_T0)
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    composer = await web_client.get(
        f"/api/cases/{case.id}/composer/link-investment", follow_redirects=False
    )
    assert composer.status_code == 200
    assert f'value="{alpha}"' in composer.text
    assert "Alpha" in composer.text
    assert f'value="{dormant}"' not in composer.text

    async def _link(path: str, investment_id: str):
        return await web_client.post(
            f"/api/cases/{case.id}/{path}",
            data={"csrf_token": csrf, "investment_id": investment_id},
            follow_redirects=False,
        )

    response = await _link("link-investment", str(alpha))
    assert response.status_code == 200
    stand = cs_block(response.text, "block", data_pf_cs_name="stand")
    assert cs("stand-investment", name="Alpha") in stand
    assert cs("unlink") in stand
    assert "1 investments" in cs_text(response.text, "facts")
    assert 'id="case-composer-slot" hx-swap-oob="innerHTML"' in response.text

    again = await _link("link-investment", str(alpha))
    assert again.status_code == 200
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=pm1) as session:
        assert len(await CaseInvestmentRepository(session).list_for_case(case.id)) == 1

    # Linked investments leave the composer's candidates.
    composer = await web_client.get(
        f"/api/cases/{case.id}/composer/link-investment", follow_redirects=False
    )
    assert f'value="{alpha}"' not in composer.text

    response = await _link("unlink-investment", str(alpha))
    assert response.status_code == 200
    stand = cs_block(response.text, "block", data_pf_cs_name="stand")
    assert cs("stand-investment", name="Alpha") not in stand
    assert "No investment linked — link one from the actions." in cs_text(stand)
    assert "0 investments" in cs_text(response.text, "facts")

    inactive = await _link("link-investment", str(dormant))
    assert inactive.status_code == 400
    assert "only an active investment can be linked" in cs_text(inactive.text, "note")
    unknown = await _link("link-investment", str(uuid4()))
    assert unknown.status_code == 404
    malformed = await _link("link-investment", "not-a-uuid")
    assert malformed.status_code == 400
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=pm1) as session:
        assert await CaseInvestmentRepository(session).list_for_case(case.id) == []


async def test_attach_document_stores_without_a_record_entry_and_lists_it(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """CS3b-07: an attachment alone — listed in the question block, no record entry."""
    pm1, email, password = seeded_user
    case = await _seed_case(app_engine, opened_by=pm1, title="Attach", opened_at=_T0)
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=pm1) as session:
        entries_before = len(await CaseRepository(session).list_entries(case.id))

    composer = await web_client.get(f"/api/cases/{case.id}/composer/attach")
    assert composer.status_code == 200
    assert 'hx-encoding="multipart/form-data"' in composer.text
    assert 'name="comment"' not in composer.text

    response = await _post_attach(web_client, case.id, csrf, filename="memo.pdf")
    assert response.status_code == 200
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=pm1) as session:
        atts = await CaseAttachmentRepository(session).list_for_case(case.id)
        entries_after = len(await CaseRepository(session).list_entries(case.id))
    assert len(atts) == 1
    assert atts[0].sha256 == hashlib.sha256(_SMALL_PDF).hexdigest()
    assert entries_after == entries_before

    block = cs_block(response.text, "block", data_pf_cs_name="question")
    document = cs_block(block, "document")
    assert "memo.pdf" in document
    assert f'href="/api/cases/{case.id}/attachments/{atts[0].id}"' in document
    cap = get_config().case_attachment_max_count
    assert (
        cs_text(response.text, "fact", data_pf_cs_name="attachments") == f"Attachments 1 of {cap}"
    )
    assert "0 investments · 1 documents" in cs_text(response.text, "facts")

    # The documents list is part of the file's first render too.
    body = (await web_client.get(f"/api/cases/{case.id}", follow_redirects=False)).text
    assert "memo.pdf" in cs_text(body, "documents")


async def test_attach_document_gates_reuse_the_pin_gates(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
    fresh_superuser_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CS3b-08: a disallowed type and an oversized body re-render the composer, write nothing."""
    pm1, email, password = seeded_user
    case = await _seed_case(app_engine, opened_by=pm1, title="Attach gates", opened_at=_T0)
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    async def _count() -> int:
        async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=pm1) as session:
            return await CaseAttachmentRepository(session).count_for_case(case.id)

    response = await _post_attach(
        web_client,
        case.id,
        csrf,
        filename="notes.txt",
        content=b"hello there",
        content_type="text/plain",
    )
    assert response.status_code == 200
    assert response.headers.get("HX-Retarget") == "#case-composer-slot"
    assert "not allowed" in cs_text(response.text, "error")
    assert cs("composer", kind="attach") in response.text
    assert await _count() == 0

    monkeypatch.setattr(get_config(), "case_attachment_max_bytes", len(_SMALL_PDF) - 1)
    response = await _post_attach(web_client, case.id, csrf)
    assert "per-file" in cs_text(response.text, "error")
    assert await _count() == 0


async def test_where_we_stand_renders_live_and_at_close(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """CS3b-09: live on an open case (subject, investment row); frozen *At close*."""
    pm1, email, password = seeded_user
    alpha = await seed_book(app_engine, SENTINEL_TENANT_ID, pm1, latest_nav=CALM_NAV)
    finding_id = await _seed_finding(app_engine, opened_by=pm1)
    live = await _seed_case(
        app_engine, opened_by=pm1, title="Live", finding_id=finding_id, opened_at=_T0
    )
    closing = await _seed_case(app_engine, opened_by=pm1, title="Closing", opened_at=_T0)
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=pm1) as session:
        links = CaseInvestmentRepository(session)
        for case_id in (live.id, closing.id):
            await links.link(case_id, investment_id=alpha, linked_by=pm1, now=_T0)
    await _login(web_client, email, password)
    nav_text = format_money_compact(float(CALM_NAV), "EUR")

    body = (await web_client.get(f"/api/cases/{live.id}", follow_redirects=False)).text
    stand = cs_block(body, "block", data_pf_cs_name="stand")
    assert cs_text(stand).startswith("Where we stand")
    subject = cs_text(stand, "stand-subject")
    assert "Private equity" in subject
    assert "saa:" not in subject
    row = cs_text(stand, "stand-investment", data_pf_cs_name="Alpha")
    assert nav_text in row
    assert cs("stand-refresh") in stand
    assert cs("unlink") in stand
    assert "live ·" in cs_text(stand, "stand-source")
    assert "live" in cs_text(body, "outline-item", data_pf_cs_name="stand")

    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=pm1) as session:
        repo = CaseRepository(session)
        current = await repo.get(closing.id)
        assert current is not None
        ctx = await StandContext.load(session, now=_T0 + timedelta(days=1))
        frozen = to_snapshot(await compose(ctx, current))
        await repo.close(
            closing.id,
            closed_by=pm1,
            closing_note="Settled.",
            now=_T0 + timedelta(days=1),
            stand=frozen,
        )

    body = (await web_client.get(f"/api/cases/{closing.id}", follow_redirects=False)).text
    stand = cs_block(body, "block", data_pf_cs_name="stand")
    assert cs_text(stand).startswith("At close")
    assert nav_text in cs_text(stand, "stand-investment", data_pf_cs_name="Alpha")
    assert "frozen · 09:00 22 Jul 2026" in cs_text(stand, "stand-source")
    assert cs("stand-refresh") not in stand
    assert cs("unlink") not in stand
    assert "at close" in cs_text(body, "outline-item", data_pf_cs_name="stand")

    # A case closed without a frozen stand says so instead of inventing one.
    plain = await _seed_case(
        app_engine, opened_by=pm1, title="Plain close", opened_at=_T0, close_note="done"
    )
    body = (await web_client.get(f"/api/cases/{plain.id}", follow_redirects=False)).text
    assert "was not frozen" in cs_text(body, "stand-missing")


async def test_section_endpoint_renders_one_block_and_404s_unknown(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """CS3b-10: one block alone, no bundle; unknown sections and cases are 404."""
    pm1, email, password = seeded_user
    case = await _seed_case(app_engine, opened_by=pm1, title="Sections", opened_at=_T0)
    await _login(web_client, email, password)

    question = await web_client.get(f"/api/cases/{case.id}/sections/question")
    assert question.status_code == 200
    assert cs_count(question.text, "block") == 1
    cs_block(question.text, "block", data_pf_cs_name="question")
    assert "hx-swap-oob" not in question.text
    assert 'id="cs-facts"' not in question.text

    revealed = await web_client.get(f"/api/cases/{case.id}/sections/question?reveal=1")
    assert "autofocus" in cs_block(revealed.text, "prose")

    stand = await web_client.get(f"/api/cases/{case.id}/sections/stand")
    assert stand.status_code == 200
    assert cs_count(stand.text, "block") == 1
    cs_block(stand.text, "block", data_pf_cs_name="stand")

    assert (await web_client.get(f"/api/cases/{case.id}/sections/threads")).status_code == 404
    assert (await web_client.get(f"/api/cases/{uuid4()}/sections/question")).status_code == 404
