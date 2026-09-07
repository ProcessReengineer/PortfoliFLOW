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

Five surfaces, still one substrate (P-4b)
-----------------------------------------
R-COMMIT and R-SEC-BUY arm the last two chooser tiles, and they too add **no
endpoint that writes**: one ``GET`` each, and the composer's own three
gestures. What they cost the substrate is one table and two flags. Every
``if secondary … elif creating …`` chain that had been growing a branch per
flow — in :func:`_ensure_draft`, :func:`_composer_context`,
:func:`post_recalc` and :func:`_composer_template` — now reads :data:`_FLOWS`,
and :class:`_ComposerForm`'s ``creating`` became the *union* of the three
flows that make an investment row rather than a synonym for the wizard.

What genuinely differs is a **commitment**. It is the one flow that moves no
cash (MD-19): no settlement position, no cash leg, no projected balance, and
the surface says so in a panel of prose rather than by leaving a block empty.
Neither purchase form offers a picker — MD-12 makes their investment an
emission effect, and a ``secondary``/``buy`` that named one is
``_unroutable`` — so the two of them, with the wizard, are the surface's
whole creating half.

Reads and writes, kept apart
----------------------------
Twelve endpoints. ``order-form``, ``secondary-sale-form``,
``commitment-form``, ``secondary-buy-form``, ``wizard``, ``chooser``,
``recalc`` and ``resolve-identifier`` are reads: they derive, they render, and
they touch no row (MD-2 — opening a composer allocates nothing and burns no
ticket number). ``draft``, ``propose``, ``book`` and ``cash-position`` are the
writes, owner-gated and CSRF-checked, and every one of them re-checks
server-side what the surface had already gated: a form is a suggestion, never
a permission. ``web/routes/areas.py`` stays a no-DB shell render: the chooser
is static markup in the area body, and everything that needs the database sits
behind the HTMX endpoints below.

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
payload.

P-4b closes that inventory. ``commitment_amount`` is R-COMMIT's amount and
carries a column of its own; ``md_vintage_year``, ``md_acquired_nav``,
``md_assumed_unfunded`` and ``md_purchase_price`` complete the fifteen
``MD_*`` keys. Two of the fifteen are never read from a field of their own:
``currency`` is written from the ticket column (W-4), and ``purchase_price``
from ``gross_amount`` — both are D-U mirrors, written from the one place the
value was entered so the two spellings cannot disagree.

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

from collections.abc import Callable, Iterable
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
from core.repositories.user_repository import UserRepository
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
    CANCEL_REASON_REQUIRED_STATUSES,
    CANCELLABLE_STATUSES,
    DIRECTION_BUY,
    DIRECTION_SELL,
    KIND_COMMITMENT,
    KIND_ORDER,
    KIND_SECONDARY,
    MD_ACQUIRED_NAV,
    MD_ANLV_CODE,
    MD_ASSET_CLASS_ID,
    MD_ASSUMED_UNFUNDED,
    MD_COMMITMENT_AMOUNT,
    MD_CURRENCY,
    MD_FIGI,
    MD_IDENTIFIER_SCHEME,
    MD_IDENTIFIER_VALUE,
    MD_INVESTMENT_TYPE,
    MD_MANAGER,
    MD_NAME,
    MD_PURCHASE_PRICE,
    MD_REGION,
    MD_VINTAGE_YEAR,
    STATUS_APPROVED,
    STATUS_DRAFT,
    STATUS_PROPOSED,
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
    is_cash_moving,
    is_investment_creating,
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

#: The ``flow`` value for the commitment composer (R-COMMIT, S4c / P-4b).
#:
#: Signalled like its two siblings. A commitment names no investment and
#: states no units, so nothing on the body would distinguish it from a
#: half-typed wizard draft; the field is what says which of the two it is.
FLOW_COMMITMENT: str = "commitment"

#: The ``flow`` value for the secondary-purchase composer (R-SEC-BUY, P-4b).
#:
#: Distinguished from :data:`FLOW_SECONDARY_SALE` by the flow rather than by
#: the direction, for the same reason the sale is distinguished from a
#: U-SELL: the direction is a *consequence* of the flow (MD-15), so reading it
#: the other way round would let a tampered body change which emission runs.
FLOW_SECONDARY_BUY: str = "secondary_buy"


@dataclass(frozen=True)
class _Flow:
    """What one composer flow builds, and which partials draw it.

    The five entry points of MD-1, stated once. Before P-4b the same five
    facts were spread over four ``if secondary … elif creating …`` chains —
    in :func:`_ensure_draft`, :func:`_composer_context`, :func:`post_recalc`
    and :func:`_composer_template` — and every new flow had to be added to
    all four in agreement. They are one table now, keyed by the ``flow``
    signal the form posts, so a flow is described in one place and read
    everywhere.

    ``direction`` is ``None`` for exactly one flow: U-BUY / U-SELL, the only
    surface that *offers* the choice. Every other flow's direction is a
    constant of the flow (MD-14, MD-15, MD-17), so a posted opposite is
    ignored rather than refused — the surface never offered it.

    Attributes:
        kind: The ticket kind this flow writes.
        direction: The flow's fixed direction, or ``None`` to take the
            form's.
        creating: Whether booking *creates* the investment (MD-12). The
            union of U-NEW, R-COMMIT and R-SEC-BUY — the same three
            :func:`~services.transactions.validation.is_investment_creating`
            names one layer down.
        costs: Whether the form offers fees and taxes. M-3's two purchase
            forms state a single net figure and offer neither.
        composer: The composer partial, or ``None`` for the wizard, which
            has an assembly and a template of its own.
        recalc: The recalculation response's partial.
        label: The human flow name every list surface shows (A-12). The
            routing table is also the labelling table, so a flow is named
            where it is described rather than in a second dict the blotter
            and history would each have to be kept in step with. The order
            flow's label is completed with its direction at render, which
            is :func:`_flow_label`'s whole job.
    """

    kind: str
    direction: str | None
    creating: bool
    costs: bool
    composer: str | None
    recalc: str
    label: str


#: Every flow this Area composes, keyed by the ``flow`` field's value.
#:
#: The empty key is U-BUY / U-SELL: M-1's composer predates the flow signal
#: and posts none, which is why the signal is read permissively — an
#: unrecognised value is the order composer, the same way an unrecognised
#: ``fraction`` is a full sale.
_FLOWS: dict[str, _Flow] = {
    "": _Flow(
        kind=KIND_ORDER,
        direction=None,
        creating=False,
        costs=True,
        composer="_order_composer.html",
        recalc="_order_recalc.html",
        label="Order",
    ),
    FLOW_NEW_INSTRUMENT: _Flow(
        kind=KIND_ORDER,
        direction=DIRECTION_BUY,
        creating=True,
        costs=True,
        composer=None,
        recalc="_wizard_recalc.html",
        label="New instrument",
    ),
    FLOW_SECONDARY_SALE: _Flow(
        kind=KIND_SECONDARY,
        direction=DIRECTION_SELL,
        creating=False,
        costs=True,
        composer="_secondary_sale_composer.html",
        recalc="_secondary_sale_recalc.html",
        label="Secondary sale",
    ),
    FLOW_COMMITMENT: _Flow(
        kind=KIND_COMMITMENT,
        direction=DIRECTION_BUY,
        creating=True,
        costs=False,
        composer="_commitment_composer.html",
        recalc="_commitment_recalc.html",
        label="Commitment",
    ),
    FLOW_SECONDARY_BUY: _Flow(
        kind=KIND_SECONDARY,
        direction=DIRECTION_BUY,
        creating=True,
        costs=False,
        composer="_secondary_buy_composer.html",
        recalc="_secondary_buy_recalc.html",
        label="Secondary purchase",
    ),
}

#: How a direction completes the order flow's label.
#:
#: Only the order flow needs it: every other flow's direction is a constant
#: of the flow (MD-14, MD-15, MD-17), so naming it in the label would state
#: twice what the flow already says once.
_DIRECTION_LABELS: dict[str, str] = {DIRECTION_BUY: "Buy", DIRECTION_SELL: "Sell"}


