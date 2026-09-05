# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Transactions area web surface — the composers, the wizard and their gestures.

The ninth Area's working surfaces (ADR-0128, S4a + S4b + S4c): the MD-1 flow
chooser, the M-1 order composer for U-BUY / U-SELL against an instrument
already on the book, the M-2 four-step wizard for U-NEW — the purchase whose
instrument does not exist yet — the M-3 R-SEC-SELL composer for the full
disposal of a statement-valued stake, the recalculation endpoint that keeps
every derived element truthful while the user types, and the gestures that
turn what is on a form into a ticket.

Two surfaces, one substrate (S4b)
---------------------------------
The wizard is a *surface over what already exists*. It adds four endpoints —
two renders and a read — and **no write path**: Continue is
``POST /api/transactions/draft`` with a step number on it, Propose and Book
now are the composer's own, and the ``investments`` row the flow is about is
created by the emission at booking and nowhere else (MD-12). The three
places the two surfaces genuinely differ are named and small:

* :class:`_ComposerForm` carries the wizard's fields in the *same* single
  inventory, so a field cannot exist on one surface and be forgotten by the
  other;
* :func:`_ensure_draft` is kind-aware — one MD-2 rule, two column maps that
  differ in ``direction`` (a flow constant, MD-14), ``investment_id``
  (always absent, MD-12) and ``currency`` (the step-1 field, W-4);
* :func:`_derived_context` learns one fallback, :class:`_Creating`, for the
  facts a picked investment would otherwise supply.

Everything else — the preview, the amounts, the settlement panel, the
message strip, the confirmation panel — is reached by both, unchanged.

Three surfaces, still one substrate (S4c)
------------------------------------------
The R-SEC-SELL composer joins on the same terms, and adds **no endpoint that
writes**: Save as draft, Propose and Book now are the composer's own three,
and the only new route is the ``GET`` that opens it. The same three seams
absorb it — one more field pair on :class:`_ComposerForm`, a third column on
:func:`_ensure_draft`'s kind-map, and two more keyword arguments on
:func:`_derived_context` — and :func:`_composer_context` serves both picking
surfaces from one assembly, switched on the flow rather than copied.

What genuinely differs is what a **reported** stake can be asked. It holds no
units, so there is no holding and no last price; it carries a last reported
NAV and an unfunded commitment instead, and its booking emits four rows where
an order emits two legs. MD-18 adds the one refusal this module owns
outright: a partial sale is not representable in the schema, so no service
can refuse it and the block lives here — the single block-aware term in
``draft_enabled``.

Reads and writes, kept apart
----------------------------
Ten endpoints. ``order-form``, ``secondary-sale-form``, ``wizard``,
``chooser``, ``recalc`` and ``resolve-identifier`` are reads: they derive,
they render, and they touch no row (MD-2 — opening a composer allocates
nothing and burns no ticket number). ``draft``, ``propose``, ``book`` and
``cash-position`` are the writes, owner-gated and CSRF-checked, and every one
of them re-checks server-side what the surface had already gated: a form is a
suggestion, never a permission. ``web/routes/areas.py`` stays a no-DB shell
render: the chooser is static markup in the area body, and everything that
needs the database sits behind the HTMX endpoints below.

The first explicit gesture allocates the ticket (MD-2), and that rule lives
in exactly one function — :func:`_ensure_draft`. All three gestures go
through it, so "Book now" on a never-saved composer writes the same draft
row that "Save as draft" would have, and then books it.

Copy gaps registered for the operator's walk (S4b)
--------------------------------------------------
M-2 is always fully filled in, so four states it never draws have no mockup
copy and are written here in its voice: the missing-currency refusal
(:data:`_WIZARD_CURRENCY_REQUIRED`), the no-match resolution
(:data:`_RESOLVE_NO_MATCH`), the wizard's own action hints, and the
"— not named yet —" placeholders on the Confirm step. Three M-2 *deviations*
are registered with them: the resolved values are editable rather than
read-only (W-4′), the identify cards both stand open rather than switching
on a radio, and "Discard draft" reads **Close** — nothing on this surface
destroys anything.

One uniform refusal (operator decision D-5, extended)
-----------------------------------------------------
Every typed service refusal a gesture can raise re-renders the composer with
``str(exc)`` in the red block and the forward actions disabled. The service
sentences are operator-grade and already name their remedy, so **no block
copy is invented here** — which is also how the S2b blocks that the composer
cannot preview (``nav_exists_at_trade_date``, ``investment_inactive``)
surface when a race makes them reachable. Nothing is written when one fires;
a draft that already existed stays a draft.

The one derivation, three times over
------------------------------------
Every number this surface shows comes from the service layer, never from
Jinja and never from JavaScript:

* the **net** cash effect is :meth:`~services.transactions.ticket_service
  .TicketService.preview`'s ``cash_effect``;
* the **gross** is the same
  :func:`~services.transactions.validation.derive_cash_effect`, called a
  second time with fees and taxes omitted — the difference between the two
  rows *is* the costs, rather than a template subtracting them again;
* the **ledger legs** are
  :func:`~services.transactions.emission.order_legs`, the pure function the
  booking itself uses, so what the composer promises and what the booking
  writes cannot drift;
* the **holdings** and every cash balance are
  :func:`~services.investments.holdings.holdings_as_of` over the tenant's own
  ledger rows.

The composer therefore states consequences by running exactly the code that
would cause them (operator decision D-2), against a **transient** ticket that
is never persisted.

Clock discipline (ADR-0127)
---------------------------
:func:`_now` and :func:`_today` are the module's only clock reads and the
monkeypatch seam the tests use. Every ``preview`` call is handed both.

Form-field contract
-------------------
The composer posts one flat form and every endpoint here parses it through
the one dependency, :class:`_ComposerForm`, so the inventory is written once.
The names map 1:1 onto
:class:`~core.repositories.trade_ticket_repository.TradeTicketDTO` columns,
which is what lets a gesture map the body onto the repository's draft
whitelist without a translation layer:

``direction``, ``investment_id``, ``trade_date``, ``settlement_date``,
``units``, ``price_per_unit``, ``fees``, ``taxes``, ``cash_investment_id``,
``settle_confirm``, ``set_inactive``, ``case_id``, ``source``, ``note``,
``currency``.

Three names carry no column. ``ticket_id`` is the composer's own memory of
which row it is editing — absent means "not saved yet" (MD-2) — and
``cash_name`` / ``cash_opening_balance`` belong to the MD-3 mini-form, which
rides inside the composer's form because HTML has no nested forms.

The R-SEC-SELL composer adds ``gross_amount`` — the stated proceeds a
reported stake has in place of units × price — and ``fraction``, MD-18's
scope control whose only refusable value is ``partial``.

The wizard adds ``flow`` and ``step`` — the flow signal and the
body to render, neither of which is state — and the nine ``md_*`` fields,
which carry no column each but *are* one together:
:meth:`_ComposerForm.master_data` projects them onto ``master_data``'s JSONB
payload. Six of the fifteen ``MD_*`` keys stay unmapped, because no flow this
strand ships uses them: ``vintage_year``, ``commitment_amount``,
``purchase_price``, ``acquired_nav`` and ``assumed_unfunded`` belong to
R-COMMIT and R-SEC-BUY (S4c), and ``currency`` is written from the ticket
column rather than from an ``md_*`` field of its own.

Copy
----
MD-9 makes the M-1 mockup's wording binding, and the templates lift it
verbatim. The sentences the mockup does not contain — the ledger-block
placeholder (operator decision D-3), the settlement state where every cash
row in the currency has been deactivated (D-F), the action hint for a form
too sparse to act on, and P-2's saved-ticket chrome, confirmation panel and
mini-form refusals — are written in M-1's voice and registered as copy gaps
for the operator's walk.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date as _date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core.exceptions import (
    CurrencyMismatchError,
    NonNegativeHoldingsError,
    TicketIncomplete,
    TicketNotFound,
    TicketStateInvalid,
    ValidationError,
    ValuationModeError,
)
from core.repositories._session import tenant_context
from core.repositories.anlv_category_repository import AnlVCategoryRepository
from core.repositories.asset_class_repository import AssetClassRepository
from core.repositories.case_repository import CaseRepository
from core.repositories.instrument_price_repository import InstrumentPriceRepository
from core.repositories.investment_cashflow_repository import (
    InvestmentCashflowRepository,
)
from core.repositories.investment_identifier_repository import (
    InvestmentIdentifierRepository,
)
from core.repositories.investment_nav_repository import InvestmentNavRepository
from core.repositories.investment_repository import InvestmentDTO, InvestmentRepository
from core.repositories.position_transaction_repository import (
    PositionTransactionRepository,
)
from core.repositories.trade_ticket_repository import (
    TradeTicketDTO,
    TradeTicketEffectDTO,
    TradeTicketRepository,
)
from services.auth.session import SessionDTO
from services.investments.aum import CASH_TYPE
from services.investments.credential_resolver import (
    CredentialResolver,
    ProviderCredential,
)
from services.investments.holdings import holdings_as_of
from services.investments.investment_service import InvestmentService
from services.investments.pacing_rows import load_called_amounts, unfunded_commitment
from services.transactions.constants import (
    BLOCK_OVERSELL,
    BLOCK_PARTIAL_SECONDARY_SALE,
    BOOKABLE_STATUSES,
    DIRECTION_BUY,
    DIRECTION_SELL,
    KIND_ORDER,
    KIND_SECONDARY,
    MD_ANLV_CODE,
    MD_ASSET_CLASS_ID,
    MD_CURRENCY,
    MD_FIGI,
    MD_IDENTIFIER_SCHEME,
    MD_IDENTIFIER_VALUE,
    MD_INVESTMENT_TYPE,
    MD_MANAGER,
    MD_NAME,
    MD_REGION,
    STATUS_DRAFT,
    WARNING_FUTURE_TRADE_DATE,
    WARNING_NEGATIVE_CASH,
    WARNING_NET_NON_POSITIVE,
    WARNING_PRICE_DEVIATION,
)

# The mapping seam and the port's exceptions, named module by module rather
# than through the package root. ADR-0093 keeps the *provider machinery* out
# of ``web/`` — the adapters, the factory that routes to one, the refresh core
# — and the package root imports the factory on the way past. Reaching for
# ``normalisation`` and ``provider`` directly is the ``provider_credentials``
# precedent and states the narrower dependency this route actually has: one
# deterministic identifier lookup, awaited, on an operator's explicit click.
from services.market_data.normalisation import ResolvedInstrument, resolve_instrument
from services.market_data.provider import (
    IdentifierNotResolvableError,
    ProviderFetchError,
    UnsupportedCapabilityError,
)
from services.transactions.emission import (
    EFFECT_CASHFLOW,
    EFFECT_INVESTMENT_UPDATE,
    EFFECT_NAV,
    EFFECT_POSITION_TXN,
    VALUATION_MODE_REPORTED,
    VALUATION_MODE_UNITISED,
    cash_leg,
    order_legs,
    provenance,
)
from services.transactions.ticket_service import TicketService
from services.transactions.validation import (
    TicketBlock,
    TicketPreview,
    TicketWarning,
    TicketWarnings,
    derive_cash_effect,
    nearest_price,
    signed_deviation_ratio,
)
from web.auth import require_session, verify_csrf
from web.permissions import require_role

router = APIRouter()


# ---------------------------------------------------------------------------
# The M-2 wizard's vocabulary (S4b)
# ---------------------------------------------------------------------------

#: The ``flow`` value that puts this surface on the creating path (U-NEW).
#:
#: One string, posted as a hidden field by every wizard step, read by
#: :class:`_ComposerForm` and by nothing else. It is what tells a *shared*
#: endpoint — ``draft`` and ``recalc`` serve both surfaces — which shape it is
#: looking at, and it is deliberately not inferred from "no investment picked":
#: a U-BUY composer with an empty picker looks exactly like that and is not
#: creating anything (the :func:`~services.transactions.validation
#: .is_investment_creating` distinction, one layer down).
FLOW_NEW_INSTRUMENT: str = "new_instrument"

#: The ``flow`` value that puts this surface on the secondary-sale path
#: (R-SEC-SELL, S4c).
#:
#: The same posture as :data:`FLOW_NEW_INSTRUMENT` and for the same reason:
#: the flow is *signalled*, never inferred. A secondary sale and a U-SELL both
#: name an existing investment and both post a ``sell`` direction, so nothing
#: on the body distinguishes them except this field — and inferring the kind
#: from "the picked row happens to be reported" would let a stale picker
#: silently change what a gesture writes.
FLOW_SECONDARY_SALE: str = "secondary_sale"

#: The wizard's four steps, in M-2's order. Index + 1 is the step number.
_WIZARD_STEPS: tuple[str, ...] = ("Identify", "Classify", "Order", "Confirm")

#: The identifier schemes M-2's Identify select offers.
#:
#: Exactly the three :func:`~services.market_data.resolve_instrument` maps to
#: an OpenFIGI ID type; the resolver refuses the rest with
#: ``UnsupportedCapabilityError``, so offering them would be offering a
#: control that cannot work.
_RESOLVABLE_SCHEMES: tuple[str, ...] = ("isin", "ticker", "cusip")

#: The provider key the Identify step resolves its OpenFIGI credential under.
_OPENFIGI: str = "openfigi"

#: The ``investment_type`` values M-2's Classify control offers.
#:
#: Seven of the eight (:data:`~core.models.investment.INVESTMENT_TYPES`), in
#: the mockup's own order. ``cash`` is absent by design and not by omission: a
#: cash position is what an order settles *against*, and the one way to open
#: one on this surface is the MD-3 mini-form, which derives its currency and
#: books an opening row the wizard has no equivalent of.
_CLASSIFIABLE_TYPES: tuple[str, ...] = (
    "listed_equity",
    "listed_bonds",
    "private_equity",
    "private_debt",
    "real_estate",
    "infra_equity",
    "other",
)


@dataclass(frozen=True)
class _Creating:
    """What the creating path substitutes for the picked investment's facts.

    The composer derives its currency, its instrument name and its AnlV
    classification from the ``investments`` row the user picked. A U-NEW
    ticket has no such row — MD-12 makes it an emission effect — so these
    three facts come off the form instead, and this is the one object that
    carries them into :func:`_derived_context`.

    One parameter rather than three, because it is one fallback: either the
    surface is looking at a picked investment or it is looking at this.

    Attributes:
        currency: The step-1 currency (W-4), already shape-validated.
        name: The Classify step's name, or ``None`` before it is typed.
        anlv_set: Whether an AnlV category has been chosen — the MD-21 finish
            gate's input, not a master-data value.
    """

    currency: str
    name: str | None
    anlv_set: bool


# ---------------------------------------------------------------------------
# Wiring helpers
# ---------------------------------------------------------------------------


def _templates(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)


