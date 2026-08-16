# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the 6F-2 polish loop (Phase 6 Block 1).

Three small follow-ups from the 6F-2 browser walk:

* The sidebar renders single-letter glyphs (visible only when the
  sidebar is collapsed) instead of the legacy two-letter codes
  ``FO`` / ``BO`` / ``AD`` / ``IC`` / ``AS``.
* The ``.pf-sidebar__item`` rule no longer forces single-line
  truncation, so ``Investor Communication`` wraps to two lines
  instead of being cut off at the 200px sidebar width.
* The ``.pf-auth__brand-bar`` rule no longer paints a surface
  background and instead centres the auth-page logo horizontally
  over the sign-in card.

Tests 2 and 3 are CSS-content tests against the static-asset path,
mirroring the pattern in ``test_static_assets.py``. Test 1 drives
the live ASGI surface and asserts on rendered markup.
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

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_STATIC_DIR: Path = _REPO_ROOT / "web" / "static"

_AREAS: tuple[tuple[str, str, str], ...] = (
    ("front_office", "/front-office", "F"),
    ("back_office", "/back-office", "B"),
    ("watch_desk", "/watch-desk", "W"),
    ("cases", "/cases", "C"),
    ("planning_desk", "/planning-desk", "P"),
    ("investor_communication", "/investor-communication", "I"),
    ("assistants", "/assistants", "A"),
    ("admin", "/admin", "A"),
)

_LEGACY_CODES: tuple[str, ...] = ("FO", "BO", "AD", "IC", "AS")


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB sidebar-glyph polish tests.",
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
    email = "polish@example.com"
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


def _read_css(filename: str) -> str:
    return (_STATIC_DIR / "css" / filename).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Fix 1 — sidebar glyphs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug,url,glyph", _AREAS)
async def test_sidebar_uses_single_letter_glyphs(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    slug: str,
    url: str,
    glyph: str,
) -> None:
    """The sidebar emits ``pf-sidebar__glyph`` spans carrying the
    first letter of each area label. The legacy two-letter codes
    (``FO`` / ``BO`` / ``AD`` / ``IC`` / ``AS``) must not appear
    inside the glyph / icon spans any more.
    """
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get(url, follow_redirects=False)
    assert response.status_code == 200
    body = response.text

    # New class name is present in the rendered sidebar.
    assert 'class="pf-sidebar__glyph"' in body, f"{url} missing pf-sidebar__glyph span class"
    # Legacy class name has been retired.
    assert "pf-sidebar__icon" not in body, f"{url} still emits the legacy pf-sidebar__icon class"

    # Each glyph span contains a single letter — extract them all and
    # confirm the expected initials appear in render order. Eight areas since
    # ADR-0107 added Cases ("C"); the sidebar order is Front Office → Back
    # Office → Assistants → Planning Desk → Investor Communication → Watch
    # Desk → Cases → Admin (ADR-0122 §1, superseding the ADR-0104 §6 order).
    # Both Assistants and Admin render "A", hence the repeated initial.
    span_contents = re.findall(
        r'<span class="pf-sidebar__glyph"[^>]*>([^<]*)</span>',
        body,
    )
    assert span_contents == ["F", "B", "A", "P", "I", "W", "C", "A"], (
        f"{url} glyph contents were {span_contents!r}"
    )

    # Scoped negative assertion: the 200 characters immediately after
    # each pf-sidebar__glyph class occurrence must not contain any of
    # the legacy two-letter codes.
    for match in re.finditer(r'class="pf-sidebar__glyph"', body):
        window = body[match.end() : match.end() + 200]
        for code in _LEGACY_CODES:
            assert code not in window, (
                f"{url} leaks legacy code {code!r} inside a glyph span window: {window!r}"
            )


# ---------------------------------------------------------------------------
# Fix 2 — sidebar label no longer forces truncation
# ---------------------------------------------------------------------------


def test_sidebar_label_does_not_force_truncation() -> None:
    """The ``.pf-sidebar__item`` rule must allow labels to wrap.

    A 200px-wide sidebar truncates ``Investor Communication`` when
    the rule carries ``white-space: nowrap`` + ``overflow: hidden``
    + ``text-overflow: ellipsis``. The 6F-2 polish loop swaps that
    block out for a relaxed ``line-height: 1.25`` so the label can
    wrap onto a second line.
    """
    css = _read_css("layout.css")
    match = re.search(
        r"\.pf-sidebar__item\s*\{([^}]*)\}",
        css,
        flags=re.DOTALL,
    )
    assert match is not None, "layout.css is missing the .pf-sidebar__item rule"
    rule = match.group(1)

    assert "text-overflow: ellipsis" not in rule, (
        ".pf-sidebar__item must not force ellipsis truncation"
    )
    assert "white-space: nowrap" not in rule, (
        ".pf-sidebar__item must not pin labels to a single line"
    )
    assert "line-height: 1.25" in rule, (
        ".pf-sidebar__item must set line-height: 1.25 for wrapped labels"
    )


# ---------------------------------------------------------------------------
# Fix 3 — auth brand bar centres the logo and drops the surface bg
# ---------------------------------------------------------------------------


def test_auth_brand_bar_has_no_surface_background() -> None:
    """The login-page brand bar must not paint the surface colour
    and must centre its contents.

    The 6F-2 polish loop drops the bar background entirely so the
    logo sits on the deepest-page background, and switches the bar
    to a centred flex layout so the logo is centred above the
    sign-in card rather than left-aligned.
    """
    css = _read_css("layout.css")
    match = re.search(
        r"\.pf-auth__brand-bar\s*\{([^}]*)\}",
        css,
        flags=re.DOTALL,
    )
    assert match is not None, "layout.css is missing the .pf-auth__brand-bar rule"
    rule = match.group(1)

    assert "background-color: var(--pf-bg-surface)" not in rule, (
        ".pf-auth__brand-bar must not paint the surface background"
    )
    assert "justify-content: center" in rule, ".pf-auth__brand-bar must centre its contents"