def _flow_of(ticket: TradeTicketDTO) -> str:
    """Return the ``_FLOWS`` key a stored ticket belongs to (A-12).

    **The one reverse lookup**, and deliberately the only one: the blotter,
    the resume ``GET`` and — later — History all need to know which of the
    five flows a row is, and three hand-written ``if kind == …`` chains would
    be three chances to disagree about a secondary purchase. The forward
    direction is :data:`_FLOWS`, which says what each flow builds; this is
    the same table read backwards.

    The classification is
    :func:`~services.transactions.validation.is_investment_creating`'s, not a
    second opinion about it — the same predicate the emission dispatches on
    (MD-12) — so a ticket cannot route to one composer here and to a
    different emission at booking.

    **Exact for an in-flight ticket.** A creating booking writes
    ``investment_id`` back onto the ticket (``link_investment``), so a
    *booked* U-NEW no longer answers ``is_investment_creating`` and reads
    here as an ordinary order. That is invisible to the blotter, whose set is
    ``draft`` / ``proposed`` / ``approved`` by definition, and it is a real
    edge for History: P-5b must not assume this function re-derives the flow
    a terminal row was composed in.

    Args:
        ticket: The stored ticket.

    Returns:
        The key into :data:`_FLOWS`.

    Raises:
        ValueError: For a ticket that is none of the five flows. A row that
            fits no flow is a corrupted book rather than a case for a
            fallback: rendering it as the order composer would offer to edit
            a ticket through a surface that cannot express it.
    """
    creating = is_investment_creating(
        kind=ticket.kind,
        direction=ticket.direction,
        investment_id=ticket.investment_id,
        master_data=ticket.master_data,
    )
    if ticket.kind == KIND_ORDER:
        return FLOW_NEW_INSTRUMENT if creating else ""
    if ticket.kind == KIND_COMMITMENT:
        return FLOW_COMMITMENT
    if ticket.kind == KIND_SECONDARY:
        return FLOW_SECONDARY_BUY if ticket.direction == DIRECTION_BUY else FLOW_SECONDARY_SALE
    raise ValueError(
        f"Trade ticket {ticket.ticket_number} has kind {ticket.kind!r}, which is no "
        "composer flow; the book is inconsistent."
    )


def _flow_label(ticket: TradeTicketDTO) -> str:
    """Return the human flow name a list row shows for this ticket.

    :data:`_FLOWS`' own label, completed with the direction for the one flow
    that offers the choice — "Order · Buy" / "Order · Sell", the copy fixed
    at the M-5 checkpoint. The four reported flows carry their direction in
    the name already ("Secondary sale"), so appending it would read as a
    stutter.
    """
    flow = _flow_of(ticket)
    label = _FLOWS[flow].label
    if flow:
        return label
    return f"{label} · {_DIRECTION_LABELS[ticket.direction]}"


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


