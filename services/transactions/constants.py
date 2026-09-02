# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Named constants for the Transactions area (ADR-0128, decision record §2.9).

The single home for the trade-ticket vocabulary. Services import from here
and the S4 templates will too; **nothing re-declares these strings anywhere
else**. The decision record fixes the list — a new warning or block
identifier is a decision, not an implementation detail, so it lands here
first and everywhere else second.

**No user-facing copy lives in this module.** MD-9 fixes the wording of the
four warnings and the four surfaced blocks in the mockups, and S4 lifts it
verbatim into templates. What the service carries is an *identifier* plus
the structured values a message needs (reference price, resulting balance,
…) — never a rendered string. That split is what lets the copy change
without touching a service and lets the service be tested without asserting
on prose.

The status / kind / direction vocabularies mirror the b034 CHECK
constraints (``ck_trade_tickets_status`` / ``..._kind`` / ``..._direction``).
The CHECKs remain the guarantee; these constants exist so the service can
refuse a bad value as a typed domain error before any SQL runs, and so the
two never drift apart silently.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

#: Price-plausibility warning threshold (ADR-0128 Q-4).
#:
#: A fixed constant, deliberately **not** coupled to the watchpoint machinery
#: of ADR-0116: the stored ``instrument_prices`` row may simply be stale and
#: the user's execution price is the better fact, so this can only ever warn.
#: Expressed as a ratio of the reference price (0.05 = 5 %).
PRICE_DEVIATION_WARN_RATIO: Final[Decimal] = Decimal("0.05")

# ---------------------------------------------------------------------------
# Warning identifiers (decision record §2.9)
#
# A warning never blocks anything (MD-5, D-2): the users are professional
# portfolio managers, the platform surfaces the consequence and steps aside.
# ---------------------------------------------------------------------------

#: Execution price deviates from the nearest known price by more than
#: :data:`PRICE_DEVIATION_WARN_RATIO`.
WARNING_PRICE_DEVIATION: Final[str] = "price_deviation"

#: The ticket would take the settlement cash position below zero. Booking is
#: never refused for this (OP-06 struck, MD-5); the persistent indicator is
#: S5's and derives from the live balance, never from a stored flag.
WARNING_NEGATIVE_CASH: Final[str] = "negative_cash"

#: Net proceeds of a sell are zero or negative.
WARNING_NET_NON_POSITIVE: Final[str] = "net_non_positive"

#: The trade date lies in the future. The book records facts, so a
#: future-dated fact is unusual — but a PM may post-date deliberately.
WARNING_FUTURE_TRADE_DATE: Final[str] = "future_trade_date"

#: The complete warning vocabulary.
WARNING_IDENTIFIERS: Final[frozenset[str]] = frozenset(
    {
        WARNING_PRICE_DEVIATION,
        WARNING_NEGATIVE_CASH,
        WARNING_NET_NON_POSITIVE,
        WARNING_FUTURE_TRADE_DATE,
    }
)

# ---------------------------------------------------------------------------
# Block identifiers (decision record §2.9, extended by S2b)
#
# A block is reserved for an input that would corrupt the book's invariants
# (D-2). The first five are the identifiers the S4 surface has fixed copy for
# (MD-9); the service-internal completeness gaps below are a separate,
# non-decision vocabulary.
#
# S2b adds three more (D-N / D-O / D-P). They guard the book's invariants in
# exactly the §2.9 sense — a NAV silently overwritten past reversal, a second
# investment row with an existing name, a trade booked onto a retired
# position — but the record predates them and fixes no MD-9 copy for them, so
# S4 must supply wording when it renders them.
# ---------------------------------------------------------------------------

#: Ticket currency differs from the investment's (F-3). No silent conversion:
#: conversion lives at the ADR-0099 §4 reporting seam, never in a write path.
#: Has no reachable UI state (MD-8) — a service-layer guard only.
BLOCK_CURRENCY_MISMATCH: Final[str] = "currency_mismatch"

#: The sell would drive derived holdings below zero on some date (ADR-0097
#: §4). The instrument leg keeps this guard unconditionally (ADR-0128 Q-2).
BLOCK_OVERSELL: Final[str] = "oversell"

#: An order ticket carries no execution price.
BLOCK_MISSING_PRICE: Final[str] = "missing_price"

#: An investment-creating flow carries no AnlV category (MD-11, MD-21). A
#: transition guard on propose and book, never a schema constraint —
#: ``investments.anlv_code`` stays nullable and ``Save as draft`` is
#: untouched by it.
BLOCK_MISSING_ANLV: Final[str] = "missing_anlv"

