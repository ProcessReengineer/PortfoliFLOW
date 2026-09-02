# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Transactions area web surface — the M-1 order composer (ADR-0128, S4a).

The ninth Area's first working surface: the MD-1 flow chooser, the M-1
order composer for U-BUY / U-SELL against an instrument already on the book,
the recalculation endpoint that keeps every derived element on that form
truthful while the user types, and the three gestures that turn what is on
the form into a ticket.

Reads and writes, kept apart
----------------------------
``order-form``, ``chooser`` and ``recalc`` are reads: they derive, they
render, and they touch no row (MD-2 — opening a composer allocates nothing
and burns no ticket number). ``draft``, ``propose``, ``book`` and
``cash-position`` are the writes, owner-gated and CSRF-checked, and every one
of them re-checks server-side what the surface had already gated: a form is a
suggestion, never a permission. ``web/routes/areas.py`` stays a no-DB shell
render: the chooser is static markup in the area body, and everything that
needs the database sits behind the HTMX endpoints below.

The first explicit gesture allocates the ticket (MD-2), and that rule lives
in exactly one function — :func:`_ensure_draft`. All three gestures go
through it, so "Book now" on a never-saved composer writes the same draft
row that "Save as draft" would have, and then books it.

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
``settle_confirm``, ``set_inactive``, ``case_id``, ``source``, ``note``.

Three names carry no column. ``ticket_id`` is the composer's own memory of
which row it is editing — absent means "not saved yet" (MD-2) — and
``cash_name`` / ``cash_opening_balance`` belong to the MD-3 mini-form, which
rides inside the composer's form because HTML has no nested forms.

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

from dataclasses import replace
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
from services.investments.holdings import holdings_as_of
from services.investments.investment_service import InvestmentService
from services.transactions.constants import (
    BLOCK_OVERSELL,
    BOOKABLE_STATUSES,
    DIRECTION_BUY,
    DIRECTION_SELL,
    KIND_ORDER,
    STATUS_DRAFT,
    WARNING_FUTURE_TRADE_DATE,
    WARNING_NEGATIVE_CASH,
    WARNING_NET_NON_POSITIVE,
    WARNING_PRICE_DEVIATION,
)
from services.transactions.emission import (
    EFFECT_CASHFLOW,
    EFFECT_INVESTMENT_UPDATE,
    EFFECT_NAV,
    EFFECT_POSITION_TXN,
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
)
from web.auth import require_session, verify_csrf
from web.permissions import require_role

