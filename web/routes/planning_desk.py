# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Web Planning Desk surface — the Cash Flow Planning lens (ADR-0104 §4/§6).

The seventh Area's first working section. One endpoint,
``GET /api/planning-desk/cash-flow-planning``, renders the whole lens: the
period-grid balance table, the Plotly timeline over the same DTO, and — as an
out-of-band swap — the sticky scenario parameter strip that sits above both
Sections of the area.

**The parameters are the entire page state** (ADR-0104 §4). There is no
server-side scenario state and no session store: every render calls
:func:`~services.investments.cash_flow_timeline.load_cash_flow_planning_inputs`
and :func:`~services.investments.cash_flow_timeline.project_cash_flow_planning`
from the request parameters alone, and every control on the surface is an HTMX
GET that re-issues *all* of them. A scenario is therefore reproducible from
*(book, URL)* — which is what makes "Copy scenario link" an honest affordance
rather than a promise the server cannot keep.

The overlay travels in the fixed ``t{n}_…`` flat encoding
(:mod:`services.overlay.serialisation`). This module never invents a key: it
parses with :func:`~services.overlay.serialisation.parse_overlay` and re-emits
with :func:`~services.overlay.serialisation.serialise_overlay`, so a chip's
remove-link is the round-trip of the set minus one transformation, re-indexed
by the serialiser rather than by hand.

**The pacing rows are the first chip producer** (S2.5). A slider does not
format a ``t{n}_`` key — it cannot, and the round-trip law depends on it not
trying. It states its *intent* in two transient parameters, ``pace_id`` and
``pace_factor``, and the server merges that intent into the parsed overlay
(:func:`_repace`) and re-emits the whole set through
:func:`~services.overlay.serialisation.serialise_overlay`. The canonical URL —
the one a "Copy scenario link" would hand on — goes back in the ``HX-Push-Url``
header, so the address bar holds the *encoded* set and never the intent that
produced it.

**The hypothetical-transaction form is the second** (S2.6), and it is the same
idiom: seven transient ``hyp_…`` fields state one entry, the server builds an
:class:`~services.overlay.contract.InsertTransaction` from them
(:func:`_parse_hyp_entry`), **appends** it to the parsed set, and re-emits the
whole thing. Nothing is written — not a ``PlanFlow``, not a
``position_transaction``, not a session key. A hypothetical trade exists only
as a parameter, which is why removing its row and removing its chip are the
same link (ADR-0104 §7, D21: actuals on the investment detail page,
hypotheticals here).

Errors are outcomes, not diagnostics (ADR-0104 §4). Each typed failure renders
an actionable partial that names the thing to fix:

* a malformed parameter set (:class:`~services.overlay.errors.OverlayParseError`,
  :class:`~services.overlay.errors.FactorOutOfBoundsError`,
  :class:`~core.exceptions.PlanHorizonInvalidError`, an unreadable view
  toggle) — **HTTP 400**, the one status on this surface that is not 200: a URL
  that cannot be read is a bad request, and saying so in the status line keeps
  a hand-edited link from masquerading as an empty scenario;
* a book with no plan/actual seam
  (:class:`~core.exceptions.PlanSeamMissingError`) — "import a statement first";
* two cash positions in one currency
  (:class:`~core.exceptions.DuplicateCashPositionError`) — the currency is named;
* a missing FX rate (:class:`~core.exceptions.MissingFxRateError`) — the missing
  **pair** is named (ADR-0104 §3): the operator needs to know *which* rate to
  supply, and "USD" alone does not say what it is missing against. An
  ``fx_shock`` does not paper this over: it restates an FX path, it never
  invents one, so a shock on an unpriced currency still fails here;
* a transformation the frames refuse
  (:class:`~services.overlay.errors.OverlayExecutionError`) — a stale link
  against a book that has moved on.

**Provenance decides the status line, not the error class.** The same
:class:`~services.overlay.errors.OverlayExecutionError` means two different
things depending on how it arrived. A set that merely *travelled* in a URL and
that the book can no longer carry is an outcome — **HTTP 200**, chips on
screen, each one removable. A request that *states a new entry* and has it
refused is a bad request — **HTTP 400**, the set unchanged, nothing pushed to
the address bar, and the form re-rendered with what the operator typed still
in it. The distinction is the one S2.5 already draws between an encoded
parameter and a transient intent, and it is why an entry the executor refuses
(a trade dated into history, a currency the plan world holds no cash in) never
lands in the parameter set at all.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, cast
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core.exceptions import (
    CaseClosedError,
    CaseStateInvalid,
    CoverageInputMissing,
    CoverageInputOutOfRange,
    DuplicateCashPositionError,
    LimitSetNotEffective,
    MissingFxRateError,
    PlanHorizonInvalidError,
    PlanSeamMissingError,
)
from core.repositories._session import tenant_context
from core.repositories.asset_class_repository import AssetClassRepository
from core.repositories.case_repository import CaseDTO, CaseRepository
from core.repositories.fx_rate_repository import FxRateRepository
from core.repositories.investment_cashflow_repository import (
    InvestmentCashflowRepository,
)
from core.repositories.investment_nav_repository import InvestmentNavRepository
from core.repositories.investment_repository import (
    InvestmentDTO,
    InvestmentRepository,
)
from core.repositories.limits_repository import LimitsRepository
from core.repositories.tenant_repository import TenantRepository
from services.auth.session import SessionDTO, SessionRepository
from services.chart_specs import (
    CurrencyView,
    WorldView,
    build_cash_flow_timeline_spec,
    build_scenario_impact_pair,
)
from services.investments import (
    DEFAULT_HORIZON_QUARTERS,
    FACTOR_STEP,
    HORIZON_QUARTERS,
    NO_PLAN_NOTE,
    PLAN_SOURCE_REPORTED,
    PLAN_SOURCE_TA,
    CashFlowPlanningResult,
    CashFlowTimeline,
    PacingRow,
    Periodisation,
    build_pacing_rows,
    capital_account_ids,
    describe_shift,
    load_called_amounts,
    load_cash_flow_planning_inputs,
    project_cash_flow_planning,
)
from services.investments.archetype import Archetype, resolve_archetype
from services.planning_desk import (
    CompositionPair,
    FamilyHeadroomDelta,
    KpiDelta,
    ScenarioResult,
    assemble_scenario_result,
    load_scenario_result_inputs,
)

# `derive_consideration` is the executor's own derivation, reused rather than
# mirrored (S2.6, §1.2): the cash-effect cell has to state exactly the amount
# `execute_insert_transaction` will settle against the cash path — stated
# consideration wins, else `units × price_per_unit` — and a second copy of that
# rule here would be a number the table promises and the engine need not keep.
# It is public API of the overlay package (ADR-0104 §8.4) for that reason; until
# S34.1 it was reached by a private cross-package import.
from services.overlay import (
    FACTOR_MAX,
    FACTOR_MIN,
    FACTOR_NEUTRAL,
    FxShock,
    InsertTransaction,
    MarketShock,
    Overlay,
    OverlayError,
    PlanFrames,
    RepaceFlows,
    Transformation,
    derive_consideration,
    parse_overlay,
    serialise_overlay,
)

from web.auth import require_session, verify_csrf

logger = logging.getLogger(__name__)
router = APIRouter()

#: The section endpoint. Every control on the lens re-issues it.
SECTION_URL: str = "/api/planning-desk/cash-flow-planning"

#: The composition drill-down endpoint (S34.5). The Scenario Analysis result
#: region loads it lazily — the composition diff is the secondary surface, off
#: the main render path (ADR-0104 §5, §7).
COMPOSITION_URL: str = "/api/planning-desk/scenario-composition"

#: The area page the "Copy scenario link" affordance points at — the URL a
#: shared scenario is *opened* from, as distinct from the partial it renders
#: through. The Cases "Capture scenario" affordance also deep-links here with a
#: ``?case=<id>`` marker (ADR-0107 C5); the Cases surface imports this constant so
#: the marker URL is built against one target, the C4 ``JOURNAL_DEEP_LINK`` idiom.
AREA_URL: str = "/planning-desk"

#: The scenario-snapshot pin endpoint (ADR-0107 C5). One URL, two verbs: ``GET``
#: renders the pin dialog (the ``list_open`` case picker + comment), ``POST``
#: writes the frozen snapshot to the chosen case's timeline.
PIN_SCENARIO_URL: str = "/api/planning-desk/pin-scenario"

#: The `case=` query key — the navigation marker a case sets when it sends the PM
#: to the Desk "capturing for" it (ADR-0107 C5, binding decision 4). It is *not*
#: scenario state: it rides every ``_query`` round-trip but never enters the
#: overlay serialisation, a snapshot's canonical query, or any persisted state.
_CASE_MARKER_KEY: str = "case"

_SECTION_TEMPLATE: str = "_partials/planning_desk_cash_flow_planning.html"
_ERROR_TEMPLATE: str = "_partials/planning_desk_error.html"
_COMPOSITION_TEMPLATE: str = "_partials/planning_desk_composition.html"
_PIN_DIALOG_TEMPLATE: str = "_partials/planning_desk_pin_dialog.html"
_PIN_CONFIRM_TEMPLATE: str = "_partials/planning_desk_pin_confirm.html"

#: The coverage-engine failures the scenario assembly can surface, caught so a
#: scenario that cannot be scored degrades to a notice in the result region
#: rather than taking the Cash Flow Planning lens down with it (ADR-0104 §4).
_SCENARIO_ERRORS: tuple[type[Exception], ...] = (
    MissingFxRateError,
    LimitSetNotEffective,
    CoverageInputMissing,
    CoverageInputOutOfRange,
    OverlayError,
)

#: Per KPI key, whether a *rise* is the favourable move — drives the delta
#: badge's tone (ADR-0067 pair idiom). More AUM, more headroom and more cash are
#: better; more limit breaches are worse. Presentation only: the figures are the
#: DTO's, never recomputed here (ADR-0104 §5).
_KPI_HIGHER_IS_BETTER: dict[str, bool] = {
    "aum": True,
    "tightest_anlv_headroom": True,
    "functional_cash_t0_plus_4q": True,
    "limit_breaches": False,
}

#: Coverage status → the bar's theme-token modifier. The status the engine
#: assigns drives the colour; the template maps the modifier to a token, never a
#: raw hex (S34.4 discipline).
_STATUS_TONE: dict[str, str] = {
    "OK": "ok",
    "WARN": "warn",
    "BREACH": "breach",
    "NO_LIMIT": "muted",
    "UNALLOCATED": "muted",
}

#: The family labels the headroom table prefixes each class row with.
_FAMILY_LABEL: dict[str, str] = {"saa": "SAA", "anlv": "AnlV"}

_MILLIONS: float = 1_000_000.0


class ViewParameterError(ValueError):
    """Raised when a view toggle carries a value the surface never emits.

    The three toggles (periodisation, horizon, currency view, world view) are
    written by this module's own links, so a value outside their closed sets
    can only come from a hand-edited or truncated URL. It is treated exactly
    like a malformed overlay parameter — a bad request that says which key is
    unreadable — rather than silently falling back to a default, which would
    answer a question the operator did not ask.
    """


# ---------------------------------------------------------------------------
# Wiring helpers
# ---------------------------------------------------------------------------


def _templates(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)


def _engine(request: Request) -> AsyncEngine:
    return cast(AsyncEngine, request.app.state.engine)


# ---------------------------------------------------------------------------
# Parameter parsing — the page state, read back off the request
# ---------------------------------------------------------------------------


def _parse_periodisation(raw: str) -> Periodisation:
    """Parse the periodisation toggle.

    Raises:
        ViewParameterError: If the value is not one of the two periodisations.
    """
    try:
        return Periodisation(raw)
    except ValueError as exc:
        offered = [member.value for member in Periodisation]
        raise ViewParameterError(f"'periodisation={raw}' is not one of {offered}") from exc


def _parse_horizon(raw: str) -> int:
    """Parse the horizon toggle into a quarter count.

    The *membership* check belongs to
    :func:`~services.investments.cash_flow_timeline.build_cash_flow_timeline`,
    which raises :class:`~core.exceptions.PlanHorizonInvalidError` against the
    single definition of the offered horizons. This function only establishes
    that the value is an integer at all.

    Raises:
        ViewParameterError: If the value is not an integer.
    """
    try:
        return int(raw)
    except ValueError as exc:
        raise ViewParameterError(f"'horizon={raw}' is not a whole number of quarters") from exc


def _parse_currency_view(raw: str) -> CurrencyView:
    """Parse the per-currency / functional-only toggle.

    Raises:
        ViewParameterError: If the value is not one of the two views.
    """
    try:
        return CurrencyView(raw)
    except ValueError as exc:
        offered = [member.value for member in CurrencyView]
        raise ViewParameterError(f"'currency_view={raw}' is not one of {offered}") from exc


def _parse_view(raw: str) -> WorldView:
    """Parse the Baseline/Scenario toggle.

    Raises:
        ViewParameterError: If the value is not one of the two worlds.
    """
    try:
        return WorldView(raw)
    except ValueError as exc:
        offered = [member.value for member in WorldView]
        raise ViewParameterError(f"'view={raw}' is not one of {offered}") from exc


def _overlay_from(request: Request) -> Overlay:
    """Parse the overlay out of the request's query parameters.

    The query string is handed over as **ordered pairs**, not as a mapping:
    :func:`~services.overlay.serialisation.parse_overlay` rejects a duplicate
    key rather than resolving it by a last-one-wins rule nobody could
    reproduce from the URL, and a mapping would have collapsed the duplicate
    before it ever saw it.

    ``pace_id`` and ``pace_factor`` travel through untouched: the encoding
    ignores every key outside the ``t{n}_`` namespace, which is exactly what
    lets a control state an intent beside the set it is transforming.

    Raises:
        OverlayError: On any malformed parameter set — the whole hierarchy,
            :class:`~services.overlay.errors.FactorOutOfBoundsError` included.
    """
    return parse_overlay(request.query_params.multi_items())