#: A partial secondary sale was selected (R-2, MD-18).
#:
#: **This identifier has no service-side enforcement in v1** — a partial sale
#: is not representable in the schema (decision record §2.7: no fraction
#: column), so there is nothing for the service to refuse. The identifier
#: exists here so that the S4 surface refusal (MD-18, which disables Book
#: now, Propose *and* Save as draft) and the service speak one vocabulary.
BLOCK_PARTIAL_SECONDARY_SALE: Final[str] = "partial_secondary_sale"

#: An ``actual`` NAV row already exists at the ticket's ``trade_date`` (D-N).
#:
#: ``add_nav`` UPSERTs and ``prior_state`` is reserved for
#: ``investment_update`` effects (T-1 D-2), so a NAV the booking overwrote
#: could not be restored by a reversal. Refusing is the only way the emission
#: stays undoable; the user re-dates the ticket or corrects the NAV through
#: the ordinary CRUD surface.
BLOCK_NAV_EXISTS_AT_TRADE_DATE: Final[str] = "nav_exists_at_trade_date"

#: A creating flow's master data names an investment that already exists
#: (D-O). ``uq_investments_tenant_name`` would refuse the INSERT anyway; the
#: block turns a driver ``IntegrityError`` at Book into a named refusal at
#: Propose, where the composer can still do something about it.
BLOCK_DUPLICATE_INVESTMENT_NAME: Final[str] = "duplicate_investment_name"

#: An existing-investment flow names an investment that has been deactivated
#: (D-P). The settlement side has said this since D-F
#: (:data:`INCOMPLETE_INACTIVE_CASH_POSITION`); this is the same rule for the
#: traded side — trading a retired position would revive it by writing to it,
#: undoing a deliberate gesture.
BLOCK_INVESTMENT_INACTIVE: Final[str] = "investment_inactive"

#: The block vocabulary: decision record §2.9's five plus S2b's three.
BLOCK_IDENTIFIERS: Final[frozenset[str]] = frozenset(
    {
        BLOCK_CURRENCY_MISMATCH,
        BLOCK_OVERSELL,
        BLOCK_MISSING_PRICE,
        BLOCK_MISSING_ANLV,
        BLOCK_PARTIAL_SECONDARY_SALE,
        BLOCK_NAV_EXISTS_AT_TRADE_DATE,
        BLOCK_DUPLICATE_INVESTMENT_NAME,
        BLOCK_INVESTMENT_INACTIVE,
    }
)

# ---------------------------------------------------------------------------
# Completeness identifiers — service-internal, not part of §2.9
#
# The propose-time completeness rules (working document §3 under MD-11/MD-21)
# cover gaps that the §2.9 block list does not name, because the S4 composer
# prevents them structurally: it does not enable Propose until the fields are
# there, so these have no fixed MD-9 copy and never reach a template. They
# are nonetheless real refusals at the service seam — the seam is reachable
# from more than one surface — and they carry a precise identifier rather
# than being folded into an inaccurate §2.9 one.
#
# This set is deliberately *separate* from BLOCK_IDENTIFIERS: extending that
# frozenset would silently rewrite a decision the record fixed.
# ---------------------------------------------------------------------------

#: An existing-investment flow carries no ``investment_id``.
INCOMPLETE_MISSING_INVESTMENT: Final[str] = "missing_investment"

#: An investment-creating flow carries no usable ``master_data`` payload
#: (name and currency are the minimum, decision record §2.5).
INCOMPLETE_MISSING_MASTER_DATA: Final[str] = "missing_master_data"

#: An order ticket carries no unit quantity.
INCOMPLETE_MISSING_UNITS: Final[str] = "missing_units"

#: A secondary ticket carries none of the amounts its direction requires.
INCOMPLETE_MISSING_AMOUNT: Final[str] = "missing_amount"

#: A commitment ticket carries no positive ``commitment_amount``.
INCOMPLETE_MISSING_COMMITMENT_AMOUNT: Final[str] = "missing_commitment_amount"

#: A cash-moving flow has no confirmed settlement position, or the one it
#: names is not a cash position (MD-3, decision record §2.2). There is no
#: default-selection logic anywhere: NULL means "not yet confirmed", never
#: "pick one for me".
INCOMPLETE_MISSING_CASH_POSITION: Final[str] = "missing_cash_position"

