# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Emission engine for booked trade tickets (ADR-0128 §2).

Where :mod:`services.transactions.ticket_service` decides *whether* a ticket
may book, this module decides *what a booking writes*. The split matters
because the two answer to different pressures: the policy seam grows a rule
per flow, while the emission stays a small, mostly-pure derivation from the
ticket's columns to ledger rows — and a derivation that can be read in one
sitting is the only kind that can be audited against a statement.

What emission means here
------------------------
An ``order``-kind ticket (U-BUY / U-SELL) settles as **two ledger rows** —
the instrument leg and its cash leg — written **atomically** (ADR-0128 §2).
Atomicity is structural rather than defended: every write goes through the
caller's one context-scoped session, nothing in the chain commits, and this
module contains no ``try``/``except`` at all. A failure anywhere — a CHECK,
a guard, a completeness refusal raised *after* the first leg is written —
propagates out of :func:`emit_order`, and the ``tenant_context`` block the
caller opened rolls the partial write back. There is no half-booked state to
clean up because there is no state until the block exits.

The single sanctioned seam (D-A)
--------------------------------
Every ledger row is written through
:meth:`services.investments.investment_service.InvestmentService.add_position_transaction`
— never through :class:`~core.repositories.position_transaction_repository.PositionTransactionRepository`
directly. That seam carries two things this module must not restate: the
ADR-0130 non-negativity decision (the instrument leg is guarded, a cash
target is exempt — see below) and the ADR-0098 §3 computed-NAV
materialisation trigger, which fires once per unitised leg in the same
transaction. The repository is still passed in, for *reads* only: the
holdings check behind MD-7 needs the ledger as it stands after the
instrument leg.

ADR-0130 end to end
-------------------
A U-BUY may take the settlement position below zero and still book. Nothing
in this module implements that — it is the decision the write seam already
carries, per *target* rather than per *caller*, which is exactly why there
is no flag to pass and no branch here to get wrong. The composer is warned
(``negative_cash``) and the book records the overdraft, because a negative
cash balance is an economic fact rather than an impossible state. The
instrument leg keeps the guard unconditionally: you cannot sell units you do
not hold.

Sign conventions (working document §2.1 / §2.2, D-B / D-C)
----------------------------------------------------------
:func:`services.transactions.validation.derive_cash_effect` yields a
*magnitude*: what the cash position gives up on a buy and receives on a
sell. Direction is applied here, once, and the **cash leg's direction
follows the sign of the cash effect, not the ticket's** (D-B) — a sell whose
costs exceed its gross moves cash *out*, and the ledger must say so. The
signed effect is recorded as the instrument leg's ``consideration`` (D-C);
the cash leg carries none, because a cash row's cash effect is its units at
1.0000 and restating it would be a second place for the same number to be
wrong.

Both legs book at ``trade_date``. ``settlement_date`` is informational in v1
(MD-4) and reaches no ledger row.

Provenance (D-D)
----------------
Both rows carry ``ingest_origin='manual'`` (ADR-0128 Q-1: a ticket booking is
a human act, not an ingest) and ``source = "ticket #<n>"``, which is what
makes a ledger row traceable back to the decision that produced it without
the ledger knowing what a ticket is. ``ticket.source`` — the composer's
free-text provenance for the *ticket* — is deliberately not copied down.

Creating flows: the ``investments`` row is an effect (MD-12, D-I)
-----------------------------------------------------------------
U-NEW, R-COMMIT and R-SEC-BUY have no ``investments`` row before booking —
the master data rides on the ticket as an opaque payload, and the row is
*emitted* like any ledger line. The ADR-0128 §2 effect vocabulary has no
``investment_create`` and gains none: creation is recorded as an
``investment_update`` whose ``prior_state`` is ``NULL``. So

* ``investment_update`` with ``prior_state IS NULL`` ⇔ this booking
  **created** the row, and a reversal deletes it;
* ``investment_update`` with a dict ⇔ this booking **updated** an existing
  row, and a reversal restores the before-image (D-H).

One effect type, one field, two states — a reversal reads the difference off
the row it is already loading, rather than off a fourth vocabulary entry that
would then have to be kept in step with a fifth. **S2c depends on this
encoding.**

Identifiers ride with the created row (D-L)
-------------------------------------------
A creating payload's ``identifier_scheme`` / ``identifier_value`` (and the
resolved ``figi`` beside them) are written as ``investment_identifiers`` rows
at creation, and are deliberately **not** effects:
``investment_identifiers.investment_id`` is ``ON DELETE CASCADE``, so they
vanish with the shell a reversal deletes, and enumerating them would record
rows that cannot outlive their owner. The identifier repository must be
wired — an unwired service raises rather than skipping quietly, because a
security master that silently lost its ISIN is worse than a booking that
refused.

The ticket learns its investment (D-T)
--------------------------------------
Creation writes ``investment_id`` back onto the ticket through
:meth:`~core.repositories.trade_ticket_repository.TradeTicketRepository.link_investment`.
``update_draft`` cannot serve — it is draft-only, and a ticket being booked
is commonly ``proposed`` or ``approved``. Note for S2c:
``trade_tickets.investment_id`` is ``ON DELETE RESTRICT``, so a reversal must
**unlink** before it can delete a created shell.

R-SEC-SELL is unconditional (MD-17, D-S)
----------------------------------------
A secondary sale is always a full disposal: the schema carries no fraction
column (MD-18), so there is no partial to distinguish. NAV → 0 and
inactivation are therefore *inherent* to the flow rather than options on it,
and ``set_inactive`` is not consulted at all on this path — the U-SELL
checkbox (MD-7, D-E) answers a question R-SEC-SELL never asks.

Non-ledger rows (D-W)
---------------------
Cashflows book at **noon UTC** on the trade date, the Excel extractor's
convention, so a ticket-emitted flow sorts among imported ones instead of on
a midnight boundary; they carry :func:`provenance` as their ``description``.
NAV rows are ``nav_kind='actual'`` with :func:`provenance` as ``source``.
Both are stamped ``ingest_origin='manual'`` by the write seam. Distribution
amounts are **positive**, following the codebase-wide convention (calls
negative, distributions positive); a sale whose costs exceed its proceeds
writes its negative net as it stands — the ``net_non_positive`` warning has
already fired, and the cash leg follows D-B onto the other side.

What each flow emits
--------------------
=============  =============================================================
Flow           Effects, by ``effect_type``
=============  =============================================================
U-BUY/U-SELL   ``position_txn`` ×2 (instrument, cash); plus
               ``investment_update`` (before-image) when MD-7 applies
U-NEW          ``investment_update`` (``prior_state`` NULL), ``position_txn``
               ×2
R-COMMIT       ``investment_update`` (``prior_state`` NULL) — and nothing
               else: no cash leg, no NAV row (MD-19)
R-SEC-BUY      ``investment_update`` (``prior_state`` NULL), ``nav``,
               ``position_txn`` (cash)
R-SEC-SELL     ``cashflow``, ``nav``, ``investment_update`` (before-image),
               ``position_txn`` (cash)
=============  =============================================================

A cash effect of exactly zero emits no ``position_txn`` on any flow
(:func:`cash_leg`), so the cash row is the one entry above that a legitimate
ticket can lack.

Reversal (S2c)
--------------
The other half of ADR-0128 §6, in the same module because it is the same
subject read backwards: a booking's effects are only reversible if something
knows what they meant, and that something is the encoding above.

* **D-Y — the audit log, not ``updated_at``.** An effect is *modified* iff
  the audit engine records an ``UPDATE`` on its row strictly after the
  effect's ``emitted_at``, and *consumed* iff the row is gone. ``updated_at``
  cannot serve: ``position_transactions.update`` / ``update_opening`` write
  by ORM assignment with no ``onupdate`` and no trigger behind the column, so
  it still reads as the insert time after an edit. The audit trigger fires on
  every path. Both ``audit_log.created_at`` and ``emitted_at`` are ``NOW()``
  — the *transaction* timestamp — so the booking's own writes tie rather than
  exceed, and the strict comparison excludes them exactly.
* **D-Z — one typed error names the offending effect.**
  :class:`~core.exceptions.TicketReversalBlocked` carries ``effect_type``,
  ``effect_id`` and a ``cause`` from
  :data:`~services.transactions.constants.REVERSAL_CAUSES`. Every effect is
  checked before any row is deleted (:func:`check_effects_untouched`), the
  all-blocks-first order ``propose`` already uses.