def _parse_pacing_intent(
    pace_id: str | None, pace_factor: str | None
) -> tuple[UUID, Decimal] | None:
    """Parse a slider's intent — *this fund, at this factor*.

    The two parameters are a **transient control shape**, not part of the
    parameter set: they say what the operator just did, and the response
    replaces them with the encoded overlay they merge into. They are therefore
    parsed here and never re-emitted.

    Both must be present or both absent — a half-stated intent is a truncated
    URL, not a default.

    Args:
        pace_id: The capital-account investment the slider belongs to.
        pace_factor: The factor the slider was released at.

    Returns:
        The intent, or ``None`` where the request carries none.

    Raises:
        ViewParameterError: If exactly one of the two is present, or if either
            is unreadable. Like the view toggles, these values are written by
            this module's own links, so a malformed one can only come from a
            hand-edited URL — and is answered as a bad request rather than as a
            silently dropped interaction.
    """
    if pace_id is None and pace_factor is None:
        return None
    if pace_id is None or pace_factor is None:
        raise ViewParameterError(
            "a pacing interaction states both 'pace_id' and 'pace_factor'; "
            f"got pace_id={pace_id!r}, pace_factor={pace_factor!r}"
        )
    try:
        investment_id = UUID(pace_id)
    except ValueError as exc:
        raise ViewParameterError(f"'pace_id={pace_id}' is not an investment id") from exc
    try:
        factor = Decimal(pace_factor)
    except InvalidOperation as exc:
        raise ViewParameterError(f"'pace_factor={pace_factor}' is not a decimal number") from exc
    return investment_id, factor


def _is_repace_of(transformation: Transformation, investment_id: UUID) -> bool:
    """Whether this transformation re-paces that investment."""
    return isinstance(transformation, RepaceFlows) and transformation.investment_id == investment_id


def _repace(overlay: Overlay, investment_id: UUID, factor: Decimal) -> Overlay:
    """Merge one slider's factor into the parameter set.

    **Replace in place, or append.** The investment's existing re-pacing keeps
    its position in the overlay, because application order is list order
    (ADR-0104 §2): a re-pacing that jumped to the end each time it was dragged
    would silently re-order the operator's scenario against the hypothetical
    transactions around it. Where the fund has no entry yet, the new one is
    appended.

    Two rules fall out of this one function rather than being enforced
    separately:

    * **One repace chip per investment.** Any *further* entries for the same
      fund are dropped — the surface never holds two, so a hand-built set that
      does is normalised the moment the operator touches that fund (its row is
      rendered off-slider until they do; see :func:`_build_pacing_rows`).
    * **The mid-position emits no chip** (ADR-0104 §4: it *is* the plan). At
      :data:`~services.overlay.contract.FACTOR_NEUTRAL` the entry is **removed**
      rather than written as ``×1.0`` — a chip stating "no change" is a
      parameter the scenario does not have.

    Args:
        overlay: The parsed parameter set.
        investment_id: The fund whose slider moved.
        factor: The factor it was released at.

    Returns:
        The merged overlay.

    Raises:
        FactorOutOfBoundsError: If ``factor`` lies outside the ADR-0104 §2
            bounds — raised by :class:`~services.overlay.contract.RepaceFlows`
            at construction, never re-checked here.
    """
    slot: int | None = None
    kept: list[Transformation] = []
    for transformation in overlay:
        if _is_repace_of(transformation, investment_id):
            if slot is None:
                slot = len(kept)
            continue
        kept.append(transformation)

    if factor == FACTOR_NEUTRAL:
        return tuple(kept)

    entry = RepaceFlows(investment_id=investment_id, factor=factor)
    kept.insert(len(kept) if slot is None else slot, entry)
    return tuple(kept)


def _without_repace(overlay: Overlay, investment_id: UUID) -> Overlay:
    """Return the overlay with every re-pacing of that investment dropped.

    The per-row Reset. It is :func:`_repace` at the mid-position — *back to
    plan* and *no chip* are the same statement (ADR-0104 §4), so they are the
    same code path, and a Reset clears a hand-built duplicate set as readily as
    a slider-produced single entry.
    """
    return _repace(overlay, investment_id, FACTOR_NEUTRAL)


# ---------------------------------------------------------------------------
# The hypothetical-transaction entry — the second transient control shape
# ---------------------------------------------------------------------------

#: The entry form's transient field namespace. Like ``pace_id``/``pace_factor``
#: these are *not* part of the parameter set: they state what the operator just
#: entered, and the response replaces them with the encoded transformation they
#: became. The overlay encoding ignores every key outside ``t{n}_``, which is
#: what lets the form state an intent beside the set it is appending to.
_HYP_PREFIX: str = "hyp_"

#: The seven fields, in the order the form renders them — the field shape of the
#: **actual**-entry form (``web/templates/investments/_position_form.html``,
#: ADR-0097 §2), minus the four persistence concerns an ephemeral transformation
#: does not have (``csrf_token``, ``transaction_id``, ``note``, ``source``).
_HYP_FIELDS: tuple[str, ...] = (
    "investment_id",
    "txn_type",
    "trade_date",
    "units",
    "price_per_unit",
    "consideration",
    "currency",
)

#: The two types the entry form offers (ADR-0104 §7; S2.6 binding decision 4).
#: The contract's other two are artefacts of the ledger rather than trades: an
#: ``opening`` is what the Excel import synthesises for a position it never saw
#: acquired, and a ``transfer`` is in-kind and has **no cash leg** — neither
#: settles against cash, which is the whole of what this surface states. A
#: URL-borne entry of another type still renders in the table: the set states
#: it, and the display does not censor it. It simply cannot be *entered* here.
_OFFERED_TXN_TYPES: tuple[str, ...] = ("buy", "sell")


def _hyp_key(field: str) -> str:
    """Return the request-parameter name of one entry-form field."""
    return f"{_HYP_PREFIX}{field}"


def _hyp_text(fields: Mapping[str, str | None], field: str) -> str:
    """Return a required entry field's raw value.

    Raises:
        ViewParameterError: If the field is absent or empty. The form marks
            every one of these ``required``, so an empty one is a hand-built or
            truncated request — answered as a bad request, never as a default.
    """
    value = (fields.get(field) or "").strip()
    if not value:
        raise ViewParameterError(
            f"a hypothetical transaction states '{_hyp_key(field)}'; it arrived empty or absent"
        )
    return value


def _hyp_decimal(field: str, raw: str) -> Decimal:
    """Convert one entry value to a Decimal — via ``Decimal(str)``, never ``float``.

    Raises:
        ViewParameterError: If ``raw`` is not a decimal number.
    """
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ViewParameterError(f"'{_hyp_key(field)}={raw}' is not a decimal number") from exc


def _hyp_optional_decimal(fields: Mapping[str, str | None], field: str) -> Decimal | None:
    """Parse an optional entry field as a Decimal.

    An empty field is *not stated* and yields ``None``: the executor derives
    what it can from what remains, and says so where it cannot
    (:class:`~services.overlay.errors.UnderivableConsiderationError`).

    Raises:
        ViewParameterError: If a stated value is not a decimal number.
    """
    raw = (fields.get(field) or "").strip()
    return _hyp_decimal(field, raw) if raw else None


def _assert_units_sign(txn_type: str, units: Decimal) -> None:
    """Enforce the ADR-0097 §2 sign rule the actual-entry form enforces.

    **The operator states the sign; no surface derives it.**
    ``web/routes/investments.py`` rejects a sell whose units are not negative
    rather than negating what was typed (``_validate_txn_rules``), and the
    hypothetical entry mirrors that exactly — the two forms describe a
    transaction the same way (ADR-0104 §2), and a sign convention that held on
    one of them and not the other would be the one difference an operator
    cannot see.

    The overlay contract itself does not re-check this: ``units`` is simply a
    signed quantity there, and a hand-built ``t{n}_`` set may state any sign it
    likes (the executor settles what the set says). The rule belongs to the
    *entry* surface, which is where it lives on both of them.

    Raises:
        ViewParameterError: If the sign contradicts the type.
    """
    if txn_type == "buy" and units <= 0:
        raise ViewParameterError("units must be greater than zero for a buy transaction")
    if txn_type == "sell" and units >= 0:
        raise ViewParameterError("units must be negative for a sell transaction")


def _parse_hyp_entry(fields: Mapping[str, str | None]) -> InsertTransaction:
    """Build one :class:`InsertTransaction` from the entry form's fields.

    The bounds this function *does* enforce are the entry surface's own — the
    offered type set, the ADR-0097 §2 sign rule, a positive price — and they are
    the ones the actual-entry form enforces too. Everything that depends on the
    **plan world** is left to the executor, which is the only thing that holds
    it: whether the trade date is past the seam, whether the currency matches
    the investment, whether that currency has a cash path to settle against, and
    whether a cash effect can be derived at all. Re-checking those here would be
    a second formulation of the plan world's rules, free to drift from the one
    that computes the scenario.

    Args:
        fields: The seven ``hyp_…`` values, keyed without the prefix. Absent and
            empty are the same thing — the form submits every field.

    Returns:
        The transformation to append to the parameter set.

    Raises:
        ViewParameterError: On any unreadable, absent, or out-of-contract field.
    """
    raw_id = _hyp_text(fields, "investment_id")
    try:
        investment_id = UUID(raw_id)
    except ValueError as exc:
        raise ViewParameterError(
            f"'{_hyp_key('investment_id')}={raw_id}' is not an investment id"
        ) from exc

    txn_type = _hyp_text(fields, "txn_type")
    if txn_type not in _OFFERED_TXN_TYPES:
        raise ViewParameterError(
            f"'{_hyp_key('txn_type')}={txn_type}' is not one of "
            f"{list(_OFFERED_TXN_TYPES)}: the Planning Desk enters trades that "
            f"settle against cash, and no other type does"
        )

    raw_date = _hyp_text(fields, "trade_date")
    try:
        trade_date = date.fromisoformat(raw_date)
    except ValueError as exc:
        raise ViewParameterError(
            f"'{_hyp_key('trade_date')}={raw_date}' is not an ISO-8601 date"
        ) from exc

    units = _hyp_decimal("units", _hyp_text(fields, "units"))
    price_per_unit = _hyp_optional_decimal(fields, "price_per_unit")
    consideration = _hyp_optional_decimal(fields, "consideration")

    _assert_units_sign(txn_type, units)
    if price_per_unit is not None and price_per_unit <= 0:
        raise ViewParameterError("price_per_unit must be greater than zero")

    return InsertTransaction(
        investment_id=investment_id,
        txn_type=txn_type,
        trade_date=trade_date,
        units=units,
        price_per_unit=price_per_unit,
        consideration=consideration,
        # Uppercased like every currency in the frames (ADR-0104 §1), so a
        # hand-built lowercase code compares as the currency it names rather
        # than failing as a currency the plan world does not hold.
        currency=_hyp_text(fields, "currency").upper(),
    )


# ---------------------------------------------------------------------------
# The shock-builder entries — the third and fourth transient control shapes
# (mockup ⑤, ADR-0104 §2/§6)
# ---------------------------------------------------------------------------

#: The two shock builders' transient field namespaces. Like ``hyp_…`` and the
#: pacing intent, these are **not** part of the parameter set: each card states
#: a scope and a magnitude, and the server merges them into an overlay entry and
#: re-emits the ``t{n}_`` encoding they became. The builder never formats an
#: overlay key (ADR-0104 §4). The two prefixes keep the cards' submissions apart
#: on one endpoint — a request carries one card's fields or none.
_MARKET_SHOCK_PREFIX: str = "mshock_"
_FX_SHOCK_PREFIX: str = "fxshock_"

#: The fields each card submits, in render order — the ``market_shock`` and
#: ``fx_shock`` halves of the ADR-0104 §2 encoding tables, minus the Operator and
#: Timing controls, which are the single fixed v1 option (E6) and carry no value
#: to the server (they render disabled and never submit).
_MARKET_SHOCK_FIELDS: tuple[str, ...] = ("archetype", "magnitude")
_FX_SHOCK_FIELDS: tuple[str, ...] = ("currency", "magnitude")


def _present_archetypes(frames: PlanFrames) -> list[Archetype]:
    """The archetypes the plan world's investments resolve to, in enum order.

    The offered set of the market builder's Scope, and the set
    :func:`_parse_market_shock_entry` validates a submission against — one
    formulation, so the dropdown and the gate cannot disagree. Cash lives in
    :attr:`~services.overlay.pipeline.PlanFrames.cash_paths`, not in
    ``investments`` (ADR-0104 §1), so it is not a market-shock target here — a
    market shock acts on value paths, which cash has none of.
    """
    present = {
        resolve_archetype(investment.investment_type) for investment in frames.investments.values()
    }
    return [archetype for archetype in Archetype if archetype in present]


def _held_currencies(frames: PlanFrames, functional_currency: str) -> list[str]:
    """The **non-functional** currencies the plan world holds, sorted.

    The offered set of the fx builder's Scope, and the gate
    :func:`_parse_fx_shock_entry` validates against. Gathered from both sides an
    ``fx_shock`` restates (ADR-0104 §3): the investments' position currency and
    the cash paths. The functional currency is excluded — a shock on the
    numéraire is the identity (:class:`~services.overlay.contract.FxShock`), so
    offering it would offer a no-op the operator could add and see nothing from.
    """
    functional = functional_currency.upper()
    held = {investment.currency for investment in frames.investments.values()}
    held |= set(frames.cash_paths.keys())
    return sorted(currency for currency in held if currency.upper() != functional)


def _shock_text(fields: Mapping[str, str | None], field: str, prefix: str) -> str:
    """Return a required shock field's raw value.

    Raises:
        ViewParameterError: If the field is absent or empty — the card marks
            both ``required``, so an empty one is a hand-built or truncated
            request, answered as a bad request rather than a default.
    """
    value = (fields.get(field) or "").strip()
    if not value:
        raise ViewParameterError(f"a shock states '{prefix}{field}'; it arrived empty or absent")
    return value


def _shock_decimal(fields: Mapping[str, str | None], field: str, prefix: str) -> Decimal:
    """Parse a required shock field as a Decimal — via ``Decimal(str)``.

    Raises:
        ViewParameterError: If the field is absent, empty, or not a decimal.
    """
    raw = _shock_text(fields, field, prefix)
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ViewParameterError(f"'{prefix}{field}={raw}' is not a decimal number") from exc