#: A commitment ticket is not shaped like one — it must be a ``buy`` and must
#: name no settlement position (R-3 / MD-19; mirror of
#: ``ck_trade_tickets_commitment_shape``).
INCOMPLETE_COMMITMENT_SHAPE: Final[str] = "commitment_shape"

#: The named settlement position is a cash position in the right currency
#: but has been deactivated (D-F). Deliberately *not*
#: :data:`INCOMPLETE_MISSING_CASH_POSITION`: that identifier is the
#: structured signal the S4 surface turns into an inline "create a cash
#: position" offer, and offering to create one when a perfectly good row
#: already exists — merely retired — would be the wrong remedy.
INCOMPLETE_INACTIVE_CASH_POSITION: Final[str] = "inactive_cash_position"

#: ``set_inactive`` was asked for on a ticket that is not a full disposal —
#: a purchase, or a sale that leaves units behind (MD-7, D-E). A block
#: rather than a silent no-op: an inactive investment still holding units is
#: a corrupted book, which is exactly what D-2 reserves blocks for.
INCOMPLETE_SET_INACTIVE_NOT_FULL_DISPOSAL: Final[str] = "set_inactive_not_full_disposal"

#: Cancelling from ``proposed`` or ``approved`` without a reason.
INCOMPLETE_MISSING_CANCEL_REASON: Final[str] = "missing_cancel_reason"

#: The service-internal completeness vocabulary.
COMPLETENESS_IDENTIFIERS: Final[frozenset[str]] = frozenset(
    {
        INCOMPLETE_MISSING_INVESTMENT,
        INCOMPLETE_MISSING_MASTER_DATA,
        INCOMPLETE_MISSING_UNITS,
        INCOMPLETE_MISSING_AMOUNT,
        INCOMPLETE_MISSING_COMMITMENT_AMOUNT,
        INCOMPLETE_MISSING_CASH_POSITION,
        INCOMPLETE_INACTIVE_CASH_POSITION,
        INCOMPLETE_SET_INACTIVE_NOT_FULL_DISPOSAL,
        INCOMPLETE_COMMITMENT_SHAPE,
        INCOMPLETE_MISSING_CANCEL_REASON,
    }
)

# ---------------------------------------------------------------------------
# Reversal causes (ADR-0128 §6)
# ---------------------------------------------------------------------------
#
# Why a reversal refused, carried on
# :class:`core.exceptions.TicketReversalBlocked` as ``cause``. Each names a
# *different remedy*, which is the only reason there are five rather than one
# "cannot reverse": the operator's next move differs in each case, and S4's
# copy has to say which.

#: An emitted row has been edited through the CRUD since the booking. The
#: audit log is the witness (``UPDATE`` after the effect's ``emitted_at``),
#: because ``updated_at`` is not maintained on every target table.
REVERSAL_CAUSE_MODIFIED: Final[str] = "modified"

#: An emitted row is gone — deleted through the CRUD, or cascaded away.
REVERSAL_CAUSE_CONSUMED: Final[str] = "consumed"

#: An emitted ledger row cannot be deleted because the units it created have
#: since been sold on: removing it would drive holdings below zero
#: (ADR-0097 §4). The economic form of "consumed", and named apart from it
#: because the row is still there and the remedy is to reverse the later
#: trade first.
REVERSAL_CAUSE_HOLDINGS_CONSUMED: Final[str] = "holdings_consumed"

#: An ``investment_update`` before-image disagrees with the row as it stands
#: in a field the booking never touched, so restoring it would overwrite
#: somebody else's edit. Unreachable behind the ``modified`` check, and kept
#: because it is what makes the restore honest rather than trusting.
REVERSAL_CAUSE_UNRESTORABLE: Final[str] = "unrestorable"

#: Another trade ticket still references the investment this booking created,
#: so the shell cannot be deleted (``trade_tickets.investment_id`` and
#: ``cash_investment_id`` are both ``ON DELETE RESTRICT``). Only a *live*
#: reference refuses the reversal: those are the operator's to clear, and the
#: remedy is theirs to choose — re-point the drafts or cancel the proposals.
#: References that are themselves terminal degrade the reversal to the D-AC
#: retain path instead of blocking it, since a cancelled ticket keeps its FK
#: forever and no amount of operator work would ever free the row.
REVERSAL_CAUSE_REFERENCED_BY_TICKET: Final[str] = "referenced_by_ticket"

