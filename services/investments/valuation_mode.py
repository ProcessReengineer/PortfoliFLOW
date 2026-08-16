# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The valuation-mode flip predicate — when an investment may be unitised.

Per ADR-0097 §6 the flip from ``'reported'`` to ``'unitised'`` is an
explicit, **one-way** operator act gated on two preconditions:

1. the investment's ``investment_type`` is ``listed_equity``,
   ``listed_bonds``, or — since ADR-0103 §1 — ``cash``; **and**
2. an ``opening`` transaction anchors its ledger — synthesised from the
   Excel units row (§7), derived from the first Cash-sheet statement date
   (ADR-0103 §4), or entered on the web surface.

An already-unitised investment fails the same predicate: the flip is
one-way, and corrections go through ledger edits rather than a mode flap.

The predicate is a pure, DB-free function mirroring the precedent of
:mod:`services.investments.market_linked` — the facts it judges arrive as
plain parameters. Both consumers of the preconditions share it: the flip
itself (:meth:`services.investments.InvestmentService.flip_to_unitised`),
which raises on a non-``None`` return, and the positions panel, which
renders the same string as the disabled flip button's explanation. A
second, independent formulation of the rules in the web layer would drift
from this one.

The returned strings are **operator-facing UI copy** (English, ADR-0008):
each names the action that would satisfy the failed precondition.
"""

from __future__ import annotations

#: The ``investment_type`` values that may carry a unitised valuation
#: (ADR-0097 §6 as extended by ADR-0103 §1): the two listed types plus
#: ``cash``.
#:
#: ADR-0100 §1 held that a cash position stays ``'reported'`` because the
#: balance *is* the NAV, never units × price. **ADR-0103 §1 supersedes that
#: reading**: cash is the *degenerate* unitised case. Units are currency
#: units, so balance ≡ holdings (derived from the ledger, ADR-0097 §4), and
#: every price is a stored ``1.0000`` — one per statement date. Then
#: ``holdings × price`` reproduces the statement balance exactly, and the
#: unchanged ADR-0098 materialisation values a cash position with no special
#: case anywhere in the book path.
#:
#: This is where this set and
#: :data:`services.investments.market_linked.MARKET_LINKED_TYPES` **diverge
#: for the first time** — the divergence the two constants were always kept
#: separate to allow, back when their membership was still identical. They
#: answer different questions: that one asks "may a live import address this
#: instrument", this one asks "may this instrument's NAV be computed from
#: units". Cash answers *yes* to the second and *permanently no* to the
#: first — it is unitisable but never live-addressable, because a currency
#: has no market price to fetch and its unity prices come only from the
#: import path (ADR-0103 §1; pinned by
#: ``tests/services/investments/test_market_linked.py``).
UNITISABLE_TYPES: frozenset[str] = frozenset({"listed_equity", "listed_bonds", "cash"})


def flip_precondition_error(
    investment_type: str,
    valuation_mode: str,
    *,
    has_opening: bool,
) -> str | None:
    """Return why this investment may not be flipped to ``'unitised'``.

    Args:
        investment_type: The investment's ``investment_type`` value.
        valuation_mode: The investment's current ``valuation_mode`` —
            ``'reported'`` or ``'unitised'``.
        has_opening: Whether an ``opening`` transaction exists on the
            investment's ledger. Passed in so the predicate stays DB-free.

    Returns:
        ``None`` when every ADR-0097 §6 precondition holds and the flip may
        proceed; otherwise a single operator-facing sentence naming the
        blocking condition, suitable for direct display.
    """
    if valuation_mode == "unitised":
        return "This investment already uses unitised valuation."
    if investment_type not in UNITISABLE_TYPES:
        return "Unitised valuation is available for listed equity, listed bonds, and cash only."
    if not has_opening:
        return "Add an opening transaction before switching to unitised valuation."
    return None


def can_flip_to_unitised(
    investment_type: str,
    valuation_mode: str,
    *,
    has_opening: bool,
) -> bool:
    """Return whether the flip to ``'unitised'`` is permitted.

    The boolean complement of :func:`flip_precondition_error`, for callers
    that need the decision without the explanation.

    Args:
        investment_type: The investment's ``investment_type`` value.
        valuation_mode: The investment's current ``valuation_mode``.
        has_opening: Whether an ``opening`` transaction exists.

    Returns:
        ``True`` iff every ADR-0097 §6 precondition holds.
    """
    return flip_precondition_error(investment_type, valuation_mode, has_opening=has_opening) is None


def shows_positions_panel(
    investment_type: str,
    valuation_mode: str,
    *,
    has_transactions: bool,
) -> bool:
    """Return whether the investment detail page shows the positions panel.

    The panel is shown only where a ledger is meaningful: an already-unitised
    investment, a unitisable type (which an operator may still flip), or any
    investment that already carries ledger rows — the last clause defensive,
    so a ledger on an unexpected type stays visible rather than becoming
    unreachable. Every other investment — the private-markets majority —
    renders exactly as it did before ADR-0097, which is this strand's
    regression obligation.

    Args:
        investment_type: The investment's ``investment_type`` value.
        valuation_mode: The investment's current ``valuation_mode``.
        has_transactions: Whether the investment carries any ledger rows.

    Returns:
        ``True`` iff the panel is relevant for this investment.
    """
    return valuation_mode == "unitised" or investment_type in UNITISABLE_TYPES or has_transactions


__all__ = [
    "UNITISABLE_TYPES",
    "can_flip_to_unitised",
    "flip_precondition_error",
    "shows_positions_panel",
]
