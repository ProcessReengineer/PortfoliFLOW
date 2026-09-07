# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the Transactions web surface — the ninth Area (ADR-0128 §7).

ASGI-level tests over a live Postgres, mirroring the fixture pattern in
``tests/web/test_cases_area.py`` (login helper, superuser-seeded
tenant/user, HTMX header simulation). They cover the Area *shell* — that it
renders, that its three Sections carry stable anchors, and that the sections
still waiting on a strand carry nothing clickable:

* Area/nav — ``/transactions`` renders the page and the HTMX branch the
  partial.
* Placeholders — the History body holds no control and no ``hx-``
  attribute, so nothing there links to a route P-5b has not built yet.
* Registry — the three Modules register into the Area and construct, which
  is the ``VALID_AREAS`` guard in ``core/base_module.py``.

Two sections **left the no-controls pin**, each when a strand filled it,
and each to a sharper statement than "no controls" ever was. The
New-transaction section left in S4a — it is the MD-1 flow chooser, pinned by
``tests/web/test_transactions_composer.py`` (five tiles, exactly one
HTMX-wired gesture, four inert ones). The Blotter left in P-5a — it is the
lazy shell over ``GET /api/transactions/blotter``, pinned by
``tests/web/test_transactions_blotter.py``. What remains here is History,
and the pin widens to nothing when P-5b lands.
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


def _section_markup(body: str, slug: str) -> str:
    """Slice one Section's markup out of a full response body.

    The OOB sidebar and the shell chrome around the Area legitimately carry
    links and forms, and since S4a so does the New-transaction section; only
    the named Section is in scope for the "no controls" pin. No Section
    nests another, so the slice runs from the opening tag to the first
    ``</section>`` after it.
    """
    start = body.index(f'<section class="pf-section" id="{slug}"')
    end = body.index("</section>", start) + len("</section>")
    return body[start:end]


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


async def test_transactions_placeholders_carry_no_controls(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The History placeholder holds nothing clickable.

    Pins the shell contract for the one section S5 still owes: no placeholder
    body links to a route that does not exist yet.

    **Narrowed twice.** The New-transaction section left in S4a, which filled
    it with the MD-1 chooser (its contract is ``test_transactions_composer``'s
    now — five tiles, exactly one HTMX-wired gesture). The Blotter left in
    P-5a, which replaced its sentence with the lazy shell; what that section
    may carry is pinned by ``test_transactions_blotter.py`` instead. The pin
    widens to nothing when P-5b fills History.
    """
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get("/transactions", follow_redirects=False)
    assert response.status_code == 200

    markup = _section_markup(response.text, "history")
    for token in ("<form", "<button", "<input", "<a "):
        assert token not in markup, f"history placeholder carries a control: {token!r}"
    assert re.search(r"\shx-[a-z-]+=", markup) is None, (
        "history placeholder carries an hx-* attribute"
    )


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
