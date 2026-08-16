# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Web Excel-import surface — HTMX section endpoints + JSON API.

The Phase-2 (sub-stream 2d) Excel-import flow per ADR-0041 §3 is
embedded in ``/admin#data-import`` as a single-button surface
(sub-stream 6F): the stepper-driven two-stage UI was collapsed into
one ``Upload and Import`` action. The standalone HTML pages at
``/data-import`` and ``/data-import/{upload_id}`` were sunset in 6F-5;
this module exposes:

* ``GET  /api/data-import/section``                       — upload-form
  fragment (form + recent uploads).
* ``POST /api/data-import/section/upload``                — multipart
  upload; persists the row, runs the dry-run extractor, and returns
  the preview fragment with projected counts. On parse / sanitisation
  error the upload-form fragment is re-rendered with an inline alert.
* ``GET  /api/data-import/section/upload/{upload_id}``    — preview
  fragment for an existing upload (same dry-run + render path).
* ``POST /api/data-uploads/{upload_id}/import-as-investments`` — JSON
  API; transforms a Phase-2 Excel snapshot into normalised investment
  rows (preserved verbatim from sub-stream 4c).

The shared helper :func:`load_data_import_section_context` is also
imported from ``web/routes/areas.py`` to pre-render the upload form
inside the Admin area page on initial load.

Per ADR-0041, the web Excel-import write path is *separate* from the
GUI's in-memory ``DataStore`` write path. The two surfaces deliberately
do not share data during Phase 2 / 3; convergence to a single
persistence path is Phase-4 work. The parsing function ``load_excel``
is reused (it is persistence-agnostic — it returns
``dict[str, pd.DataFrame]``) but the *write* layer is duplicated.

Validation
----------
- File-size limit: ``WEB_MAX_UPLOAD_SIZE_MB`` env var, default 50.
  Oversized uploads return 413 with a user-facing message.
- MIME type / parseability: the route trusts the actual parse, not the
  declared MIME type — non-Excel content surfaces as a
  :class:`DataImportError` and is rendered as 400.
- Filename sanitisation: ``pathlib.PurePosixPath`` strips path
  components; ``Path`` semantics handle the Windows ``\\`` case via
  the explicit replacement before passing to ``PurePosixPath``.
- Hash-based dedup: SHA-256 over the streamed bytes is compared
  against ``data_uploads.file_hash`` in the active tenant; a collision
  is surfaced as a non-error informational message linking to the
  existing upload.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pathlib
import tempfile
import uuid
from dataclasses import replace
from typing import cast

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    status,
)
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from starlette.datastructures import UploadFile

from core.exceptions import DataImportError, ValidationError
from core.repositories._session import tenant_context
from core.repositories.anlv_category_repository import AnlVCategoryRepository
from core.repositories.asset_class_benchmark_mapping_repository import (
    AssetClassBenchmarkMappingRepository,
)
from core.repositories.asset_class_repository import AssetClassRepository
from core.repositories.benchmark_observation_repository import (
    BenchmarkObservationRepository,
)
from core.repositories.benchmark_repository import BenchmarkRepository
from core.repositories.data_upload_repository import DataUploadRepository
from core.repositories.fx_rate_repository import FxRateRepository
from core.repositories.instrument_price_repository import (
    InstrumentPriceRepository,
)
from core.repositories.investment_bond_analytics_repository import (
    InvestmentBondAnalyticsRepository,
)
from core.repositories.investment_cashflow_repository import (
    InvestmentCashflowRepository,
)
from core.repositories.investment_identifier_repository import (
    InvestmentIdentifierRepository,
)
from core.repositories.investment_maturity_weights_repository import (
    InvestmentMaturityWeightsRepository,
)
from core.repositories.investment_nav_repository import InvestmentNavRepository
from core.repositories.investment_rating_weights_repository import (
    InvestmentRatingWeightsRepository,
)
from core.repositories.investment_region_weights_repository import (
    InvestmentRegionWeightsRepository,
)
from core.repositories.investment_repository import InvestmentRepository
from core.repositories.investment_sector_weights_repository import (
    InvestmentSectorWeightsRepository,
)
from core.repositories.limits_repository import LimitsRepository
from core.repositories.position_transaction_repository import (
    PositionTransactionRepository,
)
from core.repositories.region_repository import RegionRepository
from core.repositories.sector_repository import SectorRepository
from core.repositories.tenant_repository import TenantRepository
from core.repositories.user_repository import UserRepository
from services.data_normalization.excel_workbook_loader import load_excel
from services.auth.session import SessionDTO
from services.data_normalization import (
    ImportFormatError,
    InvestmentExtractionResult,
    InvestmentExtractor,
    UploadNotFoundError,
)
from services.investments import InvestmentService
from web.auth import require_session, verify_csrf
from web.errors import user_safe_error
from web.permissions import require_role

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