def _int_or_none(raw: str | None) -> int | None:
    """Parse a whole-number form value into an :class:`int`, or ``None``.

    :func:`_decimal_or_none`'s contract for the one field that is a count
    rather than an amount — M-3's vintage year. Absent, blank and unparseable
    all read as ``None``, because a half-typed year is the ordinary state of a
    field being typed into and not an error the recalculation should report.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _text_or_none(value: Decimal | int | None) -> str | None:
    """Render a parsed number for the JSONB payload, or ``None``.

    The inverse of :func:`_decimal_or_none` / :func:`_int_or_none`, and
    deliberately the *plainest* spelling: ``str`` of what was parsed, which is
    what :func:`~services.transactions.emission.parse_master_data` reads back
    through ``Decimal(str(value))`` and ``int(str(value))``. Formatting it any
    other way here would put a second convention between the two.
    """
    return None if value is None else str(value)


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

    P-4b completes it. ``commitment_amount`` is R-COMMIT's one amount and
    the only new field that carries a *column*; the four remaining ``md_*``
    names finish the fifteen-key ``MD_*`` contract. Only three of the four
    have a control: ``md_vintage_year`` on both new forms,
    ``md_acquired_nav`` and ``md_assumed_unfunded`` on R-SEC-BUY.
    ``md_purchase_price`` has none anywhere, because D-U makes the purchase
    price a *mirror* of ``gross_amount`` rather than a second input — it is
    listed so the inventory is the whole contract and so a body that posts
    it is parsed rather than silently absorbed by FastAPI.

    ``flow`` grew from a boolean's worth of meaning to five values, and the
    three predicates it feeds are no longer synonyms: ``creating`` is the
    **union** of the flows that make an investment row (U-NEW, R-COMMIT,
    R-SEC-BUY, per :data:`_FLOWS`), while ``new_instrument`` is the wizard
    alone — which is what template selection and the wizard's step
    navigation actually mean when they used to say ``creating``.

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
        # -- R-COMMIT and R-SEC-BUY complete the inventory (P-4b) --------
        commitment_amount: Annotated[str, Form()] = "",
        md_vintage_year: Annotated[str, Form()] = "",
        md_purchase_price: Annotated[str, Form()] = "",
        md_acquired_nav: Annotated[str, Form()] = "",
        md_assumed_unfunded: Annotated[str, Form()] = "",
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
        # The signal is narrowed to a flow this module composes, on the same
        # permissive contract as every other field: an unrecognised value is
        # the order composer, which is what a body with no signal at all is.
        self.flow = flow if flow in _FLOWS else ""
        self.new_instrument = self.flow == FLOW_NEW_INSTRUMENT
        self.secondary_sale = self.flow == FLOW_SECONDARY_SALE
        self.commitment = self.flow == FLOW_COMMITMENT
        self.secondary_buy = self.flow == FLOW_SECONDARY_BUY
        # `creating` is the **union** of the three flows whose booking makes
        # the investment row (MD-12), not a synonym for the wizard: every
        # rule that turns on "there is no investment to derive from" — the
        # currency's source (W-4), the skipped `_resolve_traded`, the
        # `_Creating` fallback, the forced `set_inactive` — holds for all
        # three. The one thing that is the wizard's alone is which template
        # renders, and that reads `new_instrument`.
        self.creating = _FLOWS[self.flow].creating
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
        self.commitment_amount = _decimal_or_none(commitment_amount)
        self.md_vintage_year = _int_or_none(md_vintage_year)
        self.md_purchase_price = _decimal_or_none(md_purchase_price)
        self.md_acquired_nav = _decimal_or_none(md_acquired_nav)
        self.md_assumed_unfunded = _decimal_or_none(md_assumed_unfunded)
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
            "commitment_amount": commitment_amount,
            "md_vintage_year": md_vintage_year,
            "md_purchase_price": md_purchase_price,
            "md_acquired_nav": md_acquired_nav,
            "md_assumed_unfunded": md_assumed_unfunded,
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

        **The two D-U mirrors are written from the same posted field as the
        column they mirror** (P-4b). ``reconcile_commitment`` refuses a
        ticket whose column and payload state different commitments, and it
        is right to: a commitment is the denominator of every pacing figure.
        The way to make that refusal unreachable from this surface is not to
        offer two inputs — R-COMMIT posts one ``commitment_amount`` and this
        method writes both the column's value and
        ``MD_COMMITMENT_AMOUNT``; R-SEC-BUY posts one
        ``md_assumed_unfunded`` and :func:`_ensure_draft` mirrors it into the
        column. ``MD_PURCHASE_PRICE`` is the same shape over
        ``gross_amount``: carried for the record, never a second input.

        Numbers are formatted the way
        :func:`~services.transactions.emission.parse_master_data` reads them
        back — ``str`` of a :class:`~decimal.Decimal` or an :class:`int`,
        which ``_optional_amount``'s ``Decimal(str(value))`` and
        ``_optional_year``'s ``int(str(value))`` both round-trip exactly.

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
            MD_VINTAGE_YEAR: _text_or_none(self.md_vintage_year),
            MD_ACQUIRED_NAV: _text_or_none(self.md_acquired_nav),
            MD_ASSUMED_UNFUNDED: _text_or_none(self.md_assumed_unfunded),
        }
        if self.commitment:
            pairs[MD_COMMITMENT_AMOUNT] = _text_or_none(self.commitment_amount)
        if self.secondary_buy:
            pairs[MD_PURCHASE_PRICE] = _text_or_none(self.gross_amount)
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
    commitment_amount: Decimal | None = None,
    master_data: dict[str, Any] | None = None,
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
        commitment_amount: R-COMMIT's stated commitment (P-4b). Carried so
            the transient ticket is the same shape as the row the gesture
            would write, not because any derivation beneath here reads it —
            a commitment moves no cash (MD-19), so
            :meth:`~services.transactions.ticket_service.TicketService
            .preview`'s ``cash_effect`` is correctly ``None`` for one.
        master_data: The projected payload, on the creating flows. Also
            carried for shape rather than for use: ``preview`` never calls
            :func:`~services.transactions.validation.is_investment_creating`
            — the only reader of this field — so an empty payload and a
            half-typed one preview identically today. Passing the real one
            keeps that true if a creating-aware block is ever previewable.

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
        commitment_amount=commitment_amount,
        master_data=master_data,
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


def _created_row(name: str) -> dict[str, Any]:
    """The ``create`` row every investment-creating flow's booking emits.

    M-3 draws the same row on both of its creating forms and the wording is
    identical on each; stating it once is what keeps the *valuation mode* it
    names honest, since both flows create a ``reported`` row (D-R) and a
    second copy is where one of them would come to say ``unitised``.

    It carries no amount. The row itself is the creation; what the position
    is worth is the ``nav`` row beside it, or — for a commitment — nothing
    yet, which is exactly MD-19's point.
    """
    return {
        "type": "create",
        "what": f"Investment · {name}",
        "detail": VALUATION_MODE_REPORTED,
        "amount": None,
    }


def _vintage_detail(vintage_year: int | None) -> str:
    """The ``vintage 2026`` note on a commitment row, or the em dash.

    M-3 always states a vintage; the service does not require one
    (``_optional_year``), so the state the mockup never draws is written here
    in its voice. A dash rather than an omitted note, because the row's three
    slots are fixed and a blank one would read as a rendering fault.
    """
    return f"vintage {vintage_year}" if vintage_year is not None else "—"


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
    commitment_amount: Decimal | None = None,
    acquired_nav: Decimal | None = None,
    assumed_unfunded: Decimal | None = None,
    vintage_year: int | None = None,
    master_data: dict[str, Any] | None = None,
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
        commitment_amount: R-COMMIT's stated commitment (P-4b).
        acquired_nav: R-SEC-BUY's stake value at transfer — the opening NAV
            its booking writes.
        assumed_unfunded: The unfunded commitment R-SEC-BUY takes on.
        vintage_year: The vintage the creating flows *state*. A picked
            investment's own vintage wins where there is one, so the context
            key this returns has a single meaning either way.
        master_data: The projected payload, passed through to the transient
            ticket; see :func:`_transient_ticket`.

    Returns:
        The template context for the four derived regions.
    """
    investments = InvestmentRepository(db)
    ledger_rows = PositionTransactionRepository(db)
    prices = InstrumentPriceRepository(db)
    service = _build_ticket_service(db)

    # -- what shape of answer this kind admits ------------------------------
    #
    # Four predicates in place of P-4a's single `secondary` boolean, and two
    # of them are the *service's own* (`is_cash_moving`, and `creating`,
    # which is `is_investment_creating`'s three flows resolved one layer up).
    # Deriving the surface's shape from the same functions the emission
    # dispatches on is what stops this file from growing a private theory of
    # which flow does what — the alternative, a fourth boolean parameter per
    # flow, is how five flows become thirty-two states.
    creating_flow = creating is not None
    moves_cash = is_cash_moving(kind=kind)
    unit_priced = kind == KIND_ORDER
    states_gross = kind == KIND_SECONDARY
    buying = direction == DIRECTION_BUY

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
            pickable=_is_reported_pickable if states_gross else _is_pickable,
        )
        if not creating_flow
        else None
    )
    currency = (
        investment.currency
        if investment is not None
        else (creating.currency if creating is not None else "")
    )

    holding: Decimal | None = None
    if investment is not None and not states_gross:
        holding = holdings_as_of(await ledger_rows.list_for_investment(investment.id), trade_date)
    elif creating_flow and unit_priced:
        holding = Decimal(0)

    # -- settlement candidates (MD-3, and the D-F split) --------------------
    #
    # Not asked at all on a flow that settles against nothing (MD-19). A
    # commitment has a currency like every other ticket, so the candidate
    # query would happily return this tenant's cash rows and the panel would
    # offer a choice the schema forbids the ticket to make.
    in_currency = await _cash_in_currency(investments, currency) if moves_cash else []
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
        commitment_amount=commitment_amount,
        master_data=master_data,
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
        if states_gross
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
    elif investment is not None and not states_gross:
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
    # inputs on the unit paths, one stated amount on the secondary ones, where
    # there are no units to enter (S4c), and the commitment on R-COMMIT, which
    # moves no cash and therefore projects no balance (P-4b). One local, used
    # by both the balance projection and the emission preview, so the two
    # cannot come to disagree about when a flow is answerable.
    amounts_stated = (
        commitment_amount is not None
        if kind == KIND_COMMITMENT
        else (
            gross is not None
            if states_gross
            else (units is not None and price_per_unit is not None)
        )
    )
    projected: Decimal | None = None
    if (
        (investment is not None or creating_flow)
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
    if investment is not None and priced and not states_gross:
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
    elif creating is not None and unit_priced and priced and creating.name:
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
        if states_gross and investment is not None
        else None
    )
    unfunded: Decimal | None = None
    if states_gross and investment is not None:
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

    # MD-20's other half: R-SEC-BUY's price against the NAV it acquired.
    #
    # A sibling key rather than a second meaning for `vs_nav`, because the two
    # rows measure *different things against different references* — proceeds
    # against the last statement, price against the value at transfer — and
    # M-3 words them apart accordingly. One key would have to carry which, and
    # a template would then decide what the number means.
    #
    # The word is a reading of the sign, not a second derivation: at or above
    # the acquired NAV a stake changed hands at a premium, below it at a
    # discount, and there is nothing here to compute a second time.
    vs_acquired_nav: str | None = None
    if acquired_nav is not None and gross is not None:
        ratio = signed_deviation_ratio(value=gross, reference=acquired_nav)
        if ratio is not None:
            side = "premium" if ratio >= 0 else "discount"
            vs_acquired_nav = f"{ratio * 100:+,.1f} % ({side})".replace("-", _MINUS)

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
    #
    # P-4b adds the two creating shapes to the same chain. R-COMMIT's two rows
    # need no settlement position and no cash effect at all — it is the one
    # flow whose consequences are complete without either (MD-19) — so its
    # branch is keyed on what it *does* state: a name for the row and an
    # amount for the commitment. R-SEC-BUY's four are keyed like R-SEC-SELL's,
    # on `priced`, because its fourth row is a real cash leg.
    effect_rows: list[dict[str, Any]] = []
    if kind == KIND_COMMITMENT:
        if creating is not None and creating.name and commitment_amount is not None:
            effect_rows = [
                _created_row(creating.name),
                {
                    "type": "commit",
                    "what": "Commitment recorded",
                    "detail": _vintage_detail(vintage_year),
                    "amount": f"{_money(commitment_amount)} {currency}",
                },
            ]
    elif states_gross and buying:
        if (
            creating is not None
            and creating.name
            and priced
            and net is not None
            and acquired_nav is not None
        ):
            effect_rows = [
                _created_row(creating.name),
                {
                    "type": "nav",
                    "what": "Opening NAV at trade date",
                    "detail": "manual origin",
                    "amount": f"{_money(acquired_nav)} {currency}",
                },
            ]
            # The commitment row only stands where a commitment is assumed:
            # a secondary stake that is fully called carries none, and a row
            # stating 0.00 would claim the emission writes one.
            if assumed_unfunded is not None:
                effect_rows.append(
                    {
                        "type": "commit",
                        "what": "Unfunded commitment assumed",
                        "detail": _vintage_detail(vintage_year),
                        "amount": f"{_money(assumed_unfunded)} {currency}",
                    }
                )
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
    elif states_gross and investment is not None and priced and net is not None:
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
    # sale needs a positive stated consideration and has no units at all, a
    # secondary purchase needs a price *and* the NAV it acquired, and a
    # commitment needs a name to create the row under and an amount to record.
    #
    # None of them asks for the whole master data (P-4b, W-3's
    # structural-minimum rule). Type, asset class and — on the two purchase
    # forms — the name are left to the service's own `missing_master_data`
    # sentence at Propose (D-5): it names the offending key, and duplicating
    # that judgement here would give the surface a second opinion about what
    # an `investments` row needs.
    if kind == KIND_COMMITMENT:
        complete = (
            creating is not None
            and bool(creating.name)
            and commitment_amount is not None
            and commitment_amount > 0
        )
    elif states_gross and buying:
        complete = gross is not None and gross > 0 and acquired_nav is not None
    elif states_gross:
        complete = investment is not None and gross is not None and gross > 0
    else:
        complete = (
            (investment is not None or creating_flow)
            and units is not None
            and units > 0
            and price_per_unit is not None
            and price_per_unit > 0
        )
    # A flow that settles against nothing is settled (MD-19): there is no
    # position to pick and no confirmation to withhold, so reading the MD-3
    # answer here would gate R-COMMIT on a question it never asks.
    settled = not moves_cash or (selected_cash is not None and settle_confirmed)
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
    instrument_name = (
        investment.name
        if investment is not None
        else (creating.name if creating is not None else None)
    )
    if kind == KIND_COMMITMENT:
        flow_title = "Record a commitment"
    elif states_gross:
        flow_title = "Buy a stake (secondary)" if buying else "Sell a stake"
    elif creating_flow:
        flow_title = "Buy a new instrument"
    else:
        flow_title = "Sell units" if direction == DIRECTION_SELL else "Buy units"
    return {
        "direction": direction,
        # The flow's name, then the instrument once there is one to name.
        #
        # M-2 is the exception and keeps the bare flow name throughout: its
        # instrument is not named until step 2, and the head sitting two lines
        # above that input would echo it back as the operator typed. The
        # single-page forms have no such step, so their heads say what the
        # ticket is about from the moment it has a name.
        "title": (
            flow_title
            if (creating_flow and unit_priced) or not instrument_name
            else f"{flow_title} · {instrument_name}"
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
        # The same number unsigned, for M-3's "Purchase price (cash out)" row.
        #
        # `cash_effect` is a *magnitude*: the direction lives on the ticket and
        # `derive_cash_effect` applies it one layer down, at the cash leg. M-3
        # draws the row with a minus, and the template states that minus the
        # way `_secondary_sale_derived.html` already states the one on its fees
        # row (P-4a) — a constant of a flow whose direction MD-15 fixes, not a
        # second arithmetic. Handing the template `_signed_money`'s ``+`` and
        # asking it to flip the sign is what that would have been.
        "cash_out": _money(net) if net is not None else None,
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
        # The picked row's vintage where there is a row, the stated one where
        # the row does not exist yet (MD-12) — one key, one meaning.
        "vintage_year": investment.vintage_year if investment is not None else vintage_year,
        "valuation_mode": investment.valuation_mode if investment is not None else None,
        "vs_nav": vs_nav,
        "vs_acquired_nav": vs_acquired_nav,
        "acquired_nav": _money(acquired_nav) if acquired_nav is not None else None,
        "assumed_unfunded": (_money(assumed_unfunded) if assumed_unfunded is not None else None),
        "commitment": _money(commitment_amount) if commitment_amount is not None else None,
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
            # The wizard answers the gate in its own outcome partial, ahead
            # of the hint chain and with a deep link back to step 2, so
            # folding it into the key there would displace the settlement
            # guidance step 3 still needs. The single-page flows have no step
            # to send anyone back to, and M-3's script tests the gate first.
            anlv_gate=anlv_gate and not unit_priced,
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
    anlv_gate: bool = False,
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

    ``anlv_gate`` ranks straight after ``incomplete``, which is M-3's order
    again — its R-SEC-BUY script tests ``!anlvSet`` before ``bNeedsConfirm``
    — and the reasoning is the same as the partial sale's, one degree softer:
    a form that cannot be proposed for want of a classification will not be
    proposed by confirming a settlement position either. It is passed in
    rather than derived, because one surface (M-2's wizard) states the gate
    ahead of this chain and needs the chain to keep answering about
    settlement; see the call site.

    Returns:
        One of ``partial_sale`` / ``incomplete`` / ``anlv`` / ``no_position``
        / ``unconfirmed`` / ``blocked`` / ``ready``.
    """
    if partial_sale:
        return "partial_sale"
    if not complete:
        return "incomplete"
    if anlv_gate:
        return "anlv"
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
    flow: str = "",
) -> dict[str, Any]:
    """Build a single-page composer's context — the opening render and every gesture's.

    One function for every render of all four single-page surfaces. A gesture
    that succeeds, a gesture that is refused and the first ``GET`` differ in
    three values (the ticket, the red block, whose warnings to show) and in
    nothing else, so writing the assembly once is what keeps a refused Propose
    from quietly showing a different picker or a stale settlement panel than
    the form it refused.

    S4c made it kind-aware rather than copying it; P-4b makes it
    **flow**-keyed, which is the same move once more. M-1's composer, M-3's
    R-SEC-SELL, R-COMMIT and R-SEC-BUY ask the same three questions in the
    same order — what is this about, where does it settle, and what does that
    mean — and differ in exactly two things this function has to know: which
    lists the form needs, and which shape the derivations take. Both come off
    :data:`_FLOWS`, so the four near-copies that would have drifted in the
    settlement panel do not exist.

    The two lists are read **per flow, not always**. A picking flow needs the
    investments its picker offers and no catalogues; a creating flow needs the
    catalogues and no picker, because there is nothing to pick — a
    ``secondary``/``buy`` naming an investment is
    :meth:`~services.transactions.ticket_service.TicketService._emit`'s
    ``_unroutable``, so a picker here would offer a ticket no emission can
    take (the top-up is a successor's, not v1's).

    The wizard keeps its own assembly (:func:`_wizard_context`) because it
    genuinely differs: it renders one step of four, it carries a resolver's
    answer, and its field carry is an exclusion list rather than a form.

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
        flow: Which of :data:`_FLOWS` this render is. The empty string is
            M-1's order composer, which posts no signal.

    Returns:
        The template context for the flow's composer partial.
    """
    spec = _FLOWS[flow]
    unit_priced = spec.kind == KIND_ORDER
    states_gross = spec.kind == KIND_SECONDARY
    currency = _validate_currency(form.currency) or "" if spec.creating else ""
    if spec.creating:
        investments: list[InvestmentDTO] = []
        asset_classes = await AssetClassRepository(db).list_all()
        anlv_categories = await AnlVCategoryRepository(db).list_all()
    else:
        pickable = _is_reported_pickable if states_gross else _is_pickable
        investments = [row for row in await InvestmentRepository(db).list_active() if pickable(row)]
        asset_classes = []
        anlv_categories = []
    cases = await CaseRepository(db).list_open()
    derived = await _derived_context(
        db,
        session=session,
        # MD-14 / MD-15 / MD-17: every flow but M-1's fixes its own
        # direction, so a posted opposite is ignored rather than refused —
        # the surface never offered the choice.
        direction=spec.direction or form.direction,
        investment_id=None if spec.creating else form.investment_id,
        trade_date=form.trade_date,
        settlement_date=form.settlement_date,
        units=form.units if unit_priced else None,
        price_per_unit=form.price_per_unit if unit_priced else None,
        # M-3's two purchase forms state one net figure and offer no costs
        # control; reading the fields anyway would let a tampered body add
        # fees to a price the operator was shown as the whole cash effect.
        fees=form.fees if spec.costs else None,
        taxes=form.taxes if spec.costs else None,
        cash_investment_id=form.cash_investment_id,
        settle_confirmed=form.settle_confirmed,
        # MD-17 again: the MD-7 checkbox is U-SELL's alone. Every other flow
        # either deactivates unconditionally or creates a row it would be
        # absurd to deactivate, so reading it would suggest a choice.
        set_inactive=form.set_inactive if (unit_priced and not spec.creating) else False,
        case_id=form.case_id,
        source=form.source,
        note=form.note,
        ticket_status=ticket.status if ticket is not None else None,
        override_warnings=override_warnings,
        creating=(
            _Creating(
                currency=currency,
                name=form.md_name,
                anlv_set=form.md_anlv_code is not None,
            )
            if spec.creating
            else None
        ),
        kind=spec.kind,
        gross_amount=form.gross_amount if states_gross else None,
        partial_sale=form.partial_sale if (states_gross and not spec.creating) else False,
        commitment_amount=form.commitment_amount if spec.kind == KIND_COMMITMENT else None,
        # R-SEC-BUY's three, read only where a control offers them: the two
        # transfer amounts on the purchase form, the vintage on either
        # creating reported form. Elsewhere a posted value is a tampered
        # body, and taking it would put a figure on a surface that never
        # asked for one.
        acquired_nav=form.md_acquired_nav if (states_gross and spec.creating) else None,
        assumed_unfunded=form.md_assumed_unfunded if (states_gross and spec.creating) else None,
        vintage_year=form.md_vintage_year if (spec.creating and not unit_priced) else None,
        master_data=form.master_data(currency=currency) if spec.creating else None,
    )
    return {
        "csrf_token": session.csrf_token,
        "flow": flow,
        "investments": investments,
        "investment_types": _CLASSIFIABLE_TYPES,
        "asset_classes": asset_classes,
        "anlv_categories": anlv_categories,
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


def _composer_template(flow: str) -> str:
    """Return the composer partial for a flow — one lookup, every render.

    The four gestures and the four opening ``GET``s all choose among the same
    four templates, and a hand-written ternary per call site is how one of
    them would come to render M-1's markup for an M-3 context.

    Raises:
        KeyError: For the wizard, which has no single-page composer. Reaching
            here with :data:`FLOW_NEW_INSTRUMENT` is a routing bug, and a
            silent fallback to M-1's markup would hide it behind a form that
            almost works.
    """
    composer = _FLOWS[flow].composer
    if composer is None:  # pragma: no cover — the callers branch first
        raise KeyError(f"Flow {flow!r} has no single-page composer; it renders as the wizard.")
    return composer


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


def _form_from_ticket(
    ticket: TradeTicketDTO, *, csrf_token: str, flow: str | None = None
) -> _ComposerForm:
    """Rebuild a composer's form state from a saved ticket (MD-10's other half).

    The resume GET has no request body, and a surface rendered from an empty
    one would show the operator a blank form over a ticket that is not blank.
    So the row is read back into the same :class:`_ComposerForm` a POST would
    have produced — every field, including ``entered``'s raw strings, since
    those are what the inputs echo.

    Nothing is interpreted here that the service would interpret differently:
    the payload's keys are read as the strings they are stored as, and
    :func:`~services.transactions.emission.parse_master_data` stays the one
    place they become domain values (D-V).

    **Total over all five flows since P-5a**, where it was the wizard's
    alone. The single resume ``GET`` (A-12) reopens any in-flight ticket, so
    the fields only the single-page composers carry are read back too —
    ``investment_id``, ``gross_amount``, ``commitment_amount``,
    ``set_inactive`` and R-SEC-BUY's three ``md_*`` amounts — and ``flow``
    comes from :func:`_flow_of` rather than being assumed. The omission that
    mattered most was the ``md_*`` trio: :meth:`_ComposerForm.master_data` is
    a **full replacement**, so a resumed purchase whose vintage and acquired
    NAV came back blank would have erased them on the next save.

    ``net_amount`` is deliberately absent, and is the one ticket column with
    no counterpart here: it is *derived* from gross, fees and taxes by
    :func:`_derived_context` on every render, and no composer posts it. A
    field for it would be a second source for a number that already has one.

    Args:
        ticket: The stored ticket to read back.
        csrf_token: The session's token, for the caller that renders a form.
        flow: The flow to render as, or ``None`` to derive it with
            :func:`_flow_of`. The wizard passes its own constant rather than
            deriving: :func:`get_wizard` admits any ``order`` ticket without
            an ``investment_id``, which includes the payload-less draft
            :func:`_flow_of` would classify as a plain order, and that surface
            has always rendered such a row as the wizard.
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
        flow=_flow_of(ticket) if flow is None else flow,
        currency=ticket.currency,
        investment_id=(str(ticket.investment_id) if ticket.investment_id is not None else ""),
        gross_amount=_plain(ticket.gross_amount),
        commitment_amount=_plain(ticket.commitment_amount),
        # The MD-7 checkbox, echoed the way the browser posts it. Only U-SELL
        # reads it back (`_derived_context` forces `False` everywhere else),
        # so this is the one flow where a resume that dropped it would quietly
        # un-answer a question the ticket records.
        set_inactive="1" if ticket.set_inactive else None,
        md_identifier_scheme=_text(MD_IDENTIFIER_SCHEME),
        md_identifier_value=_text(MD_IDENTIFIER_VALUE),
        md_figi=_text(MD_FIGI),
        md_name=_text(MD_NAME),
        md_investment_type=_text(MD_INVESTMENT_TYPE),
        md_asset_class_id=_text(MD_ASSET_CLASS_ID),
        md_anlv_code=_text(MD_ANLV_CODE),
        md_manager=_text(MD_MANAGER),
        md_region=_text(MD_REGION),
        # R-COMMIT's and R-SEC-BUY's own payload fields. `master_data` is a
        # full replacement, so these are read back not to render them alone
        # but so that saving a resumed purchase does not blank them.
        md_vintage_year=_text(MD_VINTAGE_YEAR),
        md_acquired_nav=_text(MD_ACQUIRED_NAV),
        md_assumed_unfunded=_text(MD_ASSUMED_UNFUNDED),
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
#: A creating flow has no investment to derive a currency from (MD-12), so W-4
#: makes the currency an entered fact and this is what stands in the way when
#: it is missing or malformed. Written in M-2's voice and registered as a copy
#: gap: the mockup's step 1 is always filled in, so it never renders one.
#:
#: Shared by all three creating flows since P-4b — the sentence names the
#: instrument rather than the wizard, so it reads true for a commitment and a
#: secondary purchase as it does for U-NEW, and each of the three would
#: otherwise have written the same rule in its own words.
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
    it is **flow-aware** (S4b, extended by S4c and P-4b) rather than
    duplicated per surface. Five columns of one table, one per MD-1 flow, and
    every constant in it comes from :data:`_FLOWS` rather than from a chain of
    ``if``\\ s here:

    ================= ================ ================ ================ ================ ================
    Column            U-BUY / U-SELL   U-NEW            R-SEC-SELL       R-COMMIT         R-SEC-BUY
    ================= ================ ================ ================ ================ ================
    ``kind``          ``order``        ``order`` (D-M)  ``secondary``    ``commitment``   ``secondary``
    ``direction``     the form's       ``buy`` MD-14    ``sell`` MD-17   ``buy`` R-3      ``buy`` MD-15
    ``investment_id`` the picked row   ``None`` MD-12   the reported row ``None`` MD-12   ``None`` MD-12
    ``currency``      the row's MD-8   the step-1 field the row's MD-8   the form's W-4   the form's W-4
    ``units``/price   the form's       the form's       ``None``         ``None``         ``None``
    ``gross_amount``  derived          derived          the proceeds     ``None``         the price
    ``fees``/taxes    the form's       the form's       the form's       ``None``         ``None``
    ``commitment``    ``None``         ``None``         ``None``         the amount       the unfunded
    ``cash_…_id``     the position     the position     the position     ``None`` MD-19   the position
    ``set_inactive``  the MD-7 choice  ``False``        ``False`` MD-17  ``False``        ``False``
    ``master_data``   absent           the payload      absent           the payload      the payload
    ================= ================ ================ ================ ================ ================

    ``kind`` is written on :meth:`~services.transactions.ticket_service
    .TicketService.create_draft` and **never in the update map**: a saved
    ticket's kind is a fact about which flow made it, and a body that could
    change it would let a stale form turn one flow's draft into another's.

    The ``commitment_amount`` row is D-U's, and it is why
    :func:`~services.transactions.emission.reconcile_commitment` cannot
    refuse a ticket this function wrote. R-COMMIT posts one amount and it
    becomes both the column and ``MD_COMMITMENT_AMOUNT``; R-SEC-BUY posts one
    ``md_assumed_unfunded`` and it becomes both ``MD_ASSUMED_UNFUNDED`` and
    the column. Two spellings of one number, written from one field, so the
    reconciliation compares a value with itself.

    ``cash_investment_id`` is still *in* the map for a commitment, carrying
    ``None``. Dropping the key would leave the column untouched on an update,
    which is a weaker statement than the one MD-19 makes: this flow settles
    against nothing. The value it writes is the one
    :func:`_gesture_context` already resolved, which drops a posted position
    on a flow that moves no cash.

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
    spec = _FLOWS[form.flow]
    unit_priced = spec.kind == KIND_ORDER
    states_gross = spec.kind == KIND_SECONDARY
    if spec.kind == KIND_COMMITMENT:
        commitment = form.commitment_amount
    elif states_gross and spec.creating:
        commitment = form.md_assumed_unfunded
    else:
        commitment = None
    fields: dict[str, Any] = {
        "direction": spec.direction or form.direction,
        "investment_id": None if spec.creating else cast(InvestmentDTO, investment).id,
        "cash_investment_id": cash_investment_id,
        "currency": currency,
        "trade_date": form.trade_date,
        "settlement_date": form.settlement_date,
        "units": form.units if unit_priced else None,
        "price_per_unit": form.price_per_unit if unit_priced else None,
        "gross_amount": form.gross_amount if states_gross else None,
        "fees": form.fees if spec.costs else None,
        "taxes": form.taxes if spec.costs else None,
        "commitment_amount": commitment,
        "set_inactive": form.set_inactive if (unit_priced and not spec.creating) else False,
        "note": form.note,
        "source": form.source,
        "case_id": form.case_id,
    }
    if spec.creating:
        fields["master_data"] = form.master_data(currency=currency)
    if form.ticket_id is None:
        return await service.create_draft(
            kind=spec.kind,
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

    A flow that **moves no cash** resolves to no position at all (P-4b). A
    commitment ticket that named one would be refused by the service in its
    own words and by ``ck_trade_tickets_commitment_shape`` beneath it, but
    the composer never offers the choice, so dropping the value is the honest
    shape rather than forwarding something only a hand-made body can carry.

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
    if (
        is_cash_moving(kind=_FLOWS[form.flow].kind)
        and currency is not None
        and form.cash_investment_id is not None
    ):
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
        if form.new_instrument:
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
            flow=form.flow,
        )
    return _render(request, _composer_template(form.flow), context)


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
                # deliberately does not. **Defence in depth**, and no longer
                # anything more: P-4n moved the one refusal that used to fire
                # mid-emission — the D-N NAV collision, which R-SEC-SELL
                # could reach with its distribution row already written — up
                # to propose time, where it is a block like any other. What
                # remains is the class of failures the block list does not
                # model, a driver `IntegrityError` among them: caught here,
                # they would otherwise be committed by `tenant_context` on
                # the way out and leave a half-written emission behind.
                # Rolling back to the savepoint undoes exactly the emission,
                # while the draft the same gesture may have just created
                # survives — the behaviour this module has always documented:
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
        elif form.new_instrument:
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
                flow=form.flow,
            )
    if confirmation is not None:
        return _render(request, "_order_confirmation.html", confirmation)
    if form.new_instrument:
        return _render(request, "_wizard.html", context)
    return _render(request, _composer_template(form.flow), context)


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
                    # What the creating reported flows put *on* the row, read
                    # back off the row rather than off the ticket (P-4b,
                    # operator fork 5). R-COMMIT emits this one effect and
                    # nothing else, so without these two the panel would say
                    # a fund was created and never say what was committed to
                    # it — the whole content of the booking. The mockups draw
                    # no confirmation panel, so the line is written in M-1's
                    # voice and registered as a copy gap.
                    "commitment": (
                        _money(updated.commitment_amount)
                        if updated.commitment_amount is not None
                        else None
                    ),
                    "commitment_currency": updated.currency,
                    "vintage_year": updated.vintage_year,
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
        if form.new_instrument:
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
        context = await _composer_context(db, session=session, form=form, flow=form.flow)
    return _render(request, _composer_template(form.flow), context)


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
    return _render(request, _composer_template(""), context)


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
        context = await _composer_context(
            db, session=session, form=_empty_form(), flow=FLOW_SECONDARY_SALE
        )
    return _render(request, _composer_template(FLOW_SECONDARY_SALE), context)


@router.get("/api/transactions/commitment-form", response_class=HTMLResponse)
async def get_commitment_form(
    request: Request,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Return the R-COMMIT composer (M-3), empty and ready to type into.

    The chooser's third tile swaps this in, on the same read posture as every
    other opening ``GET`` here: nothing is created, no ticket number is burnt,
    and the header says "Unsaved" until a gesture fires (MD-2).

    What differs from every other composer on this surface is what is
    *missing*. There is no picker, because MD-12 makes the investment an
    emission effect and R-COMMIT always records a new position — there is
    nothing else it could mean. And there is no settlement panel at all: a
    commitment moves no cash (MD-19, R-3), the schema forbids it a settlement
    position, and the form says so in words rather than by leaving a block
    empty.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        context = await _composer_context(
            db, session=session, form=_empty_form(), flow=FLOW_COMMITMENT
        )
    return _render(request, _composer_template(FLOW_COMMITMENT), context)


@router.get("/api/transactions/secondary-buy-form", response_class=HTMLResponse)
async def get_secondary_buy_form(
    request: Request,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Return the R-SEC-BUY composer (M-3), empty and ready to type into.

    The chooser's fourth tile, and the last of the five to be armed. Same read
    posture as its siblings (MD-2): nothing written, no number allocated.

    It has **no picker either**, and for a sharper reason than R-COMMIT's. A
    ``secondary``/``buy`` ticket that named an investment is not a top-up in
    v1 — it is :meth:`~services.transactions.ticket_service.TicketService
    ._emit`'s ``_unroutable``, none of the six flows ADR-0128 §1 defines — so
    offering a picker would offer a ticket no emission can take. Adding to an
    existing stake is a successor's flow and wants an ADR of its own.

    It settles like every other purchase, through the shared
    ``_settlement.html`` panel, and the currency it follows is the form's own
    (W-4) rather than a picked row's.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        context = await _composer_context(
            db, session=session, form=_empty_form(), flow=FLOW_SECONDARY_BUY
        )
    return _render(request, _composer_template(FLOW_SECONDARY_BUY), context)


#: What the Identify step says when OpenFIGI knows the identifier is nothing.
#:
#: M-2 draws only the resolved card, so the no-match state has no mockup copy.
#: Written in M-2's voice — it names the two remedies the step actually
#: offers — and registered as a copy gap for the operator's walk.
_RESOLVE_NO_MATCH: str = (
    "No instrument matched {scheme} {value}. Check the identifier, or use "
    "the second card and name the instrument yourself."
)


async def _render_wizard(
    request: Request,
    *,
    session: SessionDTO,
    db: AsyncSession,
    ticket: TradeTicketDTO | None,
    step_raw: str = "",
) -> HTMLResponse:
    """Render the M-2 wizard over a ticket, or fresh — both ways in.

    Lifted out of :func:`get_wizard` unchanged when P-5a gave the blotter its
    single resume ``GET`` (A-12): that endpoint must render *exactly* what
    ``/api/transactions/wizard?ticket_id=`` renders for a U-NEW row, and the
    only way to be sure of "exactly" is for both to run the same code rather
    than for one to reproduce the other.

    The caller owns the lookup and the 404. What is here is the part after
    it: the form the ticket reads back into, the step, and the render.

    Args:
        request: The live request.
        session: The authenticated session.
        db: The tenant-scoped session the caller opened.
        ticket: The draft to resume, or ``None`` for a fresh wizard.
        step_raw: An explicit step, or ``""`` to derive it from the draft
            with :func:`_resume_step` (MD-10).

    Returns:
        The wizard at one step.
    """
    form = (
        _form_from_ticket(ticket, csrf_token=session.csrf_token, flow=FLOW_NEW_INSTRUMENT)
        if ticket is not None
        else _ComposerForm(flow=FLOW_NEW_INSTRUMENT, direction=DIRECTION_BUY)
    )
    asked = _step_or_first(step_raw) if step_raw else _resume_step(ticket)
    context = await _wizard_context(db, session=session, form=form, ticket=ticket, step=asked)
    return _render(request, "_wizard.html", context)


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
        return await _render_wizard(request, session=session, db=db, ticket=ticket, step_raw=step)


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
        spec = _FLOWS[form.flow]
        unit_priced = spec.kind == KIND_ORDER
        states_gross = spec.kind == KIND_SECONDARY
        currency = _validate_currency(form.currency) or "" if spec.creating else ""
        derived = await _derived_context(
            db,
            session=session,
            direction=spec.direction or form.direction,
            investment_id=None if spec.creating else form.investment_id,
            trade_date=form.trade_date,
            settlement_date=form.settlement_date,
            units=form.units if unit_priced else None,
            price_per_unit=form.price_per_unit if unit_priced else None,
            fees=form.fees if spec.costs else None,
            taxes=form.taxes if spec.costs else None,
            cash_investment_id=form.cash_investment_id,
            settle_confirmed=form.settle_confirmed,
            set_inactive=form.set_inactive if (unit_priced and not spec.creating) else False,
            case_id=form.case_id,
            source=form.source,
            note=form.note,
            ticket_status=ticket.status if ticket is not None else None,
            creating=(
                _Creating(
                    currency=currency,
                    name=form.md_name,
                    anlv_set=form.md_anlv_code is not None,
                )
                if spec.creating
                else None
            ),
            kind=spec.kind,
            gross_amount=form.gross_amount if states_gross else None,
            partial_sale=form.partial_sale if (states_gross and not spec.creating) else False,
            commitment_amount=form.commitment_amount if spec.kind == KIND_COMMITMENT else None,
            acquired_nav=form.md_acquired_nav if (states_gross and spec.creating) else None,
            assumed_unfunded=(
                form.md_assumed_unfunded if (states_gross and spec.creating) else None
            ),
            vintage_year=form.md_vintage_year if (spec.creating and not unit_priced) else None,
            master_data=form.master_data(currency=currency) if spec.creating else None,
        )

    return _render(
        request,
        spec.recalc,
        {
            "csrf_token": session.csrf_token,
            "oob": True,
            "flow": form.flow,
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


# ---------------------------------------------------------------------------
# The blotter (S5, ADR-0128 §7)
# ---------------------------------------------------------------------------


async def _resolve_user_names(users: UserRepository, ids: Iterable[UUID]) -> dict[UUID, str]:
    """Resolve user ids to display names, one batch (the Journal idiom).

    The Cases ``_resolve_owner_names`` precedent, reused rather than
    re-invented (A-16): look each *distinct* id up and prefer
    ``display_name``, falling back to ``email`` so a station line reads as a
    person and never as a raw UUID. An id that resolves to nothing is simply
    absent, and the projection falls back to the stringified id.

    Args:
        users: The tenant-scoped user repository.
        ids: The actor ids to resolve, duplicates welcome.

    Returns:
        The names, keyed by id.
    """
    names: dict[UUID, str] = {}
    for user_id in set(ids):
        user = await users.get_by_id(user_id)
        if user is not None:
            names[user_id] = user.display_name or user.email
    return names


def _station_line(ticket: TradeTicketDTO, names: dict[UUID, str]) -> str | None:
    """Return the "by <name> · <date>" sub-line under a status chip (A-16).

    The station a ticket is *standing at*, not its whole history: ``approved``
    reads its own attribution rather than the proposal it passed through, so
    the line answers "who put it here" for the row as it is now. A ``draft``
    has no station and no line — nobody has yet said anything about it that
    another person could have seen.

    The cancel actor is deliberately unresolvable and deliberately not shown:
    there is no ``cancelled_by`` column (T-1 D-5), and deriving it from the
    audit log is a named successor rather than an S5 deliverable.
    """
    if ticket.status == STATUS_APPROVED and ticket.approved_at is not None:
        actor, when = ticket.approved_by, ticket.approved_at
    elif ticket.status == STATUS_PROPOSED and ticket.proposed_at is not None:
        actor, when = ticket.proposed_by, ticket.proposed_at
    else:
        return None
    who = names.get(actor, str(actor)) if actor is not None else "—"
    return f"by {who} · {when.date().isoformat()}"


def _amount_of(ticket: TradeTicketDTO) -> str | None:
    """Return the one figure a blotter row states for this ticket, or ``None``.

    ``net_amount`` is the cash the booking will move, and it is what every
    cash-moving flow is *about*. A commitment moves no cash (MD-19), so the
    figure that means something there is the commitment itself; showing a
    blank for it would suggest a ticket with no size.

    ``None`` — rendered "—" — is an honest answer and a common one: a draft
    is allowed to dangle (MD-11), and half of what the blotter lists has not
    been priced yet.
    """
    if ticket.net_amount is not None:
        return _money(ticket.net_amount)
    if ticket.kind == KIND_COMMITMENT and ticket.commitment_amount is not None:
        return _money(ticket.commitment_amount)
    return None


def _units_line(ticket: TradeTicketDTO) -> str | None:
    """Return the "n units @ p" sub-line, for the flows that have one.

    Order tickets only, and only once both halves are on the row: units and a
    price are what an order *is*, and the reported flows state a consideration
    instead (MD-15) which the amount column already carries.
    """
    if ticket.kind != KIND_ORDER or ticket.units is None or ticket.price_per_unit is None:
        return None
    return f"{_units(ticket.units)} units @ {_units(ticket.price_per_unit)}"


@router.get("/api/transactions/blotter", response_class=HTMLResponse)
async def get_blotter(
    request: Request,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """List the tickets in flight — the Blotter section's body (ADR-0128 §7).

    **In flight is the cancellable set**, and the route says so by loading
    :data:`~services.transactions.constants.CANCELLABLE_STATUSES` rather than
    by listing ``draft`` / ``proposed`` / ``approved`` again. The two are the
    same three statuses for a reason that is not coincidence: a ticket is on
    this list exactly while it is still a decision that can be withdrawn. A
    booked ticket is a fact and a cancelled one is a withdrawn decision;
    both are History's (P-5b).

    Every row is read-only here. The gestures the row offers — Open and the
    cancellation — are their own endpoints (A-12, A-13), so this handler
    stays a projection and the list can be re-fetched after any of them
    without re-deciding anything.

    Returns:
        The blotter table, newest ticket number first.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        tickets = await TradeTicketRepository(db).list_by_status(list(CANCELLABLE_STATUSES))
        investments = InvestmentRepository(db)
        names: dict[UUID, str] = {}

        async def _name(investment_id: UUID) -> str | None:
            # Memoised per request, the `_effect_rows` idiom: a blotter of
            # twenty orders against three positions is three reads, not
            # twenty.
            if investment_id not in names:
                found = await investments.get_by_id(investment_id)
                names[investment_id] = found.name if found is not None else "—"
            return names[investment_id]

        actors = await _resolve_user_names(
            UserRepository(db),
            [
                actor
                for ticket in tickets
                for actor in (ticket.proposed_by, ticket.approved_by)
                if actor is not None
            ],
        )
        rows: list[dict[str, Any]] = []
        for ticket in tickets:
            payload: dict[str, Any] = ticket.master_data or {}
            rows.append(
                {
                    "id": str(ticket.id),
                    "ticket_number": ticket.ticket_number,
                    "flow_label": _flow_label(ticket),
                    "investment_name": (
                        await _name(ticket.investment_id)
                        if ticket.investment_id is not None
                        else None
                    ),
                    # The name a creating flow carries on the ticket until
                    # booking makes the row (MD-12). It is the only name
                    # there is for these three flows, and it may legitimately
                    # be absent on a draft that has not reached Classify.
                    "creating_name": (
                        payload.get(MD_NAME) if ticket.investment_id is None else None
                    ),
                    "amount": _amount_of(ticket),
                    "currency": ticket.currency,
                    "units_line": _units_line(ticket),
                    "trade_date": ticket.trade_date.isoformat(),
                    "status": ticket.status,
                    "station_line": _station_line(ticket, actors),
                    "reason_required": ticket.status in CANCEL_REASON_REQUIRED_STATUSES,
                }
            )
        return _render(request, "_blotter.html", {"rows": rows})


async def _in_flight(db: AsyncSession, ticket_id: str) -> TradeTicketDTO:
    """Load one still-in-flight ticket, or 404 — the gate all three routes share.

    "In flight" is
    :data:`~services.transactions.constants.CANCELLABLE_STATUSES`, and the
    resume ``GET``, the cancel panel and the cancel ``POST`` all mean the same
    thing by it: a ticket that is still a decision rather than a fact. Written
    once so the three cannot come to disagree — a resume that opened a booked
    ticket would offer gestures the service then refuses in a red block the
    operator can do nothing about.

    A malformed id and an absent one are the same answer on purpose. The
    difference is only ever interesting to whoever typed the URL, and telling
    them apart would confirm to an unauthenticated prober which ids exist.

    The service re-checks the status on the way through
    :meth:`~services.transactions.ticket_service.TicketService.cancel`: this
    is which *surface* may be shown, not whether a write is allowed.

    Args:
        db: The tenant-scoped session.
        ticket_id: The id as it arrived in the path.

    Returns:
        The ticket.

    Raises:
        HTTPException: 404 for a malformed id, an id this tenant cannot see,
            or a ticket that has left the in-flight set.
    """
    wanted = _uuid_or_none(ticket_id)
    ticket = await TradeTicketRepository(db).get(wanted) if wanted is not None else None
    if ticket is None or ticket.status not in CANCELLABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No trade ticket {ticket_id} in this tenant.",
        )
    return ticket


@router.get("/api/transactions/ticket/{ticket_id}", response_class=HTMLResponse)
async def get_ticket(
    request: Request,
    ticket_id: str,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Reopen one in-flight ticket in the composer it was written in (A-12).

    **One address for five flows.** The blotter row's Open gesture does not
    know which composer answers it, and that is the point: the row would
    otherwise have to carry routing knowledge that :data:`_FLOWS` already
    holds, and a sixth flow would mean teaching the list about it. The
    reverse lookup is :func:`_flow_of`, the single one.

    The wizard is reached through :func:`_render_wizard` rather than
    reproduced, so this endpoint and
    ``GET /api/transactions/wizard?ticket_id=`` cannot drift.

    In-flight only. A booked or cancelled ticket is not editable and is not
    404 by accident: History shows what it did (P-5b), and opening a composer
    over it would offer gestures the service would then refuse in a red block
    the operator could do nothing about.

    Args:
        request: The live request.
        ticket_id: The ticket to reopen.
        session: The authenticated session.

    Returns:
        The composer, or the wizard at its resume step.

    Raises:
        HTTPException: 404 if the id is malformed, names no ticket this
            tenant can see, or names one that has left the in-flight set.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        ticket = await _in_flight(db, ticket_id)
        flow = _flow_of(ticket)
        if _FLOWS[flow].composer is None:
            return await _render_wizard(request, session=session, db=db, ticket=ticket)
        form = _form_from_ticket(ticket, csrf_token=session.csrf_token, flow=flow)
        context = await _composer_context(db, session=session, form=form, ticket=ticket, flow=flow)
        return _render(request, _composer_template(flow), context)


def _cancel_context(
    ticket: TradeTicketDTO, *, csrf_token: str, error: str | None
) -> dict[str, Any]:
    """Build the inline cancel panel's context (A-13).

    The panel is the reason step, and it needs exactly three things: which
    ticket, whether a reason is required, and what the last attempt said. The
    *copy* is the template's — the M-5 register fixes a lead and a sub per
    status — while ``error`` is always a service sentence rendered verbatim
    (A-7).
    """
    return {
        "id": str(ticket.id),
        "ticket_number": ticket.ticket_number,
        "status": ticket.status,
        "reason_required": ticket.status in CANCEL_REASON_REQUIRED_STATUSES,
        "csrf_token": csrf_token,
        "error": error,
    }


@router.get("/api/transactions/ticket/{ticket_id}/cancel", response_class=HTMLResponse)
async def get_cancel_panel(
    request: Request,
    ticket_id: str,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Open the inline reason step beneath a blotter row (A-13).

    A panel in the row's own detail cell, never a modal: the ticket the
    operator is about to withdraw stays on screen above the question, and the
    list behind it stays readable.

    Returns:
        The cancel panel.

    Raises:
        HTTPException: 404 for an id that is not an in-flight ticket.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        ticket = await _in_flight(db, ticket_id)
        return _render(
            request,
            "_cancel_panel.html",
            _cancel_context(ticket, csrf_token=session.csrf_token, error=None),
        )


@router.post(
    "/api/transactions/ticket/{ticket_id}/cancel",
    response_class=HTMLResponse,
    dependencies=[Depends(require_role("owner"))],
)
async def post_cancel(
    request: Request,
    ticket_id: str,
    reason: Annotated[str, Form()] = "",
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Cancel or discard one in-flight ticket, with a reason where one is owed.

    **Success re-renders the whole blotter.** The row is gone, and the list
    the server returns is the answer — not a client-side removal that would
    be this surface's own opinion about what the book now says. It also picks
    up whatever else moved while the panel was open.

    A refusal comes back as the panel, carrying the service's sentence
    verbatim (A-7): ``TicketIncomplete`` when a ``proposed`` or ``approved``
    ticket was sent without a reason, ``TicketStateInvalid`` when the ticket
    left the cancellable set between the panel opening and the button — the
    one race this surface has, and the service is the authority on it.

    Args:
        request: The live request.
        ticket_id: The ticket to cancel.
        reason: Why. Required from ``proposed`` and ``approved``; the service
            decides, not this handler.
        session: The authenticated session.

    Returns:
        The re-rendered blotter on success, the panel on a refusal.

    Raises:
        HTTPException: 404 for an id that is not an in-flight ticket.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        ticket = await _in_flight(db, ticket_id)
        try:
            await _build_ticket_service(db).cancel(
                ticket.id,
                cancelled_by=session.user_id,
                now=_now(),
                reason=_clean(reason),
            )
        except TicketNotFound as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except (TicketIncomplete, TicketStateInvalid) as exc:
            return _render(
                request,
                "_cancel_panel.html",
                _cancel_context(ticket, csrf_token=session.csrf_token, error=str(exc)),
            )
    return await get_blotter(request, session=session)
