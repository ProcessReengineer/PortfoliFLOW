# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Route-level coverage for the super-admin / platform-admin surface.

Per ADR-0064 §1. Exercises the eight ``/super-admin/*`` endpoints
plus the cross-cutting invariants:

- ``require_super_admin`` rejects non-super-admin sessions.
- Business-rule guards (system tenant, primary tenant, last
  super-admin, reserved subdomain) re-render the page with an
  error banner.
- Audit rows are written on success.
- Owner password reset invalidates the owner's sessions.
- A logged-in super-admin browsing tenant-area URLs does not leak
  tenant data (defence-in-depth complement to the import-AST
  regression test).

Tests run against the live compose Postgres. The ``reset_schema``
autouse fixture from ``tests/_db_fixtures`` truncates every tenant
table before and after each test so no fixture data leaks across.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import cast
from uuid import UUID

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.testclient import TestClient

from core.tenant_constants import PRIMARY_TENANT_ID, SYSTEM_TENANT_ID
from services.password_hashing import hash_password
from web.tick_scheduler import TickSchedulerHandle, TickSchedulerStatus

from tests._db_fixtures import (  # noqa: F401  -- fixture re-export
    app_engine,
    reset_schema,
    seed_tenant,
    superuser_engine,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SUPER_ADMIN_EMAIL: str = "super@platform.example"
_SUPER_ADMIN_PASSWORD: str = "correct-horse-battery-staple"

_TENANT_OWNER_EMAIL: str = "owner@primary.example"
_TENANT_OWNER_PASSWORD: str = "tenant-owner-pw"

_PRIMARY_SUBDOMAIN: str = "minathena-capital"
_SYSTEM_SUBDOMAIN: str = "admin"


# ---------------------------------------------------------------------------
# Seeding fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def _seed_baseline(superuser_engine: AsyncEngine, reset_schema):
    """Seed system tenant + super-admin + primary tenant + owner.

    Every test in this module needs both surfaces present: the
    system tenant for super-admin login, the primary tenant for the
    cross-tenant guard and the password-reset scenario.

    Depends on ``reset_schema`` so the seeding runs **after** the
    autouse TRUNCATE; otherwise the truncate would wipe the seed
    rows before the test sees them.
    """
    del reset_schema  # depended-on for ordering, value unused
    hashed_admin = hash_password(_SUPER_ADMIN_PASSWORD)
    hashed_owner = hash_password(_TENANT_OWNER_PASSWORD)
    async with superuser_engine.begin() as conn:
        for tid, name, subdomain in (
            (SYSTEM_TENANT_ID, "Platform Administration", _SYSTEM_SUBDOMAIN),
            (PRIMARY_TENANT_ID, "Minathena Capital", _PRIMARY_SUBDOMAIN),
        ):
            await conn.execute(
                text(
                    "INSERT INTO tenants (id, name, subdomain) "
                    "VALUES (:id, :name, :subdomain) "
                    "ON CONFLICT (id) DO UPDATE "
                    "SET subdomain = EXCLUDED.subdomain, "
                    "    name = EXCLUDED.name, "
                    "    is_active = TRUE"
                ),
                {"id": str(tid), "name": name, "subdomain": subdomain},
            )

        # Super-admin in the system tenant.
        await conn.execute(
            text(
                "INSERT INTO users "
                "(tenant_id, email, password_hash, roles, "
                " is_super_admin, is_active) "
                "VALUES (:tid, :email, :hash, "
                "ARRAY['owner']::text[], TRUE, TRUE)"
            ),
            {
                "tid": str(SYSTEM_TENANT_ID),
                "email": _SUPER_ADMIN_EMAIL,
                "hash": hashed_admin,
            },
        )

        # Primary tenant owner — for the password-reset / cross-tenant
        # scenarios.
        await conn.execute(
            text(
                "INSERT INTO users "
                "(tenant_id, email, password_hash, roles, is_active) "
                "VALUES (:tid, :email, :hash, "
                "ARRAY['owner']::text[], TRUE)"
            ),
            {
                "tid": str(PRIMARY_TENANT_ID),
                "email": _TENANT_OWNER_EMAIL,
                "hash": hashed_owner,
            },
        )
    yield


# ---------------------------------------------------------------------------
# Client helpers
# ---------------------------------------------------------------------------


_SUPER_ADMIN_SESSION_TOKEN: str = "test-super-admin-session-token"
_SUPER_ADMIN_CSRF_TOKEN: str = "test-super-admin-csrf-token"
_TENANT_OWNER_SESSION_TOKEN: str = "test-tenant-owner-session-token"
_TENANT_OWNER_CSRF_TOKEN: str = "test-tenant-owner-csrf-token"


@pytest_asyncio.fixture
async def super_admin_client(monkeypatch, superuser_engine: AsyncEngine, _seed_baseline: None):
    """TestClient pre-authenticated as the platform super-admin.

    We bypass the HTTP login flow and directly INSERT the session
    row, because Starlette's TestClient reports ``client.host =
    'testclient'`` which the production auth backend tries to
    cast to INET (login_audit), failing in test. Hand-crafting the
    session avoids that path entirely while still exercising the
    same session-cookie / session-lookup semantics the routes use
    in production.
    """
    del _seed_baseline  # depended-on for ordering
    monkeypatch.setenv("LOCAL_DEV_TENANT_SUBDOMAIN", _SYSTEM_SUBDOMAIN)

    # Look up the super-admin id, then insert the session row.
    async with superuser_engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT id FROM users WHERE email = :e AND tenant_id = :tid"),
                {
                    "e": _SUPER_ADMIN_EMAIL,
                    "tid": str(SYSTEM_TENANT_ID),
                },
            )
        ).first()
        assert row is not None, "super-admin seed did not land"
        await conn.execute(
            text(
                "INSERT INTO sessions "
                "(tenant_id, user_id, session_token, csrf_token, "
                " created_at, last_seen_at, expires_at) "
                "VALUES (:tid, :uid, :tok, :csrf, "
                " NOW(), NOW(), NOW() + INTERVAL '8 hours')"
            ),
            {
                "tid": str(SYSTEM_TENANT_ID),
                "uid": str(row.id),
                "tok": _SUPER_ADMIN_SESSION_TOKEN,
                "csrf": _SUPER_ADMIN_CSRF_TOKEN,
            },
        )

    from web.main import create_app

    app = create_app()
    with TestClient(app) as client:
        client.cookies.set(
            "portfoliflow_session",
            _SUPER_ADMIN_SESSION_TOKEN,
        )
        yield client


@pytest_asyncio.fixture
async def tenant_owner_client(monkeypatch, superuser_engine: AsyncEngine, _seed_baseline: None):
    """TestClient pre-authenticated as the primary-tenant owner."""
    del _seed_baseline
    monkeypatch.setenv("LOCAL_DEV_TENANT_SUBDOMAIN", _PRIMARY_SUBDOMAIN)

    async with superuser_engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT id FROM users WHERE email = :e AND tenant_id = :tid"),
                {
                    "e": _TENANT_OWNER_EMAIL,
                    "tid": str(PRIMARY_TENANT_ID),
                },
            )
        ).first()
        assert row is not None
        await conn.execute(
            text(
                "INSERT INTO sessions "
                "(tenant_id, user_id, session_token, csrf_token, "
                " created_at, last_seen_at, expires_at) "
                "VALUES (:tid, :uid, :tok, :csrf, "
                " NOW(), NOW(), NOW() + INTERVAL '8 hours')"
            ),
            {
                "tid": str(PRIMARY_TENANT_ID),
                "uid": str(row.id),
                "tok": _TENANT_OWNER_SESSION_TOKEN,
                "csrf": _TENANT_OWNER_CSRF_TOKEN,
            },
        )

    from web.main import create_app

    app = create_app()
    with TestClient(app) as client:
        client.cookies.set(
            "portfoliflow_session",
            _TENANT_OWNER_SESSION_TOKEN,
        )
        yield client


def _csrf_for_super_admin() -> str:
    return _SUPER_ADMIN_CSRF_TOKEN


def _csrf_for_tenant_owner() -> str:
    return _TENANT_OWNER_CSRF_TOKEN


# ---------------------------------------------------------------------------
# GET /super-admin/tenants
# ---------------------------------------------------------------------------


def test_get_tenants_renders_for_super_admin(
    super_admin_client: TestClient,
) -> None:
    response = super_admin_client.get("/super-admin/tenants", headers={"host": "localhost"})
    assert response.status_code == 200, response.text
    # Primary tenant should be present (rendered as <code>subdomain</code>).
    assert f"<code>{_PRIMARY_SUBDOMAIN}</code>".encode() in response.content
    # System tenant must not appear in the default listing
    # (include_system=False). Its UUID would only appear in the
    # action-URL of a tenant row, never elsewhere on the page.
    assert str(SYSTEM_TENANT_ID).encode() not in response.content


def test_get_tenants_rejects_non_super_admin(
    tenant_owner_client: TestClient,
) -> None:
    response = tenant_owner_client.get("/super-admin/tenants", headers={"host": "localhost"})
    assert response.status_code == 403, response.text


def test_get_tenants_rejects_unauthenticated(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_DEV_TENANT_SUBDOMAIN", _SYSTEM_SUBDOMAIN)
    from web.main import create_app

    app = create_app()
    with TestClient(app) as client:
        response = client.get(
            "/super-admin/tenants",
            headers={"host": "localhost"},
            follow_redirects=False,
        )
        # Unauthenticated → 303 to /login per require_session.
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# POST /super-admin/tenants
# ---------------------------------------------------------------------------


def test_post_tenants_creates_tenant(
    super_admin_client: TestClient, superuser_engine: AsyncEngine
) -> None:
    csrf = _csrf_for_super_admin()
    response = super_admin_client.post(
        "/super-admin/tenants",
        headers={"host": "localhost"},
        data={
            "name": "Test Customer",
            "subdomain": "testcust",
            "owner_email": "owner@testcust.example",
            "owner_password": "correct-horse-battery-staple",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 200, response.text
    assert b"testcust" in response.content

    # Verify audit row + tenant row landed in DB.
    import asyncio

    async def _verify() -> None:
        async with superuser_engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT id FROM tenants WHERE subdomain = :s"),
                    {"s": "testcust"},
                )
            ).first()
            assert row is not None
            audit = (
                await conn.execute(
                    text(
                        "SELECT COUNT(*)::int AS n "
                        "FROM super_admin_audit "
                        "WHERE action = 'create_tenant' "
                        "  AND payload->>'subdomain' = 'testcust'"
                    )
                )
            ).scalar_one()
            assert int(audit) == 1

    asyncio.run(_verify())


def test_post_tenants_rejects_reserved_subdomain(
    super_admin_client: TestClient,
) -> None:
    csrf = _csrf_for_super_admin()
    response = super_admin_client.post(
        "/super-admin/tenants",
        headers={"host": "localhost"},
        data={
            "name": "Should Fail",
            "subdomain": "admin",
            "owner_email": "x@y.example",
            "owner_password": "pwpwpwpwpwpw",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert b"reserved" in response.content.lower()


def test_post_tenants_rejects_bad_csrf(
    super_admin_client: TestClient,
) -> None:
    response = super_admin_client.post(
        "/super-admin/tenants",
        headers={"host": "localhost"},
        data={
            "name": "X",
            "subdomain": "csrf",
            "owner_email": "x@y.example",
            "owner_password": "pwpwpwpwpwpw",
            "csrf_token": "wrong",
        },
        follow_redirects=False,
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Deactivate / reactivate guard rails
# ---------------------------------------------------------------------------


def test_deactivate_tenant_refuses_primary_tenant(
    super_admin_client: TestClient,
) -> None:
    csrf = _csrf_for_super_admin()
    response = super_admin_client.post(
        f"/super-admin/tenants/{PRIMARY_TENANT_ID}/deactivate",
        headers={"host": "localhost"},
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert b"primary" in response.content.lower()


def test_deactivate_tenant_refuses_system_tenant(
    super_admin_client: TestClient,
) -> None:
    csrf = _csrf_for_super_admin()
    response = super_admin_client.post(
        f"/super-admin/tenants/{SYSTEM_TENANT_ID}/deactivate",
        headers={"host": "localhost"},
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert b"system" in response.content.lower()


def test_deactivate_then_reactivate_arbitrary_tenant(
    super_admin_client: TestClient,
    superuser_engine: AsyncEngine,
    seed_tenant,
) -> None:
    """Round-trip a non-protected tenant through deactivate/reactivate."""
    import asyncio

    async def _seed_with_users() -> UUID:
        tid = await seed_tenant(name="Disposable", subdomain="disposable")
        return tid

    tid = asyncio.run(_seed_with_users())
    csrf = _csrf_for_super_admin()

    response = super_admin_client.post(
        f"/super-admin/tenants/{tid}/deactivate",
        headers={"host": "localhost"},
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 200

    response = super_admin_client.post(
        f"/super-admin/tenants/{tid}/reactivate",
        headers={"host": "localhost"},
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Reset owner password
# ---------------------------------------------------------------------------


def test_reset_owner_password_invalidates_owner_sessions(
    super_admin_client: TestClient, superuser_engine: AsyncEngine
) -> None:
    """After reset, the owner's session row is gone."""
    import asyncio

    # Seed a session for the primary-tenant owner.
    async def _seed_session() -> UUID:
        async with superuser_engine.begin() as conn:
            row = (
                await conn.execute(
                    text("SELECT id FROM users WHERE email = :e AND tenant_id = :tid"),
                    {
                        "e": _TENANT_OWNER_EMAIL,
                        "tid": str(PRIMARY_TENANT_ID),
                    },
                )
            ).first()
            owner_id = UUID(str(row.id))
            await conn.execute(
                text(
                    "INSERT INTO sessions "
                    "(tenant_id, user_id, session_token, csrf_token, "
                    " created_at, last_seen_at, expires_at) "
                    "VALUES (:tid, :uid, :tok, :csrf, "
                    " NOW(), NOW(), NOW() + INTERVAL '8 hours')"
                ),
                {
                    "tid": str(PRIMARY_TENANT_ID),
                    "uid": str(owner_id),
                    "tok": "owner-session-token",
                    "csrf": "owner-csrf-token",
                },
            )
            return owner_id

    owner_id = asyncio.run(_seed_session())

    csrf = _csrf_for_super_admin()
    response = super_admin_client.post(
        f"/super-admin/tenants/{PRIMARY_TENANT_ID}/reset-owner",
        headers={"host": "localhost"},
        data={
            "new_password": "rotated-password-2026",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 200, response.text

    # Owner's session must be gone.
    async def _verify() -> int:
        async with superuser_engine.connect() as conn:
            n = (
                await conn.execute(
                    text("SELECT COUNT(*)::int AS n FROM sessions WHERE user_id = :uid"),
                    {"uid": str(owner_id)},
                )
            ).scalar_one()
            return int(n)

    assert asyncio.run(_verify()) == 0


# ---------------------------------------------------------------------------
# Super-admin user endpoints
# ---------------------------------------------------------------------------


def test_get_users_lists_super_admins(
    super_admin_client: TestClient,
) -> None:
    response = super_admin_client.get("/super-admin/users", headers={"host": "localhost"})
    assert response.status_code == 200
    assert _SUPER_ADMIN_EMAIL.encode() in response.content


def test_post_users_creates_super_admin(
    super_admin_client: TestClient, superuser_engine: AsyncEngine
) -> None:
    csrf = _csrf_for_super_admin()
    response = super_admin_client.post(
        "/super-admin/users",
        headers={"host": "localhost"},
        data={
            "email": "second@platform.example",
            "password": "another-strong-password",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert b"second@platform.example" in response.content


def test_deactivate_super_admin_refuses_last(
    super_admin_client: TestClient, superuser_engine: AsyncEngine
) -> None:
    """Cannot deactivate the only remaining super-admin."""
    import asyncio

    async def _resolve_self_id() -> UUID:
        async with superuser_engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT id FROM users WHERE email = :e"),
                    {"e": _SUPER_ADMIN_EMAIL},
                )
            ).first()
            return UUID(str(row.id))

    self_id = asyncio.run(_resolve_self_id())
    csrf = _csrf_for_super_admin()

    response = super_admin_client.post(
        f"/super-admin/users/{self_id}/deactivate",
        headers={"host": "localhost"},
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.content.lower()
    assert b"last" in body or b"only" in body


def test_deactivate_super_admin_succeeds_with_another_active(
    super_admin_client: TestClient, superuser_engine: AsyncEngine
) -> None:
    """Add a second super-admin, then deactivate them."""
    import asyncio

    async def _seed_second() -> UUID:
        async with superuser_engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "INSERT INTO users "
                        "(tenant_id, email, password_hash, roles, "
                        " is_super_admin, is_active) "
                        "VALUES (:tid, :email, :hash, "
                        "ARRAY['owner']::text[], TRUE, TRUE) "
                        "RETURNING id"
                    ),
                    {
                        "tid": str(SYSTEM_TENANT_ID),
                        "email": "second@platform.example",
                        "hash": hash_password("dontmatter"),
                    },
                )
            ).first()
            return UUID(str(row.id))

    second_id = asyncio.run(_seed_second())
    csrf = _csrf_for_super_admin()

    response = super_admin_client.post(
        f"/super-admin/users/{second_id}/deactivate",
        headers={"host": "localhost"},
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Platform status card (ADR-0117 §5)
# ---------------------------------------------------------------------------


class _FakeSchedulerTask:
    """Stand-in for the scheduler's task — the view reads ``done()`` only.

    The package's autouse fixture keeps the built-in scheduler off, and
    these tests want its *reporting*, not its loop (that lives in
    ``tests/web/test_tick_scheduler.py``). A real ``asyncio.Task`` would
    also need a running loop, which the synchronous ``TestClient`` does
    not offer here.
    """

    def __init__(self, *, done: bool) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


def _present_internal_scheduler(
    client: TestClient,
    *,
    alive: bool = True,
    last_tick_at: datetime | None = None,
    interval_seconds: int = 60,
) -> None:
    """Make the running app look like it hosts the built-in scheduler."""
    client.app.state.settings.tick_scheduler_enabled = True  # type: ignore[attr-defined]
    client.app.state.tick_scheduler = TickSchedulerHandle(  # type: ignore[attr-defined]
        task=cast("asyncio.Task[None]", _FakeSchedulerTask(done=not alive)),
        stop_event=asyncio.Event(),
        interval_seconds=interval_seconds,
        status=TickSchedulerStatus(last_tick_at=last_tick_at),
    )


def test_platform_card_reports_the_built_in_scheduler(
    super_admin_client: TestClient,
) -> None:
    """Internal mode: source, heartbeat, last completed tick, interval."""
    _present_internal_scheduler(
        super_admin_client,
        last_tick_at=datetime(2026, 8, 11, 9, 14, tzinfo=timezone.utc),
    )
    response = super_admin_client.get("/super-admin/tenants", headers={"host": "localhost"})

    assert response.status_code == 200, response.text
    body = response.text
    assert "Built-in scheduler" in body
    assert "Running" in body
    assert "2026-08-11 09:14 UTC" in body
    assert "Every 60 seconds" in body


def test_platform_card_says_no_tick_yet_before_the_first_one(
    super_admin_client: TestClient,
) -> None:
    """A freshly started process has an empty last-tick fact, not a blank."""
    _present_internal_scheduler(super_admin_client, last_tick_at=None)
    response = super_admin_client.get("/super-admin/tenants", headers={"host": "localhost"})

    assert response.status_code == 200, response.text
    assert "No tick yet" in response.text


def test_platform_card_reports_a_stopped_scheduler(
    super_admin_client: TestClient,
) -> None:
    """The heartbeat fact follows the task, not the configuration."""
    _present_internal_scheduler(super_admin_client, alive=False)
    response = super_admin_client.get("/super-admin/tenants", headers={"host": "localhost"})

    assert response.status_code == 200, response.text
    assert "Stopped" in response.text


def test_platform_card_expects_an_external_tick_source_when_disabled(
    super_admin_client: TestClient,
) -> None:
    """Disabled (the package default): one sentence on what must run instead.

    The liveness and interval facts are omitted rather than blanked —
    this process knows nothing about an external timer.
    """
    response = super_admin_client.get("/super-admin/tenants", headers={"host": "localhost"})

    assert response.status_code == 200, response.text
    body = response.text
    assert "External" in body
    assert "systemd timer, cron, or equivalent" in body
    assert "portfoliflow irene-tick" in body
    assert "Heartbeat" not in body, "External mode claimed a liveness it cannot know."
    assert "Every 60 seconds" not in body


def test_platform_card_names_no_internal_component(
    super_admin_client: TestClient,
) -> None:
    """The operator-facing surface says "tick scheduler", never a codename.

    Asserted in both modes, because the two branches of the card carry
    different prose and only one of them is exercised by the tests above.
    """
    external = super_admin_client.get("/super-admin/tenants", headers={"host": "localhost"})
    _present_internal_scheduler(super_admin_client)
    internal = super_admin_client.get("/super-admin/tenants", headers={"host": "localhost"})

    for response in (external, internal):
        assert response.status_code == 200, response.text
        assert "Irene" not in response.text


# ---------------------------------------------------------------------------
# Cross-tenant no-leak runtime check
# ---------------------------------------------------------------------------


def test_super_admin_route_does_not_leak_tenant_data(
    super_admin_client: TestClient,
) -> None:
    """A super-admin session cannot reach tenant-data routes with data.

    The super-admin lives in the system tenant; the system tenant
    holds zero domain rows by the regression invariant. Hitting a
    tenant-data URL either redirects to login (system-tenant
    cookies aren't valid for tenant subdomains) or renders an empty
    state. Either way, no investment-data marker appears in the
    response body.
    """
    # A super-admin browsing tenant routes lands in the system tenant
    # context. The system tenant holds zero domain rows by invariant
    # (regression test ``test_system_tenant_holds_no_domain_data``),
    # so no real investment / NAV row data can leak. Static form
    # values such as ``private_equity`` (a dropdown option) are not
    # "data leaks" — they're schema. The leak signal is the SQL
    # table name appearing in the body, which would only happen if
    # an error trace exposed the schema.
    for path in (
        "/investments",
        "/front-office",
        "/back-office",
    ):
        response = super_admin_client.get(
            path,
            headers={"host": "localhost"},
            follow_redirects=False,
        )
        body = response.content.lower()
        assert b"investment_navs" not in body
        assert b"investment_cashflows" not in body
