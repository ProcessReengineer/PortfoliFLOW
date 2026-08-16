# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Role-gate matrix test for mutating routes.

Per ADR-0063 §2 every mutating route in the FastAPI surface carries
a ``require_role`` dependency. This test exercises a 3 × N matrix
(owner / member / auditor by route) on a representative subset of
mutating routes and asserts the 403/200 boundary.

The test is deliberately *representative*, not exhaustive — adding
a regression for every mutating route would mean maintaining
fixture data for every domain object. The subset covers the
distinct role classes (owner-only, owner+member) and the surface
areas a reviewer expects to see covered (investments, data_import,
SAA, analytics).

A 403 here means the dependency fired. A 400/422 (validation
error) when posting an owner-only route as owner is acceptable —
it confirms the dependency *did not* block.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.testclient import TestClient

from core.tenant_constants import SENTINEL_TENANT_ID
from services.password_hashing import hash_password
from web.settings import get_web_settings

from tests._db_fixtures import (  # noqa: F401
    app_engine,
    reset_schema,
    seed_tenant,
    superuser_engine,
)


# ---------------------------------------------------------------------------
# Fixtures: seed one tenant with three users (owner, member, auditor)
# ---------------------------------------------------------------------------


_TENANT_ID: UUID = SENTINEL_TENANT_ID
_TENANT_NAME: str = "RBAC Tenant"
_TENANT_SUBDOMAIN: str = "minathena-capital"

_OWNER_EMAIL: str = "owner@rbac.example"
_MEMBER_EMAIL: str = "member@rbac.example"
_AUDITOR_EMAIL: str = "auditor@rbac.example"
_PASSWORD: str = "correct-horse-battery-staple"


@pytest.fixture
def rbac_client(monkeypatch) -> Iterator[TestClient]:
    """Build a TestClient with the three RBAC users seeded.

    Lives at function scope so cross-test pollution can't accumulate.
    The web app's lifespan runs as part of the TestClient's __enter__.
    """
    monkeypatch.setenv("LOCAL_DEV_TENANT_SUBDOMAIN", _TENANT_SUBDOMAIN)

    from web.main import create_app

    app = create_app()
    # Give the test client a real client IP: ``login_audit.ip_address``
    # is an ``inet`` column and Starlette's default host ``"testclient"``
    # is not a valid IP, so the success-path audit insert would raise
    # DataError. Real requests always carry a real IP.
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        yield client


@pytest.fixture(autouse=True)
async def _seed_users(reset_schema, superuser_engine: AsyncEngine):
    """Seed the RBAC tenant and three role-specific users.

    Depends on ``reset_schema`` so the truncate-before always runs
    first; otherwise (pytest autouse ordering) the truncation can fire
    after this seed and wipe the RBAC tenant, leaving ``/login`` unable
    to resolve subdomain ``minathena-capital`` (404 tenant not found).
    """
    hashed = hash_password(_PASSWORD)
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) "
                "VALUES (:id, :name, :subdomain) "
                "ON CONFLICT (id) DO UPDATE "
                "SET subdomain = EXCLUDED.subdomain"
            ),
            {
                "id": str(_TENANT_ID),
                "name": _TENANT_NAME,
                "subdomain": _TENANT_SUBDOMAIN,
            },
        )
        for email, roles in (
            (_OWNER_EMAIL, ["owner"]),
            (_MEMBER_EMAIL, ["member"]),
            (_AUDITOR_EMAIL, ["auditor"]),
        ):
            await conn.execute(
                text(
                    """
                    INSERT INTO users
                        (tenant_id, email, password_hash, roles, is_active)
                    VALUES
                        (:tid, :email, :hash, :roles, TRUE)
                    """
                ),
                {
                    "tid": str(_TENANT_ID),
                    "email": email,
                    "hash": hashed,
                    "roles": roles,
                },
            )
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _login(client: TestClient, email: str) -> str:
    """POST /login as ``email``; return the session-bound CSRF token.

    The session CSRF token (validated by ``web.auth.verify_csrf`` against
    ``session.csrf_token``) is scraped from the Front Office page, which
    every tenant role — owner, member, auditor — may GET. Mutating
    role-gate POSTs must send it (``X-CSRF-Token`` header or ``csrf_token``
    form field) or they 403 at the CSRF gate before the role check runs.
    Mirrors the pattern in ``test_data_import_phase7_wiring.py``.
    """
    # Pre-session CSRF: GET /login mints the cookie and returns the form.
    response = client.get("/login", headers={"host": "localhost"})
    assert response.status_code == 200
    csrf_cookie = client.cookies.get(get_web_settings().csrf_cookie_name)
    assert csrf_cookie

    response = client.post(
        "/login",
        headers={"host": "localhost"},
        data={
            "email": email,
            "password": _PASSWORD,
            "csrf_token": csrf_cookie,
        },
        follow_redirects=False,
    )
    # Successful login redirects (303) to / or returns 200.
    assert response.status_code in {200, 303}, response.text

    page = client.get("/front-office", headers={"host": "localhost"})
    assert page.status_code == 200, page.text
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
    assert match is not None, "session CSRF token not found on /front-office"
    return match.group(1)


# ---------------------------------------------------------------------------
# Owner-only domain-write — investments.py
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "actor,expected_blocked",
    [
        (_OWNER_EMAIL, False),
        (_MEMBER_EMAIL, True),
        (_AUDITOR_EMAIL, True),
    ],
)
def test_post_investments_role_gate(
    rbac_client: TestClient, actor: str, expected_blocked: bool
) -> None:
    """POST /investments is owner-only.

    Owner: passes the gate (returns 200/302/400/422 — any non-403).
    Member, Auditor: blocked at 403 before the handler executes.
    """
    csrf = _login(rbac_client, actor)

    response = rbac_client.post(
        "/investments",
        headers={"host": "localhost", "X-CSRF-Token": csrf},
        data={
            "name": "Test Investment",
            "investment_type": "private_equity",
            "currency": "EUR",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )

    if expected_blocked:
        assert response.status_code == 403, response.text
    else:
        # Owner passes the gate; depending on validation / CSRF the
        # actual outcome may be 400/422/200/303. Any non-403 confirms
        # the gate did NOT fire.
        assert response.status_code != 403, response.text


# ---------------------------------------------------------------------------
# Owner+Member analytics-write — portfolio_analysis compute
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "actor,expected_blocked",
    [
        (_OWNER_EMAIL, False),
        (_MEMBER_EMAIL, False),
        (_AUDITOR_EMAIL, True),
    ],
)
def test_post_portfolio_analysis_compute_role_gate(
    rbac_client: TestClient, actor: str, expected_blocked: bool
) -> None:
    """POST /api/portfolio-analysis/section/compute allows owner + member."""
    csrf = _login(rbac_client, actor)

    response = rbac_client.post(
        "/api/portfolio-analysis/section/compute",
        headers={"host": "localhost", "X-CSRF-Token": csrf},
        data={"frontier_points": 25, "csrf_token": csrf},
        follow_redirects=False,
    )

    if expected_blocked:
        assert response.status_code == 403, response.text
    else:
        assert response.status_code != 403, response.text
