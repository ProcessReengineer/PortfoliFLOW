# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Investment web surface — list, detail, and CRUD write paths.

Sub-stream 4b of the Phase-4 web migration introduces the investment
domain CRUD surface against the schema produced by 4a. Per ADR-0043
§5 this surface has two purposes: it is the schema-validation seam
(building real CRUD against the new tables exposes whether the
schema fits real workflows), and it is the developer / maintenance
seam for editing single data points without round-tripping through
Excel.

Read endpoints
    * ``GET    /investments``                                    — list view
      (HTMX, Tabulator). Includes both active and inactive investments;
      query params ``?type=…``, ``?asset_class_id=…``, and
      ``?active_only=true`` filter the list.
    * ``GET    /investments/new``                                — empty form view.
    * ``GET    /investments/{id}``                               — detail view with NAV chart and cashflow table.
    * ``GET    /investments/{id}/edit``                          — edit form view (separate from detail).

Write endpoints (all CSRF-protected)
    * ``POST   /investments``                                    — create.
    * ``PUT    /investments/{id}``                               — update.
    * ``DELETE /investments/{id}``                               — hard-delete (cascade to NAVs and cashflows).
    * ``PATCH  /investments/{id}/active``                        — soft-delete or reactivate.
    * ``POST   /investments/{id}/navs``                          — add (UPSERT) a NAV row.
    * ``PUT    /investments/{id}/navs/{nav_id}``                 — update a NAV row.
    * ``DELETE /investments/{id}/navs/{nav_id}``                 — delete a NAV row.
    * ``POST   /investments/{id}/cashflows``                     — add a cashflow row.
    * ``PUT    /investments/{id}/cashflows/{cashflow_id}``       — update a cashflow row.
    * ``DELETE /investments/{id}/cashflows/{cashflow_id}``       — delete a cashflow row.
    * ``POST   /investments/{id}/identifiers``                   — add a security identifier (ADR-0096).
    * ``POST   /investments/{id}/identifiers/{identifier_id}/primary`` — set an identifier primary.
    * ``DELETE /investments/{id}/identifiers/{identifier_id}``   — delete a security identifier.

