# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Smoke tests for the FastAPI sub-stream-2a skeleton.

Also covers the tick scheduler's health reporting (ADR-0117 §5). Those
tests keep the endpoint's founding contract in view: ``/health`` must
answer 200 with the database down, with the lifespan half-configured, and
with no scheduler at all — an operations workflow treats a 5xx here as
"restart the web process".
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from web.main import create_app
from web.settings import WebSettings

# Far longer than any test takes, so the started task never actually
# reaches a tick: what is under test is the reporting, not the loop
# (``tests/web/test_tick_scheduler.py`` owns that).
_NEVER_TICKS_SECONDS = 3600


@asynccontextmanager
async def _running_app(
    *,
    database_url: str | None,
    database_url_superuser: str | None,
    tick_scheduler_enabled: bool,
) -> AsyncIterator[tuple[AsyncClient, FastAPI]]:
    """Run an app through its real lifespan and yield a client plus the app.

    The app is handed back alongside the client because the scheduler's
    status object lives on ``app.state`` and the tests below stamp it —
    that mutability is the design (ADR-0117 §5), not a test back door.

    The three settings arrive as explicit init values because a pydantic
    init value outranks the environment: the package's autouse fixture
    turns the scheduler off for everything else, and these are the tests
    that need it on.
    """
    settings = WebSettings(
        web_host="127.0.0.1",
        web_port=8000,
        session_cookie_name="portfoliflow_session",
        database_url=database_url,
        database_url_superuser=database_url_superuser,
        tick_scheduler_enabled=tick_scheduler_enabled,
        tick_scheduler_interval_seconds=_NEVER_TICKS_SECONDS,
    )
    app = create_app(settings)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client,
        app.router.lifespan_context(app),
    ):
        yield client, app


def _require_superuser_url() -> str:
    url = os.getenv("DATABASE_URL_SUPERUSER")
    if not url:
        pytest.skip("DATABASE_URL_SUPERUSER not set; the scheduler cannot start.")
    return url


@pytest.mark.asyncio
async def test_health_reports_ok_with_schema_revision_when_db_reachable(
    web_client_with_db: AsyncClient,
) -> None:
    response = await web_client_with_db.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["schema_revision"] is not None
    assert isinstance(payload["schema_revision"], str)


@pytest.mark.asyncio
async def test_health_reports_degraded_when_database_url_absent(
    web_client_no_db: AsyncClient,
) -> None:
    response = await web_client_no_db.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "degraded"
    assert payload["schema_revision"] is None


# ---------------------------------------------------------------------------
# Tick-scheduler health (ADR-0117 §5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_reports_an_external_tick_source_when_the_scheduler_is_disabled(
    web_client_no_db: AsyncClient,
) -> None:
    """``TICK_SCHEDULER_ENABLED=false`` (the package default here).

    External mode reports the mode alone — this process knows nothing
    about an external timer's liveness or rhythm, so it claims nothing.
    """
    payload = (await web_client_no_db.get("/health")).json()
    assert payload["tick_scheduler"] == {"mode": "external"}


@pytest.mark.asyncio
async def test_health_reports_external_when_the_scheduler_was_enabled_but_never_started() -> None:
    """Enabled, but with no RLS-bypassing URL the lifespan starts no task.

    Factually an external expectation: whatever the operator configured,
    this process is not ticking, and reporting ``internal`` would tell
    them the opposite of what they need to know.
    """
    async with _running_app(
        database_url=None,
        database_url_superuser=None,
        tick_scheduler_enabled=True,
    ) as (client, app):
        assert app.state.tick_scheduler is None
        payload = (await client.get("/health")).json()

    assert payload["status"] == "degraded"
    assert payload["tick_scheduler"] == {"mode": "external"}


@pytest.mark.asyncio
async def test_health_reports_the_internal_scheduler_before_its_first_tick() -> None:
    """A live task that has not ticked yet: alive, ``last_tick_at`` null."""
    async with _running_app(
        database_url=os.getenv("DATABASE_URL"),
        database_url_superuser=_require_superuser_url(),
        tick_scheduler_enabled=True,
    ) as (client, app):
        assert app.state.tick_scheduler is not None
        payload = (await client.get("/health")).json()

    assert payload["tick_scheduler"] == {
        "mode": "internal",
        "alive": True,
        "last_tick_at": None,
        "interval_seconds": _NEVER_TICKS_SECONDS,
    }


@pytest.mark.asyncio
async def test_health_reports_the_timestamp_of_the_last_completed_tick() -> None:
    """Once a tick completes, its instant is what the endpoint publishes."""
    ticked_at = datetime(2026, 8, 11, 9, 14, 0, tzinfo=timezone.utc)

    async with _running_app(
        database_url=os.getenv("DATABASE_URL"),
        database_url_superuser=_require_superuser_url(),
        tick_scheduler_enabled=True,
    ) as (client, app):
        app.state.tick_scheduler.status.last_tick_at = ticked_at
        payload = (await client.get("/health")).json()

    assert payload["tick_scheduler"]["last_tick_at"] == ticked_at.isoformat()


@pytest.mark.asyncio
async def test_health_reports_a_task_that_has_stopped_as_not_alive() -> None:
    """``alive`` is derived from the task, not from the configuration.

    A scheduler that died has to read differently from one that is
    running, or the field reports the operator's intent back at them
    instead of the process's state.
    """
    async with _running_app(
        database_url=os.getenv("DATABASE_URL"),
        database_url_superuser=_require_superuser_url(),
        tick_scheduler_enabled=True,
    ) as (client, app):
        handle = app.state.tick_scheduler
        handle.stop_event.set()
        await asyncio.wait_for(handle.task, timeout=2.0)
        payload = (await client.get("/health")).json()

    assert payload["tick_scheduler"]["mode"] == "internal"
    assert payload["tick_scheduler"]["alive"] is False


@pytest.mark.asyncio
async def test_health_still_answers_200_with_its_original_fields_when_degraded(
    web_client_no_db: AsyncClient,
) -> None:
    """The scheduler object is additive — it never displaces the contract."""
    response = await web_client_no_db.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "degraded"
    assert payload["schema_revision"] is None
    assert set(payload) == {"status", "schema_revision", "tick_scheduler"}


@pytest.mark.asyncio
async def test_login_form_renders_without_database(
    web_client_no_db: AsyncClient,
) -> None:
    """``GET /login`` works without a configured database.

    No cookie means no session lookup, which means no engine
    interaction — the form is pure HTML.
    """
    response = await web_client_no_db.get("/login")
    assert response.status_code == 200
    assert "<form" in response.text
    assert "csrf_token" in response.text