def _engine(request: Request) -> AsyncEngine:
    return cast(AsyncEngine, request.app.state.engine)


def _now() -> datetime:
    """Return the current instant as a timezone-aware UTC datetime.

    The module's only instant read (ADR-0127): every ``preview`` call is
    handed this value rather than reading a clock further down, so a test can
    fix "now" by patching one name.
    """
    return datetime.now(timezone.utc)


def _today() -> _date:
    """Return the current date, derived from :func:`_now`.

    Deliberately *not* a second clock read: the future-trade-date warning and
    the oversell candidate's ordering must agree about when they are running.
    """
    return _now().date()


def _build_ticket_service(session: AsyncSession) -> TicketService:
    """Construct a fully wired :class:`TicketService` on a tenant-scoped session.

    Every dependency is wired, including the ``asset_classes`` repository
    that only :meth:`~services.investments.investment_service.InvestmentService
    .create_cash_position` needs — the MD-3 mini-form's call, which reaches it
    through :func:`_build_investment_service` rather than through this
    service. An unwired repository fails loudly at first use (the CP-07
    pattern) rather than at construction.

    All repositories share the caller's session, so a booking's ledger rows,
    its ``trade_ticket_effects`` linkage and the ticket's status flip commit
    together (ADR-0128 §2) — a property this read-only sub-strand does not
    exercise but must not design away.

    Args:
        session: A session already scoped by ``tenant_context``.

    Returns:
        The service, ready for :meth:`~services.transactions.ticket_service
        .TicketService.preview` and for P-2's transitions.
    """
    investments = InvestmentRepository(session)
    navs = InvestmentNavRepository(session)
    cashflows = InvestmentCashflowRepository(session)
    return TicketService(
        tickets=TradeTicketRepository(session),
        investments=investments,
        position_transactions=PositionTransactionRepository(session),
        instrument_prices=InstrumentPriceRepository(session),
        investment_service=_build_investment_service(session),
        navs=navs,
        cashflows=cashflows,
    )


def _build_investment_service(session: AsyncSession) -> InvestmentService:
    """Construct the ledger write seam, wired for every method this module calls.

    Two callers: :func:`_build_ticket_service`, for which this is the D-A
    write seam a booking emits through, and the MD-3 mini-form, which calls
    :meth:`~services.investments.investment_service.InvestmentService
    .create_cash_position` directly. Extracted so the second one does not
    reach into the ticket service's private attribute for a service it holds
    — a route reading ``_investment_service`` would be a dependency on the
    service's internals rather than on its interface.

    Repositories are stateless wrappers over the session
    (:class:`~core.repositories.base.BaseRepository`), so the instances this
    builds are interchangeable with the ticket service's own. What has to be
    shared for the ADR-0128 §2 atomicity guarantee is the **session**, and it
    is: every repository below takes the caller's.

    Args:
        session: A session already scoped by ``tenant_context``.

    Returns:
        The service, ready for the ledger writes and for the cash-position
        triple.
    """
    return InvestmentService(
        investments=InvestmentRepository(session),
        navs=InvestmentNavRepository(session),
        cashflows=InvestmentCashflowRepository(session),
        identifiers=InvestmentIdentifierRepository(session),
        position_transactions=PositionTransactionRepository(session),
        instrument_prices=InstrumentPriceRepository(session),
        asset_classes=AssetClassRepository(session),
    )


# ---------------------------------------------------------------------------
# Form parsing — a keystroke endpoint refuses nothing
# ---------------------------------------------------------------------------