#: The reversal-cause vocabulary.
REVERSAL_CAUSES: Final[frozenset[str]] = frozenset(
    {
        REVERSAL_CAUSE_MODIFIED,
        REVERSAL_CAUSE_CONSUMED,
        REVERSAL_CAUSE_HOLDINGS_CONSUMED,
        REVERSAL_CAUSE_UNRESTORABLE,
        REVERSAL_CAUSE_REFERENCED_BY_TICKET,
    }
)


# ---------------------------------------------------------------------------
# Lifecycle vocabulary (ADR-0128 §3)
# ---------------------------------------------------------------------------

STATUS_DRAFT: Final[str] = "draft"
STATUS_PROPOSED: Final[str] = "proposed"
STATUS_APPROVED: Final[str] = "approved"
STATUS_SENT: Final[str] = "sent"
STATUS_ACKNOWLEDGED: Final[str] = "acknowledged"
STATUS_EXECUTED: Final[str] = "executed"
STATUS_BOOKED: Final[str] = "booked"
STATUS_CANCELLED: Final[str] = "cancelled"

#: All eight statuses, CHECK-defined from day one (``ck_trade_tickets_status``).
STATUSES: Final[frozenset[str]] = frozenset(
    {
        STATUS_DRAFT,
        STATUS_PROPOSED,
        STATUS_APPROVED,
        STATUS_SENT,
        STATUS_ACKNOWLEDGED,
        STATUS_EXECUTED,
        STATUS_BOOKED,
        STATUS_CANCELLED,
    }
)

#: The statuses a v1 transition can actually write. ``sent`` /
#: ``acknowledged`` / ``executed`` are defined but unreachable — ADR-0129
#: arms them, and a provider confirmation then lands in ``booked`` through
#: exactly this ADR's machinery.
V1_REACHABLE_STATUSES: Final[frozenset[str]] = frozenset(
    {
        STATUS_DRAFT,
        STATUS_PROPOSED,
        STATUS_APPROVED,
        STATUS_BOOKED,
        STATUS_CANCELLED,
    }
)

#: The statuses the "Book now" gesture may start from. Exactly the
#: pre-terminal v1 stations: a ``booked`` ticket is already a fact and is
#: *reversed* rather than re-booked (ADR-0128 §6), and a ``cancelled`` one is
#: a decision that was withdrawn. Equal to :data:`CANCELLABLE_STATUSES` today
#: and named separately on purpose — the two answer different questions, and
#: the four-eyes setting that later narrows booking must not silently narrow
#: cancellation with it.
BOOKABLE_STATUSES: Final[tuple[str, ...]] = (
    STATUS_DRAFT,
    STATUS_PROPOSED,
    STATUS_APPROVED,
)

#: The statuses a plain cancellation may start from. Cancelling a ``booked``
#: ticket is the S2c *reversal* — the enumerated effects are deleted in one
#: DB transaction (ADR-0128 §6) — not a status flip, so ``booked`` is absent
#: here on purpose.
CANCELLABLE_STATUSES: Final[tuple[str, ...]] = (
    STATUS_DRAFT,
    STATUS_PROPOSED,
    STATUS_APPROVED,
)

#: The statuses whose cancellation requires a reason. A draft is private
#: workspace and needs none; once a ticket has been proposed it is a
#: decision others may have seen, so withdrawing it is explained.
CANCEL_REASON_REQUIRED_STATUSES: Final[tuple[str, ...]] = (
    STATUS_PROPOSED,
    STATUS_APPROVED,
)

# ---------------------------------------------------------------------------
# Kind and direction (mirrors ck_trade_tickets_kind / ..._direction)
# ---------------------------------------------------------------------------

KIND_ORDER: Final[str] = "order"
KIND_COMMITMENT: Final[str] = "commitment"
KIND_SECONDARY: Final[str] = "secondary"

#: The three ticket kinds.
KINDS: Final[frozenset[str]] = frozenset({KIND_ORDER, KIND_COMMITMENT, KIND_SECONDARY})

DIRECTION_BUY: Final[str] = "buy"
DIRECTION_SELL: Final[str] = "sell"

#: The two directions. ``units`` is stored unsigned; the sign is applied at
#: emission (working document §1.1), so direction is the only carrier.
DIRECTIONS: Final[frozenset[str]] = frozenset({DIRECTION_BUY, DIRECTION_SELL})

# ---------------------------------------------------------------------------
# Master-data payload keys (decision record §2.5)
#
# The ticket carries the full U-NEW / R-COMMIT / R-SEC-BUY master-data
# inventory as an opaque JSONB payload until booking emits the ``investments``
# row (MD-12 / MD-15). These are the canonical key names from here on: S2
# (emission) and S4 (composer) import them and never re-type the strings,
# because a typo in a JSONB key is invisible to every schema guard there is.
# ---------------------------------------------------------------------------