* **D-AA — deletes go through** :class:`~services.investments.investment_service.InvestmentService`,
  the same seam the emission writes through, so the ADR-0097 §4 re-check
  (with ADR-0130's cash exemption), the ADR-0098 materialisation rerun and
  the ADR-0103 §6 cash-plan recompute all happen without being restated here.
  The one ``except`` in the path translates the ledger's
  ``NonNegativeHoldingsError`` into ``holdings_consumed`` — units sold on
  since the booking — chained, never swallowed.
* **D-AB — restore, do not roll back.** ``prior_state`` records the whole row
  (D-H); :func:`restore_from_before_image` writes back only ``is_active``,
  the one field an emission can change, and refuses if any other field has
  moved.
* **D-AC — created shells: platform rows cascade, user rows retain.**
  :func:`cleanup_new_investment_shell` deletes the ``investment_update`` /
  ``prior_state IS NULL`` row when only platform artefacts remain
  (``instrument_prices``, ``'system'`` NAVs, ``watchpoints``, the D-L
  identifiers), unlinking the ticket first because
  ``trade_tickets.investment_id`` is ``ON DELETE RESTRICT``. Otherwise it
  retains the shell, deactivated, and the outcome says which table kept it.
* **D-AD — effect rows survive.** A reversal never deletes from
  ``trade_ticket_effects``. The rows are FK-less by design (T-1 §3) so the
  history of what a cancelled ticket once did outlives the rows it did it to.

Scope
-----
Emission and its reversal. What may reverse, and when, is policy and lives in
:meth:`services.transactions.ticket_service.TicketService.reverse`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import date as _date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from core.exceptions import (
    NonNegativeHoldingsError,
    TicketIncomplete,
    TicketReversalBlocked,
)
from core.repositories.audit_log_repository import AuditLogRepository
from core.repositories.investment_cashflow_repository import InvestmentCashflowRepository
from core.repositories.investment_nav_repository import InvestmentNavRepository
from core.repositories.investment_repository import InvestmentDTO, InvestmentRepository
from core.repositories.position_transaction_repository import (
    PositionTransactionRepository,
)
from core.repositories.trade_ticket_repository import (
    EffectInput,
    TradeTicketDTO,
    TradeTicketEffectDTO,
    TradeTicketRepository,
)
from services.investments.holdings import holdings_as_of
from services.investments.investment_service import InvestmentService
from services.transactions.constants import (
    BLOCK_NAV_EXISTS_AT_TRADE_DATE,
    DIRECTION_BUY,
    DIRECTION_SELL,
    INCOMPLETE_COMMITMENT_SHAPE,
    INCOMPLETE_MISSING_INVESTMENT,
    INCOMPLETE_MISSING_MASTER_DATA,
    INCOMPLETE_SET_INACTIVE_NOT_FULL_DISPOSAL,
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
    REVERSAL_CAUSE_CONSUMED,
    REVERSAL_CAUSE_HOLDINGS_CONSUMED,
    REVERSAL_CAUSE_MODIFIED,
    REVERSAL_CAUSE_UNRESTORABLE,
)

#: The unit price of cash. Cash positions are unitised at 1.0000 (F-2,
#: ADR-0103), which is what makes a cash balance *be* its holdings and lets
#: the same pure derivation serve both kinds of position.
CASH_UNIT_PRICE: Decimal = Decimal("1.0000")

#: The ``ingest_origin`` every ticket-emitted ledger row carries (ADR-0128
#: Q-1). A booking is a deliberate human write, so a later Excel re-import
#: overwrites it as book of record but a live market-data fetch does not.
EMISSION_INGEST_ORIGIN: str = "manual"

#: The ``effect_type`` values this module emits (ADR-0128 §2 vocabulary).
EFFECT_POSITION_TXN: str = "position_txn"
EFFECT_INVESTMENT_UPDATE: str = "investment_update"
EFFECT_CASHFLOW: str = "cashflow"
EFFECT_NAV: str = "nav"

#: How a secondary sale's proceeds book (MD-17, ADR-0128 Q-3): as an ordinary
#: realised distribution on the investment being sold, not as a bespoke
#: disposal type. The post-trade analytics then treat a secondary exit exactly
#: as they treat a GP-driven one, which is what makes the two comparable.
FLOW_TYPE_DISTRIBUTION: str = "distribution"
FLOW_KIND_ACTUAL: str = "actual"

#: NAV rows a booking writes are always realised, never projections.
NAV_KIND_ACTUAL: str = "actual"

#: The time of day every ticket-emitted cashflow books at (D-W). Noon UTC is
#: the Excel extractor's convention, so a ticket-emitted flow sorts among
#: imported ones rather than on a midnight boundary where a timezone shift
#: would move it across a day.
FLOW_TIME_OF_DAY: time = time(12, 0)

#: The two ``investments.valuation_mode`` values (ADR-0097 §1). Creating flows
#: state one explicitly (D-R) rather than inheriting the column default: a
#: fund recorded from a statement is ``reported``, a unit-dealt instrument is
#: ``unitised``, and which one a ticket creates is a property of its flow.
VALUATION_MODE_REPORTED: str = "reported"
VALUATION_MODE_UNITISED: str = "unitised"

#: The ``investment_identifiers.scheme`` a resolved FIGI is written under
#: (D-L). Named separately from :data:`~services.transactions.constants.MD_FIGI`
#: even though the two strings coincide: one is a payload key, the other a
#: member of ``core.models.investment_identifier.IDENTIFIER_SCHEMES``, and
#: conflating two vocabularies because they agree today is how they come to
#: disagree tomorrow.
IDENTIFIER_SCHEME_FIGI: str = "figi"


@dataclass(frozen=True)
class LegSpec:
    """One ledger row a booking will write, fully derived and not yet written.

    The intermediate that makes the sign and provenance rules testable
    without a database: :func:`order_legs` produces these purely, and
    :func:`emit_order` does nothing but hand them to the write seam. A bug in
    the sign convention is therefore a failing unit test rather than a
    corrupted book.

    ``trade_date`` is deliberately **absent**. Both legs of an order book on
    the ticket's ``trade_date`` (working document §4.1) — carrying it per leg
    would invite a future caller to set them apart, and a two-leg settlement
    split across two dates is not a settlement.

    Attributes:
        investment_id: The position the row lands on — the traded instrument
            or the settlement cash position.
        txn_type: ``buy`` or ``sell``, in the ledger's vocabulary. On the
            cash leg this follows the sign of the cash effect (D-B), which is
            not always the ticket's direction.
        units: Signed, per ``ck_position_transactions_sign``.
        price_per_unit: The execution price; :data:`CASH_UNIT_PRICE` on a
            cash leg.
        consideration: The signed cash effect on the instrument leg, ``None``
            on the cash leg (D-C).
        currency: The ticket's currency, which validation has already proved
            equal to both positions' (F-3, ADR-0097 §5).
        note: The ticket's note, passed through unchanged.
        source: The ticket's provenance string, per :func:`provenance`.
    """

    investment_id: UUID
    txn_type: str
    units: Decimal
    price_per_unit: Decimal
    consideration: Decimal | None
    currency: str
    note: str | None
    source: str


def provenance(ticket: TradeTicketDTO) -> str:
    """Return the ``source`` string a ticket stamps on the rows it emits.

    The one place the format is written. ``position_transactions.source`` is
    free text that nothing parses — the existing producers write
    ``'excel-import'`` and ``'excel-import:cash-statement'`` — so this is a
    human-readable trace, not a join key. The machine-readable linkage is
    ``trade_ticket_effects`` (ADR-0128 §2), which is what a reversal walks.

    Args:
        ticket: The booking ticket.

    Returns:
        ``"ticket #<ticket_number>"``, using the tenant-sequential number the
        operator actually sees rather than the UUID they never do.
    """
    return f"ticket #{ticket.ticket_number}"


def order_legs(
    ticket: TradeTicketDTO,
    *,
    cash_effect: Decimal,
) -> tuple[LegSpec, LegSpec | None]:
    """Derive the instrument leg and its cash leg from an order ticket.

    Pure: no I/O, no clock, no session. The whole sign convention of the
    working document §2.1 / §2.2 lives in these few lines and nowhere else.

    The instrument leg is the ticket read literally — ``units`` signed by the
    direction, at the execution price. The cash leg is the *consequence*, and
    its direction is decided by the sign of the cash effect rather than by
    the ticket's direction (D-B): a sale whose fees exceed its gross proceeds
    takes money out of the settlement position, and a ledger that recorded
    that as an inflow would be lying about a real, if unusual, trade.

    A cash effect of exactly zero produces **no cash leg**. This is not a
    rounding accommodation but a schema fact: ``ck_position_transactions_sign``
    admits no zero-unit ``buy`` or ``sell``, and an event that moves no cash
    is not an event. The ``net_non_positive`` warning has already fired.

    Args:
        ticket: A complete order ticket. Completeness is the service's
            responsibility and has already been established.
        cash_effect: The magnitude from
            :func:`services.transactions.validation.derive_cash_effect` —
            what the cash position gives up on a buy, receives on a sell.

    Returns:
        ``(instrument_leg, cash_leg)``; ``cash_leg`` is ``None`` when the
        ticket moves no cash.

    Raises:
        ValueError: If the ticket is not a complete order ticket. These are
            programmer errors, not user errors: every one of them is
            unreachable behind ``_block_completeness``, and reporting them as
            domain errors would invite a caller to handle what it should
            have prevented.
    """
    if ticket.kind != KIND_ORDER:
        raise ValueError(f"order_legs is for {KIND_ORDER!r} tickets, not {ticket.kind!r}.")
    if ticket.investment_id is None:
        raise ValueError("An order ticket reaching emission must name an investment.")
    if ticket.cash_investment_id is None:
        raise ValueError("An order ticket reaching emission must name a settlement position.")
    if ticket.units is None or ticket.price_per_unit is None:
        raise ValueError("An order ticket reaching emission must carry units and a price.")

    selling = ticket.direction == DIRECTION_SELL
    instrument = LegSpec(
        investment_id=ticket.investment_id,
        txn_type=ticket.direction,
        units=-ticket.units if selling else ticket.units,
        price_per_unit=ticket.price_per_unit,
        # D-C: one signed number serves both legs — it is the instrument
        # leg's `consideration` and the cash leg's `units`, because the cash
        # the trade moves and the cash the position receives are the same
        # fact seen from two rows. :func:`cash_leg` derives it identically.
        consideration=_signed_cash_effect(ticket, cash_effect),
        currency=ticket.currency,
        note=ticket.note,
        source=provenance(ticket),
    )
    return instrument, cash_leg(ticket, cash_effect=cash_effect)


def _signed_cash_effect(ticket: TradeTicketDTO, cash_effect: Decimal) -> Decimal:
    """Apply the ticket's direction to the cash magnitude (D-B).

    A buy spends and a sell receives, so the sign is the direction's — and
    it is applied in exactly this one place, which is what lets
    :func:`order_legs` and :func:`cash_leg` be read as two views of one
    number rather than as two derivations that happen to agree today.
    """
    return cash_effect if ticket.direction == DIRECTION_SELL else -cash_effect


def cash_leg(ticket: TradeTicketDTO, *, cash_effect: Decimal) -> LegSpec | None:
    """Derive the settlement leg any cash-moving ticket writes. Pure.

    Every cash-moving flow settles the same way — an order, a secondary
    purchase and a secondary sale all move money into or out of one cash
    position at 1.0000 — so the derivation is stated once here and reused,
    rather than restated per flow where three copies could drift into three
    sign conventions.

    The **direction follows the sign of the cash effect, not the ticket's**
    (D-B): a sale whose fees exceed its gross proceeds takes money out of the
    settlement position, and a ledger recording that as an inflow would be
    lying about a real, if unusual, trade.

    A cash effect of exactly zero produces **no leg**. Not a rounding
    accommodation but a schema fact: ``ck_position_transactions_sign`` admits
    no zero-unit ``buy`` or ``sell``, and an event that moves no cash is not
    an event. The ``net_non_positive`` warning has already fired.

    Args:
        ticket: A cash-moving ticket whose settlement position is confirmed.
        cash_effect: The magnitude from
            :func:`services.transactions.validation.derive_cash_effect`.

    Returns:
        The cash leg, or ``None`` when the ticket moves no cash.

    Raises:
        ValueError: If the ticket names no settlement position — a
            programmer error, unreachable behind ``_require_settlement_position``.
    """
    if ticket.cash_investment_id is None:
        raise ValueError("A cash-moving ticket reaching emission must name a settlement position.")

    signed_effect = _signed_cash_effect(ticket, cash_effect)
    if signed_effect == 0:
        return None

    return LegSpec(
        investment_id=ticket.cash_investment_id,
        txn_type=DIRECTION_BUY if signed_effect > 0 else DIRECTION_SELL,
        units=signed_effect,
        price_per_unit=CASH_UNIT_PRICE,
        # D-C: a cash row's cash effect *is* its units at 1.0000. Restating
        # it here would be a second place for one number to go wrong.
        consideration=None,
        currency=ticket.currency,
        note=ticket.note,
        source=provenance(ticket),
    )


# ---------------------------------------------------------------------------
# Master data (MD-12 / MD-15, D-V)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MasterData:
    """A creating ticket's master-data payload, parsed into domain types.

    The payload is JSONB, so everything in it arrives as a string, an int or
    a float — a date is text, an amount is text, a foreign key is text. This
    dataclass is what the payload *means*, produced by the one parser
    (:func:`parse_master_data`) that both the validation seam and the
    emission run. That single interpretation is the point: a second reading
    of ``master_data`` somewhere else is how a ticket comes to validate
    against one set of values and book another.

    Only ``name`` / ``investment_type`` / ``asset_class_id`` / ``currency``
    are required — they are the four columns an ``investments`` row is
    ``NOT NULL`` in (decision record §2.5, D-J). Everything else is optional
    because the flows disagree about which of it applies: a commitment has a
    vintage year and no acquired NAV, a secondary purchase has both, a
    new-instrument order has neither.

    Attributes:
        name: The tenant-unique investment name.
        investment_type: One of the eight CHECK-allowed discriminators. The
            membership check is the service's, not the parser's — this is a
            conversion, and which values are legal is policy.
        asset_class_id: The asset-class FK. Existence is **not** checked
            here: the service has no catalogue repository, the S4 picker
            offers only real rows, and a bad FK fails loudly at the DB.
        currency: The investment's currency, which the ticket's must equal.
        anlv_code: The AnlV category; the MD-21 gate checks its presence.
        identifier_scheme: The scheme of the identifier to write (D-L).
        identifier_value: That identifier's value.
        figi: The resolved FIGI, written as a second identifier row.
        manager: The GP / fund-manager name.
        region: The geographic region label.
        vintage_year: The vintage year.
        commitment_amount: R-COMMIT's commitment as stated in the payload;
            reconciled against the ticket column by
            :func:`reconcile_commitment`.
        purchase_price: R-SEC-BUY's price, carried for the record — the
            ticket's ``gross_amount`` is what the cash leg uses.
        acquired_nav: The stake's value at transfer; R-SEC-BUY's opening NAV.
        assumed_unfunded: The unfunded commitment assumed with a secondary
            stake; becomes the created row's ``commitment_amount`` (D-U).
    """

    name: str
    investment_type: str
    asset_class_id: UUID
    currency: str
    anlv_code: str | None = None
    identifier_scheme: str | None = None
    identifier_value: str | None = None
    figi: str | None = None
    manager: str | None = None
    region: str | None = None
    vintage_year: int | None = None
    commitment_amount: Decimal | None = None
    purchase_price: Decimal | None = None
    acquired_nav: Decimal | None = None
    assumed_unfunded: Decimal | None = None


def parse_master_data(payload: Mapping[str, object]) -> MasterData:
    """Parse a creating ticket's JSONB payload into :class:`MasterData`. Pure.

    The **one** interpretation of ``trade_tickets.master_data`` (D-V). Both
    the propose/book validation and the emission call it, so a payload that
    validates is a payload that books — there is no second reading in which
    an amount is a float here and a string there.

    A key that is present but unusable is refused with the same identifier as
    one that is absent, because from the composer's point of view they are
    the same failure: the master data cannot build an ``investments`` row.
    The message names the offending key, which is what a caller actually
    needs — the identifier is for copy, the key is for debugging.

    Args:
        payload: The ticket's ``master_data``, or an empty mapping.

    Returns:
        The parsed master data.

    Raises:
        TicketIncomplete: With ``identifier='missing_master_data'`` if a
            required key is absent, or any key present cannot be converted.
    """
    return MasterData(
        name=_required_text(payload, MD_NAME),
        investment_type=_required_text(payload, MD_INVESTMENT_TYPE),
        asset_class_id=_required_uuid(payload, MD_ASSET_CLASS_ID),
        currency=_required_text(payload, MD_CURRENCY),
        anlv_code=_optional_text(payload, MD_ANLV_CODE),
        identifier_scheme=_optional_text(payload, MD_IDENTIFIER_SCHEME),
        identifier_value=_optional_text(payload, MD_IDENTIFIER_VALUE),
        figi=_optional_text(payload, MD_FIGI),
        manager=_optional_text(payload, MD_MANAGER),
        region=_optional_text(payload, MD_REGION),
        vintage_year=_optional_year(payload, MD_VINTAGE_YEAR),
        commitment_amount=_optional_amount(payload, MD_COMMITMENT_AMOUNT),
        purchase_price=_optional_amount(payload, MD_PURCHASE_PRICE),
        acquired_nav=_optional_amount(payload, MD_ACQUIRED_NAV),
        assumed_unfunded=_optional_amount(payload, MD_ASSUMED_UNFUNDED),
    )


def _payload_error(key: str, expected: str) -> TicketIncomplete:
    """Build the one refusal every payload-conversion failure raises."""
    return TicketIncomplete(
        f"Master-data key {key!r} must be {expected}. This flow creates the "
        "investment at booking (MD-12, decision record §2.5), so its payload "
        "is the row.",
        identifier=INCOMPLETE_MISSING_MASTER_DATA,
        field="master_data",
    )


def _is_blank(value: object) -> bool:
    """Whether an optional payload entry is absent, in JSONB's several spellings."""
    return value is None or (isinstance(value, str) and not value.strip())


def _required_text(payload: Mapping[str, object], key: str) -> str:
    """Return a required text entry, trimmed."""
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _payload_error(key, "present and a non-empty string")
    return value.strip()


def _optional_text(payload: Mapping[str, object], key: str) -> str | None:
    """Return an optional text entry, trimmed, or ``None``."""
    value = payload.get(key)
    if _is_blank(value):
        return None
    if not isinstance(value, str):
        raise _payload_error(key, "a string when present")
    return value.strip()


def _required_uuid(payload: Mapping[str, object], key: str) -> UUID:
    """Return a required UUID entry; JSONB stores it as text."""
    value = payload.get(key)
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str) or not value.strip():
        raise _payload_error(key, "present and a UUID")
    try:
        return UUID(value.strip())
    except ValueError as exc:
        raise _payload_error(key, "a UUID") from exc