def _decimal_or_none(raw: str | None) -> Decimal | None:
    """Parse a form value into a :class:`Decimal`, or ``None``.

    Absent, blank and unparseable all read as ``None``. The recalculation
    endpoint fires on every keystroke, where a half-typed number is the
    normal case and not an error: the surface simply derives less until the
    value is complete (operator decision D-2's sparse contract).
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _date_or_none(raw: str | None) -> _date | None:
    """Parse an ISO date form value, or ``None`` when absent or malformed."""
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        return _date.fromisoformat(text)
    except ValueError:
        return None


def _uuid_or_none(raw: str | None) -> UUID | None:
    """Parse a UUID form value, or ``None`` when absent or malformed."""
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        return UUID(text)
    except ValueError:
        return None


def _clean(raw: str | None) -> str | None:
    """Return a stripped string, or ``None`` when it carries nothing."""
    if raw is None:
        return None
    text = raw.strip()
    return text or None


def _step_or_first(raw: str | None) -> int:
    """Read the wizard step to render, defaulting to the first.

    The step is a **navigation value, not state**: it says which of M-2's
    four bodies the response should show, it is posted by the button that
    asked for it, and nothing persists it — the ticket's own content is what
    a resumed wizard derives its step from (:func:`_resume_step`). Anything
    outside ``1..4`` reads as 1, on the same permissive contract as every
    other field here: a tampered step is a step that says nothing.
    """
    if raw is None:
        return 1
    text = raw.strip()
    return int(text) if text.isdigit() and 1 <= int(text) <= len(_WIZARD_STEPS) else 1


class _ComposerForm:
    """The composer's posted body, parsed once for every endpoint that reads it.

    A FastAPI class dependency rather than fifteen repeated ``Form(...)``
    parameters on five handlers. The field inventory is a contract between
    this module and ``_order_composer.html``, and a contract stated five
    times is a contract that drifts: one of the copies acquires a field, or
    loses a default, and only one endpoint notices.

    **Nothing here refuses anything.** Parsing is
    :func:`_decimal_or_none`'s permissive contract throughout — absent, blank
    and unparseable all read as ``None`` — because the same body is posted by
    a keystroke (where a half-typed number is normal) and by a gesture (where
    the *service* is the authority on what is missing, and says so in a
    sentence the surface has no better version of). ``direction`` is the one
    value narrowed on arrival, to the two-member vocabulary the ticket
    column's CHECK would otherwise refuse.

    S4b adds the wizard's fields to the same inventory rather than beside it.
    ``flow`` is the creating-path signal (empty for U-BUY / U-SELL,
    ``new_instrument`` for the M-2 wizard), ``currency`` is the step-1 fact
    the creating path has no investment to derive one from (operator decision
    W-4; U-BUY / U-SELL ignore it and keep deriving from the picked row,
    MD-8), and the nine ``md_*`` fields are the master-data payload the
    wizard carries on the ticket until booking creates the investment
    (MD-12). The names mirror the ``MD_*`` keys so
    :meth:`master_data` is a rename-free projection.

    S4c adds two more to the same inventory. ``gross_amount`` is the
    R-SEC-SELL proceeds — a *stated* amount rather than a derived one, since
    a reported stake has no units to multiply — and ``fraction`` is MD-18's
    scope control, whose only refusable value is ``partial``. Neither is
    offered by the order composer or the wizard, and both are harmless
    there: an order derives its own gross and never posts a ``fraction``,
    which then reads as the default ``full``.

    Attributes:
        entered: The raw strings, echoed back into the composer's own inputs
            so a re-render after a gesture shows what the user typed rather
            than an empty form. Deliberately *not* the parsed values: a
            re-formatted number in a ``type="number"`` input is a number the
            browser may reject, and the form is the unit of state.
    """

    def __init__(
        self,
        # ``Annotated`` rather than this module's ``= Form(default)`` idiom
        # for one reason: it leaves a real Python default on every parameter,
        # so :func:`_empty_form` can construct the opening state by calling
        # the class. With ``= Form("")`` the default *is* the ``FormInfo``
        # object, and a direct call would silently parse metadata as input.
        direction: Annotated[str, Form()] = DIRECTION_SELL,
        investment_id: Annotated[str, Form()] = "",
        trade_date: Annotated[str, Form()] = "",
        settlement_date: Annotated[str, Form()] = "",
        units: Annotated[str, Form()] = "",
        price_per_unit: Annotated[str, Form()] = "",
        gross_amount: Annotated[str, Form()] = "",
        fees: Annotated[str, Form()] = "",
        taxes: Annotated[str, Form()] = "",
        cash_investment_id: Annotated[str, Form()] = "",
        settle_confirm: Annotated[str | None, Form()] = None,
        set_inactive: Annotated[str | None, Form()] = None,
        case_id: Annotated[str, Form()] = "",
        source: Annotated[str, Form()] = "",
        note: Annotated[str, Form()] = "",
        ticket_id: Annotated[str, Form()] = "",
        cash_name: Annotated[str, Form()] = "",
        cash_opening_balance: Annotated[str, Form()] = "",
        # -- the M-2 wizard's own inventory (S4b) ------------------------
        flow: Annotated[str, Form()] = "",
        step: Annotated[str, Form()] = "",
        currency: Annotated[str, Form()] = "",
        md_identifier_scheme: Annotated[str, Form()] = "",
        md_identifier_value: Annotated[str, Form()] = "",
        md_figi: Annotated[str, Form()] = "",
        md_name: Annotated[str, Form()] = "",
        md_investment_type: Annotated[str, Form()] = "",
        md_asset_class_id: Annotated[str, Form()] = "",
        md_anlv_code: Annotated[str, Form()] = "",
        md_manager: Annotated[str, Form()] = "",
        md_region: Annotated[str, Form()] = "",
        # -- the R-SEC-SELL composer's own inventory (S4c) ---------------
        fraction: Annotated[str, Form()] = "full",
    ) -> None:
        self.direction = direction if direction == DIRECTION_BUY else DIRECTION_SELL
        self.investment_id = _uuid_or_none(investment_id)
        self.trade_date = _date_or_none(trade_date) or _today()
        self.settlement_date = _date_or_none(settlement_date)
        self.units = _decimal_or_none(units)
        self.price_per_unit = _decimal_or_none(price_per_unit)
        self.gross_amount = _decimal_or_none(gross_amount)
        self.fees = _decimal_or_none(fees)
        self.taxes = _decimal_or_none(taxes)
        self.cash_investment_id = _uuid_or_none(cash_investment_id)
        self.settle_confirmed = settle_confirm is not None
        self.set_inactive = set_inactive is not None
        self.case_id = _uuid_or_none(case_id)
        self.source = _clean(source)
        self.note = _clean(note)
        self.ticket_id = _uuid_or_none(ticket_id)
        self.cash_name = _clean(cash_name)
        self.cash_opening_balance = _decimal_or_none(cash_opening_balance)
        self.creating = flow == FLOW_NEW_INSTRUMENT
        self.secondary_sale = flow == FLOW_SECONDARY_SALE
        # Only ``partial`` means partial. The control offers two values and
        # nothing else, so anything unrecognised — an absent field, a
        # tampered body — reads as the flow's own default rather than as an
        # error: a full disposal is what a secondary sale *is* (MD-17), and
        # defaulting the other way would let a malformed post block a ticket.
        self.partial_sale = fraction == "partial"
        self.step = _step_or_first(step)
        self.currency = _clean(currency)
        self.md_identifier_scheme = _clean(md_identifier_scheme)
        self.md_identifier_value = _clean(md_identifier_value)
        self.md_figi = _clean(md_figi)
        self.md_name = _clean(md_name)
        self.md_investment_type = _clean(md_investment_type)
        self.md_asset_class_id = _clean(md_asset_class_id)
        self.md_anlv_code = _clean(md_anlv_code)
        self.md_manager = _clean(md_manager)
        self.md_region = _clean(md_region)
        self.entered: dict[str, str] = {
            "units": units,
            "price_per_unit": price_per_unit,
            "gross_amount": gross_amount,
            "fees": fees,
            "taxes": taxes,
            "settlement_date": settlement_date,
            "source": source,
            "note": note,
            "cash_name": cash_name,
            "cash_opening_balance": cash_opening_balance,
            "currency": currency,
            "md_identifier_scheme": md_identifier_scheme,
            "md_identifier_value": md_identifier_value,
            "md_figi": md_figi,
            "md_name": md_name,
            "md_investment_type": md_investment_type,
            "md_asset_class_id": md_asset_class_id,
            "md_anlv_code": md_anlv_code,
            "md_manager": md_manager,
            "md_region": md_region,
        }

    def master_data(self, *, currency: str) -> dict[str, Any]:
        """Project the ``md_*`` fields onto the ticket's ``master_data`` payload.

        A **full replacement**, computed fresh on every save. The wizard
        carries every ``md_*`` field as a hidden input on the steps that do
        not show it, so the posted form is always the payload's whole truth
        and there is no merge to get wrong — a merge would also make "clear
        this field" impossible to express.

        Empty fields are *omitted* rather than stored as ``""``:
        :func:`~services.transactions.emission.parse_master_data` refuses a
        present-but-unusable key with the same identifier as an absent one,
        so writing a blank would manufacture the one state that reads as a
        malformed payload instead of an unfinished one.

        ``currency`` is not read from the ``md_*`` inventory. It is the
        ticket's own column (W-4's step-1 fact), passed in here so the
        payload's ``currency`` and the ticket's cannot disagree — the exact
        pair :meth:`~services.transactions.ticket_service.TicketService
        ._require_master_data` compares (F-3).

        Args:
            currency: The ticket currency, already shape-validated.

        Returns:
            The payload, carrying only the keys that say something.
        """
        pairs: dict[str, str | None] = {
            MD_NAME: self.md_name,
            MD_INVESTMENT_TYPE: self.md_investment_type,
            MD_ASSET_CLASS_ID: self.md_asset_class_id,
            MD_CURRENCY: currency,
            MD_ANLV_CODE: self.md_anlv_code,
            MD_IDENTIFIER_SCHEME: self.md_identifier_scheme,
            MD_IDENTIFIER_VALUE: self.md_identifier_value,
            MD_FIGI: self.md_figi,
            MD_MANAGER: self.md_manager,
            MD_REGION: self.md_region,
        }
        return {key: value for key, value in pairs.items() if value}


def _empty_form() -> _ComposerForm:
    """Return the composer's opening state — every field at its default.

    The ``GET`` order-form render needs a form object and has no request body
    to build one from. Constructing it here rather than writing a second,
    hand-listed "initial state" is what keeps the empty composer's disabled
    actions and placeholder ledger block the *same* answer the recalculation
    endpoint gives, from the same code.
    """
    return _ComposerForm()


def _transient_ticket(
    *,
    session: SessionDTO,
    direction: str,
    investment_id: UUID | None,
    cash_investment_id: UUID | None,
    currency: str,
    trade_date: _date,
    settlement_date: _date | None,
    units: Decimal | None,
    price_per_unit: Decimal | None,
    fees: Decimal | None,
    taxes: Decimal | None,
    set_inactive: bool,
    case_id: UUID | None,
    source: str | None,
    note: str | None,
    kind: str = KIND_ORDER,
    gross_amount: Decimal | None = None,
) -> TradeTicketDTO:
    """Build the never-persisted ticket the derivations run against.

    This DTO exists to be handed to :meth:`~services.transactions
    .ticket_service.TicketService.preview` and to the pure emission helpers,
    and is **never written**. Its ``id`` and ``ticket_number`` are therefore
    placeholders: MD-2 allocates the real number on the first explicit
    gesture (P-2), so a composer that has not been saved has no number to
    show and must not burn one to ask a question.

    Unparseable or absent numeric fields arrive as ``None`` and stay ``None``
    — every derivation beneath here is already ``None``-guarded, so a sparse
    ticket previews quietly instead of complaining about fields the user has
    not reached yet.

    Args:
        session: The authenticated session; supplies tenant and user.
        direction: ``buy`` or ``sell``.
        investment_id: The traded instrument, once one has been picked and
            resolved against the picker's own eligibility rules.
        cash_investment_id: The confirmed settlement position, or ``None``.
        currency: The investment's currency (MD-8: derived, never entered);
            empty while no investment is picked.
        trade_date: The execution date; both legs book on it (MD-4).
        settlement_date: Recorded only, informational in v1 (MD-4).
        units: Unsigned quantity; the sign is applied at emission.
        price_per_unit: The execution price.
        fees: Transaction costs, optional.
        taxes: Taxes split out of fees, optional.
        set_inactive: The MD-7 full-disposal choice.
        case_id: The optional linked case (the Provenance block).
        source: Free-text provenance, optional.
        note: Why the trade was made, optional.
        kind: Which flow's ticket this is. Defaults to ``order``, so every
            caller that predates S4c is unchanged; the R-SEC-SELL composer
            passes ``secondary``, and the kind is what decides which
            derivations :meth:`~services.transactions.ticket_service
            .TicketService.preview` runs at all.
        gross_amount: A *stated* consideration, for the flows that have one.
            ``None`` on the order path, where the gross is derived from
            units and price rather than entered.

    Returns:
        A complete-looking :class:`TradeTicketDTO` that no repository has
        seen and none will.
    """
    stamp = _now()
    return TradeTicketDTO(
        id=uuid4(),
        tenant_id=session.tenant_id,
        ticket_number=0,
        kind=kind,
        direction=direction,
        status=STATUS_DRAFT,
        investment_id=investment_id,
        cash_investment_id=cash_investment_id,
        trade_date=trade_date,
        settlement_date=settlement_date,
        units=units,
        price_per_unit=price_per_unit,
        gross_amount=gross_amount,
        fees=fees,
        taxes=taxes,
        net_amount=None,
        currency=currency,
        commitment_amount=None,
        master_data=None,
        set_inactive=set_inactive,
        note=note,
        source=source,
        cancel_reason=None,
        case_id=case_id,
        proposed_by=None,
        proposed_at=None,
        approved_by=None,
        approved_at=None,
        booked_by=None,
        booked_at=None,
        cancelled_at=None,
        created_by=session.user_id,
        created_at=stamp,
        updated_at=stamp,
    )


# ---------------------------------------------------------------------------
# Presentation helpers
#
# M-1's number formatting, stated once: money to two decimals with thousands
# separators, unit quantities to four, and the typographic minus (U+2212) the
# mockup uses rather than a hyphen.
# ---------------------------------------------------------------------------

_MINUS: str = "−"


def _money(value: Decimal) -> str:
    """Format an amount the way M-1 does: grouped, two decimals, sign kept."""
    return f"{_MINUS if value < 0 else ''}{abs(value):,.2f}"


def _signed_money(value: Decimal) -> str:
    """Format an amount with an explicit ``+`` / ``−`` sign (M-1's ``signed``).

    The magnitude is formatted from ``abs``: :func:`_money` carries its own
    sign, and composing the two would print it twice.
    """
    return f"{'+' if value >= 0 else _MINUS}{abs(value):,.2f}"


def _units(value: Decimal) -> str:
    """Format a unit quantity: grouped, four decimals, sign kept."""
    return f"{_MINUS if value < 0 else ''}{abs(value):,.4f}"


def _signed_units(value: Decimal) -> str:
    """Format a unit quantity with an explicit sign, from ``abs``."""
    return f"{'+' if value >= 0 else _MINUS}{abs(value):,.4f}"


def _is_pickable(investment: InvestmentDTO) -> bool:
    """Is this investment offerable in the order composer's picker?

    The M-1 hint states the rule in one line — "Unitised and active
    investments only." — and cash is excluded on top of it: a cash position
    is the thing an order *settles against*, never the thing it trades. The
    filter runs in the route because no repository method combines the three
    predicates and inventing one for a single caller would be the wrong
    shape (T-4 verify-first §2).
    """
    return (
        investment.is_active
        and investment.valuation_mode == VALUATION_MODE_UNITISED
        and investment.investment_type != CASH_TYPE
    )


def _is_reported_pickable(investment: InvestmentDTO) -> bool:
    """Is this investment offerable in the R-SEC-SELL composer's picker?

    M-3's hint states the rule in one line — "Reported and active
    investments only." — and it is :func:`_is_pickable` with the valuation
    mode the other way round, because that is exactly what the two flows
    differ in: an order deals in units and a secondary sale disposes of a
    statement-valued stake (D-Q, :data:`~services.transactions
    .ticket_service._REQUIRED_VALUATION_MODE`). Cash is excluded on both for
    the same reason — a cash position is what a ticket settles *against*.

    The two predicates stay separate functions rather than one with a mode
    argument: a picker's eligibility is a sentence the surface shows the
    user, and each of these has its own.
    """
    return (
        investment.is_active
        and investment.valuation_mode == VALUATION_MODE_REPORTED
        and investment.investment_type != CASH_TYPE
    )


def _project_leg(units: Decimal, price: Decimal, txn_type: str, name: str) -> dict[str, Any]:
    """Shape one :class:`~services.transactions.emission.LegSpec` for the template.

    Only fields the ``LegSpec`` actually carries are read. It has no
    ``trade_date`` by design — both legs book on the ticket's trade date
    (MD-4), so the leg does not restate it — and the block's copy therefore
    does not mention one.
    """
    return {
        "txn_type": txn_type,
        "units": _signed_units(units),
        "price": _units(price),
        "name": name,
    }


# ---------------------------------------------------------------------------
# Resolving what the browser posted
#
# Both ids on this form arrive from the client and are therefore never
# trusted. These two functions are the only readings of them, so the read
# surface and the write gestures cannot disagree about which investment a
# ticket names or which cash rows may settle it.
# ---------------------------------------------------------------------------


async def _resolve_investment(
    investments: InvestmentRepository,
    investment_id: UUID | None,
    *,
    pickable: Callable[[InvestmentDTO], bool] = _is_pickable,
) -> InvestmentDTO | None:
    """Return the posted investment, but only if it is one the picker offers.

    Tenant visibility (RLS) and the picker's own predicate both have to
    hold. A failure is ``None`` rather than an error: a stale form or a
    foreign id is a field that says nothing, and reporting it would leak the
    existence of rows the tenant cannot see.

    Args:
        investments: The tenant-scoped repository.
        investment_id: The posted id, unverified.
        pickable: Which picker's eligibility to apply — :func:`_is_pickable`
            for the order composer, :func:`_is_reported_pickable` for the
            secondary one (S4c). A parameter rather than a second function,
            so the "verified before use" rule is written once and the two
            surfaces cannot come to enforce it differently.
    """
    if investment_id is None:
        return None
    found = await investments.get_by_id(investment_id)
    return found if found is not None and pickable(found) else None


async def _resolve_traded(
    investments: InvestmentRepository,
    investment_id: UUID | None,
) -> InvestmentDTO | None:
    """Return the posted investment as a *gesture* may name it.

    Deliberately laxer than :func:`_resolve_investment`, and the difference
    is a division of labour rather than an oversight. A gesture needs exactly
    one fact from the row — the currency, which MD-8 derives here and never
    takes from the client — and the two remaining halves of
    :func:`_is_pickable` are refusals the *service* already owns and states
    better: a deactivated investment raises D-P's sentence and a
    statement-valued one D-Q's, each naming its own remedy. Filtering them
    out here would replace those sentences with silence, or with a worse
    sentence written in this module.

    What does not go through is a **cash position**. That one is this
    surface's own rule — a cash row is what an order settles against, never
    what it trades — and the service has no equivalent guard, so dropping it
    here would let a composer trade the settlement account.
    """
    if investment_id is None:
        return None
    found = await investments.get_by_id(investment_id)
    return found if found is not None and found.investment_type != CASH_TYPE else None


async def _cash_in_currency(
    investments: InvestmentRepository,
    currency: str | None,
) -> list[InvestmentDTO]:
    """Return every cash row in ``currency``, active or not.

    Unfiltered on purpose: the answer needs both halves. The *active* rows
    are what may settle a ticket, and the difference between "no row in this
    currency at all" and "rows exist but every one is retired" is what
    decides whether the surface offers to create one (operator decision
    D-F) — and, in :func:`post_cash_position`, whether it accepts the
    creation.

    Keyed on the **currency** rather than on the traded investment (S4b): the
    settlement question is "what can settle in this currency", and a U-NEW
    ticket asks it with no investment row to read one off (MD-12). Every
    caller with an investment in hand passes ``investment.currency``, so the
    U-BUY / U-SELL answer is unchanged — MD-8 still derives it, one step
    earlier.
    """
    if not currency:
        return []
    return [row for row in await investments.list_by_type(CASH_TYPE) if row.currency == currency]


# ---------------------------------------------------------------------------
# The derived surface
# ---------------------------------------------------------------------------


async def _derived_context(
    db: AsyncSession,
    *,
    session: SessionDTO,
    direction: str,
    investment_id: UUID | None,
    trade_date: _date,
    settlement_date: _date | None,
    units: Decimal | None,
    price_per_unit: Decimal | None,
    fees: Decimal | None,
    taxes: Decimal | None,
    cash_investment_id: UUID | None,
    settle_confirmed: bool,
    set_inactive: bool,
    case_id: UUID | None,
    source: str | None,
    note: str | None,
    ticket_status: str | None = None,
    override_warnings: TicketWarnings | None = None,
    creating: _Creating | None = None,
    kind: str = KIND_ORDER,
    gross_amount: Decimal | None = None,
    partial_sale: bool = False,
) -> dict[str, Any]:
    """Derive every element the composer shows, from one transient ticket.

    The single read path behind every composer on this surface: the order
    form's first render, every keystroke afterwards, the wizard's Order step
    and the R-SEC-SELL composer all produce their numbers here, so no two of
    them can disagree about what "derived" means.

    The **kind** is what shapes the answer, and it does so one layer down
    rather than here: ``preview`` runs the derivations the kind admits — an
    oversell check and a price-deviation warning are ``order``-only, and a
    secondary sale's cash effect comes from its stated gross rather than
    from units × price. This function's own branching is therefore about
    what a *reported* stake can be asked (no holding, no last price, but a
    last reported NAV and an unfunded commitment) and not about arithmetic.

    Two ids arrive from the browser and are therefore **verified before
    use**, through :func:`_resolve_investment` and :func:`_cash_in_currency`
    — the same two functions the write gestures use, so what the composer
    shows and what a gesture saves cannot name different rows. Anything that
    fails to resolve is treated as absent rather than as an error.

    Args:
        db: The tenant-scoped session.
        session: The authenticated session.
        direction: ``buy`` or ``sell``.
        investment_id: The posted instrument id, unverified.
        trade_date: The execution date.
        settlement_date: Informational only (MD-4).
        units: Unsigned quantity, or ``None``.
        price_per_unit: Execution price, or ``None``.
        fees: Optional costs.
        taxes: Optional taxes.
        cash_investment_id: The posted settlement position id, unverified.
        settle_confirmed: Whether the MD-3 confirmation is ticked.
        set_inactive: The MD-7 full-disposal choice.
        case_id: The optional linked case.
        source: Optional provenance text.
        note: Optional note.
        ticket_status: The saved ticket's status, or ``None`` while the
            composer is unsaved (MD-2). It narrows *which* gestures are
            offered, never whether the ticket is sound: a proposed ticket is
            no longer editable, so Save as draft and Propose retire while
            Book now stays (:data:`~services.transactions.constants
            .BOOKABLE_STATUSES`).
        override_warnings: The warnings a gesture actually returned, when one
            just ran. The strip then states the answer the *service* gave
            rather than a re-derivation that agrees with it today — and it
            travels the same projection as the preview's, so there is one
            warning renderer on this surface and not two.
        creating: The M-2 wizard's substitutes for the picked investment's
            facts (S4b), or ``None`` on the U-BUY / U-SELL path. **One
            fallback, in one object**: where it is present the currency comes
            off the form rather than off a row, the holding is zero because a
            row that does not exist yet holds nothing, and the instrument leg
            is named from the master data.
        kind: The ticket kind this composer is building (S4c). ``order`` for
            M-1 and M-2, ``secondary`` for R-SEC-SELL — which picks from the
            *reported* rows, states its own proceeds and shows the four
            emission rows in place of the two ledger legs.
        gross_amount: The stated proceeds, on the flows that state them.
            ``None`` on the order path, where the gross is derived.
        partial_sale: MD-18's scope refusal (S4c). The one surface-side
            block on this page: the schema has no fraction column, so the
            service has nothing to refuse and the rule is entirely here.

    Returns:
        The template context for the four derived regions.
    """
    investments = InvestmentRepository(db)
    ledger_rows = PositionTransactionRepository(db)
    prices = InstrumentPriceRepository(db)
    service = _build_ticket_service(db)
    secondary = kind == KIND_SECONDARY

    # -- the traded instrument, and what is held on the trade date ----------
    #
    # The creating path has neither, by construction (MD-12): there is no row
    # to resolve and nothing is held on a row that does not exist yet, which
    # is why M-2's context strip states the holding as a flat 0.0000 rather
    # than as an unknown.
    #
    # The secondary path resolves against the *reported* picker and computes
    # no holding at all: a statement-valued stake has no units by definition
    # (ADR-0097 §1), so a zero here would be a number where there is none.
    investment = (
        await _resolve_investment(
            investments,
            investment_id,
            pickable=_is_reported_pickable if secondary else _is_pickable,
        )
        if creating is None
        else None
    )
    currency = (
        investment.currency
        if investment is not None
        else (creating.currency if creating is not None else "")
    )

    holding: Decimal | None = None
    if investment is not None and not secondary:
        holding = holdings_as_of(await ledger_rows.list_for_investment(investment.id), trade_date)
    elif creating is not None:
        holding = Decimal(0)

    # -- settlement candidates (MD-3, and the D-F split) --------------------
    in_currency = await _cash_in_currency(investments, currency)
    active_cash = [row for row in in_currency if row.is_active]
    selected_cash = next((row for row in active_cash if row.id == cash_investment_id), None)

    ticket = _transient_ticket(
        session=session,
        direction=direction,
        investment_id=investment.id if investment is not None else None,
        cash_investment_id=selected_cash.id if selected_cash is not None else None,
        currency=currency,
        trade_date=trade_date,
        settlement_date=settlement_date,
        units=units,
        price_per_unit=price_per_unit,
        fees=fees,
        taxes=taxes,
        set_inactive=set_inactive,
        case_id=case_id,
        source=source,
        note=note,
        kind=kind,
        gross_amount=gross_amount,
    )

    preview: TicketPreview = await service.preview(ticket, now=_now(), today=_today())

    # -- amounts: one arithmetic, called twice ------------------------------
    #
    # The net is the service's own ``cash_effect``; the gross is the same
    # derivation with fees and taxes withheld. Neither is ``units × price``
    # in a template.
    net = preview.cash_effect
    # The gross is derived where there is something to derive it from, and
    # *stated* where there is not: a reported stake has no units and no price,
    # so R-SEC-SELL's proceeds are the operator's own figure and the second
    # `derive_cash_effect` call would have nothing to compute (S4c).
    gross = (
        gross_amount
        if secondary
        else derive_cash_effect(
            direction=direction,
            units=units,
            price_per_unit=price_per_unit,
        )
    )

    # -- last known price ---------------------------------------------------
    #
    # When the deviation warning fired, the reference it measured against is
    # the price to show: two reads of the same series could otherwise
    # disagree. Otherwise the same repository call and the same pure
    # ``nearest_price`` the service uses — never a second arithmetic.
    reference_price: Decimal | None = None
    reference_date: _date | None = None
    deviation = next(
        (w for w in preview.warnings.warnings if w.identifier == WARNING_PRICE_DEVIATION),
        None,
    )
    if deviation is not None:
        reference_price = cast(Decimal, deviation.data["reference_price"])
        reference_date = cast(_date, deviation.data["reference_date"])
    elif investment is not None and not secondary:
        point = nearest_price(await prices.list_by_investment(investment.id), trade_date)
        if point is not None:
            reference_price = point.price
            reference_date = point.as_of_date

    # -- projected balances -------------------------------------------------
    #
    # One call answers for every candidate row: the cash leg's magnitude and
    # sign are the ticket's, not the position's, so substituting a different
    # candidate would change only which row the units land on.
    #
    # What each flow needs before its consequences can be stated: two entered
    # inputs on the unit paths, one stated amount on the secondary one, where
    # there are no units to enter (S4c). One local, used by both the balance
    # projection and the emission preview, so the two cannot come to disagree
    # about when a flow is answerable.
    amounts_stated = (
        gross is not None if secondary else (units is not None and price_per_unit is not None)
    )
    projected: Decimal | None = None
    if (
        (investment is not None or creating is not None)
        and amounts_stated
        and net is not None
        and active_cash
    ):
        leg = cash_leg(replace(ticket, cash_investment_id=active_cash[0].id), cash_effect=net)
        projected = leg.units if leg is not None else Decimal(0)

    candidates: list[dict[str, Any]] = []
    for row in active_cash:
        balance = holdings_as_of(await ledger_rows.list_for_investment(row.id), trade_date)
        after = balance + projected if projected is not None else None
        candidates.append(
            {
                "id": str(row.id),
                "name": row.name,
                "currency": row.currency,
                "balance": _money(balance),
                "balance_negative": balance < 0,
                "after": _money(after) if after is not None else None,
                "after_negative": after is not None and after < 0,
                "selected": selected_cash is not None and row.id == selected_cash.id,
            }
        )

    # -- ledger effect (D-3): a placeholder until the four inputs are in ----
    #
    # Two shapes, one number. On the U-BUY / U-SELL path both legs come out
    # of `order_legs`, the pure function the booking runs. On the creating
    # path that function refuses by design — it asserts an `investment_id`
    # the ticket cannot have yet (MD-12) — so the *settlement* leg is taken
    # from `cash_leg`, which the emission calls through `order_legs` anyway
    # and which needs no traded row, and the instrument leg is stated from
    # the master data at the flow's constant `buy` (D-AF). That constant is
    # not arithmetic: MD-14 fixes the direction, so there is nothing here to
    # derive and nothing that can drift from `order_legs`' sign convention.
    legs: list[dict[str, Any]] = []
    priced = selected_cash is not None and amounts_stated and net is not None
    if investment is not None and priced and not secondary:
        instrument_leg, settlement_leg = order_legs(ticket, cash_effect=cast(Decimal, net))
        legs.append(
            _project_leg(
                instrument_leg.units,
                instrument_leg.price_per_unit,
                instrument_leg.txn_type,
                investment.name,
            )
        )
        if settlement_leg is not None:
            legs.append(
                _project_leg(
                    settlement_leg.units,
                    settlement_leg.price_per_unit,
                    settlement_leg.txn_type,
                    selected_cash.name if selected_cash is not None else "",
                )
            )
    elif creating is not None and priced and creating.name:
        legs.append(
            _project_leg(
                cast(Decimal, units),
                cast(Decimal, price_per_unit),
                DIRECTION_BUY,
                creating.name,
            )
        )
        settlement_leg = cash_leg(ticket, cash_effect=cast(Decimal, net))
        if settlement_leg is not None:
            legs.append(
                _project_leg(
                    settlement_leg.units,
                    settlement_leg.price_per_unit,
                    settlement_leg.txn_type,
                    selected_cash.name if selected_cash is not None else "",
                )
            )

    # -- the reported stake's own facts (M-3's context strip, S4c) ---------
    #
    # Four values a *statement-valued* position has and a unit-dealt one does
    # not. The unfunded commitment is `pacing_rows.unfunded_commitment`, the
    # same public helper the Planning Desk's pacing rows state — one formula
    # for `commitment − called`, not a third copy of it — fed by the same
    # batched loader beside it.
    last_nav = (
        await InvestmentNavRepository(db).get_latest_actual(investment.id)
        if secondary and investment is not None
        else None
    )
    unfunded: Decimal | None = None
    if secondary and investment is not None:
        called = await load_called_amounts(
            cashflows=InvestmentCashflowRepository(db),
            investment_ids=[investment.id],
        )
        unfunded = unfunded_commitment(investment, called.get(investment.id))

    # -- MD-20's context row: proceeds against the last reported NAV --------
    #
    # An *info* row and never a warning. A secondary that changed hands below
    # the last statement is ordinary economics, so the surface states the
    # distance and says nothing about it; the sign carries the whole meaning,
    # which is why this is the signed twin of the deviation ratio and not the
    # ratio the price warning thresholds against.
    vs_nav: str | None = None
    if last_nav is not None and net is not None:
        ratio = signed_deviation_ratio(value=net, reference=last_nav.nav_value)
        if ratio is not None:
            vs_nav = f"{ratio * 100:+,.1f} %".replace("-", _MINUS)

    # -- what booking will emit (M-3: "Emitted together, or not at all") ----
    #
    # Three stated facts of the flow and one derived leg. The first three are
    # not arithmetic: MD-17 makes a secondary sale a full disposal, so the
    # NAV write and the deactivation are the flow's definition rather than
    # options on it (D-S) — the same D-AF reasoning that lets the creating
    # path state its instrument leg from a constant. The fourth is
    # `cash_leg`, the pure function the booking itself runs.
    #
    # A separate context key rather than more entries in `legs`: that list's
    # dict shape is `_project_leg`'s — units and a price — and three of these
    # four rows have neither.
    effect_rows: list[dict[str, Any]] = []
    if secondary and investment is not None and priced and net is not None:
        effect_rows = [
            {
                "type": "flow",
                "what": investment.name,
                "detail": "distribution · actual",
                "amount": f"{_signed_money(net)} {currency}",
            },
            {
                "type": "nav",
                "what": "NAV set to zero at trade date",
                "detail": "manual origin",
                "amount": f"{_money(Decimal(0))} {currency}",
            },
            {
                "type": "status",
                "what": "Investment set inactive",
                "detail": "full disposal",
                "amount": None,
            },
        ]
        settlement_leg = cash_leg(ticket, cash_effect=net)
        if settlement_leg is not None:
            effect_rows.append(
                {
                    "type": settlement_leg.txn_type,
                    "what": selected_cash.name if selected_cash is not None else "",
                    "detail": f"@ {_units(settlement_leg.price_per_unit)}",
                    "amount": f"{_signed_units(settlement_leg.units)} units",
                }
            )

    messages = _project_messages(
        blocks=preview.blocks,
        warnings=(preview.warnings if override_warnings is None else override_warnings).warnings,
        currency=currency,
        holding=holding,
        price_per_unit=price_per_unit,
        selected_cash=selected_cash,
        partial_sale=partial_sale,
    )

    # -- gating (T-1 D-2 surface mapping, MD-3) -----------------------------
    #
    # "Complete" is per flow, because the flows ask for different things: a
    # unit order needs a positive quantity and a positive price, a secondary
    # sale needs a positive stated consideration and has no units at all.
    complete = (
        (investment is not None and gross is not None and gross > 0)
        if secondary
        else (
            (investment is not None or creating is not None)
            and units is not None
            and units > 0
            and price_per_unit is not None
            and price_per_unit > 0
        )
    )
    settled = selected_cash is not None and settle_confirmed
    # MD-18's refusal joins the service's own blocks rather than standing
    # beside them: the schema has no fraction column (decision record §2.7),
    # so `preview` has nothing to refuse and
    # :data:`~services.transactions.constants.BLOCK_PARTIAL_SECONDARY_SALE`
    # exists precisely so the surface and the service speak one vocabulary
    # about a rule only the surface can enforce.
    blocked = bool(preview.blocks) or partial_sale
    # MD-21's finish gate, folded into the one gating expression rather than
    # added as a second one beside it (S4b). It withholds Propose and Book
    # now — never Save as draft, which MD-11 lets dangle — and only on the
    # creating path, where the AnlV category is being decided for a row that
    # does not exist yet. The service's `missing_anlv` block stays the
    # backstop for the race this cannot see.
    anlv_gate = creating is not None and not creating.anlv_set
    actions_enabled = complete and settled and not blocked and not anlv_gate
    # A saved ticket that has left ``draft`` is a record, not a form
    # (``TradeTicketRepository.update_draft``), so the two editing gestures
    # retire with the status while Book now survives to the stations
    # BOOKABLE_STATUSES names.
    editable = ticket_status is None or ticket_status == STATUS_DRAFT
    return {
        "direction": direction,
        # M-2 heads the wizard with the flow's own name rather than with the
        # instrument's: until the last step there is no instrument to name.
        "title": (
            "Buy a new instrument"
            if creating is not None
            else (
                "Sell a stake"
                if secondary
                else ("Sell units" if direction == DIRECTION_SELL else "Buy units")
            )
            + (f" · {investment.name}" if investment is not None else "")
        ),
        "investment": investment,
        "currency": currency,
        "holding": _units(holding) if holding is not None else None,
        "reference_price": _units(reference_price) if reference_price is not None else None,
        "reference_date": reference_date,
        "gross": _money(gross) if gross is not None else None,
        "fees": _money(fees) if fees is not None else None,
        "taxes": _money(taxes) if taxes is not None else None,
        "net": _signed_money(net) if net is not None else None,
        "net_label": "Net proceeds" if direction == DIRECTION_SELL else "Net cost",
        "formula": (
            f"{_units(units)} units × {_units(price_per_unit)}"
            if units is not None and price_per_unit is not None
            else None
        ),
        "legs": legs,
        "effect_rows": effect_rows,
        # M-3's four context items. Every one is `None`-safe: a stake with no
        # statement yet, or none the book states a commitment for, renders a
        # dash rather than an invented figure.
        "last_nav": _money(last_nav.nav_value) if last_nav is not None else None,
        "last_nav_currency": last_nav.currency if last_nav is not None else None,
        "last_nav_date": last_nav.as_of_date if last_nav is not None else None,
        "unfunded": _money(unfunded) if unfunded is not None else None,
        "vintage_year": investment.vintage_year if investment is not None else None,
        "valuation_mode": investment.valuation_mode if investment is not None else None,
        "vs_nav": vs_nav,
        "partial_sale": partial_sale,
        "candidates": candidates,
        # The D-F split, decided above and handed to the template as two
        # exclusive booleans so no rule is restated in Jinja.
        "offer_cash_creation": bool(currency) and not in_currency,
        "inactive_cash_only": bool(currency) and bool(in_currency) and not active_cash,
        # Which "nothing to settle against yet" sentence the panel shows: the
        # creating path has a currency and no investment, so it asks for the
        # currency; every other path derives the currency from a picked row
        # (MD-8) and asks for that. Keyed on where the currency comes from
        # rather than on `flow`, which S4c made a third value of.
        "currency_from_form": creating is not None,
        "settle_confirmed": settle_confirmed,
        "set_inactive": set_inactive,
        "full_disposal": (
            direction == DIRECTION_SELL
            and holding is not None
            and units is not None
            and units == holding
        ),
        "messages": messages,
        "instrument_name": investment.name
        if investment is not None
        else (creating.name if creating is not None else None),
        "actions_enabled": actions_enabled,
        # W-3: a draft may dangle, so Save as draft asks only for what
        # `create_draft` cannot do without — a direction, the investment the
        # currency derives from (MD-8) and a trade date, the last two of
        # which the form always carries. Neither a warning nor a block gates
        # it; only "there is not yet a ticket here" does.
        "anlv_gate": anlv_gate,
        # MD-18 is the one block that reaches Save as draft, and this is the
        # term that lets it: a partial-sale ticket cannot exist in v1 even as
        # a draft, because the schema cannot represent one. Every other block
        # and every warning still leaves the draft gesture alone (W-3).
        "draft_enabled": (
            (investment is not None or creating is not None) and editable and not partial_sale
        ),
        "propose_enabled": actions_enabled and editable,
        "book_enabled": actions_enabled
        and (ticket_status is None or ticket_status in BOOKABLE_STATUSES),
        "hint_key": _hint_key(
            complete=complete,
            has_selection=selected_cash is not None,
            confirmed=settle_confirmed,
            blocked=blocked,
            partial_sale=partial_sale,
        ),
    }


def _project_messages(
    *,
    blocks: tuple[TicketBlock, ...],
    warnings: tuple[TicketWarning, ...],
    currency: str,
    holding: Decimal | None,
    price_per_unit: Decimal | None,
    selected_cash: InvestmentDTO | None,
    partial_sale: bool = False,
) -> list[dict[str, Any]]:
    """Shape the preview's blocks and warnings for the message strip.

    Blocks first, then warnings — M-1's order, and the order that reads
    correctly: what stops the ticket before what merely qualifies it.

    **The route supplies values, never sentences.** MD-9 fixes the wording in
    the mockup and the template lifts it verbatim, so what crosses this
    boundary is an identifier plus the formatted numbers the copy
    interpolates — exactly the split
    :mod:`services.transactions.constants` makes one layer down.

    Only ``oversell`` can appear as a block *from the preview*: it is the
    one block :meth:`~services.transactions.ticket_service.TicketService
    .preview` derives (the others fire on the P-2 gestures, or are prevented
    structurally by this composer). A ``missing_price`` block is not
    rendered because the actions are already gated on a price being present.

    ``partial_secondary_sale`` is the exception, and it arrives as an
    argument rather than in ``blocks`` because no service derived it: a
    partial sale is not representable in the schema (MD-18, decision record
    §2.7), so there is nothing for ``preview`` to refuse and the rule lives
    entirely on this surface. It is still rendered as a block, in the
    vocabulary :mod:`services.transactions.constants` reserved for it, so
    the strip has one shape and not two.

    Blocks and warnings arrive as two arguments rather than as one
    :class:`~services.transactions.validation.TicketPreview` because the
    gestures have no preview to hand: :meth:`propose` and :meth:`book` return
    a bare :class:`~services.transactions.validation.TicketWarnings`, and
    routing that through here is what keeps the amber strip one renderer
    instead of two that drift.

    Args:
        blocks: The refusals to render, in detection order.
        warnings: The warnings to render, in detection order.
        currency: The investment's currency, for the copy that names one.
        holding: Units held on the trade date — the oversell sentence needs
            it and the block itself does not carry it.
        price_per_unit: The execution price. The deviation warning carries
            the reference it measured against but not the price it measured,
            and the copy names a side.
        selected_cash: The confirmed settlement position, for the
            negative-cash sentence that names it.
        partial_sale: Whether MD-18's scope refusal stands. Prepended, so it
            reads before anything the preview found — it is the reason the
            ticket cannot be made at all, and the rest are qualifications of
            a ticket that could be.

    Returns:
        One dict per message: ``kind`` (``block`` / ``warning``),
        ``identifier``, and the ``data`` its copy interpolates — named for
        the service DTO field it carries, and never ``values``, which Jinja
        would resolve to ``dict.values`` before ever reaching the key.
    """
    messages: list[dict[str, Any]] = []

    if partial_sale:
        messages.append({"kind": "block", "identifier": BLOCK_PARTIAL_SECONDARY_SALE, "data": {}})

    for block in blocks:
        if block.identifier != BLOCK_OVERSELL:
            continue
        block_units = cast("Decimal | None", block.data.get("units"))
        messages.append(
            {
                "kind": "block",
                "identifier": block.identifier,
                "data": {
                    "units": _units(block_units) if block_units is not None else None,
                    "holding": _units(holding) if holding is not None else None,
                    "trade_date": block.data.get("trade_date"),
                },
            }
        )

    for warning in warnings:
        data: dict[str, Any] = {"currency": currency}
        if warning.identifier == WARNING_PRICE_DEVIATION:
            ratio = cast(Decimal, warning.data["deviation_ratio"])
            reference = cast(Decimal, warning.data["reference_price"])
            data |= {
                "percent": f"{ratio * 100:,.1f}",
                # The ratio is an absolute magnitude; which side of the
                # reference the execution sits on is a comparison, not a
                # second derivation of the deviation.
                "side": (
                    "below"
                    if price_per_unit is not None and price_per_unit < reference
                    else "above"
                ),
                "price": _units(reference),
                "date": warning.data["reference_date"],
            }
        elif warning.identifier == WARNING_NEGATIVE_CASH:
            balance = cast(Decimal, warning.data["resulting_balance"])
            data |= {
                "position": selected_cash.name if selected_cash is not None else None,
                "balance": _signed_money(balance),
                "currency": cast(str, warning.data["currency"]),
            }
        elif warning.identifier == WARNING_NET_NON_POSITIVE:
            data |= {
                "amount": _signed_money(cast(Decimal, warning.data["net_amount"])),
                "currency": cast(str, warning.data["currency"]),
            }
        elif warning.identifier == WARNING_FUTURE_TRADE_DATE:
            data |= {"trade_date": warning.data["trade_date"]}
        messages.append({"kind": "warning", "identifier": warning.identifier, "data": data})

    return messages


def _hint_key(
    *,
    complete: bool,
    has_selection: bool,
    confirmed: bool,
    blocked: bool,
    partial_sale: bool = False,
) -> str:
    """Choose which action hint the composer shows.

    The route decides *which* sentence applies; the template holds the words
    (MD-9). M-1's precedence is kept — missing position, then unconfirmed,
    then blocked — with one case in front of it that the mockup never
    reaches, since M-1 is always fully filled in.

    ``partial_sale`` ranks **first**, ahead of even the incomplete case, and
    that is M-3's own order: its script tests ``ssBlocked`` before
    ``ssNeedsConfirm``, because a partial-sale ticket cannot be created at
    all and telling the operator to fill in a field first would be advice
    about a form that is not going to be accepted whatever they enter.

    Returns:
        One of ``partial_sale`` / ``incomplete`` / ``no_position`` /
        ``unconfirmed`` / ``blocked`` / ``ready``.
    """
    if partial_sale:
        return "partial_sale"
    if not complete:
        return "incomplete"
    if not has_selection:
        return "no_position"
    if not confirmed:
        return "unconfirmed"
    if blocked:
        return "blocked"
    return "ready"


# ---------------------------------------------------------------------------
# Rendering — one composer, however it was reached
# ---------------------------------------------------------------------------


async def _composer_context(
    db: AsyncSession,
    *,
    session: SessionDTO,
    form: _ComposerForm,
    ticket: TradeTicketDTO | None = None,
    error: str | None = None,
    override_warnings: TicketWarnings | None = None,
    secondary: bool = False,
) -> dict[str, Any]:
    """Build a picking composer's context — the opening render and every gesture's.

    One function for every render of both picking surfaces. A gesture that
    succeeds, a gesture that is refused and the first ``GET`` differ in three
    values (the ticket, the red block, whose warnings to show) and in nothing
    else, so writing the assembly once is what keeps a refused Propose from
    quietly showing a different picker or a stale settlement panel than the
    form it refused.

    S4c makes it kind-aware rather than copying it. M-1's composer and M-3's
    R-SEC-SELL ask the same three questions in the same order — which row,
    which settlement position, and what does that mean — and differ in the
    *eligibility* of the picker and in which derivations the kind admits.
    Two values carry both differences, so the near-copy that would have
    drifted in the settlement panel does not exist.

    The wizard keeps its own assembly (:func:`_wizard_context`) because it
    genuinely differs: it has no picker at all, it carries two catalogues,
    and its context strip states facts no row supplies yet.

    Args:
        db: The tenant-scoped session.
        session: The authenticated session.
        form: The parsed body — or :func:`_empty_form`'s opening state.
        ticket: The saved ticket, once one exists (MD-2). ``None`` renders
            the "New ticket · Unsaved" chrome.
        error: A service refusal's own sentence, rendered in the red block
            (operator decision D-5). Never composed here.
        override_warnings: Warnings a gesture returned; see
            :func:`_derived_context`.
        secondary: Whether this is the R-SEC-SELL composer (S4c). It selects
            the picker's eligibility (:func:`_is_reported_pickable`) and the
            ticket kind the derivations run against.

    Returns:
        The template context for ``_order_composer.html`` or
        ``_secondary_sale_composer.html``.
    """
    pickable = _is_reported_pickable if secondary else _is_pickable
    investments = [row for row in await InvestmentRepository(db).list_active() if pickable(row)]
    cases = await CaseRepository(db).list_open()
    derived = await _derived_context(
        db,
        session=session,
        # MD-17: a secondary sale has no direction control and never had one.
        # The constant is the flow's, exactly as MD-14's `buy` is the
        # wizard's, so a posted `buy` is ignored rather than refused.
        direction=DIRECTION_SELL if secondary else form.direction,
        investment_id=form.investment_id,
        trade_date=form.trade_date,
        settlement_date=form.settlement_date,
        units=None if secondary else form.units,
        price_per_unit=None if secondary else form.price_per_unit,
        fees=form.fees,
        taxes=form.taxes,
        cash_investment_id=form.cash_investment_id,
        settle_confirmed=form.settle_confirmed,
        # MD-17 again: the MD-7 checkbox is U-SELL's, and a secondary sale
        # deactivates unconditionally. Reading it here would suggest a choice.
        set_inactive=False if secondary else form.set_inactive,
        case_id=form.case_id,
        source=form.source,
        note=form.note,
        ticket_status=ticket.status if ticket is not None else None,
        override_warnings=override_warnings,
        kind=KIND_SECONDARY if secondary else KIND_ORDER,
        gross_amount=form.gross_amount if secondary else None,
        partial_sale=form.partial_sale if secondary else False,
    )
    return {
        "csrf_token": session.csrf_token,
        "flow": FLOW_SECONDARY_SALE if secondary else "",
        "investments": investments,
        "cases": cases,
        "trade_date": form.trade_date,
        "entered": form.entered,
        "case_id": form.case_id,
        "ticket_id": str(ticket.id) if ticket is not None else None,
        "ticket_number": ticket.ticket_number if ticket is not None else None,
        "ticket_status": ticket.status if ticket is not None else None,
        "error": error,
        "oob": False,
        **derived,
    }


def _composer_template(secondary: bool) -> str:
    """Return the composer partial for a flow — one switch, every render.

    The four gestures and the two opening ``GET``s all choose between the
    same two templates, and a fifth hand-written ternary is how one of them
    would come to render M-1's markup for an M-3 context.
    """
    return "_secondary_sale_composer.html" if secondary else "_order_composer.html"


# ---------------------------------------------------------------------------
# The M-2 wizard (S4b)
# ---------------------------------------------------------------------------


def _resume_step(ticket: TradeTicketDTO | None) -> int:
    """Derive which step a saved wizard ticket reopens at (MD-10).

    **No stored step, and therefore no schema.** The wizard's position is a
    property of what the draft already says, so a ticket that was abandoned
    between two browsers, or advanced by a later slice, reopens where its
    own content puts it rather than where a column remembers it was. The
    predicate is the first step whose facts are incomplete:

    ==== ============================================================
    Step Incomplete when
    ==== ============================================================
    1    the payload carries no ``currency`` — W-4's step-1 fact
    2    the payload lacks any of the D-J columns an ``investments``
         row is ``NOT NULL`` in (``name`` / ``investment_type`` /
         ``asset_class_id``), or carries no ``anlv_code``
    3    ``units``, ``price_per_unit`` or the settlement position is
         unset
    4    otherwise — everything the finish needs is on the ticket
    ==== ============================================================

    Step 2 counts an unset AnlV category as incomplete even though MD-11
    explicitly lets the draft dangle without one. The two are not in tension:
    dangling is what makes the draft *legal*, and step 2 is where the gate is
    answered, so a resumed ticket that cannot finish opens on the field that
    is stopping it. Continue moves past it exactly as it did the first time.

    Step 1's branch is reachable only from outside this wizard — its own trio
    rule (:func:`_ensure_draft`) will not write a creating draft without a
    currency. It is stated anyway so the function is total over any draft S5's
    blotter may hand it, rather than silently answering "2" for a payload
    that has not begun.

    Args:
        ticket: The saved draft, or ``None`` for a wizard opened fresh.

    Returns:
        The step number, ``1`` when there is no ticket yet.
    """
    if ticket is None:
        return 1
    payload: dict[str, Any] = ticket.master_data or {}
    if not payload.get(MD_CURRENCY):
        return 1
    if not all(payload.get(key) for key in (MD_NAME, MD_INVESTMENT_TYPE, MD_ASSET_CLASS_ID)):
        return 2
    if not payload.get(MD_ANLV_CODE):
        return 2
    if ticket.units is None or ticket.price_per_unit is None or ticket.cash_investment_id is None:
        return 3
    return 4


def _plain(value: Decimal | None) -> str:
    """Render a stored amount the way it was typed, or ``""``.

    The ``NUMERIC`` columns carry a fixed scale, so a resumed draft reads back
    ``950.00000000`` for a ``950`` somebody entered. That is the same number,
    but it is not the same *form*, and a resume that showed it would look like
    the surface had rewritten the operator's input.

    ``normalize`` strips the trailing zeros and ``format(..., "f")`` keeps the
    result out of exponent notation — ``Decimal("950").normalize()`` is
    ``9.5E+2``, which a ``type="number"`` input would accept and no operator
    would recognise.
    """
    return "" if value is None else format(value.normalize(), "f")


def _form_from_ticket(ticket: TradeTicketDTO, *, csrf_token: str) -> _ComposerForm:
    """Rebuild the wizard's form state from a saved draft (MD-10's other half).

    The resume GET has no request body, and a wizard rendered from an empty
    one would show the operator a blank form over a ticket that is not blank.
    So the row is read back into the same :class:`_ComposerForm` a POST would
    have produced — every field, including ``entered``'s raw strings, since
    those are what the inputs echo.

    Nothing is interpreted here that the service would interpret differently:
    the payload's keys are read as the strings they are stored as, and
    :func:`~services.transactions.emission.parse_master_data` stays the one
    place they become domain values (D-V).
    """
    payload: dict[str, Any] = ticket.master_data or {}

    def _text(key: str) -> str:
        value = payload.get(key)
        return str(value) if value else ""

    return _ComposerForm(
        direction=ticket.direction,
        trade_date=ticket.trade_date.isoformat(),
        settlement_date=ticket.settlement_date.isoformat() if ticket.settlement_date else "",
        units=_plain(ticket.units),
        price_per_unit=_plain(ticket.price_per_unit),
        fees=_plain(ticket.fees),
        taxes=_plain(ticket.taxes),
        cash_investment_id=(
            str(ticket.cash_investment_id) if ticket.cash_investment_id is not None else ""
        ),
        # A saved settlement position is a confirmed one: the tick is what put
        # it on the ticket (MD-3), so a resume that dropped it would ask the
        # operator to re-answer a question the row already records.
        settle_confirm="1" if ticket.cash_investment_id is not None else None,
        case_id=str(ticket.case_id) if ticket.case_id is not None else "",
        source=ticket.source or "",
        note=ticket.note or "",
        ticket_id=str(ticket.id),
        flow=FLOW_NEW_INSTRUMENT,
        currency=ticket.currency,
        md_identifier_scheme=_text(MD_IDENTIFIER_SCHEME),
        md_identifier_value=_text(MD_IDENTIFIER_VALUE),
        md_figi=_text(MD_FIGI),
        md_name=_text(MD_NAME),
        md_investment_type=_text(MD_INVESTMENT_TYPE),
        md_asset_class_id=_text(MD_ASSET_CLASS_ID),
        md_anlv_code=_text(MD_ANLV_CODE),
        md_manager=_text(MD_MANAGER),
        md_region=_text(MD_REGION),
    )


async def _wizard_context(
    db: AsyncSession,
    *,
    session: SessionDTO,
    form: _ComposerForm,
    ticket: TradeTicketDTO | None,
    step: int,
    error: str | None = None,
    resolved: ResolvedInstrument | None = None,
    resolve_error: str | None = None,
    override_warnings: TicketWarnings | None = None,
) -> dict[str, Any]:
    """Build the context for one wizard step — every render goes through here.

    The wizard's counterpart to :func:`_composer_context`, and the same
    argument for existing: the fresh GET, the resume GET, a Continue, a Back,
    a refused Propose and a resolve all differ in a handful of values and in
    nothing else, so assembling them once is what keeps a refused step from
    quietly showing a different catalogue or a stale settlement panel.

    The catalogues are read on **every** step rather than only on step 2.
    They are two small tenant-scoped selects, and fetching them conditionally
    would make the Classify render depend on which gesture reached it.

    Args:
        db: The tenant-scoped session.
        session: The authenticated session.
        form: The parsed body, or :func:`_form_from_ticket`'s reconstruction.
        ticket: The saved draft once one exists (MD-2), else ``None``.
        step: Which of M-2's four bodies to render.
        error: A service refusal's own sentence (D-5). Never composed here.
        resolved: What OpenFIGI said, on the render that follows a Resolve.
        resolve_error: Why it said nothing, on the render that follows a
            failed one.
        override_warnings: Warnings a gesture returned; see
            :func:`_derived_context`.

    Returns:
        The template context for ``_wizard.html``.
    """
    currency = _validate_currency(form.currency) or ""
    derived = await _derived_context(
        db,
        session=session,
        direction=DIRECTION_BUY,
        investment_id=None,
        trade_date=form.trade_date,
        settlement_date=form.settlement_date,
        units=form.units,
        price_per_unit=form.price_per_unit,
        fees=form.fees,
        taxes=form.taxes,
        cash_investment_id=form.cash_investment_id,
        settle_confirmed=form.settle_confirmed,
        set_inactive=False,
        case_id=form.case_id,
        source=form.source,
        note=form.note,
        ticket_status=ticket.status if ticket is not None else None,
        override_warnings=override_warnings,
        creating=_Creating(
            currency=currency,
            name=form.md_name,
            anlv_set=form.md_anlv_code is not None,
        ),
    )
    asset_classes = await AssetClassRepository(db).list_all()
    anlv_categories = await AnlVCategoryRepository(db).list_all()
    return {
        "csrf_token": session.csrf_token,
        "flow": FLOW_NEW_INSTRUMENT,
        "step": step,
        "steps": _WIZARD_STEPS,
        "schemes": _RESOLVABLE_SCHEMES,
        "investment_types": _CLASSIFIABLE_TYPES,
        "asset_classes": asset_classes,
        "anlv_categories": anlv_categories,
        "cases": await CaseRepository(db).list_open(),
        "trade_date": form.trade_date,
        "entered": form.entered,
        "case_id": form.case_id,
        "ticket_id": str(ticket.id) if ticket is not None else None,
        "ticket_number": ticket.ticket_number if ticket is not None else None,
        "ticket_status": ticket.status if ticket is not None else None,
        "error": error,
        "resolved": resolved,
        "resolve_error": resolve_error,
        "oob": False,
        **derived,
    }


def _render(request: Request, template: str, context: dict[str, Any]) -> HTMLResponse:
    """Render one of this module's partials."""
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request, f"_partials/transactions/{template}", context
        ),
    )