Cross-tenant safety: every route that references an ``investment_id``
(or a child ``nav_id`` / ``cashflow_id``) relies on RLS to filter
foreign-tenant rows out of the repository response. A foreign-tenant
id therefore appears to the route as absence and is mapped to 404
(deliberately not 403 — disclosing whether an id exists in another
tenant is itself an information leak).
"""

from __future__ import annotations

import logging
from datetime import date as _date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core.exceptions import (
    CurrencyMismatchError,
    InvestorFlowScopeError,
    NonNegativeHoldingsError,
    ValidationError,
    ValuationModeError,
)
from core.models.investment_identifier import IDENTIFIER_SCHEMES
from core.repositories._session import tenant_context
from core.repositories.asset_class_repository import AssetClassRepository
from core.repositories.instrument_price_repository import (
    InstrumentPriceRepository,
)
from core.repositories.position_transaction_repository import (
    PositionTransactionRepository,
)
from core.repositories.region_repository import RegionRepository
from core.repositories.investment_cashflow_repository import (
    InvestmentCashflowRepository,
)
from core.repositories.investment_region_weights_repository import (
    InvestmentRegionWeightsRepository,
)
from core.repositories.investment_identifier_repository import (
    InvestmentIdentifierRepository,
)
from core.repositories.investment_nav_repository import InvestmentNavRepository
from core.repositories.investment_repository import InvestmentRepository
from core.repositories.investment_sector_weights_repository import (
    InvestmentSectorWeightsRepository,
)
from core.repositories.sector_repository import SectorRepository
from core.repositories.user_repository import UserRepository
from services.auth.session import SessionDTO
from services.chart_specs import (
    build_cashflows_nav_spec,
    build_multiples_spec,
    build_nav_timeseries_spec,
    build_total_return_spec,
)
from services.investments import InvestmentService
from web.auth import require_session, verify_csrf
from web.permissions import require_role

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Domain constants
# ---------------------------------------------------------------------------


_VALID_INVESTMENT_TYPES: frozenset[str] = frozenset(
    {
        "private_equity",
        "private_debt",
        "real_estate",
        "infra_equity",
        "listed_equity",
        "listed_bonds",
        "other",
        # ADR-0100 §1: a foreign-currency cash balance modelled as a
        # first-class investment row (NAV-only, converted at the ADR-0099
        # seam), assignable an AnlV code like any holding.
        "cash",
    }
)
_VALID_NAV_KINDS: frozenset[str] = frozenset({"plan", "actual"})
_VALID_FLOW_TYPES: frozenset[str] = frozenset(
    {
        "capital_call",
        "distribution",
        "fee",
        "carry",
        "dividend",
        "coupon",
        "other",
        # ADR-0103 §5: a net contribution to / withdrawal from the mandate.
        # Bookable on cash positions only — that rule is the service's
        # (InvestorFlowScopeError), not this route's: it spans two tables and
        # a second formulation here would drift.
        "investor_flow",
    }
)
_VALID_FLOW_KINDS: frozenset[str] = frozenset({"plan", "actual"})
#: The closed ``position_transactions.txn_type`` set (ADR-0097 §2).
_VALID_TXN_TYPES: frozenset[str] = frozenset({"opening", "buy", "sell", "transfer"})
#: Transaction types that require a ``price_per_unit`` (ADR-0097 §2). An
#: ``opening`` (Excel-synthesised, price derived at materialisation) and a
#: ``transfer`` (in-kind, no trade price) may omit it.
_TXN_TYPES_REQUIRING_PRICE: frozenset[str] = frozenset({"buy", "sell"})


# ---------------------------------------------------------------------------
# Helpers
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


def _build_service(session: AsyncSession) -> InvestmentService:
    """Construct an :class:`InvestmentService` against a tenant-scoped session.

    Wires the ledger and price repositories alongside the three core ones so
    the positions surface (ADR-0097 §6, ADR-0098 §3) can write transactions
    and run the in-transaction computed-NAV materialisation. Both share this
    session, so a ledger write and the NAV rows it implies commit together.
    """
    return InvestmentService(
        investments=InvestmentRepository(session),
        navs=InvestmentNavRepository(session),
        cashflows=InvestmentCashflowRepository(session),
        identifiers=InvestmentIdentifierRepository(session),
        position_transactions=PositionTransactionRepository(session),
        instrument_prices=InstrumentPriceRepository(session),
    )


def _bad_request(message: str, *, field: str | None = None) -> JSONResponse:
    """Render a structured 400 with ``error`` / ``field`` keys."""
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"error": message, "field": field},
    )


def _validate_currency(value: str) -> str:
    """Normalise and check the 3-letter ISO 4217 convention.

    Raises :class:`HTTPException` (400) on failure. ISO 4217 is a
    convention here, not a whitelist — we only check the structural
    shape (three uppercase letters) per ADR-0043 §4.
    """
    cleaned = value.strip().upper()
    if len(cleaned) != 3 or not cleaned.isalpha():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(f"currency must be a 3-letter ISO 4217 code (got {value!r})."),
        )
    return cleaned


def _parse_decimal(raw: object, *, field: str) -> Decimal:
    """Parse a JSON number / string into :class:`Decimal`.

    Raises :class:`HTTPException` (400) on failure.
    """
    if raw is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field} is required.",
        )
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field} must be a valid number ({exc}).",
        )


def _parse_iso_date(raw: object, *, field: str) -> _date:
    """Parse an ISO-8601 ``YYYY-MM-DD`` string into :class:`date`."""
    if not isinstance(raw, str) or not raw.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field} is required (YYYY-MM-DD).",
        )
    try:
        return _date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field} must be ISO-8601 YYYY-MM-DD ({exc}).",
        )


def _parse_flow_timestamp(payload: dict) -> datetime:
    """Resolve ``flow_timestamp`` from the payload, with a date-only fallback.

    If ``flow_timestamp`` is supplied it is parsed as ISO-8601. If it
    is missing or empty, the route falls back to ``flow_date`` at
    12:00 UTC — the operational convention from ADR-0043 §1 for
    cashflows whose precise time is unknown. If neither field is
    present the route raises 400.
    """
    raw_ts = payload.get("flow_timestamp")
    if isinstance(raw_ts, str) and raw_ts.strip():
        try:
            parsed = datetime.fromisoformat(raw_ts.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"flow_timestamp must be ISO-8601 ({exc}).",
            )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    raw_date = payload.get("flow_date")
    if not (isinstance(raw_date, str) and raw_date.strip()):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="flow_timestamp or flow_date is required.",
        )
    parsed_date = _parse_iso_date(raw_date, field="flow_date")
    return datetime.combine(parsed_date, time(12, 0), tzinfo=timezone.utc)


def _investment_payload(investment, asset_classes_by_id: dict[str, dict]) -> dict:
    """Project an :class:`InvestmentDTO` to a Tabulator-friendly row.

    The dict carries the asset-class display name for the list view
    so the table can show a human-readable label without a second
    request.
    """
    ac = asset_classes_by_id.get(str(investment.asset_class_id))
    return {
        "id": str(investment.id),
        "name": investment.name,
        "investment_type": investment.investment_type,
        "asset_class_id": str(investment.asset_class_id),
        "asset_class_name": ac["display_name"] if ac else "",
        "manager_name": investment.manager_name or "",
        "region": investment.region or "",
        "currency": investment.currency,
        "vintage_year": investment.vintage_year,
        "commitment_amount": (
            float(investment.commitment_amount)
            if investment.commitment_amount is not None
            else None
        ),
        "is_active": investment.is_active,
        "updated_at": investment.updated_at.isoformat(),
    }


def _nav_payload(nav) -> dict:
    return {
        "id": str(nav.id),
        "investment_id": str(nav.investment_id),
        "as_of_date": nav.as_of_date.isoformat(),
        "nav_value": float(nav.nav_value),
        "currency": nav.currency,
        "nav_kind": nav.nav_kind,
        "source": nav.source or "",
        "updated_at": nav.updated_at.isoformat(),
    }


def _cashflow_payload(cashflow) -> dict:
    return {
        "id": str(cashflow.id),
        "investment_id": str(cashflow.investment_id),
        "flow_timestamp": cashflow.flow_timestamp.isoformat(),
        "flow_type": cashflow.flow_type,
        "flow_kind": cashflow.flow_kind,
        "amount": float(cashflow.amount),
        "currency": cashflow.currency,
        "description": cashflow.description or "",
        "updated_at": cashflow.updated_at.isoformat(),
    }


def _identifier_payload(identifier) -> dict:
    return {
        "id": str(identifier.id),
        "investment_id": str(identifier.investment_id),
        "scheme": identifier.scheme,
        "value": identifier.value,
        "is_primary": identifier.is_primary,
        "source": identifier.source or "",
        "updated_at": identifier.updated_at.isoformat(),
    }


def _position_payload(txn) -> dict:
    return {
        "id": str(txn.id),
        "investment_id": str(txn.investment_id),
        "txn_type": txn.txn_type,
        "trade_date": txn.trade_date.isoformat(),
        "units": float(txn.units),
        "price_per_unit": (float(txn.price_per_unit) if txn.price_per_unit is not None else None),
        "consideration": (float(txn.consideration) if txn.consideration is not None else None),
        "currency": txn.currency,
        "note": txn.note or "",
        "source": txn.source or "",
        "ingest_origin": txn.ingest_origin,
        "updated_at": txn.updated_at.isoformat(),
    }


def _position_summary_payload(summary) -> dict:
    """Shape a :class:`PositionSummaryDTO` for the positions panel.

    ``latest_computed_nav`` carries ``basis`` and ``ingest_origin`` side by
    side: they are orthogonal (ADR-0098 §1) and the panel badges the former
    without inferring it from the latter.
    """
    price = summary.latest_price
    nav = summary.latest_computed_nav
    return {
        "valuation_mode": summary.valuation_mode,
        "currency": summary.currency,
        "shows_panel": summary.shows_panel,
        "transactions": [_position_payload(t) for t in summary.transactions],
        "holdings_units": float(summary.holdings_units),
        "holdings_as_of_date": (
            summary.holdings_as_of_date.isoformat()
            if summary.holdings_as_of_date is not None
            else None
        ),
        "latest_price": (
            {
                "as_of_date": price.as_of_date.isoformat(),
                "price": float(price.price),
                "currency": price.currency,
                "ingest_origin": price.ingest_origin,
                "source": price.source or "",
            }
            if price is not None
            else None
        ),
        "latest_computed_nav": (
            {
                "as_of_date": nav.as_of_date.isoformat(),
                "nav_value": float(nav.nav_value),
                "currency": nav.currency,
                "basis": nav.basis,
                "ingest_origin": nav.ingest_origin,
                "source": nav.source or "",
            }
            if nav is not None
            else None
        ),
        "can_flip": summary.can_flip,
        "flip_blocked_reason": summary.flip_blocked_reason,
    }


def _validate_txn_rules(
    txn_type: str, units: Decimal, price_per_unit: Decimal | None
) -> JSONResponse | None:
    """Check the ADR-0097 §2 sign and price rules before the service call.

    These rules are CHECK-enforced in the database, so a violating write
    would surface as an ``IntegrityError`` — a 500, not a form error. This
    module already validates the other closed sets and shapes at the route
    layer (``_VALID_NAV_KINDS``, ``IDENTIFIER_SCHEMES``, ``_validate_currency``)
    for exactly that reason; the CHECKs remain the backstop.

    Args:
        txn_type: One of :data:`_VALID_TXN_TYPES` (validated by the caller).
        units: The signed unit quantity.
        price_per_unit: The per-unit trade price, or ``None``.

    Returns:
        A structured 400 response describing the first violated rule, or
        ``None`` when the transaction satisfies both rule families.
    """
    if txn_type in ("opening", "buy") and units <= 0:
        return _bad_request(
            f"units must be greater than zero for a {txn_type} transaction.",
            field="units",
        )
    if txn_type == "sell" and units >= 0:
        return _bad_request("units must be negative for a sell transaction.", field="units")
    if txn_type == "transfer" and units == 0:
        return _bad_request("units must not be zero for a transfer transaction.", field="units")
    if price_per_unit is not None and price_per_unit <= 0:
        return _bad_request("price_per_unit must be greater than zero.", field="price_per_unit")
    if txn_type in _TXN_TYPES_REQUIRING_PRICE and price_per_unit is None:
        return _bad_request(
            f"price_per_unit is required for a {txn_type} transaction.",
            field="price_per_unit",
        )
    return None


def _domain_error_response(exc: ValidationError) -> JSONResponse:
    """Render a typed domain error as the structured body the forms read.

    :class:`ValuationModeError` reports a conflict with the investment's
    current state (ADR-0097 §6 preconditions), not a malformed field, so it
    renders as ``409``; the field-level errors render as ``400``.
    """
    code = (
        status.HTTP_409_CONFLICT
        if isinstance(exc, ValuationModeError)
        else status.HTTP_400_BAD_REQUEST
    )
    return JSONResponse(
        status_code=code,
        content={"error": exc.message, "field": exc.field},
    )


# ---------------------------------------------------------------------------
# GET /investments — list view
#
# Order matters: ``/investments/new`` must be registered *before*
# ``/investments/{id}`` so FastAPI does not try (and 422-fail) the UUID
# parameter parser for the literal "new" path.
# ---------------------------------------------------------------------------


@router.get("/investments", response_class=HTMLResponse)
async def investments_list_view(
    request: Request,
    type: str | None = None,
    asset_class_id: UUID | None = None,
    active_only: bool = False,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Render the investments list page.

    The ``type`` query parameter narrows the list to one of the eight
    CHECK-allowed discriminator values. The ``asset_class_id`` query
    parameter narrows by the asset-class FK. ``active_only=true``
    filters out soft-deleted rows; the default shows both so an
    operator can re-activate a missing investment without a separate
    page.
    """
    if type is not None and type not in _VALID_INVESTMENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Unknown investment_type filter "
                f"{type!r}; expected one of {sorted(_VALID_INVESTMENT_TYPES)}."
            ),
        )

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        service = _build_service(db_session)
        if active_only:
            investments = await service.list_active_investments()
        else:
            investments = await service.list_investments()

        if type is not None:
            investments = [i for i in investments if i.investment_type == type]
        if asset_class_id is not None:
            investments = [i for i in investments if i.asset_class_id == asset_class_id]

        asset_classes = await AssetClassRepository(db_session).list_all()
        user = await UserRepository(db_session).get_by_id(session.user_id)

    asset_classes_by_id = {
        str(ac.id): {
            "id": str(ac.id),
            "code": ac.code,
            "display_name": ac.display_name,
        }
        for ac in asset_classes
    }
    rows = [_investment_payload(inv, asset_classes_by_id) for inv in investments]

    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "investments/list.html",
            {
                "user_email": user.email if user is not None else "",
                "csrf_token": session.csrf_token,
                "investments": rows,
                "asset_classes": list(asset_classes_by_id.values()),
                "investment_types": sorted(_VALID_INVESTMENT_TYPES),
                "filter": {
                    "type": type or "",
                    "asset_class_id": (str(asset_class_id) if asset_class_id else ""),
                    "active_only": active_only,
                },
            },
        ),
    )