def _parse_market_shock_entry(
    fields: Mapping[str, str | None],
    *,
    present_archetypes: Sequence[Archetype],
) -> MarketShock:
    """Build one :class:`MarketShock` from the market builder's fields.

    The entry surface enforces its **own offered set**, exactly as
    :func:`_parse_hyp_entry` enforces :data:`_OFFERED_TXN_TYPES`: the Scope is a
    ``<select>`` over the archetypes the book holds, so a value outside that set
    — a hand-built request, or a stale one after the last holding of a class was
    archived — is refused here rather than folded into a shock that marks down a
    class the plan world does not have. That is not a *new* rule: an archetype no
    investment resolves to would execute as a silent no-op, and ADR-0104 §4
    forbids a scenario that quietly equals its baseline as loudly as a wrong one.

    The magnitude is **not** bounds-checked — the contract puts no bound on a
    shock's per-cent magnitude (:class:`MarketShock`), and a stress test is
    exactly where an operator may want an extreme one.

    Raises:
        ViewParameterError: On an absent or unreadable field, an ``archetype``
            the contract does not name, or one the book does not hold.
    """
    raw = _shock_text(fields, "archetype", _MARKET_SHOCK_PREFIX)
    try:
        archetype = Archetype(raw)
    except ValueError as exc:
        known = [member.value for member in Archetype]
        raise ViewParameterError(
            f"'{_MARKET_SHOCK_PREFIX}archetype={raw}' is not an archetype; "
            f"the archetypes are {known}"
        ) from exc
    if archetype not in present_archetypes:
        offered = [member.value for member in present_archetypes]
        raise ViewParameterError(
            f"the plan world holds no '{archetype.value}' investment to shock; "
            f"the archetypes it holds are {offered}"
        )
    return MarketShock(
        archetype=archetype,
        magnitude=_shock_decimal(fields, "magnitude", _MARKET_SHOCK_PREFIX),
    )


def _parse_fx_shock_entry(
    fields: Mapping[str, str | None],
    *,
    held_currencies: Sequence[str],
) -> FxShock:
    """Build one :class:`FxShock` from the fx builder's fields.

    Mirrors :func:`_parse_market_shock_entry`: the Scope is a ``<select>`` over
    the non-functional currencies the plan world holds, so a currency outside
    that set is refused here — a shock on a currency the book holds nothing in
    is vacuous by design (:func:`services.fx.plan_shock.shock_plan_fx_path`), and
    the offered-set gate is what turns that silent no-op into the loud notice
    ADR-0104 §4 requires.

    A currency the book *holds* but the dataset never priced is a **different**
    failure and a later one: the projection raises
    :class:`~core.exceptions.MissingFxRateError` naming the pair (ADR-0104 §3),
    never a 1:1 fallback. So this gate is the offered-set check, not the rate
    check — the two are distinct and both loud.

    Raises:
        ViewParameterError: On an absent or unreadable field, or a ``currency``
            the plan world holds nothing in.
    """
    currency = _shock_text(fields, "currency", _FX_SHOCK_PREFIX).upper()
    held = {code.upper() for code in held_currencies}
    if currency not in held:
        raise ViewParameterError(
            f"the plan world holds no position in '{currency}' to shock; "
            f"the currencies it holds are {sorted(held)}"
        )
    return FxShock(
        currency=currency,
        magnitude=_shock_decimal(fields, "magnitude", _FX_SHOCK_PREFIX),
    )


# ---------------------------------------------------------------------------
# Link building — every control re-issues the whole parameter set
# ---------------------------------------------------------------------------


def _state_pairs(
    *,
    periodisation: Periodisation,
    horizon: int,
    currency_view: CurrencyView,
    view: WorldView,
    overlay: Overlay,
) -> list[tuple[str, str]]:
    """The page state's ordered ``(key, value)`` pairs — the marker excluded.

    The body :func:`_query` encodes into a URL, and the body the pin dialog
    embeds as hidden ``<input>``s so its POST round-trips exactly what the page
    round-trips (ADR-0107 C5). Factored so the link a control carries and the
    form the dialog submits describe one state — and so the marker, which is
    navigation context, is added *only* by :func:`_query`, never by the state.
    """
    return [
        ("periodisation", periodisation.value),
        ("horizon", str(horizon)),
        ("currency_view", currency_view.value),
        ("view", view.value),
        *serialise_overlay(overlay),
    ]


def _query(
    *,
    periodisation: Periodisation,
    horizon: int,
    currency_view: CurrencyView,
    view: WorldView,
    overlay: Overlay,
    case_marker: str | None = None,
) -> str:
    """Render one complete page state as a query string.

    The four view toggles first, in a fixed order, then the overlay in the
    encoding's own order (:func:`~services.overlay.serialisation.serialise_overlay`).
    Deterministic by construction: the same state always renders the same
    string, so a copied link and a link produced by a click are the same link.

    ``case_marker`` — the navigation marker a case sets when it sends the PM here
    "capturing for" it (ADR-0107 C5) — is appended **after** the overlay, as its
    own ``case=`` pair, when present. It rides every page-emitted link so the
    capturing context survives overlay edits, removals, resets and view toggles,
    but it is **not** part of the parameter set: it goes on beside
    ``serialise_overlay(overlay)``, never through it, so the overlay serialisation
    never carries it (binding decision 4). A snapshot's canonical query is built
    with ``case_marker=None`` for exactly this reason — the marker is navigation
    context, not scenario state.
    """
    pairs = _state_pairs(
        periodisation=periodisation,
        horizon=horizon,
        currency_view=currency_view,
        view=view,
        overlay=overlay,
    )
    if case_marker is not None:
        pairs.append((_CASE_MARKER_KEY, case_marker))
    return urlencode(pairs)


def _pushed(
    *,
    periodisation: Periodisation,
    horizon: int,
    currency_view: CurrencyView,
    view: WorldView,
    overlay: Overlay,
    case_marker: str | None = None,
) -> str:
    """Render the canonical area URL of one page state, for ``HX-Push-Url``.

    What a transient control (a slider's factor, an entry form's seven fields)
    leaves behind in the address bar: the **encoded** set, never the intent that
    produced it. A reload of this URL, and a "Copy scenario link" from it, both
    reproduce exactly the scenario on screen (ADR-0104 §4). The ``case_marker``
    rides along so a reload keeps the capturing context (ADR-0107 C5).
    """
    return f"{AREA_URL}?" + _query(
        periodisation=periodisation,
        horizon=horizon,
        currency_view=currency_view,
        view=view,
        overlay=overlay,
        case_marker=case_marker,
    )


def _without(overlay: Overlay, index: int) -> Overlay:
    """Return the overlay with the transformation at ``index`` dropped.

    Re-indexing is not done here: it falls out of
    :func:`~services.overlay.serialisation.serialise_overlay`, which numbers
    the ``t{n}_`` namespaces by list position. Dropping the middle of three
    transformations therefore yields a contiguous ``t0_``/``t1_`` set without
    this module ever formatting a key.

    It backs **both** removal affordances of an inserted transaction — the
    strip's chip and the row table's ✕ — which are therefore not merely
    consistent but the same link (S2.6 §1.2).
    """
    return tuple(
        transformation for position, transformation in enumerate(overlay) if position != index
    )


# ---------------------------------------------------------------------------
# Chips — the parameter set made visible
# ---------------------------------------------------------------------------


def _factor_text(factor: Decimal) -> str:
    """Render a re-pacing factor for display.

    Two decimals — ``1.50``, the form the slider's own steps take — *unless*
    the factor carries more precision than that, in which case it is stated in
    full. A hand-built ``×1.234`` is a legal parameter set (the contract bounds
    the factor, it does not quantise it), and rounding it to ``×1.23`` on the
    chip would mean the strip states a scenario the engine is not computing.
    """
    two_places = factor.quantize(Decimal("0.01"))
    return f"{two_places if two_places == factor else factor}"


def _number_text(value: Decimal) -> str:
    """Render one parameter-set number for display.

    Thousands-separated, and stated to exactly the precision the parameter set
    carries: the trailing zeros of a *fraction* are dropped (``104.20`` →
    ``104.2``), those of an integer are not (``12000`` → ``12,000``). The
    surface shows what the URL says, neither rounded nor padded.
    """
    text = f"{value:,f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _magnitude_text(magnitude: Decimal) -> str:
    """Render a shock magnitude for display — signed, in per cent.

    A true minus sign, not a hyphen: the typography the rest of the surface
    uses for a negative figure (:func:`services.investments.pacing_rows.describe_shift`).
    """
    sign = "−" if magnitude < 0 else "+"
    return f"{sign}{_number_text(abs(magnitude))} %"


#: Operator-facing display name per presentation archetype (ADR-0082 §1, the
#: planning-desk mockup ⑤). This is the **one** archetype naming this surface
#: holds: a ``market_shock`` chip (:func:`_chip_label`) and the market builder's
#: Scope dropdown (:func:`_build_shock_context`) both read it, so a shock reads
#: the same in the card that states it and the chip that carries it. A second
#: map would be the one that later disagrees — the "one display taxonomy" rule
#: ADR-0104 §4 makes of the parameter set. The verbatim enum value is the honest
#: fallback for a member with no entry, which the closed enum keeps unreachable.
_ARCHETYPE_DISPLAY: dict[Archetype, str] = {
    Archetype.CAPITAL_ACCOUNT: "Private markets",
    Archetype.TOTAL_RETURN_EQUITY: "Listed equities",
    Archetype.FIXED_INCOME: "Listed bonds",
    Archetype.NAV_ONLY: "Other & cash",
}


def _archetype_display(archetype: Archetype) -> str:
    """Name one archetype for the operator (ADR-0082 §1, the mockup ⑤)."""
    return _ARCHETYPE_DISPLAY.get(archetype, archetype.value)


def _chip_label(transformation: Transformation, names: dict[UUID, str]) -> str:
    """Label one transformation for its chip.

    Total over the four ADR-0104 §2 kinds, and it has to be: since S34.1 a
    ``market_shock`` executes and an ``fx_shock`` at least parses, so either can
    arrive in the parameter set by URL. An in-force transformation with no chip
    would leave the strip stating a scenario the engine is not computing — the
    inverse of the failure ADR-0104 §4 guards against, and just as misleading.

    The two shock kinds are **not** investment-scoped (a ``market_shock`` names
    an archetype, an ``fx_shock`` a currency), which is why the name lookup
    lives inside the investment-scoped branches rather than above them.

    A ``market_shock`` names its archetype through the shared display taxonomy
    (:data:`_ARCHETYPE_DISPLAY`) — the same map the builder's Scope dropdown
    reads (:func:`_build_shock_context`), so the chip and the card that produced
    it never disagree (ADR-0104 §4, one display taxonomy). An ``fx_shock`` names
    its currency by its code, which is its own display name.

    Args:
        transformation: The transformation to label.
        names: Investment id → name, for the ones the book still holds. A
            transformation naming an investment that is gone (a scenario link
            shared after the position was archived) keeps its id, shortened:
            the chip must still identify *something*, and the projection will
            fail with :class:`~services.overlay.errors.UnknownInvestmentError`
            beside it saying why.
    """
    if isinstance(transformation, InsertTransaction | RepaceFlows):
        name = names.get(
            transformation.investment_id,
            f"investment {str(transformation.investment_id)[:8]}…",
        )
        if isinstance(transformation, InsertTransaction):
            return (
                f"Hyp. Txn: {transformation.txn_type} "
                f"{_number_text(transformation.units)} u — {name} "
                f"({transformation.trade_date.isoformat()})"
            )
        return f"Pacing ×{_factor_text(transformation.factor)} — {name}"
    if isinstance(transformation, MarketShock):
        return (
            f"Market shock: {_archetype_display(transformation.archetype)} "
            f"{_magnitude_text(transformation.magnitude)}"
        )
    if isinstance(transformation, FxShock):
        return f"FX shock: {transformation.currency} {_magnitude_text(transformation.magnitude)}"
    # Unreachable — the contract is closed over four kinds. A fifth (an
    # ADR-level decision, never a code one) must add its label here rather than
    # inherit a silently wrong one.
    raise ViewParameterError(f"no chip label for transformation kind '{transformation.kind.value}'")


#: Chip modifier class per kind — the mockup's chip--txn / chip--pacing idiom.
#: Both shock kinds share the mockup's ``chip--shock`` colour: S34.4 is the
#: surface that offers them, so the colour decision the earlier note deferred
#: "to the surface that offers them" is made here — one modifier for the pair,
#: exactly as the mockup carries one ``chip--shock`` for both.
_CHIP_CLASS: dict[str, str] = {
    "insert_transaction": "pd-chip--txn",
    "repace_flows": "pd-chip--pacing",
    "market_shock": "pd-chip--shock",
    "fx_shock": "pd-chip--shock",
}


def _build_chips(
    *,
    overlay: Overlay,
    names: dict[UUID, str],
    periodisation: Periodisation,
    horizon: int,
    currency_view: CurrencyView,
    view: WorldView,
    case_marker: str | None = None,
) -> list[dict[str, Any]]:
    """Project the overlay into the strip's removable chips.

    Each chip's ``remove_query`` carries the ``case_marker`` like every other
    page-emitted link, so removing a transformation keeps the capturing context
    (ADR-0107 C5). A snapshot freezes only ``label`` and ``css_class`` — the
    ``remove_query`` is a live Desk link and never enters a frozen record.
    """
    return [
        {
            "label": _chip_label(transformation, names),
            "css_class": _CHIP_CLASS[transformation.kind.value],
            "remove_query": _query(
                periodisation=periodisation,
                horizon=horizon,
                currency_view=currency_view,
                view=view,
                overlay=_without(overlay, index),
                case_marker=case_marker,
            ),
        }
        for index, transformation in enumerate(overlay)
    ]


# ---------------------------------------------------------------------------
# Pacing rows — the slider's control surface (mockup ③)
# ---------------------------------------------------------------------------


