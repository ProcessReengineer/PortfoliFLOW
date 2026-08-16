# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Typed errors of the overlay package (ADR-0104 §2/§4).

One hierarchy, rooted at :class:`OverlayError`, so a caller — the S2.3
Planning Desk route — can catch a single class and render one actionable
message. The root derives from :class:`core.exceptions.PortfoliFlowError`
per the project exception policy; ``core.exceptions`` is stdlib-only, so
importing it keeps the package inside its purity contract
(``tests/regression/test_overlay_layer_pure.py``).

The hierarchy::

    OverlayError
    ├── OverlayParseError          — the parameter set is not a valid overlay
    │   ├── UnknownKindError
    │   ├── KindNotImplementedError
    │   ├── MissingFieldError
    │   ├── MalformedFieldError
    │   └── IndexSequenceError
    ├── OverlayExecutionError      — a valid transformation the frames refuse
    │   ├── UnknownInvestmentError
    │   ├── MissingCashPathError
    │   ├── CurrencyMismatchError
    │   ├── HistoricTradeDateError
    │   ├── UnderivableConsiderationError
    │   └── NotRepaceableError
    ├── FactorOutOfBoundsError     — a factor outside the ADR-0104 §2 bounds
    └── ExecutorNotRegisteredError — no executor for a kind (S2.1b fills these)

An :class:`OverlayExecutionError` is a *well-formed* transformation the plan
world cannot carry — an investment that is not in the frames, a settlement
currency with no cash path, a trade date inside realised history. It is
therefore an ordinary, actionable outcome of the Planning Desk rather than a
bug: the S2.3 route renders it beside the scenario chips, and every message
names the offending id, currency, or date so the operator can act on it
without reading a log.

