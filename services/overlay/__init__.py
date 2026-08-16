# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The pure overlay package — the scenario contract (ADR-0104 §2/§4).

A scenario is a **parameter set, not a dataset** (ADR-0104 §1, D15): an
ordered list of pure transformations, applied to plan frames assembled from
the book, recomputed per request, held nowhere. This package owns that
contract and nothing else:

* :mod:`services.overlay.contract` — the closed four-kind discriminator and
  the transformation dataclasses.
* :mod:`services.overlay.serialisation` — the flat request-parameter
  encoding, and its round-trip law.
* :mod:`services.overlay.pipeline` — the frame container, the executor
  registry, the fold that applies an overlay, and the router that splits the
  seam-level ``fx_shock`` out of it.
* :mod:`services.overlay.executors` — the executable kinds. Importing it
  registers them, which is why this façade imports it: ``apply_overlay`` must
  be able to execute an overlay for anyone who imports the package.
* :mod:`services.overlay.steps` — the two path primitives the executors share:
  the settle-against-cash step and the market-shock rescale.
* :mod:`services.overlay.errors` — one typed error hierarchy.

**The package is ephemeral by construction.** It performs zero repository
access, zero database writes, and holds zero scenario state (ADR-0104 §1) —
which is what makes "the book is never written from the Planning Desk" a
property of the architecture rather than a discipline of the implementer.
``tests/regression/test_overlay_layer_pure.py`` enforces it permanently.

It lives **beside** :mod:`services.analytics`, not inside it, deliberately:
the executors dispatch on
:func:`services.investments.archetype.resolve_archetype`, and the ADR-0103 §8
type-blindness guard forbids investment-type semantics within the analytics
layer. The overlay changes engine *inputs*; the engines themselves stay pure
consumers (ADR-0104 §5, D17).

Status (S34.2): **all four kinds are dataclasses, all four encode, and all four
are applied — at two seams.** Three fold here as ``frames → frames`` executors:
``insert_transaction``, ``repace_flows``, ``market_shock``. ``fx_shock`` is
applied at the **conversion seam** instead (ADR-0104 §2/§3, N3), because the
plan-world FX path it restates is not in :class:`PlanFrames` — it lives in the
:class:`~services.fx.functional_currency.PortfolioFxConverter`, assembled from
rows this package is forbidden to read. :func:`partition_fx_shocks` routes the
two halves, and :func:`services.fx.plan_shock.shock_plan_fx_path` is the FX
half's executor in all but name. :func:`apply_overlay` still raises
:class:`~services.overlay.errors.ExecutorNotRegisteredError` for an ``fx_shock``
handed to it — the error now means *mis-routed*, not *unimplemented*.
"""

from services.overlay.contract import (
    EMPTY_OVERLAY,
    EXECUTABLE_KINDS,
    FACTOR_MAX,
    FACTOR_MIN,
    FACTOR_NEUTRAL,
    FxShock,
    InsertTransaction,
    MarketShock,
    Overlay,
    RepaceFlows,
    Transformation,
    TransformationKind,
)
from services.overlay.errors import (
    CurrencyMismatchError,
    ExecutorNotRegisteredError,
    FactorOutOfBoundsError,
    HistoricTradeDateError,
    IndexSequenceError,
    KindNotImplementedError,
    MalformedFieldError,
    MissingCashPathError,
    MissingFieldError,
    NotRepaceableError,
    OverlayError,
    OverlayExecutionError,
    OverlayParseError,
    UnderivableConsiderationError,
    UnknownInvestmentError,
    UnknownKindError,
)

# Imported for its side effect as much as for its names: importing the
# executors module registers the executable kinds in the pipeline's executor
# table (ADR-0104 §2). Without this line, `apply_overlay` would raise
# ExecutorNotRegisteredError for every kind — the S2.1a state.
#
# `derive_consideration` and `repaced_date` are public API (ADR-0104 §8.4), not
# incidental exports: the Planning Desk's hypothetical table and the pacing
# slider's readout must state exactly what the executors will do, and they call
# these rules rather than mirror them. They were reached by private
# cross-package import until S34.1; the underscore is gone and so is the noqa.
from services.overlay.executors import (
    derive_consideration,
    execute_insert_transaction,
    execute_market_shock,
    execute_repace_flows,
    repaced_date,
)
from services.overlay.pipeline import (
    Executor,
    PlanFlow,
    PlanFrames,
    PlanInvestment,
    apply_overlay,
    partition_fx_shocks,
)
from services.overlay.serialisation import parse_overlay, serialise_overlay
from services.overlay.steps import add_step, scale_after, zero_path

__all__ = [
    "EMPTY_OVERLAY",
    "EXECUTABLE_KINDS",
    "FACTOR_MAX",
    "FACTOR_MIN",
    "FACTOR_NEUTRAL",
    "CurrencyMismatchError",
    "Executor",
    "ExecutorNotRegisteredError",
    "FactorOutOfBoundsError",
    "FxShock",
    "HistoricTradeDateError",
    "IndexSequenceError",
    "InsertTransaction",
    "KindNotImplementedError",
    "MalformedFieldError",
    "MarketShock",
    "MissingCashPathError",
    "MissingFieldError",
    "NotRepaceableError",
    "Overlay",
    "OverlayError",
    "OverlayExecutionError",
    "OverlayParseError",
    "PlanFlow",
    "PlanFrames",
    "PlanInvestment",
    "RepaceFlows",
    "Transformation",
    "TransformationKind",
    "UnderivableConsiderationError",
    "UnknownInvestmentError",
    "UnknownKindError",
    "add_step",
    "apply_overlay",
    "derive_consideration",
    "execute_insert_transaction",
    "execute_market_shock",
    "execute_repace_flows",
    "parse_overlay",
    "partition_fx_shocks",
    "repaced_date",
    "scale_after",
    "serialise_overlay",
    "zero_path",
]
