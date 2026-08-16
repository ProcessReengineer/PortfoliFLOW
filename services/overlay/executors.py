# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The executable transformation kinds (ADR-0104 §2).

Importing this module **registers** ``insert_transaction``, ``repace_flows``,
and ``market_shock`` in :data:`services.overlay.pipeline._EXECUTORS`; the
package façade imports it, so
:func:`services.overlay.pipeline.apply_overlay` can execute them for anyone who
imports :mod:`services.overlay`. ``fx_shock`` stays unregistered and keeps
raising :class:`~services.overlay.errors.ExecutorNotRegisteredError` until
S34.2: it acts at the conversion seam (ADR-0104 §3, N3), not on a value path,
and that seam is a different hook from the one the three executors here share.

Every executor is a **pure function** ``frames → frames`` (ADR-0104 §2): no
repository, no session, no clock, no randomness, and no mutation of the
frames it is handed. A scenario must be reproducible from *(book,
parameters)* alone, so a hidden input — a "now", a database read, a mutated
input frame surviving into the next request — would break the one property
the Planning Desk rests on.

Three rules are enforced here rather than restated:

* **Archetype dispatch.** Routing goes through
  :func:`services.investments.archetype.resolve_archetype`. No overlay code
  compares an ``investment_type`` against a string literal — that would be a
  second routing concept beside the one ADR-0082 established.
* **The exemption invariant** (ADR-0103 §5). ``investor_flow`` is spared by
  importing :func:`services.investments.flow_type_invariants.is_overlay_exempt`
  — the single formulation, never a local copy. A scenario asks what if the
  portfolio's *investments* behaved differently; it has no standing to invent
  capital the investor never committed.
* **Identical history** (ADR-0104 §5). Nothing at or before ``frames.t0`` is
  ever touched: an inserted transaction dated there is an error, a re-paced
  flow is drawn from the *remaining* profile only, and a market shock rescales
  strictly to the right of the seam. Because
  :func:`services.overlay.steps.add_step` only ever steps a balance from a
  date onward, and :func:`services.overlay.steps.scale_after` only ever
  rescales after one, the invariant holds by construction — the anchors assert
  it, they do not create it.

**Two of the three executors preserve AUM; one does not, and must not.** An
``insert_transaction`` re-allocates the plan world (value ``+C``, cash ``−C``)
and a ``repace_flows`` moves capital in time without resizing it, so both leave
``Σ NAV`` unchanged. A ``market_shock`` **deliberately moves NAV, and therefore
AUM** — see :func:`execute_market_shock`. That is the transformation's whole
point, not a leak in it: a scenario in which private markets fall 20 % and the
portfolio total does not move would be describing nothing.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from services.investments.archetype import Archetype, resolve_archetype
from services.investments.flow_type_invariants import is_overlay_exempt
from services.overlay.contract import (
    FACTOR_NEUTRAL,
    InsertTransaction,
    MarketShock,
    RepaceFlows,
    Transformation,
    TransformationKind,
)
from services.overlay.errors import (
    CurrencyMismatchError,
    HistoricTradeDateError,
    MissingCashPathError,
    NotRepaceableError,
    UnderivableConsiderationError,
    UnknownInvestmentError,
)

# The executor table is a plain module-level mapping, not a registration API
# (ADR-0104 §2: the kind discriminator is closed, so an extension hook would
# invite the fifth-kind-by-plugin the ADR rules out). Filling it from the
# module that defines the executors keeps the table and its rows in one
# import, and keeps `pipeline` free of a back-import of this module.
from services.overlay.pipeline import (  # noqa: PLC2701 — same-package table
    _EXECUTORS,
    PlanFrames,
    PlanInvestment,
)
from services.overlay.steps import add_step, scale_after, zero_path

#: The divisor that turns an ADR-0104 §2 per-cent magnitude into a ratio. A
#: shock's magnitude is stated in per cent (``-20`` is −20 %), and the level it
#: acts on is multiplied by ``1 + magnitude / 100``.
_PER_CENT: Decimal = Decimal(100)