_DEFAULT_MAX_UPLOAD_SIZE_MB: int = 50
# The literal "v2" is the persisted database format-version
# identifier (see data_uploads.format_version). It remains
# unchanged for audit-trail integrity per ADR-0059. User-facing
# references to this format use the term "Excel import format".
_FORMAT_VERSION: str = "v2"
_TMP_UPLOAD_DIR_NAME: str = "uploads"
_FILENAME_MAX_LEN: int = 255
_RECENT_UPLOAD_LIMIT: int = 20


def _max_upload_bytes() -> int:
    """Return the configured upload-size cap in bytes.

    Read at request time (rather than at import time) so tests can
    override ``WEB_MAX_UPLOAD_SIZE_MB`` between cases.
    """
    raw = os.getenv("WEB_MAX_UPLOAD_SIZE_MB")
    try:
        mb = int(raw) if raw is not None else _DEFAULT_MAX_UPLOAD_SIZE_MB
    except ValueError:
        mb = _DEFAULT_MAX_UPLOAD_SIZE_MB
    return max(1, mb) * 1024 * 1024


# ---------------------------------------------------------------------------
# Helper accessors
# ---------------------------------------------------------------------------


def _templates(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)


def _engine(request: Request) -> AsyncEngine:
    engine = request.app.state.engine
    if engine is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database engine is not configured.",
        )
    return cast(AsyncEngine, engine)


def _tmp_upload_dir(request: Request) -> pathlib.Path:
    """Return (and lazily create) the temp directory for streamed uploads.

    Located under the system temp directory rather than the project
    tree to avoid accidentally checking partial uploads into git and
    to match the operator's expectation that ``data/`` holds curated
    fixtures, not transient bytes.
    """
    cached = getattr(request.app.state, "upload_tmp_dir", None)
    if cached is not None:
        return cast(pathlib.Path, cached)
    base = pathlib.Path(tempfile.gettempdir()) / "portfoliflow" / _TMP_UPLOAD_DIR_NAME
    base.mkdir(parents=True, exist_ok=True)
    request.app.state.upload_tmp_dir = base
    return base


def _sanitise_filename(raw: str | None) -> str:
    """Return a safe basename for ``raw``.

    Strips path components (both POSIX and Windows separators), trims
    whitespace, caps length, and rejects empty results. The sanitised
    string is what we persist on the row — the original bytes never
    touch the filesystem in their raw form.
    """
    if not raw:
        return ""
    # Normalise Windows-style separators before basename extraction.
    candidate = raw.replace("\\", "/").strip()
    base = pathlib.PurePosixPath(candidate).name.strip()
    if not base:
        return ""
    return base[:_FILENAME_MAX_LEN]


# ---------------------------------------------------------------------------
# Shared section-context loader
#
# The Data Import section is embedded in /front-office#data-import
# (sub-stream 6F-5). Both the area route and the HTMX section
# endpoints need the same context shape: the configured upload-size
# cap, the recent-uploads list, and the resolved uploader emails. The
# helper below is the single source of truth.
# ---------------------------------------------------------------------------