def _optional_year(payload: Mapping[str, object], key: str) -> int | None:
    """Return an optional whole-year entry, or ``None``."""
    value = payload.get(key)
    if _is_blank(value):
        return None
    try:
        return int(str(value).strip())
    except ValueError as exc:
        raise _payload_error(key, "a whole year") from exc


def _optional_amount(payload: Mapping[str, object], key: str) -> Decimal | None:
    """Return an optional decimal entry, or ``None``.

    ``Decimal(str(value))`` rather than ``Decimal(value)`` so a float that
    reached the payload converts through its repr instead of its binary
    expansion — ``0.1`` becomes ``Decimal('0.1')``, not fifty-five digits.
    """
    value = payload.get(key)
    if _is_blank(value):
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise _payload_error(key, "a decimal amount") from exc


def reconcile_commitment(ticket: TradeTicketDTO, *, master: MasterData) -> Decimal | None:
    """Return the ``commitment_amount`` a creating flow's new row carries (D-U).

    The two creating flows that record a commitment disagree about where it
    lives, and both spellings are legitimate:

    * **R-COMMIT** — the commitment *is* the ticket. ``commitment_amount`` is
      the column S1's completeness already requires, and the payload's copy,
      if the composer wrote one, is a mirror.
    * **R-SEC-BUY** — the commitment is the *unfunded* part of the stake being
      assumed (MD-15's inventory), so it lives in the payload; the ticket
      column normally stays NULL.

    Where both are stated they must agree. A silent precedence rule would let
    a composer bug ship a fund whose commitment is off by whichever number
    lost, and a commitment is the denominator of every pacing and coverage
    figure the platform draws.

    U-NEW records no commitment: a listed instrument has none.

    Args:
        ticket: The creating ticket.
        master: Its parsed payload.

    Returns:
        The commitment for the new row, or ``None``.

    Raises:
        TicketIncomplete: With ``identifier='commitment_shape'`` if the ticket
            column and the payload state different amounts.
    """
    if ticket.kind == KIND_COMMITMENT:
        stated = master.commitment_amount
        if stated is not None and stated != ticket.commitment_amount:
            raise _commitment_disagreement(ticket.commitment_amount, stated, MD_COMMITMENT_AMOUNT)
        return ticket.commitment_amount

    if ticket.kind == KIND_SECONDARY:
        assumed = master.assumed_unfunded
        if ticket.commitment_amount is not None and ticket.commitment_amount != assumed:
            raise _commitment_disagreement(ticket.commitment_amount, assumed, MD_ASSUMED_UNFUNDED)
        return assumed

    return None