MD_NAME: Final[str] = "name"
MD_INVESTMENT_TYPE: Final[str] = "investment_type"
MD_ASSET_CLASS_ID: Final[str] = "asset_class_id"
MD_CURRENCY: Final[str] = "currency"
MD_ANLV_CODE: Final[str] = "anlv_code"
MD_IDENTIFIER_SCHEME: Final[str] = "identifier_scheme"
MD_IDENTIFIER_VALUE: Final[str] = "identifier_value"
MD_FIGI: Final[str] = "figi"
MD_MANAGER: Final[str] = "manager"
MD_REGION: Final[str] = "region"
MD_VINTAGE_YEAR: Final[str] = "vintage_year"
MD_COMMITMENT_AMOUNT: Final[str] = "commitment_amount"
MD_PURCHASE_PRICE: Final[str] = "purchase_price"
MD_ACQUIRED_NAV: Final[str] = "acquired_nav"
MD_ASSUMED_UNFUNDED: Final[str] = "assumed_unfunded"


__all__ = [
    "BLOCK_CURRENCY_MISMATCH",
    "BLOCK_DUPLICATE_INVESTMENT_NAME",
    "BLOCK_IDENTIFIERS",
    "BLOCK_INVESTMENT_INACTIVE",
    "BLOCK_MISSING_ANLV",
    "BLOCK_MISSING_PRICE",
    "BLOCK_NAV_EXISTS_AT_TRADE_DATE",
    "BLOCK_OVERSELL",
    "BLOCK_PARTIAL_SECONDARY_SALE",
    "BOOKABLE_STATUSES",
    "CANCELLABLE_STATUSES",
    "CANCEL_REASON_REQUIRED_STATUSES",
    "COMPLETENESS_IDENTIFIERS",
    "DIRECTIONS",
    "DIRECTION_BUY",
    "DIRECTION_SELL",
    "INCOMPLETE_COMMITMENT_SHAPE",
    "INCOMPLETE_INACTIVE_CASH_POSITION",
    "INCOMPLETE_MISSING_AMOUNT",
    "INCOMPLETE_MISSING_CANCEL_REASON",
    "INCOMPLETE_MISSING_CASH_POSITION",
    "INCOMPLETE_MISSING_COMMITMENT_AMOUNT",
    "INCOMPLETE_MISSING_INVESTMENT",
    "INCOMPLETE_MISSING_MASTER_DATA",
    "INCOMPLETE_MISSING_UNITS",
    "INCOMPLETE_SET_INACTIVE_NOT_FULL_DISPOSAL",
    "KINDS",
    "KIND_COMMITMENT",
    "KIND_ORDER",
    "KIND_SECONDARY",
    "MD_ACQUIRED_NAV",
    "MD_ANLV_CODE",
    "MD_ASSET_CLASS_ID",
    "MD_ASSUMED_UNFUNDED",
    "MD_COMMITMENT_AMOUNT",
    "MD_CURRENCY",
    "MD_FIGI",
    "MD_IDENTIFIER_SCHEME",
    "MD_IDENTIFIER_VALUE",
    "MD_INVESTMENT_TYPE",
    "MD_MANAGER",
    "MD_NAME",
    "MD_PURCHASE_PRICE",
    "MD_REGION",
    "MD_VINTAGE_YEAR",
    "PRICE_DEVIATION_WARN_RATIO",
    "REVERSAL_CAUSES",
    "REVERSAL_CAUSE_CONSUMED",
    "REVERSAL_CAUSE_HOLDINGS_CONSUMED",
    "REVERSAL_CAUSE_MODIFIED",
    "REVERSAL_CAUSE_REFERENCED_BY_TICKET",
    "REVERSAL_CAUSE_UNRESTORABLE",
    "STATUSES",
    "STATUS_ACKNOWLEDGED",
    "STATUS_APPROVED",
    "STATUS_BOOKED",
    "STATUS_CANCELLED",
    "STATUS_DRAFT",
    "STATUS_EXECUTED",
    "STATUS_PROPOSED",
    "STATUS_SENT",
    "V1_REACHABLE_STATUSES",
    "WARNING_FUTURE_TRADE_DATE",
    "WARNING_IDENTIFIERS",
    "WARNING_NEGATIVE_CASH",
    "WARNING_NET_NON_POSITIVE",
    "WARNING_PRICE_DEVIATION",
]