def _build_pacing_rows(
    *,
    rows: tuple[PacingRow, ...],
    frames: PlanFrames,
    overlay: Overlay,
    periodisation: Periodisation,
    horizon: int,
    currency_view: CurrencyView,
    view: WorldView,
    case_marker: str | None = None,
) -> list[dict[str, Any]]:
    """Project the pacing rows against the parameter set in force.

    The row says what the *book* holds (:class:`~services.investments.pacing_rows.PacingRow`);
    the parameter set says where the slider stands. This function is the join,
    and it resolves the three states a row can be in:

    * **At plan** — no entry for this fund. The slider sits at the
      mid-position, the readout is muted, and Reset has nothing to do.
    * **Re-paced** — exactly one entry. The slider renders at its factor and
      the readout states the shift the executor will perform.
    * **Off-slider** — *more than one* entry for the same fund. The encoding
      permits it (the executors compose in order) and a hand-built URL can
      carry it, but no slider position denotes it. Rather than pick one of the
      factors and quietly misstate the scenario, the row renders at the
      mid-position **disabled**, with a note; the chips stay in the strip
      stating each factor exactly, and the row's Reset clears all of them.
      Honest display beats clever composition.

    A fund with no remaining profile is disabled in every one of the three —
    but its Reset stays live if a stale set re-paces it anyway, because a chip
    the operator can see must be a chip the operator can remove.

    Args:
        rows: The book's capital-account rows, in name order.
        frames: The **baseline** frames — the seam the shift is measured about.
        overlay: The parameter set in force.
        periodisation: The active periodisation (carried by every link).
        horizon: The active horizon (likewise).
        currency_view: The active currency view (likewise).
        view: The active world (likewise).

    Returns:
        One template context per row.
    """
    projected: list[dict[str, Any]] = []
    for row in rows:
        entries = [
            transformation
            for transformation in overlay
            if _is_repace_of(transformation, row.investment_id)
        ]
        off_slider = len(entries) > 1
        factor = entries[0].factor if len(entries) == 1 else FACTOR_NEUTRAL
        interactive = row.enabled and not off_slider

        base_query = _query(
            periodisation=periodisation,
            horizon=horizon,
            currency_view=currency_view,
            view=view,
            overlay=overlay,
            case_marker=case_marker,
        )
        reset_query = _query(
            periodisation=periodisation,
            horizon=horizon,
            currency_view=currency_view,
            view=view,
            overlay=_without_repace(overlay, row.investment_id),
            case_marker=case_marker,
        )

        projected.append(
            {
                "name": row.name,
                "meta": _pacing_meta(row),
                "enabled": interactive,
                "factor": str(factor),
                "factor_label": (
                    f"×{_factor_text(factor)}" if interactive and factor != FACTOR_NEUTRAL else ""
                ),
                "readout": _pacing_readout(row, frames, factor, off_slider),
                "is_muted": (not interactive or factor == FACTOR_NEUTRAL),
                "note": (
                    "off-slider parameter set: this fund carries "
                    f"{len(entries)} pacing parameters, which no single "
                    "slider position states. Reset clears them."
                    if off_slider
                    else ""
                ),
                # The slider re-issues the *whole* set and states its intent
                # beside it; the input contributes `pace_factor` itself, which
                # is why this query stops at the id (ADR-0104 §4 — the UI never
                # formats a t{n}_ key).
                "slider_query": f"{base_query}&pace_id={row.investment_id}",
                "reset_query": reset_query,
                "can_reset": bool(entries),
            }
        )
    return projected


#: The badge each plan source is shown as — the mockup's row meta, and the
#: whole of what tells the operator the two profiles apart.
#:
#: The **plan source is a code value; the badge is copy.** They are separated
#: here rather than reused as one string because ``'ta'`` is a discriminator the
#: seam and the row agree on, and "TA-generated profile" is a sentence a person
#: reads. ADR-0105 §4 makes the badge mandatory on every surface that shows the
#: row, and its §Consequences says why in the ADR's own blunt register: two
#: visually identical profiles with different epistemic status live on one
#: surface, and a manager's plan and a standard model's guess must never be
#: mistaken for one another.
_PLAN_SOURCE_BADGES: dict[str, str] = {
    PLAN_SOURCE_REPORTED: "reported",
    PLAN_SOURCE_TA: "TA-generated profile",
}


def _pacing_meta(row: PacingRow) -> str:
    """Render a pacing row's meta line (the mockup's ``pace__meta``).

    ``'reported · unfunded 22,000,000 EUR'`` for a fund with a remaining
    manager profile, ``'TA-generated profile · unfunded …'`` for one the
    assembly seam modelled (ADR-0105 §4); the unfunded clause is dropped where
    the book states no commitment, rather than showing a figure derived from one
    it does not have. A fund without a profile at all — no plan, and nothing the
    generator could model — carries
    :data:`~services.investments.pacing_rows.NO_PLAN_NOTE` instead, which names
    what is missing.
    """
    if not row.enabled:
        return NO_PLAN_NOTE
    meta = _PLAN_SOURCE_BADGES.get(row.plan_source or "", "")
    if row.unfunded is not None:
        meta += f" · unfunded {row.unfunded:,.0f} {row.currency}"
    return meta


def _pacing_readout(
    row: PacingRow,
    frames: PlanFrames,
    factor: Decimal,
    off_slider: bool,
) -> str:
    """Render a pacing row's readout (the mockup's ``pace__val``).

    An em dash where there is nothing to state — a fund with no remaining
    profile, or an off-slider set whose composed effect no single readout
    denotes. Otherwise
    :func:`~services.investments.pacing_rows.describe_shift`, which measures
    the shift with the executor's own date rule.

    The ``profile_end is None`` test is the same condition as ``not enabled``
    (:attr:`~services.investments.pacing_rows.PacingRow.enabled` is defined as
    it); it is spelled out so the narrowing is the type-checker's, not a
    comment's.
    """
    if off_slider or row.profile_end is None:
        return "—"
    return describe_shift(t0=frames.t0, profile_end=row.profile_end, factor=factor)


# ---------------------------------------------------------------------------
# Hypothetical transactions — the entry surface of `insert_transaction`
# (mockup ④)
# ---------------------------------------------------------------------------

#: Pill modifier per offered type — the mockup's ``pill--buy`` / ``pill--sell``.
#: A type the *form* cannot enter but a URL may carry (an ``opening``, a
#: ``transfer``) renders its type plain: the parameter set states it, and the
#: table's job is to say what the set states, not to decide what it should have
#: said.
_PILL_CLASS: dict[str, str] = {
    "buy": "pd-pill--buy",
    "sell": "pd-pill--sell",
}


def _cash_effect_text(effect: Decimal, currency: str) -> str:
    """State one transaction's effect on the cash path it settles against.

    The **signed −C** of the executor: value in, cash out (a buy), or the
    reverse (a sale). Signed explicitly in both directions, because a cash
    effect whose sign the reader has to infer from the transaction type is the
    one number on this surface that must never be guessed at.
    """
    sign = "−" if effect < 0 else "+"
    return f"{sign}{_number_text(abs(effect))} {currency} cash"


def _build_txn_rows(
    *,
    overlay: Overlay,
    frames: PlanFrames,
    names: dict[UUID, str],
    periodisation: Periodisation,
    horizon: int,
    currency_view: CurrencyView,
    view: WorldView,
    case_marker: str | None = None,
) -> list[dict[str, Any]]:
    """Project the set's inserted transactions into the row table (mockup ④).

    The table is rendered **from the parameter set**, not from the frames: a
    hypothetical transaction is a parameter, and the frames carry only its
    *effect* (ADR-0104 §2 — the executor appends no ``PlanFlow`` for it). Its
    row and its chip are therefore two views of one thing, and the ✕ in the row
    is the very link the chip's ✕ carries.

    The cash effect is derived by the executor's own
    :func:`~services.overlay.derive_consideration` — stated consideration wins,
    else ``units × price_per_unit`` — so the cell states the
    amount the projection actually settled. Where it cannot be derived at all,
    the cell is an em dash rather than a zero: after a successful projection
    that case is unreachable (the executor would have refused the set), but the
    table also renders beside an error notice, and a table that invented a
    ``0`` there would state a trade with no effect where the truth is *not
    stated*.

    The **AUM effect is the inert badge**, never a computed check: value and
    cash move by the same amount, in the same currency, on the same date, so
    AUM is invariant *by construction* (ADR-0104 §2). Re-proving it per row in a
    template would turn a property of the engine into a claim of the display.

    Args:
        overlay: The parameter set in force.
        frames: The baseline frames — the archetype and the currency badge come
            from the investment as the *plan world* holds it.
        names: Investment id → name, for the ones the book still holds.
        periodisation: The active periodisation (carried by every remove link).
        horizon: The active horizon (likewise).
        currency_view: The active currency view (likewise).
        view: The active world (likewise).

    Returns:
        One template context per inserted transaction, in overlay order.
    """
    rows: list[dict[str, Any]] = []
    for index, transformation in enumerate(overlay):
        if not isinstance(transformation, InsertTransaction):
            continue

        investment = frames.investments.get(transformation.investment_id)
        consideration: Decimal | None = None
        if investment is not None:
            try:
                consideration = derive_consideration(
                    transformation,
                    resolve_archetype(investment.investment_type),
                )
            except OverlayError:
                consideration = None

        rows.append(
            {
                "txn_type": transformation.txn_type,
                "pill_class": _PILL_CLASS.get(transformation.txn_type, ""),
                "name": names.get(
                    transformation.investment_id,
                    f"investment {str(transformation.investment_id)[:8]}…",
                ),
                "currency": transformation.currency,
                "trade_date": transformation.trade_date.isoformat(),
                "units": _number_text(transformation.units),
                "price": (
                    _number_text(transformation.price_per_unit)
                    if transformation.price_per_unit is not None
                    else "—"
                ),
                "consideration": (
                    _number_text(consideration) if consideration is not None else "—"
                ),
                "cash_effect": (
                    _cash_effect_text(-consideration, transformation.currency)
                    if consideration is not None
                    else "—"
                ),
                "is_cash_out": consideration is not None and consideration > 0,
                "remove_query": _query(
                    periodisation=periodisation,
                    horizon=horizon,
                    currency_view=currency_view,
                    view=view,
                    overlay=_without(overlay, index),
                    case_marker=case_marker,
                ),
            }
        )
    return rows


def _build_hyp_context(
    *,
    overlay: Overlay,
    frames: PlanFrames,
    names: dict[UUID, str],
    periodisation: Periodisation,
    horizon: int,
    currency_view: CurrencyView,
    view: WorldView,
    case_marker: str | None = None,
    values: Mapping[str, str | None] | None = None,
    is_open: bool = False,
) -> dict[str, Any]:
    """Build the Jinja context for the hypothetical-transaction block.

    The **universe the form offers is the plan world's**, not the book's: an
    ``insert_transaction`` lands on ``frames.investments``, which holds the
    active *non-cash* investments (cash positions live in ``cash_paths`` and are
    what a trade settles *against*, ADR-0104 §1). Offering a cash row would
    offer an entry the executor refuses by definition.

    The currency is **not** a choice: it is the selected investment's, rendered
    read-only and re-bound to the selection, exactly as the actual-entry form
    pins it to the investment (ADR-0097 §5). That is what makes
    :class:`~services.overlay.errors.CurrencyMismatchError` unreachable through
    this surface — a hand-built request still meets it, typed, at the executor.

    The date floor is the **seam, plus a day**: ``t0`` itself is realised
    history and refused there
    (:class:`~services.overlay.errors.HistoricTradeDateError`). It is read off
    the frames rather than from a clock — a scenario is reproducible from
    *(book, parameters)*, and "today" is neither.

    Args:
        overlay: The parameter set the rows and the form's base query state.
        frames: The baseline frames — the universe, the currencies, the seam.
        names: Investment id → name.
        periodisation: The active periodisation (carried by every link).
        horizon: The active horizon (likewise).
        currency_view: The active currency view (likewise).
        view: The active world (likewise).
        values: The raw fields of a **refused** submission, re-seated in the
            form so the operator's entry survives the notice that rejected it.
            ``None`` on an ordinary render, which yields an empty form.
        is_open: Whether the disclosure renders open — true exactly when a
            submission was refused and the form is the thing to look at.

    Returns:
        The ``hyp`` sub-context, ready to merge into a section or error context.
    """
    stated = values or {}
    options = sorted(
        (
            {
                "id": str(investment.investment_id),
                "name": names.get(
                    investment.investment_id,
                    f"investment {str(investment.investment_id)[:8]}…",
                ),
                "currency": investment.currency,
            }
            for investment in frames.investments.values()
        ),
        key=lambda option: option["name"],
    )

    field_values = {field: (stated.get(field) or "") for field in _HYP_FIELDS}
    # The read-only currency is bound to the selection by the form's own script;
    # seating the first option's currency here means the field is right for the
    # investment the select opens on, before a single event has fired — and a
    # refused submission keeps the currency it was refused for.
    field_values["currency"] = field_values["currency"] or (
        options[0]["currency"] if options else ""
    )

    return {
        "hyp": {
            "options": options,
            "txn_types": list(_OFFERED_TXN_TYPES),
            # The floor of the date input and the earliest date the executor
            # will accept — one statement, in one place.
            "earliest_trade_date": (frames.t0 + timedelta(days=1)).isoformat(),
            "seam_date": frames.t0.isoformat(),
            "rows": _build_txn_rows(
                overlay=overlay,
                frames=frames,
                names=names,
                periodisation=periodisation,
                horizon=horizon,
                currency_view=currency_view,
                view=view,
                case_marker=case_marker,
            ),
            # The form re-issues the *whole* set and appends its seven fields to
            # it, the way the slider appends its factor: the UI states an entry,
            # never a `t{n}_` key (ADR-0104 §4). The case marker rides along so a
            # refused submission comes back still capturing for its case.
            "base_query": _query(
                periodisation=periodisation,
                horizon=horizon,
                currency_view=currency_view,
                view=view,
                overlay=overlay,
                case_marker=case_marker,
            ),
            "is_open": is_open,
            # ``fields``, not ``values``: Jinja resolves ``hyp.values`` to the
            # dict's own ``values()`` method before it ever looks for a key of
            # that name, and the form would silently render empty.
            "fields": field_values,
            "prefix": _HYP_PREFIX,
        }
    }


# ---------------------------------------------------------------------------
# The shock builders — the entry surface of `market_shock` / `fx_shock`
# (mockup ⑤)
# ---------------------------------------------------------------------------