def _resolve_investment(
    frames: PlanFrames, transformation: InsertTransaction | RepaceFlows
) -> PlanInvestment:
    """Return the targeted investment, or raise if the frames lack it.

    Typed against the two **investment-scoped** kinds. The shock kinds carry no
    ``investment_id`` — a ``market_shock`` is scoped to an archetype and an
    ``fx_shock`` to a currency (ADR-0104 §2) — so they have no targeted
    investment to resolve, and the signature says so rather than leaving it to
    an ``AttributeError`` at runtime.
    """
    investment = frames.investments.get(transformation.investment_id)
    if investment is None:
        raise UnknownInvestmentError(
            f"transformation '{transformation.kind.value}' targets investment "
            f"{transformation.investment_id}, which the plan frames do not "
            f"carry"
        )
    return investment


def derive_consideration(transformation: InsertTransaction, archetype: Archetype) -> Decimal:
    """Return the transaction's signed cash effect.

    Stated outright where the form supplied it, otherwise derived as
    ``units × price_per_unit``. The sign follows ADR-0097 §2: units are
    positive for an opening or a buy and negative for a sell, so a buy
    produces a positive consideration (value in, cash out) and a sell a
    negative one.

    **Public API of the package** (ADR-0104 §8.4). The Planning Desk's
    hypothetical-transaction table has to state exactly the amount the executor
    will settle against the cash path, and a second copy of this rule there
    would be a number the table promises and the engine need not keep. Since
    the rule is *shared* rather than internal, it carries a public name and is
    exported from :mod:`services.overlay` — the alternative, a private import
    across a package boundary, was a cross-package reach into a leading
    underscore and is retired.

    Args:
        transformation: The hypothetical transaction.
        archetype: The target's archetype — named in the error path, so a
            failure says *what kind of thing* could not be derived.

    Returns:
        The signed cash effect, in the transaction's currency.

    Raises:
        UnderivableConsiderationError: If neither the consideration nor a
            price per unit is available.
    """
    if transformation.consideration is not None:
        return transformation.consideration
    if transformation.price_per_unit is None:
        raise UnderivableConsiderationError(
            f"cannot derive the cash effect of the '{transformation.txn_type}' "
            f"on investment {transformation.investment_id} "
            f"(archetype '{archetype.value}') dated "
            f"{transformation.trade_date.isoformat()}: neither a consideration "
            f"nor a price per unit was given"
        )
    return transformation.units * transformation.price_per_unit


def execute_insert_transaction(frames: PlanFrames, transformation: Transformation) -> PlanFrames:
    """Insert a hypothetical transaction into the plan world (ADR-0104 §2).

    The transaction moves value into the investment and cash out of the
    settling currency — or the reverse, for a sale. Both legs are the same
    signed consideration ``C``, applied from the trade date onward:

    * the investment's **value path** (position currency) gains ``+C``;
    * the **cash path of the transaction's own currency** loses ``C``
      (settle-against-cash, ADR-0104 §2; the flow-currency rule of
      ADR-0103 §6).

    **AUM is therefore invariant by construction**, not by correction: value
    and cash move by the same amount, in the same currency, on the same date,
    so the sum over a currency is unchanged at every index point. A
    hypothetical trade re-*allocates* the plan world; it does not fund it. The
    regression anchor asserts this rather than establishing it.

    The archetype is resolved (and named in the error paths) so the dispatch
    seam sits where ADR-0104 §2 places it, even though the value effect is
    archetype-uniform in v1: a unitised holding and a reported one both gain
    the consideration. Nothing here branches on an ``investment_type``
    literal.

    **No ``PlanFlow`` is appended.** A hypothetical transaction is not a plan
    cashflow: it settles against cash directly, and the Planning Desk renders
    the list of hypotheticals from the parameter set — the frames carry the
    *effect*, never the parameters.

    Args:
        frames: The plan frames to transform. Not mutated.
        transformation: The :class:`~services.overlay.contract.InsertTransaction`
            to apply.

    Returns:
        New frames whose value path (for the targeted investment) and cash
        path (for the settling currency) carry the step. All other paths, and
        ``plan_flows``, are the objects the caller passed in.

    Raises:
        UnknownInvestmentError: If the investment is not in the frames.
        UnderivableConsiderationError: If the cash effect cannot be derived.
        HistoricTradeDateError: If ``trade_date <= frames.t0`` — an overlay
            never touches realised history (ADR-0104 §5).
        CurrencyMismatchError: If the transaction settles in a currency other
            than the investment's position currency — the two legs would then
            be amounts in different currencies, and the overlay never converts
            (ADR-0104 §3, N2).
        MissingCashPathError: If the frames carry no cash path for the
            settling currency.
    """
    if not isinstance(transformation, InsertTransaction):
        raise TypeError(f"execute_insert_transaction received {type(transformation).__name__}")

    investment = _resolve_investment(frames, transformation)
    archetype = resolve_archetype(investment.investment_type)
    consideration = derive_consideration(transformation, archetype)

    if transformation.trade_date <= frames.t0:
        raise HistoricTradeDateError(
            f"the hypothetical '{transformation.txn_type}' on investment "
            f"{transformation.investment_id} is dated "
            f"{transformation.trade_date.isoformat()}, at or before the "
            f"plan/actual seam {frames.t0.isoformat()}: an overlay transforms "
            f"the plan world only — realised history is identical in every "
            f"scenario (ADR-0104 §5)"
        )

    if transformation.currency != investment.currency:
        raise CurrencyMismatchError(
            f"the hypothetical '{transformation.txn_type}' settles in "
            f"{transformation.currency}, but investment "
            f"{transformation.investment_id} is held in {investment.currency}: "
            f"the overlay would have to convert between them, and it never "
            f"converts (ADR-0104 §3, N2 — conversion happens downstream, at "
            f"the ADR-0099 §4 seam)"
        )

    cash_path = frames.cash_paths.get(transformation.currency)
    if cash_path is None:
        raise MissingCashPathError(
            f"the hypothetical '{transformation.txn_type}' settles in "
            f"{transformation.currency}, but the plan world holds no cash "
            f"position in that currency: a scenario cannot invent a balance "
            f"nobody funded (ADR-0103 §6)"
        )

    value_path = frames.value_paths.get(transformation.investment_id)
    if value_path is None:
        # A plan world in which the investment exists but carries no plan NAV
        # is legal — the contributing-nothing member of the universe. Its
        # balance before the trade is zero, and the trade creates the path.
        value_path = zero_path()

    value_paths = dict(frames.value_paths)
    value_paths[transformation.investment_id] = add_step(
        value_path, transformation.trade_date, consideration
    )

    cash_paths = dict(frames.cash_paths)
    cash_paths[transformation.currency] = add_step(
        cash_path, transformation.trade_date, -consideration
    )

    return replace(frames, value_paths=value_paths, cash_paths=cash_paths)