# ---------------------------------------------------------------------------
# GET /investments/new — empty-form view
# ---------------------------------------------------------------------------


@router.get("/investments/new", response_class=HTMLResponse)
async def investments_new_view(
    request: Request,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Render the empty investment-creation form."""
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        asset_classes = await AssetClassRepository(db_session).list_all()
        user = await UserRepository(db_session).get_by_id(session.user_id)

    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "investments/new.html",
            {
                "user_email": user.email if user is not None else "",
                "csrf_token": session.csrf_token,
                "asset_classes": [
                    {
                        "id": str(ac.id),
                        "code": ac.code,
                        "display_name": ac.display_name,
                    }
                    for ac in asset_classes
                ],
                "investment_types": sorted(_VALID_INVESTMENT_TYPES),
            },
        ),
    )


# ---------------------------------------------------------------------------
# POST /investments — create
# ---------------------------------------------------------------------------


@router.post("/investments", dependencies=[Depends(require_role("owner"))])
async def investments_create(
    request: Request,
    name: str = Form(..., min_length=1, max_length=200),
    investment_type: str = Form(...),
    asset_class_id: UUID = Form(...),
    currency: str = Form(...),
    manager_name: str = Form(""),
    region: str = Form(""),
    vintage_year: int | None = Form(None),
    commitment_amount: str | None = Form(None),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> RedirectResponse:
    """Create a new investment and redirect to its detail view.

    Validation:
    - ``name`` is non-empty (Form validator) and tenant-unique
      (DB UNIQUE constraint surfaces a duplicate as 409).
    - ``investment_type`` is one of the eight CHECK-allowed values.
    - ``asset_class_id`` resolves in the active tenant catalogue.
    - ``currency`` is a 3-letter ISO 4217 code.
    """
    if investment_type not in _VALID_INVESTMENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"investment_type must be one of "
                f"{sorted(_VALID_INVESTMENT_TYPES)}; got {investment_type!r}."
            ),
        )
    cleaned_name = name.strip()
    cleaned_currency = _validate_currency(currency)
    cleaned_manager = manager_name.strip() or None
    cleaned_region = region.strip() or None
    parsed_commitment: Decimal | None = None
    if commitment_amount is not None and str(commitment_amount).strip():
        try:
            parsed_commitment = Decimal(str(commitment_amount).strip())
        except (InvalidOperation, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"commitment_amount must be a number ({exc}).",
            )

    engine = _engine(request)
    try:
        async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
            # Ensure the asset class exists in the active tenant.
            ac = await AssetClassRepository(db_session).get_by_id(asset_class_id)
            if ac is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="asset_class_id does not exist in this tenant.",
                )
            service = _build_service(db_session)
            investment = await service.create_investment(
                name=cleaned_name,
                investment_type=investment_type,
                asset_class_id=asset_class_id,
                currency=cleaned_currency,
                created_by=session.user_id,
                manager_name=cleaned_manager,
                region=cleaned_region,
                vintage_year=vintage_year,
                commitment_amount=parsed_commitment,
            )
    except IntegrityError:
        logger.info(
            "investments-create: duplicate name %r in tenant %s",
            cleaned_name,
            session.tenant_id,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(f"Investment with name {cleaned_name!r} already exists in this tenant."),
        )

    logger.info(
        "investments-create: tenant=%s user=%s name=%r id=%s",
        session.tenant_id,
        session.user_id,
        investment.name,
        investment.id,
    )
    return RedirectResponse(
        url=f"/investments/{investment.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# GET /investments/{id} — detail view
# ---------------------------------------------------------------------------


@router.get("/investments/{investment_id}", response_class=HTMLResponse)
async def investments_detail_view(
    request: Request,
    investment_id: UUID,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Render the investment detail page.

    Three sections: stammdaten header, NAV time-series chart (Plotly,
    plan dashed + actual solid), cashflow table (Tabulator, sortable
    and filterable). The "Edit" button on this page navigates to the
    edit form; inline editing of investments is *not* part of 4b.

    A positions panel (ADR-0097 §6) renders only where a unit ledger is
    meaningful — an unitised investment, a unitisable type, or one that
    already carries ledger rows. The private-markets majority renders
    exactly as it did before the position model landed.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        service = _build_service(db_session)
        detail = await service.get_investment_detail(investment_id)
        if detail is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Investment not found.",
            )
        identifiers = await service.list_identifiers(investment_id)
        positions = await service.get_position_summary(investment_id)
        ac = await AssetClassRepository(db_session).get_by_id(detail.investment.asset_class_id)
        user = await UserRepository(db_session).get_by_id(session.user_id)
        # Load region and sector weights for the read-only allocation
        # sections. Investments without weights render empty sections —
        # no 500, no layout break. The Excel import path writes
        # region weights (ADR-0046); the country-weights table is
        # reserved for ISO-granular data sources and not consumed by
        # the detail view today.
        # Per ADR-0080 §4 the detail view shows "the" allocation as the
        # latest historised snapshot for this investment.
        region_weights = await InvestmentRegionWeightsRepository(
            db_session
        ).list_latest_for_investment(investment_id)
        sector_weights = await InvestmentSectorWeightsRepository(
            db_session
        ).list_latest_for_investment(investment_id)
        regions_by_id: dict[str, dict] = {}
        if region_weights:
            region_repo = RegionRepository(db_session)
            for w in region_weights:
                key = str(w.region_id)
                if key in regions_by_id:
                    continue
                region = await region_repo.get_by_id(w.region_id)
                if region is not None:
                    regions_by_id[key] = {
                        "id": str(region.id),
                        "code": region.code,
                        "display_name": region.display_name,
                    }
        sectors_by_id: dict[str, dict] = {}
        if sector_weights:
            sector_repo = SectorRepository(db_session)
            for w in sector_weights:
                key = str(w.sector_id)
                if key in sectors_by_id:
                    continue
                sector = await sector_repo.get_by_id(w.sector_id)
                if sector is not None:
                    sectors_by_id[key] = {
                        "id": str(sector.id),
                        "code": sector.code,
                        "display_name": sector.display_name,
                    }

    chart_spec = build_nav_timeseries_spec(detail.investment, detail.navs)

    region_allocation_rows = []
    for w in region_weights:
        region = regions_by_id.get(str(w.region_id))
        region_allocation_rows.append(
            {
                "code": region["code"] if region else "",
                "display_name": (region["display_name"] if region else str(w.region_id)),
                "weight_pct": float(w.weight_pct),
            }
        )
    sector_allocation_rows = []
    for w in sector_weights:
        sector = sectors_by_id.get(str(w.sector_id))
        sector_allocation_rows.append(
            {
                "code": sector["code"] if sector else "",
                "display_name": (sector["display_name"] if sector else str(w.sector_id)),
                "weight_pct": float(w.weight_pct),
            }
        )

    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "investments/detail.html",
            {
                "user_email": user.email if user is not None else "",
                "csrf_token": session.csrf_token,
                "investment": {
                    "id": str(detail.investment.id),
                    "name": detail.investment.name,
                    "investment_type": detail.investment.investment_type,
                    "asset_class_id": str(detail.investment.asset_class_id),
                    "asset_class_name": ac.display_name if ac else "",
                    "manager_name": detail.investment.manager_name or "",
                    "region": detail.investment.region or "",
                    "currency": detail.investment.currency,
                    "vintage_year": detail.investment.vintage_year,
                    "commitment_amount": (
                        float(detail.investment.commitment_amount)
                        if detail.investment.commitment_amount is not None
                        else None
                    ),
                    "is_active": detail.investment.is_active,
                    "updated_at": detail.investment.updated_at.isoformat(),
                },
                "navs": [_nav_payload(n) for n in detail.navs],
                "cashflows": [_cashflow_payload(c) for c in detail.cashflows],
                "identifiers": [_identifier_payload(i) for i in identifiers],
                "chart_spec": chart_spec,
                "nav_kinds": sorted(_VALID_NAV_KINDS),
                "flow_kinds": sorted(_VALID_FLOW_KINDS),
                "flow_types": sorted(_VALID_FLOW_TYPES),
                "identifier_schemes": sorted(IDENTIFIER_SCHEMES),
                "txn_types": sorted(_VALID_TXN_TYPES),
                "positions": _position_summary_payload(positions),
                "region_allocation": region_allocation_rows,
                "sector_allocation": sector_allocation_rows,
            },
        ),
    )


# ---------------------------------------------------------------------------
# GET /investments/{id}/edit — edit-form view
# ---------------------------------------------------------------------------


@router.get("/investments/{investment_id}/edit", response_class=HTMLResponse)
async def investments_edit_view(
    request: Request,
    investment_id: UUID,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Render the edit form for an existing investment."""
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        service = _build_service(db_session)
        investment = await service.get_investment(investment_id)
        if investment is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Investment not found.",
            )
        asset_classes = await AssetClassRepository(db_session).list_all()
        user = await UserRepository(db_session).get_by_id(session.user_id)

    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "investments/edit.html",
            {
                "user_email": user.email if user is not None else "",
                "csrf_token": session.csrf_token,
                "investment": {
                    "id": str(investment.id),
                    "name": investment.name,
                    "investment_type": investment.investment_type,
                    "asset_class_id": str(investment.asset_class_id),
                    "manager_name": investment.manager_name or "",
                    "region": investment.region or "",
                    "currency": investment.currency,
                    "vintage_year": investment.vintage_year,
                    "commitment_amount": (
                        float(investment.commitment_amount)
                        if investment.commitment_amount is not None
                        else None
                    ),
                },
                "asset_classes": [
                    {
                        "id": str(ac.id),
                        "code": ac.code,
                        "display_name": ac.display_name,
                    }
                    for ac in asset_classes
                ],
                "investment_types": sorted(_VALID_INVESTMENT_TYPES),
            },
        ),
    )


