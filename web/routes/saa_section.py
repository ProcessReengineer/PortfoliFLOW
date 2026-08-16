# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""SAA section surface — embedded in /back-office#saa.

Roadmap A5 (ADR-0054) lifts the SAA workflow from the standalone
``/saa`` pages into a section inside the Back Office area. The
endpoints under this module live at ``/api/saa/*`` and return HTML
partials (no full ``base.html`` layout) or JSON for mutations.

Endpoints
---------

* ``GET    /api/saa/section`` — Lazy-load the section. Returns the
  picker drawer plus the active (or query-param-pinned) configuration
  body, or an empty-state partial when the tenant has no configs.
* ``GET    /api/saa/configuration/{id}`` — Render only the
  configuration body (header + inputs + correlations + run-button)
  for picker switches.
* ``GET    /api/saa/configuration/{id}/optimization`` — Run-optimisation
  HTMX partial (chart + weights tabulator).
* ``PUT    /api/saa/configuration/{id}`` — Atomic save of metadata,
  inputs, and correlations.
* ``POST   /api/saa/configuration`` — Create a new configuration.
* ``POST   /api/saa/configuration/{id}/activate`` — Make this the
  tenant's active configuration.
* ``DELETE /api/saa/configuration/{id}`` — Hard-delete.
* ``GET    /api/saa/asset-classes`` — Modal partial for the asset-class
  catalogue.
* ``POST   /api/saa/asset-classes`` — Create an asset class.
* ``PUT    /api/saa/asset-classes/{id}`` — Update an asset class.
* ``DELETE /api/saa/asset-classes/{id}`` — Delete (409 when in use).

Mutations signal frontend state changes via ``HX-Trigger`` headers
rather than ``HX-Redirect`` — the section stays inside the
``/back-office`` shell and the front-end coordinates partial swaps
locally.
"""

from __future__ import annotations

import logging
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core.exceptions import ValidationError
from core.repositories._session import tenant_context
from core.repositories.asset_class_repository import AssetClassRepository
from core.repositories.saa_asset_class_input_repository import (
    SAAAssetClassInputRepository,
)
from core.repositories.saa_configuration_repository import (
    SAAConfigurationRepository,
)
from core.repositories.saa_correlation_repository import (
    SAACorrelationRepository,
)
from services.auth.session import SessionDTO
from services.chart_specs import build_efficient_frontier_spec
from services.saa import (
    SAAAssetClassInputSpec,
    SAACorrelationSpec,
    SAAService,
    SAAValidationError,
)
from web.auth import require_session, verify_csrf
from web.permissions import require_role

logger = logging.getLogger(__name__)
router = APIRouter()


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


def _build_service(session: AsyncSession) -> SAAService:
    """Construct an :class:`SAAService` against a tenant-scoped session."""
    return SAAService(
        configurations=SAAConfigurationRepository(session),
        asset_classes=AssetClassRepository(session),
        inputs=SAAAssetClassInputRepository(session),
        correlations=SAACorrelationRepository(session),
    )


def _detail_payload(detail) -> dict[str, object]:
    """Project an :class:`SAAConfigurationDetailDTO` to a JSON-friendly shape."""
    return {
        "configuration": {
            "id": str(detail.configuration.id),
            "name": detail.configuration.name,
            "is_active": detail.configuration.is_active,
            "risk_free_rate": detail.configuration.risk_free_rate,
            "n_frontier_points": detail.configuration.n_frontier_points,
            "updated_at": detail.configuration.updated_at.isoformat(),
        },
        "inputs": [
            {
                "id": str(row.id),
                "asset_class_id": str(row.asset_class_id),
                "expected_return": row.expected_return,
                "volatility": row.volatility,
                "min_weight": row.min_weight,
                "max_weight": row.max_weight,
            }
            for row in detail.inputs
        ],
        "correlations": [
            {
                "asset_class_a_id": str(row.asset_class_a_id),
                "asset_class_b_id": str(row.asset_class_b_id),
                "correlation": row.correlation,
            }
            for row in detail.correlations
        ],
    }


def _validation_error_response(exc: SAAValidationError) -> JSONResponse:
    """Render a typed :class:`SAAValidationError` as a 400 JSON body."""
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "error": exc.message,
            "field": exc.field,
            "row_index": exc.row_index,
        },
    )


def _build_configuration_context(
    detail,
    asset_classes,
    csrf_token: str,
) -> dict[str, object]:
    """Build the context dict consumed by ``saa_configuration_partial.html``.

    Shared between the section endpoint (which inlines the
    configuration partial) and the configuration-switch endpoint.
    """
    asset_class_lookup = {
        str(ac.id): {
            "id": str(ac.id),
            "code": ac.code,
            "display_name": ac.display_name,
        }
        for ac in asset_classes
    }
    inputs_payload = [
        {
            "id": str(row.id),
            "asset_class_id": str(row.asset_class_id),
            "expected_return": row.expected_return,
            "volatility": row.volatility,
            "min_weight": row.min_weight,
            "max_weight": row.max_weight,
        }
        for row in detail.inputs
    ]
    correlations_payload = [
        {
            "asset_class_a_id": str(row.asset_class_a_id),
            "asset_class_b_id": str(row.asset_class_b_id),
            "correlation": row.correlation,
        }
        for row in detail.correlations
    ]
    return {
        "csrf_token": csrf_token,
        "current_config": {
            "id": str(detail.configuration.id),
            "name": detail.configuration.name,
            "is_active": detail.configuration.is_active,
            "risk_free_rate": detail.configuration.risk_free_rate,
            "n_frontier_points": detail.configuration.n_frontier_points,
        },
        "inputs": inputs_payload,
        "correlations": correlations_payload,
        "asset_class_lookup": asset_class_lookup,
        "asset_class_count": len(detail.inputs),
    }


def _pick_configuration_to_render(
    configurations,
    requested_id: UUID | None,
) -> object | None:
    """Pick which configuration the section should land on.

    Order of preference: ``requested_id`` (from the optional
    ``config_id`` query param, used for deep-linking from URL
    fragments) — falling back to the active configuration — falling
    back to the most-recently-updated configuration. Returns ``None``
    when the tenant has no configurations.
    """
    if not configurations:
        return None
    if requested_id is not None:
        for config in configurations:
            if config.id == requested_id:
                return config
    for config in configurations:
        if config.is_active:
            return config
    # ``list_configurations`` orders by updated_at desc per repository
    # contract — first row is the freshest non-active fallback.
    return configurations[0]


# ---------------------------------------------------------------------------
# GET /api/saa/section — lazy-loaded section body
# ---------------------------------------------------------------------------


@router.get("/api/saa/section", response_class=HTMLResponse)
async def saa_section(
    request: Request,
    config_id: UUID | None = None,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Render the SAA section for the Back Office area shell.

    Returns the picker drawer plus the picked configuration's body in
    one response. The optional ``config_id`` query parameter pins the
    section to a specific configuration for URL-fragment deep-linking
    (``/back-office#saa-config-{uuid}`` is resolved client-side and
    triggers a re-fetch with this parameter set).

    When the tenant has no configurations, returns the empty-state
    partial inside the section wrapper so the operator can create the
    first configuration directly from the section.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        service = _build_service(db_session)
        configurations = await service.list_configurations()
        picked = _pick_configuration_to_render(configurations, config_id)
        detail = None
        asset_classes: list = []
        if picked is not None:
            detail = await service.get_configuration_full(picked.id)
            asset_classes = await service.list_asset_classes()

    configurations_payload = [
        {
            "id": str(config.id),
            "name": config.name,
            "is_active": config.is_active,
        }
        for config in configurations
    ]

    context: dict[str, object] = {
        "csrf_token": session.csrf_token,
        "configurations": configurations_payload,
        "has_active_config": detail is not None,
    }
    if detail is not None:
        context.update(_build_configuration_context(detail, asset_classes, session.csrf_token))

    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/saa_section.html",
            context,
        ),
    )


# ---------------------------------------------------------------------------
# GET /api/saa/configuration/{config_id} — picker switch
# ---------------------------------------------------------------------------


@router.get(
    "/api/saa/configuration/{config_id}",
    response_class=HTMLResponse,
)
async def saa_configuration_partial(
    request: Request,
    config_id: UUID,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Render only the configuration body for a picker switch.

    HTMX swaps this partial into the section's ``#saa-config-body``
    container without disturbing the picker drawer above.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        service = _build_service(db_session)
        detail = await service.get_configuration_full(config_id)
        if detail is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Configuration not found.",
            )
        asset_classes = await service.list_asset_classes()

    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/saa_configuration_partial.html",
            _build_configuration_context(detail, asset_classes, session.csrf_token),
        ),
    )


# ---------------------------------------------------------------------------
# POST /api/saa/configuration — create configuration
# ---------------------------------------------------------------------------


@router.post("/api/saa/configuration", dependencies=[Depends(require_role("owner"))])
async def saa_create_configuration(
    request: Request,
    name: str = Form(..., min_length=1, max_length=200),
    risk_free_rate_pct: float = Form(..., ge=-5.0, le=20.0),
    n_frontier_points: int = Form(..., ge=20, le=500),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> JSONResponse:
    """Create an empty configuration and signal the frontend to switch to it.

    Returns JSON carrying the new ``config_id`` plus an
    ``HX-Trigger: pf:saa-config-created`` header. The frontend
    listens for the event and re-fetches the section with the new
    id pinned via ``?config_id={uuid}``.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        service = _build_service(db_session)
        config = await service.create_configuration(
            name=name.strip(),
            risk_free_rate=risk_free_rate_pct / 100.0,
            n_frontier_points=n_frontier_points,
            created_by=session.user_id,
        )
    logger.info(
        "saa-create: tenant=%s user=%s name=%r id=%s",
        session.tenant_id,
        session.user_id,
        config.name,
        config.id,
    )
    return JSONResponse(
        content={
            "id": str(config.id),
            "name": config.name,
            "is_active": config.is_active,
        },
        headers={"HX-Trigger": "pf:saa-config-created"},
    )


# ---------------------------------------------------------------------------
# PUT /api/saa/configuration/{config_id} — atomic save
# ---------------------------------------------------------------------------


@router.put("/api/saa/configuration/{config_id}", dependencies=[Depends(require_role("owner"))])
async def saa_save(
    request: Request,
    config_id: UUID,
    payload: dict,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> JSONResponse:
    """Save the full SAA configuration state in one atomic operation.

    Body shape::

        {
          "metadata": {
            "name": "...",
            "risk_free_rate": 0.025,
            "n_frontier_points": 100
          },
          "inputs": [
            {"asset_class_id": "uuid", "expected_return": 0.075,
             "volatility": 0.15, "min_weight": 0.05, "max_weight": 0.25}
          ],
          "correlations": [
            {"asset_class_a_id": "uuid", "asset_class_b_id": "uuid",
             "correlation": 0.65}
          ]
        }

    Per ADR-0042 §4 this is the sole authority for persisting SAA
    state. Validation runs in the service layer; a
    :class:`SAAValidationError` becomes a structured 400 the front-end
    can attach to the offending row.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        service = _build_service(db_session)
        existing = await service.get_configuration(config_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Configuration not found.",
            )

        metadata = payload.get("metadata") or {}
        inputs_raw = payload.get("inputs") or []
        correlations_raw = payload.get("correlations") or []

        try:
            metadata_kwargs: dict[str, object] = {}
            if "name" in metadata and metadata["name"] is not None:
                metadata_kwargs["name"] = str(metadata["name"]).strip()
            if "risk_free_rate" in metadata and metadata["risk_free_rate"] is not None:
                metadata_kwargs["risk_free_rate"] = float(metadata["risk_free_rate"])
            if "n_frontier_points" in metadata and metadata["n_frontier_points"] is not None:
                metadata_kwargs["n_frontier_points"] = int(metadata["n_frontier_points"])
            if metadata_kwargs:
                await service.update_configuration_metadata(config_id, **metadata_kwargs)

            input_specs = [
                SAAAssetClassInputSpec(
                    asset_class_id=UUID(str(item["asset_class_id"])),
                    expected_return=float(item["expected_return"]),
                    volatility=float(item["volatility"]),
                    min_weight=float(item["min_weight"]),
                    max_weight=float(item["max_weight"]),
                )
                for item in inputs_raw
            ]
            correlation_specs = [
                SAACorrelationSpec(
                    asset_class_a_id=UUID(str(item["asset_class_a_id"])),
                    asset_class_b_id=UUID(str(item["asset_class_b_id"])),
                    correlation=float(item["correlation"]),
                )
                for item in correlations_raw
            ]
        except (KeyError, TypeError, ValueError) as exc:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "error": f"Malformed save payload: {exc}",
                    "field": None,
                    "row_index": None,
                },
            )

        try:
            await service.save_inputs_and_correlations(config_id, input_specs, correlation_specs)
        except SAAValidationError as exc:
            logger.info("saa-save: validation rejected %s: %s", config_id, exc)
            return _validation_error_response(exc)
        except ValidationError as exc:
            logger.info("saa-save: validation rejected %s: %s", config_id, exc)
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "error": str(exc),
                    "field": getattr(exc, "field", None),
                    "row_index": None,
                },
            )

        detail = await service.get_configuration_full(config_id)

    logger.info(
        "saa-save: tenant=%s user=%s config=%s inputs=%d correlations=%d",
        session.tenant_id,
        session.user_id,
        config_id,
        len(input_specs),
        len(correlation_specs),
    )
    return JSONResponse(content=jsonable_encoder(_detail_payload(detail)))


# ---------------------------------------------------------------------------
# POST /api/saa/configuration/{config_id}/activate
# ---------------------------------------------------------------------------


@router.post(
    "/api/saa/configuration/{config_id}/activate", dependencies=[Depends(require_role("owner"))]
)
async def saa_activate(
    request: Request,
    config_id: UUID,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> JSONResponse:
    """Make this configuration the tenant's single active SAA.

    Returns JSON plus an ``HX-Trigger: pf:saa-config-activated``
    header. The frontend reloads the section so the active-badge
    moves and the "Activate" button disappears.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        service = _build_service(db_session)
        existing = await service.get_configuration(config_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Configuration not found.",
            )
        activated = await service.activate_configuration(config_id)

    logger.info(
        "saa-activate: tenant=%s user=%s config=%s",
        session.tenant_id,
        session.user_id,
        config_id,
    )
    return JSONResponse(
        content=jsonable_encoder(
            {
                "id": str(activated.id),
                "name": activated.name,
                "is_active": activated.is_active,
            }
        ),
        headers={"HX-Trigger": "pf:saa-config-activated"},
    )


# ---------------------------------------------------------------------------
# DELETE /api/saa/configuration/{config_id}
# ---------------------------------------------------------------------------


@router.delete("/api/saa/configuration/{config_id}", dependencies=[Depends(require_role("owner"))])
async def saa_delete(
    request: Request,
    config_id: UUID,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> JSONResponse:
    """Hard-delete a configuration with cascade to inputs / correlations.

    Returns JSON plus an ``HX-Trigger: pf:saa-config-deleted``
    header. The frontend re-fetches the section, which lands on the
    next configuration (active or freshest) or the empty-state.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        service = _build_service(db_session)
        existing = await service.get_configuration(config_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Configuration not found.",
            )
        await service.delete_configuration(config_id)

    logger.info(
        "saa-delete: tenant=%s user=%s config=%s",
        session.tenant_id,
        session.user_id,
        config_id,
    )
    return JSONResponse(
        content={"deleted": True},
        headers={"HX-Trigger": "pf:saa-config-deleted"},
    )


# ---------------------------------------------------------------------------
# GET /api/saa/configuration/{config_id}/optimization
# ---------------------------------------------------------------------------


@router.get(
    "/api/saa/configuration/{config_id}/optimization",
    response_class=HTMLResponse,
)
async def saa_optimization_view(
    request: Request,
    config_id: UUID,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Run the optimisation and return the HTMX partial.

    Validation failures from the service layer (configuration
    absent, fewer than two asset classes, references to deleted
    asset classes) surface as a 400 rendering of the error partial
    — HTMX swaps the partial in regardless of status code, so the
    user sees the message inline instead of a generic 500 popup.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        service = _build_service(db_session)
        config = await service.get_configuration(config_id)
        if config is None:
            return cast(
                HTMLResponse,
                _templates(request).TemplateResponse(
                    request,
                    "_partials/saa_optimization_error.html",
                    {"error": "Configuration not found."},
                    status_code=status.HTTP_404_NOT_FOUND,
                ),
            )
        try:
            result = await service.run_optimization(config_id)
        except (SAAValidationError, ValidationError) as exc:
            logger.info(
                "saa-optimization: validation rejected %s: %s",
                config_id,
                exc,
            )
            return cast(
                HTMLResponse,
                _templates(request).TemplateResponse(
                    request,
                    "_partials/saa_optimization_error.html",
                    {"error": str(exc)},
                    status_code=status.HTTP_400_BAD_REQUEST,
                ),
            )
        except ValueError as exc:
            logger.warning(
                "saa-optimization: numeric failure for %s: %s",
                config_id,
                exc,
            )
            return cast(
                HTMLResponse,
                _templates(request).TemplateResponse(
                    request,
                    "_partials/saa_optimization_error.html",
                    {"error": f"Optimization failed: {exc}"},
                    status_code=status.HTTP_400_BAD_REQUEST,
                ),
            )

    spec = build_efficient_frontier_spec(
        frontier=result.frontier,
        tangency=result.tangency,
        min_var=result.min_var,
        cloud=result.cloud,
        cml=result.cml,
        asset_names=result.asset_names,
        risk_free_rate=config.risk_free_rate,
    )

    weights_payload = [
        {
            "asset_class": result.asset_names[idx],
            "tangency_pct": float(result.tangency.weights[idx]) * 100.0,
            "min_var_pct": float(result.min_var.weights[idx]) * 100.0,
        }
        for idx in range(len(result.asset_names))
    ]

    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/saa_optimization_partial.html",
            {
                "spec": spec,
                "weights_payload": weights_payload,
                "tangency_summary": {
                    "expected_return": result.tangency.expected_return,
                    "volatility": result.tangency.volatility,
                    "sharpe_ratio": result.tangency.sharpe_ratio,
                },
                "min_var_summary": {
                    "expected_return": result.min_var.expected_return,
                    "volatility": result.min_var.volatility,
                },
            },
        ),
    )


# ---------------------------------------------------------------------------
# GET /api/saa/asset-classes — modal partial
# ---------------------------------------------------------------------------


@router.get("/api/saa/asset-classes", response_class=HTMLResponse)
async def saa_asset_classes_modal(
    request: Request,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Render the asset-class catalogue modal partial.

    HTMX swaps this into the section's hidden ``<dialog>`` element
    and the front-end opens the dialog.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        service = _build_service(db_session)
        asset_classes = await service.list_asset_classes()
        usage: dict[str, int] = {
            str(ac.id): await service.count_configurations_using_asset_class(ac.id)
            for ac in asset_classes
        }

    asset_class_rows = [
        {
            "id": str(ac.id),
            "code": ac.code,
            "display_name": ac.display_name,
            "description": ac.description or "",
            "usage_count": usage[str(ac.id)],
            "updated_at": ac.updated_at.isoformat(),
        }
        for ac in asset_classes
    ]

    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/saa_asset_classes_modal.html",
            {
                "csrf_token": session.csrf_token,
                "asset_classes": asset_class_rows,
            },
        ),
    )


# ---------------------------------------------------------------------------
# POST /api/saa/asset-classes — create asset class
# ---------------------------------------------------------------------------


@router.post("/api/saa/asset-classes", dependencies=[Depends(require_role("owner"))])
async def saa_create_asset_class(
    request: Request,
    code: str = Form(..., min_length=1, max_length=64),
    display_name: str = Form(..., min_length=1, max_length=200),
    description: str = Form("", max_length=2000),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> JSONResponse:
    """Create a new asset class in the active tenant catalogue.

    Codes must be unique per tenant — the b005 unique constraint
    surfaces a duplicate as an :class:`IntegrityError` which we map
    to a 409 JSON response so the frontend can attach the error to
    the create-form inline.
    """
    engine = _engine(request)
    cleaned_code = code.strip()
    cleaned_name = display_name.strip()
    cleaned_description = description.strip() or None
    try:
        async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
            service = _build_service(db_session)
            ac = await service.create_asset_class(
                code=cleaned_code,
                display_name=cleaned_name,
                description=cleaned_description,
            )
    except IntegrityError:
        logger.info(
            "saa-asset-class-create: duplicate code %r in tenant %s",
            cleaned_code,
            session.tenant_id,
        )
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": (f"Asset class code '{cleaned_code}' already exists for this tenant."),
                "field": "code",
            },
        )
    logger.info(
        "saa-asset-class-create: tenant=%s code=%r id=%s",
        session.tenant_id,
        ac.code,
        ac.id,
    )
    return JSONResponse(
        content={
            "id": str(ac.id),
            "code": ac.code,
            "display_name": ac.display_name,
            "description": ac.description or "",
            "usage_count": 0,
        },
        headers={"HX-Trigger": "pf:saa-asset-class-created"},
    )


# ---------------------------------------------------------------------------
# PUT /api/saa/asset-classes/{asset_class_id} — update
# ---------------------------------------------------------------------------


@router.put(
    "/api/saa/asset-classes/{asset_class_id}", dependencies=[Depends(require_role("owner"))]
)
async def saa_update_asset_class(
    request: Request,
    asset_class_id: UUID,
    payload: dict,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> JSONResponse:
    """Update ``display_name`` / ``description`` on an asset class.

    The asset class ``code`` is not updatable — see the repository
    docstring for the rationale.
    """
    engine = _engine(request)
    display_name_raw = payload.get("display_name")
    description_raw = payload.get("description")
    display_name = str(display_name_raw).strip() if display_name_raw is not None else None
    description = str(description_raw).strip() if description_raw is not None else None
    if display_name is not None and len(display_name) == 0:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": "display_name must not be empty.",
                "field": "display_name",
            },
        )
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        service = _build_service(db_session)
        existing = await service.get_asset_class(asset_class_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Asset class not found.",
            )
        updated = await service.update_asset_class(
            asset_class_id,
            display_name=display_name,
            description=description,
        )
    return JSONResponse(
        content={
            "id": str(updated.id),
            "code": updated.code,
            "display_name": updated.display_name,
            "description": updated.description or "",
        }
    )


# ---------------------------------------------------------------------------
# DELETE /api/saa/asset-classes/{asset_class_id}
# ---------------------------------------------------------------------------


@router.delete(
    "/api/saa/asset-classes/{asset_class_id}", dependencies=[Depends(require_role("owner"))]
)
async def saa_delete_asset_class(
    request: Request,
    asset_class_id: UUID,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> JSONResponse:
    """Delete an asset class, returning 409 if it is in use.

    The b005 foreign keys carry ``ON DELETE RESTRICT``. We pre-count
    references for the friendly error message, then attempt the
    delete; a race against a concurrent save still surfaces as 409
    via the :class:`IntegrityError` fallback.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        service = _build_service(db_session)
        existing = await service.get_asset_class(asset_class_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Asset class not found.",
            )
        usage = await service.count_configurations_using_asset_class(asset_class_id)
        if usage > 0:
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={
                    "error": (
                        f"Asset class is in use by {usage} "
                        f"configuration{'s' if usage != 1 else ''} "
                        f"and cannot be deleted."
                    ),
                    "usage_count": usage,
                },
            )
        try:
            await service.delete_asset_class(asset_class_id)
        except IntegrityError:
            logger.warning(
                "saa-asset-class-delete: integrity error after pre-count "
                "indicated 0 usage (asset_class=%s)",
                asset_class_id,
            )
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={
                    "error": (
                        "Asset class became referenced during deletion; "
                        "retry after refreshing the page."
                    ),
                },
            )
    logger.info(
        "saa-asset-class-delete: tenant=%s id=%s",
        session.tenant_id,
        asset_class_id,
    )
    return JSONResponse(
        content={"deleted": True},
        headers={"HX-Trigger": "pf:saa-asset-class-deleted"},
    )