def _build_shock_context(
    *,
    frames: PlanFrames,
    functional_currency: str,
    overlay: Overlay,
    periodisation: Periodisation,
    horizon: int,
    currency_view: CurrencyView,
    view: WorldView,
    case_marker: str | None = None,
) -> dict[str, Any]:
    """Build the Jinja context for the two shock builders (ADR-0104 §2/§6).

    Mirrors :func:`_build_hyp_context`: the **universe the builders offer is the
    plan world's**, not a fixed menu. The market card's Scope is the archetypes
    the book resolves to (:func:`_present_archetypes`); the fx card's Scope is
    the non-functional currencies it holds (:func:`_held_currencies`). A book
    with only functional-currency positions offers the fx card nothing, and the
    card says so rather than offering a no-op.

    **Operator and Timing are the fixed v1 options** (ADR-0104 §2, E6): a
    ``market_shock`` shifts the price / NAV level immediately at t₀; an
    ``fx_shock`` shifts the rate against the functional currency, immediately.
    Each renders as its only option, disabled — a fixed choice, not a free one.
    Richer regimes (paths, lags, decay) are ADR-E territory (#034) and are
    deliberately absent from the contract, so there is nothing here to offer.

    Both cards **append**, exactly as the hypothetical-transaction form does:
    each re-issues the whole parameter set (``base_query``) and states its scope
    and magnitude beside it; the server merges and re-emits the ``t{n}_``
    encoding (:func:`_parse_market_shock_entry`, :func:`_parse_fx_shock_entry`),
    so the UI never formats an overlay key.

    Args:
        frames: The baseline frames — the archetypes and currencies the cards
            offer, and the offered sets the parsers gate against.
        functional_currency: The numéraire, excluded from the fx Scope.
        overlay: The parameter set the cards re-issue and append to.
        periodisation: The active periodisation (carried by every link).
        horizon: The active horizon (likewise).
        currency_view: The active currency view (likewise).
        view: The active world (likewise).

    Returns:
        The ``shock`` sub-context, ready to merge into the section context.
    """
    base_query = _query(
        periodisation=periodisation,
        horizon=horizon,
        currency_view=currency_view,
        view=view,
        overlay=overlay,
        case_marker=case_marker,
    )
    market_options = [
        {"value": archetype.value, "label": _archetype_display(archetype)}
        for archetype in _present_archetypes(frames)
    ]
    fx_options = [
        {"value": currency, "label": currency}
        for currency in _held_currencies(frames, functional_currency)
    ]
    return {
        "shock": {
            "market": {
                "title": "Market shock",
                "scope_label": "Archetype",
                "scope_field": f"{_MARKET_SHOCK_PREFIX}archetype",
                "scope_options": market_options,
                "scope_empty": (
                    "This book holds no investment to shock — a market shock "
                    "marks a class of holdings, and there is none here yet."
                ),
                "operator_label": "Price / NAV level shift",
                "magnitude_field": f"{_MARKET_SHOCK_PREFIX}magnitude",
                "timing_label": "Immediate (t₀)",
                "base_query": base_query,
            },
            "fx": {
                "title": "FX shock",
                "scope_label": "Currency",
                "scope_field": f"{_FX_SHOCK_PREFIX}currency",
                "scope_options": fx_options,
                "scope_empty": (
                    "Every position settles in the functional currency "
                    f"({functional_currency}) — an FX shock on the numéraire is "
                    "the identity, so there is nothing here to shock."
                ),
                "operator_label": "FX rate shift vs functional",
                "magnitude_field": f"{_FX_SHOCK_PREFIX}magnitude",
                "timing_label": "Immediate (t₀)",
                "base_query": base_query,
            },
        }
    }


# ---------------------------------------------------------------------------
# Table projection — the same DTO the chart reads
# ---------------------------------------------------------------------------


def _format_balance(value: Any) -> str:
    """Format one balance cell.

    An empty cell is an em dash, never a zero: a currency with no observation
    at or before the period end contributes *nothing*, and a rendered ``0``
    would state a drawn-down account (ADR-0104 §3, the empty-is-not-zero rule
    the DTO carries).
    """
    if value is None:
        return "—"
    return f"{float(value):,.0f}"


def _cells(balances: tuple[Any, ...], timeline: CashFlowTimeline) -> list[dict[str, Any]]:
    """Project one row's balances into table cells."""
    return [
        {
            "text": _format_balance(value),
            "is_actual": period.is_actual,
            "is_seam": index == timeline.seam_index,
        }
        for index, (value, period) in enumerate(zip(balances, timeline.periods, strict=True))
    ]


# ---------------------------------------------------------------------------
# Scenario Analysis result view models (S34.5, ADR-0104 §5/§7)
#
# Pure presentation: the ScenarioResult carries the figures, these turn them
# into the mockup's strings. No figure is recomputed here — every value routes
# from the DTO the assembly produced (ADR-0104 §5).
# ---------------------------------------------------------------------------


#: The mockup's minus glyph (U+2212), used on every signed readout so the badges
#: read as the mockup draws them rather than with a hyphen.
_MINUS: str = "−"


def _signed_number(value: float, *, decimals: int) -> str:
    """A signed magnitude — ``+``, the mockup's minus, or ``±`` for zero."""
    rounded = round(value, decimals)
    body = f"{abs(rounded):,.{decimals}f}"
    if rounded > 0:
        return f"+{body}"
    if rounded < 0:
        return f"{_MINUS}{body}"
    return f"±{body}"


def _money_m(value: Decimal | int | float | None, currency: str) -> str:
    """Format a functional-currency figure in millions (the mockup's €m)."""
    if value is None:
        return "—"
    return f"{float(value) / _MILLIONS:,.1f}m {currency}"


def _delta_money_m(value: Decimal | int | float | None, currency: str) -> str:
    """A signed functional-currency delta, in millions."""
    if value is None:
        return "—"
    return f"{_signed_number(float(value) / _MILLIONS, decimals=1)}m {currency}"


def _count_text(value: Decimal | int | float | None) -> str:
    """Format an integer count."""
    return "—" if value is None else f"{int(value):,}"


def _delta_count(value: Decimal | int | float | None) -> str:
    """A signed integer delta (the breach tile)."""
    return "—" if value is None else _signed_number(int(value), decimals=0)


def _pct_text(value: Decimal | float | None) -> str:
    """Format a utilisation figure as a percentage (one decimal)."""
    return "—" if value is None else f"{float(value):.1f}%"


def _delta_pp(value: float | None) -> str:
    """A signed percentage-point delta."""
    return "—" if value is None else f"{_signed_number(value, decimals=1)}pp"


def _return_text(index_value: Decimal | float | None) -> str:
    """State a rebased-to-100 index as a signed total return (index − 100)."""
    if index_value is None:
        return "—"
    return f"{_signed_number(float(index_value) - 100.0, decimals=1)}%"


def _delta_tone(value: Decimal | float | int | None, higher_is_better: bool) -> str:
    """Return the badge tone — ``pos`` / ``neg`` / ``zero`` — for a delta.

    Favourability, not raw sign: a rise is ``pos`` (green) only where a rise is
    the good move for that figure. Presentation only.
    """
    if value is None or float(value) == 0.0:
        return "zero"
    return "pos" if (float(value) > 0.0) == higher_is_better else "neg"


def _kpi_view(kpi: KpiDelta, currency: str) -> dict[str, Any]:
    """One KPI tile as the ADR-0067 pair (baseline / scenario / delta badge)."""
    higher = _KPI_HIGHER_IS_BETTER.get(kpi.key, True)
    if kpi.unit == "count":
        base, scen, delta = (
            _count_text(kpi.baseline),
            _count_text(kpi.scenario),
            _delta_count(kpi.delta),
        )
    else:  # 'functional_currency' — the three money tiles
        base, scen, delta = (
            _money_m(kpi.baseline, currency),
            _money_m(kpi.scenario, currency),
            _delta_money_m(kpi.delta, currency),
        )
    return {
        "label": kpi.label,
        "base": base,
        "scen": scen,
        "delta": delta,
        "tone": _delta_tone(kpi.delta, higher),
    }


def _class_label(family: str, class_key: str) -> str:
    """Prefix a limit class with its family — ``'AnlV — Listed equity'``."""
    family_label = _FAMILY_LABEL.get(family, family.upper())
    return f"{family_label} — {class_key.replace('_', ' ').capitalize()}"


def _headroom_row_view(row) -> dict[str, Any]:  # HeadroomClassDelta
    """One ``(family, class)`` row of the headroom deltatable.

    The bar states the **scenario** utilisation (the world being decided on),
    coloured by the engine's status; the Δ is the change in headroom, which is
    the negative of the change in utilisation — headroom shrinks as utilisation
    grows.
    """
    scen_cov = row.scenario_coverage_pct
    bar_pct = 0.0 if scen_cov is None else max(0.0, min(100.0, float(scen_cov)))
    headroom_delta_pp = None if row.delta_coverage_pct is None else -float(row.delta_coverage_pct)
    return {
        "label": _class_label(row.family, row.class_key),
        "baseline_util": _pct_text(row.baseline_coverage_pct),
        "scenario_util": _pct_text(row.scenario_coverage_pct),
        "bar_pct": f"{bar_pct:.0f}",
        "bar_tone": _STATUS_TONE.get(row.scenario_status or "", "muted"),
        "delta": _delta_pp(headroom_delta_pp),
        # More headroom is the favourable move.
        "delta_tone": _delta_tone(headroom_delta_pp, True),
    }


def _headroom_family_view(family: FamilyHeadroomDelta) -> dict[str, Any]:
    """One limit family's rows, labelled."""
    return {
        "family": _FAMILY_LABEL.get(family.family, family.family.upper()),
        "rows": [_headroom_row_view(row) for row in family.rows],
    }