# ---------------------------------------------------------------------------
# GET /investments/{id}/charts — analytics charts partial
# ---------------------------------------------------------------------------


@router.get("/investments/{investment_id}/charts", response_class=HTMLResponse)
async def investments_charts_view(
    request: Request,
    investment_id: UUID,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Render the three Phase-5b investment charts (Total Return,
    Cash Flows & NAV, TVPI & DPI) in a 1×3 horizontal row.

    The route returns a full HTML page so users can navigate to it
    directly from the Investment list / detail. The same template is
    HTMX-friendly: the container ``#inv-charts-row`` carries the
    three Plotly targets and an inline script that bootstraps them
    from the embedded specs, so an HTMX swap into a parent shell
    works without a page reload.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        service = _build_service(db_session)
        # The "lines" variant of build_multiples_spec does not consume
        # rolling_irr (see services/chart_specs/investment_multiples.py).
        # Skip the per-NAV Brent's-method IRR computation — it is the
        # dominant cost in this hot path.
        bundle = await service.get_charts_data(investment_id, include_irr=False)
        if bundle is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Investment not found.",
            )
        user = await UserRepository(db_session).get_by_id(session.user_id)

    total_return_spec = build_total_return_spec(bundle.total_return_series, bundle.investment_name)
    cashflows_nav_spec = build_cashflows_nav_spec(
        bundle.cashflows_actual,
        bundle.nav_series,
        bundle.net_capital_gain,
        bundle.investment_name,
    )
    multiples_spec = build_multiples_spec(
        bundle.rolling_multiples,
        bundle.rolling_irr,
        bundle.investment_name,
        style="lines",
    )

    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "investments/charts.html",
            {
                "user_email": user.email if user is not None else "",
                "csrf_token": session.csrf_token,
                "investment": {
                    "id": str(investment_id),
                    "name": bundle.investment_name,
                },
                "total_return_spec": total_return_spec,
                "cashflows_nav_spec": cashflows_nav_spec,
                "multiples_spec": multiples_spec,
            },
        ),
    )


# ---------------------------------------------------------------------------
# PUT /investments/{id} — update
# ---------------------------------------------------------------------------