# ---------------------------------------------------------------------------
# The gestures
# ---------------------------------------------------------------------------

#: The service refusals a gesture re-renders rather than raises (D-5).
#:
#: Every one carries an operator-grade sentence that names its own remedy, so
#: the surface shows ``str(exc)`` and invents nothing. They are listed rather
#: than caught as their common :class:`~core.exceptions.ValidationError` base
#: on purpose: the mini-form's own ``ValidationError`` is a *field* error with
#: a different rendering, and one broad except would swallow it into the wrong
#: shape.
_REFUSALS: tuple[type[Exception], ...] = (
    TicketIncomplete,
    NonNegativeHoldingsError,
    CurrencyMismatchError,
    ValuationModeError,
    TicketStateInvalid,
)

#: The refusal for a gesture that arrives before there is a ticket to make.
#:
#: Written in M-1's voice — the mockup has no state for it, because M-1 is
#: always fully filled in and its Save as draft is never reached this early.
#: Registered as a copy gap for the operator's walk.
_DRAFT_MINIMUM: str = (
    "A ticket needs a direction, an investment and a trade date before it can be saved."
)

#: The same refusal for the creating path, which asks for a currency instead.
#:
#: The wizard has no investment to derive a currency from (MD-12), so W-4
#: makes the currency a step-1 fact and this is what stands in the way when it
#: is missing or malformed. Written in M-2's voice and registered as a copy
#: gap: the mockup's step 1 is always filled in, so it never renders one.
_WIZARD_CURRENCY_REQUIRED: str = (
    "A new instrument needs a currency before the draft can be saved — three "
    "letters, ISO 4217 (EUR, USD, CHF)."
)