def _build_scenario_context(
    *,
    result: ScenarioResult | None,
    functional_currency: str,
    labels: list[str],
    composition_query: str,
    capturing: dict[str, Any] | None = None,
    pin_open_query: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Build the ``scenario`` sub-context for the result region (ADR-0104 §5).

    A ``None`` result (with ``error`` set) yields a notice-only context: the
    scenario could not be scored, and the region says why rather than the lens
    failing. Otherwise it carries the two impact panels, the panel footers, the
    KPI pairs, the headroom families and the composition lazy-load address.

    Two ADR-0107 C5 additions ride the same sub-context:

    * ``capturing`` — the "Capturing for CASE-NNNN" chip when a valid, open case
      marker is in force, or ``None``. It is navigation context, so it shows in
      **both** the notice and the scored branch (the marker survives even when
      the scenario cannot be scored).
    * ``pin`` — the "Pin to case…" affordance's dialog-open query, present only in
      the scored branch: the pin freezes a *result*, so the affordance appears
      exactly when there is one to freeze (mirroring the region's own condition).
    """
    if result is None:
        return {"scenario": {"error": error, "capturing": capturing}}

    baseline_spec, scenario_spec = build_scenario_impact_pair(
        result, functional_currency=functional_currency, labels=labels
    )
    nav = result.nav_path
    ret = result.return_index
    return {
        "scenario": {
            "error": None,
            "baseline_spec": baseline_spec,
            "scenario_spec": scenario_spec,
            "baseline_foot": {
                "nav": _money_m(nav.baseline_end, functional_currency),
                "ret": _return_text(ret.baseline_end),
            },
            "scenario_foot": {
                "nav": _money_m(nav.scenario_end, functional_currency),
                "nav_delta": _delta_money_m(nav.delta_end, functional_currency),
                "nav_tone": _delta_tone(nav.delta_end, True),
                "ret": _return_text(ret.scenario_end),
                "ret_delta": _delta_pp(None if ret.delta_end is None else float(ret.delta_end)),
                "ret_tone": _delta_tone(ret.delta_end, True),
            },
            "kpis": [_kpi_view(kpi, functional_currency) for kpi in result.kpis],
            "headroom": [_headroom_family_view(family) for family in result.headroom],
            "has_headroom": any(family.rows for family in result.headroom),
            "composition_url": COMPOSITION_URL,
            "composition_query": composition_query,
            "capturing": capturing,
            "pin": {"open_query": pin_open_query},
        }
    }


def _build_composition_context(pair: CompositionPair) -> dict[str, Any]:
    """Build the composition drill-down view model (ADR-0104 §5, §7).

    Diffs the two NAV-weighted breakdowns fund by fund — a scenario moves the
    NAV weights, so the weight delta is the signal. Funds are keyed by id (the
    synthetic ``"Other"`` aggregate by name), ordered by the scenario weight the
    breakdown already sorts descending, with baseline-only funds appended.
    """
    baseline_by = {(r.investment_id or r.name): r for r in pair.baseline.rows}
    scenario_by = {(r.investment_id or r.name): r for r in pair.scenario.rows}
    ordered_keys = list(scenario_by) + [key for key in baseline_by if key not in scenario_by]

    rows: list[dict[str, Any]] = []
    for key in ordered_keys:
        base = baseline_by.get(key)
        scen = scenario_by.get(key)
        anchor = scen if scen is not None else base
        base_pct = None if base is None else base.weight_pct
        scen_pct = None if scen is None else scen.weight_pct
        delta = (scen_pct or 0.0) - (base_pct or 0.0)
        rows.append(
            {
                "name": anchor.name if anchor is not None else "—",
                "baseline": "—" if base_pct is None else f"{base_pct:.1f}%",
                "scenario": "—" if scen_pct is None else f"{scen_pct:.1f}%",
                "delta": _delta_pp(delta),
            }
        )
    return {"composition": {"rows": rows}}


def _build_section_context(
    *,
    result: CashFlowPlanningResult,
    frames: PlanFrames,
    pacing: tuple[PacingRow, ...],
    periodisation: Periodisation,
    horizon: int,
    currency_view: CurrencyView,
    view: WorldView,
    overlay: Overlay,
    names: dict[UUID, str],
    scenario_result: ScenarioResult | None = None,
    scenario_error: str | None = None,
    case_marker: str | None = None,
    capturing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the Jinja context for the section body and the parameter strip.

    Both are built from one :class:`CashFlowPlanningResult` and one parameter
    set, so the chart, the table, the pacing rows and the chips state the same
    world. The Scenario Analysis result region travels along as the ``scenario``
    sub-context — the deltas-first impact of the same overlay, assembled once by
    the caller (S34.5) — and is swapped out of band into the sibling section, the
    way the parameter strip and the shock builders are.
    """
    timeline = result.baseline if view is WorldView.BASELINE else result.scenario
    # The impact chart's x-axis is the *baseline* grid's labels — one grid spans
    # both worlds (ADR-0104 §5), so the pair aligns column-for-column with the
    # cash-flow timeline above it regardless of the active view.
    scenario_labels = [period.label for period in result.baseline.periods]

    header = [
        {
            "label": period.label,
            "is_actual": period.is_actual,
            "is_seam": index == timeline.seam_index,
        }
        for index, period in enumerate(timeline.periods)
    ]
    # The currency view selects what is *shown*, never what is computed: both
    # worlds are assembled in full either way (ADR-0104 §5), so the toggle
    # cannot change a number — only whether the position-currency rows are on
    # screen beside the converted total.
    rows = (
        [
            {
                "currency": row.currency,
                "is_functional": row.currency == timeline.functional_currency,
                "cells": _cells(row.balances, timeline),
            }
            for row in timeline.currency_rows
        ]
        if currency_view is CurrencyView.PER_CURRENCY
        else []
    )

    return {
        **_build_strip_context(
            overlay=overlay,
            names=names,
            periodisation=periodisation,
            horizon=horizon,
            currency_view=currency_view,
            view=view,
            case_marker=case_marker,
        ),
        **_build_hyp_context(
            overlay=overlay,
            frames=frames,
            names=names,
            periodisation=periodisation,
            horizon=horizon,
            currency_view=currency_view,
            view=view,
            case_marker=case_marker,
        ),
        **_build_shock_context(
            frames=frames,
            functional_currency=timeline.functional_currency,
            overlay=overlay,
            periodisation=periodisation,
            horizon=horizon,
            currency_view=currency_view,
            view=view,
            case_marker=case_marker,
        ),
        **_build_scenario_context(
            result=scenario_result,
            functional_currency=timeline.functional_currency,
            labels=scenario_labels,
            composition_query=_query(
                periodisation=periodisation,
                horizon=horizon,
                currency_view=currency_view,
                view=view,
                overlay=overlay,
                case_marker=case_marker,
            ),
            capturing=capturing,
            # The dialog-open query carries the marker (so the picker preselects
            # the capturing case) *and* the whole page state (which the dialog
            # freezes into hidden inputs for the POST). ADR-0107 C5.
            pin_open_query=_query(
                periodisation=periodisation,
                horizon=horizon,
                currency_view=currency_view,
                view=view,
                overlay=overlay,
                case_marker=case_marker,
            ),
            error=scenario_error,
        ),
        # The pin endpoint the "Pin to case…" affordance opens against (C5).
        "pin_scenario_url": PIN_SCENARIO_URL,
        "header": header,
        "rows": rows,
        "total_cells": _cells(timeline.total, timeline),
        "functional_currency": timeline.functional_currency,
        "seam_date": timeline.seam_date.isoformat(),
        "periodisation": periodisation.value,
        "horizon": horizon,
        "currency_view": currency_view.value,
        "chart_spec": build_cash_flow_timeline_spec(result, currency_view=currency_view, view=view),
        # Every control link carries the *whole* state with exactly one of its
        # members replaced — the parameter set survives every interaction
        # (ADR-0104 §4). The repetition is the point: there is no partial link.
        "periodisation_links": {
            member.value: _query(
                periodisation=member,
                horizon=horizon,
                currency_view=currency_view,
                view=view,
                overlay=overlay,
                case_marker=case_marker,
            )
            for member in Periodisation
        },
        "horizon_links": {
            quarters: _query(
                periodisation=periodisation,
                horizon=quarters,
                currency_view=currency_view,
                view=view,
                overlay=overlay,
                case_marker=case_marker,
            )
            for quarters in sorted(HORIZON_QUARTERS)
        },
        "currency_view_links": {
            member.value: _query(
                periodisation=periodisation,
                horizon=horizon,
                currency_view=member,
                view=view,
                overlay=overlay,
                case_marker=case_marker,
            )
            for member in CurrencyView
        },
        "pacing_rows": _build_pacing_rows(
            rows=pacing,
            frames=frames,
            overlay=overlay,
            periodisation=periodisation,
            horizon=horizon,
            currency_view=currency_view,
            view=view,
            case_marker=case_marker,
        ),
        # The slider's geometry, from the contract and nowhere else: a template
        # restating 0.5 / 2.0 / 1.0 would be a second definition of the
        # ADR-0104 §2 bounds, free to drift from the one the executor enforces.
        "factor_min": str(FACTOR_MIN),
        "factor_max": str(FACTOR_MAX),
        "factor_neutral": str(FACTOR_NEUTRAL),
        "factor_step": str(FACTOR_STEP),
    }


def _build_strip_context(
    *,
    overlay: Overlay,
    names: dict[UUID, str],
    periodisation: Periodisation,
    horizon: int,
    currency_view: CurrencyView,
    view: WorldView,
    case_marker: str | None = None,
) -> dict[str, Any]:
    """Build the Jinja context for the sticky parameter strip alone.

    Separate from the section context because the error partials need it too:
    an FX rate that is missing, or a transformation the book refuses, does not
    make the parameter set unreadable — the chips stay on screen so the
    operator can remove the offending one without hand-editing a URL.

    Every link here carries the ``case_marker`` (chips' remove links, the view
    toggles, reset-all, and the copy-scenario link) so the capturing context
    survives an overlay edit made from the strip in *any* render state — the
    error partials included (ADR-0107 C5, binding decision 4).
    """
    scenario_query = _query(
        periodisation=periodisation,
        horizon=horizon,
        currency_view=currency_view,
        view=view,
        overlay=overlay,
        case_marker=case_marker,
    )
    return {
        "chips": _build_chips(
            overlay=overlay,
            names=names,
            periodisation=periodisation,
            horizon=horizon,
            currency_view=currency_view,
            view=view,
            case_marker=case_marker,
        ),
        "view": view.value,
        "is_baseline": view is WorldView.BASELINE,
        "view_links": {
            member.value: _query(
                periodisation=periodisation,
                horizon=horizon,
                currency_view=currency_view,
                view=member,
                overlay=overlay,
                case_marker=case_marker,
            )
            for member in WorldView
        },
        "reset_query": _query(
            periodisation=periodisation,
            horizon=horizon,
            currency_view=currency_view,
            view=view,
            overlay=(),
            case_marker=case_marker,
        ),
        "scenario_link": f"{AREA_URL}?{scenario_query}",
        "section_url": SECTION_URL,
        "area_url": AREA_URL,
    }


def _empty_strip_context() -> dict[str, Any]:
    """Build the strip context for a request whose parameters did not parse.

    The strip cannot show chips for a set it could not read, and it must not
    invent an empty one — an unreadable link is not a baseline. It renders its
    unreadable state instead, offering the one action that is certainly safe:
    dropping back to the plan world.
    """
    return {
        "chips": [],
        "unreadable": True,
        "view": WorldView.SCENARIO.value,
        "is_baseline": False,
        "view_links": {},
        "reset_query": _query(
            periodisation=Periodisation.QUARTERLY,
            horizon=DEFAULT_HORIZON_QUARTERS,
            currency_view=CurrencyView.PER_CURRENCY,
            view=WorldView.SCENARIO,
            overlay=(),
        ),
        "scenario_link": AREA_URL,
        "section_url": SECTION_URL,
        "area_url": AREA_URL,
    }


# ---------------------------------------------------------------------------
# The `case=` capture marker — navigation context, never scenario state
# (ADR-0107 C5, binding decision 4)
# ---------------------------------------------------------------------------


def _parse_case_marker(raw: str | None) -> UUID | None:
    """Parse the ``case=`` marker to a UUID, or ``None`` for anything unusable.

    A malformed or absent marker is **silently dropped** — a stale or hand-edited
    link must never break the Desk (ADR-0107 C5, Step 1). This only establishes
    that the value is a UUID at all; whether that case exists and is open is a
    DB question answered by :func:`_resolve_case_marker`.
    """
    if not raw:
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


async def _resolve_case_marker(db_session: AsyncSession, raw: str | None) -> CaseDTO | None:
    """Resolve the ``case=`` marker to an **open** case, or ``None``.

    Malformed, unknown (or foreign-tenant, which RLS resolves to ``None``), and
    closed markers all yield ``None`` — the marker is dropped and the Desk renders
    normally (ADR-0107 C5, Step 1). Validated fresh on every request, so a link
    made while a case was open silently loses its chip once the case closes,
    rather than dangling.
    """
    marker_id = _parse_case_marker(raw)
    if marker_id is None:
        return None
    case = await CaseRepository(db_session).get(marker_id)
    if case is None or case.state != "open":
        return None
    return case


def _build_capturing(
    case: CaseDTO,
    *,
    periodisation: Periodisation,
    horizon: int,
    currency_view: CurrencyView,
    view: WorldView,
    overlay: Overlay,
) -> dict[str, Any]:
    """Build the "Capturing for CASE-NNNN" chip context (ADR-0107 C5, Step 1).

    Carries the badge and title (linking to the case detail) and a
    ``dismiss_query`` — the current page state **without** the marker, so its
    dismiss drops the ``case=`` param through the standard re-projection and
    nothing else changes (binding decision 4).
    """
    return {
        "badge": f"CASE-{case.case_number:04d}",
        "title": case.title,
        "href": f"/cases/{case.id}",
        "dismiss_query": _query(
            periodisation=periodisation,
            horizon=horizon,
            currency_view=currency_view,
            view=view,
            overlay=overlay,
            case_marker=None,
        ),
    }


# ---------------------------------------------------------------------------
# Scenario assembly (the DB seam in front of the pure assembly)
# ---------------------------------------------------------------------------


async def _assemble_scenario(
    *,
    db_session: AsyncSession,
    cash_flow_inputs: Any,
    result: CashFlowPlanningResult,
    overlay: Overlay,
) -> tuple[ScenarioResult | None, str | None]:
    """Load the scenario inputs and assemble the deltas-first result.

    The grid is the cash-flow lens's own — the baseline timeline's period ends
    and its seam — so the two lenses state one period grid (ADR-0104 §5). Any
    coverage-engine or FX failure (:data:`_SCENARIO_ERRORS`) is caught and
    returned as a message, so the result region degrades to a notice while the
    Cash Flow Planning lens stays live. The lens already projected with the same
    overlay and converter, so this rarely fires — it is the rail, not the path.

    Returns:
        ``(scenario_result, None)`` on success, ``(None, message)`` on a caught
        failure.
    """
    scenario_inputs = await load_scenario_result_inputs(
        cash_flow_inputs=cash_flow_inputs,
        evaluation_dates=[period.end_date for period in result.baseline.periods],
        cut_over=result.baseline.seam_date,
        investments=InvestmentRepository(db_session),
        navs=InvestmentNavRepository(db_session),
        cashflows=InvestmentCashflowRepository(db_session),
        asset_classes=AssetClassRepository(db_session),
        limits=LimitsRepository(db_session),
    )
    try:
        return assemble_scenario_result(scenario_inputs, overlay), None
    except _SCENARIO_ERRORS as exc:
        logger.debug(
            "planning desk: scenario assembly failed (%s: %s) — "
            "rendering the result-region notice.",
            type(exc).__name__,
            exc,
        )
        return None, str(exc)


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


@router.get(SECTION_URL, response_class=HTMLResponse)
async def get_cash_flow_planning_section(  # noqa: PLR0913 — one flat page state
    request: Request,
    periodisation: str = Periodisation.QUARTERLY.value,
    horizon: str = str(DEFAULT_HORIZON_QUARTERS),
    currency_view: str = CurrencyView.PER_CURRENCY.value,
    view: str = WorldView.SCENARIO.value,
    pace_id: str | None = None,
    pace_factor: str | None = None,
    hyp_investment_id: str | None = None,
    hyp_txn_type: str | None = None,
    hyp_trade_date: str | None = None,
    hyp_units: str | None = None,
    hyp_price_per_unit: str | None = None,
    hyp_consideration: str | None = None,
    hyp_currency: str | None = None,
    mshock_archetype: str | None = None,
    mshock_magnitude: str | None = None,
    fxshock_currency: str | None = None,
    fxshock_magnitude: str | None = None,
    case: str | None = None,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Render the Cash Flow Planning section and its parameter strip.

    Lazy-loaded on first visibility from the area body, and re-issued by every
    control on the surface — each of which carries the *whole* parameter set,
    so no interaction can silently drop a transformation (ADR-0104 §4).

    The response always carries two things: the section body (the swap target)
    and the sticky parameter strip as an ``hx-swap-oob`` fragment. The strip
    lives above both Sections of the area and therefore outside this endpoint's
    swap target; the out-of-band swap is what keeps it in step with the set it
    renders.

    Two of the parameters are *encoded* transformations (the ``t{n}_`` set); the
    rest are view toggles and the two **transient control shapes** — a slider's
    pacing intent and the entry form's seven ``hyp_…`` fields. Both intents are
    merged into the set server-side and never re-emitted: the response's
    ``HX-Push-Url`` carries the encoding they became.

    Args:
        request: The FastAPI request — the overlay is parsed off its query
            parameters as ordered pairs.
        periodisation: ``quarterly`` (default) or ``monthly``.
        horizon: The horizon in quarters — ``4``, ``8`` (default) or ``12``.
        currency_view: ``per-currency`` (default) or ``functional-only``.
        view: ``scenario`` (default) or ``baseline``.
        pace_id: A pacing slider's investment — the transient half of a pacing
            interaction (S2.5). Merged into the overlay and never re-emitted.
        pace_factor: The factor that slider was released at. Both pacing
            parameters are stated together or not at all.
        hyp_investment_id: The entry form's investment (S2.6). It and the six
            fields below are the transient shape of one hypothetical
            transaction; stating any of them makes the request a *submission*.
        hyp_txn_type: ``buy`` or ``sell`` — the two the form offers.
        hyp_trade_date: The trade date. Strictly after the seam.
        hyp_units: The signed unit count (ADR-0097 §2: positive for a buy,
            negative for a sell — the operator states the sign).
        hyp_price_per_unit: The per-unit price, or empty where a consideration
            is stated instead.
        hyp_consideration: The signed cash effect, overriding
            ``units × price_per_unit`` where stated.
        hyp_currency: The settling currency — the investment's own, rendered
            read-only in the form.
        mshock_archetype: The market builder's Scope — the archetype to shock
            (S34.4). It and ``mshock_magnitude`` are the transient shape of one
            ``market_shock``; stating either makes the request a submission.
        mshock_magnitude: The market shock's level shift, in per cent.
        fxshock_currency: The fx builder's Scope — the currency whose plan-world
            FX path to shock. It and ``fxshock_magnitude`` are the transient
            shape of one ``fx_shock``.
        fxshock_magnitude: The fx shock's move, in per cent.
        case: The optional capture marker (ADR-0107 C5) — a case id this Desk is
            "capturing for". Navigation context, never scenario state: validated
            (open case) and threaded through every emitted link, silently dropped
            when malformed / unknown / closed, and excluded from the overlay
            serialisation and from any snapshot's canonical query.
        session: The active session resolved by :func:`require_session`.

    Returns:
        The rendered section, HTTP 200 — including for a book that cannot be
        projected (no seam, a missing rate, a stale link the frames refuse),
        whose error partial *is* the section's content. HTTP **400** for a
        parameter set that could not be read at all, and for a *submission* the
        plan world refuses — which leaves the set untouched and hands the form
        back with the operator's inputs in it. An interaction that changed the
        set additionally carries ``HX-Push-Url``: the canonical, fully encoded
        URL of the merged set, so the address bar never holds the intent.
    """
    try:
        parsed = (
            _parse_periodisation(periodisation),
            _parse_horizon(horizon),
            _parse_currency_view(currency_view),
            _parse_view(view),
            _overlay_from(request),
        )
        intent = _parse_pacing_intent(pace_id, pace_factor)
    except (ViewParameterError, OverlayError) as exc:
        return _error(
            request,
            exc,
            kind="parameters",
            context=_empty_strip_context(),
            status_code=400,
        )
    periods, quarters, currencies, world, overlay = parsed

    submitted: dict[str, str | None] = {
        "investment_id": hyp_investment_id,
        "txn_type": hyp_txn_type,
        "trade_date": hyp_trade_date,
        "units": hyp_units,
        "price_per_unit": hyp_price_per_unit,
        "consideration": hyp_consideration,
        "currency": hyp_currency,
    }
    # The form submits all seven fields, so *any* of them present makes this a
    # submission — and a request carrying only some of them is a truncated one,
    # answered by the missing-field error rather than by a silent default.
    is_submission = any(value is not None for value in submitted.values())

    # The shock builders are two more transient shapes (S34.4). Each states one
    # card's scope and magnitude; the server appends the shock and re-emits, the
    # way the hypothetical-transaction form and the pacing slider do. A request
    # carries at most one of these three submissions or none.
    market_submitted: dict[str, str | None] = {
        "archetype": mshock_archetype,
        "magnitude": mshock_magnitude,
    }
    is_market_submission = any(value is not None for value in market_submitted.values())
    fx_submitted: dict[str, str | None] = {
        "currency": fxshock_currency,
        "magnitude": fxshock_magnitude,
    }
    is_fx_submission = any(value is not None for value in fx_submitted.values())
    is_any_submission = is_submission or is_market_submission or is_fx_submission

    if intent is not None:
        try:
            overlay = _repace(overlay, *intent)
        except OverlayError as exc:
            # An out-of-bounds factor. The slider cannot emit one — its range
            # comes from the contract — so this is a hand-built URL, and it is
            # refused exactly as a `t{n}_factor` outside the bounds is.
            return _error(
                request,
                exc,
                kind="parameters",
                context=_empty_strip_context(),
                status_code=400,
            )

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        await SessionRepository(db_session).touch_throttled(session.id)
        # The capture marker is resolved once, up front, so every render path
        # inside this context — the book-error strip included — threads it
        # through its links (ADR-0107 C5). ``case_marker`` is the string form the
        # links carry; ``marker_case`` is kept for the "Capturing for" chip.
        marker_case = await _resolve_case_marker(db_session, case)
        case_marker = str(marker_case.id) if marker_case is not None else None
        investments = InvestmentRepository(db_session)
        investments_by_id: dict[UUID, InvestmentDTO] = {
            investment.id: investment for investment in await investments.list_active()
        }
        names = {
            investment_id: investment.name
            for investment_id, investment in investments_by_id.items()
        }
        try:
            inputs = await load_cash_flow_planning_inputs(
                investments=investments,
                navs=InvestmentNavRepository(db_session),
                cashflows=InvestmentCashflowRepository(db_session),
                tenants=TenantRepository(db_session),
                fx_rates=FxRateRepository(db_session),
                periodisation=periods,
            )
        except (PlanSeamMissingError, DuplicateCashPositionError) as exc:
            return _error(
                request,
                exc,
                kind="book",
                context=_build_strip_context(
                    overlay=overlay,
                    names=names,
                    periodisation=periods,
                    horizon=quarters,
                    currency_view=currencies,
                    view=world,
                    case_marker=case_marker,
                ),
            )

        frames = inputs.baseline
        #: The set as it arrived — what a *refused* submission leaves in force.
        stated_overlay = overlay

        def _refused(exc: Exception) -> HTMLResponse:
            """Render a refused entry: the set unchanged, the form handed back.

            The submission is not a parameter until the plan world accepts it,
            so nothing is appended, nothing is pushed to the address bar, and
            the operator gets their own inputs back beside the reason.
            """
            return _error(
                request,
                exc,
                kind="entry",
                status_code=400,
                context={
                    **_build_strip_context(
                        overlay=stated_overlay,
                        names=names,
                        periodisation=periods,
                        horizon=quarters,
                        currency_view=currencies,
                        view=world,
                        case_marker=case_marker,
                    ),
                    **_build_hyp_context(
                        overlay=stated_overlay,
                        frames=frames,
                        names=names,
                        periodisation=periods,
                        horizon=quarters,
                        currency_view=currencies,
                        view=world,
                        case_marker=case_marker,
                        values=submitted,
                        is_open=True,
                    ),
                },
            )

        def _refused_shock(exc: Exception) -> HTMLResponse:
            """Render a refused shock: the set unchanged, the notice shown.

            The provenance rule of :func:`_refused`, for the shock builders: a
            submission the plan world will not carry never becomes a parameter,
            so nothing is appended and nothing is pushed. Unlike the
            hypothetical-transaction form, the builders live in the Scenario
            Analysis section — outside this swap target — so nothing needs
            re-seating: the operator's card stays exactly as they left it while
            the notice states why the shock did not land.
            """
            return _error(
                request,
                exc,
                kind="shock",
                status_code=400,
                context=_build_strip_context(
                    overlay=stated_overlay,
                    names=names,
                    periodisation=periods,
                    horizon=quarters,
                    currency_view=currencies,
                    view=world,
                    case_marker=case_marker,
                ),
            )

        # At most one submission fires (the three field namespaces are
        # disjoint), and each **appends** — application order is list order
        # (ADR-0104 §2), so a shock composes after the transformations already
        # in the set rather than jumping ahead of them.
        if is_submission:
            try:
                entry = _parse_hyp_entry(submitted)
            except ViewParameterError as exc:
                return _refused(exc)
            # **Appended**, never merged in place: two hypothetical trades on
            # one investment are two trades, and application order is list
            # order (ADR-0104 §2).
            overlay = (*overlay, entry)
        elif is_market_submission:
            try:
                shock = _parse_market_shock_entry(
                    market_submitted,
                    present_archetypes=_present_archetypes(frames),
                )
            except ViewParameterError as exc:
                return _refused_shock(exc)
            overlay = (*overlay, shock)
        elif is_fx_submission:
            try:
                shock = _parse_fx_shock_entry(
                    fx_submitted,
                    held_currencies=_held_currencies(frames, inputs.converter.functional_currency),
                )
            except ViewParameterError as exc:
                return _refused_shock(exc)
            overlay = (*overlay, shock)

        push_url: str | None = (
            _pushed(
                periodisation=periods,
                horizon=quarters,
                currency_view=currencies,
                view=world,
                overlay=overlay,
                case_marker=case_marker,
            )
            if intent is not None or is_any_submission
            else None
        )
        strip = _build_strip_context(
            overlay=overlay,
            names=names,
            periodisation=periods,
            horizon=quarters,
            currency_view=currencies,
            view=world,
            case_marker=case_marker,
        )

        pacing = build_pacing_rows(
            frames=frames,
            investments_by_id=investments_by_id,
            called_by_investment=await load_called_amounts(
                cashflows=InvestmentCashflowRepository(db_session),
                investment_ids=capital_account_ids(
                    frames=frames,
                    investments_by_id=investments_by_id,
                ),
            ),
        )

        try:
            result = project_cash_flow_planning(
                baseline=frames,
                overlay=overlay,
                actual_cash=inputs.actual_cash,
                converter=inputs.converter,
                periodisation=periods,
                horizon_quarters=quarters,
            )
        except PlanHorizonInvalidError as exc:
            return _error(
                request,
                exc,
                kind="parameters",
                context=_empty_strip_context(),
                status_code=400,
            )
        except MissingFxRateError as exc:
            return _error(
                request,
                exc,
                kind="fx",
                context={
                    **strip,
                    "missing_pair": (f"{exc.currency} → {inputs.converter.functional_currency}"),
                },
            )
        except OverlayError as exc:
            # OverlayExecutionError — a well-formed transformation the frames
            # refuse. Since S34.2 an `fx_shock` is no longer among them: the
            # composer partitions it out and applies it at the conversion seam
            # (ADR-0104 §3), so an fx_shock arriving by URL now computes a
            # scenario rather than raising ExecutorNotRegisteredError. That
            # error stays catchable here — it now means a kind was mis-routed to
            # the fold — and this still catches at the root of the hierarchy
            # rather than enumerating the execution errors: no kind falls
            # through to a 500.
            #
            # Provenance decides the status. A submission the plan world refuses
            # (a trade dated into realised history, a currency it holds no cash
            # in, a cash effect it cannot derive) is a **bad request**: the entry
            # never becomes a parameter. A set that merely *arrived* by URL is an
            # outcome — the stale-link case, HTTP 200, chips removable. A shock
            # submission is refused the same way, into its own notice; a valid
            # shock rarely reaches here (a market shock on an absent archetype is
            # vacuous, an fx shock is partitioned to the seam), so this is the
            # defensive rail rather than the common path.
            if is_submission:
                return _refused(exc)
            if is_market_submission or is_fx_submission:
                return _refused_shock(exc)
            return _error(request, exc, kind="overlay", context=strip)

        # The Scenario Analysis result region rides along on this response as an
        # out-of-band swap (ADR-0104 §5), the way the strip and the shock
        # builders do — the same overlay, scored once against the same book,
        # over the cash-flow lens's own grid.
        scenario_result, scenario_error = await _assemble_scenario(
            db_session=db_session,
            cash_flow_inputs=inputs,
            result=result,
            overlay=overlay,
        )

    # The "Capturing for" chip — built from the *final* overlay so its dismiss
    # drops the marker while keeping the scenario the operator is looking at
    # (ADR-0107 C5). Only the scored render carries it; the error partials above
    # thread the marker through their links but show no results region.
    capturing = (
        _build_capturing(
            marker_case,
            periodisation=periods,
            horizon=quarters,
            currency_view=currencies,
            view=world,
            overlay=overlay,
        )
        if marker_case is not None
        else None
    )

    context = _build_section_context(
        result=result,
        frames=frames,
        pacing=pacing,
        periodisation=periods,
        horizon=quarters,
        currency_view=currencies,
        view=world,
        overlay=overlay,
        names=names,
        scenario_result=scenario_result,
        scenario_error=scenario_error,
        case_marker=case_marker,
        capturing=capturing,
    )
    response = cast(
        HTMLResponse,
        _templates(request).TemplateResponse(request, _SECTION_TEMPLATE, context),
    )
    if push_url is not None:
        # The request carried an intent — a slider's factor, an entry form's
        # fields — not an encoding. The address bar gets the encoding, which is
        # what "Copy scenario link" and a browser reload both depend on being
        # there (ADR-0104 §4).
        response.headers["HX-Push-Url"] = push_url
    return response


def _error(
    request: Request,
    exc: Exception,
    *,
    kind: str,
    context: dict[str, Any],
    status_code: int = 200,
) -> HTMLResponse:
    """Render one typed failure as the section's actionable error state.

    Args:
        request: The FastAPI request.
        exc: The caught exception. Its message travels verbatim — every error
            of this surface is a :class:`~core.exceptions.PortfoliFlowError`
            (or a view-parameter error raised here), and each names the
            offending id, currency, date or key (``web/errors.py``).
        kind: Which hint the partial shows — ``parameters``, ``book``, ``fx``,
            ``overlay``, ``entry`` or ``shock``. The ``entry`` partial
            additionally re-renders the submission form (the ``hyp`` context); a
            refused ``shock`` leaves its builders untouched in the sibling
            section, so it needs no such re-render.
        context: The strip context, plus any hint-specific extras.
        status_code: ``400`` for an unreadable parameter set and for a refused
            submission, ``200`` for a book the projection cannot carry.

    Returns:
        The rendered error partial.
    """
    logger.debug(
        "planning desk: %s (%s) — rendering the %s error state.",
        type(exc).__name__,
        exc,
        kind,
    )
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            _ERROR_TEMPLATE,
            {
                **context,
                "error_kind": kind,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            },
            status_code=status_code,
        ),
    )


# ---------------------------------------------------------------------------
# The composition drill-down endpoint (the lazy secondary surface)
# ---------------------------------------------------------------------------


@router.get(COMPOSITION_URL, response_class=HTMLResponse)
async def get_scenario_composition_section(
    request: Request,
    periodisation: str = Periodisation.QUARTERLY.value,
    horizon: str = str(DEFAULT_HORIZON_QUARTERS),
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Render the scenario composition drill-down (S34.5, ADR-0104 §5/§7).

    The **secondary** result surface, loaded lazily by the result region so it
    stays off the main render path (the mockup's "composition drill-down as a
    lazy second partial"). It reads the same parameter set the section endpoint
    does — the overlay off the query, plus the periodisation and horizon that fix
    the grid — reprojects the plan world, scores the scenario, and diffs the two
    worlds' NAV-weighted fund composition at the plan horizon.

    Every failure renders as a notice inside the drill-down rather than an HTTP
    error: this is a secondary surface, and a book the main region already
    rendered should not have its drill-down answer with a status line.

    Args:
        request: The FastAPI request — the overlay is parsed off its query.
        periodisation: ``quarterly`` (default) or ``monthly`` — fixes the grid.
        horizon: The horizon in quarters — ``4``, ``8`` (default) or ``12``.
        session: The active session resolved by :func:`require_session`.

    Returns:
        The rendered composition partial, HTTP 200 — a diff table, or a notice.
    """
    try:
        periods = _parse_periodisation(periodisation)
        quarters = _parse_horizon(horizon)
        overlay = _overlay_from(request)
    except (ViewParameterError, OverlayError) as exc:
        return _composition_notice(request, str(exc))

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        await SessionRepository(db_session).touch_throttled(session.id)
        try:
            inputs = await load_cash_flow_planning_inputs(
                investments=InvestmentRepository(db_session),
                navs=InvestmentNavRepository(db_session),
                cashflows=InvestmentCashflowRepository(db_session),
                tenants=TenantRepository(db_session),
                fx_rates=FxRateRepository(db_session),
                periodisation=periods,
            )
        except (PlanSeamMissingError, DuplicateCashPositionError) as exc:
            return _composition_notice(request, str(exc))

        try:
            result = project_cash_flow_planning(
                baseline=inputs.baseline,
                overlay=overlay,
                actual_cash=inputs.actual_cash,
                converter=inputs.converter,
                periodisation=periods,
                horizon_quarters=quarters,
            )
        except (
            PlanHorizonInvalidError,
            MissingFxRateError,
            OverlayError,
        ) as exc:
            return _composition_notice(request, str(exc))

        scenario_result, scenario_error = await _assemble_scenario(
            db_session=db_session,
            cash_flow_inputs=inputs,
            result=result,
            overlay=overlay,
        )

    if scenario_result is None:
        return _composition_notice(request, scenario_error or "The scenario could not be scored.")

    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            _COMPOSITION_TEMPLATE,
            _build_composition_context(scenario_result.composition),
        ),
    )


def _composition_notice(request: Request, message: str) -> HTMLResponse:
    """Render the composition drill-down as a notice (the diff was unavailable)."""
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            _COMPOSITION_TEMPLATE,
            {"composition": {"error": message}},
        ),
    )


