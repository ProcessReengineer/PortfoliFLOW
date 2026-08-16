# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The overlay application pipeline (ADR-0104 §2/§4).

Folds an ordered overlay over the baseline plan frames, dispatching each
transformation to its executor. The pipeline is the whole of the overlay's
interaction with the rest of the system: engines downstream consume the
frames it returns **unchanged** (ADR-0104 §5, D17 — no engine forks, no
scenario branch inside :mod:`services.analytics`).

Two properties carry the architecture:

* **The empty overlay is the identity.** ``apply_overlay(frames, ())``
  returns the very frames it was given. That is the Baseline side of the
  Planning Desk's Baseline/Scenario toggle (ADR-0104 §4): baseline and
  scenario render through one code path, so the baseline cannot drift from
  the world the scenario is measured against.
* **An unexecutable overlay fails loudly.** A kind with no executor raises
  :class:`~services.overlay.errors.ExecutorNotRegisteredError` rather than
  passing the frames through. A scenario that silently equals its baseline
  is the one failure mode the Planning Desk must never have: the operator
  would read "no impact" where the truth is "not computed".

**The registry is filled by :mod:`services.overlay.executors`**, which
registers the executable kinds at import time; the package façade imports it,
so ``apply_overlay`` executes them for anyone importing
:mod:`services.overlay`. That is three of the four kinds, and it is three
permanently: ``fx_shock`` has no fold executor and never will. It acts at the
**conversion seam** (ADR-0104 §2/§3, N3) — on a rate path that is not in
:class:`PlanFrames` and cannot be, since this package may not read an FX row —
so :func:`partition_fx_shocks` routes it to
:func:`services.fx.plan_shock.shock_plan_fx_path` before the fold runs, and
``apply_overlay`` goes on raising
:class:`~services.overlay.errors.ExecutorNotRegisteredError` for one handed to
it directly. That is not an unfinished registration; it is the seam saying an
``fx_shock`` was routed to the wrong one.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from uuid import UUID

import pandas as pd

from services.overlay.contract import (
    FxShock,
    Overlay,
    Transformation,
    TransformationKind,
)
from services.overlay.errors import ExecutorNotRegisteredError


@dataclass(frozen=True)
class PlanInvestment:
    """Static per-investment metadata the executors dispatch on.

    Attributes:
        investment_id: The investment's identifier — the key of
            :attr:`PlanFrames.value_paths` and
            :attr:`PlanFrames.investments`.
        currency: The position currency the investment's value path is
            denominated in. Plan values and cash paths are assembled in
            position currency and converted at the ordinary ADR-0099 §4 seam
            (ADR-0104 §3, N2) — the overlay never converts.
        investment_type: The canonical ``investments.investment_type``
            discriminator. Executors resolve it through
            :func:`services.investments.archetype.resolve_archetype` and
            dispatch on the resulting archetype (ADR-0104 §2); they never
            compare it against a type literal. Carrying the raw value here
            keeps the single resolution seam inside the executor rather than
            spreading a second routing concept through the frames.
    """

    investment_id: UUID
    currency: str
    investment_type: str


@dataclass(frozen=True)
class PlanFlow:
    """One plan flow of the baseline plan world.

    Carries :attr:`flow_type` because the executors need it: the exemption
    invariant (ADR-0103 §5) forbids any transformation from creating,
    deleting, re-pacing, or re-scaling an ``investor_flow``, and an executor
    enforces it by importing
    :data:`services.investments.flow_type_invariants.OVERLAY_EXEMPT_FLOW_TYPES`
    — never by restating the set.

    Attributes:
        investment_id: The investment the flow belongs to.
        as_of_date: The flow's event date (ADR-0043 §1).
        amount: The signed flow amount in :attr:`currency`. Negative debits
            the settling cash path (a plan capital call), positive credits it
            (a plan distribution).
        currency: The flow's own currency — which selects the cash path it
            settles against, independently of the owning investment's
            currency (ADR-0103 §6).
        flow_type: The canonical ``investment_cashflows.flow_type`` value.
    """

    investment_id: UUID
    as_of_date: date
    amount: Decimal
    currency: str
    flow_type: str


