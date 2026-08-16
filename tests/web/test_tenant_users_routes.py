# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the tenant Users surface (ADR-0121 §6, strand U2).

Follows the idiom of ``test_provider_credentials.py`` — live Postgres,
real login flow, ``AsyncClient`` over ``ASGITransport`` — and asserts
against the rendered section plus the persisted ``users`` rows the
service wrote through RLS.

Seven groups:

* **Authz matrix** — an owner reads and writes every endpoint; a member
  is 403 on all of them *including the section GET*; a missing CSRF token
  is refused; an unauthenticated caller is redirected.
* **Create** — the happy path lists the new user; a duplicate email, a
  weak password and the un-offered ``auditor`` role are each refused at
  400 with mapped copy, and the role selector offers owner and member
  only.
* **Deactivate / reactivate** — a row moves between the two groups, self
  deactivation is refused, and sessions are ended by the service.
* **Reset password** — another user's reset banners inline; the caller's
  own answers with the login ``HX-Redirect`` and leaves the caller
  unauthenticated for the next request.
* **Change role** — member→owner promotes; demoting the last active owner
  is refused; self-demotion with a second owner answers with the
  ``/admin`` ``HX-Redirect``.
* **Banner copy** — the mapping table itself, including the one guard the
  route cannot reach (see :func:`test_last_owner_deactivation_copy_is_mapped`).
* **Admin shell** — the owner conditional decides whether the section
  markup is in the page at all.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack
from html import unescape
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.repositories._session import tenant_context
from core.repositories.user_repository import UserDTO, UserRepository
from core.tenant_constants import SENTINEL_TENANT_ID
from services.password_hashing import hash_password
from services.tenant_users import (
    MANAGEABLE_ROLES,
    CannotDeactivateLastOwnerError,
    CannotDeactivateSelfError,
    CannotDemoteLastOwnerError,
    EmailTakenError,
    UserNotFoundError,
)
from web.main import create_app
from web.routes.tenant_users import _ROLE_OPTIONS, _banner_for
from web.settings import WebSettings

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

_PASSWORD = "correct-horse-battery-staple"
#: Passes the policy (>= 12 chars, >= 2 character classes).
_NEW_PASSWORD = "Another-Horse-2026"
#: Fails it on length, so the policy message is what gets rendered.
_WEAK_PASSWORD = "short1"

_SECTION_URL = "/admin/users/section"
_CREATE_URL = "/admin/users/create"


def _deactivate_url(user_id: UUID) -> str:
    return f"/admin/users/{user_id}/deactivate"


def _reactivate_url(user_id: UUID) -> str:
    return f"/admin/users/{user_id}/reactivate"


def _reset_url(user_id: UUID) -> str:
    return f"/admin/users/{user_id}/reset-password"


def _role_url(user_id: UUID) -> str:
    return f"/admin/users/{user_id}/role"


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB tenant-users tests.",
            allow_module_level=False,
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fresh_superuser_engine() -> AsyncGenerator[AsyncEngine, None]:
    _require_db()
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def app_engine() -> AsyncGenerator[AsyncEngine, None]:
    """An RLS-subject engine for reading rows back the way the app writes them."""
    _require_db()
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def reset_schema(
    fresh_superuser_engine: AsyncEngine,
) -> AsyncGenerator[None, None]:
    truncate_sql = text(
        "TRUNCATE TABLE scoped_settings, data_upload_sheets, data_uploads, "
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
async def seeded_users(
    fresh_superuser_engine: AsyncEngine,
    reset_schema: None,
) -> dict[str, tuple[UUID, str]]:
    """Seed exactly one active owner and one member in the Sentinel tenant.

    One owner is deliberate: it is the state the last-active-owner guards
    protect, so a test reaches them without first dismantling a fixture.

    Returns:
        Role key → ``(user_id, email)``. The password is
        :data:`_PASSWORD` for both.
    """
    accounts: dict[str, tuple[UUID, str]] = {
        "owner": (uuid4(), "tu-owner@example.com"),
        "member": (uuid4(), "tu-member@example.com"),
    }
    roles = {"owner": "owner", "member": "member"}
    password_hash = hash_password(_PASSWORD)

    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) "
                "VALUES (:id, :name, 'minathena-capital') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": str(SENTINEL_TENANT_ID), "name": "Sentinel Tenant"},
        )
        for key, (user_id, email) in accounts.items():
            await conn.execute(
                text(
                    """
                    INSERT INTO users
                        (id, tenant_id, email, password_hash, roles,
                         display_name, is_active)
                    VALUES
                        (:id, :tid, :email, :hash, ARRAY[:role]::text[],
                         :display_name, TRUE)
                    """
                ),
                {
                    "id": str(user_id),
                    "tid": str(SENTINEL_TENANT_ID),
                    "email": email,
                    "hash": password_hash,
                    "role": roles[key],
                    "display_name": f"Test {key.title()}",
                },
            )
    return accounts