async def load_data_import_section_context(
    request: Request,
    session: SessionDTO,
    *,
    info: str | None = None,
    error: str | None = None,
    duplicate_id: str | None = None,
) -> dict:
    """Return the context dict for the embedded Data Import section.

    Args:
        request: Active FastAPI request — used to reach the DB engine
            and the configured upload cap.
        session: Authenticated session DTO.
        info: Optional informational message rendered inside the
            Stage 1 alert strip.
        error: Optional error message rendered inside the Stage 1
            alert strip.
        duplicate_id: When ``info`` reports a deduplicated upload, the
            string id of the existing record — used by Stage 1 to
            render a "View existing upload" link.

    Returns:
        Context keys required by ``_partials/data_import_section.html``
        and its Stage 1 child partial: ``csrf_token``,
        ``max_upload_mb``, ``recent_uploads``, ``uploader_emails``,
        ``info``, ``error``, ``duplicate_id``.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        # No touch here (ADR-0065 §3): the idle timer is reset by
        # require_authenticated_session in the gated dependency chain.
        recent = await DataUploadRepository(db).list_recent(limit=_RECENT_UPLOAD_LIMIT)
        uploader_emails: dict[str, str] = {}
        for upload in recent:
            key = str(upload.uploaded_by)
            if key in uploader_emails:
                continue
            u = await UserRepository(db).get_by_id(upload.uploaded_by)
            uploader_emails[key] = u.email if u else ""

    return {
        "csrf_token": session.csrf_token,
        "max_upload_mb": _max_upload_bytes() // (1024 * 1024),
        "recent_uploads": recent,
        "uploader_emails": uploader_emails,
        "info": info,
        "error": error,
        "duplicate_id": duplicate_id,
    }


# ---------------------------------------------------------------------------
# Stream-to-disk helper (shared by the section upload handler)
# ---------------------------------------------------------------------------


async def _stream_to_disk_with_hash(
    upload: UploadFile,
    destination: pathlib.Path,
    max_bytes: int,
) -> tuple[str, int]:
    """Stream ``upload`` to ``destination`` while computing SHA-256.

    Aborts with HTTPException 413 if the streamed size exceeds
    ``max_bytes``. The partial file is removed before raising so the
    temp directory does not grow without bound.

    Returns:
        ``(file_hash_hex, size_bytes)`` on success.
    """
    digest = hashlib.sha256()
    written = 0
    chunk_size = 64 * 1024

    try:
        with destination.open("wb") as out:
            while True:
                chunk = await upload.read(chunk_size)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(
                        # HTTP 413: the WG renamed the title from
                        # "Request Entity Too Large" to "Content Too
                        # Large" in RFC 9110 (2022). Newer Starlette
                        # status modules expose both names; the
                        # numeric code is what matters on the wire.
                        status_code=413,
                        detail=(
                            f"File exceeds the {max_bytes // (1024 * 1024)} MB upload size limit."
                        ),
                    )
                digest.update(chunk)
                out.write(chunk)
    except HTTPException:
        # Best-effort cleanup; missing-file is tolerated.
        destination.unlink(missing_ok=True)
        raise
    except Exception:
        destination.unlink(missing_ok=True)
        raise

    return digest.hexdigest(), written


# ---------------------------------------------------------------------------
# HTMX section endpoints (sub-stream 6F)
#
# The Data Import workflow is embedded in /admin#data-import as a
# single-button surface. The upload-form fragment is the entry point;
# the preview fragment is swapped in after a successful upload
# (server-rendered counts from the dry-run extraction). The remaining
# client-side action is the destructive "Apply to Investments" POST.
# These endpoints serve HTML fragments (not JSON) for in-place swap
# into ``.pf-data-import__panel``; the JSON API below keeps the
# resource slug ``data-uploads``.
# ---------------------------------------------------------------------------


def _render_section_root(
    request: Request,
    context: dict,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    """Render the full section root (panel + initial upload form)."""
    templates = _templates(request)
    response = templates.TemplateResponse(
        request,
        "_partials/data_import_section.html",
        context,
        status_code=status_code,
    )
    return cast(HTMLResponse, response)


def _render_upload_form_body(
    request: Request,
    context: dict,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    """Render just the upload-form fragment (without the panel wrapper).

    Used by the POST upload handler on error: the section root is
    already in the DOM, so HTMX swaps in only the form body.
    """
    templates = _templates(request)
    response = templates.TemplateResponse(
        request,
        "_partials/data_import_upload_form.html",
        context,
        status_code=status_code,
    )
    return cast(HTMLResponse, response)


async def _render_preview_body(
    request: Request,
    session: SessionDTO,
    upload_id: uuid.UUID,
) -> HTMLResponse:
    """Render the preview fragment (upload metadata + dry-run counts).

    Opens one tenant-scoped session, fetches the upload row + sheets +
    uploader, then runs the dry-run extraction via
    :func:`_run_dry_run_extraction` to project the counts the template
    renders. Raises ``HTTPException(404)`` when the upload is not
    visible in the active tenant.

    If the dry-run fails with :class:`ImportFormatError` — i.e. the
    workbook parsed cleanly but cannot be transformed — the row stays
    persisted (per the immutable-upload model) and the upload-form
    fragment is rendered with an inline error explaining the file is
    stored but not importable.
    """
    engine = _engine(request)
    templates = _templates(request)
    format_error: str | None = None
    upload_filename = ""
    upload = None
    sheets: list = []
    uploader = None
    result: InvestmentExtractionResult | None = None

    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        # No touch here (ADR-0065 §3). This was the T2 that blocked on
        # the retired request-scoped T1's sessions-row lock and hung the
        # upload route forever.
        repo = DataUploadRepository(db)
        upload = await repo.get_by_id(upload_id)
        if upload is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Upload not found.",
            )
        upload_filename = upload.filename
        sheets = await repo.get_sheets(upload_id)
        uploader = await UserRepository(db).get_by_id(upload.uploaded_by)
        try:
            result = await _run_dry_run_extraction(db, upload_id, session.user_id)
        except ImportFormatError as exc:
            logger.info(
                "data-import preview: import-format error on %s: %s",
                upload_id,
                exc,
            )
            user_msg, _error_id = user_safe_error(exc)
            format_error = user_msg or "Excel snapshot has invalid structure."

    if format_error is not None:
        ctx = await load_data_import_section_context(
            request,
            session,
            error=(f"'{upload_filename}' was stored, but cannot be imported: {format_error}"),
        )
        return _render_upload_form_body(request, ctx, status_code=400)

    response = templates.TemplateResponse(
        request,
        "_partials/data_import_preview.html",
        {
            "csrf_token": session.csrf_token,
            "upload": upload,
            "uploader_email": uploader.email if uploader is not None else "",
            "sheets": sheets,
            "counts": _import_result_payload(result),
        },
    )
    return cast(HTMLResponse, response)


@router.get(
    "/api/data-import/section",
    response_class=HTMLResponse,
)
async def get_data_import_section(
    request: Request,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Return the section root (panel + initial upload-form fragment)."""
    context = await load_data_import_section_context(request, session)
    return _render_section_root(request, context)


