# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for section anchors, the sticky view header and the palette.

Sub-stream 6F-2 originally, re-aimed by P-UX-A0b at the surface that
replaced it: the right-edge dot strip and its scroll-spy are gone — an
area shows one section at a time and the URL fragment selects it — so
what used to assert the indicator now asserts its absence. The command
palette survived the change intact and is still covered here.

The tests are deliberately ASGI-level: they assert markup, CSS rules
and JSON endpoint shapes. Real browser behaviour (the fragment
switching views, Ctrl K, focus traps) is the operator walk in
``docs/reports/P-UX-A0b-report.md``.
"""

from __future__ import annotations

import html
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
from web.shell import all_areas, all_sections, section_title

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_STATIC_DIR: Path = _REPO_ROOT / "web" / "static"


# Derived, not hand-maintained. ``web.shell`` is the single source of
# truth for the area/section catalogue; these tests assert that the
# RENDERED pages agree with it. The complementary direction — that the
# catalogue agrees with what the body partials actually render — is
# guarded by
# tests/regression/test_section_catalogue_matches_body_partials.py.
# Together the two make "shell says X, page shows Y" structurally
# impossible without re-introducing a hand-copied slug list here.
_AREAS: tuple[tuple[str, str, tuple[str, ...]], ...] = tuple(
    (
        area.slug,
        area.url,
        tuple(section.slug for section in all_sections(area.slug)),
    )
    for area in all_areas()
)


# ---------------------------------------------------------------------------
# The sticky view header — P-UX-A0b
# ---------------------------------------------------------------------------

# The 6F-2 theme-token assertion is gone with the surface it guarded:
# the five ``--pf-section-indicator-*`` and two ``--pf-sticky-header-*``
# tokens are no longer read by any sheet. They still sit in the ``pf``
# block of config/chart_theme.json, which this strand does not touch —
# see docs/reports/P-UX-A0b-report.md for the parked cleanup.


def test_layout_css_sticky_view_header() -> None:
    """``.pf-view__head`` sticks, opaquely, against the document scroll.

    The header pins to the viewport rather than to an inner scroll
    container, so it needs a solid background: the old in-card blur
    read correctly only over the card it was cut into. The actual
    sticking behaviour is verified in the browser walk.
    """
    css = (_STATIC_DIR / "css" / "layout.css").read_text(encoding="utf-8")
    rule = re.search(r"\.pf-view__head\s*\{([^}]*)\}", css, flags=re.DOTALL)
    assert rule is not None, "layout.css is missing the .pf-view__head rule"
    body = rule.group(1)
    assert "position: sticky" in body
    assert "top: 0" in body
    assert "background-color: var(--ui-background-primary)" in body
    # The retired backdrop tokens must not come back with it.
    assert "--pf-sticky-header-blur" not in css
    assert "--pf-sticky-header-bg" not in css


def test_layout_css_main_does_not_scroll() -> None:
    """``.pf-main`` carries no ``overflow``.

    An inner scroll container would break two things at once: the view
    header would stick to the container instead of the viewport, and the
    ``intersect once`` loaders — plus tools/ux_atlas.py, which drives
    them by scrolling the page — would stop seeing the document move.
    """
    css = (_STATIC_DIR / "css" / "layout.css").read_text(encoding="utf-8")
    rule = re.search(r"\.pf-main\s*\{([^}]*)\}", css, flags=re.DOTALL)
    assert rule is not None, "layout.css is missing the .pf-main rule"
    assert "overflow" not in rule.group(1)


# ---------------------------------------------------------------------------
# Test fixtures (live DB) — copied pattern from
# tests/web/test_shell_sidebar_and_areas.py
# ---------------------------------------------------------------------------


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB section-navigation tests.",
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
    email = "section-nav@example.com"
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
# The retired section indicator — P-UX-A0b
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("area_slug,url,_section_slugs", _AREAS)
async def test_no_section_indicator_on_any_area(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    area_slug: str,
    url: str,
    _section_slugs: tuple[str, ...],
) -> None:
    """No area page carries the right-edge dot strip any more.

    It was a map of a long scroll; there is no long scroll. The sidebar's
    second level names the same sections and says which one you are on.
    """
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get(url, follow_redirects=False)
    assert response.status_code == 200
    assert "pf-section-indicator" not in response.text


@pytest.mark.parametrize("area_slug,url,_section_slugs", _AREAS)
async def test_no_section_indicator_in_htmx_fragment(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    area_slug: str,
    url: str,
    _section_slugs: tuple[str, ...],
) -> None:
    """Nor does the HTMX area-swap fragment, which used to re-emit it."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get(
        url,
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "pf-section-indicator" not in response.text


