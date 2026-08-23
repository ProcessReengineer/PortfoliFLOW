# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the market-data schedule web surface (ADR-0093, #036 slice 5).

ASGI-level tests over a live Postgres, mirroring the fixture pattern in
``tests/web/test_watch_desk.py`` (login helper, superuser-seeded
tenant/user, session-CSRF read from the DB). They cover the deliberately
small surface: the enable/disable + cadence CRUD, and "Refresh now" setting
``next_due_at := now`` on an enabled schedule (and refusing on a disabled
one, without moving the cursor).

The cadence tests keep ``daily`` — still an offered choice — and add the
sub-hourly case ADR-0125 §2 introduced: an ``every_15m`` save must land on
the quarter-hour grid and must render its label, not the raw value.

ADR-0125 §5/§6 adds two more concerns, both covered below:

* **The owner gate.** "Refresh now" is a tenant-level action, so a member
  posting to the endpoint directly gets a 403 and the schedule cursor does
  not move. The module's ``seeded_user`` already carries ``owner``; the
  member is seeded alongside it by ``seeded_member``, in this module rather
  than in ``conftest.py`` because no other web module needs one.
* **The post-enqueue poll**, one-for-one with the Watch Desk's briefing poll
  (ADR-0120): 204 while pending, 286 + the re-rendered panel once the run
  has landed, and 286 + ``HX-Reswap: none`` when there is nothing left to
  wait for.

ADR-0126 extends that gate to the rest of the surface: the schedule *save*
is owner-only too, and the Admin section is hidden from a member entirely.
The last three tests cover both halves.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient
from pathlib import Path
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.tenant_constants import SENTINEL_TENANT_ID
from services.password_hashing import hash_password
from web.main import create_app
from web.settings import WebSettings

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB market-data web tests.",
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


_TRUNCATE = text(
    "TRUNCATE TABLE market_data_schedule, login_audit, sessions, audit_log, "
    "users, tenants RESTART IDENTITY CASCADE"
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
    plaintext = "correct-horse-battery-staple"
    user_id = uuid4()
    email = "md-owner@example.com"
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, 'minathena-capital')"
            ),
            {"id": str(SENTINEL_TENANT_ID), "name": "Sentinel Tenant"},
        )
        await conn.execute(
            text(
                "INSERT INTO users "
                "(id, tenant_id, email, password_hash, roles, is_active) "
                "VALUES (:id, :tid, :email, :hash, "
                "ARRAY['owner']::text[], TRUE)"
            ),
            {
                "id": str(user_id),
                "tid": str(SENTINEL_TENANT_ID),
                "email": email,
                "hash": hash_password(plaintext),
            },
        )
    return user_id, email, plaintext


_MEMBER_EMAIL = "md-member@example.com"


@pytest_asyncio.fixture
async def seeded_member(
    fresh_superuser_engine: AsyncEngine,
    seeded_user: tuple[UUID, str, str],
) -> tuple[UUID, str, str]:
    """Seed a second user in the same tenant holding ``member`` only.

    Added here rather than to ``tests/web/conftest.py``: the owner gate on
    "Refresh now" (ADR-0125 §6) is the only thing in this package that needs
    to post as a non-owner, and the module already owns its seeding. Depends
    on ``seeded_user`` so the tenant row exists first.
    """
    plaintext = "correct-horse-battery-staple"
    user_id = uuid4()
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users "
                "(id, tenant_id, email, password_hash, roles, is_active) "
                "VALUES (:id, :tid, :email, :hash, "
                "ARRAY['member']::text[], TRUE)"
            ),
            {
                "id": str(user_id),
                "tid": str(SENTINEL_TENANT_ID),
                "email": _MEMBER_EMAIL,
                "hash": hash_password(plaintext),
            },
        )
    return user_id, _MEMBER_EMAIL, plaintext


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


async def _session_csrf(client: AsyncClient, engine: AsyncEngine) -> str:
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