@router.put("/investments/{investment_id}", dependencies=[Depends(require_role("owner"))])
async def investments_update(
    request: Request,
    investment_id: UUID,
    payload: dict,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> JSONResponse:
    """Update mutable fields on an investment.

    Body shape: ``{"name": "...", "investment_type": "...",
    "asset_class_id": "...", "currency": "...", "manager_name": "...",
    "region": "...", "vintage_year": 2020, "commitment_amount": "..."}``.
    Every field is optional; only those present are updated.
    """
    engine = _engine(request)
    update_kwargs: dict[str, object] = {}

    if "name" in payload and payload["name"] is not None:
        cleaned = str(payload["name"]).strip()
        if not cleaned:
            return _bad_request("name must not be empty.", field="name")
        update_kwargs["name"] = cleaned
    if "investment_type" in payload and payload["investment_type"] is not None:
        itype = str(payload["investment_type"])
        if itype not in _VALID_INVESTMENT_TYPES:
            return _bad_request(
                f"investment_type must be one of {sorted(_VALID_INVESTMENT_TYPES)}; got {itype!r}.",
                field="investment_type",
            )
        update_kwargs["investment_type"] = itype
    if "asset_class_id" in payload and payload["asset_class_id"] is not None:
        try:
            update_kwargs["asset_class_id"] = UUID(str(payload["asset_class_id"]))
        except ValueError:
            return _bad_request("asset_class_id must be a UUID.", field="asset_class_id")
    if "currency" in payload and payload["currency"] is not None:
        try:
            update_kwargs["currency"] = _validate_currency(str(payload["currency"]))
        except HTTPException as exc:
            return _bad_request(str(exc.detail), field="currency")
    if "manager_name" in payload:
        raw = payload["manager_name"]
        update_kwargs["manager_name"] = str(raw).strip() if raw is not None else None
    if "region" in payload:
        raw = payload["region"]
        update_kwargs["region"] = str(raw).strip() if raw is not None else None
    if "vintage_year" in payload and payload["vintage_year"] is not None:
        try:
            update_kwargs["vintage_year"] = int(payload["vintage_year"])
        except (TypeError, ValueError):
            return _bad_request("vintage_year must be an integer.", field="vintage_year")
    if (
        "commitment_amount" in payload
        and payload["commitment_amount"] is not None
        and str(payload["commitment_amount"]).strip()
    ):
        try:
            update_kwargs["commitment_amount"] = Decimal(str(payload["commitment_amount"]))
        except (InvalidOperation, ValueError):
            return _bad_request(
                "commitment_amount must be a number.",
                field="commitment_amount",
            )

    try:
        async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
            service = _build_service(db_session)
            existing = await service.get_investment(investment_id)
            if existing is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Investment not found.",
                )
            if "asset_class_id" in update_kwargs:
                ac = await AssetClassRepository(db_session).get_by_id(
                    cast(UUID, update_kwargs["asset_class_id"])
                )
                if ac is None:
                    return _bad_request(
                        "asset_class_id does not exist in this tenant.",
                        field="asset_class_id",
                    )
            updated = await service.update_investment(investment_id, **update_kwargs)
    except IntegrityError:
        logger.info(
            "investments-update: duplicate name on %s in tenant %s",
            investment_id,
            session.tenant_id,
        )
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": ("Another investment with this name already exists in this tenant."),
                "field": "name",
            },
        )

    if updated is None:
        # Defence in depth — get_by_id said the row existed; if a
        # concurrent delete races, surface as 404.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Investment not found.",
        )
    logger.info(
        "investments-update: tenant=%s user=%s id=%s",
        session.tenant_id,
        session.user_id,
        investment_id,
    )
    return JSONResponse(content=jsonable_encoder(_investment_payload(updated, {})))


# ---------------------------------------------------------------------------
# DELETE /investments/{id}
# ---------------------------------------------------------------------------


@router.delete("/investments/{investment_id}", dependencies=[Depends(require_role("owner"))])
async def investments_delete(
    request: Request,
    investment_id: UUID,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> JSONResponse:
    """Hard-delete an investment.

    Cascade on the FKs ``investment_navs.investment_id`` and
    ``investment_cashflows.investment_id`` removes child rows
    automatically. Operators who want to preserve history use
    :func:`investments_set_active` (PATCH) for a soft-delete instead.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        service = _build_service(db_session)
        existing = await service.get_investment(investment_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Investment not found.",
            )
        await service.delete_investment(investment_id)

    logger.info(
        "investments-delete: tenant=%s user=%s id=%s",
        session.tenant_id,
        session.user_id,
        investment_id,
    )
    return JSONResponse(
        content={"deleted": True},
        headers={"HX-Redirect": "/investments"},
    )


# ---------------------------------------------------------------------------
# PATCH /investments/{id}/active — soft-delete / reactivate
# ---------------------------------------------------------------------------


@router.patch("/investments/{investment_id}/active", dependencies=[Depends(require_role("owner"))])
async def investments_set_active(
    request: Request,
    investment_id: UUID,
    payload: dict,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> JSONResponse:
    """Toggle the soft-delete flag on an investment.

    Body: ``{"is_active": true | false}``. The Excel-import workflow
    (sub-stream 4c) uses the same repository path for its
    soft-delete-with-reactivation pattern.
    """
    raw = payload.get("is_active")
    if not isinstance(raw, bool):
        return _bad_request("is_active must be a boolean.", field="is_active")

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        service = _build_service(db_session)
        existing = await service.get_investment(investment_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Investment not found.",
            )
        await service.set_investment_active(investment_id, raw)

    logger.info(
        "investments-set-active: tenant=%s user=%s id=%s active=%s",
        session.tenant_id,
        session.user_id,
        investment_id,
        raw,
    )
    return JSONResponse(content={"id": str(investment_id), "is_active": raw})


# ---------------------------------------------------------------------------
# POST /investments/{id}/navs — add or upsert a NAV row
# ---------------------------------------------------------------------------


@router.post("/investments/{investment_id}/navs", dependencies=[Depends(require_role("owner"))])
async def investments_add_nav(
    request: Request,
    investment_id: UUID,
    payload: dict,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> JSONResponse:
    """Add or UPSERT a NAV row by ``(investment, as_of_date, nav_kind)``.

    Body: ``{"as_of_date": "...", "nav_value": "...", "currency": "...",
    "nav_kind": "actual"|"plan", "source": "..."}``.
    """
    as_of_date = _parse_iso_date(payload.get("as_of_date"), field="as_of_date")
    nav_value = _parse_decimal(payload.get("nav_value"), field="nav_value")
    currency = _validate_currency(str(payload.get("currency", "")))
    nav_kind = str(payload.get("nav_kind", ""))
    if nav_kind not in _VALID_NAV_KINDS:
        return _bad_request(
            f"nav_kind must be one of {sorted(_VALID_NAV_KINDS)}.",
            field="nav_kind",
        )
    raw_source = payload.get("source")
    source: str | None = str(raw_source).strip() if raw_source not in (None, "") else None

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        service = _build_service(db_session)
        # Cross-tenant safety: the route must reject NAV writes against
        # an investment id that does not exist in the active tenant.
        existing = await service.get_investment(investment_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Investment not found.",
            )
        nav = await service.add_nav(
            investment_id=investment_id,
            as_of_date=as_of_date,
            nav_kind=nav_kind,
            nav_value=nav_value,
            currency=currency,
            source=source,
            created_by=session.user_id,
        )

    logger.info(
        "investments-add-nav: tenant=%s user=%s investment=%s nav=%s",
        session.tenant_id,
        session.user_id,
        investment_id,
        nav.id,
    )
    return JSONResponse(content=jsonable_encoder(_nav_payload(nav)))


# ---------------------------------------------------------------------------
# PUT /investments/{id}/navs/{nav_id} — update a NAV row
# ---------------------------------------------------------------------------


@router.put(
    "/investments/{investment_id}/navs/{nav_id}", dependencies=[Depends(require_role("owner"))]
)
async def investments_update_nav(
    request: Request,
    investment_id: UUID,
    nav_id: UUID,
    payload: dict,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> JSONResponse:
    """Update a NAV row by id.

    Body: ``{"nav_value": "...", "currency": "...", "source": "..."}``.
    The natural-key fields (``investment_id``, ``as_of_date``,
    ``nav_kind``) are immutable through this path — the repository
    re-uses them automatically for the UPSERT.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        service = _build_service(db_session)
        existing_inv = await service.get_investment(investment_id)
        if existing_inv is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Investment not found.",
            )
        existing_nav = await service.get_nav(nav_id)
        if existing_nav is None or existing_nav.investment_id != investment_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="NAV not found.",
            )
        nav_value = _parse_decimal(payload.get("nav_value"), field="nav_value")
        currency = _validate_currency(str(payload.get("currency", "")))
        raw_source = payload.get("source")
        source: str | None = str(raw_source).strip() if raw_source not in (None, "") else None
        updated = await service.update_nav(
            nav_id=nav_id,
            nav_value=nav_value,
            currency=currency,
            source=source,
            created_by=session.user_id,
        )

    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="NAV not found.",
        )
    logger.info(
        "investments-update-nav: tenant=%s user=%s investment=%s nav=%s",
        session.tenant_id,
        session.user_id,
        investment_id,
        nav_id,
    )
    return JSONResponse(content=jsonable_encoder(_nav_payload(updated)))


