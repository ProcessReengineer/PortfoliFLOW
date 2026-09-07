# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the Transactions web surface — the ninth Area (ADR-0128 §7).

ASGI-level tests over a live Postgres, mirroring the fixture pattern in
``tests/web/test_cases_area.py`` (login helper, superuser-seeded
tenant/user, HTMX header simulation). They cover the Area *shell* — that it
renders and that its three Sections carry stable anchors:

* Area/nav — ``/transactions`` renders the page and the HTMX branch the
  partial.
* Registry — the three Modules register into the Area and construct, which
  is the ``VALID_AREAS`` guard in ``core/base_module.py``.

**The no-controls pin is retired.** It guarded the sections still waiting on
a strand, and S5 filled the last two of them — so there is no placeholder
body left in this Area to hold nothing clickable. Each section left the pin
when a strand filled it, and each to a sharper statement than "no controls"
ever was: New transaction in S4a, to
``tests/web/test_transactions_composer.py`` (five tiles, exactly one
HTMX-wired gesture, four inert ones); the Blotter in P-5a and History in
P-5b, to ``tests/web/test_transactions_blotter.py`` and
``tests/web/test_transactions_history.py`` (a lazy shell each, over the list
endpoint behind it). The section-anchor pin below stays: it is about the
Area's shape, not about what any one section is waiting for.
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

import modules  # noqa: F401 — importing the package populates the ModuleRegistry
from core.config import get_config
from core.tenant_constants import SENTINEL_TENANT_ID
from modules.module_registry import registry
from services.password_hashing import hash_password
from web.main import create_app
from web.settings import WebSettings

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

#: The three Sections, in ``web.shell._SECTIONS_BY_AREA`` order.
_SECTIONS: tuple[tuple[str, str], ...] = (
    ("new", "New transaction"),
    ("blotter", "Blotter"),
    ("history", "History"),
)


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB Transactions tests.",
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


_TRUNCATE = text(
    "TRUNCATE TABLE login_audit, sessions, audit_log, "
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
    """Seed the primary tenant and its owner."""
    plaintext = "correct-horse-battery-staple"
    user_id = uuid4()
    email = "transactions-owner@example.com"
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


# ---------------------------------------------------------------------------
# Area / nav
# ---------------------------------------------------------------------------


async def test_transactions_page_renders_three_sections(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """``GET /transactions`` renders the area with its three Sections."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get("/transactions", follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    assert 'data-area="transactions"' in body
    assert "<html" in body.lower()
    for slug, title in _SECTIONS:
        assert f'id="{slug}"' in body, f'missing section anchor id="{slug}"'
        assert f'data-section="{slug}"' in body, f"missing section-indicator dot for {slug}"
        assert title in body, f"missing section title {title!r}"


async def test_transactions_htmx_branch_returns_partial(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """An HTMX swap returns the body partial plus the OOB sidebar."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get(
        "/transactions", headers={"HX-Request": "true"}, follow_redirects=False
    )
    assert response.status_code == 200
    body = response.text
    assert "<html" not in body.lower()
    assert 'hx-swap-oob="outerHTML"' in body
    assert 'data-area="transactions"' in body


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_transactions_modules_registered() -> None:
    """The Area's three Modules register and construct (the VALID_AREAS guard)."""
    classes = registry.list_by_area("transactions")
    assert {cls.module_name for cls in classes} == {"new", "blotter", "history"}
    assert len(classes) == 3

    config = get_config()
    for cls in classes:
        cls(config)