async def _read_schedule(engine: AsyncEngine) -> tuple[bool, datetime] | None:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT enabled, next_due_at FROM market_data_schedule "
                    "WHERE tenant_id = :t AND user_id IS NULL"
                ),
                {"t": str(SENTINEL_TENANT_ID)},
            )
        ).first()
    if row is None:
        return None
    return bool(row.enabled), row.next_due_at


async def _seed_schedule(
    engine: AsyncEngine,
    *,
    enabled: bool = True,
    next_due_at: datetime | None = None,
    last_run_at: datetime | None = None,
) -> None:
    """Write the tenant-level schedule row directly, bypassing the route.

    The poll's branches are about ``last_run_at``, which no web route writes
    (the tick's ``mark_run_done`` does). Seeding through the superuser engine
    is how the Watch Desk's own poll tests stage the equivalent
    ``last_beat_at``, and it keeps the poll tests independent of the save
    route's cadence arithmetic.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM market_data_schedule WHERE tenant_id = :t AND user_id IS NULL"),
            {"t": str(SENTINEL_TENANT_ID)},
        )
        await conn.execute(
            text(
                "INSERT INTO market_data_schedule "
                "(tenant_id, user_id, cadence, preferred_hour, timezone, "
                " enabled, next_due_at, last_run_at) "
                "VALUES (:t, NULL, 'every_15m', 0, 'Europe/Berlin', "
                " :enabled, :next_due, :last_run)"
            ),
            {
                "t": str(SENTINEL_TENANT_ID),
                "enabled": enabled,
                "next_due": next_due_at or datetime.now(timezone.utc),
                "last_run": last_run_at,
            },
        )


def _poll_url(since: datetime) -> str:
    """The poll URL the confirmation partial builds, for a given instant."""
    return f"/api/market-data/refresh/poll?since={quote(since.isoformat(), safe='')}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_save_schedule_creates_enabled_row(
    web_client: AsyncClient, fresh_superuser_engine: AsyncEngine
) -> None:
    _, email, pw = (None, "md-owner@example.com", "correct-horse-battery-staple")
    await _login(web_client, email, pw)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    resp = await web_client.post(
        "/api/market-data/schedule",
        data={
            "cadence": "daily",
            "preferred_hour": "6",
            "timezone": "Europe/Berlin",
            "enabled": "on",
            "csrf_token": csrf,
        },
    )
    assert resp.status_code == 200, resp.text
    assert "Schedule saved." in resp.text

    state = await _read_schedule(fresh_superuser_engine)
    assert state is not None
    enabled, _next_due = state
    assert enabled is True


async def test_disable_via_crud(
    web_client: AsyncClient, fresh_superuser_engine: AsyncEngine
) -> None:
    await _login(web_client, "md-owner@example.com", "correct-horse-battery-staple")
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    # Enable, then disable (omit the checkbox field).
    await web_client.post(
        "/api/market-data/schedule",
        data={
            "cadence": "daily",
            "preferred_hour": "6",
            "timezone": "Europe/Berlin",
            "enabled": "on",
            "csrf_token": csrf,
        },
    )
    await web_client.post(
        "/api/market-data/schedule",
        data={
            "cadence": "daily",
            "preferred_hour": "6",
            "timezone": "Europe/Berlin",
            "csrf_token": csrf,
        },
    )
    state = await _read_schedule(fresh_superuser_engine)
    assert state is not None
    enabled, _ = state
    assert enabled is False


async def test_refresh_now_sets_due_now_when_enabled(
    web_client: AsyncClient, fresh_superuser_engine: AsyncEngine
) -> None:
    await _login(web_client, "md-owner@example.com", "correct-horse-battery-staple")
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    # Enable with a far-future preferred hour so next_due_at is well ahead.
    await web_client.post(
        "/api/market-data/schedule",
        data={
            "cadence": "daily",
            "preferred_hour": "6",
            "timezone": "Europe/Berlin",
            "enabled": "on",
            "csrf_token": csrf,
        },
    )
    before = await _read_schedule(fresh_superuser_engine)
    assert before is not None and before[1] > datetime.now(timezone.utc)

    resp = await web_client.post("/api/market-data/refresh-now", data={"csrf_token": csrf})
    assert resp.status_code == 200, resp.text
    assert "queued" in resp.text.lower()

    after = await _read_schedule(fresh_superuser_engine)
    assert after is not None
    # next_due_at was pulled back to ~now (<= now), so the next tick claims it.
    assert after[1] <= datetime.now(timezone.utc)


async def test_refresh_now_disabled_does_not_move_cursor(
    web_client: AsyncClient, fresh_superuser_engine: AsyncEngine
) -> None:
    await _login(web_client, "md-owner@example.com", "correct-horse-battery-staple")
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    # Save a DISABLED schedule (omit the enabled checkbox).
    await web_client.post(
        "/api/market-data/schedule",
        data={
            "cadence": "daily",
            "preferred_hour": "6",
            "timezone": "Europe/Berlin",
            "csrf_token": csrf,
        },
    )
    before = await _read_schedule(fresh_superuser_engine)
    assert before is not None

    resp = await web_client.post("/api/market-data/refresh-now", data={"csrf_token": csrf})
    assert resp.status_code == 200, resp.text
    assert "enable the schedule first" in resp.text.lower()

    after = await _read_schedule(fresh_superuser_engine)
    assert after is not None
    # A disabled schedule's cursor is untouched.
    assert after[1] == before[1]


async def test_save_schedule_every_15m_lands_on_the_quarter_hour_grid(
    web_client: AsyncClient, fresh_superuser_engine: AsyncEngine
) -> None:
    """A sub-hourly save round-trips through the shared cadence arithmetic.

    ADR-0125 §2 adds ``every_15m`` to what this panel offers; §1 states the
    quarter-hour grid is a *property* of the existing ``anchor + k·step``
    arithmetic rather than a new rule. Asserting the persisted
    ``next_due_at`` (rather than only a 200) is what proves the route feeds
    the new vocabulary through ``compute_next_due_at`` unchanged.

    The render assertions cover the other half of §2: the label map exists
    and the template uses it — a ``|capitalize`` would emit "Every_15m".
    """
    await _login(web_client, "md-owner@example.com", "correct-horse-battery-staple")
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    resp = await web_client.post(
        "/api/market-data/schedule",
        data={
            "cadence": "every_15m",
            "preferred_hour": "0",
            "timezone": "Europe/Berlin",
            "enabled": "on",
            "csrf_token": csrf,
        },
    )
    assert resp.status_code == 200, resp.text
    assert "Schedule saved." in resp.text
    assert "Every 15 minutes" in resp.text, "the cadence label must be rendered, not the raw value."
    assert "Refresh interval" in resp.text, "the panel caption is 'Refresh interval' (ADR-0125 §2)."
    assert "Every_15m" not in resp.text, "a |capitalize fallback would betray a missing label map."

    state = await _read_schedule(fresh_superuser_engine)
    assert state is not None
    enabled, next_due = state
    assert enabled is True
    assert next_due.minute in (0, 15, 30, 45), (
        "an every_15m schedule is due on the quarter-hour grid measured from "
        "the full hour (ADR-0125 §1)."
    )
    assert next_due.second == 0


# ---------------------------------------------------------------------------
# Owner gate and surface-aware confirmation — ADR-0125 §6
# ---------------------------------------------------------------------------


async def test_refresh_now_member_gets_403(
    web_client: AsyncClient,
    seeded_member: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """A member posting directly is refused, and the cursor does not move.

    Hiding the control in the template is a courtesy; the gate is the route.
    ``require_role("owner")`` returns the plain 403 (``insufficient role``)
    the other owner-gated routes return (ADR-0121, ADR-0125 §6) — the "same
    403 shape", not a bespoke one.
    """
    _uid, member_email, password = seeded_member
    await _seed_schedule(
        fresh_superuser_engine,
        enabled=True,
        next_due_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    await _login(web_client, member_email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    before = await _read_schedule(fresh_superuser_engine)
    assert before is not None

    resp = await web_client.post("/api/market-data/refresh-now", data={"csrf_token": csrf})

    assert resp.status_code == 403, resp.text
    after = await _read_schedule(fresh_superuser_engine)
    assert after is not None
    assert after[1] == before[1], "a refused refresh must not move next_due_at."


async def test_refresh_now_owner_admin_surface_renders_poller(
    web_client: AsyncClient, fresh_superuser_engine: AsyncEngine
) -> None:
    """The Admin confirmation is the panel, and it starts the poll.

    The poller is what closes ADR-0125's fourth gap: the enqueue used to be
    the last thing the operator saw. The ``since`` marker in the URL is the
    server's enqueue instant, so its presence is what proves the route hands
    the poll its own clock rather than leaving it to the browser.
    """
    await _login(web_client, "md-owner@example.com", "correct-horse-battery-staple")
    csrf = await _session_csrf(web_client, fresh_superuser_engine)
    await _seed_schedule(fresh_superuser_engine, enabled=True)

    resp = await web_client.post("/api/market-data/refresh-now", data={"csrf_token": csrf})

    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "Refresh queued" in body
    assert "/api/market-data/refresh/poll?since=" in body
    assert 'hx-trigger="every 15s"' in body
    # It is the panel, not the compact Overview partial.
    assert "Refresh interval" in body


async def test_refresh_now_overview_surface_renders_confirmation(
    web_client: AsyncClient, fresh_superuser_engine: AsyncEngine
) -> None:
    """``surface=overview`` swaps the compact line, never the Admin panel.

    One enqueue endpoint, two confirmations (ADR-0125 §6): the field selects
    a partial and nothing else. Rendering the settings panel into the
    Overview's meta line would be the visible failure this pins.
    """
    await _login(web_client, "md-owner@example.com", "correct-horse-battery-staple")
    csrf = await _session_csrf(web_client, fresh_superuser_engine)
    await _seed_schedule(fresh_superuser_engine, enabled=True)

    resp = await web_client.post(
        "/api/market-data/refresh-now",
        data={"csrf_token": csrf, "surface": "overview"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "ov-meta__refresh-state" in body
    assert "/api/overview/refresh/poll?since=" in body
    # Emphatically not the panel: no settings surface in a meta line.
    assert "Refresh interval" not in body
    assert "Save schedule" not in body


async def test_refresh_now_overview_surface_disabled_has_no_poller(
    web_client: AsyncClient, fresh_superuser_engine: AsyncEngine
) -> None:
    """Nothing enqueued, nothing to wait for — so no poller starts.

    The ADR-0120 no-schedule branch, applied to the disabled schedule: a
    poller here would run its full 10-minute horizon out asking about a run
    that was never queued.
    """
    await _login(web_client, "md-owner@example.com", "correct-horse-battery-staple")
    csrf = await _session_csrf(web_client, fresh_superuser_engine)
    await _seed_schedule(fresh_superuser_engine, enabled=False)

    resp = await web_client.post(
        "/api/market-data/refresh-now",
        data={"csrf_token": csrf, "surface": "overview"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "enable the schedule first" in body.lower()
    assert "hx-trigger" not in body
    assert "refresh/poll" not in body


# ---------------------------------------------------------------------------
# Refresh poll — the post-enqueue feedback (ADR-0125 §5, the ADR-0120 pattern)
# ---------------------------------------------------------------------------


async def test_admin_poll_pending_returns_204(
    web_client: AsyncClient, fresh_superuser_engine: AsyncEngine
) -> None:
    """No run yet: 204, which HTMX does not swap, so the panel stands."""
    await _login(web_client, "md-owner@example.com", "correct-horse-battery-staple")
    now = datetime.now(timezone.utc)
    await _seed_schedule(fresh_superuser_engine, enabled=True, last_run_at=None)

    resp = await web_client.get(_poll_url(now - timedelta(seconds=30)))

    assert resp.status_code == 204
    assert resp.text == ""


async def test_admin_poll_is_204_when_the_last_run_predates_the_enqueue(
    web_client: AsyncClient, fresh_superuser_engine: AsyncEngine
) -> None:
    """The condition is "since the enqueue", not "ever".

    A tenant with a refresh behind it would otherwise terminate the poll on
    its first tick and report a stale run as this click's result.
    """
    await _login(web_client, "md-owner@example.com", "correct-horse-battery-staple")
    now = datetime.now(timezone.utc)
    since = now - timedelta(seconds=30)
    await _seed_schedule(
        fresh_superuser_engine,
        enabled=True,
        last_run_at=since - timedelta(hours=6),
    )

    resp = await web_client.get(_poll_url(since))

    assert resp.status_code == 204
    assert resp.text == ""


async def test_admin_poll_landed_returns_286_with_panel(
    web_client: AsyncClient, fresh_superuser_engine: AsyncEngine
) -> None:
    """The done condition: 286 (stop polling) carrying the refreshed panel.

    286 is HTMX's "stop polling" status *and* a swappable one, so the one
    response both ends the poll and updates the page. The body must be the
    whole panel — the container swaps ``innerHTML``, and that swap is what
    removes the poller along with the markup it sat in.
    """
    await _login(web_client, "md-owner@example.com", "correct-horse-battery-staple")
    now = datetime.now(timezone.utc)
    since = now - timedelta(seconds=30)
    await _seed_schedule(
        fresh_superuser_engine,
        enabled=True,
        last_run_at=since + timedelta(seconds=5),
    )

    resp = await web_client.get(_poll_url(since))

    assert resp.status_code == 286
    body = resp.text
    assert "Refreshed at" in body
    # The whole panel rides along, not just a flash line.
    assert "Refresh interval" in body
    assert "Save schedule" in body
    # A settled panel starts no poll of its own — only a confirmation does.
    assert "refresh/poll?since=" not in body


@pytest.mark.parametrize(
    "query",
    [
        "",  # no marker at all
        "?since=",
        "?since=not-a-timestamp",
        "?since=2026-08-13T10:00:00",  # naive: no zone to compare against
    ],
    ids=["absent", "empty", "garbage", "naive"],
)
async def test_admin_poll_stops_on_unusable_since(
    web_client: AsyncClient, fresh_superuser_engine: AsyncEngine, query: str
) -> None:
    """A hand-edited marker terminates the poll — it never 500s.

    ``HX-Reswap: none`` is the load-bearing half: the poller declares an
    ``innerHTML`` swap of ``#pf-market-data-panel``, so an empty 286 without
    it would blank the panel instead of leaving it alone.
    """
    await _login(web_client, "md-owner@example.com", "correct-horse-battery-staple")
    await _seed_schedule(fresh_superuser_engine, enabled=True)

    resp = await web_client.get(f"/api/market-data/refresh/poll{query}")

    assert resp.status_code == 286
    assert resp.text == ""
    assert resp.headers["HX-Reswap"] == "none"


async def test_admin_poll_stops_without_a_schedule_row(web_client: AsyncClient) -> None:
    """No row for a run to stamp — nothing can ever land."""
    await _login(web_client, "md-owner@example.com", "correct-horse-battery-staple")

    resp = await web_client.get(_poll_url(datetime.now(timezone.utc)))

    assert resp.status_code == 286
    assert resp.text == ""
    assert resp.headers["HX-Reswap"] == "none"


async def test_admin_poll_stops_past_horizon(
    web_client: AsyncClient, fresh_superuser_engine: AsyncEngine
) -> None:
    """A run that never happens must not leave a tab polling for ever.

    The control case pins that it is the *horizon* terminating the poll and
    not the pending branch generally: nine minutes still gets a 204.
    """
    await _login(web_client, "md-owner@example.com", "correct-horse-battery-staple")
    now = datetime.now(timezone.utc)
    await _seed_schedule(fresh_superuser_engine, enabled=True)

    inside = await web_client.get(_poll_url(now - timedelta(minutes=9)))
    assert inside.status_code == 204

    beyond = await web_client.get(_poll_url(now - timedelta(minutes=11)))
    assert beyond.status_code == 286
    assert beyond.text == ""
    assert beyond.headers["HX-Reswap"] == "none"


# ---------------------------------------------------------------------------
# Owner gate on the schedule save and the section — ADR-0126
# ---------------------------------------------------------------------------


async def _read_schedule_shape(engine: AsyncEngine) -> tuple[str, int, str, bool] | None:
    """Read the settings a save writes, for the refusal assertion.

    ``_read_schedule`` answers the cursor questions the refresh tests ask;
    a refused *save* has to be shown not to have changed the configuration
    itself, which is the cadence, anchor hour, timezone and enabled flag.
    """
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT cadence, preferred_hour, timezone, enabled "
                    "FROM market_data_schedule "
                    "WHERE tenant_id = :t AND user_id IS NULL"
                ),
                {"t": str(SENTINEL_TENANT_ID)},
            )
        ).first()
    if row is None:
        return None
    return str(row.cadence), int(row.preferred_hour), str(row.timezone), bool(row.enabled)


async def test_save_schedule_member_gets_403(
    web_client: AsyncClient,
    seeded_member: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """A member cannot change the tenant's refresh cadence (ADR-0126).

    The schedule is a tenant-level resource — it governs how often the whole
    tenant spends its provider budget — so the save carries the same
    ``require_role("owner")`` gate as "Refresh now". Asserting the row is
    byte-for-byte the seeded one is what separates "refused" from "refused
    after writing".
    """
    _uid, member_email, password = seeded_member
    await _seed_schedule(fresh_superuser_engine, enabled=True)
    before = await _read_schedule_shape(fresh_superuser_engine)
    assert before == ("every_15m", 0, "Europe/Berlin", True)

    await _login(web_client, member_email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    resp = await web_client.post(
        "/api/market-data/schedule",
        data={
            "cadence": "daily",
            "preferred_hour": "6",
            "timezone": "UTC",
            "csrf_token": csrf,
        },
    )

    assert resp.status_code == 403, resp.text
    after = await _read_schedule_shape(fresh_superuser_engine)
    assert after == before, "a refused save must not have written anything."


async def test_the_admin_page_carries_the_market_data_section_for_an_owner(
    web_client: AsyncClient,
) -> None:
    await _login(web_client, "md-owner@example.com", "correct-horse-battery-staple")
    response = await web_client.get("/admin", follow_redirects=False)

    assert response.status_code == 200
    body = response.text
    assert 'id="market-data"' in body
    assert "/api/market-data/schedule" in body


async def test_the_admin_page_omits_the_market_data_section_for_a_member(
    web_client: AsyncClient,
    seeded_member: tuple[UUID, str, str],
) -> None:
    """Cosmetic mirroring of a gate the routes enforce on their own.

    Hiding it is what removes the two dead affordances ADR-0126 names: a
    save form that would 403 and a "Refresh now" button that swallows its
    own refusal (HTMX swaps nothing on 4xx).
    """
    _uid, member_email, password = seeded_member
    await _login(web_client, member_email, password)
    response = await web_client.get("/admin", follow_redirects=False)

    assert response.status_code == 200
    body = response.text
    assert 'id="market-data"' not in body
    assert "/api/market-data/schedule" not in body
    # The sections a member does get are untouched by the conditional.
    assert 'id="data-import"' in body
    assert 'id="providers-credentials"' in body
