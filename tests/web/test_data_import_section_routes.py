# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""HTTP-level tests for the embedded Data Import section endpoints.

Live-DB tests against the compose Postgres. The fixtures seed the
sentinel tenant plus a sentinel-tenant user; an additional fixture
seeds the bootstrap-required ``unclassified`` asset class so the
dry-run extraction the upload endpoint now performs has the
deployment-time row it needs. The per-test client is bound via
``ASGITransport``; an in-process ``openpyxl`` workbook keeps the
suite free of ``data/sample/`` file dependencies.

Coverage targets — sub-stream 6F (single-button workflow):

* ``GET /api/data-import/section`` requires authentication and
  returns the upload-form fragment.
* ``POST /api/data-import/section/upload`` rejects requests without
  a valid CSRF token (403).
* A valid Excel import workbook returns the preview fragment with projected
  counts and the ``Apply to Investments`` button — the upload and the
  dry-run happen in one round-trip.
* Re-uploading the same bytes returns the existing upload's preview
  fragment without inserting a duplicate row.
* Oversized uploads return 413 with the upload-form fragment.
* Malformed (non-Excel) uploads return the upload-form fragment with
  an inline error.
* A parseable-but-not-transformable workbook (dry-run raises
  :class:`ImportFormatError`) returns the upload-form fragment with
  an explanatory inline error and the row still persists.
* The audit trail captures the INSERT with the correct
  ``tenant_id`` / ``user_id``.
"""

from __future__ import annotations

import datetime
import io
import os
import pathlib
from collections.abc import AsyncGenerator
from uuid import UUID, uuid4

import openpyxl
import pytest
import pytest_asyncio
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.tenant_constants import SENTINEL_TENANT_ID
from services.data_normalization import ImportFormatError
from services.password_hashing import hash_password
from web.main import create_app
from web.settings import WebSettings

load_dotenv(pathlib.Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB data-import section tests.",
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
async def reset_schema(
    fresh_superuser_engine: AsyncEngine,
) -> AsyncGenerator[None, None]:
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE data_upload_sheets, data_uploads, "
                "login_audit, sessions, audit_log, "
                "data_store_entries, users, tenants "
                "RESTART IDENTITY CASCADE"
            )
        )
    yield


@pytest_asyncio.fixture
async def seeded_user(
    fresh_superuser_engine: AsyncEngine,
    reset_schema: None,
) -> tuple[UUID, str, str]:
    plaintext = "correct-horse-battery-staple"
    user_id = uuid4()
    email = "uploader@example.com"
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, 'minathena-capital') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": str(SENTINEL_TENANT_ID), "name": "Sentinel Tenant"},
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
                "tid": str(SENTINEL_TENANT_ID),
                "email": email,
                "hash": hash_password(plaintext),
            },
        )
    return user_id, email, plaintext


@pytest_asyncio.fixture
async def seeded_bootstrap(
    fresh_superuser_engine: AsyncEngine,
    seeded_user: tuple[UUID, str, str],
) -> tuple[UUID, str, str]:
    """Seed the bootstrap rows the dry-run extraction relies on.

    :meth:`InvestmentService.transform_upload_to_investments` raises
    ``ValueError("The 'unclassified' asset class is missing …")`` if the
    per-tenant ``unclassified`` asset class is missing. The upload route now
    runs that extraction in dry-run mode on every successful upload,
    so any test that posts a valid workbook needs the row in place.
    Returns the same tuple as :func:`seeded_user` for convenience.
    """
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO asset_classes
                    (id, tenant_id, code, display_name)
                VALUES
                    (:id, :tid, 'unclassified', 'Unclassified')
                ON CONFLICT DO NOTHING
                """
            ),
            {"id": str(uuid4()), "tid": str(SENTINEL_TENANT_ID)},
        )
    return seeded_user


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