def repaced_date(t0: date, as_of_date: date, factor: Decimal) -> date:
    """Time-scale one plan-flow date about the seam.

    ``new_date = t0 + round_half_up(factor × (as_of_date − t0).days)``, with
    the arithmetic in :class:`~decimal.Decimal` — a float product would make
    the mid-position identity (factor 1.0) a near-miss on some offsets rather
    than an exactness, and the mid-position bit-identity is a regression
    anchor (ADR-0104 §2).

    **Public API of the package** (ADR-0104 §8.4). The pacing slider's readout
    (:func:`services.investments.pacing_rows.quarter_shift`) must state exactly
    the shift this executor will perform, so it calls this rule rather than
    mirroring it — a second copy of round-half-up Decimal arithmetic would let
    the surface promise a quarter the engine never delivers. It carries a public
    name for that reason; the private cross-package import it used to be
    reached by is retired.

    Note the direction: ``× 0.5`` is *faster* — the dates move toward the seam
    — and ``× 2.0`` is *slower*. Since the caller only ever passes flows with
    ``(as_of_date − t0).days >= 1`` and the factor is bounded below by 0.5,
    the scaled offset rounds to at least 1: a re-paced flow always stays
    strictly after the seam and can never fall into realised history.

    Args:
        t0: The plan/actual seam the scaling is measured about.
        as_of_date: The flow's baseline date. Strictly after ``t0``.
        factor: The time-scaling factor, in ``[0.5, 2.0]``.

    Returns:
        The re-paced date. For :data:`~services.overlay.contract.FACTOR_NEUTRAL`
        it is ``as_of_date`` exactly, for every offset.
    """
    offset_days = Decimal((as_of_date - t0).days)
    scaled_days = (offset_days * factor).to_integral_value(rounding=ROUND_HALF_UP)
    return t0 + timedelta(days=int(scaled_days))