@pytest_asyncio.fixture
async def client_factory(seeded_users: dict[str, tuple[UUID, str]]):
    """Yield a factory returning a logged-in ``AsyncClient`` for one role."""
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

    async def _make(role: str | None = None, *, password: str = _PASSWORD) -> AsyncClient:
        app = create_app(settings)
        await stack.enter_async_context(app.router.lifespan_context(app))
        transport = ASGITransport(app=app)
        client = AsyncClient(transport=transport, base_url="http://testserver")
        await stack.enter_async_context(client)
        if role is not None:
            await _login(client, seeded_users[role][1], password=password)
        return client

    try:
        yield _make
    finally:
        await stack.aclose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _login(client: AsyncClient, email: str, *, password: str = _PASSWORD) -> None:
    get_response = await client.get("/login")
    pre_csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
    assert pre_csrf is not None
    response = await client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": pre_csrf},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303), response.text


async def _csrf_token(client: AsyncClient) -> str:
    """Read the session CSRF token out of the Admin page's meta tag.

    Deliberately not read from the Users section: a member has to be able
    to obtain a valid token too, so that the 403 their POST earns is
    unambiguously the role gate rather than a missing token.
    """
    response = await client.get("/admin", follow_redirects=False)
    assert response.status_code == 200, response.text
    match = re.search(r'name="csrf-token" content="([^"]+)"', response.text)
    assert match is not None, "the Admin page rendered no CSRF meta tag"
    return match.group(1)


def _visible_text(markup: str) -> str:
    """Return the rendered copy with every tag — and so every attribute — gone.

    Entities are unescaped as well, so a banner assertion can be written
    the way the copy is written: Jinja autoescaping turns the apostrophe
    in "the tenant's last active owner" into ``&#39;``, which is what the
    owner reads but not what the source says.
    """
    return unescape(re.sub(r"<[^>]+>", " ", markup))


def _group_of(html: str, email: str) -> str:
    """Return ``active`` / ``deactivated`` / ``absent`` for one row.

    The two groups render the same row markup, so membership is read from
    the row's own state chip rather than from its position in the
    document.
    """
    match = re.search(
        rf'data-user-email="{re.escape(email)}".*?pf-user-state--(active|deactivated)',
        html,
        re.DOTALL,
    )
    return match.group(1) if match else "absent"


async def _read_user(engine: AsyncEngine, user_id: UUID) -> UserDTO | None:
    async with tenant_context(engine, SENTINEL_TENANT_ID) as session:
        return await UserRepository(session).get_by_id(user_id)


async def _session_count(engine: AsyncEngine, user_id: UUID) -> int:
    async with tenant_context(engine, SENTINEL_TENANT_ID) as session:
        result = await session.execute(
            text("SELECT COUNT(*) FROM sessions WHERE user_id = :uid"),
            {"uid": str(user_id)},
        )
        return int(result.scalar_one())


# ---------------------------------------------------------------------------
# Authz matrix — the route is authoritative
# ---------------------------------------------------------------------------


async def test_owner_sees_the_user_list_grouped(
    client_factory: Any,
    seeded_users: dict[str, tuple[UUID, str]],
) -> None:
    client = await client_factory("owner")
    response = await client.get(_SECTION_URL, follow_redirects=False)

    assert response.status_code == 200
    body = response.text
    assert _group_of(body, seeded_users["owner"][1]) == "active"
    assert _group_of(body, seeded_users["member"][1]) == "active"
    # Both groups exist as concepts; only the populated one renders.
    assert "Active" in body
    assert "Deactivated" not in body
    # The caller's own row is marked, and the two self-targeting hints ride on it.
    assert "You will lose access to this area immediately." in body
    assert "You will be signed out immediately." in body