@dataclass(frozen=True, eq=False)
class PlanFrames:
    """The baseline plan frames an overlay transforms (ADR-0104 §1/§2).

    The container the pipeline passes through: assembled from the book
    (Layer 1) and transformed in memory (Layer 2), held nowhere. It is a
    plain dataclass over pandas and stdlib types — it imports no repository
    and no ORM model, which is what lets the whole package stay inside its
    purity contract.

    **S2.2 owns filling it.** The baseline assembly seam (plan NAV series,
    plan flows, the ADR-0103 §6 cash plan path, ADR-0060 carry-forward where
    a plan stream is missing) is the next strand's concern; this strand fixes
    only the shape the executors receive and return.

    Equality is by identity (``eq=False``): the frames hold pandas objects,
    whose ``==`` is elementwise, so a generated ``__eq__`` would return an
    array rather than a bool. The pipeline's identity law is asserted with
    ``is``, and the ADR-0104 §5 regression anchors (identical history,
    mid-position bit-identity) compare the *paths* with pandas' own
    assertions — never the container.

    Attributes:
        t0: The plan/actual seam (ADR-0060) — the anchor every transformation
            measures "remaining" and "immediate" against. Left of it, the
            baseline and the scenario are the same path by definition
            (ADR-0104 §5, the identical-history invariant); overlays only
            ever touch the future.
        value_paths: Per investment, its plan value path in **position
            currency**, indexed by date. The unit of a ``market_shock`` and
            the target of an ``insert_transaction``'s value effect.
        cash_paths: Per currency code, the plan cash-balance path in that
            currency (ADR-0103 §6), indexed by date. The settling side of an
            ``insert_transaction`` (settle-against-cash, ADR-0104 §2).
        plan_flows: The plan flows of the baseline world, in no guaranteed
            order. The unit a ``repace_flows`` re-paces — minus the exempt
            flow types, which no transformation may touch.
        investments: Per investment, the static metadata the executors
            dispatch on. Keyed identically to :attr:`value_paths`.
        profile_source: Per investment, where its **remaining flow profile**
            came from, for the investments whose profile is not the book's own.
            An investment absent from the mapping carries the manager's plan as
            the book states it — which is every investment of a book with no
            generated profile, so the mapping is **empty by default** and the
            frames of such a book are the frames they always were (ADR-0105 §6,
            the non-interference invariant).

            The one value in v1 is ``'ta'``: an ephemeral Takahashi–Alexander
            profile, generated at the assembly seam for a capital-account fund
            with no manager plan and held nowhere else (ADR-0105 §4). It is a
            *label on the frames*, never on the flow — a generated
            :class:`PlanFlow` is an ordinary plan flow, and the executors are
            indifferent to its provenance: ``repace_flows`` time-scales whatever
            remaining profile the frames carry (ADR-0105 §5).

            **Set once, at the seam, and only read afterwards.** Every consumer
            badges from this mapping rather than re-deriving the un-paceable
            predicate over the augmented frames — which would not work anyway,
            since a generated fund *has* remaining flows and so is no longer
            un-paceable by the time it is marked.
    """

    t0: date
    value_paths: Mapping[UUID, pd.Series]
    cash_paths: Mapping[str, pd.Series]
    plan_flows: tuple[PlanFlow, ...]
    investments: Mapping[UUID, PlanInvestment]
    profile_source: Mapping[UUID, str] = field(default_factory=dict)


#: An executor: a **pure** function ``frames → frames``.
#:
#: Every executor takes the frames and one transformation and returns the
#: transformed frames. Purity is the contract (ADR-0104 §2): no repository,
#: no session, no clock, no randomness, no mutation of the frames it was
#: handed — a scenario must be reproducible from *(book, parameters)* alone,
#: and a hidden input would break that.
Executor = Callable[[PlanFrames, Transformation], PlanFrames]


#: The closed executor table: kind → executor.
#:
#: :mod:`services.overlay.executors` fills the ``insert_transaction``,
#: ``repace_flows``, and ``market_shock`` rows at import time. **The fourth row
#: does not exist and will not be added.** ``fx_shock`` is not executed by this
#: fold — it is applied at the conversion seam
#: (:func:`services.fx.plan_shock.shock_plan_fx_path`), on a rate path that is
#: not in :class:`PlanFrames`; :func:`partition_fx_shocks` routes it there. A
#: reader looking for the fourth entry is looking in the right table for the
#: wrong kind.
#:
#: It is a plain module-level mapping, not a registration API: the kind
#: discriminator is closed (ADR-0104 §2), so an extension hook would invite
#: exactly the fifth-kind-by-plugin the ADR rules out. The table lives here
#: rather than in ``executors`` so the fold and its dispatch table stay in one
#: module and ``pipeline`` needs no back-import of its executors.
#:
#: It is the **executor** registry, not the encoding: an ``fx_shock`` parses
#: and constructs, and is refused here. The two tables part company on purpose
#: — a shared scenario link stays readable, and a shock that reaches the fold
#: has been mis-routed rather than merely unimplemented.
#:
#: Every executor registered here must honour the **exemption invariant**
#: (ADR-0103 §5, binding): no transformation of any kind creates, deletes,
#: re-paces, or re-scales an ``investor_flow``. Executors enforce it by
#: importing ``OVERLAY_EXEMPT_FLOW_TYPES`` — or its predicate
#: ``is_overlay_exempt`` — from
#: :mod:`services.investments.flow_type_invariants`: the single
#: formulation, never restated locally. A second copy of the set drifts
#: from the first the moment the set changes, and an invariant that can be
#: locally overridden is not one.
_EXECUTORS: dict[TransformationKind, Executor] = {}