async def _login_and_get_csrf(client: AsyncClient, email: str, password: str) -> str:
    """Drive ``GET /login`` + ``POST /login``, return the session CSRF.

    Scrapes the token from the Admin page since that is the surface
    that embeds the Data Import section.
    """
    get_response = await client.get("/login")
    csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
    assert csrf is not None
    await client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf},
        follow_redirects=False,
    )
    page = await client.get("/admin", follow_redirects=False)
    assert page.status_code == 200
    import re

    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
    assert match is not None
    return match.group(1)


# ---------------------------------------------------------------------------
# Minimal Excel-import workbook generator (same shape as the sunset test file)
# ---------------------------------------------------------------------------


def _build_minimal_v2_workbook() -> bytes:
    """Return the bytes of a minimal but valid Excel import workbook."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    sheet_names = [
        "Attributes",
        "Cash Flow In actual",
        "Cash Flow In plan",
        "Cash Flow Out actual",
        "Cash Flow Out plan",
        "NAVs actual",
        "NAVs plan",
        "total return actual",
        "total return plan",
        "interest rates",
    ]
    for name in sheet_names:
        wb.create_sheet(name)

    ws = wb["Attributes"]
    ws.append([None, "Investition A"])
    ws.append([None, "Aktien"])
    ws.append([None, "Large Cap"])
    ws.append(["Region", "Europa"])

    date = datetime.datetime(2024, 1, 1)
    for name in (
        "Cash Flow In actual",
        "Cash Flow In plan",
        "Cash Flow Out actual",
        "Cash Flow Out plan",
        "NAVs actual",
        "NAVs plan",
        "total return actual",
        "total return plan",
    ):
        ws = wb[name]
        ws.append([None, "Investition A"])
        ws.append([None, "Aktien"])
        ws.append([None, "Large Cap"])
        if name == "NAVs actual":
            ws.append([date, 1_000_000])
        elif name == "Cash Flow Out actual":
            ws.append([date, -1_000_000])
        elif name == "total return actual":
            ws.append([date, 0.01])
        elif name == "Cash Flow In actual":
            ws.append([date, 50_000])
        else:
            ws.append([date, None])

    ws = wb["interest rates"]
    ws.append([None, "risk free rate"])
    ws.append([None, None])
    ws.append([None, None])
    ws.append([date, 0.04])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# GET /api/data-import/section
# ---------------------------------------------------------------------------


async def test_get_section_unauthenticated_redirects(
    web_client: AsyncClient,
) -> None:
    """``require_session`` redirects unauthenticated callers to /login.

    The HTMX endpoints share the same auth dependency as the page
    routes, so the redirect status is preserved.
    """
    response = await web_client.get("/api/data-import/section", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_get_section_renders_upload_form_when_authenticated(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _id, email, password = seeded_user
    await _login_and_get_csrf(web_client, email, password)

    response = await web_client.get(
        "/api/data-import/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert 'id="pf-data-import-panel"' in body
    assert "Upload and Import" in body
    assert 'name="file"' in body
    assert "Recent uploads" in body
    assert "No uploads yet." in body


# ---------------------------------------------------------------------------
# POST /api/data-import/section/upload — happy / error paths
# ---------------------------------------------------------------------------


async def test_post_section_upload_without_csrf_returns_403(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _id, email, password = seeded_user
    await _login_and_get_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/data-import/section/upload",
        files={
            "file": (
                "x.xlsx",
                _build_minimal_v2_workbook(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        },
        follow_redirects=False,
    )
    assert response.status_code == 403


async def test_post_section_upload_with_valid_xlsx_returns_preview(
    web_client: AsyncClient,
    seeded_bootstrap: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """The happy path now returns the preview fragment in one round-trip.

    The route persists the ``data_uploads`` row and then runs the
    dry-run extractor server-side; the response body is the preview
    with projected counts and the ``Apply to Investments`` confirm
    button.
    """
    _id, email, password = seeded_bootstrap
    csrf = await _login_and_get_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/data-import/section/upload",
        data={"csrf_token": csrf},
        files={
            "file": (
                "happy.xlsx",
                _build_minimal_v2_workbook(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        },
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    # Preview markers — filename, the status line, the confirm /
    # discard buttons, the back link, the projected-counts list.
    assert "happy.xlsx" in body
    assert "preview computed" in body
    assert "Apply to Investments" in body
    assert "Discard Preview" in body
    assert "Back to upload" in body
    assert "Created:" in body
    # The pre-preview trigger button is gone — the dry-run already ran.
    assert "Import to Investments" not in body

    # Verify exactly one upload row landed.
    async with fresh_superuser_engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT filename, format_version FROM data_uploads WHERE tenant_id = :tid"),
            {"tid": str(SENTINEL_TENANT_ID)},
        )
        all_rows = rows.fetchall()
    assert len(all_rows) == 1
    assert all_rows[0][0] == "happy.xlsx"
    assert all_rows[0][1] == "v2"


async def test_post_section_upload_dedups_returns_existing_preview(
    web_client: AsyncClient,
    seeded_bootstrap: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    _id, email, password = seeded_bootstrap
    csrf = await _login_and_get_csrf(web_client, email, password)
    payload = _build_minimal_v2_workbook()

    first = await web_client.post(
        "/api/data-import/section/upload",
        data={"csrf_token": csrf},
        files={
            "file": (
                "dup.xlsx",
                payload,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        },
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert first.status_code == 200
    assert "dup.xlsx" in first.text
    assert "Apply to Investments" in first.text

    # Re-upload the same bytes — the dedup path returns the existing
    # upload's preview fragment instead of inserting a duplicate row.
    second = await web_client.post(
        "/api/data-import/section/upload",
        data={"csrf_token": csrf},
        files={
            "file": (
                "dup.xlsx",
                payload,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        },
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert second.status_code == 200
    assert "Apply to Investments" in second.text
    assert "preview computed" in second.text

    # And the table still has exactly one row.
    async with fresh_superuser_engine.connect() as conn:
        count = await conn.execute(
            text("SELECT COUNT(*) FROM data_uploads WHERE tenant_id = :tid"),
            {"tid": str(SENTINEL_TENANT_ID)},
        )
        assert count.scalar_one() == 1


async def test_post_section_upload_oversized_returns_413(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file larger than the configured cap is rejected with 413."""
    _id, email, password = seeded_user
    csrf = await _login_and_get_csrf(web_client, email, password)

    monkeypatch.setenv("WEB_MAX_UPLOAD_SIZE_MB", "1")
    blob = b"\x00" * (2 * 1024 * 1024)

    response = await web_client.post(
        "/api/data-import/section/upload",
        data={"csrf_token": csrf},
        files={
            "file": (
                "big.xlsx",
                blob,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        },
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 413
    # The body is the upload-form fragment with an inline error alert.
    assert "alert--error" in response.text
    assert "upload size limit" in response.text
    assert "Upload and Import" in response.text


async def test_post_section_upload_malformed_returns_upload_form_with_error(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _id, email, password = seeded_user
    csrf = await _login_and_get_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/data-import/section/upload",
        data={"csrf_token": csrf},
        files={
            "file": (
                "bad.xlsx",
                b"this is not an excel file",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        },
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    # Upload-form fragment with inline error.
    assert "Could not parse" in response.text
    assert "alert--error" in response.text
    # The upload form is still present.
    assert 'name="file"' in response.text
    assert "Upload and Import" in response.text


async def test_post_section_upload_dry_run_format_error_keeps_row(
    web_client: AsyncClient,
    seeded_bootstrap: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parseable-but-not-transformable workbook surfaces an inline error.

    Per the immutable-upload model the ``data_uploads`` row stays
    persisted; only the dry-run preview is unavailable. The route
    falls back to the upload-form fragment with an explanatory inline
    error rather than 500.
    """
    _id, email, password = seeded_bootstrap
    csrf = await _login_and_get_csrf(web_client, email, password)

    async def _raise_format_error(*_args, **_kwargs):
        raise ImportFormatError("attributes sheet missing")

    monkeypatch.setattr(
        "web.routes.data_import._run_dry_run_extraction",
        _raise_format_error,
    )

    response = await web_client.post(
        "/api/data-import/section/upload",
        data={"csrf_token": csrf},
        files={
            "file": (
                "untransformable.xlsx",
                _build_minimal_v2_workbook(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        },
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    body = response.text
    assert "Upload and Import" in body
    assert "alert--error" in body
    assert "untransformable.xlsx" in body
    assert "cannot be imported" in body

    # The row still landed in data_uploads despite the dry-run failure.
    async with fresh_superuser_engine.connect() as conn:
        count = await conn.execute(
            text(
                "SELECT COUNT(*) FROM data_uploads "
                "WHERE tenant_id = :tid AND filename = 'untransformable.xlsx'"
            ),
            {"tid": str(SENTINEL_TENANT_ID)},
        )
        assert count.scalar_one() == 1


# ---------------------------------------------------------------------------
# GET /api/data-import/section/upload/{upload_id}
# ---------------------------------------------------------------------------


async def test_get_section_upload_renders_preview(
    web_client: AsyncClient,
    seeded_bootstrap: tuple[UUID, str, str],
) -> None:
    """Clicking a recent-uploads row swaps the preview into the panel."""
    _id, email, password = seeded_bootstrap
    csrf = await _login_and_get_csrf(web_client, email, password)

    # First, upload a file so we have an id to follow.
    upload = await web_client.post(
        "/api/data-import/section/upload",
        data={"csrf_token": csrf},
        files={
            "file": (
                "revisit.xlsx",
                _build_minimal_v2_workbook(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        },
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert upload.status_code == 200

    # Find the upload id via the section endpoint (the recent-uploads
    # row has hx-get pointing at the section/upload/{id} URL).
    import re

    section = await web_client.get(
        "/api/data-import/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    match = re.search(
        r"/api/data-import/section/upload/([0-9a-f-]{36})",
        section.text,
    )
    assert match is not None
    upload_id = match.group(1)

    response = await web_client.get(
        f"/api/data-import/section/upload/{upload_id}",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "revisit.xlsx" in response.text
    assert "Apply to Investments" in response.text
    assert "preview computed" in response.text


async def test_get_section_upload_404_for_unknown_id(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _id, email, password = seeded_user
    await _login_and_get_csrf(web_client, email, password)

    response = await web_client.get(
        f"/api/data-import/section/upload/{uuid4()}",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


async def test_post_section_upload_writes_audit_row(
    web_client: AsyncClient,
    seeded_bootstrap: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    user_id, email, password = seeded_bootstrap
    csrf = await _login_and_get_csrf(web_client, email, password)

    upload_response = await web_client.post(
        "/api/data-import/section/upload",
        data={"csrf_token": csrf},
        files={
            "file": (
                "audited.xlsx",
                _build_minimal_v2_workbook(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        },
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert upload_response.status_code == 200

    async with fresh_superuser_engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT tenant_id, user_id, table_name, operation
                FROM audit_log
                WHERE table_name = 'data_uploads'
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        )
        row = result.mappings().one()

    assert row["tenant_id"] == SENTINEL_TENANT_ID
    assert row["user_id"] == user_id
    assert row["operation"] == "INSERT"


# ---------------------------------------------------------------------------
# Sunset confirmation — legacy HTML routes are gone
# ---------------------------------------------------------------------------


async def test_legacy_data_import_page_returns_404(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The standalone /data-import HTML page is gone (6F-5 sunset)."""
    _id, email, password = seeded_user
    await _login_and_get_csrf(web_client, email, password)
    response = await web_client.get("/data-import", follow_redirects=False)
    assert response.status_code == 404


async def test_legacy_data_import_detail_returns_404(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The standalone /data-import/{id} HTML page is gone (6F-5 sunset)."""
    _id, email, password = seeded_user
    await _login_and_get_csrf(web_client, email, password)
    response = await web_client.get(f"/data-import/{uuid4()}", follow_redirects=False)
    assert response.status_code == 404