def test_section_nav_js_is_referenced_in_base_template() -> None:
    """``base.html`` includes the ``section_nav.js`` script tag."""
    base = (_REPO_ROOT / "web" / "templates" / "base.html").read_text(encoding="utf-8")
    assert "/static/js/section_nav.js" in base


def test_shell_js_replaced_the_scroll_spy() -> None:
    """``shell.js`` is linked and the scroll-spy is gone from its old home.

    The two halves are one fact: the indicator's ``bindScrollSpy`` left
    ``section_nav.js`` because ``shell.js`` marks the current section
    from the URL fragment instead of from a scroll position.
    """
    base = (_REPO_ROOT / "web" / "templates" / "base.html").read_text(encoding="utf-8")
    assert "/static/js/shell.js" in base
    js = (_STATIC_DIR / "js" / "section_nav.js").read_text(encoding="utf-8")
    assert "bindScrollSpy" not in js


# ---------------------------------------------------------------------------
# Command palette markup and endpoint — Commit 3
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("area_slug,url,_section_slugs", _AREAS)
async def test_command_palette_markup_present_on_every_area(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    area_slug: str,
    url: str,
    _section_slugs: tuple[str, ...],
) -> None:
    """Every area page carries the ``<dialog id="pf-palette">`` element."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get(url, follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    assert 'id="pf-palette"' in body
    assert 'class="pf-palette"' in body
    assert 'class="pf-palette__input"' in body
    assert 'id="pf-palette__results"' in body


def test_command_palette_js_binds_cmd_k() -> None:
    """``section_nav.js`` binds Cmd/Ctrl+K to open the palette."""
    js = (_STATIC_DIR / "js" / "section_nav.js").read_text(encoding="utf-8")
    # Test the hotkey gate: metaKey / ctrlKey + "k".
    assert "metaKey" in js
    assert "ctrlKey" in js
    assert "/api/cmd-search" in js


async def test_cmd_search_returns_full_catalogue_on_empty_query(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """An empty ``q`` returns all areas and all sections."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get("/api/cmd-search?q=", follow_redirects=False)
    assert response.status_code == 200
    payload = response.json()
    assert "areas" in payload
    assert "sections" in payload
    assert "actions" in payload
    # Nine areas (Watch Desk added in ADR-0089, Planning Desk in
    # ADR-0104, Cases in ADR-0107, Transactions in ADR-0128), no actions
    # today.
    area_slugs = {entry["slug"] for entry in payload["areas"]}
    assert area_slugs == {
        "front_office",
        "watch_desk",
        "cases",
        "transactions",
        "planning_desk",
        "back_office",
        "admin",
        "investor_communication",
        "assistants",
    }
    section_slugs = {entry["slug"] for entry in payload["sections"]}
    expected_section_slugs: set[str] = set()
    for _slug, _url, slugs in _AREAS:
        expected_section_slugs.update(slugs)
    assert section_slugs == expected_section_slugs
    assert payload["actions"] == []


