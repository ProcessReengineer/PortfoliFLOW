# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the sidebar shell and HTMX-partial area switching.

Sub-stream 6F-1 of Phase 6 Block 1 (ADR-0046). Covers:

* Per-area URL renders the sidebar with all nine areas, in order.
* Active-state highlighting is correct for each area URL.
* HTMX requests (``HX-Request: true``) get a partial fragment and
  no full ``<html>`` / ``<body>`` wrapper.
* HTMX responses include the out-of-band sidebar fragment with the
  correct active marker.
* Direct-navigation responses include the full layout.
* Legacy module URLs (``/charts``, ``/statistics``,
  ``/portfolio-analysis``, ``/import``, ``/portfolio-review``)
  return 404.
* Status bar renders area name, tenant, build SHA, config flag.
* Sidebar collapse cookie persists across requests.

The fixture pattern matches ``test_login_flow.py`` and
``test_chat_routes.py`` — live-DB, ASGITransport, sentinel tenant.
"""

from __future__ import annotations

import os
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
from web.shell import all_areas

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")


# Derived from ``web.shell`` — the single source of truth for the area
# catalogue (the ``test_section_navigation.py`` idiom). Each row is
# ``(slug, url, label)``; these tests assert that the RENDERED sidebar and
# status bar agree with whatever the catalogue declares. The deliberate
# sidebar-order pin lives once, as the glyph-sequence assert in
# ``tests/web/test_sidebar_glyph_and_auth_polish.py``.
_AREAS: tuple[tuple[str, str, str], ...] = tuple(
    (area.slug, area.url, area.label) for area in all_areas()
)


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB shell-sidebar tests.",
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
    email = "shell@example.com"
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
    """Drive ``GET /login`` + ``POST /login`` to seat a session cookie."""
    get_response = await client.get("/login")
    csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
    assert csrf is not None
    await client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf},
        follow_redirects=False,
    )


# ---------------------------------------------------------------------------
# Sidebar render and active state — one assertion per area
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug,url,label", _AREAS)
async def test_web_sidebar_renders_for_each_area(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    slug: str,
    url: str,
    label: str,
) -> None:
    """Each area URL renders the sidebar with all nine areas."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get(url, follow_redirects=False)
    assert response.status_code == 200, f"{url} returned {response.status_code}"
    body = response.text
    # All nine area labels appear in the sidebar.
    for _slug2, _url2, label2 in _AREAS:
        assert label2 in body, f"{url} missing sidebar entry for {label2}"
    # The sidebar marker id is present (the OOB swap target).
    assert 'id="pf-sidebar"' in body


