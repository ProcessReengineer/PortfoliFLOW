# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Web integration test: the upload route persists benchmark sheets.

Regression coverage for the hotfix where
``InvestmentService.transform_benchmarks_from_upload`` was tested in
isolation but **not** invoked by ``web/routes/data_import.py``. The
JSONB ``data_upload_sheets`` row carried ``benchmarks_actual`` and
``benchmark_mapping`` payloads, but nothing landed in
``benchmarks`` / ``benchmark_observations`` /
``asset_class_benchmark_mapping``.

Focus is on the *wiring*; the end-to-end semantics of the benchmark
transformer itself are covered in
``tests/services/test_investment_service_transform_benchmarks.py``.

Two cases:

* ``test_upload_endpoint_persists_benchmarks_when_sheets_present`` —
  the v24 workbook (Bootstrap-aligned asset-class strings + the two
  Phase-7 sheets) goes through the route; benchmark tables hold
  rows and the response payload exposes the new ``benchmarks_*``
  counters.
* ``test_upload_endpoint_skips_benchmarks_when_sheets_absent`` — the
  v21 (pre-benchmark) workbook returns 200 with ``benchmarks_created
  == 0`` and no benchmark rows are written.

Skip-friendly: requires ``DATABASE_URL`` + ``DATABASE_URL_SUPERUSER``
plus the relevant sample workbooks under ``data/sample/``.
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
    AssetClassBenchmarkMappingRepository,
    AssetClassRepository,
    BenchmarkObservationRepository,
    BenchmarkRepository,
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

_SAMPLE_DIR = pathlib.Path(__file__).resolve().parents[2] / "data" / "sample"
V21_PATH = _SAMPLE_DIR / "PortfoliFLOW_Testdaten_v21.xlsx"
V24_PATH = _SAMPLE_DIR / "PortfoliFLOW_Testdaten_v24.xlsx"


def _require(path: pathlib.Path) -> None:
    if not path.exists():
        pytest.skip(f"testdata not at {path}; skipping.")


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

    Bootstrap default asset classes are needed because the v24
    workbook's ``Benchmark Mapping`` sheet uses Excel strings that
    normalise to the bootstrap-installed codes (e.g. ``"Gov Bonds
    DM"`` → ``"gov_bonds_dm"``). Without the bootstrap catalogue the
    benchmark import would raise :class:`ValidationError` for unknown
    asset-class codes.

    Returns ``(tenant_id, user_id, email, plaintext_password)``.
    """
    plaintext = "correct-horse-battery-staple"
    tenant_id = SENTINEL_TENANT_ID
    user_id = uuid4()
    email = "benchmark-uploader@example.com"
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
    """Drive ``GET /login`` + ``POST /login``, return the session CSRF."""
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


async def _upload_workbook(client: AsyncClient, csrf: str, path: pathlib.Path) -> UUID:
    """Upload a workbook via the section endpoint; return upload id."""
    payload = path.read_bytes()
    response = await client.post(
        "/api/data-import/section/upload",
        data={"csrf_token": csrf},
        files={
            "file": (
                path.name,
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


@pytest.mark.timeout(600)
async def test_upload_endpoint_persists_benchmarks_when_sheets_present(
    web_client: AsyncClient,
    seeded_tenant: tuple[UUID, UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """v24 workbook → upload endpoint → benchmark tables populated.

    Before the hotfix, the route silently dropped the Phase-7
    benchmark payload: the JSONB upload row carried the two sheets,
    but ``transform_benchmarks_from_upload`` was never called from
    the write branch.

    The v24 workbook carries ~30k benchmark observations on top of
    the full investment-domain payload; the per-row inserts push the
    end-to-end transform well past the pytest-timeout default. The
    longer timeout is realistic for a single full-workbook write
    cycle on the local Postgres.
    """
    _require(V24_PATH)
    tenant_id, user_id, email, password = seeded_tenant

    csrf = await _login_and_get_csrf(web_client, email, password)
    upload_id = await _upload_workbook(web_client, csrf, V24_PATH)

    commit = await web_client.post(
        f"/api/data-uploads/{upload_id}/import-as-investments",
        params={"dry_run": "false"},
        headers={"X-CSRF-Token": csrf, "HX-Request": "true"},
        follow_redirects=False,
    )
    assert commit.status_code == 200, commit.text
    body = commit.json()
    assert body.get("benchmarks_created", 0) > 0, (
        "Response payload reports zero benchmarks_created — the "
        "transform_benchmarks_from_upload call is not wired into the "
        "write branch."
    )
    assert body.get("benchmark_observations_inserted", 0) > 0
    assert body.get("benchmark_mappings_created", 0) > 0
    assert "benchmark_warnings" in body

    # Verify in a fresh tenant-scoped session so the assertion path
    # is independent from the route handler's connection.
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        benchmarks = await BenchmarkRepository(session).list_all()
        assert len(benchmarks) > 0, "benchmarks table is empty after the upload."
        obs_repo = BenchmarkObservationRepository(session)
        obs = await obs_repo.list_for_benchmarks([b.id for b in benchmarks])
        assert len(obs) > 0, "benchmark_observations is empty after the upload."
        mappings = await AssetClassBenchmarkMappingRepository(session).list_all()
        assert len(mappings) > 0, "asset_class_benchmark_mapping is empty after the upload."


async def test_upload_endpoint_skips_benchmarks_when_sheets_absent(
    web_client: AsyncClient,
    seeded_tenant: tuple[UUID, UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """v21 (pre-benchmark) workbook → no benchmark rows, no exceptions.

    The benchmark transformer must short-circuit cleanly when the
    workbook has no ``Benchmarks actual`` / ``Benchmark Mapping``
    sheets; the route must return 200 with ``benchmarks_created ==
    0``.
    """
    _require(V21_PATH)
    tenant_id, user_id, email, password = seeded_tenant

    csrf = await _login_and_get_csrf(web_client, email, password)
    upload_id = await _upload_workbook(web_client, csrf, V21_PATH)

    commit = await web_client.post(
        f"/api/data-uploads/{upload_id}/import-as-investments",
        params={"dry_run": "false"},
        headers={"X-CSRF-Token": csrf, "HX-Request": "true"},
        follow_redirects=False,
    )
    assert commit.status_code == 200, commit.text
    body = commit.json()
    assert body.get("benchmarks_created", 0) == 0
    assert body.get("benchmark_observations_inserted", 0) == 0
    assert body.get("benchmark_mappings_created", 0) == 0

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        assert await BenchmarkRepository(session).list_all() == []
        assert await AssetClassBenchmarkMappingRepository(session).list_all() == []