# ---------------------------------------------------------------------------
# DELETE /investments/{id}/navs/{nav_id}
# ---------------------------------------------------------------------------


@router.delete(
    "/investments/{investment_id}/navs/{nav_id}", dependencies=[Depends(require_role("owner"))]
)
async def investments_delete_nav(
    request: Request,
    investment_id: UUID,
    nav_id: UUID,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> JSONResponse:
    """Hard-delete a NAV row."""
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        service = _build_service(db_session)
        existing_inv = await service.get_investment(investment_id)
        if existing_inv is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Investment not found.",
            )
        existing_nav = await service.get_nav(nav_id)
        if existing_nav is None or existing_nav.investment_id != investment_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="NAV not found.",
            )
        await service.delete_nav(nav_id)

    logger.info(
        "investments-delete-nav: tenant=%s user=%s investment=%s nav=%s",
        session.tenant_id,
        session.user_id,
        investment_id,
        nav_id,
    )
    return JSONResponse(content={"deleted": True})


# ---------------------------------------------------------------------------
# POST /investments/{id}/cashflows
# ---------------------------------------------------------------------------


@router.post(
    "/investments/{investment_id}/cashflows", dependencies=[Depends(require_role("owner"))]
)
async def investments_add_cashflow(
    request: Request,
    investment_id: UUID,
    payload: dict,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> JSONResponse:
    """Append a cashflow row to an investment.

    Body: ``{"flow_timestamp": "...", "flow_type": "...",
    "flow_kind": "actual"|"plan", "amount": "...", "currency": "...",
    "description": "..."}``. ``flow_timestamp`` may be omitted if
    ``flow_date`` is supplied — the route then synthesises a
    timestamp at 12:00 UTC on that date (the operational convention
    from ADR-0043 §1).
    """
    flow_timestamp = _parse_flow_timestamp(payload)
    flow_type = str(payload.get("flow_type", ""))
    if flow_type not in _VALID_FLOW_TYPES:
        return _bad_request(
            f"flow_type must be one of {sorted(_VALID_FLOW_TYPES)}.",
            field="flow_type",
        )
    flow_kind = str(payload.get("flow_kind", ""))
    if flow_kind not in _VALID_FLOW_KINDS:
        return _bad_request(
            f"flow_kind must be one of {sorted(_VALID_FLOW_KINDS)}.",
            field="flow_kind",
        )
    amount = _parse_decimal(payload.get("amount"), field="amount")
    currency = _validate_currency(str(payload.get("currency", "")))
    raw_desc = payload.get("description")
    description: str | None = str(raw_desc).strip() if raw_desc not in (None, "") else None

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        service = _build_service(db_session)
        existing = await service.get_investment(investment_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Investment not found.",
            )
        try:
            cashflow = await service.add_cashflow(
                investment_id=investment_id,
                flow_timestamp=flow_timestamp,
                flow_type=flow_type,
                flow_kind=flow_kind,
                amount=amount,
                currency=currency,
                description=description,
                created_by=session.user_id,
            )
        except InvestorFlowScopeError as exc:
            return _domain_error_response(exc)

    logger.info(
        "investments-add-cashflow: tenant=%s user=%s investment=%s cf=%s",
        session.tenant_id,
        session.user_id,
        investment_id,
        cashflow.id,
    )
    return JSONResponse(content=jsonable_encoder(_cashflow_payload(cashflow)))


# ---------------------------------------------------------------------------
# PUT /investments/{id}/cashflows/{cashflow_id}
# ---------------------------------------------------------------------------


@router.put(
    "/investments/{investment_id}/cashflows/{cashflow_id}",
    dependencies=[Depends(require_role("owner"))],
)
async def investments_update_cashflow(
    request: Request,
    investment_id: UUID,
    cashflow_id: UUID,
    payload: dict,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> JSONResponse:
    """Update mutable fields on a cashflow row."""
    engine = _engine(request)
    update_kwargs: dict[str, object] = {}

    if "flow_timestamp" in payload or "flow_date" in payload:
        update_kwargs["flow_timestamp"] = _parse_flow_timestamp(payload)
    if "flow_type" in payload and payload["flow_type"] is not None:
        ft = str(payload["flow_type"])
        if ft not in _VALID_FLOW_TYPES:
            return _bad_request(
                f"flow_type must be one of {sorted(_VALID_FLOW_TYPES)}.",
                field="flow_type",
            )
        update_kwargs["flow_type"] = ft
    if "flow_kind" in payload and payload["flow_kind"] is not None:
        fk = str(payload["flow_kind"])
        if fk not in _VALID_FLOW_KINDS:
            return _bad_request(
                f"flow_kind must be one of {sorted(_VALID_FLOW_KINDS)}.",
                field="flow_kind",
            )
        update_kwargs["flow_kind"] = fk
    if "amount" in payload and payload["amount"] is not None:
        update_kwargs["amount"] = _parse_decimal(payload["amount"], field="amount")
    if "currency" in payload and payload["currency"] is not None:
        update_kwargs["currency"] = _validate_currency(str(payload["currency"]))
    if "description" in payload:
        raw = payload["description"]
        update_kwargs["description"] = str(raw).strip() if raw not in (None, "") else None

    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        service = _build_service(db_session)
        existing_inv = await service.get_investment(investment_id)
        if existing_inv is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Investment not found.",
            )
        target = await service.get_cashflow(cashflow_id)
        if target is None or target.investment_id != investment_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Cashflow not found.",
            )
        try:
            updated = await service.update_cashflow(
                cashflow_id, acting_user=session.user_id, **update_kwargs
            )
        except InvestorFlowScopeError as exc:
            return _domain_error_response(exc)

    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cashflow not found.",
        )
    logger.info(
        "investments-update-cashflow: tenant=%s user=%s investment=%s cf=%s",
        session.tenant_id,
        session.user_id,
        investment_id,
        cashflow_id,
    )
    return JSONResponse(content=jsonable_encoder(_cashflow_payload(updated)))


# ---------------------------------------------------------------------------
# DELETE /investments/{id}/cashflows/{cashflow_id}
# ---------------------------------------------------------------------------