async def test_member_is_forbidden_on_the_section_get(
    client_factory: Any,
) -> None:
    """The gate is on the GET as much as on the writes (ADR-0121 §6)."""
    client = await client_factory("member")
    response = await client.get(_SECTION_URL, follow_redirects=False)

    assert response.status_code == 403
    assert "tu-owner@example.com" not in response.text


@pytest.mark.parametrize("target", ["create", "deactivate", "reactivate", "reset", "role"])
async def test_every_write_is_forbidden_for_a_member(
    client_factory: Any,
    app_engine: AsyncEngine,
    seeded_users: dict[str, tuple[UUID, str]],
    target: str,
) -> None:
    client = await client_factory("member")
    csrf = await _csrf_token(client)
    owner_id = seeded_users["owner"][0]
    urls = {
        "create": _CREATE_URL,
        "deactivate": _deactivate_url(owner_id),
        "reactivate": _reactivate_url(owner_id),
        "reset": _reset_url(owner_id),
        "role": _role_url(owner_id),
    }

    response = await client.post(
        urls[target],
        data={
            "csrf_token": csrf,
            "email": "tu-intruder@example.com",
            "password": _NEW_PASSWORD,
            "role": "owner",
            "new_role": "member",
            "new_password": _NEW_PASSWORD,
        },
        follow_redirects=False,
    )

    assert response.status_code == 403
    # Nothing moved: the owner is still an active owner, and no new row landed.
    owner = await _read_user(app_engine, owner_id)
    assert owner is not None and owner.is_active and "owner" in owner.roles


@pytest.mark.parametrize("target", ["create", "deactivate", "reactivate", "reset", "role"])
async def test_every_write_requires_csrf(
    client_factory: Any,
    app_engine: AsyncEngine,
    seeded_users: dict[str, tuple[UUID, str]],
    target: str,
) -> None:
    client = await client_factory("owner")
    member_id = seeded_users["member"][0]
    urls = {
        "create": _CREATE_URL,
        "deactivate": _deactivate_url(member_id),
        "reactivate": _reactivate_url(member_id),
        "reset": _reset_url(member_id),
        "role": _role_url(member_id),
    }

    response = await client.post(
        urls[target],
        data={
            "email": "tu-nocsrf@example.com",
            "password": _NEW_PASSWORD,
            "role": "member",
            "new_role": "owner",
            "new_password": _NEW_PASSWORD,
        },
        follow_redirects=False,
    )

    assert response.status_code == 403
    member = await _read_user(app_engine, member_id)
    assert member is not None and member.is_active and member.roles == ("member",)


async def test_unauthenticated_access_redirects_to_login(
    client_factory: Any,
) -> None:
    client = await client_factory(None)

    section = await client.get(_SECTION_URL, follow_redirects=False)
    assert section.status_code == 303
    assert section.headers["location"] == "/login"

    write = await client.post(
        _CREATE_URL,
        data={"csrf_token": "x", "email": "a@b.example", "password": _NEW_PASSWORD},
        follow_redirects=False,
    )
    assert write.status_code == 303


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_create_lists_the_new_user_with_a_success_banner(
    client_factory: Any,
    app_engine: AsyncEngine,
) -> None:
    client = await client_factory("owner")
    csrf = await _csrf_token(client)

    response = await client.post(
        _CREATE_URL,
        data={
            "csrf_token": csrf,
            "email": "tu-new@example.com",
            "display_name": "New Person",
            "password": _NEW_PASSWORD,
            "role": "member",
        },
        follow_redirects=False,
    )

    assert response.status_code == 200, response.text
    body = response.text
    assert "tu-new@example.com added as member." in _visible_text(body)
    assert _group_of(body, "tu-new@example.com") == "active"
    assert "New Person" in body
    # The password is never echoed back into the list it just created.
    assert _NEW_PASSWORD not in body

    async with tenant_context(app_engine, SENTINEL_TENANT_ID) as session:
        created = await UserRepository(session).get_by_email("tu-new@example.com")
    assert created is not None
    assert created.roles == ("member",)
    assert created.is_active is True