def execute_repace_flows(frames: PlanFrames, transformation: Transformation) -> PlanFrames:
    """Time-scale one capital-account fund's remaining plan flows (ADR-0104 §2).

    Stretches (``factor > 1``, a slower manager) or compresses
    (``factor < 1``, a faster one) the **remaining** drawdown profile: the
    investment's plan flows strictly after ``frames.t0``, minus the exempt
    types. Each flow keeps its **amount** and moves in **time** —
    ``new_date = t0 + round_half_up(factor × (as_of_date − t0).days)`` — and
    the cash path of the flow's own currency follows it: the flow is removed
    from its old date and re-applied at its new one (two steps through
    :func:`~services.overlay.steps.add_step`, the same settle-against-cash
    primitive the inserted transaction uses).

    Three things it deliberately does **not** do:

    * **It never touches an ``investor_flow``** (ADR-0103 §5, binding). The
      exempt set is imported, never restated.
    * **It never touches history.** Flows at or before the seam are not part
      of the remaining profile, and a re-paced date always lands strictly
      after it (see :func:`repaced_date`).
    * **It does not re-pace the plan NAV path** — a v1 simplification, stated
      here so it is not mistaken for an oversight. ADR-0104 §2 scopes this
      kind to the plan-*flow* profile, and the economic consequence of moved
      calls for the plan NAV path (a fund drawn down faster is also marked up
      earlier) is a modelling question the ADR does not answer. Answering it
      is ADR-successor territory, not a code decision.

    At :data:`~services.overlay.contract.FACTOR_NEUTRAL` the executor returns
    the frames it was given: a mid-position pacing **is** the manager plan,
    and the Planning Desk shows no chip for it (ADR-0104 §4).

    Args:
        frames: The plan frames to transform. Not mutated.
        transformation: The :class:`~services.overlay.contract.RepaceFlows` to
            apply. Its factor is already bounds-checked by construction.

    Returns:
        New frames whose ``plan_flows`` carry the re-paced dates — in their
        original tuple positions, so the structure stays deterministic — and
        whose cash paths carry the corresponding steps.

    Raises:
        UnknownInvestmentError: If the investment is not in the frames.
        NotRepaceableError: If the investment is not a capital-account
            archetype — only a manager-plan drawdown profile can be re-paced.
        MissingCashPathError: If a moved flow settles in a currency the plan
            world holds no cash position in.
    """
    if not isinstance(transformation, RepaceFlows):
        raise TypeError(f"execute_repace_flows received {type(transformation).__name__}")

    investment = _resolve_investment(frames, transformation)
    archetype = resolve_archetype(investment.investment_type)
    if archetype is not Archetype.CAPITAL_ACCOUNT:
        raise NotRepaceableError(
            f"investment {transformation.investment_id} resolves to archetype "
            f"'{archetype.value}': only a capital-account investment has a "
            f"manager-plan drawdown profile to re-pace (ADR-0104 §2)"
        )

    if transformation.factor == FACTOR_NEUTRAL:
        # The mid-position *is* the plan. Returning the frames themselves
        # makes the identity exact rather than approximately reconstructed.
        return frames

    plan_flows = list(frames.plan_flows)
    cash_paths = dict(frames.cash_paths)

    for position, flow in enumerate(plan_flows):
        if flow.investment_id != transformation.investment_id:
            continue
        if flow.as_of_date <= frames.t0:
            continue
        if is_overlay_exempt(flow.flow_type):
            continue

        new_date = repaced_date(frames.t0, flow.as_of_date, transformation.factor)
        if new_date == flow.as_of_date:
            continue

        cash_path = cash_paths.get(flow.currency)
        if cash_path is None:
            raise MissingCashPathError(
                f"the plan flow of investment {flow.investment_id} dated "
                f"{flow.as_of_date.isoformat()} settles in {flow.currency}, "
                f"but the plan world holds no cash position in that currency: "
                f"a re-paced flow cannot settle against a balance that does "
                f"not exist (ADR-0103 §6)"
            )

        # Lift the flow off its old date and set it down on the new one. The
        # amount is unchanged — re-pacing moves capital in time, it does not
        # resize it.
        cash_path = add_step(cash_path, flow.as_of_date, -flow.amount)
        cash_path = add_step(cash_path, new_date, flow.amount)
        cash_paths[flow.currency] = cash_path

        plan_flows[position] = replace(flow, as_of_date=new_date)

    return replace(frames, plan_flows=tuple(plan_flows), cash_paths=cash_paths)