router = APIRouter()


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
    ) -> None:
        self.direction = direction if direction == DIRECTION_BUY else DIRECTION_SELL
        self.investment_id = _uuid_or_none(investment_id)
        self.trade_date = _date_or_none(trade_date) or _today()
        self.settlement_date = _date_or_none(settlement_date)
        self.units = _decimal_or_none(units)
        self.price_per_unit = _decimal_or_none(price_per_unit)
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
        self.entered: dict[str, str] = {
            "units": units,
            "price_per_unit": price_per_unit,
            "fees": fees,
            "taxes": taxes,
            "settlement_date": settlement_date,
            "source": source,
            "note": note,
            "cash_name": cash_name,
            "cash_opening_balance": cash_opening_balance,
        }


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

    Returns:
        A complete-looking :class:`TradeTicketDTO` that no repository has
        seen and none will.
    """
    stamp = _now()
    return TradeTicketDTO(
        id=uuid4(),
        tenant_id=session.tenant_id,
        ticket_number=0,
        kind=KIND_ORDER,
        direction=direction,
        status=STATUS_DRAFT,
        investment_id=investment_id,
        cash_investment_id=cash_investment_id,
        trade_date=trade_date,
        settlement_date=settlement_date,
        units=units,
        price_per_unit=price_per_unit,
        gross_amount=None,
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
) -> InvestmentDTO | None:
    """Return the posted investment, but only if it is one the picker offers.

    Tenant visibility (RLS) and :func:`_is_pickable` both have to hold. A
    failure is ``None`` rather than an error: a stale form or a foreign id is
    a field that says nothing, and reporting it would leak the existence of
    rows the tenant cannot see.
    """
    if investment_id is None:
        return None
    found = await investments.get_by_id(investment_id)
    return found if found is not None and _is_pickable(found) else None


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
    investment: InvestmentDTO | None,
) -> list[InvestmentDTO]:
    """Return every cash row in the investment's currency, active or not.

    Unfiltered on purpose: the answer needs both halves. The *active* rows
    are what may settle a ticket, and the difference between "no row in this
    currency at all" and "rows exist but every one is retired" is what
    decides whether the surface offers to create one (operator decision
    D-F) — and, in :func:`post_cash_position`, whether it accepts the
    creation.
    """
    if investment is None:
        return []
    return [
        row
        for row in await investments.list_by_type(CASH_TYPE)
        if row.currency == investment.currency
    ]


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
) -> dict[str, Any]:
    """Derive every element the composer shows, from one transient ticket.

    The single read path behind both endpoints: the order form's first render
    and every keystroke afterwards produce their numbers here, so an empty
    composer and a full one cannot disagree about what "derived" means.

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

    Returns:
        The template context for the four derived regions.
    """
    investments = InvestmentRepository(db)
    ledger_rows = PositionTransactionRepository(db)
    prices = InstrumentPriceRepository(db)
    service = _build_ticket_service(db)

    # -- the traded instrument, and what is held on the trade date ----------
    investment = await _resolve_investment(investments, investment_id)

    holding: Decimal | None = None
    if investment is not None:
        holding = holdings_as_of(await ledger_rows.list_for_investment(investment.id), trade_date)

    # -- settlement candidates (MD-3, and the D-F split) --------------------
    in_currency = await _cash_in_currency(investments, investment)
    active_cash = [row for row in in_currency if row.is_active]
    selected_cash = next((row for row in active_cash if row.id == cash_investment_id), None)

    ticket = _transient_ticket(
        session=session,
        direction=direction,
        investment_id=investment.id if investment is not None else None,
        cash_investment_id=selected_cash.id if selected_cash is not None else None,
        currency=investment.currency if investment is not None else "",
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
    )

    preview: TicketPreview = await service.preview(ticket, now=_now(), today=_today())

    # -- amounts: one arithmetic, called twice ------------------------------
    #
    # The net is the service's own ``cash_effect``; the gross is the same
    # derivation with fees and taxes withheld. Neither is ``units × price``
    # in a template.
    net = preview.cash_effect
    gross = derive_cash_effect(
        direction=direction,
        units=units,
        price_per_unit=price_per_unit,
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
    elif investment is not None:
        point = nearest_price(await prices.list_by_investment(investment.id), trade_date)
        if point is not None:
            reference_price = point.price
            reference_date = point.as_of_date

    # -- projected balances -------------------------------------------------
    #
    # One call answers for every candidate row: the cash leg's magnitude and
    # sign are the ticket's, not the position's, so substituting a different
    # candidate would change only which row the units land on.
    projected: Decimal | None = None
    if (
        investment is not None
        and units is not None
        and price_per_unit is not None
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
    legs: list[dict[str, Any]] = []
    if (
        investment is not None
        and selected_cash is not None
        and units is not None
        and price_per_unit is not None
        and net is not None
    ):
        instrument_leg, settlement_leg = order_legs(ticket, cash_effect=net)
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
                    selected_cash.name,
                )
            )

    currency = investment.currency if investment is not None else ""
    messages = _project_messages(
        blocks=preview.blocks,
        warnings=(preview.warnings if override_warnings is None else override_warnings).warnings,
        currency=currency,
        holding=holding,
        price_per_unit=price_per_unit,
        selected_cash=selected_cash,
    )

    # -- gating (T-1 D-2 surface mapping, MD-3) -----------------------------
    complete = (
        investment is not None
        and units is not None
        and units > 0
        and price_per_unit is not None
        and price_per_unit > 0
    )
    settled = selected_cash is not None and settle_confirmed
    blocked = bool(preview.blocks)
    actions_enabled = complete and settled and not blocked
    # A saved ticket that has left ``draft`` is a record, not a form
    # (``TradeTicketRepository.update_draft``), so the two editing gestures
    # retire with the status while Book now survives to the stations
    # BOOKABLE_STATUSES names.
    editable = ticket_status is None or ticket_status == STATUS_DRAFT
    return {
        "direction": direction,
        "title": ("Sell units" if direction == DIRECTION_SELL else "Buy units")
        + (f" · {investment.name}" if investment is not None else ""),
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
        "candidates": candidates,
        # The D-F split, decided above and handed to the template as two
        # exclusive booleans so no rule is restated in Jinja.
        "offer_cash_creation": investment is not None and not in_currency,
        "inactive_cash_only": investment is not None and bool(in_currency) and not active_cash,
        "settle_confirmed": settle_confirmed,
        "set_inactive": set_inactive,
        "full_disposal": (
            direction == DIRECTION_SELL
            and holding is not None
            and units is not None
            and units == holding
        ),
        "messages": messages,
        "actions_enabled": actions_enabled,
        # W-3: a draft may dangle, so Save as draft asks only for what
        # `create_draft` cannot do without — a direction, the investment the
        # currency derives from (MD-8) and a trade date, the last two of
        # which the form always carries. Neither a warning nor a block gates
        # it; only "there is not yet a ticket here" does.
        "draft_enabled": investment is not None and editable,
        "propose_enabled": actions_enabled and editable,
        "book_enabled": actions_enabled
        and (ticket_status is None or ticket_status in BOOKABLE_STATUSES),
        "hint_key": _hint_key(
            complete=complete,
            has_selection=selected_cash is not None,
            confirmed=settle_confirmed,
            blocked=blocked,
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
) -> list[dict[str, Any]]:
    """Shape the preview's blocks and warnings for the message strip.

    Blocks first, then warnings — M-1's order, and the order that reads
    correctly: what stops the ticket before what merely qualifies it.

    **The route supplies values, never sentences.** MD-9 fixes the wording in
    the mockup and the template lifts it verbatim, so what crosses this
    boundary is an identifier plus the formatted numbers the copy
    interpolates — exactly the split
    :mod:`services.transactions.constants` makes one layer down.

    Only ``oversell`` can appear as a block here: it is the one block
    :meth:`~services.transactions.ticket_service.TicketService.preview`
    derives (the others fire on the P-2 gestures, or are prevented
    structurally by this composer). A ``missing_price`` block is not
    rendered because the actions are already gated on a price being present.

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

    Returns:
        One dict per message: ``kind`` (``block`` / ``warning``),
        ``identifier``, and the ``data`` its copy interpolates — named for
        the service DTO field it carries, and never ``values``, which Jinja
        would resolve to ``dict.values`` before ever reaching the key.
    """
    messages: list[dict[str, Any]] = []

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


def _hint_key(*, complete: bool, has_selection: bool, confirmed: bool, blocked: bool) -> str:
    """Choose which action hint the composer shows.

    The route decides *which* sentence applies; the template holds the words
    (MD-9). M-1's precedence is kept — missing position, then unconfirmed,
    then blocked — with one case in front of it that the mockup never
    reaches, since M-1 is always fully filled in.

    Returns:
        One of ``incomplete`` / ``no_position`` / ``unconfirmed`` /
        ``blocked`` / ``ready``.
    """
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
) -> dict[str, Any]:
    """Build the full composer context — the opening render and every gesture's.

    One function for all five renders. A gesture that succeeds, a gesture
    that is refused and the first ``GET`` differ in three values (the ticket,
    the red block, whose warnings to show) and in nothing else, so writing
    the assembly once is what keeps a refused Propose from quietly showing a
    different picker or a stale settlement panel than the form it refused.

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

    Returns:
        The template context for ``_order_composer.html``.
    """
    investments = [row for row in await InvestmentRepository(db).list_active() if _is_pickable(row)]
    cases = await CaseRepository(db).list_open()
    derived = await _derived_context(
        db,
        session=session,
        direction=form.direction,
        investment_id=form.investment_id,
        trade_date=form.trade_date,
        settlement_date=form.settlement_date,
        units=form.units,
        price_per_unit=form.price_per_unit,
        fees=form.fees,
        taxes=form.taxes,
        cash_investment_id=form.cash_investment_id,
        settle_confirmed=form.settle_confirmed,
        set_inactive=form.set_inactive,
        case_id=form.case_id,
        source=form.source,
        note=form.note,
        ticket_status=ticket.status if ticket is not None else None,
        override_warnings=override_warnings,
    )
    return {
        "csrf_token": session.csrf_token,
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


async def _ensure_draft(
    service: TicketService,
    *,
    session: SessionDTO,
    form: _ComposerForm,
    investment: InvestmentDTO,
    cash_investment_id: UUID | None,
) -> TradeTicketDTO:
    """Create the ticket, or update the one this composer is already editing.

    **MD-2 lives here and nowhere else.** The first explicit gesture — any of
    the three — allocates the row and with it the tenant-sequential ticket
    number; a second gesture on the same composer updates that row rather
    than burning another number. Book now on a never-saved composer therefore
    writes exactly the draft Save as draft would have written, and then books
    it, instead of having a creation path of its own.

    The field map is the repository's draft whitelist and nothing else. Two
    of the columns are *derived* rather than taken from the body: ``currency``
    is the investment's (MD-8 — the client never states it) and ``kind`` is
    fixed at creation, since this composer builds one kind of ticket and the
    whitelist would happily accept another.

    Args:
        service: The wired ticket service.
        session: The authenticated session; supplies the acting user.
        form: The parsed body.
        investment: The resolved traded investment — the currency's source,
            so this is never called without one.
        cash_investment_id: The settlement position, already verified against
            the active candidates for this currency, or ``None``.

    Returns:
        The draft, created or updated.

    Raises:
        TicketNotFound: If ``ticket_id`` names no ticket in this tenant.
        TicketStateInvalid: If it names a ticket that has left ``draft``.
    """
    fields: dict[str, Any] = {
        "direction": form.direction,
        "investment_id": investment.id,
        "cash_investment_id": cash_investment_id,
        "currency": investment.currency,
        "trade_date": form.trade_date,
        "settlement_date": form.settlement_date,
        "units": form.units,
        "price_per_unit": form.price_per_unit,
        "fees": form.fees,
        "taxes": form.taxes,
        "set_inactive": form.set_inactive,
        "note": form.note,
        "source": form.source,
        "case_id": form.case_id,
    }
    if form.ticket_id is None:
        return await service.create_draft(
            kind=KIND_ORDER,
            created_by=session.user_id,
            now=_now(),
            **fields,
        )
    return await service.update_draft(form.ticket_id, **fields)


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


async def _gesture_context(
    db: AsyncSession,
    *,
    session: SessionDTO,
    form: _ComposerForm,
) -> tuple[TicketService, InvestmentDTO | None, UUID | None]:
    """Re-resolve, server-side, everything a gesture is about to act on.

    The surface gated these already; that is not the point. The form arrived
    over the wire and may say anything, so the investment is re-read
    (:func:`_resolve_traded`) and the settlement position re-checked against
    the active candidates for that investment's currency. A
    ``cash_investment_id`` that no longer qualifies is dropped to ``None``,
    which the service then refuses in its own words rather than this route
    inventing a sentence for a state the user cannot see.
    """
    investments = InvestmentRepository(db)
    investment = await _resolve_traded(investments, form.investment_id)
    cash_id: UUID | None = None
    if investment is not None and form.cash_investment_id is not None:
        active = [row for row in await _cash_in_currency(investments, investment) if row.is_active]
        if any(row.id == form.cash_investment_id for row in active):
            cash_id = form.cash_investment_id
    return _build_ticket_service(db), investment, cash_id


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
        service, investment, cash_id = await _gesture_context(db, session=session, form=form)
        ticket: TradeTicketDTO | None = None
        error: str | None = None
        if investment is None:
            error = _DRAFT_MINIMUM
        else:
            try:
                ticket = await _ensure_draft(
                    service,
                    session=session,
                    form=form,
                    investment=investment,
                    cash_investment_id=cash_id,
                )
            except TicketNotFound as exc:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
            except _REFUSALS as exc:
                error = str(exc)
        context = await _composer_context(
            db,
            session=session,
            form=form,
            ticket=await _reload_ticket(db, form=form, ticket=ticket),
            error=error,
        )
    return _render(request, "_order_composer.html", context)


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
        service, investment, cash_id = await _gesture_context(db, session=session, form=form)
        ticket: TradeTicketDTO | None = None
        error: str | None = None
        warnings: TicketWarnings | None = None
        if investment is None:
            error = _DRAFT_MINIMUM
        else:
            try:
                ticket = await _ensure_draft(
                    service,
                    session=session,
                    form=form,
                    investment=investment,
                    cash_investment_id=cash_id,
                )
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
        else:
            context = await _composer_context(
                db,
                session=session,
                form=form,
                ticket=await _reload_ticket(db, form=form, ticket=ticket),
                error=error,
                override_warnings=warnings,
            )
    if confirmation is not None:
        return _render(request, "_order_confirmation.html", confirmation)
    return _render(request, "_order_composer.html", context)


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
        investment = await _resolve_traded(investments, form.investment_id)
        if investment is None:
            return _bad_request(_CASH_NO_INVESTMENT, field="investment_id")
        existing = await _cash_in_currency(investments, investment)
        if any(row.is_active for row in existing):
            return _bad_request(
                _CASH_ALREADY_EXISTS.format(currency=investment.currency),
                field="cash_investment_id",
            )
        if existing:
            return _bad_request(
                _CASH_ONLY_RETIRED.format(currency=investment.currency),
                field="cash_investment_id",
            )

        try:
            created = await _build_investment_service(db).create_cash_position(
                name=form.cash_name or "",
                currency=investment.currency,
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
        context = await _composer_context(db, session=session, form=form)
    return _render(request, "_order_composer.html", context)


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
        derived = await _derived_context(
            db,
            session=session,
            direction=form.direction,
            investment_id=form.investment_id,
            trade_date=form.trade_date,
            settlement_date=form.settlement_date,
            units=form.units,
            price_per_unit=form.price_per_unit,
            fees=form.fees,
            taxes=form.taxes,
            cash_investment_id=form.cash_investment_id,
            settle_confirmed=form.settle_confirmed,
            set_inactive=form.set_inactive,
            case_id=form.case_id,
            source=form.source,
            note=form.note,
            ticket_status=ticket.status if ticket is not None else None,
        )

    return _render(
        request,
        "_order_recalc.html",
        {
            "csrf_token": session.csrf_token,
            "oob": True,
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
