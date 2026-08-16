# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Web integration test: Phase-7 repositories are wired into the import route.

Regression coverage for the bug where the Excel-import web surface
called :meth:`InvestmentService.transform_upload_to_investments`
without the opt-in Phase-7 repositories
(``anlv_category_repository``,
``limits_repository``). The service silently skipped persistence of
the ``AUM``, ``limit_set_saa``, and ``limit_set_anlv`` sheets, so a
V21 workbook uploaded through ``/admin#data-import`` left the
``limit_sets`` / ``limits`` tables empty and the
Investment-Limits section under ``/back-office#limits`` showed its
empty-state. The service-level V21 roundtrip test was a false
positive because it called the service directly with the repos
correctly wired.

This file exercises the HTTP route end-to-end: real multipart upload,
real ``import-as-investments`` POST, real RLS-scoped verification via
a separate ``tenant_context`` session. Two cases:

* ``test_web_import_persists_limits_and_anlv_but_not_aum`` — the write
  branch (``?dry_run=false``) populates ``limit_sets`` (saa + anlv
  families) and ``investments.anlv_code``, and — since ADR-0103 §3 demoted
  the AUM sheet to a reconciliation control, and §7 dropped the table
  outright (b030) — so the import writes no AUM
  rows, surfacing the stated-vs-Σ-NAV deviations as import warnings instead.
* ``test_web_dry_run_completes_with_phase7_workbook`` — the dry-run
  branch returns 200 against a V21 workbook and writes nothing.

Skip-friendly: requires ``DATABASE_URL`` + ``DATABASE_URL_SUPERUSER``
plus ``data/sample/PortfoliFLOW_Testdaten_v21.xlsx``. Each test gets a
fresh tenant via the shared ``_db_fixtures`` truncation; the
``anlv_categories`` global catalogue is preserved across resets per
the b010 seed contract.
"""

from __future__ import annotations

import os
import pathlib
import re
from collections.abc import AsyncGenerator
from uuid import UUID, uuid4

import pytest
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

V21_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "data"
    / "sample"
    / "PortfoliFLOW_Testdaten_v21.xlsx"
)


def _require_v21() -> None:
    if not V21_PATH.exists():
        pytest.skip(f"v21 testdata not at {V21_PATH}; skipping.")


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

    The V21 limit sets reference asset-class codes (``equities``,
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


async def _upload_v21(client: AsyncClient, csrf: str) -> UUID:
    """Upload the V21 workbook via the section endpoint; return upload id.

    The preview fragment carries ``data-upload-id="…"`` on the
    confirm button — the most stable hook into the rendered HTML.
    """
    payload = V21_PATH.read_bytes()
    response = await client.post(
        "/api/data-import/section/upload",
        data={"csrf_token": csrf},
        files={
            "file": (
                "v21.xlsx",
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


async def test_web_import_persists_limits_and_anlv_but_not_aum(
    web_client: AsyncClient,
    seeded_tenant: tuple[UUID, UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """Write branch wires the Phase-7 repositories into the service call.

    Before the original fix, ``limit_sets`` / ``limits`` stayed empty after
    a V21 upload through the web route — the JSONB ``data_upload_sheets``
    row was the only landing place.

    AUM is the exception, and deliberately so since ADR-0103
    §3: the AUM sheet was **demoted to a reconciliation control** and no
    longer persists. The import compares each stated figure against Σ NAV
    and reports the deviations as warnings; the table keeps whatever it last
    held (its read consumers are retired in ADR-0103 §7, strand S1.7) and
    receives nothing new. Zero rows after an import of a workbook with a
    5,479-row AUM sheet is therefore the assertion, not a regression.
    """
    _require_v21()
    tenant_id, user_id, email, password = seeded_tenant

    csrf = await _login_and_get_csrf(web_client, email, password)
    upload_id = await _upload_v21(web_client, csrf)

    commit = await web_client.post(
        f"/api/data-uploads/{upload_id}/import-as-investments",
        params={"dry_run": "false"},
        headers={"X-CSRF-Token": csrf, "HX-Request": "true"},
        follow_redirects=False,
    )
    assert commit.status_code == 200, commit.text

    # The control ran: the V21 book cannot possibly agree with its own AUM
    # sheet (the residual was the whole point of that sheet), so the import
    # reports findings — and reports them as *warnings*, never as an error.
    payload = commit.json()
    aum_findings = [w for w in payload["warnings"] if w["field"] == "aum_reconciliation"]
    assert aum_findings, (
        "The demoted AUM sheet produced no reconciliation findings; the "
        "control did not run (ADR-0103 §3)."
    )

    # Verify in a fresh tenant-scoped session so the assertion path
    # is independent from the route handler's connection.
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        limits_repo = LimitsRepository(session)
        saa_sets = await limits_repo.list_sets("saa")
        anlv_sets = await limits_repo.list_sets("anlv")
        assert len(saa_sets) >= 1, "SAA limit sets were not persisted."
        assert len(anlv_sets) >= 1, "AnlV limit sets were not persisted."

        investments = await InvestmentRepository(session).list_active()
        assert any(inv.anlv_code is not None for inv in investments), (
            "anlv_code was not populated from the Attributes sheet."
        )


async def test_web_dry_run_completes_with_phase7_workbook(
    web_client: AsyncClient,
    seeded_tenant: tuple[UUID, UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """Dry-run branch accepts a V21 workbook and writes nothing.

    Defensive coverage: even with the Phase-7 repositories now wired,
    ``dry_run=true`` must remain read-only — the service short-circuits
    before the persistence step.
    """
    _require_v21()
    tenant_id, user_id, email, password = seeded_tenant

    csrf = await _login_and_get_csrf(web_client, email, password)
    upload_id = await _upload_v21(web_client, csrf)

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