def execute_market_shock(frames: PlanFrames, transformation: Transformation) -> PlanFrames:
    """Shock the plan value paths of one archetype (ADR-0104 §2).

    Marks every plan value point of every investment resolving to the shock's
    archetype by ``× (1 + magnitude / 100)``, strictly after the seam. A
    magnitude of ``-20`` leaves each targeted plan NAV at 80 % of its baseline;
    ``+10`` marks it up a tenth.

    **The shock moves NAV, and therefore AUM — by design.** A reader arriving
    from :func:`execute_insert_transaction`, whose AUM invariance is a
    construction property and a regression anchor, should expect the same here
    and must not: an inserted transaction *re-allocates* the plan world
    (value ``+C``, cash ``−C``, netting to nothing), whereas a market shock
    *revalues* it. Value paths move and cash paths do not, so ``Σ NAV`` moves
    with them. That is the transformation's entire content — a scenario in
    which private markets fall 20 % and the portfolio total is unchanged would
    be describing nothing at all — and a separate regression anchor asserts the
    **non**-invariance, as the counterpart to the insert-transaction one rather
    than an exception to it.

    Three properties, in the order they are easy to get wrong:

    * **Multiplicative, not additive.** ADR-0104 §2 states the magnitude "in
      %", and a per-cent magnitude on a *level* has no additive reading. Each
      point is scaled by the same ratio, so the euro amount of the shock differs
      from point to point — which is what a level shift means.
    * **Strictly after t₀.** The observation *at* the seam is the last actual
      NAV, and ADR-0104 §5 is binding that an overlay never touches actuals.
      §2's "timing v1 = immediate at t₀" fixes the *regime* — the full magnitude
      is in force from the first plan point, with no ramp, lag, or decay — not a
      licence to re-mark a realised valuation. See
      :func:`services.overlay.steps.scale_after`.
    * **Archetype dispatch, never a type literal.** The target set is resolved
      through :func:`services.investments.archetype.resolve_archetype`
      (ADR-0104 §2; the ADR-0103 §8 type-blindness rule), so a shock aimed at
      capital-account holdings reaches private equity, private debt, real
      estate, and infrastructure equity without this module knowing any of
      those names.

    **No flow is touched**, and no ``investor_flow`` could be even in
    principle: a shock acts on value paths, and flows are a different frame.
    The exemption invariant (ADR-0103 §5) therefore holds vacuously here rather
    than by enforcement — which the anchor asserts, so that a future shock kind
    that *did* reach flows could not quietly inherit the claim.

    An archetype the plan world holds nothing in, and a magnitude of zero, both
    return the frames unchanged. Neither is the silent-baseline failure mode the
    pipeline guards against: that one is *"not computed"* wearing the face of
    *"no impact"*, whereas these two **are** computed, and the impact of
    revaluing nothing — or of revaluing something by nought per cent — is
    nothing.

    Args:
        frames: The plan frames to transform. Not mutated.
        transformation: The :class:`~services.overlay.contract.MarketShock` to
            apply.

    Returns:
        New frames whose value paths carry the shock for every investment of
        the targeted archetype. Cash paths, plan flows, and the value paths of
        every other archetype are the objects the caller passed in.
    """
    if not isinstance(transformation, MarketShock):
        raise TypeError(f"execute_market_shock received {type(transformation).__name__}")

    factor = Decimal(1) + transformation.magnitude / _PER_CENT
    if factor == 1:
        # A nought-per-cent shock is the identity. Returning the frames
        # themselves makes it exact rather than approximately reconstructed —
        # the same reason `execute_repace_flows` short-circuits at FACTOR_NEUTRAL.
        return frames

    targeted = [
        investment_id
        for investment_id, investment in frames.investments.items()
        if resolve_archetype(investment.investment_type) is transformation.archetype
        and investment_id in frames.value_paths
    ]
    if not targeted:
        return frames

    value_paths = dict(frames.value_paths)
    for investment_id in targeted:
        value_paths[investment_id] = scale_after(value_paths[investment_id], frames.t0, factor)

    return replace(frames, value_paths=value_paths)


_EXECUTORS[TransformationKind.INSERT_TRANSACTION] = execute_insert_transaction
_EXECUTORS[TransformationKind.REPACE_FLOWS] = execute_repace_flows
_EXECUTORS[TransformationKind.MARKET_SHOCK] = execute_market_shock
# `fx_shock` is deliberately absent: it acts at the conversion seam (ADR-0104
# §3, N3), not on a value path, and its executor lands in S34.2. Until then
# `apply_overlay` raises ExecutorNotRegisteredError for it — loudly, because a
# scenario that quietly equals its baseline is the one failure mode the
# Planning Desk must never have.


__all__ = [
    "derive_consideration",
    "execute_insert_transaction",
    "execute_market_shock",
    "execute_repace_flows",
    "repaced_date",
]