async def _ensure_draft(
    service: TicketService,
    *,
    session: SessionDTO,
    form: _ComposerForm,
    investment: InvestmentDTO | None,
    cash_investment_id: UUID | None,
    currency: str,
) -> TradeTicketDTO:
    """Create the ticket, or update the one this composer is already editing.

    **MD-2 lives here and nowhere else.** The first explicit gesture — any of
    the three, and the wizard's first Continue — allocates the row and with
    it the tenant-sequential ticket number; a second gesture on the same
    composer updates that row rather than burning another number. Book now on
    a never-saved composer therefore writes exactly the draft Save as draft
    would have written, and then books it, instead of having a creation path
    of its own.

    The field map is the repository's draft whitelist and nothing else, and
    it is **kind-aware** (S4b, extended by S4c) rather than duplicated per
    surface. Three columns of one table, one per flow this strand ships:

    ==================== ==================== ==================== ====================
    Column               U-BUY / U-SELL       U-NEW                R-SEC-SELL
                                              (``creating``)       (``secondary_sale``)
    ==================== ==================== ==================== ====================
    ``kind``             ``order``            ``order`` (D-M)      ``secondary``
    ``direction``        the form's, of two   fixed ``buy`` MD-14  fixed ``sell`` MD-17
    ``investment_id``    the picked row       ``None`` (MD-12)     the reported row
    ``currency``         the row's (MD-8)     the step-1 field     the row's (MD-8)
    ``units`` / price    the form's           the form's           always ``None``
    ``gross_amount``     ``None`` (derived)   ``None`` (derived)   the stated proceeds
    ``set_inactive``     the MD-7 choice      ``False``            ``False`` (MD-17)
    ``master_data``      absent               the full payload     absent
    ==================== ==================== ==================== ====================

    ``kind`` is written on :meth:`~services.transactions.ticket_service
    .TicketService.create_draft` and **never in the update map**: a saved
    ticket's kind is a fact about which flow made it, and a body that could
    change it would let a stale form turn one flow's draft into another's.

    Two columns are constants on the creating path rather than form values.
    ``master_data`` is :meth:`_ComposerForm.master_data`'s full replacement —
    the payload that *is* the ``investments`` row until booking — and
    ``set_inactive`` is forced ``False``, because MD-7's full-disposal choice
    is a sell concept and the wizard offers no control for it; taking it from
    a tampered body would ask the emission to deactivate the row it had just
    created.

    ``set_inactive`` is ``False`` on the secondary path for the opposite
    reason: a secondary sale deactivates the position *unconditionally*
    (MD-17, D-S), and the emission never consults the column. Writing
    ``False`` says what is true — the U-SELL control was not used — rather
    than implying the flow had a choice.

    ``direction`` being a flow constant is the same rule as ``kind``: MD-14
    gives the wizard no direction control and MD-17 gives the secondary sale
    none, so the value is the flow's and a posted opposite is ignored rather
    than refused — the surface never offered the choice, so there is no user
    error to report.

    Args:
        service: The wired ticket service.
        session: The authenticated session; supplies the acting user.
        form: The parsed body.
        investment: The resolved traded investment, or ``None`` on the
            creating path.
        cash_investment_id: The settlement position, already verified against
            the active candidates for this currency, or ``None``.
        currency: The ticket currency — the investment's on the ordinary
            path, the validated step-1 field on the creating one. Resolved by
            :func:`_gesture_context`, so this function has one source for it
            rather than two branches.

    Returns:
        The draft, created or updated.

    Raises:
        TicketNotFound: If ``ticket_id`` names no ticket in this tenant.
        TicketStateInvalid: If it names a ticket that has left ``draft``.
    """
    secondary = form.secondary_sale
    kind = KIND_SECONDARY if secondary else KIND_ORDER
    if secondary:
        direction = DIRECTION_SELL
    elif form.creating:
        direction = DIRECTION_BUY
    else:
        direction = form.direction
    fields: dict[str, Any] = {
        "direction": direction,
        "investment_id": None if form.creating else cast(InvestmentDTO, investment).id,
        "cash_investment_id": cash_investment_id,
        "currency": currency,
        "trade_date": form.trade_date,
        "settlement_date": form.settlement_date,
        "units": None if secondary else form.units,
        "price_per_unit": None if secondary else form.price_per_unit,
        "gross_amount": form.gross_amount if secondary else None,
        "fees": form.fees,
        "taxes": form.taxes,
        "set_inactive": False if (form.creating or secondary) else form.set_inactive,
        "note": form.note,
        "source": form.source,
        "case_id": form.case_id,
    }
    if form.creating:
        fields["master_data"] = form.master_data(currency=currency)
    if form.ticket_id is None:
        return await service.create_draft(
            kind=kind,
            created_by=session.user_id,
            now=_now(),
            **fields,
        )
    return await service.update_draft(form.ticket_id, **fields)


