# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The overlay contract — four transformation kinds, closed (ADR-0104 §2).

An **overlay** is an ordered list of pure transformations applied to plan
frames assembled from the book. It is *ephemeral*: a parameter set, never a
dataset (ADR-0104 §1). Nothing in this package reads or writes the database;
the overlay is recomputed from *(book, parameters)* on every request.

**The kind discriminator is closed.** Exactly four kinds exist, and there is
no extension hook — no registry of kinds, no plugin seam, no subclass
protocol. ADR-0104 §Alternatives states why: both successors (ADR-D, the TA
engine, #023; ADR-E, scenario regimes, #034) are meant to extend the *value
sets* inside these kinds, not to reshape the contract. A hook here would be a
structure held for an undecided future.

The four kinds, their scope, and what they act on (ADR-0104 §2):

* ``insert_transaction`` — one investment; its value path plus the
  settling cash path. Dispatches on ``resolve_archetype``.
* ``repace_flows`` — one capital-account investment; its remaining
  plan-flow profile. Dispatches on ``resolve_archetype``.
* ``market_shock`` — one archetype; the value paths. Dispatches on
  ``resolve_archetype``.
* ``fx_shock`` — one currency; the **conversion seam** rather than any
  value path, which is why it is a kind of its own and not a scope
  variant of ``market_shock``. Archetype-blind.

**All four kinds are dataclasses, and all four are applied** (S34.2, roadmap
#034) — but not at the same seam. Three fold through
:func:`~services.overlay.pipeline.apply_overlay` as ``frames → frames``
executors. ``fx_shock`` does not, and this is structural rather than pending:
the path it restates is the plan-world FX path, which is not in
:class:`~services.overlay.pipeline.PlanFrames` at all. The
:func:`~services.overlay.pipeline.partition_fx_shocks` router splits the shocks
out at the seam and :func:`services.fx.plan_shock.shock_plan_fx_path` restates
the rates; an ``fx_shock`` handed to the fold still raises
:class:`~services.overlay.errors.ExecutorNotRegisteredError`, which is now a
mis-routing error rather than a not-yet-implemented one.

Application order is list order. An **empty overlay is legal** and means
*baseline*: it is what the Planning Desk's Baseline/Scenario toggle sends
(ADR-0104 §4), and :func:`services.overlay.pipeline.apply_overlay` returns
the frames unchanged for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import ClassVar
from uuid import UUID

from services.investments.archetype import Archetype
from services.overlay.errors import FactorOutOfBoundsError


class TransformationKind(StrEnum):
    """The four transformation kinds of the overlay contract (ADR-0104 §2).

    The set is closed: a fifth kind is an ADR-level decision, not a code
    change. Mirrors the :class:`services.investments.archetype.Archetype`
    idiom — a ``StrEnum`` whose values are the wire strings, so the
    serialised form and the in-memory form never drift apart.

    Members:
        INSERT_TRANSACTION: A hypothetical trade on one investment. Adjusts
            the investment's value path and settles against the cash path of
            the transaction's currency. Executable from S2.1b.
        REPACE_FLOWS: Time-scales the **remaining** manager-plan drawdown
            profile of one capital-account investment by a factor in
            ``[FACTOR_MIN, FACTOR_MAX]``. Executable from S2.1b.
        MARKET_SHOCK: A price-level / NAV-level shift over one archetype,
            magnitude in per cent (ADR-0104 §2). Executable from S34.1.
        FX_SHOCK: Restates the plan-world FX path of one currency at the
            conversion seam (ADR-0104 §3, N3). Applied since S34.2 — at the
            seam, not by the fold: it is the one kind
            :data:`EXECUTABLE_KINDS` does not carry.
    """

    INSERT_TRANSACTION = "insert_transaction"
    REPACE_FLOWS = "repace_flows"
    MARKET_SHOCK = "market_shock"
    FX_SHOCK = "fx_shock"


#: The kinds :func:`services.overlay.pipeline.apply_overlay` **executes** — the
#: value transformations, folded ``frames → frames``.
#:
#: ``FX_SHOCK`` is deliberately absent, and this is its permanent state rather
#: than a gap awaiting a fourth executor. It is a complete value — it
#: constructs, it round-trips through the encoding, and since S34.2 it is fully
#: applied — but it is applied at the **conversion seam** (ADR-0104 §2/§3, N3),
#: not by the fold: it restates a currency's plan-world FX path, and that path
#: is not in :class:`services.overlay.pipeline.PlanFrames`. It lives in the
#: converter, built from rows this package is forbidden to read. So the seam
#: splits the shocks out (:func:`services.overlay.pipeline.partition_fx_shocks`)
#: and hands them to :func:`services.fx.plan_shock.shock_plan_fx_path`, while
#: an overlay carrying one *into the fold* still fails loudly with
#: :class:`services.overlay.errors.ExecutorNotRegisteredError` — a mis-route,
#: never a silent baseline.
#:
#: Read the name as "executable **by the fold**", not as "computable".
EXECUTABLE_KINDS: frozenset[TransformationKind] = frozenset(
    {
        TransformationKind.INSERT_TRANSACTION,
        TransformationKind.REPACE_FLOWS,
        TransformationKind.MARKET_SHOCK,
    }
)

#: Inclusive lower bound of a re-pacing factor (ADR-0104 §2, D18 Variant B).
FACTOR_MIN: Decimal = Decimal("0.5")

#: Inclusive upper bound of a re-pacing factor (ADR-0104 §2, D18 Variant B).
FACTOR_MAX: Decimal = Decimal("2.0")

#: The factor that reproduces the manager plan exactly. ADR-0104 §2 makes
#: mid-position bit-identity a regression anchor of the pipeline, and §4
#: makes it a UI rule (a mid-position pacing emits no chip — it *is* the
#: plan).
FACTOR_NEUTRAL: Decimal = Decimal("1.0")


@dataclass(frozen=True)
class InsertTransaction:
    """A hypothetical transaction inserted into the plan world (ADR-0104 §2).

    Carries the field shape of the **actual**-entry form
    (``web/templates/investments/_position_form.html``, ADR-0097 §2) so the
    hypothetical and the real entry describe a transaction the same way. It
    is nonetheless a strictly separate path: an ``InsertTransaction`` never
    writes ``position_transactions``, ``investment_navs``, or
    ``instrument_prices`` (ADR-0104 §2, annex §B.2), and the entry
    affordances stay separate too (ADR-0104 §7 — actuals on the investment
    detail page, hypotheticals on the Planning Desk).

    The form's ``csrf_token``, ``transaction_id``, ``note``, and ``source``
    fields are **not** overlay parameters: they are persistence and
    request-integrity concerns, and an ephemeral transformation has neither.

    Attributes:
        investment_id: The investment the hypothetical trade lands on.
        txn_type: The ADR-0097 §2 transaction type (``opening``, ``buy``,
            ``sell``, ``transfer``). Held as a string, exactly as the form
            submits it; the S2.1b executor interprets it.
        trade_date: The trade date of the hypothetical transaction.
        units: Signed unit count, following the ADR-0097 §2 sign rules
            (positive for opening and buy, negative for sell).
        price_per_unit: Price per unit, or ``None`` where the type permits
            its omission (opening, transfer).
        consideration: Optional signed cash effect. ``None`` where not
            stated; the executor derives it from ``units × price_per_unit``.
        currency: The transaction's currency — the currency whose cash path
            the trade settles against (ADR-0104 §2, settle-against-cash).
    """

    kind: ClassVar[TransformationKind] = TransformationKind.INSERT_TRANSACTION

    investment_id: UUID
    txn_type: str
    trade_date: date
    units: Decimal
    price_per_unit: Decimal | None
    consideration: Decimal | None
    currency: str


@dataclass(frozen=True)
class RepaceFlows:
    """A time-scaling of one investment's remaining plan flows (ADR-0104 §2).

    Stretches (``factor > 1``) or compresses (``factor < 1``) the
    **remaining** manager-plan drawdown profile — the plan flows strictly
    after the plan/actual seam. Realised history is never touched
    (ADR-0104 §5, the identical-history invariant), and no ``investor_flow``
    is ever re-paced (ADR-0103 §5, the exemption invariant the S2.1b
    executor enforces by importing ``OVERLAY_EXEMPT_FLOW_TYPES`` from
    :mod:`services.investments.flow_type_invariants`).

    The bounds are validated at construction: an out-of-bounds
    ``RepaceFlows`` cannot exist in memory, so no executor and no serialiser
    has to re-check them.

    Attributes:
        investment_id: The capital-account investment whose remaining plan
            flows are re-paced.
        factor: The time-scaling factor, in ``[0.5, 2.0]`` inclusive.
            :data:`FACTOR_NEUTRAL` (1.0) reproduces the plan exactly.

    Raises:
        FactorOutOfBoundsError: If ``factor`` lies outside
            ``[FACTOR_MIN, FACTOR_MAX]``.
    """

    kind: ClassVar[TransformationKind] = TransformationKind.REPACE_FLOWS

    investment_id: UUID
    factor: Decimal

    def __post_init__(self) -> None:
        """Validate the factor against the ADR-0104 §2 bounds."""
        if not FACTOR_MIN <= self.factor <= FACTOR_MAX:
            raise FactorOutOfBoundsError(
                f"re-pacing factor {self.factor} is outside the ADR-0104 §2 "
                f"bounds [{FACTOR_MIN}, {FACTOR_MAX}] (inclusive)"
            )


@dataclass(frozen=True)
class MarketShock:
    """A price-level / NAV-level shift over one archetype (ADR-0104 §2).

    Scoped to an **archetype**, not to an investment: ADR-0104 §2's kind table
    gives its scope as "one archetype" and its dispatch as ``resolve_archetype``.
    A market shock is a statement about a *class* of holdings — "private
    markets are marked down 20 %" — and the archetype is the routing concept
    the codebase already has for that class (ADR-0082 §1). Every investment in
    the plan world resolving to :attr:`archetype` is shocked; one that resolves
    elsewhere is untouched.

    The shift is **multiplicative on the level**: each plan value point becomes
    ``level × (1 + magnitude / 100)``. ADR-0104 §2 states the magnitude "in %",
    and a per-cent magnitude on a *level* has no additive reading — a NAV in
    euros cannot gain "−20 %" of nothing. So a magnitude of ``-20`` marks every
    targeted plan value down to 80 % of its baseline, at every point of the
    plan horizon.

    **Timing is immediate at t₀ (v1).** The full magnitude is in force from the
    first plan point onward — no ramp, no lag, no decay. ADR-0104 §2 puts
    richer timing regimes (paths, lagged, mean-reverting) in ADR-E territory
    and explicitly out of scope, so this dataclass carries **no** timing, rate,
    duration, or spread field. A future regime is an ADR-level decision that
    extends the *value set* inside this kind, not a field nobody has asked for
    yet.

    Attributes:
        archetype: The archetype whose plan value paths are shocked.
        magnitude: The level shift **in per cent** (ADR-0104 §2). Negative
            marks down, positive marks up. ``Decimal("-20")`` is −20 %.
            ``Decimal("0")`` is the identity — the shock that says nothing.
    """

    kind: ClassVar[TransformationKind] = TransformationKind.MARKET_SHOCK

    archetype: Archetype
    magnitude: Decimal


@dataclass(frozen=True)
class FxShock:
    """A move in one currency's plan-world FX path (ADR-0104 §2/§3, N3).

    Scoped to a **currency** and **archetype-blind** — the one kind whose
    dispatch column in ADR-0104 §2's table reads "none". It restates the
    held-flat plan-world FX path of :attr:`currency` (§3, N1), which translates
    *every* position of that currency — NAVs, plan flows, cash paths — into the
    functional currency. It therefore executes at the **conversion seam**
    (ADR-0099 §4), after the value-level transformations and before
    functional-currency aggregation.

    That is why it is a kind of its own rather than a scope variant of
    :class:`MarketShock`, and ADR-0104 §2 says so outright: the intervention
    point — values versus seam — is the essential difference between the two,
    and hiding it behind a scope discriminant would embed a concealed branch in
    one executor.

    **It has no fold executor, and never will** (S34.2). The path it restates
    is not in :class:`~services.overlay.pipeline.PlanFrames` — it lives in the
    :class:`~services.fx.functional_currency.PortfolioFxConverter`, built from
    rows this package may not read — so a ``frames → frames`` executor would
    have nothing to act on. The seam applies it instead:
    :func:`~services.overlay.pipeline.partition_fx_shocks` splits the shocks
    out and :func:`services.fx.plan_shock.shock_plan_fx_path` restates the
    paths, while :func:`~services.overlay.pipeline.apply_overlay` goes on
    raising :class:`~services.overlay.errors.ExecutorNotRegisteredError` for one
    routed to it. A shock that reached the fold is a mis-route, and saying so
    is better than the alternative: a scenario that quietly equals its baseline
    is the one failure mode the Planning Desk must never have.

    **Sign and unit.** ``magnitude`` is a per-cent move in the *currency's own
    value*: ``-10`` means one unit of :attr:`currency` is worth 90 % of what it
    was, so every position denominated in it translates 10 % lower into the
    functional currency. Under the normative quoting convention (ADR-0099 §2,
    ``rate_to_reference`` = the price of one unit of the currency in the
    reference currency) that is one multiplication on the rate, and the two
    readings of "a −10 % FX shock" — *the currency is worth 10 % less* and *the
    rate number drops 10 %* — are the same statement rather than opposite ones.

    **Timing follows :class:`MarketShock`:** immediate and in force from the
    first plan point, and the realised segment of the rate path is never
    restated (ADR-0104 §5, the identical-history invariant — the actual columns
    convert at the rates that actually prevailed).

    Attributes:
        currency: The currency whose plan-world FX path moves. The conversion
            seam is the unit here; no value path is touched directly. The
            functional currency itself is a no-op — it is the numéraire, and
            its translation into itself is the identity.
        magnitude: The move **in per cent**, the same unit as
            :attr:`MarketShock.magnitude`, so the unified Scope → Operator →
            Magnitude → Timing form of ADR-0104 §2 reads one way across both
            shock kinds.
    """

    kind: ClassVar[TransformationKind] = TransformationKind.FX_SHOCK

    currency: str
    magnitude: Decimal


#: A single transformation. The union is closed over the four ADR-0104 §2
#: kinds. Membership is not executability: an ``FxShock`` is a legal value of
#: this union whose executor ships with S34.2 — see :data:`EXECUTABLE_KINDS`.
Transformation = InsertTransaction | RepaceFlows | MarketShock | FxShock

#: An overlay: the **ordered** list of transformations, applied in list
#: order. A tuple rather than a list — an overlay is a value, compared and
#: round-tripped by equality (ADR-0104 §4), never mutated in place.
Overlay = tuple[Transformation, ...]

#: The empty overlay — the baseline (ADR-0104 §4). What the Baseline side of
#: the Baseline/Scenario toggle sends, and the identity of
#: :func:`services.overlay.pipeline.apply_overlay`.
EMPTY_OVERLAY: Overlay = ()


__all__ = [
    "EMPTY_OVERLAY",
    "EXECUTABLE_KINDS",
    "FACTOR_MAX",
    "FACTOR_MIN",
    "FACTOR_NEUTRAL",
    "FxShock",
    "InsertTransaction",
    "MarketShock",
    "Overlay",
    "RepaceFlows",
    "Transformation",
    "TransformationKind",
]
