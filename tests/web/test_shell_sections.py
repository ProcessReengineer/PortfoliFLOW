# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the one-section-per-view shell frame (P-UX-A0b).

The rendered half of the landing-view contract: an area page carries
every section in the DOM, shows the landing one, and hides the rest
behind the ``hidden`` attribute. The sidebar's second level lists the
active area's sections and marks the landing one ``aria-current``.

The complementary catalogue half — which slug is the landing view and
why — is DB-free and lives in ``tests/web/test_shell_catalogue.py``.

Browser behaviour (the fragment switching views, back/forward, the
loaders firing on ``intersect``) is the operator walk in
``docs/reports/P-UX-A0b-report.md``; ASGI-level markup is what this
module can prove.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncGenerator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.tenant_constants import SENTINEL_TENANT_ID
from services.password_hashing import hash_password
from web.main import create_app
from web.settings import WebSettings
from web.shell import all_sections, landing_section_for

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB shell-section tests.",
            allow_module_level=False,
        )


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
    truncate_sql = text(
        "TRUNCATE TABLE data_upload_sheets, data_uploads, "
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
    email = "shell-sections@example.com"
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


async def _login(client: AsyncClient, email: str, password: str) -> None:
    get_response = await client.get("/login")
    csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
    assert csrf is not None
    await client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf},
        follow_redirects=False,
    )


# The opening tag of one section block, with its ``hidden`` attribute if
# the server emitted one. Anchored on ``data-pf-section`` because that is
# the attribute ``shell.js`` keys on.
_SECTION_TAG_RE: re.Pattern[str] = re.compile(
    r'<section class="pf-section"[^>]*data-pf-section="([^"]+)"([^>]*)>'
)


def _section_visibility(body: str) -> dict[str, bool]:
    """Map section slug to whether the server rendered it visible."""
    return {slug: "hidden" not in rest for slug, rest in _SECTION_TAG_RE.findall(body)}


async def test_transactions_opens_on_the_blotter(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """``/transactions`` shows ``blotter`` and hides its two neighbours."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get("/transactions", follow_redirects=False)
    assert response.status_code == 200

    visible = _section_visibility(response.text)
    assert visible == {"new": False, "blotter": True, "history": False}


async def test_front_office_opens_on_its_first_section(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """An area that flags nothing shows its first section and no other."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get("/front-office", follow_redirects=False)
    assert response.status_code == 200

    visible = _section_visibility(response.text)
    expected = {
        section.slug: section.slug == landing_section_for("front_office")
        for section in all_sections("front_office")
    }
    assert visible == expected


async def test_sidebar_second_level_lists_the_active_area_sections(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The nav's second level carries one link per catalogue section."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    body = (await web_client.get("/transactions", follow_redirects=False)).text

    assert 'class="pf-sidebar__sections"' in body
    links = re.findall(r'data-pf-section-link="([^"]+)"', body)
    assert links == [section.slug for section in all_sections("transactions")]


async def test_sidebar_second_level_marks_the_landing_section(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """``aria-current`` sits on the landing link, and on that one only."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    body = (await web_client.get("/transactions", follow_redirects=False)).text

    marked = re.findall(
        r'data-pf-section-link="([^"]+)" aria-current="true"',
        body,
    )
    assert marked == ["blotter"]


async def test_area_page_carries_no_section_indicator(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The right-edge dot strip retired with the long-scroll page."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    body = (await web_client.get("/transactions", follow_redirects=False)).text
    assert "pf-section-indicator" not in body


async def test_area_page_links_the_shell_script(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Without ``shell.js`` the fragment would not switch views."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    body = (await web_client.get("/transactions", follow_redirects=False)).text
    assert "/static/js/shell.js" in body


async def test_every_section_carries_one_view_header(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """One ``pf-view__head`` per section — hidden ones included.

    The header is part of the section, not of the page: it has to come
    with the view when the fragment switches, so it is rendered up front
    for all of them rather than moved around by script.
    """
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    body = (await web_client.get("/transactions", follow_redirects=False)).text
    assert body.count('class="pf-view__head"') == len(all_sections("transactions"))


def test_layout_css_makes_hidden_sections_actually_hidden() -> None:
    """``.pf-section[hidden]`` must restate ``display: none``.

    An author ``display`` declaration beats the user-agent's
    ``[hidden] { display: none }`` whatever the specificity, so
    ``.pf-section { display: flex }`` alone would leave every hidden
    section on screen — and every one of its ``intersect once`` loaders
    would fire at page load. That is the precise failure this shell
    exists to avoid, and one deleted rule is all it takes to bring it
    back, so it is pinned here rather than left to the browser walk.
    """
    css = (_REPO_ROOT / "web" / "static" / "css" / "layout.css").read_text(encoding="utf-8")
    rule = re.search(r"\.pf-section\[hidden\]\s*\{([^}]*)\}", css, flags=re.DOTALL)
    assert rule is not None, "layout.css is missing the .pf-section[hidden] rule"
    assert "display: none" in rule.group(1)


def test_shell_js_issues_no_request() -> None:
    """The view switch is DOM-only: no fetch, no htmx ajax, no hx-* write.

    This is the whole reason a hidden section costs nothing — it must
    stay that way, so the script is pinned against acquiring a fetch.
    """
    js = (_REPO_ROOT / "web" / "static" / "js" / "shell.js").read_text(encoding="utf-8")
    assert "fetch(" not in js
    assert "htmx.ajax" not in js
    assert "hx-get" not in js