def _scope_refuses(form: _ComposerForm) -> bool:
    """Does MD-18's scope refusal stand on this body, whatever it asks for?

    A partial-sale ticket **cannot exist in v1, not even as a draft** — the
    schema has no fraction column to represent one (decision record §2.7) —
    so the three gestures do not merely re-render disabled here: they decline
    to write. The surface had already disabled all three buttons, so a body
    that reaches this can only have been made by hand.

    It answers ``True`` rather than raising, and the caller re-renders the
    composer with the block already on it (operator decision, P-4a §3.7). No
    sentence is invented for it: the service has no rule to state, and the
    red block the render carries is the same one the operator saw before they
    tampered with the body.
    """
    return form.secondary_sale and form.partial_sale


async def _reload_ticket(
    db: AsyncSession,
    *,
    form: _ComposerForm,
    ticket: TradeTicketDTO | None,
) -> TradeTicketDTO | None:
    """Recover the composer's ticket identity after a refusal.

    A gesture that was refused before :func:`_ensure_draft` returned still
    has a ticket, if the composer was already editing one — proposing twice
    in quick succession is the ordinary way to reach this. Without the
    re-read the refused render would fall back to "New ticket · Unsaved",
    inviting the user to save a second row over the top of the first.
    """
    if ticket is not None or form.ticket_id is None:
        return ticket
    return await TradeTicketRepository(db).get(form.ticket_id)


def _validate_currency(value: str | None) -> str | None:
    """Return ``value`` as a three-letter ISO 4217 code, or ``None``.

    The ``web/routes/investments.py`` shape rule (P-3a flag F-B), restated
    here rather than imported — ``web/routes/`` modules do not import one
    another — with one difference that follows from where it is used. That
    one raises a 400; this one **answers with ``None``**, because the
    currency arrives on a form the wizard re-renders rather than on a JSON
    CRUD body, and the caller turns the absence into
    :data:`_WIZARD_CURRENCY_REQUIRED` with the field still on screen.

    ISO 4217 is a convention here and not a whitelist: only the structural
    shape is checked (ADR-0043 §4), so a resolver pre-fill the operator
    overrides passes on the same terms an ordinary tenant currency does.
    """
    if value is None:
        return None
    cleaned = value.strip().upper()
    return cleaned if len(cleaned) == 3 and cleaned.isalpha() else None


async def _gesture_context(
    db: AsyncSession,
    *,
    session: SessionDTO,
    form: _ComposerForm,
) -> tuple[TicketService, InvestmentDTO | None, UUID | None, str | None]:
    """Re-resolve, server-side, everything a gesture is about to act on.

    The surface gated these already; that is not the point. The form arrived
    over the wire and may say anything, so the investment is re-read
    (:func:`_resolve_traded`) and the settlement position re-checked against
    the active candidates for the ticket's currency. A
    ``cash_investment_id`` that no longer qualifies is dropped to ``None``,
    which the service then refuses in its own words rather than this route
    inventing a sentence for a state the user cannot see.

    The **currency** is resolved here too, and it is the one value whose
    source differs between the two paths: the picked investment's on the
    ordinary path (MD-8), the shape-validated step-1 field on the creating
    one (W-4 / F-B). Returning it rather than recomputing it in each caller
    is what lets :func:`_ensure_draft` take a single ``currency`` argument
    instead of branching on the flow a second time.

    Returns:
        ``(service, investment, cash_investment_id, currency)``. ``currency``
        is ``None`` exactly when the gesture has no ticket to make — no
        investment picked, or a creating path whose currency is missing or
        malformed.
    """
    investments = InvestmentRepository(db)
    investment = None if form.creating else await _resolve_traded(investments, form.investment_id)
    currency = (
        _validate_currency(form.currency)
        if form.creating
        else (investment.currency if investment is not None else None)
    )
    cash_id: UUID | None = None
    if currency is not None and form.cash_investment_id is not None:
        active = [row for row in await _cash_in_currency(investments, currency) if row.is_active]
        if any(row.id == form.cash_investment_id for row in active):
            cash_id = form.cash_investment_id
    return _build_ticket_service(db), investment, cash_id, currency