@router.delete(
    "/investments/{investment_id}/cashflows/{cashflow_id}",
    dependencies=[Depends(require_role("owner"))],
)
async def investments_delete_cashflow(
    request: Request,
    investment_id: UUID,
    cashflow_id: UUID,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> JSONResponse:
    """Hard-delete a cashflow row."""
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        service = _build_service(db_session)
        existing_inv = await service.get_investment(investment_id)
        if existing_inv is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Investment not found.",
            )
        target = await service.get_cashflow(cashflow_id)
        if target is None or target.investment_id != investment_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Cashflow not found.",
            )
        await service.delete_cashflow(cashflow_id, acting_user=session.user_id)

    logger.info(
        "investments-delete-cashflow: tenant=%s user=%s investment=%s cf=%s",
        session.tenant_id,
        session.user_id,
        investment_id,
        cashflow_id,
    )
    return JSONResponse(content={"deleted": True})


# ---------------------------------------------------------------------------
# Security identifiers (ADR-0096) — the human-operated mapping surface.
#
# Nested-resource routes mirroring the NAV / cashflow idiom above: an
# owner-only, CSRF-protected write path; cross-tenant / cross-investment ids
# resolve to 404 (never 403), exactly as the sibling routes do. Each mutation
# returns the freshly re-listed identifier set so the detail-page panel
# re-renders wholesale — set-primary flips two rows, so a whole-panel refresh
# is the honest shape for all three. Verb choice: POST for the /primary action
# segment (an action on a sub-resource), matching the create/append verb idiom
# of the sibling nested routes.
# ---------------------------------------------------------------------------


async def _identifier_list_response(
    service: InvestmentService, investment_id: UUID
) -> JSONResponse:
    """Re-list an investment's identifiers as the panel-refresh payload."""
    rows = await service.list_identifiers(investment_id)
    return JSONResponse(
        content=jsonable_encoder({"identifiers": [_identifier_payload(r) for r in rows]})
    )


@router.post(
    "/investments/{investment_id}/identifiers",
    dependencies=[Depends(require_role("owner"))],
)
async def investments_add_identifier(
    request: Request,
    investment_id: UUID,
    payload: dict,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> JSONResponse:
    """Add one human-confirmed identifier (``source='manual'``).

    Body: ``{"scheme": "isin"|…|"preqin"|"pitchbook", "value": "..."}``.
    The row is never primary on creation; promotion is a separate action.
    A duplicate ``(scheme, value)`` — on this investment or, for a real
    identity, elsewhere in the tenant — is a 409 conflict.
    """
    scheme = str(payload.get("scheme", "")).strip()
    if scheme not in IDENTIFIER_SCHEMES:
        return _bad_request(
            f"scheme must be one of {sorted(IDENTIFIER_SCHEMES)}.",
            field="scheme",
        )
    raw_value = payload.get("value")
    value = str(raw_value).strip() if raw_value is not None else ""
    if not value:
        return _bad_request("value must not be empty.", field="value")

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        service = _build_service(db_session)
        # Cross-tenant safety: a foreign-tenant investment id is invisible
        # (RLS) and therefore appears as absence → 404, never 403.
        existing = await service.get_investment(investment_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Investment not found.",
            )
        try:
            await service.add_identifier_manual(
                investment_id=investment_id,
                scheme=scheme,
                value=value,
                user_id=session.user_id,
            )
        except IntegrityError:
            logger.info(
                "investments-add-identifier: conflict scheme=%s on tenant=%s",
                scheme,
                session.tenant_id,
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"An identifier {scheme}:{value} already exists for this "
                    "investment or another investment in this tenant."
                ),
            )
        response = await _identifier_list_response(service, investment_id)

    logger.info(
        "investments-add-identifier: tenant=%s user=%s investment=%s scheme=%s",
        session.tenant_id,
        session.user_id,
        investment_id,
        scheme,
    )
    return response


@router.post(
    "/investments/{investment_id}/identifiers/{identifier_id}/primary",
    dependencies=[Depends(require_role("owner"))],
)
async def investments_set_primary_identifier(
    request: Request,
    investment_id: UUID,
    identifier_id: UUID,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> JSONResponse:
    """Make one identifier the investment's single primary.

    Demotes the current primary (if any) and promotes the target in one
    transaction. An id that does not belong to the investment in the active
    tenant resolves to 404.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        service = _build_service(db_session)
        existing = await service.get_investment(investment_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Investment not found.",
            )
        promoted = await service.set_primary_identifier(
            investment_id=investment_id,
            identifier_id=identifier_id,
            user_id=session.user_id,
        )
        if not promoted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Identifier not found.",
            )
        response = await _identifier_list_response(service, investment_id)

    logger.info(
        "investments-set-primary-identifier: tenant=%s user=%s investment=%s identifier=%s",
        session.tenant_id,
        session.user_id,
        investment_id,
        identifier_id,
    )
    return response


@router.delete(
    "/investments/{investment_id}/identifiers/{identifier_id}",
    dependencies=[Depends(require_role("owner"))],
)
async def investments_delete_identifier(
    request: Request,
    investment_id: UUID,
    identifier_id: UUID,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> JSONResponse:
    """Delete one identifier row.

    Deleting the primary is allowed and leaves the investment without a
    primary by design (ADR-0096 §3). An id that does not belong to the
    investment in the active tenant resolves to 404.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        service = _build_service(db_session)
        existing = await service.get_investment(investment_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Investment not found.",
            )
        deleted = await service.delete_identifier(
            investment_id=investment_id,
            identifier_id=identifier_id,
            user_id=session.user_id,
        )
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Identifier not found.",
            )
        response = await _identifier_list_response(service, investment_id)

    logger.info(
        "investments-delete-identifier: tenant=%s user=%s investment=%s identifier=%s",
        session.tenant_id,
        session.user_id,
        investment_id,
        identifier_id,
    )
    return response


# ---------------------------------------------------------------------------
# Position ledger and valuation mode (ADR-0097 / ADR-0098) — strand S5.
#
# Nested-resource routes mirroring the NAV / cashflow / identifier idiom
# above: owner-only, CSRF-protected write paths; cross-tenant and
# cross-investment ids resolve to 404, never 403. Each mutation returns the
# freshly re-read position summary so the detail-page panel re-renders
# wholesale — a ledger write moves holdings, the latest computed NAV, and the
# flip's eligibility together, so a whole-panel refresh is the honest shape.
#
# Every write runs inside one `tenant_context` transaction, and the
# computed-NAV materialisation (ADR-0098 §3) rides it synchronously in the
# service layer. The route performs no provider I/O and spawns nothing: the
# web layer stays thin (ADR-0093 precedent).
#
# Verb choice for the flip: POST to a named action segment
# (`/valuation-mode/unitised`), matching `/identifiers/{id}/primary`. The flip
# is a one-way act, not a settable field — `PATCH /valuation-mode` with a
# body would invite the reverse write that ADR-0097 §6 forbids.
# ---------------------------------------------------------------------------


async def _position_summary_response(
    service: InvestmentService, investment_id: UUID
) -> JSONResponse:
    """Re-read the position summary as the panel-refresh payload."""
    summary = await service.get_position_summary(investment_id)
    if summary is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Investment not found.",
        )
    return JSONResponse(content=jsonable_encoder({"positions": _position_summary_payload(summary)}))


def _read_txn_payload(
    payload: dict,
) -> tuple[_date, Decimal, Decimal | None, Decimal | None, str | None, str | None]:
    """Parse the fields common to the create and update transaction bodies.

    Returns ``(trade_date, units, price_per_unit, consideration, note,
    source)``. Raises :class:`HTTPException` (400) via the shared parse
    helpers on a malformed date or number.
    """
    trade_date = _parse_iso_date(payload.get("trade_date"), field="trade_date")
    units = _parse_decimal(payload.get("units"), field="units")
    raw_price = payload.get("price_per_unit")
    price_per_unit = (
        _parse_decimal(raw_price, field="price_per_unit") if raw_price not in (None, "") else None
    )
    raw_consideration = payload.get("consideration")
    consideration = (
        _parse_decimal(raw_consideration, field="consideration")
        if raw_consideration not in (None, "")
        else None
    )
    raw_note = payload.get("note")
    note = str(raw_note).strip() if raw_note not in (None, "") else None
    raw_source = payload.get("source")
    source = str(raw_source).strip() if raw_source not in (None, "") else None
    return trade_date, units, price_per_unit, consideration, note, source