def _commitment_disagreement(
    column: Decimal | None,
    payload: Decimal | None,
    key: str,
) -> TicketIncomplete:
    """Build the refusal for a ticket and its payload stating two commitments."""
    return TicketIncomplete(
        f"The ticket states a commitment of {column} but its master data's "
        f"{key!r} states {payload}; a commitment is the denominator of every "
        "pacing and coverage figure, so the two must agree before booking "
        "(D-U).",
        identifier=INCOMPLETE_COMMITMENT_SHAPE,
        field="commitment_amount",
    )


def investment_before_image(dto: InvestmentDTO) -> dict[str, Any]:
    """Return a JSON-safe before-image of an investment row (D-H).

    The ``prior_state`` an ``investment_update`` effect carries, and the only
    thing a reversal has to restore from — so it records the **whole** row
    rather than the field the booking happened to touch. Recording only the
    changed field would make the effect table's usefulness depend on the
    emission remembering to widen it, which is precisely the coupling
    ADR-0128 §2 avoids by enumerating effects rather than describing them.

    The transform is total and lossless-for-restoration: ``UUID``,
    ``Decimal``, ``date`` and ``datetime`` become strings (ISO-8601 for the
    temporal pair), containers are converted recursively, and ``None`` stays
    ``None``. Everything survives ``json.dumps`` because the destination is a
    JSONB column, and ``Decimal`` in particular goes to *string* rather than
    float so a restored amount is the amount that was stored.

    ``datetime`` is tested before ``date`` deliberately — it is a subclass,
    and the reverse order would silently truncate every timestamp to a day.

    Args:
        dto: The investment as it stands *before* the booking's update.

    Returns:
        A plain dict keyed by the DTO's field names.
    """
    return {field.name: _json_safe(getattr(dto, field.name)) for field in fields(dto)}