# ---------------------------------------------------------------------------
# Scenario-snapshot pinning — the pin pipeline's second artifact class
# (ADR-0107 C5)
#
# A scenario state becomes a frozen, curated record on a case's timeline. The
# snapshot serialises exactly what the results region *renders* — parameter
# chips, the four KPI pairs, the headroom families, the two horizon feet — plus
# the canonical query string as documentation. Nothing new is computed (Gate-C0):
# the endpoint rebuilds the results context through the very builders the page
# uses, then reads their presentation output. No charts, no live links, no
# rehydration path (binding decisions 1-3).
# ---------------------------------------------------------------------------

#: The artifact-class discriminator written into a case's pin payload
#: (binding decision 3). The Cases timeline reads it to select the
#: scenario-snapshot render arm; any other value falls to its calm fallback.
_SNAPSHOT_ARTIFACT: str = "scenario_snapshot"

#: The Cases area page the empty-picker dialog links out to (there are no open
#: cases to capture into, so the dialog points the PM at Cases rather than
#: offering a dead dropdown — Step 2).
_CASES_AREA_URL: str = "/cases"

_PIN_CLOSED_CASE_MESSAGE: str = (
    "This case is closed — closed cases are read-only and cannot be captured "
    "into. Pick an open case."
)
_PIN_NO_RESULT_MESSAGE: str = (
    "This scenario produced no result to pin — an empty snapshot is never "
    "frozen. Re-check the parameters and try again."
)