async def test_cmd_search_filters_by_substring(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """``q=charts`` returns only entries whose label or slug match."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get("/api/cmd-search?q=charts", follow_redirects=False)
    assert response.status_code == 200
    payload = response.json()
    assert payload["areas"] == []
    assert len(payload["sections"]) == 1
    assert payload["sections"][0]["slug"] == "charts"
    assert payload["sections"][0]["url"] == "/front-office#charts"
    assert payload["sections"][0]["area"] == "front_office"


async def test_cmd_search_filter_is_case_insensitive(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Substring matching ignores case on both labels and slugs."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get("/api/cmd-search?q=FRONT", follow_redirects=False)
    assert response.status_code == 200
    payload = response.json()
    area_slugs = {entry["slug"] for entry in payload["areas"]}
    assert "front_office" in area_slugs


async def test_cmd_search_requires_authentication(
    web_client: AsyncClient,
) -> None:
    """Unauthenticated requests get a 303 redirect to ``/login``.

    Matches the ``require_session`` pattern in ``web/auth.py`` —
    plain GETs raise 303 + ``Location: /login`` so the browser does a
    full-page navigation rather than swapping a fragment.
    """
    response = await web_client.get("/api/cmd-search?q=", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers.get("location") == "/login"


async def test_cmd_search_requires_authentication_htmx(
    web_client: AsyncClient,
) -> None:
    """HTMX-flagged unauthenticated requests get 401 + ``HX-Redirect``.

    The palette JS sends plain ``fetch`` so the 303 path is what
    matters most, but the HTMX branch exists in ``require_session``
    too — exercise it to lock the existing contract.
    """
    response = await web_client.get(
        "/api/cmd-search?q=",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert response.headers.get("hx-redirect") == "/login"


# ---------------------------------------------------------------------------
# Section anchor presence per area — Commit 4
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("area_slug,url,section_slugs", _AREAS)
async def test_section_anchors_match_module_slugs(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    area_slug: str,
    url: str,
    section_slugs: tuple[str, ...],
) -> None:
    """Every expected section slug appears as an ``id`` on the area page.

    The slug list per area is the canonical 6F-2 catalogue from §3 of
    the prompt and from :func:`web.shell.section_index_for`. A miss
    here means either the body partial drifted from the catalogue, or
    the catalogue drifted from the registry — both classed as a
    sub-stream regression.
    """
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get(url, follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    for slug in section_slugs:
        assert f'id="{slug}"' in body, f'{url} is missing section anchor id="{slug}"'


@pytest.mark.parametrize("area_slug,url,section_slugs", _AREAS)
async def test_section_anchors_in_htmx_fragment(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    area_slug: str,
    url: str,
    section_slugs: tuple[str, ...],
) -> None:
    """The HTMX area-swap fragment carries the same section anchors."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get(
        url,
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    for slug in section_slugs:
        assert f'id="{slug}"' in body, f'{url} (HTMX) is missing section anchor id="{slug}"'


# ---------------------------------------------------------------------------
# Rendered section headings — P-UX-2
# ---------------------------------------------------------------------------

# The ``<h2>`` emitted by web/templates/areas/_section.html. The id
# anchors the match to a section heading (sub-surface headings and page
# ``<h1>``s do not carry the ``-title`` suffix), and the capture is the
# raw inner HTML, collapsed and unescaped below. The class became
# ``pf-view__title`` in P-UX-A0b, when the section header became the
# view header — every section still emits one, hidden ones included.
_SECTION_H2_RE: re.Pattern[str] = re.compile(
    r'<h2 class="pf-view__title" id="([^"]+)-title">(.*?)</h2>',
    re.DOTALL,
)


def _rendered_section_headings(body: str) -> dict[str, str]:
    """Map section slug to the collapsed text of its rendered ``<h2>``."""
    return {
        slug: html.unescape(" ".join(inner.split())) for slug, inner in _SECTION_H2_RE.findall(body)
    }


@pytest.mark.parametrize("area_slug,url,section_slugs", _AREAS)
async def test_rendered_section_titles_match_catalogue(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    area_slug: str,
    url: str,
    section_slugs: tuple[str, ...],
) -> None:
    """Every rendered heading is the catalogue's title for that pair.

    Since P-UX-2 the body partials carry no ``section_title`` literal;
    the heading is looked up from ``web.shell`` at render time. This is
    the render-side half of that guarantee — the template-side half
    (no literal comes back) lives in
    tests/regression/test_section_catalogue_matches_body_partials.py.
    """
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get(url, follow_redirects=False)
    assert response.status_code == 200

    rendered = _rendered_section_headings(response.text)
    expected = {slug: section_title(area_slug, slug) for slug in section_slugs}
    assert rendered == expected, (
        f"{url}: rendered section headings disagree with the "
        f"web.shell catalogue. rendered={rendered} expected={expected}"
    )