def _json_safe(value: Any) -> Any:
    """Convert one value to its JSON-safe form; see :func:`investment_before_image`."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, _date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _json_safe(getattr(value, field.name)) for field in fields(value)}
    return str(value)


async def emit_order(
    ticket: TradeTicketDTO,
    *,
    investment_service: InvestmentService,
    position_transactions: PositionTransactionRepository,
    investments: InvestmentRepository,
    booked_by: UUID,
    cash_effect: Decimal,
) -> list[EffectInput]:
    """Write an order ticket's ledger rows and return the effects they are.

    The order of the writes is load-bearing. The **instrument leg goes
    first** so that its ADR-0097 §4 guard fires before any cash row exists:
    were the cash leg written first, an oversell would still be refused, but
    the rollback would be doing work the ordering makes unnecessary. In
    practice ``_block_oversell`` has already refused it — this ordering is
    what keeps that a redundancy rather than the only line of defence.

    Nothing here commits and nothing here catches. The caller's
    ``tenant_context`` block is the transaction boundary, so a raise from any
    step — including the MD-7 refusal below, which fires *after* both legs
    are written — leaves the book exactly as it was (ADR-0128 §2).

    MD-7 (``set_inactive``, D-E) is honoured for a full disposal only. The
    check is deliberately made against the ledger **as it stands after the
    sell**, not against a computed prediction: the question "does this
    investment still hold units?" is answered by the book, and answering it
    any other way would let the two disagree. A partial sale that asks for
    deactivation is refused rather than silently ignored — an inactive
    investment holding units is a corrupted book, and D-2 reserves blocks for
    exactly that.

    Args:
        ticket: The order ticket to emit. Already validated and complete.
        investment_service: The single sanctioned ledger write seam (D-A).
        position_transactions: Read-only here — the ledger the MD-7 holdings
            check reads back.
        investments: Read-only here — the before-image source for D-H.
        booked_by: The booking user; the ``created_by`` of every emitted row.
        cash_effect: The cash magnitude, from the one shared derivation.

    Returns:
        The effects in emission order: the instrument leg, the cash leg if
        one was written, then the ``investment_update`` if MD-7 applied.

    Raises:
        TicketIncomplete: If ``set_inactive`` is set on a ticket that is not
            a full disposal — a buy, or a sale leaving units behind.
        NonNegativeHoldingsError: If the instrument leg would drive holdings
            below zero (ADR-0097 §4, via the write seam).
        CurrencyMismatchError: If a leg's currency differs from its
            position's (ADR-0097 §5, via the write seam).
    """
    if ticket.set_inactive and ticket.direction != DIRECTION_SELL:
        # Refused before any write: a buy is never a disposal, and no ledger
        # read can change that. (D-E)
        raise TicketIncomplete(
            f"Trade ticket {ticket.ticket_number} asks to deactivate its "
            "investment, but a purchase is never a disposal (MD-7).",
            identifier=INCOMPLETE_SET_INACTIVE_NOT_FULL_DISPOSAL,
            field="set_inactive",
        )

    instrument_leg, cash_leg = order_legs(ticket, cash_effect=cash_effect)
    effects: list[EffectInput] = []

    for leg in (instrument_leg, cash_leg):
        if leg is None:
            continue
        created = await investment_service.add_position_transaction(
            investment_id=leg.investment_id,
            txn_type=leg.txn_type,
            trade_date=ticket.trade_date,
            units=leg.units,
            currency=leg.currency,
            ingest_origin=EMISSION_INGEST_ORIGIN,
            created_by=booked_by,
            price_per_unit=leg.price_per_unit,
            consideration=leg.consideration,
            note=leg.note,
            source=leg.source,
        )
        effects.append(EffectInput(effect_type=EFFECT_POSITION_TXN, effect_id=created.id))

    if ticket.set_inactive:
        effects.append(
            await _deactivate_on_full_disposal(
                ticket,
                investment_service=investment_service,
                position_transactions=position_transactions,
                investments=investments,
            )
        )

    return effects


async def _deactivate_on_full_disposal(
    ticket: TradeTicketDTO,
    *,
    investment_service: InvestmentService,
    position_transactions: PositionTransactionRepository,
    investments: InvestmentRepository,
) -> EffectInput:
    """Deactivate the traded investment, refusing anything but a full disposal.

    U-SELL's MD-7 gate: the *question* whether this disposal was full is
    asked here, and the deactivation itself is :func:`deactivate`'s, shared
    with R-SEC-SELL — which never asks, because a secondary sale is always
    full (D-S). See :func:`emit_order` for why the holdings check reads the
    written ledger rather than predicting it.
    """
    investment_id = ticket.investment_id
    assert investment_id is not None  # guaranteed by order_legs' preconditions

    ledger = await position_transactions.list_for_investment(investment_id)
    remaining = holdings_as_of(ledger, ticket.trade_date)
    if remaining != 0:
        raise TicketIncomplete(
            f"Trade ticket {ticket.ticket_number} asks to deactivate investment "
            f"{investment_id}, but {remaining} units remain on "
            f"{ticket.trade_date}; an inactive investment holding units is a "
            "corrupted book (MD-7).",
            identifier=INCOMPLETE_SET_INACTIVE_NOT_FULL_DISPOSAL,
            field="set_inactive",
        )

    return await deactivate(
        investment_id,
        investments=investments,
        investment_service=investment_service,
    )


# ---------------------------------------------------------------------------
# Shared emission steps
# ---------------------------------------------------------------------------


async def create_investment_from_ticket(
    ticket: TradeTicketDTO,
    *,
    master: MasterData,
    valuation_mode: str,
    commitment_amount: Decimal | None,
    investment_service: InvestmentService,
    tickets: TradeTicketRepository,
    booked_by: UUID,
    now: datetime,
) -> tuple[InvestmentDTO, EffectInput]:
    """Create the ``investments`` row a creating flow books, and link it back.

    MD-12's whole claim in one function: the investment is an **emission
    effect**, not a precondition. Nothing exists before this call, which is
    what makes a creating booking reversible at all — a shell created at
    Propose and abandoned would be a row nobody asked for and nobody owns.

    Three writes, in an order that is not arbitrary. The row goes first
    because everything else needs its id; the identifiers follow (D-L) so a
    reversal that deletes the shell takes them with it by CASCADE; and the
    ticket learns its ``investment_id`` last (D-T), because that link is what
    a reversal walks and it should not exist before the thing it points at is
    complete.

    ``valuation_mode`` is always passed explicitly (D-R): the column's
    ``reported`` default is right for two of the three creating flows and
    wrong for U-NEW, and a default that is right by coincidence is a bug
    waiting for a fourth flow.

    Args:
        ticket: The creating ticket.
        master: Its parsed payload.
        valuation_mode: ``reported`` or ``unitised``, per the flow (D-R).
        commitment_amount: From :func:`reconcile_commitment`.
        investment_service: The write seam; must be wired with an identifier
            repository if the payload carries one (D-L).
        tickets: The ticket repository, for the D-T link.
        booked_by: The booking user; ``created_by`` on every row written.
        now: The ticket's new ``updated_at``.

    Returns:
        ``(created_investment, effect)`` — the effect being the D-I creation
        encoding: ``investment_update`` with ``prior_state`` ``None``.

    Raises:
        RuntimeError: If the payload carries an identifier and the investment
            service was constructed without an identifier repository.
        TicketStateInvalid: If the ticket already names an investment (D-T).
    """
    created = await investment_service.create_investment(
        name=master.name,
        investment_type=master.investment_type,
        asset_class_id=master.asset_class_id,
        currency=master.currency,
        created_by=booked_by,
        manager_name=master.manager,
        region=master.region,
        vintage_year=master.vintage_year,
        commitment_amount=commitment_amount,
        anlv_code=master.anlv_code,
        valuation_mode=valuation_mode,
    )
    await _write_identifiers(
        created.id,
        master=master,
        investment_service=investment_service,
        booked_by=booked_by,
    )
    await tickets.link_investment(ticket.id, investment_id=created.id, now=now)
    return created, EffectInput(
        effect_type=EFFECT_INVESTMENT_UPDATE,
        effect_id=created.id,
        # D-I: NULL is the creation marker. S2c reads exactly this.
        prior_state=None,
    )


async def _write_identifiers(
    investment_id: UUID,
    *,
    master: MasterData,
    investment_service: InvestmentService,
    booked_by: UUID,
) -> None:
    """Write the created row's security identifiers (D-L).

    The scheme/value pair the composer captured is promoted to **primary**,
    because an investment created from a wizard that asked for an ISIN has
    exactly one identity and the market-data joins want to know which. The
    resolved FIGI rides alongside as an ordinary second row: it is a
    cross-reference, not the identity the operator chose.

    These rows are not recorded as effects — see the module docstring.
    """
    if master.identifier_scheme and master.identifier_value:
        primary = await investment_service.add_identifier_manual(
            investment_id,
            master.identifier_scheme,
            master.identifier_value,
            booked_by,
        )
        await investment_service.set_primary_identifier(investment_id, primary.id, booked_by)
    if master.figi:
        await investment_service.add_identifier_manual(
            investment_id,
            IDENTIFIER_SCHEME_FIGI,
            master.figi,
            booked_by,
        )


async def deactivate(
    investment_id: UUID,
    *,
    investments: InvestmentRepository,
    investment_service: InvestmentService,
) -> EffectInput:
    """Set an investment inactive and return the ``investment_update`` effect.

    The before-image is taken **before** the flag flips, and it is the whole
    row rather than the one field (D-H) — so the effect is what a reversal
    restores from, not a hint about what to go and look up.

    Args:
        investment_id: The investment to retire.
        investments: Read-only — the before-image source.
        investment_service: The write seam for the flag.

    Returns:
        An ``investment_update`` effect carrying the before-image.

    Raises:
        TicketIncomplete: If the investment is not visible in this tenant —
            unreachable behind the propose-time blocks, reported rather than
            assumed away.
    """
    before = await investments.get_by_id(investment_id)
    if before is None:  # pragma: no cover — the blocks have just resolved it
        raise TicketIncomplete(
            f"Investment {investment_id} is not visible in this tenant.",
            identifier=INCOMPLETE_MISSING_INVESTMENT,
            field="investment_id",
        )
    await investment_service.set_investment_active(investment_id, False)
    return EffectInput(
        effect_type=EFFECT_INVESTMENT_UPDATE,
        effect_id=investment_id,
        prior_state=investment_before_image(before),
    )


async def write_nav(
    ticket: TradeTicketDTO,
    *,
    investment_id: UUID,
    nav_value: Decimal,
    navs: InvestmentNavRepository,
    investment_service: InvestmentService,
    booked_by: UUID,
) -> EffectInput:
    """Write the ``actual`` NAV a reported-kind booking states, refusing a collision.

    **The collision check is what makes the row reversible** (D-N).
    ``add_nav`` UPSERTs on ``(investment_id, as_of_date, nav_kind)``, and
    ``prior_state`` is reserved for ``investment_update`` effects (T-1 D-2),
    so a NAV this booking silently overwrote could never be put back. Rather
    than widen the effect vocabulary for a case the user can resolve in one
    gesture, the booking refuses and says which date is occupied: re-date the
    ticket, or correct the NAV through the ordinary CRUD surface.

    On a freshly created investment the check is trivially empty. It runs
    anyway — one rule, applied everywhere, is cheaper to reason about than a
    rule with an exemption whose precondition a later flow might not meet.

    Args:
        ticket: The booking ticket; supplies ``trade_date``, ``currency`` and
            the provenance string.
        investment_id: The investment the NAV belongs to.
        nav_value: The value to write — ``0`` for a full disposal (MD-17),
            the acquired NAV for a secondary purchase.
        navs: Read-only — the collision check.
        investment_service: The write seam.
        booked_by: The booking user.

    Returns:
        A ``nav`` effect naming the written row.

    Raises:
        TicketIncomplete: With ``identifier='nav_exists_at_trade_date'`` if an
            ``actual`` NAV already stands on the trade date.
    """
    existing = await navs.list_by_investment_and_kind(investment_id, NAV_KIND_ACTUAL)
    if any(row.as_of_date == ticket.trade_date for row in existing):
        raise TicketIncomplete(
            f"An actual NAV already stands on {ticket.trade_date} for investment "
            f"{investment_id}. Booking would overwrite it, and an overwritten "
            "NAV cannot be restored by a reversal (D-N); pick another trade "
            "date, or correct the existing NAV first.",
            identifier=BLOCK_NAV_EXISTS_AT_TRADE_DATE,
            field="trade_date",
        )
    created = await investment_service.add_nav(
        investment_id=investment_id,
        as_of_date=ticket.trade_date,
        nav_kind=NAV_KIND_ACTUAL,
        nav_value=nav_value,
        currency=ticket.currency,
        source=provenance(ticket),
        created_by=booked_by,
    )
    return EffectInput(effect_type=EFFECT_NAV, effect_id=created.id)


async def _write_cash_leg(
    ticket: TradeTicketDTO,
    *,
    cash_effect: Decimal,
    investment_service: InvestmentService,
    booked_by: UUID,
) -> list[EffectInput]:
    """Write the settlement leg, if the ticket moves any cash.

    Returns a list rather than an optional effect so every caller can splice
    it into its effect list without a branch — the "no cash moved" case is
    then an empty list, which is what it is.
    """
    leg = cash_leg(ticket, cash_effect=cash_effect)
    if leg is None:
        return []
    created = await investment_service.add_position_transaction(
        investment_id=leg.investment_id,
        txn_type=leg.txn_type,
        trade_date=ticket.trade_date,
        units=leg.units,
        currency=leg.currency,
        ingest_origin=EMISSION_INGEST_ORIGIN,
        created_by=booked_by,
        price_per_unit=leg.price_per_unit,
        consideration=leg.consideration,
        note=leg.note,
        source=leg.source,
    )
    return [EffectInput(effect_type=EFFECT_POSITION_TXN, effect_id=created.id)]


# ---------------------------------------------------------------------------
# The reported kinds
# ---------------------------------------------------------------------------


async def emit_secondary_sell(
    ticket: TradeTicketDTO,
    *,
    investment_service: InvestmentService,
    navs: InvestmentNavRepository,
    investments: InvestmentRepository,
    booked_by: UUID,
    cash_effect: Decimal,
) -> list[EffectInput]:
    """Emit R-SEC-SELL: proceeds, NAV → 0, inactivation, cash in (MD-17).

    Four rows, and the middle two are the flow rather than options on it
    (D-S). A secondary sale is a **full** disposal — the schema has no
    fraction column (MD-18) — so the stake is worth nothing afterwards and
    the position is closed. ``set_inactive`` is not consulted: it is U-SELL's
    question (MD-7), and asking it here would imply an answer other than yes.

    The order is the story the book should tell: money arrives, the stake is
    written down, the position is retired, the cash lands. Nothing depends on
    it for correctness — the whole sequence is one transaction — but a
    partial state that a reader could ever see should still read forwards.

    Proceeds book as ``distribution`` / ``actual`` (ADR-0128 Q-3), positive
    by the codebase's sign convention. A sale whose costs exceeded its gross
    writes its negative net as it stands and the cash leg follows it out
    (D-B); the ``net_non_positive`` warning has already said so.

    Args:
        ticket: A complete ``secondary`` / ``sell`` ticket.
        investment_service: The single write seam for every row.
        navs: Read-only — the D-N collision check.
        investments: Read-only — the before-image source.
        booked_by: The booking user.
        cash_effect: The proceeds, from the one shared derivation.

    Returns:
        The four effects in emission order: ``cashflow``, ``nav``,
        ``investment_update`` (before-image), ``position_txn``. The last is
        absent when the proceeds are exactly zero.

    Raises:
        TicketIncomplete: If an actual NAV already stands on the trade date.
        ValueError: If the ticket names no investment — unreachable behind
            ``_block_completeness``.
    """
    investment_id = ticket.investment_id
    if investment_id is None:
        raise ValueError("A secondary sale reaching emission must name an investment.")

    flow = await investment_service.add_cashflow(
        investment_id=investment_id,
        flow_timestamp=datetime.combine(ticket.trade_date, FLOW_TIME_OF_DAY, tzinfo=timezone.utc),
        flow_type=FLOW_TYPE_DISTRIBUTION,
        flow_kind=FLOW_KIND_ACTUAL,
        amount=cash_effect,
        currency=ticket.currency,
        description=provenance(ticket),
        created_by=booked_by,
    )
    effects = [EffectInput(effect_type=EFFECT_CASHFLOW, effect_id=flow.id)]
    effects.append(
        await write_nav(
            ticket,
            investment_id=investment_id,
            nav_value=Decimal(0),
            navs=navs,
            investment_service=investment_service,
            booked_by=booked_by,
        )
    )
    effects.append(
        await deactivate(
            investment_id,
            investments=investments,
            investment_service=investment_service,
        )
    )
    effects.extend(
        await _write_cash_leg(
            ticket,
            cash_effect=cash_effect,
            investment_service=investment_service,
            booked_by=booked_by,
        )
    )
    return effects


async def emit_commitment(
    ticket: TradeTicketDTO,
    *,
    master: MasterData,
    investment_service: InvestmentService,
    tickets: TradeTicketRepository,
    booked_by: UUID,
    now: datetime,
) -> list[EffectInput]:
    """Emit R-COMMIT: one ``investments`` row, and nothing else (MD-19).

    The smallest emission in the system, and deliberately so. **No cash moves
    with a commitment** — the capital calls do that, and they stay ordinary
    cashflows outside the ticket object (R-3). **No NAV is written** either:
    a commitment that has not been called is worth nothing yet, and a zero
    NAV row would be a statement the manager never made.

    The ticket books once, at the commitment date, and remains the provenance
    anchor for the position from then on.

    Args:
        ticket: A complete ``commitment`` ticket.
        master: Its parsed payload.
        investment_service: The write seam.
        tickets: The ticket repository, for the D-T link.
        booked_by: The booking user.
        now: The ticket's new ``updated_at``.

    Returns:
        Exactly one effect: ``investment_update`` with ``prior_state`` NULL.

    Raises:
        RuntimeError: If the ticket names a settlement position. The b034
            ``ck_trade_tickets_commitment_shape`` CHECK forbids it, so this
            cannot happen — which is why it is reported rather than assumed.
        TicketIncomplete: If the ticket and its payload state different
            commitments (D-U).
    """
    if ticket.cash_investment_id is not None:  # pragma: no cover — CHECK-guarded
        raise RuntimeError(
            f"Trade ticket {ticket.ticket_number} is a commitment but names a "
            "settlement position; ck_trade_tickets_commitment_shape should "
            "have made this impossible (MD-19)."
        )
    _, effect = await create_investment_from_ticket(
        ticket,
        master=master,
        valuation_mode=VALUATION_MODE_REPORTED,
        commitment_amount=reconcile_commitment(ticket, master=master),
        investment_service=investment_service,
        tickets=tickets,
        booked_by=booked_by,
        now=now,
    )
    return [effect]


async def emit_secondary_buy(
    ticket: TradeTicketDTO,
    *,
    master: MasterData,
    investment_service: InvestmentService,
    navs: InvestmentNavRepository,
    tickets: TradeTicketRepository,
    booked_by: UUID,
    now: datetime,
    cash_effect: Decimal,
) -> list[EffectInput]:
    """Emit R-SEC-BUY: the stake's row, its opening NAV, and cash out.

    A secondary purchase acquires a position that already has a history, so
    the created row is ``reported`` (D-R) — its value comes from the GP's
    statements, not from units and a price — and it opens at the **acquired
    NAV**, the stake's value at transfer, rather than at what was paid for
    it. The two legitimately differ, and that difference is the discount or
    premium the analytics want to see (MD-20). Neither is a warning.

    The unfunded commitment assumed with the stake becomes the row's
    ``commitment_amount`` (D-U), which is what makes the position's pacing
    figures correct from its first day rather than from its first call.

    Args:
        ticket: A complete ``secondary`` / ``buy`` ticket.
        master: Its parsed payload; supplies the acquired NAV.
        investment_service: The write seam.
        navs: Read-only — the D-N collision check.
        tickets: The ticket repository, for the D-T link.
        booked_by: The booking user.
        now: The ticket's new ``updated_at``.
        cash_effect: What the purchase costs, from the one shared derivation.

    Returns:
        Three effects: ``investment_update`` (``prior_state`` NULL), ``nav``,
        ``position_txn``. The last is absent when the purchase costs nothing.

    Raises:
        TicketIncomplete: If the ticket and its payload state different
            commitments (D-U).
        ValueError: If the payload carries no acquired NAV — unreachable
            behind ``_require_secondary_amounts``.
    """
    acquired_nav = master.acquired_nav
    if acquired_nav is None:
        raise ValueError(
            "A secondary purchase reaching emission must carry an acquired NAV; "
            "_require_secondary_amounts should have refused it."
        )

    created, effect = await create_investment_from_ticket(
        ticket,
        master=master,
        valuation_mode=VALUATION_MODE_REPORTED,
        commitment_amount=reconcile_commitment(ticket, master=master),
        investment_service=investment_service,
        tickets=tickets,
        booked_by=booked_by,
        now=now,
    )
    effects = [effect]
    effects.append(
        await write_nav(
            ticket,
            investment_id=created.id,
            nav_value=acquired_nav,
            navs=navs,
            investment_service=investment_service,
            booked_by=booked_by,
        )
    )
    effects.extend(
        await _write_cash_leg(
            ticket,
            cash_effect=cash_effect,
            investment_service=investment_service,
            booked_by=booked_by,
        )
    )
    return effects


async def emit_new_order(
    ticket: TradeTicketDTO,
    *,
    master: MasterData,
    investment_service: InvestmentService,
    position_transactions: PositionTransactionRepository,
    investments: InvestmentRepository,
    tickets: TradeTicketRepository,
    booked_by: UUID,
    now: datetime,
    cash_effect: Decimal,
) -> list[EffectInput]:
    """Emit U-NEW: create the instrument, then book it exactly as a U-BUY (D-M).

    U-NEW is not a fourth settlement shape — it is a U-BUY whose instrument
    did not exist yet. So it creates the row and then **delegates**, rather
    than restating the two-leg emission a third time: the wizard and the
    ordinary purchase must produce identical ledger rows, and the only way to
    guarantee that is for them to be the same code.

    The created row is ``unitised`` (D-R): a U-NEW instrument is unit-dealt
    by definition — the ticket states units and a price — and its NAV series
    is materialised from holdings × price (ADR-0098) rather than carried.

    The delegation passes a ticket with ``investment_id`` filled in
    (:func:`dataclasses.replace` on the frozen DTO). The **persisted** link is
    :func:`create_investment_from_ticket`'s (D-T); this copy exists only so
    :func:`emit_order` can read the id it needs without re-loading the row it
    was just handed.

    Args:
        ticket: A complete ``order`` / ``buy`` ticket naming no investment.
        master: Its parsed payload.
        investment_service: The write seam.
        position_transactions: Read-only — the MD-7 holdings check.
        investments: Read-only — the before-image source.
        tickets: The ticket repository, for the D-T link.
        booked_by: The booking user.
        now: The ticket's new ``updated_at``.
        cash_effect: The purchase cost, from the one shared derivation.

    Returns:
        The creation effect followed by :func:`emit_order`'s.
    """
    created, effect = await create_investment_from_ticket(
        ticket,
        master=master,
        valuation_mode=VALUATION_MODE_UNITISED,
        # A listed instrument has no commitment; U-NEW records none (D-U).
        commitment_amount=None,
        investment_service=investment_service,
        tickets=tickets,
        booked_by=booked_by,
        now=now,
    )
    order_effects = await emit_order(
        replace(ticket, investment_id=created.id),
        investment_service=investment_service,
        position_transactions=position_transactions,
        investments=investments,
        booked_by=booked_by,
        cash_effect=cash_effect,
    )
    return [effect, *order_effects]


# ---------------------------------------------------------------------------
# Reversal (S2c, ADR-0128 §6)
# ---------------------------------------------------------------------------


#: The table each ``effect_type`` names a row in.
#:
#: One mapping, used twice and never restated: the audit-log modification
#: probe needs the table name, and the "is it still there?" probe needs to
#: know which repository owns it. The keys are exactly the ADR-0128 §2
#: vocabulary (``ck_trade_ticket_effects_effect_type``), and a test pins that
#: they stay exactly it — an effect type with no target table would be an
#: effect nothing could reverse.
TARGET_TABLES: Mapping[str, str] = {
    EFFECT_POSITION_TXN: "position_transactions",
    EFFECT_CASHFLOW: "investment_cashflows",
    EFFECT_NAV: "investment_navs",
    EFFECT_INVESTMENT_UPDATE: "investments",
}

#: The ``ingest_origin`` the platform writes its own derived rows under.
#:
#: Computed NAVs (ADR-0098) and projected cash-plan rows carry it. They are
#: the platform's arithmetic rather than anybody's data, so the shell
#: clean-up steps over them: a materialised series is re-derivable and its
#: presence says nothing about whether a human has adopted the row.
SYSTEM_INGEST_ORIGIN: str = "system"

#: The user-owned children whose presence retains a created shell, in the
#: order :func:`cleanup_new_investment_shell` reports them.
_LEDGER_TABLE: str = "position_transactions"
_CASHFLOW_TABLE: str = "investment_cashflows"
_NAV_TABLE: str = "investment_navs"


@dataclass(frozen=True)
class ShellOutcome:
    """What became of the ``investments`` row a creating booking made (D-AC).

    A reversal deletes the shell when nothing but platform artefacts is left
    on it, and retains it — deactivated — when a human has since put
    something there. Both are correct; which one happened is not something
    the caller should have to go and look up, so it is reported.

    Args:
        investment_id: The created row.
        deleted: Whether it was removed. When ``False`` the row survives,
            ``is_active`` is ``False`` (MD-12: it must not appear in a
            picker), and the ticket still names it.
        retained_because: The child table that kept it, or ``None`` when it
            was deleted.
    """

    investment_id: UUID
    deleted: bool
    retained_because: str | None


@dataclass(frozen=True)
class ReversalReport:
    """What a reversal undid (D-AD).

    ``reversed`` lists the effect rows the reversal acted on — and they are
    still there afterwards. Effect rows are **never deleted** by a reversal
    (T-1 §3): ``trade_ticket_effects.effect_id`` carries no foreign key
    precisely so the enumeration survives the rows it enumerated, which is
    what lets history say *what* a cancelled ticket once did rather than only
    that it was cancelled. ``delete_effects_for_ticket`` exists on the
    repository and this path does not call it.

    Args:
        ticket: The ticket, now ``cancelled`` with its reason.
        reversed: The effects that were undone, as they were enumerated.
        shell: The created row's fate, or ``None`` for a ticket that created
            no investment (U-BUY / U-SELL).
    """

    ticket: TradeTicketDTO
    reversed: tuple[TradeTicketEffectDTO, ...]
    shell: ShellOutcome | None


def restore_from_before_image(current: InvestmentDTO, image: Mapping[str, Any]) -> bool:
    """Check a before-image against the row it describes; return its ``is_active``.

    The inverse of :func:`investment_before_image`, and deliberately only
    *half* an inverse: it decides whether the image may be restored and hands
    back the single field the emission can change. It writes nothing — the
    caller owns the write, through the one sanctioned seam.

    **What the restore is allowed to put back.** The only ``investments``
    update any emission performs is ``is_active`` → ``False``
    (:func:`deactivate`, for U-SELL's MD-7 and R-SEC-SELL's MD-17). So that
    is the only field restored. Writing the whole image back would be a
    reversal that reached past what the booking did and overwrote every edit
    made in between — a before-image is evidence, not a snapshot to roll the
    world back to.

    **Why every other field is compared.** ``prior_state`` records the whole
    row (D-H) rather than the field that changed, so the effect stays useful
    if a later flow updates something else. That generosity is only safe if
    the reversal checks it: if any other field has moved, this before-image
    describes a row that no longer exists in that shape and the reversal
    refuses rather than restoring against it. ``updated_at`` is excluded
    because the booking's own ``set_active`` bumped it, and ``is_active``
    because it is the field under restoration.

    The comparison encodes ``current`` through :func:`investment_before_image`
    rather than parsing the image back into ``Decimal`` / ``UUID`` /
    ``datetime``. The pairing is then exact by construction — the same
    transform that produced the stored side produces the compared side — and
    no per-field type table has to be maintained alongside
    :class:`~core.repositories.investment_repository.InvestmentDTO`, which is
    the thing that would rot.

    In practice this cannot fire: any edit to the row would have left an
    ``UPDATE`` in the audit log and :func:`check_effects_untouched` would
    already have refused with ``modified``. It is the belt to that check's
    braces, and it is what makes the restore honest rather than trusting.

    Args:
        current: The investment as it stands now.
        image: The ``prior_state`` recorded at booking.

    Returns:
        The ``is_active`` value to write back.

    Raises:
        TicketReversalBlocked: With ``cause='unrestorable'`` if any compared
            field differs, or if the image carries no boolean ``is_active``.
    """
    encoded = investment_before_image(current)
    divergent = sorted(
        name
        for name, value in encoded.items()
        if name not in _RESTORE_EXCLUDED_FIELDS and image.get(name) != value
    )
    missing = sorted(set(encoded) - set(image) - _RESTORE_EXCLUDED_FIELDS)
    restored = image.get("is_active")

    if divergent or missing or not isinstance(restored, bool):
        raise TicketReversalBlocked(
            f"The before-image recorded for investment {current.id} no longer "
            f"describes the row: {_unrestorable_detail(divergent, missing, restored)}. "
            "Restoring it would overwrite an edit this booking never made; "
            "correct the investment through the CRUD instead (ADR-0128 §6).",
            effect_type=EFFECT_INVESTMENT_UPDATE,
            effect_id=current.id,
            cause=REVERSAL_CAUSE_UNRESTORABLE,
        )
    return restored


#: Fields :func:`restore_from_before_image` does not compare.
#:
#: ``is_active`` is what the restore writes; ``updated_at`` was bumped by the
#: booking's own write of it, so comparing either would refuse every
#: legitimate reversal.
_RESTORE_EXCLUDED_FIELDS: frozenset[str] = frozenset({"is_active", "updated_at"})


def _unrestorable_detail(
    divergent: list[str],
    missing: list[str],
    restored: object,
) -> str:
    """Phrase which half of the before-image check failed."""
    parts: list[str] = []
    if divergent:
        parts.append(f"{', '.join(divergent)} changed")
    if missing:
        parts.append(f"{', '.join(missing)} absent from the image")
    if not isinstance(restored, bool):
        parts.append(f"is_active is {restored!r}, not a boolean")
    return "; ".join(parts)


async def check_effects_untouched(
    effects: Sequence[TradeTicketEffectDTO],
    *,
    audit_log: AuditLogRepository,
    position_transactions: PositionTransactionRepository,
    cashflows: InvestmentCashflowRepository,
    navs: InvestmentNavRepository,
    investments: InvestmentRepository,
) -> None:
    """Refuse the reversal if any emitted row has moved since the booking (D-Y, D-Z).

    ADR-0128 §6's precondition, asked of **every** effect before a single row
    is deleted — the same all-blocks-before-any-write order
    :meth:`~services.transactions.ticket_service.TicketService.propose` uses,
    and for the same reason: a reversal that deleted three rows and then
    discovered the fourth was untouchable would be a reversal the transaction
    has to undo, and the operator would be told about the fourth row while
    wondering what happened to the other three.

    Two questions per effect, in this order:

    * **Is the row still there?** Absence is the stronger fact and is checked
      first: a row that was updated and then deleted is *consumed*, and
      saying "modified" about something that no longer exists would send the
      operator to look for it.
    * **Has it been UPDATEd since?** Through the audit log, never through
      ``updated_at``. ``position_transactions.update`` and
      ``update_opening`` write the row's fields by ORM assignment with no
      ``onupdate`` and no trigger maintaining ``updated_at``, so that column
      still reads as the insert time after an edit — a check built on it
      would pass for an edited ledger row, which is the one case that matters
      most. The audit trigger fires on every path (ADR-0035 §7). Its
      ``created_at`` and the effect's ``emitted_at`` are both the transaction
      timestamp, so the booking's own ``UPDATE`` of a deactivated investment
      ties rather than exceeds and is correctly not counted.

    Args:
        effects: The ticket's effects, as enumerated at booking.
        audit_log: The read-only audit seam behind the modification probe.
        position_transactions: Read-only — presence of ledger targets.
        cashflows: Read-only — presence of cashflow targets.
        navs: Read-only — presence of NAV targets.
        investments: Read-only — presence of ``investment_update`` targets.

    Raises:
        TicketReversalBlocked: On the first effect that fails, with
            ``cause`` ``'consumed'`` or ``'modified'`` and the effect named.
        KeyError: For an effect type outside the ADR-0128 §2 vocabulary —
            unreachable behind the repository's own validation and the b034
            CHECK.
    """
    for effect in effects:
        table = TARGET_TABLES[effect.effect_type]
        if not await _target_exists(
            effect,
            position_transactions=position_transactions,
            cashflows=cashflows,
            navs=navs,
            investments=investments,
        ):
            raise TicketReversalBlocked(
                f"The {effect.effect_type} row {effect.effect_id} this booking "
                f"emitted is gone from {table}; there is nothing left to "
                "reverse and reversing the rest would leave a half-undone "
                "booking (ADR-0128 §6).",
                effect_type=effect.effect_type,
                effect_id=effect.effect_id,
                cause=REVERSAL_CAUSE_CONSUMED,
            )
        if await audit_log.has_update_since(table, effect.effect_id, after=effect.emitted_at):
            raise TicketReversalBlocked(
                f"The {effect.effect_type} row {effect.effect_id} this booking "
                f"emitted has been edited in {table} since. Deleting it would "
                "delete somebody else's correction; correct the ticket through "
                "the CRUD and cancel it with a note instead (ADR-0128 §6).",
                effect_type=effect.effect_type,
                effect_id=effect.effect_id,
                cause=REVERSAL_CAUSE_MODIFIED,
            )


async def _target_exists(
    effect: TradeTicketEffectDTO,
    *,
    position_transactions: PositionTransactionRepository,
    cashflows: InvestmentCashflowRepository,
    navs: InvestmentNavRepository,
    investments: InvestmentRepository,
) -> bool:
    """Report whether the row an effect names is still visible in this tenant."""
    if effect.effect_type == EFFECT_POSITION_TXN:
        return await position_transactions.get_by_id(effect.effect_id) is not None
    if effect.effect_type == EFFECT_CASHFLOW:
        return await cashflows.get_by_id(effect.effect_id) is not None
    if effect.effect_type == EFFECT_NAV:
        return await navs.get_by_id(effect.effect_id) is not None
    return await investments.get_by_id(effect.effect_id) is not None


async def undo_effects(
    ticket: TradeTicketDTO,
    effects: Sequence[TradeTicketEffectDTO],
    *,
    investment_service: InvestmentService,
    investments: InvestmentRepository,
    position_transactions: PositionTransactionRepository,
    acting_user: UUID,
) -> None:
    """Delete what the booking wrote and restore what it changed (D-AA, D-AB).

    Called only after :func:`check_effects_untouched` has passed over every
    effect, and containing no ``try``/``except`` but the one D-AA translation
    below. The caller's ``tenant_context`` block is the transaction boundary,
    exactly as it is for the emission: a raise anywhere here leaves the book
    as it was, so there is no half-reversed state to compensate for.

    **Every delete goes through** :class:`~services.investments.investment_service.InvestmentService`,
    never through a repository — the same single sanctioned seam the emission
    writes through (D-A). It carries what this module must not restate: the
    ADR-0097 §4 non-negativity re-check with the row removed (and ADR-0130's
    cash exemption from it), the ADR-0098 computed-NAV materialisation rerun
    from the deleted row's ``trade_date``, and the ADR-0103 §6 cash-plan
    recompute behind a deleted ``plan`` flow.

    **Order.** Ledger rows first, instrument leg before cash leg, then
    cashflows, then NAVs, then the ``investment_update`` restores. Nothing
    depends on it — the effect table cannot even recover emission order,
    since every effect of one booking shares the transaction's ``NOW()`` —
    but the non-negativity guard only fires on an instrument leg, and putting
    that leg first means the operator hears about the units they sold on
    before hearing about anything else.

    ``investment_update`` effects with ``prior_state IS NULL`` are **not**
    handled here. Under D-I those mark a row this booking *created*, and a
    created row is not restored, it is cleaned up —
    :func:`cleanup_new_investment_shell` — which needs a decision this
    function has no business making.

    Args:
        ticket: The booked ticket being reversed. Read for its
            ``investment_id``, which is what distinguishes the instrument leg
            from the cash leg.
        effects: Its effects, already checked untouched.
        investment_service: The single sanctioned delete seam.
        investments: Read-only — the current row the before-image is checked
            against.
        position_transactions: Read-only — resolves each ledger effect to the
            investment that owns it.
        acting_user: The reversing user; attributable for the materialisation
            and cash-plan writes the deletes trigger.

    Raises:
        TicketReversalBlocked: With ``cause='holdings_consumed'`` if a ledger
            delete would drive holdings below zero, chained from the
            :class:`~core.exceptions.NonNegativeHoldingsError` that says so;
            with ``cause='unrestorable'`` if a before-image no longer
            describes its row.
    """
    grouped = _group_by_type(effects)

    for effect in await _ledger_effects_in_undo_order(
        grouped[EFFECT_POSITION_TXN],
        ticket=ticket,
        position_transactions=position_transactions,
    ):
        await _delete_ledger_row(
            effect,
            investment_service=investment_service,
            position_transactions=position_transactions,
            acting_user=acting_user,
        )

    for effect in grouped[EFFECT_CASHFLOW]:
        await investment_service.delete_cashflow(effect.effect_id, acting_user=acting_user)

    for effect in grouped[EFFECT_NAV]:
        await investment_service.delete_nav(effect.effect_id)

    for effect in grouped[EFFECT_INVESTMENT_UPDATE]:
        if effect.prior_state is None:
            continue  # D-I: a created row; the shell clean-up owns it.
        current = await investments.get_by_id(effect.effect_id)
        if current is None:  # pragma: no cover — check_effects_untouched saw it
            raise TicketReversalBlocked(
                f"Investment {effect.effect_id} vanished mid-reversal.",
                effect_type=effect.effect_type,
                effect_id=effect.effect_id,
                cause=REVERSAL_CAUSE_CONSUMED,
            )
        await investment_service.set_investment_active(
            effect.effect_id,
            restore_from_before_image(current, effect.prior_state),
        )


def _group_by_type(
    effects: Sequence[TradeTicketEffectDTO],
) -> dict[str, list[TradeTicketEffectDTO]]:
    """Bucket effects by ``effect_type``, with every vocabulary key present."""
    grouped: dict[str, list[TradeTicketEffectDTO]] = {key: [] for key in TARGET_TABLES}
    for effect in effects:
        grouped[effect.effect_type].append(effect)
    return grouped


async def _ledger_effects_in_undo_order(
    effects: Sequence[TradeTicketEffectDTO],
    *,
    ticket: TradeTicketDTO,
    position_transactions: PositionTransactionRepository,
) -> list[TradeTicketEffectDTO]:
    """Return the ledger effects instrument-leg first, cash-leg second.

    Which leg is which is read off the row rather than off the effect: an
    effect records an id and a type, and the ticket's ``investment_id`` is
    the only thing that says whose ledger a given row belongs to. A leg whose
    row names neither the traded investment nor anything else recognisable
    sorts with the cash legs — the ordering is a courtesy for error
    precedence, not a correctness property, so an unclassifiable row is not
    worth refusing over.
    """
    instrument: list[TradeTicketEffectDTO] = []
    cash: list[TradeTicketEffectDTO] = []
    for effect in effects:
        row = await position_transactions.get_by_id(effect.effect_id)
        target = (
            instrument if row is not None and row.investment_id == ticket.investment_id else cash
        )
        target.append(effect)
    return [*instrument, *cash]


async def _delete_ledger_row(
    effect: TradeTicketEffectDTO,
    *,
    investment_service: InvestmentService,
    position_transactions: PositionTransactionRepository,
    acting_user: UUID,
) -> None:
    """Delete one emitted ledger row, translating the holdings refusal (D-AA).

    ``delete_position_transaction`` needs the owning investment, which the
    effect does not carry, so the row is re-read for it — a session-cached
    read, and the honest one: the effect names a row, not a position.

    The only ``except`` in the reversal path. A
    :class:`~core.exceptions.NonNegativeHoldingsError` here does not mean the
    ledger is inconsistent; it means the units this booking created have been
    sold on since, and the later trade has to be reversed first. That is a
    different sentence for the operator than "holdings would go negative", so
    it gets a different error — chained, so the ledger's own diagnosis is
    still in the traceback. The cash leg cannot reach it: ADR-0130 exempts a
    cash target from the guard on every write path, deletes included.
    """
    row = await position_transactions.get_by_id(effect.effect_id)
    if row is None:  # pragma: no cover — check_effects_untouched saw it
        raise TicketReversalBlocked(
            f"Ledger row {effect.effect_id} vanished mid-reversal.",
            effect_type=effect.effect_type,
            effect_id=effect.effect_id,
            cause=REVERSAL_CAUSE_CONSUMED,
        )
    try:
        await investment_service.delete_position_transaction(
            investment_id=row.investment_id,
            transaction_id=effect.effect_id,
            acting_user=acting_user,
        )
    except NonNegativeHoldingsError as exc:
        raise TicketReversalBlocked(
            f"The ledger row {effect.effect_id} this booking emitted cannot be "
            f"removed: investment {row.investment_id} would hold negative units "
            "afterwards, because the units it created have since been traded on. "
            "Reverse the later transaction first (ADR-0097 §4, ADR-0128 §6).",
            effect_type=effect.effect_type,
            effect_id=effect.effect_id,
            cause=REVERSAL_CAUSE_HOLDINGS_CONSUMED,
        ) from exc


async def cleanup_new_investment_shell(
    ticket: TradeTicketDTO,
    investment_id: UUID,
    *,
    tickets: TradeTicketRepository,
    investment_service: InvestmentService,
    position_transactions: PositionTransactionRepository,
    cashflows: InvestmentCashflowRepository,
    navs: InvestmentNavRepository,
    investments: InvestmentRepository,
    now: datetime,
) -> ShellOutcome:
    """Delete or retire the ``investments`` row a creating booking made (D-AC).

    ADR-0128 §6's clean-up clause, and the one place in the reversal where
    there is a judgement rather than an inverse. The row was an emission
    effect (MD-12), so undoing the booking means undoing it — but only while
    it is still nothing but what the booking made. The moment a human has put
    something on it, deleting it would take their work with it silently, by
    ``ON DELETE CASCADE``, and no amount of being right about the ticket
    justifies that.

    **What blocks the delete — user rows.** Ledger transactions, cashflows,
    NAVs that are not the platform's own, and any of the six classification
    children (region / sector / country weights, the two fixed-income weight
    tables, bond analytics). This runs *after* :func:`undo_effects`, so the
    booking's own rows are already gone and anything still standing is
    somebody else's.

    **What does not block it — platform artefacts.** ``instrument_prices``,
    ``'system'``-origin NAVs, ``watchpoints``, and the
    ``investment_identifiers`` written at creation (D-L). Every one of these
    is either the platform's arithmetic, re-derivable from the market data,
    or part of the shell itself; all of them cascade with the delete, and
    letting a materialised NAV series veto a reversal would mean a U-NEW
    could never be reversed at all, since booking one always produces some.

    **Order is forced by the schema.** ``trade_tickets.investment_id`` is
    ``ON DELETE RESTRICT``, so the ticket is unlinked first, in this same
    transaction, before the row it points at can go.

    **Retention is never silent.** A retained shell is deactivated —
    ``is_active`` false, so it never appears in a picker (MD-12) — the ticket
    keeps naming it, and the returned :class:`ShellOutcome` says which table
    kept it. A row the operator cannot see and was never told about is worse
    than one they were told about.

    Args:
        ticket: The booked ticket being reversed.
        investment_id: The created row, from the ``prior_state IS NULL``
            effect rather than from the ticket — the effect is what records
            what this booking made.
        tickets: For the D-AC unlink.
        investment_service: The single sanctioned write seam, for the delete
            and for the deactivation.
        position_transactions: Read-only — the ledger probe.
        cashflows: Read-only — the cashflow probe.
        navs: Read-only — the non-``'system'`` NAV probe.
        investments: Read-only — the six-child classification probe.
        now: The ticket's new ``updated_at`` when it is unlinked.

    Returns:
        The :class:`ShellOutcome` describing what happened.

    Raises:
        TicketNotFound: If the ticket is gone — unreachable, the caller
            loaded it.
        TicketStateInvalid: If the ticket names no investment to unlink.
    """
    retained_by = await _first_user_child(
        investment_id,
        position_transactions=position_transactions,
        cashflows=cashflows,
        navs=navs,
        investments=investments,
    )
    if retained_by is not None:
        await investment_service.set_investment_active(investment_id, False)
        return ShellOutcome(
            investment_id=investment_id,
            deleted=False,
            retained_because=(
                f"{retained_by} still holds rows this booking did not write; the "
                "investment was deactivated rather than deleted"
            ),
        )

    # RESTRICT: the link goes before the row it points at (D-T's other half).
    await tickets.unlink_investment(ticket.id, now=now)
    await investment_service.delete_investment(investment_id)
    return ShellOutcome(investment_id=investment_id, deleted=True, retained_because=None)


async def _first_user_child(
    investment_id: UUID,
    *,
    position_transactions: PositionTransactionRepository,
    cashflows: InvestmentCashflowRepository,
    navs: InvestmentNavRepository,
    investments: InvestmentRepository,
) -> str | None:
    """Return the first user-owned child table holding rows, or ``None``.

    First rather than all: the outcome names one table because the operator
    needs a reason, not an inventory — and the remedy (go and look at the
    investment) is the same whichever it is.
    """
    if await position_transactions.list_for_investment(investment_id):
        return _LEDGER_TABLE
    if await cashflows.list_by_investment(investment_id):
        return _CASHFLOW_TABLE
    if any(
        row.ingest_origin != SYSTEM_INGEST_ORIGIN
        for row in await navs.list_by_investment(investment_id)
    ):
        return _NAV_TABLE
    children = await investments.analytics_children_with_rows(investment_id)
    return children[0] if children else None


__all__ = [
    "CASH_UNIT_PRICE",
    "EFFECT_CASHFLOW",
    "EFFECT_INVESTMENT_UPDATE",
    "EFFECT_NAV",
    "EFFECT_POSITION_TXN",
    "EMISSION_INGEST_ORIGIN",
    "FLOW_KIND_ACTUAL",
    "FLOW_TIME_OF_DAY",
    "FLOW_TYPE_DISTRIBUTION",
    "IDENTIFIER_SCHEME_FIGI",
    "NAV_KIND_ACTUAL",
    "SYSTEM_INGEST_ORIGIN",
    "TARGET_TABLES",
    "VALUATION_MODE_REPORTED",
    "VALUATION_MODE_UNITISED",
    "LegSpec",
    "MasterData",
    "ReversalReport",
    "ShellOutcome",
    "cash_leg",
    "check_effects_untouched",
    "cleanup_new_investment_shell",
    "create_investment_from_ticket",
    "deactivate",
    "emit_commitment",
    "emit_new_order",
    "emit_order",
    "emit_secondary_buy",
    "emit_secondary_sell",
    "investment_before_image",
    "order_legs",
    "parse_master_data",
    "provenance",
    "reconcile_commitment",
    "restore_from_before_image",
    "undo_effects",
    "write_nav",
]