def _partial(
    request: Request,
    template: str,
    context: dict[str, Any],
    *,
    status_code: int = 200,
) -> HTMLResponse:
    """Render ``template`` with ``context`` as an :class:`HTMLResponse`."""
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(request, template, context, status_code=status_code),
    )


def _picker_options(cases: Sequence[CaseDTO]) -> list[dict[str, str]]:
    """Project open cases into the pin dialog's picker (``CASE-NNNN — title``).

    Order is the repository's — newest ``opened_at`` first (Step 2); the route
    never re-sorts.
    """
    return [
        {
            "id": str(case.id),
            "label": f"CASE-{case.case_number:04d} — {case.title}",
        }
        for case in cases
    ]


def _snapshot_chips(chips: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Freeze the parameter chips as presentation, dropping their live links.

    A snapshot is a frozen record with **no live links** into the Desk (binding
    decision 3): each chip keeps its ``label`` — the parameter line — and its
    ``css_class`` — the tone — and never its ``remove_query``, which is a control
    affordance against a page state the frozen record is deliberately severed
    from.
    """
    return [{"label": chip["label"], "css_class": chip["css_class"]} for chip in chips]


def _build_snapshot(
    *,
    scenario: dict[str, Any],
    chips: list[dict[str, Any]],
    query: str,
) -> dict[str, Any]:
    """Serialise the rendered results view model into the frozen snapshot.

    Exactly the presentation-level data the results region shows — the parameter
    chips, the four KPI pairs, the headroom families, the two horizon feet — plus
    the canonical query string as documentation (binding decisions 1-3).
    **No charts** (``baseline_spec`` / ``scenario_spec`` are dropped) and nothing
    recomputed: ``scenario`` is the very ``_build_scenario_context`` sub-context
    the page renders, ``chips`` the very ``_build_chips`` projection, ``query`` a
    ``_query`` built with no marker (it is scenario state, not navigation).
    """
    return {
        "chips": _snapshot_chips(chips),
        "kpis": scenario["kpis"],
        "headroom": scenario["headroom"],
        "baseline_foot": scenario["baseline_foot"],
        "scenario_foot": scenario["scenario_foot"],
        "query": query,
    }


@router.get(PIN_SCENARIO_URL, response_class=HTMLResponse)
async def get_pin_scenario_dialog(  # noqa: PLR0913 — one flat page state
    request: Request,
    periodisation: str = Periodisation.QUARTERLY.value,
    horizon: str = str(DEFAULT_HORIZON_QUARTERS),
    currency_view: str = CurrencyView.PER_CURRENCY.value,
    view: str = WorldView.SCENARIO.value,
    case: str | None = None,
    cancel: bool = False,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Render the "Pin to case…" dialog into ``#pd-pin-dialog`` (ADR-0107 C5).

    Opened by the results region's affordance, carrying the current page state
    (which the dialog embeds as hidden inputs, so the POST freezes exactly the
    state on screen) and the ``case`` marker (which preselects the picker). A
    ``cancel`` flag clears the slot — the composer-cancel idiom.

    The picker is the tenant's open cases, newest first; with none, the dialog
    says so calmly and links to the Cases area rather than offering a dead
    dropdown (Step 2). The page state is re-serialised as ``(key, value)`` pairs
    **without** the marker — a snapshot's query is scenario state, not navigation.

    A malformed dialog-open link (the affordance never emits one) degrades to an
    empty slot rather than a status line on this secondary surface.
    """
    if cancel:
        return HTMLResponse("")
    try:
        periods = _parse_periodisation(periodisation)
        quarters = _parse_horizon(horizon)
        currencies = _parse_currency_view(currency_view)
        world = _parse_view(view)
        overlay = _overlay_from(request)
    except (ViewParameterError, OverlayError):
        return HTMLResponse("")

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        open_cases = await CaseRepository(db_session).list_open()
        marker_case = await _resolve_case_marker(db_session, case)

    return _partial(
        request,
        _PIN_DIALOG_TEMPLATE,
        {
            "csrf_token": session.csrf_token,
            "pin_scenario_url": PIN_SCENARIO_URL,
            "cases_area_url": _CASES_AREA_URL,
            "cases": _picker_options(open_cases),
            "preselect": (str(marker_case.id) if marker_case is not None else None),
            "state_pairs": _state_pairs(
                periodisation=periods,
                horizon=quarters,
                currency_view=currencies,
                view=world,
                overlay=overlay,
            ),
            "comment": "",
            "error": None,
        },
    )


@router.post(PIN_SCENARIO_URL, response_class=HTMLResponse)
async def post_pin_scenario(
    request: Request,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Freeze the on-screen scenario result and pin it to a case (ADR-0107 C5).

    The body carries the page state (the same params the page round-trips) plus
    ``case_id`` and ``comment``. Gates, each re-rendering the dialog with an
    inline error: comment non-empty → case exists → case open → the posted state
    yields a scenario result (else "nothing to pin"; an empty snapshot is never
    frozen). The results context is rebuilt through the *same* builders the page
    uses; the snapshot serialises the presentation data (no charts, no live
    links) and the canonical query as documentation (binding decisions 1-3).

    On success the dialog is replaced by a quiet in-place confirmation with a
    link to the case — the Desk keeps its state, so the PM can pin another right
    away (Step 3). Nothing is written on any gate failure.
    """
    form = getattr(request.state, "form", None)
    if form is None:
        form = await request.form()

    comment = str(form.get("comment") or "").strip()
    case_id_raw = str(form.get("case_id") or "").strip()

    # The page state is parsed first — a malformed set is a hand-built request
    # (the dialog only ever emits valid hidden inputs), answered as an empty slot
    # rather than folded into a snapshot.
    try:
        periods = _parse_periodisation(str(form.get("periodisation") or ""))
        quarters = _parse_horizon(str(form.get("horizon") or ""))
        currencies = _parse_currency_view(str(form.get("currency_view") or ""))
        world = _parse_view(str(form.get("view") or ""))
        overlay = parse_overlay(form.multi_items())
    except (ViewParameterError, OverlayError):
        return HTMLResponse("")

    state_pairs = _state_pairs(
        periodisation=periods,
        horizon=quarters,
        currency_view=currencies,
        view=world,
        overlay=overlay,
    )
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        open_cases = await CaseRepository(db_session).list_open()

        def _dialog_error(message: str, *, status_code: int = 200) -> HTMLResponse:
            """Re-render the dialog inline, carrying an error and the inputs.

            200 for a reliable HTMX swap (the composer idiom); the picked case
            and the comment survive so the operator does not retype them.
            """
            return _partial(
                request,
                _PIN_DIALOG_TEMPLATE,
                {
                    "csrf_token": session.csrf_token,
                    "pin_scenario_url": PIN_SCENARIO_URL,
                    "cases_area_url": _CASES_AREA_URL,
                    "cases": _picker_options(open_cases),
                    "preselect": case_id_raw or None,
                    "state_pairs": state_pairs,
                    "comment": comment,
                    "error": message,
                },
                status_code=status_code,
            )

        # Gate 1: a curation comment is mandatory.
        if not comment:
            return _dialog_error(
                "A capture comment is required — say what this scenario shows "
                "and why it is worth keeping."
            )
        # Gate 2: the case exists in this tenant. The picker only offers real
        # open cases, so a miss is a race (closed since) or a hand-built id.
        try:
            case_id = UUID(case_id_raw)
        except ValueError:
            return _dialog_error("Choose a case to capture into.")
        case = await CaseRepository(db_session).get(case_id)
        if case is None:
            return _dialog_error("That case could not be found — it may have just been closed.")
        # Gate 3: the case is open (closed cases are immutable, ADR-0107 §4).
        if case.state != "open":
            return _dialog_error(_PIN_CLOSED_CASE_MESSAGE)

        # Gate 4: the posted state yields a scenario result. Rebuild the results
        # context through the same builders the page uses; any book/projection
        # failure is "nothing to pin" rather than a status line.
        investments = InvestmentRepository(db_session)
        names = {inv.id: inv.name for inv in await investments.list_active()}
        try:
            inputs = await load_cash_flow_planning_inputs(
                investments=investments,
                navs=InvestmentNavRepository(db_session),
                cashflows=InvestmentCashflowRepository(db_session),
                tenants=TenantRepository(db_session),
                fx_rates=FxRateRepository(db_session),
                periodisation=periods,
            )
            result = project_cash_flow_planning(
                baseline=inputs.baseline,
                overlay=overlay,
                actual_cash=inputs.actual_cash,
                converter=inputs.converter,
                periodisation=periods,
                horizon_quarters=quarters,
            )
        except (
            PlanSeamMissingError,
            DuplicateCashPositionError,
            PlanHorizonInvalidError,
            MissingFxRateError,
            OverlayError,
        ):
            return _dialog_error(_PIN_NO_RESULT_MESSAGE)

        scenario_result, _scenario_error = await _assemble_scenario(
            db_session=db_session,
            cash_flow_inputs=inputs,
            result=result,
            overlay=overlay,
        )
        if scenario_result is None:
            return _dialog_error(_PIN_NO_RESULT_MESSAGE)

        # The frozen record: the very presentation the results region renders.
        scenario = _build_scenario_context(
            result=scenario_result,
            functional_currency=result.baseline.functional_currency,
            labels=[period.label for period in result.baseline.periods],
            composition_query="",
        )["scenario"]
        chips = _build_chips(
            overlay=overlay,
            names=names,
            periodisation=periods,
            horizon=quarters,
            currency_view=currencies,
            view=world,
        )
        snapshot = _build_snapshot(
            scenario=scenario,
            chips=chips,
            query=_query(
                periodisation=periods,
                horizon=quarters,
                currency_view=currencies,
                view=world,
                overlay=overlay,
            ),
        )

        try:
            await CaseRepository(db_session).append_entry(
                case_id,
                kind="pin",
                actor="pm",
                actor_user_id=session.user_id,
                payload={
                    "artifact": _SNAPSHOT_ARTIFACT,
                    "comment": comment,
                    "snapshot": snapshot,
                },
                now=datetime.now(timezone.utc),
            )
        except (CaseClosedError, CaseStateInvalid):
            # Raced to closed between the open-gate and the write.
            return _dialog_error(_PIN_CLOSED_CASE_MESSAGE)

    logger.info(
        "planning desk: scenario snapshot pinned tenant=%s user=%s case=%s",
        session.tenant_id,
        session.user_id,
        case_id,
    )
    return _partial(
        request,
        _PIN_CONFIRM_TEMPLATE,
        {
            "case_badge": f"CASE-{case.case_number:04d}",
            "case_href": f"/cases/{case_id}",
            "pin_scenario_url": PIN_SCENARIO_URL,
        },
    )
