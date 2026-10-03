# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Web integration test: Phase-7 repositories are wired into the import route.

Regression coverage for the bug where the Excel-import web surface
called :meth:`InvestmentService.transform_upload_to_investments`
without the opt-in Phase-7 repositories
(``anlv_category_repository``, ``limits_repository``). The service
silently skipped persistence of the ``limit_set_saa`` and
``limit_set_2`` sheets, so a workbook uploaded through
``/admin#data-import`` left the ``limit_sets`` / ``limits`` tables
empty and the Investment-Limits section under ``/back-office#limits``
showed its empty state. The service-level roundtrip test was a false
positive because it called the service directly with the repositories
correctly wired.

This file exercises the HTTP route end to end with the committed
``sample_data/PortfoliFLOW_example_portfolio.xlsx``: real multipart
upload, real ``import-as-investments`` POST, real RLS-scoped
verification via a separate ``tenant_context`` session. Two cases:

* ``test_web_import_persists_limits_and_anlv`` — the write branch
  (``?dry_run=false``) populates ``limit_sets`` (both families) and
  ``investments.anlv_code``. The workbook has no ``AUM`` sheet, so the
  ADR-0103 §3 reconciliation control has nothing to report; its findings
  are covered at the service layer in
  ``tests/services/test_aum_reconciliation.py``.
* ``test_web_dry_run_completes_with_phase7_workbook`` — the dry-run
  branch returns 200 against the same workbook and writes nothing.