def apply_overlay(frames: PlanFrames, overlay: Overlay) -> PlanFrames:
    """Apply an ordered overlay to the baseline plan frames.

    Folds the transformations over the frames in **list order** (ADR-0104
    §2): each executor receives the output of the previous one, so a
    re-pacing followed by an inserted transaction sees the re-paced world,
    and the reverse order is a different — equally legal — scenario.

    Args:
        frames: The baseline plan frames, assembled from the book (S2.2).
        overlay: The ordered transformations. Empty means baseline.

    Returns:
        The transformed frames. For the empty overlay, the ``frames``
        argument itself — the identity, unchanged and not copied (ADR-0104
        §4: the Baseline toggle renders the same regions with an empty
        transformation list).

    Raises:
        ExecutorNotRegisteredError: If a transformation's kind has no
            registered executor — ``fx_shock``, which is not a value
            transformation and is applied at the conversion seam instead
            (:func:`partition_fx_shocks`). Callers split it out first; one that
            reaches the fold has been mis-routed, and an overlay that cannot be
            executed must say so rather than quietly return the baseline.
        OverlayExecutionError: If an executor cannot apply a well-formed
            transformation to these frames (an unknown investment, a missing
            cash path, a trade date inside realised history). The Planning
            Desk renders it as an actionable error (ADR-0104 §4).
    """
    for transformation in overlay:
        kind = transformation.kind
        executor = _EXECUTORS.get(kind)
        if executor is None:
            raise ExecutorNotRegisteredError(
                f"no executor registered for transformation kind '{kind.value}'"
            )
        frames = executor(frames, transformation)
    return frames


def partition_fx_shocks(overlay: Overlay) -> tuple[Overlay, tuple[FxShock, ...]]:
    """Split an overlay into the value fold and the seam's FX shocks.

    The two halves of an overlay go to two different seams (ADR-0104 §2). Three
    kinds are value transformations: they fold through :func:`apply_overlay`,
    which is what ``frames → frames`` means. The fourth — ``fx_shock`` — acts on
    "the **conversion seam** — the plan-world FX path for that currency", with
    an executor dispatch of "none". The ADR's Rationale states the division
    plainly: *"Three transformations act on values and dispatch on archetype;
    the FX shock acts on the seam and is archetype-blind."*

    So this function is the router, and the reason it can exist at all is that
    the FX path is **not in the frames**. It lives in the converter, assembled
    from rows this package may not read (``test_overlay_layer_pure.py``). A
    value executor for ``fx_shock`` would have nothing to act on; the seam
    applies it instead, through
    :func:`services.fx.plan_shock.shock_plan_fx_path`, and
    :func:`apply_overlay` continues to refuse one handed to it directly.

    **The split is order-preserving on the result**, which is what makes it
    legitimate under ADR-0104 §2's "application order is list order": the value
    executors touch only frames and a shock touches only rates, so the two
    commute. An ``fx_shock`` sitting between two value transformations changes
    neither, and the value transformations keep their relative order here.

    Args:
        overlay: The parsed parameter set, in list order.

    Returns:
        ``(value_overlay, fx_shocks)`` — the transformations
        :func:`apply_overlay` executes, in their original relative order, and
        the FX shocks the seam applies, in theirs.
    """
    value_overlay = tuple(
        transformation for transformation in overlay if not isinstance(transformation, FxShock)
    )
    fx_shocks = tuple(
        transformation for transformation in overlay if isinstance(transformation, FxShock)
    )
    return value_overlay, fx_shocks


__all__ = [
    "Executor",
    "PlanFlow",
    "PlanFrames",
    "PlanInvestment",
    "apply_overlay",
    "partition_fx_shocks",
]