:class:`FactorOutOfBoundsError` sits **beside**
:class:`OverlayParseError` rather than under it because it is raised at
*construction* of a :class:`~services.overlay.contract.RepaceFlows` — a
Python call, not a parse. :func:`~services.overlay.serialisation.parse_overlay`
constructs the transformation and lets the error propagate (re-raised with
the offending index and field named), so an out-of-bounds factor in a
request is still one typed error from one hierarchy.
"""

from __future__ import annotations

from core.exceptions import PortfoliFlowError


class OverlayError(PortfoliFlowError):
    """Base class for every error raised inside ``services/overlay/``."""


class OverlayParseError(OverlayError):
    """Raised when a flat parameter set does not denote a valid overlay.

    Every subclass names the offending transformation index and, where one
    applies, the offending field — the request that produced it is
    reproducible from the message alone (ADR-0104 §4: a scenario is
    reproducible from *(book, URL)*).
    """


class UnknownKindError(OverlayParseError):
    """Raised when ``t{n}_kind`` is not one of the four ADR-0104 §2 kinds."""


class KindNotImplementedError(OverlayParseError):
    """Raised for a kind the contract knows but this strand cannot execute.

    The two shock kinds — ``market_shock`` and ``fx_shock`` — are part of
    the closed discriminator and are therefore *recognised*, but their
    executors ship with roadmap #034. Rejecting them by name (rather than
    as an unknown kind) keeps the contract's closure visible: the encoding
    defines four kinds today and executes two.
    """


class MissingFieldError(OverlayParseError):
    """Raised when a required field of a transformation is absent."""


class MalformedFieldError(OverlayParseError):
    """Raised when a field is present but not parseable as its type.

    Covers an unparseable UUID, date, or Decimal; a duplicate parameter
    key; and any key inside the ``t{n}_`` namespace that the kind's field
    table does not define.
    """


class IndexSequenceError(OverlayParseError):
    """Raised when transformation indices are not contiguous from zero.

    Application order *is* list order (ADR-0104 §2), so a gap in the index
    sequence has no defined meaning — it is a malformed request, never a
    silently compacted one.
    """


class OverlayExecutionError(OverlayError):
    """Raised when an executor cannot apply a well-formed transformation.

    The parse succeeded and the transformation is legal in itself; the
    *frames* cannot carry it. Every subclass names the offending investment,
    currency, or date — these messages surface on the Planning Desk as
    actionable errors, not as diagnostics (ADR-0104 §4).
    """


class UnknownInvestmentError(OverlayExecutionError):
    """Raised when a transformation targets an investment not in the frames.

    The overlay transforms the plan world it was handed; an investment absent
    from it has no value path to step and no archetype to dispatch on. A
    stale URL — a scenario shared after the investment was archived — is the
    ordinary way to produce one (ADR-0104 §4: a scenario is reproducible from
    *(book, URL)*, and a book that has moved on must say so).
    """


class MissingCashPathError(OverlayExecutionError):
    """Raised when the settling currency has no cash path in the frames.

    Settlement is by **flow currency** (ADR-0103 §6, Strand-1 closure §2.5):
    a transformation settles against the cash path of its own currency. Where
    the plan world holds no cash position in that currency, the executor
    raises rather than conjuring one — the ``MissingFxRateError`` philosophy
    of ADR-0099, applied to cash: a balance nobody funded must never be
    invented silently. The S2.6 entry surface constrains the choice to the
    currencies the frames actually carry, so this is a guard, not a workflow.
    """


class CurrencyMismatchError(OverlayExecutionError):
    """Raised when a transaction settles in a currency the investment is not in.

    An ``insert_transaction`` adds its consideration to the investment's value
    path (position currency) and subtracts it from the cash path of the
    transaction's currency. Where the two differ, the two legs are amounts in
    *different* currencies, and squaring them would require an FX conversion —
    which the overlay must never perform (ADR-0104 §3, N2: the overlay changes
    engine inputs in position currency; conversion happens at the ordinary
    ADR-0099 §4 seam, downstream). A cross-currency settlement is therefore
    undefined in v1 and rejected loudly, rather than executed as a silent 1:1
    conversion — the one outcome ADR-0099 forbids above all others.
    """


class HistoricTradeDateError(OverlayExecutionError):
    """Raised when a hypothetical trade is dated at or before the seam.

    ADR-0104 §5 makes identical history an invariant: left of ``t0`` the
    baseline and the scenario are the same path *by definition*. An overlay
    that could insert a transaction into realised history would rewrite what
    already happened — the book's own account of itself — and every figure
    the operator reconciles against would come loose.
    """


class UnderivableConsiderationError(OverlayExecutionError):
    """Raised when a transaction's cash effect cannot be determined.

    The consideration is either stated outright or derived as
    ``units × price_per_unit``. With neither available there is no cash
    effect to settle and no value step to take — the transformation says
    nothing, and an executor that guessed a zero would report "no impact"
    where the truth is "not stated".
    """


class NotRepaceableError(OverlayExecutionError):
    """Raised when re-pacing an investment that has no drawdown profile.

    ``repace_flows`` time-scales a **manager-plan drawdown profile**, which
    only a capital-account investment has (ADR-0104 §2; the archetype is
    resolved through :func:`services.investments.archetype.resolve_archetype`,
    never inferred from an ``investment_type`` literal). A listed holding's
    plan is a value path, not a call schedule: stretching it in time would
    mean something else entirely, and that something else is a
    ``market_shock`` (roadmap #034), not this kind.
    """


class FactorOutOfBoundsError(OverlayError):
    """Raised when a re-pacing factor falls outside the ADR-0104 §2 bounds.

    The bounds — ``[0.5, 2.0]`` inclusive — are part of the transformation
    contract, not a UI convenience: they bound how far a manager-plan
    drawdown profile may be stretched or compressed. A value outside them
    is rejected at construction, so an invalid :class:`RepaceFlows` cannot
    exist in memory.
    """


class ExecutorNotRegisteredError(OverlayError):
    """Raised when applying a transformation kind that has no executor.

    Since S2.1b the registry holds the two executable kinds; the two shock
    kinds ship with roadmap #034 and remain unregistered. Applying an overlay
    that names one therefore fails loudly rather than silently returning the
    baseline — a scenario that quietly equals its baseline is the one failure
    mode the Planning Desk must never have.
    """


__all__ = [
    "CurrencyMismatchError",
    "ExecutorNotRegisteredError",
    "FactorOutOfBoundsError",
    "HistoricTradeDateError",
    "IndexSequenceError",
    "KindNotImplementedError",
    "MalformedFieldError",
    "MissingCashPathError",
    "MissingFieldError",
    "NotRepaceableError",
    "OverlayError",
    "OverlayExecutionError",
    "OverlayParseError",
    "UnderivableConsiderationError",
    "UnknownInvestmentError",
    "UnknownKindError",
]