Requires ``DATABASE_URL`` + ``DATABASE_URL_SUPERUSER``. Each test gets a
fresh tenant via the shared ``_db_fixtures`` truncation; the
``anlv_categories`` global catalogue is preserved across resets per the
b010 seed contract.
"""

from __future__ import annotations

import os
import pathlib
import re
from collections.abc import AsyncGenerator
from uuid import UUID, uuid4

import pytest_asyncio
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from cli.bootstrap import (
    install_default_asset_classes,
    install_unclassified_asset_class,
)
from core.repositories import (
    AssetClassRepository,
    InvestmentRepository,
    LimitsRepository,
    tenant_context,
)
from core.tenant_constants import SENTINEL_TENANT_ID
from services.password_hashing import hash_password
from tests._db_fixtures import (  # noqa: F401 — fixture re-exports
    app_engine,
    reset_schema,
    superuser_engine,
)
from web.main import create_app
from web.settings import WebSettings

load_dotenv(pathlib.Path(__file__).resolve().parents[2] / ".env")

_WORKBOOK_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "sample_data"
    / "PortfoliFLOW_example_portfolio.xlsx"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def seeded_tenant(
    superuser_engine: AsyncEngine,
    app_engine: AsyncEngine,
) -> tuple[UUID, UUID, str, str]:
    """Re-seed the Sentinel tenant + an owner user; bootstrap asset classes.

    The login flow writes to ``login_audit`` against the Sentinel
    tenant for unauthenticated requests, so the Sentinel row must
    exist before ``/login`` is hit. The shared ``reset_schema``
    fixture truncates ``tenants`` between tests, so each test
    re-inserts it here.

    The workbook's limit sets reference asset-class codes (``equities``,
    ``private_equity``, …) that live in the per-tenant
    ``asset_classes`` table. Without the catalogue the SAA limit-set
    import would fail with an unknown-code error before the bug
    under test ever fires.

    Returns ``(tenant_id, user_id, email, plaintext_password)``.
    """
    plaintext = "correct-horse-battery-staple"
    tenant_id = SENTINEL_TENANT_ID
    user_id = uuid4()
    email = "phase7-uploader@example.com"
    async with superuser_engine.begin() as conn:
        # The autouse fixture in tests/web/conftest.py sets
        # LOCAL_DEV_TENANT_SUBDOMAIN="minathena-capital", so the tenant
        # resolver looks up exactly that subdomain on POST /login. Seed the
        # canonical subdomain and overwrite any pre-existing wrong value (a
        # bare DO NOTHING would leave a stale subdomain on a surviving row).
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) "
                "VALUES (:id, :name, 'minathena-capital') "
                "ON CONFLICT (id) DO UPDATE SET subdomain = EXCLUDED.subdomain"
            ),
            {"id": str(tenant_id), "name": "Sentinel Tenant"},
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
                "tid": str(tenant_id),
                "email": email,
                "hash": hash_password(plaintext),
            },
        )
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        repo = AssetClassRepository(session)
        await install_unclassified_asset_class(repo)
        await install_default_asset_classes(repo)
    return tenant_id, user_id, email, plaintext


@pytest_asyncio.fixture
async def web_client(
    seeded_tenant: tuple[UUID, UUID, str, str],
) -> AsyncGenerator[AsyncClient, None]:
    """ASGI client whose app engine is bound to the test database."""
    db_url = os.getenv("DATABASE_URL")
    db_super = os.getenv("DATABASE_URL_SUPERUSER")
    settings = WebSettings(
        web_host="127.0.0.1",
        web_port=8000,
        session_cookie_name="portfoliflow_session",
        csrf_cookie_name="portfoliflow_csrf_pre_session",
        database_url=db_url,
        database_url_superuser=db_super,
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


async def _login_and_get_csrf(client: AsyncClient, email: str, password: str) -> str:
    """Drive ``GET /login`` + ``POST /login``, return the session CSRF.

    The session-bound token is scraped from the Admin page, which
    embeds the Data Import section's form.
    """
    get_response = await client.get("/login")
    pre_session_csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
    assert pre_session_csrf is not None
    await client.post(
        "/login",
        data={
            "email": email,
            "password": password,
            "csrf_token": pre_session_csrf,
        },
        follow_redirects=False,
    )
    page = await client.get("/admin", follow_redirects=False)
    assert page.status_code == 200, page.text
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
    assert match is not None, "CSRF token not found on the Admin page"
    return match.group(1)


async def _upload_example(client: AsyncClient, csrf: str) -> UUID:
    """Upload the example workbook via the section endpoint; return upload id.

    The preview fragment carries ``data-upload-id="…"`` on the
    confirm button — the most stable hook into the rendered HTML.
    """
    payload = _WORKBOOK_PATH.read_bytes()
    response = await client.post(
        "/api/data-import/section/upload",
        data={"csrf_token": csrf},
        files={
            "file": (
                _WORKBOOK_PATH.name,
                payload,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        },
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200, response.text
    match = re.search(r'data-upload-id="([0-9a-f-]{36})"', response.text)
    assert match is not None, (
        "Preview fragment did not expose data-upload-id; the upload may have failed silently."
    )
    return UUID(match.group(1))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_web_import_persists_limits_and_anlv(
    web_client: AsyncClient,
    seeded_tenant: tuple[UUID, UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """Write branch wires the Phase-7 repositories into the service call.

    Before the original fix, ``limit_sets`` / ``limits`` stayed empty after
    an upload through the web route — the JSONB ``data_upload_sheets`` row
    was the only landing place.
    """
    tenant_id, user_id, email, password = seeded_tenant

    csrf = await _login_and_get_csrf(web_client, email, password)
    upload_id = await _upload_example(web_client, csrf)

    commit = await web_client.post(
        f"/api/data-uploads/{upload_id}/import-as-investments",
        params={"dry_run": "false"},
        headers={"X-CSRF-Token": csrf, "HX-Request": "true"},
        follow_redirects=False,
    )
    assert commit.status_code == 200, commit.text

    # No AUM sheet, so the reconciliation control reports nothing.
    payload = commit.json()
    assert not [w for w in payload["warnings"] if w["field"] == "aum_reconciliation"]

    # Verify in a fresh tenant-scoped session so the assertion path
    # is independent from the route handler's connection.
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        limits_repo = LimitsRepository(session)
        assert len(await limits_repo.list_sets("saa")) == 2, "SAA limit sets were not persisted."
        assert len(await limits_repo.list_sets("anlv")) == 2, "AnlV limit sets were not persisted."

        investments = await InvestmentRepository(session).list_active()
        anlv_by_name = {inv.name: inv.anlv_code for inv in investments}
        assert anlv_by_name.get("Investment A") == "anlv_12", (
            "anlv_code was not populated from the Attributes sheet."
        )
        assert sum(code is not None for code in anlv_by_name.values()) == 21


async def test_web_dry_run_completes_with_phase7_workbook(
    web_client: AsyncClient,
    seeded_tenant: tuple[UUID, UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """Dry-run branch accepts the example workbook and writes nothing.

    Defensive coverage: even with the Phase-7 repositories now wired,
    ``dry_run=true`` must remain read-only — the service short-circuits
    before the persistence step.
    """
    tenant_id, user_id, email, password = seeded_tenant

    csrf = await _login_and_get_csrf(web_client, email, password)
    upload_id = await _upload_example(web_client, csrf)

    response = await web_client.post(
        f"/api/data-uploads/{upload_id}/import-as-investments",
        params={"dry_run": "true"},
        headers={"X-CSRF-Token": csrf, "HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200, response.text

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        limits_repo = LimitsRepository(session)
        assert await limits_repo.list_sets("saa") == []
        assert await limits_repo.list_sets("anlv") == []
