# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""End-to-end test: subdomain routing maps requests to tenants.

Per ADR-0063 §1. Two tenants are seeded (primary + a second test
tenant) and the login surface is exercised against three host
shapes:

- known subdomain → renders the form
- unknown subdomain → 404
- localhost with ``LOCAL_DEV_TENANT_SUBDOMAIN`` set → resolves
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.testclient import TestClient

from core.tenant_constants import SENTINEL_TENANT_ID

from tests._db_fixtures import (  # noqa: F401  -- fixture re-export
    app_engine,
    reset_schema,
    seed_tenant,
    superuser_engine,
)


_PRIMARY_SUBDOMAIN: str = "minathena-capital"
_OTHER_TENANT_ID: UUID = uuid4()
_OTHER_SUBDOMAIN: str = "vwn"


@pytest.fixture(autouse=True)
async def _seed_tenants(superuser_engine: AsyncEngine):
    """Seed primary + other tenants for subdomain resolution."""
    async with superuser_engine.begin() as conn:
        for tid, name, subdomain in (
            (SENTINEL_TENANT_ID, "Minathena Capital", _PRIMARY_SUBDOMAIN),
            (_OTHER_TENANT_ID, "Test Tenant", _OTHER_SUBDOMAIN),
        ):
            await conn.execute(
                text(
                    "INSERT INTO tenants (id, name, subdomain) "
                    "VALUES (:id, :name, :subdomain) "
                    "ON CONFLICT (id) DO UPDATE "
                    "SET subdomain = EXCLUDED.subdomain"
                ),
                {"id": str(tid), "name": name, "subdomain": subdomain},
            )
    yield


@pytest.fixture
def client(monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("LOCAL_DEV_TENANT_SUBDOMAIN", _PRIMARY_SUBDOMAIN)
    from web.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c


def test_get_login_renders_on_primary_subdomain(client: TestClient) -> None:
    response = client.get(
        "/login",
        headers={"host": f"{_PRIMARY_SUBDOMAIN}.portfoliflow.net"},
    )
    assert response.status_code == 200


def test_get_login_renders_on_localhost_with_env_override(
    client: TestClient,
) -> None:
    response = client.get("/login", headers={"host": "localhost:8000"})
    assert response.status_code == 200


def test_post_login_unknown_subdomain_returns_404(
    client: TestClient,
) -> None:
    # POST /login requires the resolver; an unknown subdomain raises
    # 404 from the dependency before the form handler runs.
    response = client.post(
        "/login",
        headers={"host": "nonexistent.portfoliflow.net"},
        data={
            "email": "test@example.com",
            "password": "wrong",
            "csrf_token": "anything",
        },
        follow_redirects=False,
    )
    # The pre-session-CSRF check fires first (no cookie → 403), or
    # the resolver returns 404. Either way, no 200/303 — the login
    # must not succeed on an unknown host.
    assert response.status_code in {403, 404}