@router.post(
    "/api/data-import/section/upload",
    response_class=HTMLResponse,
    dependencies=[Depends(require_role("owner"))],
)
async def post_data_import_section_upload(
    request: Request,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Accept a multipart upload and swap in the preview fragment.

    Reuses ``_stream_to_disk_with_hash``, dedup, parsing and
    :meth:`DataUploadRepository.create_upload` from the standalone
    POST handler, then runs the dry-run extractor on the freshly
    persisted row via :func:`_render_preview_body`. On success the
    response body is the preview fragment with the projected counts
    server-rendered. On parse / sanitisation error the response body
    is the upload-form fragment with an inline alert.
    """
    max_bytes = _max_upload_bytes()
    engine = _engine(request)

    # ``verify_csrf`` parses the multipart form to read the CSRF token
    # and caches it on ``request.state.form`` so we can reuse the parse
    # here. Going via ``request.form()`` directly would either trigger
    # a second parse or fail outright depending on Starlette's caching
    # behaviour — keep this read-through pattern intact.
    form = getattr(request.state, "form", None)
    if form is None:
        form = await request.form()
    file_field = form.get("file")
    if not isinstance(file_field, UploadFile):
        ctx = await load_data_import_section_context(
            request,
            session,
            error="No file was attached. Choose an .xlsx file and try again.",
        )
        return _render_upload_form_body(request, ctx, status_code=400)

    sanitised_name = _sanitise_filename(file_field.filename)
    if not sanitised_name:
        ctx = await load_data_import_section_context(
            request,
            session,
            error="Filename is empty after sanitisation.",
        )
        return _render_upload_form_body(request, ctx, status_code=400)

    tmp_dir = _tmp_upload_dir(request)
    tmp_path = tmp_dir / f"{uuid.uuid4().hex}.xlsx"

    try:
        try:
            file_hash, size_bytes = await _stream_to_disk_with_hash(file_field, tmp_path, max_bytes)
        except HTTPException as exc:
            if exc.status_code == 413:
                ctx = await load_data_import_section_context(
                    request, session, error=str(exc.detail)
                )
                return _render_upload_form_body(request, ctx, status_code=413)
            raise

        async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
            existing = await DataUploadRepository(db).get_by_hash(file_hash)
        if existing is not None:
            tmp_path.unlink(missing_ok=True)
            # Dedup: surface the existing upload's preview directly so
            # the operator lands on the inspect-then-commit view
            # without re-uploading.
            return await _render_preview_body(request, session, existing.id)

        try:
            sheets = load_excel(tmp_path)
        except (DataImportError, ValidationError) as exc:
            logger.info(
                "data-import: parse rejected %s: %s",
                sanitised_name,
                exc,
            )
            user_msg, _error_id = user_safe_error(exc)
            ctx = await load_data_import_section_context(
                request,
                session,
                error=f"Could not parse '{sanitised_name}': {user_msg}",
            )
            return _render_upload_form_body(request, ctx, status_code=400)

        async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
            created = await DataUploadRepository(db).create_upload(
                uploaded_by=session.user_id,
                filename=sanitised_name,
                file_hash=file_hash,
                size_bytes=size_bytes,
                format_version=_FORMAT_VERSION,
                sheets=sheets,
            )

        logger.info(
            "data-import: uploaded %s (%d bytes, %d sheets) as %s",
            sanitised_name,
            size_bytes,
            len(sheets),
            created.id,
        )
        return await _render_preview_body(request, session, created.id)
    finally:
        tmp_path.unlink(missing_ok=True)


@router.get(
    "/api/data-import/section/upload/{upload_id}",
    response_class=HTMLResponse,
)
async def get_data_import_section_upload(
    upload_id: uuid.UUID,
    request: Request,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Return the preview fragment for one upload.

    Triggered when the operator clicks a row in the recent-uploads
    table or follows the "View existing upload" link after a dedup.
    The dry-run extraction is re-run on each visit; the
    ``data_uploads`` row is immutable, so the projected counts depend
    only on the current investment-domain state and are always
    fresh.
    """
    return await _render_preview_body(request, session, upload_id)


# ---------------------------------------------------------------------------
# POST /api/data-uploads/{upload_id}/import-as-investments
#
# Sub-stream 4c (ADR-0043 §3) — async transformation of a previously
# uploaded Excel snapshot into normalised investment-domain rows.
#
# Body / query: optional ``?dry_run=true`` runs the extractor and
# reports the projected counts without issuing any DB write — used by
# the UI for a confirm-before-write preview.
# ---------------------------------------------------------------------------


async def _run_dry_run_extraction(
    db: AsyncSession,
    upload_id: uuid.UUID,
    user_id: uuid.UUID,
) -> InvestmentExtractionResult:
    """Run a dry-run investment extraction with the canonical wiring.

    Single source of truth for the dependency graph the
    ``import-as-investments`` flow needs: three Investment-domain
    repositories on :class:`InvestmentService`, plus the asset-class /
    region / sector / weights repositories and the
    :class:`InvestmentExtractor` passed as keyword arguments. Used by
    both the embedded upload-then-preview handler (which always runs
    the dry-run after :meth:`DataUploadRepository.create_upload`) and
    the JSON :func:`post_import_upload_as_investments` endpoint when
    invoked with ``?dry_run=true``. The write branch
    (``?dry_run=false``) keeps its own explicit wiring inline so the
    commit call site stays self-contained.

    Args:
        db: Tenant-scoped :class:`AsyncSession` opened by the caller.
        upload_id: Row in ``data_uploads`` visible in the active
            tenant.
        user_id: Acting user; threaded into the service call for
            audit-log binding.

    Returns:
        The :class:`InvestmentExtractionResult` reported by
        :meth:`InvestmentService.transform_upload_to_investments`.

    Raises:
        UploadNotFoundError: If ``upload_id`` is not visible in the
            active tenant.
        ImportFormatError: If the Excel snapshot is structurally
            invalid.
    """
    service = InvestmentService(
        investments=InvestmentRepository(db),
        navs=InvestmentNavRepository(db),
        cashflows=InvestmentCashflowRepository(db),
        position_transactions=PositionTransactionRepository(db),
        instrument_prices=InstrumentPriceRepository(db),
    )
    return await service.transform_upload_to_investments(
        upload_id,
        user_id=user_id,
        asset_class_repository=AssetClassRepository(db),
        data_upload_repository=DataUploadRepository(db),
        extractor=InvestmentExtractor(),
        region_repository=RegionRepository(db),
        sector_repository=SectorRepository(db),
        region_weights_repository=InvestmentRegionWeightsRepository(db),
        sector_weights_repository=InvestmentSectorWeightsRepository(db),
        investment_identifier_repository=InvestmentIdentifierRepository(db),
        bond_analytics_repository=InvestmentBondAnalyticsRepository(db),
        rating_weights_repository=InvestmentRatingWeightsRepository(db),
        maturity_weights_repository=InvestmentMaturityWeightsRepository(db),
        anlv_category_repository=AnlVCategoryRepository(db),
        limits_repository=LimitsRepository(db),
        dry_run=True,
    )


def _import_result_payload(result, benchmark_result=None, fx_result=None) -> dict:
    """Project an :class:`InvestmentExtractionResult` to a JSON dict.

    When ``benchmark_result`` is supplied (write branch only — the
    dry-run path does not exercise the benchmark transformer per
    ADR-0061 §Decision), four additional fields summarise the
    benchmark persistence side-effects: ``benchmarks_created``,
    ``benchmark_observations_inserted``, ``benchmark_mappings_created``,
    and ``benchmark_warnings`` (a list of dicts mirroring the
    investment-side warning shape).

    When ``fx_result`` is supplied (write branch only — the ``FX rates``
    sheet is persisted per ADR-0099 §5), three further fields summarise
    the FX persistence side-effects: ``fx_rates_created`` (rows
    upserted), ``fx_currencies`` (the base currencies seen), and
    ``fx_warnings`` (row-level errors).
    """
    payload = {
        "investments_created": result.investments_created,
        "investments_updated": result.investments_updated,
        "investments_deactivated": result.investments_deactivated,
        "investments_reactivated": result.investments_reactivated,
        "navs_replaced": result.navs_replaced,
        "cashflows_replaced": result.cashflows_replaced,
        "region_weights_replaced": result.region_weights_replaced,
        "sector_weights_replaced": result.sector_weights_replaced,
        "bond_analytics_replaced": result.bond_analytics_replaced,
        "rating_weights_replaced": result.rating_weights_replaced,
        "maturity_weights_replaced": result.maturity_weights_replaced,
        # Cash statement path (ADR-0103 §3/§4). All zero for a workbook
        # without a ``Cash`` sheet — and, on a re-import that changed
        # nothing, for one with it: these are deltas, not replace-counts.
        "cash_statement_rows": result.cash_statement_rows,
        "cash_ledger_inserted": result.cash_ledger_inserted,
        "cash_ledger_updated": result.cash_ledger_updated,
        "cash_ledger_deleted": result.cash_ledger_deleted,
        "cash_prices_written": result.cash_prices_written,
        "cash_prices_deleted": result.cash_prices_deleted,
        "errors": [
            {
                "investment_name": err.investment_name,
                "sheet": err.sheet,
                "row_index": err.row_index,
                "column": err.column,
                "message": err.message,
            }
            for err in result.errors
        ],
        "warnings": [
            {
                "investment_name": w.investment_name,
                "field": w.field,
                "raw_value": w.raw_value,
                "action": w.action,
                "message": w.message,
            }
            for w in result.warnings
        ],
    }
    if benchmark_result is not None:
        payload["benchmarks_created"] = benchmark_result.n_benchmarks
        payload["benchmark_observations_inserted"] = benchmark_result.n_observations
        payload["benchmark_mappings_created"] = benchmark_result.n_mappings
        payload["benchmark_warnings"] = [
            {
                "sheet": w.sheet,
                "row_index": w.row_index,
                "column": w.column,
                "message": w.message,
            }
            for w in benchmark_result.warnings
        ]
    if fx_result is not None:
        payload["fx_rates_created"] = fx_result.n_rates
        payload["fx_currencies"] = list(fx_result.currencies)
        payload["fx_warnings"] = [
            {
                "sheet": w.sheet,
                "row_index": w.row_index,
                "column": w.column,
                "message": w.message,
            }
            for w in fx_result.warnings
        ]
    return payload


@router.post(
    "/api/data-uploads/{upload_id}/import-as-investments",
    dependencies=[Depends(require_role("owner"))],
)
async def post_import_upload_as_investments(
    request: Request,
    upload_id: uuid.UUID,
    dry_run: bool = False,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> Response:
    """Transform a Phase-2 Excel upload into normalised investment rows.

    Per ADR-0043 §3 the route delegates to
    :meth:`InvestmentService.transform_upload_to_investments` —
    replace-by-investment plus soft-delete-with-reactivation, all in
    a single tenant-scoped transaction. The body is empty;
    ``?dry_run=true`` returns the projected counts (and any
    row-level errors the extractor would surface) without writing.

    Status codes:
        * 200 — transformation (or dry-run) succeeded; body is the
          structured :class:`InvestmentExtractionResult`.
        * 400 — Excel snapshot is structurally invalid
          (:class:`ImportFormatError`).
        * 404 — ``upload_id`` is not visible in the active tenant.
    """
    from fastapi.responses import JSONResponse

    engine = _engine(request)

    benchmark_result = None
    fx_result = None
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        try:
            if dry_run:
                result = await _run_dry_run_extraction(db, upload_id, session.user_id)
            else:
                service = InvestmentService(
                    investments=InvestmentRepository(db),
                    navs=InvestmentNavRepository(db),
                    cashflows=InvestmentCashflowRepository(db),
                    position_transactions=PositionTransactionRepository(db),
                    instrument_prices=InstrumentPriceRepository(db),
                )
                result = await service.transform_upload_to_investments(
                    upload_id,
                    user_id=session.user_id,
                    asset_class_repository=AssetClassRepository(db),
                    data_upload_repository=DataUploadRepository(db),
                    extractor=InvestmentExtractor(),
                    region_repository=RegionRepository(db),
                    sector_repository=SectorRepository(db),
                    region_weights_repository=(InvestmentRegionWeightsRepository(db)),
                    sector_weights_repository=(InvestmentSectorWeightsRepository(db)),
                    investment_identifier_repository=(InvestmentIdentifierRepository(db)),
                    bond_analytics_repository=(InvestmentBondAnalyticsRepository(db)),
                    rating_weights_repository=(InvestmentRatingWeightsRepository(db)),
                    maturity_weights_repository=(InvestmentMaturityWeightsRepository(db)),
                    anlv_category_repository=AnlVCategoryRepository(db),
                    limits_repository=LimitsRepository(db),
                    dry_run=False,
                )

                # Persist benchmark sheets (sofern in der Workbook
                # vorhanden) into the Phase-7 benchmark tables.
                # Idempotent per ADR-0061 §Decision; a re-upload of
                # the same workbook produces the same final state.
                # The service returns ``BenchmarkImportResult(0, 0, 0,
                # [])`` defensively when no benchmark sheets are
                # present, so no upfront check is needed.
                benchmark_result = await service.transform_benchmarks_from_upload(
                    upload_id,
                    user_id=session.user_id,
                    data_upload_repository=DataUploadRepository(db),
                    asset_class_repository=AssetClassRepository(db),
                    benchmark_repository=BenchmarkRepository(db),
                    benchmark_observation_repository=(BenchmarkObservationRepository(db)),
                    mapping_repository=(AssetClassBenchmarkMappingRepository(db)),
                )

                # Persist the FX rates sheet (if present) into the
                # ``fx_rates`` table (ADR-0099 §5). Idempotent and
                # optional: the service returns a zero-result when the
                # workbook has no ``FX rates`` sheet, so no upfront check
                # is needed. A malformed-header ValidationError surfaces
                # to the operator via the shared 400 branch below, exactly
                # like the benchmark-mapping validation errors.
                fx_result = await service.transform_fx_rates_from_upload(
                    upload_id,
                    user_id=session.user_id,
                    data_upload_repository=DataUploadRepository(db),
                    fx_rate_repository=FxRateRepository(db),
                )

                # Reconcile the (optional) AUM sheet against Σ NAV
                # (ADR-0103 §3). The sheet no longer persists — it is a
                # control: each stated figure is compared against the book,
                # and a deviation beyond the NAV quantum surfaces as an
                # import warning. It runs **after** the FX transform on
                # purpose, so the comparison can see this workbook's own
                # rates; and after the investments transform, so it sees
                # this workbook's own NAVs. Nothing is written, nothing
                # raises: the findings merge into the displayed result.
                aum_warnings = await service.reconcile_aum_sheet(
                    upload_id,
                    data_upload_repository=DataUploadRepository(db),
                    tenant_repository=TenantRepository(db),
                    fx_rate_repository=FxRateRepository(db),
                )
                result = replace(result, warnings=result.warnings + aum_warnings)
        except UploadNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Upload not found.",
            )
        except ImportFormatError as exc:
            logger.info(
                "import-as-investments: structural failure on %s: %s",
                upload_id,
                exc,
            )
            user_msg, _error_id = user_safe_error(exc)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=user_msg or "Excel snapshot has invalid structure.",
            )
        except ValidationError as exc:
            # ADR-0061 §Decision: unknown asset-class codes in the
            # Benchmark Mapping sheet (and weights outside [0, 1]) are
            # hard import errors. Surface as 400 with the actionable
            # operator message; the investment-side transformation
            # already committed in the same transaction is rolled back
            # by ``tenant_context``.
            logger.info(
                "import-as-investments: benchmark validation failed on %s: %s",
                upload_id,
                exc,
            )
            user_msg, _error_id = user_safe_error(exc)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=user_msg,
            )

    logger.info(
        "import-as-investments: tenant=%s user=%s upload=%s "
        "dry_run=%s created=%d updated=%d deactivated=%d "
        "reactivated=%d navs=%d cashflows=%d region_weights=%d "
        "sector_weights=%d errors=%d warnings=%d benchmarks=%d "
        "benchmark_observations=%d benchmark_mappings=%d "
        "benchmark_warnings=%d fx_rates=%d fx_warnings=%d",
        session.tenant_id,
        session.user_id,
        upload_id,
        dry_run,
        result.investments_created,
        result.investments_updated,
        result.investments_deactivated,
        result.investments_reactivated,
        result.navs_replaced,
        result.cashflows_replaced,
        result.region_weights_replaced,
        result.sector_weights_replaced,
        len(result.errors),
        len(result.warnings),
        benchmark_result.n_benchmarks if benchmark_result else 0,
        benchmark_result.n_observations if benchmark_result else 0,
        benchmark_result.n_mappings if benchmark_result else 0,
        len(benchmark_result.warnings) if benchmark_result else 0,
        fx_result.n_rates if fx_result else 0,
        len(fx_result.warnings) if fx_result else 0,
    )
    return JSONResponse(
        content={
            "dry_run": dry_run,
            **_import_result_payload(result, benchmark_result, fx_result),
        }
    )