@router.post(
    "/investments/{investment_id}/positions",
    dependencies=[Depends(require_role("owner"))],
)
async def investments_add_position(
    request: Request,
    investment_id: UUID,
    payload: dict,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> JSONResponse:
    """Add one ledger transaction (``ingest_origin='manual'``).

    Body: ``{"txn_type": "opening"|"buy"|"sell"|"transfer", "trade_date":
    "YYYY-MM-DD", "units": "...", "currency": "...", "price_per_unit": "...",
    "consideration": "...", "note": "...", "source": "..."}``.

    The currency must equal the investment's (ADR-0097 §5) — the form carries
    it read-only, and a mismatch is a 400 rather than a silent conversion.
    A second ``opening`` is refused by the partial unique index and rendered
    as a 409.
    """
    txn_type = str(payload.get("txn_type", "")).strip()
    if txn_type not in _VALID_TXN_TYPES:
        return _bad_request(
            f"txn_type must be one of {sorted(_VALID_TXN_TYPES)}.",
            field="txn_type",
        )
    currency = _validate_currency(str(payload.get("currency", "")))
    (
        trade_date,
        units,
        price_per_unit,
        consideration,
        note,
        source,
    ) = _read_txn_payload(payload)

    rule_error = _validate_txn_rules(txn_type, units, price_per_unit)
    if rule_error is not None:
        return rule_error

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        service = _build_service(db_session)
        existing = await service.get_investment(investment_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Investment not found.",
            )
        try:
            created = await service.add_position_transaction(
                investment_id=investment_id,
                txn_type=txn_type,
                trade_date=trade_date,
                units=units,
                currency=currency,
                ingest_origin="manual",
                created_by=session.user_id,
                price_per_unit=price_per_unit,
                consideration=consideration,
                note=note,
                source=source,
            )
        except (CurrencyMismatchError, NonNegativeHoldingsError) as exc:
            return _domain_error_response(exc)
        except IntegrityError:
            # The one-opening-per-investment partial unique index, or a CHECK
            # the route-level rules did not already cover.
            logger.info(
                "investments-add-position: conflict type=%s on investment=%s",
                txn_type,
                investment_id,
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "This transaction conflicts with the existing ledger; an "
                    "investment may carry at most one opening transaction."
                ),
            )
        response = await _position_summary_response(service, investment_id)

    logger.info(
        "investments-add-position: tenant=%s user=%s investment=%s txn=%s type=%s",
        session.tenant_id,
        session.user_id,
        investment_id,
        created.id,
        txn_type,
    )
    return response


@router.put(
    "/investments/{investment_id}/positions/{transaction_id}",
    dependencies=[Depends(require_role("owner"))],
)
async def investments_update_position(
    request: Request,
    investment_id: UUID,
    transaction_id: UUID,
    payload: dict,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> JSONResponse:
    """Restate one ledger transaction in place.

    ``txn_type``, ``currency``, and ``ingest_origin`` are immutable; retyping
    a row is a delete plus a create. An ``'excel'``-origin row may be edited,
    and the panel warns that the next Excel import restates it (ADR-0097 §7).
    A transaction that does not belong to this investment resolves to 404.
    """
    (
        trade_date,
        units,
        price_per_unit,
        consideration,
        note,
        source,
    ) = _read_txn_payload(payload)

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        service = _build_service(db_session)
        existing = await service.get_investment(investment_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Investment not found.",
            )
        # The sign and price rules depend on txn_type, which the edit cannot
        # change — read it from the persisted row rather than the body.
        current = await service.list_position_transactions(investment_id)
        target = next((t for t in current if t.id == transaction_id), None)
        if target is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Transaction not found.",
            )
        rule_error = _validate_txn_rules(target.txn_type, units, price_per_unit)
        if rule_error is not None:
            return rule_error

        try:
            updated = await service.update_position_transaction(
                investment_id=investment_id,
                transaction_id=transaction_id,
                trade_date=trade_date,
                units=units,
                acting_user=session.user_id,
                price_per_unit=price_per_unit,
                consideration=consideration,
                note=note,
                source=source,
            )
        except NonNegativeHoldingsError as exc:
            return _domain_error_response(exc)
        if updated is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Transaction not found.",
            )
        response = await _position_summary_response(service, investment_id)

    logger.info(
        "investments-update-position: tenant=%s user=%s investment=%s txn=%s",
        session.tenant_id,
        session.user_id,
        investment_id,
        transaction_id,
    )
    return response


@router.delete(
    "/investments/{investment_id}/positions/{transaction_id}",
    dependencies=[Depends(require_role("owner"))],
)
async def investments_delete_position(
    request: Request,
    investment_id: UUID,
    transaction_id: UUID,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> JSONResponse:
    """Delete one ledger transaction.

    Rejected with a 400 when the deletion would drive derived holdings below
    zero on any date — removing a buy that a later sell depends on. On a
    unitised investment the computed NAV rows are recomputed in the same
    transaction, which removes any now-stranded ``'system'`` rows.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        service = _build_service(db_session)
        existing = await service.get_investment(investment_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Investment not found.",
            )
        try:
            deleted = await service.delete_position_transaction(
                investment_id=investment_id,
                transaction_id=transaction_id,
                acting_user=session.user_id,
            )
        except NonNegativeHoldingsError as exc:
            return _domain_error_response(exc)
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Transaction not found.",
            )
        response = await _position_summary_response(service, investment_id)

    logger.info(
        "investments-delete-position: tenant=%s user=%s investment=%s txn=%s",
        session.tenant_id,
        session.user_id,
        investment_id,
        transaction_id,
    )
    return response


@router.post(
    "/investments/{investment_id}/valuation-mode/unitised",
    dependencies=[Depends(require_role("owner"))],
)
async def investments_flip_to_unitised(
    request: Request,
    investment_id: UUID,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> JSONResponse:
    """Switch an investment to unitised valuation — one-way (ADR-0097 §6).

    Preconditions: the type is ``listed_equity``/``listed_bonds``/``cash``
    (the last since ADR-0103 §1) and an ``opening`` transaction exists. A
    violated precondition — including a second flip of an already-unitised
    investment — is a 409 carrying the same sentence the panel shows beneath
    its disabled button.

    On success the investment's ``'live'``-origin NAV rows are deleted (the
    F1 defect artifacts; ``'excel'`` and ``'manual'`` rows are never touched)
    and the full computed-NAV series is materialised, all in this request's
    transaction.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        service = _build_service(db_session)
        existing = await service.get_investment(investment_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Investment not found.",
            )
        try:
            report = await service.flip_to_unitised(investment_id, acting_user=session.user_id)
        except ValuationModeError as exc:
            return _domain_error_response(exc)
        response = await _position_summary_response(service, investment_id)

    logger.info(
        "investments-flip-to-unitised: tenant=%s user=%s investment=%s "
        "inserted=%d updated=%d deleted=%d",
        session.tenant_id,
        session.user_id,
        investment_id,
        report.inserted,
        report.updated,
        report.deleted,
    )
    return response