@router.post(
    "/api/transactions/draft",
    response_class=HTMLResponse,
    dependencies=[Depends(require_role("owner"))],
)
async def post_draft(
    request: Request,
    form: _ComposerForm = Depends(),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Save what is on the composer as a draft (MD-2's first gesture).

    **A draft may dangle** (operator decision W-3, MD-11). This gesture asks
    only for what :meth:`~services.transactions.ticket_service.TicketService
    .create_draft` cannot do without — a direction, the investment the
    currency derives from, and a trade date — and neither a warning nor a
    block withholds it: an oversold, cash-short, future-dated ticket saves
    perfectly well, because ``draft`` is a private workspace and ``proposed``
    is what means "complete and validated" (ADR-0128 §3).

    The gate is re-checked here rather than trusted from the render that
    disabled the button. What comes back is the composer again, now carrying
    the ticket's number in its head and its id in a hidden field, so the next
    gesture updates this row instead of allocating a second one.

    Returns:
        The re-rendered composer.

    Raises:
        HTTPException: 404 if ``ticket_id`` names no ticket this tenant can
            see.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        service, investment, cash_id, currency = await _gesture_context(
            db, session=session, form=form
        )
        ticket: TradeTicketDTO | None = None
        error: str | None = None
        # The one refusal that names a field on a step the operator has
        # already left, and therefore the one that changes where the answer
        # is rendered. Tracked as a flag rather than re-read off `error`:
        # matching on a sentence would make the copy load-bearing.
        currency_missing = currency is None
        if _scope_refuses(form):
            # MD-18. Nothing is written and nothing is said: the composer
            # comes back carrying the block it already carried.
            pass
        elif currency_missing:
            error = _WIZARD_CURRENCY_REQUIRED if form.creating else _DRAFT_MINIMUM
        else:
            try:
                ticket = await _ensure_draft(
                    service,
                    session=session,
                    form=form,
                    investment=investment,
                    cash_investment_id=cash_id,
                    currency=cast(str, currency),
                )
            except TicketNotFound as exc:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
            except _REFUSALS as exc:
                error = str(exc)
        ticket = await _reload_ticket(db, form=form, ticket=ticket)
        if form.creating:
            # Continue is Save-as-draft with a step number on it: the gesture
            # endpoint is reused whole (MC §3) and only the render differs.
            #
            # A missing currency sends the render *back* one step, because
            # that is where the field is — the wizard shows it on Identify
            # and again on Classify, and the operator cannot have passed
            # both without one. Every other refusal is about the step that
            # was just posted, so it renders there: advancing past a
            # sentence the operator has to act on would hide it.
            wizard = await _wizard_context(
                db,
                session=session,
                form=form,
                ticket=ticket,
                step=max(form.step - 1, 1) if currency_missing else form.step,
                error=error,
            )
            return _render(request, "_wizard.html", wizard)
        context = await _composer_context(
            db,
            session=session,
            form=form,
            ticket=ticket,
            error=error,
            secondary=form.secondary_sale,
        )
    return _render(request, _composer_template(form.secondary_sale), context)


@router.post(
    "/api/transactions/propose",
    response_class=HTMLResponse,
    dependencies=[Depends(require_role("owner"))],
)
async def post_propose(
    request: Request,
    form: _ComposerForm = Depends(),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Advance the composer's ticket to ``proposed``, saving it first if need be.

    Draft-if-needed, then propose: the two halves are one gesture because
    MD-2 says the row arrives with the first explicit action, and Propose on
    an unsaved composer is one. Both halves share the request's single
    transaction, so a refused proposal that had just created the draft leaves
    that draft behind — deliberately, since the user's work is in it — while
    a refusal after an *update* leaves the update.

    The strip that comes back is the service's own answer:
    :meth:`~services.transactions.ticket_service.TicketService.propose`
    returns the warnings it collected and those are what render, rather than
    a second derivation that happens to agree with them today.

    Returns:
        The re-rendered composer, headed "Ticket #n · proposed" on success
        and carrying the refusal's own sentence otherwise.

    Raises:
        HTTPException: 404 if ``ticket_id`` names no ticket this tenant can
            see.
    """
    return await _advance(request, form=form, session=session, book=False)


@router.post(
    "/api/transactions/book",
    response_class=HTMLResponse,
    dependencies=[Depends(require_role("owner"))],
)
async def post_book(
    request: Request,
    form: _ComposerForm = Depends(),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Book the composer's ticket, saving it first if need be, and confirm what landed.

    The gesture that changes the book. Everything before it is intent; the
    ledger legs, the linkage rows and the status flip commit together on this
    request's one transaction (ADR-0128 §2), and a refusal anywhere rolls the
    whole thing back to where it started.

    Success replaces the composer with the MD-16 confirmation panel, which
    reads the emitted rows back out of ``trade_ticket_effects`` rather than
    restating what the composer predicted: what the panel lists is what the
    database holds. A negative resulting balance is not a refusal here and
    never will be (MD-5, OP-06 struck) — it books, and the flag notice says
    where the position now stands.

    Returns:
        The confirmation panel on success, the re-rendered composer on a
        refusal.

    Raises:
        HTTPException: 404 if ``ticket_id`` names no ticket this tenant can
            see.
    """
    return await _advance(request, form=form, session=session, book=True)


async def _advance(
    request: Request,
    *,
    form: _ComposerForm,
    session: SessionDTO,
    book: bool,
) -> HTMLResponse:
    """Run the draft-then-transition gesture shared by Propose and Book now.

    The two differ in one call and one success render; everything else — the
    MD-2 draft-if-needed, the server-side re-resolution, the uniform D-5
    refusal — is identical, and writing it twice is how the two would come to
    disagree about which of them saves first.

    Args:
        request: The live request, for the engine and the templates.
        form: The parsed body.
        session: The authenticated session.
        book: True to book, False to propose.

    Returns:
        The confirmation panel, or the re-rendered composer.
    """
    engine = _engine(request)
    confirmation: dict[str, Any] | None = None
    context: dict[str, Any] = {}
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        service, investment, cash_id, currency = await _gesture_context(
            db, session=session, form=form
        )
        ticket: TradeTicketDTO | None = None
        error: str | None = None
        warnings: TicketWarnings | None = None
        if _scope_refuses(form):
            # MD-18, as in `post_draft`: no row, no transition, no sentence.
            pass
        elif currency is None:
            error = _WIZARD_CURRENCY_REQUIRED if form.creating else _DRAFT_MINIMUM
        else:
            try:
                ticket = await _ensure_draft(
                    service,
                    session=session,
                    form=form,
                    investment=investment,
                    cash_investment_id=cash_id,
                    currency=currency,
                )
                # The transition runs inside a SAVEPOINT, and the draft above
                # deliberately does not. A refusal that fires *mid-emission*
                # — R-SEC-SELL writes its distribution before the D-N NAV
                # check can refuse (`emit_secondary_sell`) — would otherwise
                # be caught here and then committed by `tenant_context` on
                # the way out, leaving a phantom cashflow behind. Rolling
                # back to the savepoint undoes exactly the emission, while
                # the draft the same gesture may have just created survives,
                # which is the behaviour this module has always documented:
                # the user's work stays in the draft.
                async with db.begin_nested():
                    if book:
                        ticket, warnings = await service.book(
                            ticket.id, booked_by=session.user_id, now=_now(), today=_today()
                        )
                    else:
                        ticket, warnings = await service.propose(
                            ticket.id, proposed_by=session.user_id, now=_now(), today=_today()
                        )
            except TicketNotFound as exc:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
            except _REFUSALS as exc:
                error = str(exc)

        if book and error is None and ticket is not None and warnings is not None:
            confirmation = await _confirmation_context(db, ticket=ticket, warnings=warnings)
        elif form.creating:
            # A refused Propose or Book on the wizard comes back as the
            # wizard's own Confirm step, carrying the service's sentence
            # (D-5). Nothing was written, or a draft was and stays one.
            context = await _wizard_context(
                db,
                session=session,
                form=form,
                ticket=await _reload_ticket(db, form=form, ticket=ticket),
                step=form.step,
                error=error,
                override_warnings=warnings,
            )
        else:
            context = await _composer_context(
                db,
                session=session,
                form=form,
                ticket=await _reload_ticket(db, form=form, ticket=ticket),
                error=error,
                override_warnings=warnings,
                secondary=form.secondary_sale,
            )
    if confirmation is not None:
        return _render(request, "_order_confirmation.html", confirmation)
    if form.creating:
        return _render(request, "_wizard.html", context)
    return _render(request, _composer_template(form.secondary_sale), context)


# ---------------------------------------------------------------------------
# The confirmation panel (MD-16)
# ---------------------------------------------------------------------------


async def _confirmation_context(
    db: AsyncSession,
    *,
    ticket: TradeTicketDTO,
    warnings: TicketWarnings,
) -> dict[str, Any]:
    """Assemble the panel that says what the booking actually wrote.

    MD-16 asks the reported forms to list every emission row *before* the
    user acts; this is the same inventory afterwards, and for every flow, so
    nothing a booking did is a surprise. The rows are read back through
    ``trade_ticket_effects`` — the machine-readable linkage a reversal walks
    (ADR-0128 §6) — rather than re-derived from the ticket, so the panel is a
    statement about the database rather than a second prediction that happens
    to match.

    Args:
        db: The tenant-scoped session, inside the booking's own transaction.
        ticket: The booked ticket.
        warnings: The warnings the booking carried. Informational by the time
            they arrive here — the booking has happened (MD-5).

    Returns:
        The template context for ``_order_confirmation.html``.
    """
    tickets = TradeTicketRepository(db)
    investments = InvestmentRepository(db)
    investment = (
        await investments.get_by_id(ticket.investment_id)
        if ticket.investment_id is not None
        else None
    )
    cash = (
        await investments.get_by_id(ticket.cash_investment_id)
        if ticket.cash_investment_id is not None
        else None
    )
    messages = _project_messages(
        blocks=(),
        warnings=warnings.warnings,
        currency=ticket.currency,
        holding=None,
        price_per_unit=ticket.price_per_unit,
        selected_cash=cash,
    )
    return {
        # No CSRF token: the panel's only controls are a link and an hx-get.
        "ticket_number": ticket.ticket_number,
        "ticket_status": ticket.status,
        "trade_date": ticket.trade_date,
        "provenance": provenance(ticket),
        "investment": investment,
        "rows": await _effect_rows(
            db, ticket=ticket, effects=await tickets.list_effects(ticket.id)
        ),
        # The one warning the panel restates. The others were answered on the
        # composer before the user pressed the button; this one describes the
        # book as it now stands and outlives the gesture (MD-5's flag notice,
        # whose indicator is S5's).
        "flag_notice": next(
            (m for m in messages if m["identifier"] == WARNING_NEGATIVE_CASH), None
        ),
    }


async def _effect_rows(
    db: AsyncSession,
    *,
    ticket: TradeTicketDTO,
    effects: list[TradeTicketEffectDTO],
) -> list[dict[str, Any]]:
    """Read each emitted row back and shape it for the panel.

    One branch per member of the ``EFFECT_*`` vocabulary
    (:mod:`services.transactions.emission`), so the panel is complete for
    every flow rather than for the order flow that reaches it today: S4b's
    creating flows emit ``investment_update``, and S4c's reported flows emit
    ``cashflow`` and ``nav``.

    A row whose target is **gone** is reported as gone rather than skipped.
    ``trade_ticket_effects.effect_id`` is unconstrained by design (ADR-0128
    §2) — the ledger stays ignorant of the layer above it and the referenced
    row may legitimately have been deleted through the CRUD — and a panel
    that silently dropped it would be claiming the booking wrote less than it
    did.

    Args:
        db: The tenant-scoped session.
        ticket: The booked ticket, for the provenance string.
        effects: Its effects, in emission order.

    Returns:
        One dict per effect: ``kind``, the pre-formatted values its line
        needs, and the ``provenance`` string every emitted row carries.
    """
    ledger = PositionTransactionRepository(db)
    navs = InvestmentNavRepository(db)
    cashflows = InvestmentCashflowRepository(db)
    investments = InvestmentRepository(db)
    names: dict[UUID, str] = {}

    async def _name(investment_id: UUID) -> str:
        if investment_id not in names:
            found = await investments.get_by_id(investment_id)
            names[investment_id] = found.name if found is not None else "—"
        return names[investment_id]

    rows: list[dict[str, Any]] = []
    for effect in effects:
        row: dict[str, Any] = {
            "kind": effect.effect_type,
            "provenance": provenance(ticket),
        }
        if effect.effect_type == EFFECT_POSITION_TXN:
            txn = await ledger.get_by_id(effect.effect_id)
            if txn is not None:
                row |= {
                    "txn_type": txn.txn_type,
                    "name": await _name(txn.investment_id),
                    "units": _signed_units(txn.units),
                    "price": _units(txn.price_per_unit) if txn.price_per_unit else None,
                    "currency": txn.currency,
                    "trade_date": txn.trade_date,
                }
        elif effect.effect_type == EFFECT_NAV:
            nav = await navs.get_by_id(effect.effect_id)
            if nav is not None:
                row |= {
                    "name": await _name(nav.investment_id),
                    "value": _money(nav.nav_value),
                    "currency": nav.currency,
                    "nav_kind": nav.nav_kind,
                    "as_of_date": nav.as_of_date,
                }
        elif effect.effect_type == EFFECT_CASHFLOW:
            flow = await cashflows.get_by_id(effect.effect_id)
            if flow is not None:
                row |= {
                    "name": await _name(flow.investment_id),
                    "amount": _signed_money(flow.amount),
                    "currency": flow.currency,
                    "flow_type": flow.flow_type,
                    "flow_kind": flow.flow_kind,
                    "as_of_date": flow.flow_timestamp.date(),
                }
        elif effect.effect_type == EFFECT_INVESTMENT_UPDATE:
            updated = await investments.get_by_id(effect.effect_id)
            if updated is not None:
                row |= {
                    "name": updated.name,
                    # NULL prior_state is the creation marker (D-I); anything
                    # else is a restatement of a row that already existed.
                    "created": effect.prior_state is None,
                    "is_active": updated.is_active,
                }
        row["missing"] = "name" not in row
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# The MD-3 inline cash-position mini-form
# ---------------------------------------------------------------------------


def _bad_request(message: str, *, field: str | None = None) -> JSONResponse:
    """Render a structured 400 with ``error`` / ``field`` keys.

    The ``web/routes/investments.py`` idiom, restated here rather than
    imported: ``web/routes/`` modules do not import one another, and a
    four-line response shape is not worth a shared module.
    """
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"error": message, "field": field},
    )


#: Mini-form refusals. All three describe a state the offer block does not
#: render in, so none has mockup copy; written in M-1's voice and registered.
_CASH_NO_INVESTMENT: str = "Pick an investment first — a cash position follows its currency."
_CASH_ALREADY_EXISTS: str = (
    "This tenant already holds an active {currency} cash position, so there "
    "is nothing to create here."
)
_CASH_ONLY_RETIRED: str = (
    "Every {currency} cash position has been deactivated. Reactivate the one "
    "this trade settles on rather than opening a second beside it."
)


@router.post(
    "/api/transactions/cash-position",
    # The union return type is not a Pydantic field, and there is no response
    # model to infer: this endpoint answers with rendered markup on success
    # and with the CRUD's structured-400 shape on a field error.
    response_model=None,
    dependencies=[Depends(require_role("owner"))],
)
async def post_cash_position(
    request: Request,
    form: _ComposerForm = Depends(),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse | JSONResponse:
    """Create the cash position this ticket has nothing to settle against (MD-3).

    The third MD-3 state's remedy. PortfoliFLOW never converts on the user's
    behalf, so a ticket in a currency the book holds no cash row for is
    unbookable until a row exists — and opening one is a three-write triple
    (investment, unity price, opening ledger row) that only
    :meth:`~services.investments.investment_service.InvestmentService
    .create_cash_position` composes correctly.

    Two values are **not** taken from the mini-form. The currency is the
    picked investment's (MD-8, as everywhere on this surface), and the
    opening date is the composer's own trade date (operator decision W-1) —
    so the new position's unity price and opening row exist *on the day the
    ticket books*, which is the only date at which the ticket needs them.

    **Creating is not confirming.** The new row comes back as the one-match
    candidate with the confirmation box still unticked: MD-3 asks for one
    deliberate click on every order, and a position created two seconds ago
    is not exempt from it.

    The offer only ever renders in the state where nothing exists in the
    currency, so the guard below is for a body that did not come from that
    render. It answers by naming the state rather than by writing: a second
    active row beside a perfectly good one, or beside a deliberately retired
    one, is not what any of these three states asks for.

    Returns:
        The re-rendered composer, or a structured 400.

    Raises:
        HTTPException: 409 if the name is already taken in this tenant.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        investments = InvestmentRepository(db)
        # MD-8 on one path, W-4 on the other: the currency is the picked
        # investment's, or — when the wizard is creating that investment —
        # the step-1 field. Never the mini-form's own, on either path.
        if form.creating:
            currency = _validate_currency(form.currency)
        else:
            investment = await _resolve_traded(investments, form.investment_id)
            currency = investment.currency if investment is not None else None
        if currency is None:
            return _bad_request(
                _WIZARD_CURRENCY_REQUIRED if form.creating else _CASH_NO_INVESTMENT,
                field="currency" if form.creating else "investment_id",
            )
        existing = await _cash_in_currency(investments, currency)
        if any(row.is_active for row in existing):
            return _bad_request(
                _CASH_ALREADY_EXISTS.format(currency=currency),
                field="cash_investment_id",
            )
        if existing:
            return _bad_request(
                _CASH_ONLY_RETIRED.format(currency=currency),
                field="cash_investment_id",
            )

        try:
            created = await _build_investment_service(db).create_cash_position(
                name=form.cash_name or "",
                currency=currency,
                opening_balance=form.cash_opening_balance or Decimal(0),
                # W-1: the ticket's own trade date, so the position is
                # already open — and priced — on the day the booking lands.
                opening_date=form.trade_date,
                created_by=session.user_id,
            )
        except ValidationError as exc:
            return _bad_request(str(exc), field=exc.field)
        except IntegrityError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(f"Investment with name {form.cash_name!r} already exists in this tenant."),
            ) from exc

        # The new row becomes the selected candidate; the tick does not
        # follow it (MD-3). `settle_confirmed` is left exactly as it arrived.
        form.cash_investment_id = created.id
        if form.creating:
            ticket = (
                await TradeTicketRepository(db).get(form.ticket_id)
                if form.ticket_id is not None
                else None
            )
            # Step 3, stated rather than taken from the body: the offer
            # block renders on the Order step and nowhere else, so that is
            # the step this answer belongs on.
            wizard = await _wizard_context(db, session=session, form=form, ticket=ticket, step=3)
            return _render(request, "_wizard.html", wizard)
        context = await _composer_context(
            db, session=session, form=form, secondary=form.secondary_sale
        )
    return _render(request, _composer_template(form.secondary_sale), context)


# ---------------------------------------------------------------------------
# The read endpoints
# ---------------------------------------------------------------------------


@router.get("/api/transactions/order-form", response_class=HTMLResponse)
async def get_order_form(
    request: Request,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Return the U-BUY / U-SELL composer (M-1), empty and ready to type into.

    The chooser's one live tile swaps this in. Nothing is created: MD-2 puts
    ticket persistence on the first explicit gesture, so opening the composer
    allocates no row and burns no ticket number, and the header says
    "Unsaved" until one of them fires.

    The empty state is :func:`_empty_form`'s, run through the same
    :func:`_composer_context` every gesture uses — so the disabled actions and
    the placeholder ledger block are the server's own answer rather than a
    separately written "initial state" that could come to disagree with it.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        context = await _composer_context(db, session=session, form=_empty_form())
    return _render(request, "_order_composer.html", context)


@router.get("/api/transactions/secondary-sale-form", response_class=HTMLResponse)
async def get_secondary_sale_form(
    request: Request,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Return the R-SEC-SELL composer (M-3), empty and ready to type into.

    The chooser's fifth tile swaps this in, and it is the same read posture
    as :func:`get_order_form`: nothing is created, no ticket number is burnt,
    and the header says "Unsaved" until a gesture fires (MD-2).

    The one thing that differs from the order form is *what the picker
    offers*. A secondary sale disposes of a statement-valued stake, so the
    picker lists the ``reported`` rows and the order composer's lists the
    ``unitised`` ones — the same division D-Q enforces one layer down, stated
    here as an eligibility so the operator never picks a row the service will
    refuse.

    The empty state is :func:`_empty_form`'s, run through the same
    :func:`_composer_context` every gesture uses, so the disabled actions and
    the placeholder emission block are the server's own answer rather than a
    separately written "initial state".
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        context = await _composer_context(db, session=session, form=_empty_form(), secondary=True)
    return _render(request, "_secondary_sale_composer.html", context)


#: What the Identify step says when OpenFIGI knows the identifier is nothing.
#:
#: M-2 draws only the resolved card, so the no-match state has no mockup copy.
#: Written in M-2's voice — it names the two remedies the step actually
#: offers — and registered as a copy gap for the operator's walk.
_RESOLVE_NO_MATCH: str = (
    "No instrument matched {scheme} {value}. Check the identifier, or use "
    "the second card and name the instrument yourself."
)


@router.get("/api/transactions/wizard", response_class=HTMLResponse)
async def get_wizard(
    request: Request,
    ticket_id: str = "",
    step: str = "",
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Open the M-2 new-instrument wizard, fresh or on a saved draft (MD-10).

    Two readings of one address, and the difference is a query parameter
    rather than a second endpoint, because they answer the same question —
    "show me this wizard" — and only differ in whether a ticket exists yet.

    Without ``ticket_id`` this is the chooser's U-NEW tile: step 1, nothing
    written. MD-2 holds here as everywhere on this surface — opening the
    wizard allocates no row and burns no ticket number, and the head reads
    "New ticket · Unsaved" until the first Continue.

    With one it is the **resume** (MD-10: a mid-wizard draft "reopens where
    it stopped"), and this is the URL S5's blotter links a U-NEW row to. The
    step comes from :func:`_resume_step` — the draft's own content — unless
    ``step`` overrides it, which is how Back re-renders the previous step
    without writing anything. A ticket that has left ``draft`` renders with
    the editing gestures already retired, exactly as the composer reads that
    status; nothing here decides it a second time.

    Args:
        request: The live request.
        ticket_id: The draft to reopen, or empty for a fresh wizard.
        step: The step to render, or empty to derive it from the draft.
        session: The authenticated session.

    Returns:
        The wizard at one step.

    Raises:
        HTTPException: 404 if ``ticket_id`` names no ticket this tenant can
            see, or names one that is not this wizard's — a ticket of another
            kind, or one that already carries an investment, is not a U-NEW
            in progress and this surface has nothing to show for it (D-AG).
    """
    engine = _engine(request)
    wanted = _uuid_or_none(ticket_id)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        ticket: TradeTicketDTO | None = None
        if wanted is not None:
            ticket = await TradeTicketRepository(db).get(wanted)
            if ticket is None or ticket.kind != KIND_ORDER or ticket.investment_id is not None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"No new-instrument ticket {wanted} in this tenant.",
                )
        form = (
            _form_from_ticket(ticket, csrf_token=session.csrf_token)
            if ticket is not None
            else _ComposerForm(flow=FLOW_NEW_INSTRUMENT, direction=DIRECTION_BUY)
        )
        asked = _step_or_first(step) if step else _resume_step(ticket)
        context = await _wizard_context(db, session=session, form=form, ticket=ticket, step=asked)
    return _render(request, "_wizard.html", context)


@router.post("/api/transactions/resolve-identifier", response_class=HTMLResponse)
async def post_resolve_identifier(
    request: Request,
    form: _ComposerForm = Depends(),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Resolve the Identify step's identifier through OpenFIGI. **Writes nothing.**

    A POST for the same reason ``recalc`` is one — it carries the whole form
    and takes this surface's uniform CSRF posture — and, like ``recalc``, it
    is not role-gated and touches no row. Neither the FIGI nor the identifier
    pair is persisted anywhere by this call: they ride on the form, reach the
    ticket's ``master_data`` at the next Continue, and become
    ``investment_identifiers`` rows only when the emission creates the
    investment (MD-13, ``emission._write_identifiers``).

    The API key comes from the :class:`~services.investments.credential_resolver
    .CredentialResolver` and never from the environment directly (P-3a flag
    F-G). ``openfigi`` is declared *optional* with ``env_fallback: allowed``,
    so an unconfigured tenant resolves to
    :class:`~services.investments.credential_resolver.NoCredential` rather
    than an error and the call is made keyless at the lower public rate limit
    — the documented v1 posture, not a degraded one.

    **Everything it learns is a pre-fill, not a fact** (operator decision
    W-4′). The FIGI, the name and the currency come back into *editable*
    inputs, and a currency OpenFIGI does not state comes back empty rather
    than as an error: the recorded fixtures do not evidence the field at all
    (P-3a flag F-A), so ``None`` is the normal case here.

    Returns:
        The Identify step, re-rendered with whatever was learned.

    Raises:
        HTTPException: 400 if the scheme is not one OpenFIGI can map — a
            state the select cannot reach, so only a tampered body arrives
            here.
    """
    engine = _engine(request)
    scheme = form.md_identifier_scheme or ""
    value = form.md_identifier_value or ""
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        resolved: ResolvedInstrument | None = None
        resolve_error: str | None = None
        if scheme and value:
            credential = await CredentialResolver(session=db).resolve(
                _OPENFIGI, tenant_id=session.tenant_id, user_id=session.user_id
            )
            api_key = (
                credential.payload.get("api_key")
                if isinstance(credential, ProviderCredential)
                else None
            )
            try:
                resolved = await resolve_instrument(scheme, value, api_key=api_key)
            except IdentifierNotResolvableError:
                resolve_error = _RESOLVE_NO_MATCH.format(scheme=scheme, value=value)
            except ProviderFetchError as exc:
                resolve_error = str(exc)
            except UnsupportedCapabilityError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
                ) from exc

        if resolved is not None:
            # The pre-fill lands on the form before the render, so the
            # editable inputs and the "Resolved" card state one set of values
            # rather than two — and so a Continue posted straight afterwards
            # carries what the operator can see.
            form.md_figi = resolved.figi
            form.md_name = form.md_name or resolved.name
            form.currency = form.currency or _validate_currency(resolved.currency)
            form.entered["currency"] = form.currency or ""
            form.entered["md_figi"] = resolved.figi
            form.entered["md_name"] = form.md_name or ""

        ticket = (
            await TradeTicketRepository(db).get(form.ticket_id)
            if form.ticket_id is not None
            else None
        )
        context = await _wizard_context(
            db,
            session=session,
            form=form,
            ticket=ticket,
            step=1,
            resolved=resolved,
            resolve_error=resolve_error,
        )
    return _render(request, "_wizard.html", context)


@router.get("/api/transactions/chooser", response_class=HTMLResponse)
async def get_chooser(
    request: Request,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Return the MD-1 flow chooser — what Close and Discard go back to.

    Neither control destroys anything. Before a gesture there is nothing to
    destroy (MD-2), and after one the ticket is a saved draft worth keeping,
    which is why the label changes to "Close": cancelling a draft and
    reversing a booking are S5's surfaces and a different gesture entirely.
    """
    return _render(request, "_chooser.html", {"csrf_token": session.csrf_token})


@router.post("/api/transactions/recalc", response_class=HTMLResponse)
async def post_recalc(
    request: Request,
    form: _ComposerForm = Depends(),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Re-derive every element the composer shows. **Writes nothing.**

    A POST because it carries the whole form and takes the uniform CSRF
    posture of every POST on this surface — not because it changes anything.
    No repository write runs on this path, no ticket row is created (MD-2),
    and the transient ticket it builds is discarded when the response is
    rendered. It is deliberately **not** role-gated: it reads what the
    session may already read, and the gestures that do change the book carry
    ``require_role("owner")`` where they land.

    Malformed input is not refused. The endpoint fires on every keystroke,
    where half-typed numbers and empty fields are the normal case; each is
    read as "not said yet" and the surface derives less (operator decision
    D-2). An id the tenant cannot see is treated the same way — see
    :func:`_resolve_investment`.

    Returns:
        The four derived regions: the amounts and settlement panel as the
        swap target, plus the ticket title, the instrument context strip and
        the message-and-actions panel as out-of-band swaps, since M-1
        interleaves them with static inputs and they cannot be one
        contiguous fragment.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        # Which gestures are still on offer depends on the ticket's status,
        # and the status is read from the row rather than taken from the body
        # — the composer keeps only the id, so a tampered form can narrow its
        # own buttons and nothing else.
        ticket = (
            await TradeTicketRepository(db).get(form.ticket_id)
            if form.ticket_id is not None
            else None
        )
        secondary = form.secondary_sale
        derived = await _derived_context(
            db,
            session=session,
            direction=(
                DIRECTION_BUY
                if form.creating
                else (DIRECTION_SELL if secondary else form.direction)
            ),
            investment_id=None if form.creating else form.investment_id,
            trade_date=form.trade_date,
            settlement_date=form.settlement_date,
            units=None if secondary else form.units,
            price_per_unit=None if secondary else form.price_per_unit,
            fees=form.fees,
            taxes=form.taxes,
            cash_investment_id=form.cash_investment_id,
            settle_confirmed=form.settle_confirmed,
            set_inactive=False if (form.creating or secondary) else form.set_inactive,
            case_id=form.case_id,
            source=form.source,
            note=form.note,
            ticket_status=ticket.status if ticket is not None else None,
            creating=(
                _Creating(
                    currency=_validate_currency(form.currency) or "",
                    name=form.md_name,
                    anlv_set=form.md_anlv_code is not None,
                )
                if form.creating
                else None
            ),
            kind=KIND_SECONDARY if secondary else KIND_ORDER,
            gross_amount=form.gross_amount if secondary else None,
            partial_sale=form.partial_sale if secondary else False,
        )

    return _render(
        request,
        (
            "_wizard_recalc.html"
            if form.creating
            else ("_secondary_sale_recalc.html" if secondary else "_order_recalc.html")
        ),
        {
            "csrf_token": session.csrf_token,
            "oob": True,
            "flow": (
                FLOW_NEW_INSTRUMENT if form.creating else (FLOW_SECONDARY_SALE if secondary else "")
            ),
            "step": form.step,
            # The head's ticket number and state pill are not out-of-band
            # regions, so a keystroke never disturbs them; the id travels
            # because the action row's own label depends on it, and `entered`
            # because the MD-3 mini-form sits inside a region this response
            # does replace.
            "ticket_id": str(ticket.id) if ticket is not None else None,
            "entered": form.entered,
            **derived,
        },
    )