async def test_web_sidebar_renders_areas_in_order(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The sidebar emits the areas in the order ``web.shell`` declares.

    Sequence, not membership: the rendered ``_partials/sidebar.html`` hrefs
    must equal the catalogue order (Front Office → Back Office → Assistants
    → Planning Desk → Investor Communication → Watch Desk → Cases →
    Transactions → Admin, ADR-0122 §1 with Transactions inserted by
    ADR-0128 §7). This asserts the template renders in step with
    ``web.shell``; the deliberate order pin — held independently of the
    catalogue — is the glyph-sequence assert in
    ``tests/web/test_sidebar_glyph_and_auth_polish.py``.
    """
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get("/front-office", follow_redirects=False)
    assert response.status_code == 200

    import re

    rendered = re.findall(r'<a class="pf-sidebar__item[^"]*"\s+href="([^"]+)"', response.text)
    assert rendered == [url for _slug, url, _label in _AREAS], f"sidebar rendered {rendered!r}"


@pytest.mark.parametrize("slug,url,label", _AREAS)
async def test_web_sidebar_active_state(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    slug: str,
    url: str,
    label: str,
) -> None:
    """The sidebar entry for the requested area carries ``is-active``."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get(url, follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    # The active entry combines its slug with the is-active class.
    # We assert both the slug appears in a data-area attribute and
    # that the active CSS class string appears in the same vicinity
    # (the simplest reliable heuristic without a real HTML parser).
    assert f'data-area="{slug}"' in body
    # ``is-active`` shows up exactly once — for the active area.
    assert body.count("pf-sidebar__item is-active") == 1
    # And it co-occurs with the active slug. Chunk extraction:
    active_chunk_start = body.index("pf-sidebar__item is-active")
    active_chunk = body[active_chunk_start : active_chunk_start + 200]
    assert f'data-area="{slug}"' in active_chunk


# ---------------------------------------------------------------------------
# HTMX-partial response branching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug,url,_label", _AREAS)
async def test_web_htmx_partial_response(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    slug: str,
    url: str,
    _label: str,
) -> None:
    """HTMX requests get a partial fragment without the full layout."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get(
        url,
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert "<html" not in body.lower()
    assert "<body" not in body.lower()
    # The OOB sidebar fragment IS in the response.
    assert 'hx-swap-oob="outerHTML"' in body
    # The area body's data-area attribute is present.
    assert f'data-area="{slug}"' in body


@pytest.mark.parametrize("slug,url,_label", _AREAS)
async def test_web_full_response_when_no_htmx_header(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    slug: str,
    url: str,
    _label: str,
) -> None:
    """Direct navigation returns the full HTML document."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get(url, follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    assert "<html" in body.lower()
    assert "<body" in body.lower()
    # No OOB swap on a non-HTMX response — the persistent sidebar
    # is in the document body, not as a swap fragment.
    assert 'hx-swap-oob="outerHTML"' not in body


@pytest.mark.parametrize("slug,url,_label", _AREAS)
async def test_web_oob_sidebar_fragment_marks_correct_area(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    slug: str,
    url: str,
    _label: str,
) -> None:
    """The OOB sidebar fragment marks the requested area as active."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get(
        url,
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    # The OOB sidebar fragment carries the active slug. Same proximity
    # check as the full-response active-state test.
    assert "pf-sidebar__item is-active" in body
    active_chunk_start = body.index("pf-sidebar__item is-active")
    active_chunk = body[active_chunk_start : active_chunk_start + 200]
    assert f'data-area="{slug}"' in active_chunk


# ---------------------------------------------------------------------------
# Legacy URL retirement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "/charts",
        "/statistics",
        "/portfolio-analysis",
        "/import",
        "/portfolio-review",
    ],
)
async def test_web_legacy_urls_404(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    url: str,
) -> None:
    """The retired module URLs return 404."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get(url, follow_redirects=False)
    assert response.status_code == 404, f"{url} should be retired (404); got {response.status_code}"


# ---------------------------------------------------------------------------
# Status bar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug,url,label", _AREAS)
async def test_web_statusbar_contents(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    slug: str,
    url: str,
    label: str,
) -> None:
    """Status bar shows area, tenant, Cmd+K hint, build SHA and config flag."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get(url, follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    # Status bar wrapper class is present.
    assert "pf-statusbar" in body
    # Active area label appears in the status bar's left group.
    assert label in body
    # Tenant name (from the dev fallback or auth surface).
    assert "Sentinel Tenant" in body
    # Cmd+K hint key.
    assert "pf-statusbar__shortcut-key" in body
    # Build SHA placeholder.
    assert "pf-statusbar__build" in body
    # Config status indicator data attribute.
    assert "pf-statusbar__config" in body


# ---------------------------------------------------------------------------
# Sidebar collapse cookie
# ---------------------------------------------------------------------------


async def test_web_sidebar_collapse_state_persists(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The pf_sidebar_collapsed cookie is honoured across requests.

    Drives the full lifecycle: initial expanded render → POST to
    the toggle endpoint sets the cookie → subsequent renders show
    the collapsed state on every area URL.
    """
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    # First request — cookie absent, sidebar is expanded.
    expanded = await web_client.get("/front-office", follow_redirects=False)
    assert expanded.status_code == 200
    assert 'data-sidebar-collapsed="false"' in expanded.text

    # Pull the session CSRF token off the rendered page.
    import re

    csrf_match = re.search(r'name="csrf_token"\s+value="([^"]+)"', expanded.text)
    assert csrf_match is not None
    csrf = csrf_match.group(1)

    # Toggle — the response sets pf_sidebar_collapsed=true and
    # httpx's cookie jar persists it for subsequent requests.
    toggle = await web_client.post(
        "/shell/sidebar/toggle",
        data={"csrf_token": csrf, "redirect_to": "/front-office"},
        follow_redirects=False,
    )
    assert toggle.status_code == 303
    assert toggle.cookies.get("pf_sidebar_collapsed") == "true"

    collapsed = await web_client.get("/front-office", follow_redirects=False)
    assert collapsed.status_code == 200
    assert 'data-sidebar-collapsed="true"' in collapsed.text

    # And it survives a navigation to a different area.
    other = await web_client.get("/investor-communication", follow_redirects=False)
    assert other.status_code == 200
    assert 'data-sidebar-collapsed="true"' in other.text


# ---------------------------------------------------------------------------
# Sidebar toggle endpoint
# ---------------------------------------------------------------------------


async def test_web_sidebar_toggle_flips_cookie(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """POST /shell/sidebar/toggle flips the collapse cookie."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    # Pull the session CSRF token off the area page.
    page = await web_client.get("/front-office", follow_redirects=False)
    assert page.status_code == 200
    import re

    csrf_match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
    assert csrf_match is not None
    csrf = csrf_match.group(1)

    # First toggle — set the cookie to "true".
    response = await web_client.post(
        "/shell/sidebar/toggle",
        data={"csrf_token": csrf, "redirect_to": "/front-office"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/front-office"
    assert response.cookies.get("pf_sidebar_collapsed") == "true"

    # Second toggle — back to "false".
    response = await web_client.post(
        "/shell/sidebar/toggle",
        data={"csrf_token": csrf, "redirect_to": "/front-office"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.cookies.get("pf_sidebar_collapsed") == "false"


async def test_web_sidebar_toggle_open_redirect_blocked(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Off-origin redirect targets are sanitised to /front-office."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    page = await web_client.get("/front-office", follow_redirects=False)
    import re

    csrf_match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
    assert csrf_match is not None
    csrf = csrf_match.group(1)

    response = await web_client.post(
        "/shell/sidebar/toggle",
        data={
            "csrf_token": csrf,
            "redirect_to": "https://evil.example.com/",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/front-office"


# ---------------------------------------------------------------------------
# Area body content — Admin pointer tiles
# ---------------------------------------------------------------------------


async def test_web_admin_investments_tile_carries_settings_button(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The Admin Investments pointer tile offers the same button as Charts.

    The Front-Office Charts section and this tile are the two entries to
    the investment maintenance surface (ADR-0043 §5); they carry the same
    label so they read as one affordance. Role-blind like the tile itself
    — the ``/investments`` list GET is session-gated, not owner-gated.
    """
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get("/admin", follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    assert "Change investment settings" in body
    assert 'href="/investments"' in body