async def test_create_with_a_duplicate_email_is_refused_inline(
    client_factory: Any,
    seeded_users: dict[str, tuple[UUID, str]],
) -> None:
    client = await client_factory("owner")
    csrf = await _csrf_token(client)

    response = await client.post(
        _CREATE_URL,
        data={
            "csrf_token": csrf,
            "email": seeded_users["member"][1],
            "password": _NEW_PASSWORD,
            "role": "member",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "That email already has an account in this tenant." in _visible_text(response.text)
    # The rejection re-renders the same body, so the list is still there.
    assert _group_of(response.text, seeded_users["member"][1]) == "active"


async def test_create_with_a_weak_password_renders_the_policy_message(
    client_factory: Any,
    app_engine: AsyncEngine,
) -> None:
    """The one service message surfaced verbatim — it states the rule."""
    client = await client_factory("owner")
    csrf = await _csrf_token(client)

    response = await client.post(
        _CREATE_URL,
        data={
            "csrf_token": csrf,
            "email": "tu-weak@example.com",
            "password": _WEAK_PASSWORD,
            "role": "member",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "at least 12 characters" in _visible_text(response.text)
    assert _WEAK_PASSWORD not in response.text

    async with tenant_context(app_engine, SENTINEL_TENANT_ID) as session:
        assert await UserRepository(session).get_by_email("tu-weak@example.com") is None


@pytest.mark.parametrize("bad_role", ["auditor", "", "wizard"])
async def test_create_refuses_a_role_the_surface_does_not_offer(
    client_factory: Any,
    app_engine: AsyncEngine,
    bad_role: str,
) -> None:
    client = await client_factory("owner")
    csrf = await _csrf_token(client)

    response = await client.post(
        _CREATE_URL,
        data={
            "csrf_token": csrf,
            "email": "tu-auditor@example.com",
            "password": _NEW_PASSWORD,
            "role": bad_role,
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "Choose a role: owner or member." in _visible_text(response.text)

    async with tenant_context(app_engine, SENTINEL_TENANT_ID) as session:
        assert await UserRepository(session).get_by_email("tu-auditor@example.com") is None


async def test_create_with_an_invalid_email_is_refused_inline(
    client_factory: Any,
) -> None:
    client = await client_factory("owner")
    csrf = await _csrf_token(client)

    response = await client.post(
        _CREATE_URL,
        data={
            "csrf_token": csrf,
            "email": "not-an-email",
            "password": _NEW_PASSWORD,
            "role": "member",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "That is not a valid email address." in _visible_text(response.text)


async def test_the_role_selector_offers_owner_and_member_only() -> None:
    """ADR-0121 §6: ``auditor`` gates nothing, so it is not offered."""
    assert {value for value, _label in _ROLE_OPTIONS} == set(MANAGEABLE_ROLES)


async def test_the_section_never_names_the_dormant_role(
    client_factory: Any,
) -> None:
    client = await client_factory("owner")
    response = await client.get(_SECTION_URL, follow_redirects=False)

    assert response.status_code == 200
    assert "auditor" not in response.text.lower()


# ---------------------------------------------------------------------------
# Deactivate / reactivate
# ---------------------------------------------------------------------------


async def test_deactivating_a_user_moves_the_row_and_ends_their_sessions(
    client_factory: Any,
    app_engine: AsyncEngine,
    seeded_users: dict[str, tuple[UUID, str]],
) -> None:
    member_id, member_email = seeded_users["member"]
    # Give the member a live session, so the ADR-0121 §4.5 sweep has
    # something to sweep.
    await client_factory("member")
    assert await _session_count(app_engine, member_id) == 1

    owner_client = await client_factory("owner")
    csrf = await _csrf_token(owner_client)

    response = await owner_client.post(
        _deactivate_url(member_id),
        data={"csrf_token": csrf},
        follow_redirects=False,
    )

    assert response.status_code == 200, response.text
    body = response.text
    assert f"{member_email} deactivated." in _visible_text(body)
    assert _group_of(body, member_email) == "deactivated"

    member = await _read_user(app_engine, member_id)
    assert member is not None and member.is_active is False
    assert await _session_count(app_engine, member_id) == 0


async def test_deactivating_yourself_is_refused_inline(
    client_factory: Any,
    app_engine: AsyncEngine,
    seeded_users: dict[str, tuple[UUID, str]],
) -> None:
    owner_id = seeded_users["owner"][0]
    client = await client_factory("owner")
    csrf = await _csrf_token(client)

    response = await client.post(
        _deactivate_url(owner_id),
        data={"csrf_token": csrf},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "You cannot deactivate your own account." in _visible_text(response.text)
    owner = await _read_user(app_engine, owner_id)
    assert owner is not None and owner.is_active is True


async def test_reactivating_returns_the_row_to_the_active_group(
    client_factory: Any,
    app_engine: AsyncEngine,
    seeded_users: dict[str, tuple[UUID, str]],
) -> None:
    member_id, member_email = seeded_users["member"]
    client = await client_factory("owner")
    csrf = await _csrf_token(client)

    await client.post(_deactivate_url(member_id), data={"csrf_token": csrf}, follow_redirects=False)
    response = await client.post(
        _reactivate_url(member_id), data={"csrf_token": csrf}, follow_redirects=False
    )

    assert response.status_code == 200, response.text
    assert f"{member_email} reactivated." in _visible_text(response.text)
    assert _group_of(response.text, member_email) == "active"

    member = await _read_user(app_engine, member_id)
    assert member is not None and member.is_active is True
    # Reactivation restores access, never a session.
    assert await _session_count(app_engine, member_id) == 0


async def test_acting_on_an_unknown_user_is_refused_inline(
    client_factory: Any,
) -> None:
    client = await client_factory("owner")
    csrf = await _csrf_token(client)

    response = await client.post(
        _reactivate_url(uuid4()), data={"csrf_token": csrf}, follow_redirects=False
    )

    assert response.status_code == 400
    assert "That user is no longer in this tenant." in _visible_text(response.text)


# ---------------------------------------------------------------------------
# Reset password
# ---------------------------------------------------------------------------


async def test_resetting_another_users_password_banners_inline(
    client_factory: Any,
    app_engine: AsyncEngine,
    seeded_users: dict[str, tuple[UUID, str]],
) -> None:
    member_id, member_email = seeded_users["member"]
    await client_factory("member")
    assert await _session_count(app_engine, member_id) == 1

    owner_client = await client_factory("owner")
    csrf = await _csrf_token(owner_client)

    response = await owner_client.post(
        _reset_url(member_id),
        data={"csrf_token": csrf, "new_password": _NEW_PASSWORD},
        follow_redirects=False,
    )

    assert response.status_code == 200, response.text
    assert f"Password reset for {member_email}." in _visible_text(response.text)
    assert "HX-Redirect" not in response.headers
    assert _NEW_PASSWORD not in response.text
    assert await _session_count(app_engine, member_id) == 0

    # The new credential is the one that works now.
    reborn = await client_factory(None)
    await _login(reborn, member_email, password=_NEW_PASSWORD)


async def test_a_weak_reset_is_refused_inline(
    client_factory: Any,
    seeded_users: dict[str, tuple[UUID, str]],
) -> None:
    client = await client_factory("owner")
    csrf = await _csrf_token(client)

    response = await client.post(
        _reset_url(seeded_users["member"][0]),
        data={"csrf_token": csrf, "new_password": _WEAK_PASSWORD},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "at least 12 characters" in _visible_text(response.text)


async def test_resetting_your_own_password_redirects_to_login_and_signs_you_out(
    client_factory: Any,
    seeded_users: dict[str, tuple[UUID, str]],
) -> None:
    """The swap would land on a dead cookie, so the browser is sent away."""
    owner_id = seeded_users["owner"][0]
    client = await client_factory("owner")
    csrf = await _csrf_token(client)

    response = await client.post(
        _reset_url(owner_id),
        data={"csrf_token": csrf, "new_password": _NEW_PASSWORD},
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == "/login"
    assert response.text == ""

    # The caller's own session went with the reset.
    follow_up = await client.get(_SECTION_URL, follow_redirects=False)
    assert follow_up.status_code == 303
    assert follow_up.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# Change role
# ---------------------------------------------------------------------------


async def test_promoting_a_member_to_owner_succeeds(
    client_factory: Any,
    app_engine: AsyncEngine,
    seeded_users: dict[str, tuple[UUID, str]],
) -> None:
    member_id, member_email = seeded_users["member"]
    client = await client_factory("owner")
    csrf = await _csrf_token(client)

    response = await client.post(
        _role_url(member_id),
        data={"csrf_token": csrf, "new_role": "owner"},
        follow_redirects=False,
    )

    assert response.status_code == 200, response.text
    assert f"{member_email} is now owner." in _visible_text(response.text)
    member = await _read_user(app_engine, member_id)
    assert member is not None and member.roles == ("owner",)


async def test_demoting_the_last_active_owner_is_refused_inline(
    client_factory: Any,
    app_engine: AsyncEngine,
    seeded_users: dict[str, tuple[UUID, str]],
) -> None:
    """Reachable only as *self*-demotion: the actor is the owner it counts."""
    owner_id = seeded_users["owner"][0]
    client = await client_factory("owner")
    csrf = await _csrf_token(client)

    response = await client.post(
        _role_url(owner_id),
        data={"csrf_token": csrf, "new_role": "member"},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "This is the tenant's last active owner." in _visible_text(response.text)
    owner = await _read_user(app_engine, owner_id)
    assert owner is not None and owner.roles == ("owner",)


async def test_self_demotion_with_a_second_owner_redirects_to_admin(
    client_factory: Any,
    app_engine: AsyncEngine,
    seeded_users: dict[str, tuple[UUID, str]],
) -> None:
    """The hand-over case: the session stays valid, the gate no longer opens."""
    owner_id = seeded_users["owner"][0]
    member_id = seeded_users["member"][0]
    client = await client_factory("owner")
    csrf = await _csrf_token(client)

    promoted = await client.post(
        _role_url(member_id),
        data={"csrf_token": csrf, "new_role": "owner"},
        follow_redirects=False,
    )
    assert promoted.status_code == 200, promoted.text

    response = await client.post(
        _role_url(owner_id),
        data={"csrf_token": csrf, "new_role": "member"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == "/admin"
    assert response.text == ""

    owner = await _read_user(app_engine, owner_id)
    assert owner is not None and owner.roles == ("member",)
    # The session is untouched; it simply no longer passes the owner gate.
    assert await _session_count(app_engine, owner_id) == 1
    follow_up = await client.get(_SECTION_URL, follow_redirects=False)
    assert follow_up.status_code == 403


async def test_a_self_targeting_no_op_role_change_re_renders_in_place(
    client_factory: Any,
    seeded_users: dict[str, tuple[UUID, str]],
) -> None:
    """owner→owner on your own row is a write, not a hand-over."""
    owner_id, owner_email = seeded_users["owner"]
    client = await client_factory("owner")
    csrf = await _csrf_token(client)

    response = await client.post(
        _role_url(owner_id),
        data={"csrf_token": csrf, "new_role": "owner"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "HX-Redirect" not in response.headers
    assert f"{owner_email} is now owner." in _visible_text(response.text)


# ---------------------------------------------------------------------------
# Banner copy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (EmailTakenError("email x already exists"), "That email already has an account"),
        (UserNotFoundError("no user with id …"), "That user is no longer in this tenant."),
        (CannotDeactivateSelfError("…"), "You cannot deactivate your own account."),
        (CannotDemoteLastOwnerError("…"), "This is the tenant's last active owner."),
    ],
)
def test_banner_copy_never_leaks_a_service_message(exc: Exception, expected: str) -> None:
    banner = _banner_for(exc)
    assert expected in banner
    assert str(exc) not in banner


def test_last_owner_deactivation_copy_is_mapped() -> None:
    """The one guard the *route* cannot reach, pinned at the mapping.

    ``CannotDeactivateLastOwnerError`` needs a target who is the tenant's
    last active owner. Through this surface the actor is always an active
    owner (``require_role('owner')`` plus the active check in
    ``get_authenticated_user``), and self-deactivation is refused before
    the count is taken — so any *other* owner the actor could target
    leaves the count at two. The guard therefore only fires for a caller
    the routes do not have; it is exercised end to end in
    ``tests/services/tenant_users/test_service.py``. What U2 owes it is
    the copy, and that is what this pins.
    """
    assert "last active owner" in _banner_for(CannotDeactivateLastOwnerError("…"))


# ---------------------------------------------------------------------------
# Admin shell — the cosmetic owner conditional
# ---------------------------------------------------------------------------


async def test_the_admin_page_carries_the_users_section_for_an_owner(
    client_factory: Any,
) -> None:
    client = await client_factory("owner")
    response = await client.get("/admin", follow_redirects=False)

    assert response.status_code == 200
    body = response.text
    assert 'id="users"' in body
    assert 'hx-get="/admin/users/section"' in body


async def test_the_admin_page_omits_the_users_section_for_a_member(
    client_factory: Any,
) -> None:
    """Cosmetic mirroring of a gate the route enforces on its own."""
    client = await client_factory("member")
    response = await client.get("/admin", follow_redirects=False)

    assert response.status_code == 200
    body = response.text
    assert 'id="users"' not in body
    assert "/admin/users/section" not in body
    # The sections a member does get are untouched by the conditional.
    assert 'id="providers-credentials"' in body
