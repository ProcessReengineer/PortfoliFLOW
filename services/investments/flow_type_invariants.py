# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Flow types the scenario and TA overlays may never touch (ADR-0103 §5).

ADR-0103 §5 states the **exemption invariant** as binding and
regression-testable: *no scenario transformation and no TA transformation
ever creates, deletes, re-paces, or re-scales an investor flow.* The reason
is a separation of authorship. A scenario asks "what if the portfolio's
*investments* behaved differently"; an investor flow is not the portfolio's
behaviour but the **investor's** — a contribution to, or withdrawal from,
the mandate (decision N4). Letting an overlay re-scale one would let the
plan world quietly invent capital the investor never committed, and the
cash plan path (ADR-0103 §6) would then project a balance nobody funded.

This module owns the invariant's **definition** — the single seam. ADR-0104
§2 owes its **enforcement** in the overlay executors, which must import
:data:`OVERLAY_EXEMPT_FLOW_TYPES` rather than restate the set: a second
formulation drifts from this one the first time the set changes, and the
whole point of an invariant is that it cannot be locally overridden.

The module is import-pure — stdlib only, no database, no FastAPI, no
provider SDK — following the precedent of
:mod:`services.investments.cashflow_dedup_key` and
:mod:`services.investments.valuation_mode`, and guarded by
``tests/regression/test_flow_type_invariants_pure.py``. That guard also
pins the set against the schema's own ``flow_type`` members, so the
invariant can never name a flow type the database does not know.
"""

from __future__ import annotations

#: The ``flow_type`` values exempt from every overlay transformation
#: (ADR-0103 §5). ``'investor_flow'`` is the sole member: it is the only
#: flow type whose author is the investor rather than the portfolio, and so
#: the only one a scenario or TA overlay has no standing to change. Every
#: other flow type — a call, a distribution, a fee, an income payment — is
#: portfolio behaviour and therefore legitimately re-paceable or
#: re-scalable by an overlay.
OVERLAY_EXEMPT_FLOW_TYPES: frozenset[str] = frozenset({"investor_flow"})


def is_overlay_exempt(flow_type: str) -> bool:
    """Return whether an overlay must leave this flow type untouched.

    The predicate form of :data:`OVERLAY_EXEMPT_FLOW_TYPES`, for the
    ADR-0104 executors that filter a flow collection row by row.

    Args:
        flow_type: A canonical ``investment_cashflows.flow_type`` value.

    Returns:
        ``True`` iff no scenario or TA transformation may create, delete,
        re-pace, or re-scale a flow of this type.
    """
    return flow_type in OVERLAY_EXEMPT_FLOW_TYPES


__all__ = [
    "OVERLAY_EXEMPT_FLOW_TYPES",
    "is_overlay_exempt",
]
