# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Custom exception hierarchy for PortfoliFLOW.

All application exceptions derive from ``PortfoliFlowError`` so callers can
catch the base class when they don't need to distinguish subtypes.
"""

from datetime import date


class PortfoliFlowError(Exception):
    """Base exception for all PortfoliFLOW errors.

    Args:
        message: Human-readable description of the error.
    """

    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.message = message

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.message!r})"


class ConfigurationError(PortfoliFlowError):
    """Raised when application configuration is missing or invalid."""


class DataImportError(PortfoliFlowError):
    """Raised when importing or parsing external data fails."""


class ValidationError(PortfoliFlowError):
    """Raised when module input validation fails.

    Args:
        message: Description of the validation failure.
        field: Optional name of the offending field or parameter.
    """

    def __init__(self, message: str = "", field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


class CurrencyMismatchError(ValidationError):
    """Raised when a position transaction or price currency is inconsistent.

    Per ADR-0097 §5 the currency of a ``position_transactions`` or
    ``instrument_prices`` row **must equal** ``investments.currency``. The
    write path fails loudly rather than converting — a silent FX conversion
    point inside the write path is an audit hazard. Cross-currency
    instruments are a named successor concern.
    """


class NonNegativeHoldingsError(ValidationError):
    """Raised when a position transaction would drive holdings below zero.

    Per ADR-0097 §4 no transaction may take derived holdings below zero on
    any date. The service layer recomputes holdings forward over the ledger
    (existing rows plus the candidate) via
    :func:`services.investments.holdings.first_negative_holding_date` and
    rejects the write with this typed error. Short positions are out of
    scope; a successor ADR owns them if ever needed.
    """


class ValuationModeError(ValidationError):
    """Raised when a valuation-mode flip violates its preconditions.

    Per ADR-0097 §6 the flip to ``'unitised'`` is an explicit, **one-way**
    operator act gated on two preconditions: the investment's type is
    ``listed_equity``/``listed_bonds``, and an ``opening`` transaction
    exists. Re-flipping an already-unitised investment is refused by the
    same rule — corrections go through ledger edits, never a mode flap.

    Distinct from the sibling :class:`ValidationError` subclasses in that it
    reports a **conflict with the investment's current state** rather than a
    malformed field, and the web layer therefore renders it as ``409``.
    """


class InvestorFlowScopeError(ValidationError):
    """Raised when an ``investor_flow`` is booked on a non-cash investment.

    Per ADR-0103 §5 an investor flow — a net contribution to, or withdrawal
    from, the mandate — is booked on the **cash position of the currency it
    settles in** (decision N4). Booking one on a fund, a bond, or any other
    non-cash investment would place a mandate-level flow inside an
    instrument's performance history, where it does not belong.

    The rule spans two tables (``investment_cashflows.flow_type`` and
    ``investments.investment_type``), which a DB CHECK cannot see across, so
    the service seam owns it and every caller inherits it. The web layer
    renders it as ``400`` with ``field='flow_type'``: it is a malformed
    field-and-target combination, not a conflict with the investment's
    current state (contrast :class:`ValuationModeError`, which is ``409``).
    """


class ModuleError(PortfoliFlowError):
    """Raised when a module encounters an unexpected runtime error."""


class ServiceError(PortfoliFlowError):
    """Raised when an external service (AI, storage, etc.) fails."""


class AnlVCodeUnknown(DataImportError):
    """Raised when an Excel ``AnlV`` cell does not resolve against the catalogue.

    Per ADR-0057 §Excel import the normaliser accepts ``"Nr. 13"``,
    ``"Nr.13"``, ``"13"``, and ``"anlv_13"``. Anything else (including
    out-of-range numbers) raises this exception. The extractor
    collects row-level errors rather than aborting, consistent with
    the partial-success import convention.
    """


class LimitValidationError(DataImportError):
    """Raised when a limit-set import payload fails validation.

    Covers the sum-to-100 rule, unknown class keys, duplicate class
    keys, and re-import of an already-persisted limit set (ADR-0056
    immutability). The importer surfaces this with a message that
    explains the violation and, where relevant, the immutability
    invariant.
    """


class LimitSetNotEffective(PortfoliFlowError):
    """Raised when no limit set is in force at the evaluation date.

    Engine-level error per ADR-0056 §Selection. Block A defines the
    symbol so downstream code can import it; Block B (the coverage
    engine) raises it.
    """


class CoverageInputMissing(PortfoliFlowError):
    """Raised when the coverage engine has no denominator to divide by.

    Since ADR-0103 §2 the denominator is the book itself —
    ``aum(t) = Σ nav_functional(t)``, cash rows included — so the condition
    is no longer "no AUM row on or before ``t``" (the ADR-0055 formulation,
    retired with the ``portfolio_aum`` series) but "the book carries no value
    at ``t``": an empty universe, or one whose every position resolves to
    zero. Coverage of a portfolio worth nothing is not zero, it is undefined,
    and the engine says so rather than dividing.
    """


class IreneResolutionInvalid(ValidationError):
    """Raised when an Irene finding is resolved with a value outside the vocabulary.

    Per ADR-0085 / ADR-0088 a finding's ``resolution`` must be one of
    ``open`` / ``acted`` / ``dismissed`` / ``acknowledged`` (lowercase).
    The repository validates the value in application code — the column
    is a plain ``TEXT`` (no SQL enum), matching the codebase's
    TEXT-for-status convention — and raises this typed error on a bad
    value rather than persisting it.
    """


class IreneCadenceInvalid(ValidationError):
    """Raised when an Irene schedule carries a cadence the tick cannot honour.

    Per ADR-0086 the due-evaluation layer maps a schedule's ``cadence``
    to a concrete ``next_due_at`` via
    :func:`services.irene.scheduling.compute_next_due_at`, which owns the
    supported vocabulary (ADR-0119 §1). A value outside it (a future
    cadence not yet implemented, or a corrupt row) raises this typed
    error rather than silently mis-scheduling. Mirrors
    :class:`IreneResolutionInvalid`
    (same base, same TEXT-for-status style — the column is a plain
    ``TEXT``, validated in application code).
    """


class WatchpointInvalid(ValidationError):
    """Raised when a watchpoint write violates a value or shape rule.

    Per ADR-0116 §1/§3 the ``watchpoints`` schema owns the **asymmetry**
    (per-family CHECKs deciding which columns may be non-NULL) while the
    repository owns the **values**: ``50 < warn_threshold_pct < 100``,
    positive deltas / windows / thresholds, a well-formed ``BASE/QUOTE``
    currency pair, and the singleton rule for the ``freshness`` and
    ``liquidity`` families. The repository raises this typed error before
    any SQL runs, so a caller sees a named field rather than an opaque
    ``IntegrityError`` from the CHECK underneath.
    """


class WatchpointNotFound(PortfoliFlowError):
    """Raised when a watchpoint identity has no version to revise or retire.

    Watchpoints are historised (ADR-0116 §1): ``revise`` and ``retire``
    write a *new version* of an existing identity, which means the current
    version must be readable first. A missing identity — never created, or
    belonging to another tenant and therefore invisible under RLS — is this
    error, not a silent no-op.
    """


class FloorCalibrationInvalid(ValidationError):
    """Raised when a floor-calibration revision would be an invalid config.

    Per ADR-0116 §5/§7 the calibration write path composes the candidate
    revision over ``DEFAULT_FLOOR_CONFIG`` and runs the full ``FloorConfig``
    constructor validation **plus** the pinned invariants
    (:func:`services.analytics.irene_floor.validate_pinned_invariants`)
    before persisting. An inverted clamp, an uncovered band scale, or a
    ``limit_breach`` floor that a boundary edit pushed out of the critical
    band is rejected at write time — the beat must never be the first to
    discover it.
    """


class CoverageInputOutOfRange(PortfoliFlowError):
    """Raised when a coverage-engine input lies outside its supported range.

    Raised by the limit-coverage engine when an evaluation date has no
    NAV observation at or before ``t`` in
      **either** stream of an investment (ADR-0060). The engine
      prefers the cut-over-selected stream (actual for
      ``t <= cut_over``, plan otherwise), carries forward the latest
      entry at or before ``t``, and falls back to the other stream
      under the same carry-forward rule; only when both streams have
      no historical entry is this error raised. Liquidations are
      expressed by an explicit ``nav_value == 0`` entry; the engine
      never extrapolates to zero.

    The AUM-forecast-end condition this error also used to carry retired
    with the ``portfolio_aum`` series (ADR-0103 §2/§7): there is no separate
    AUM horizon to run past any more, because the denominator is the book's
    own NAVs — the assembly seam clamps the evaluation range to them.

    Distinct from :class:`CoverageInputMissing`, which signals that the book
    carries no *value* at the evaluation date, and so offers no denominator
    to divide by.
    """


class MissingFxRateError(PortfoliFlowError):
    """Raised when a required FX rate cannot be resolved.

    Engine-level error per ADR-0099 §3, and a sibling of
    :class:`CoverageInputMissing` rather than a :class:`ValidationError`:
    it reports a **missing computation input** at conversion time, not a
    malformed field on the way in. The conversion service raises it when
    no rate for ``currency`` exists at or before ``as_of_date`` — either
    the currency is entirely uncovered, or the requested date precedes the
    first stored rate (carry-forward has no anchor).

    **There is no silent 1:1 fallback, anywhere.** That is the whole point:
    a multi-currency tenant with missing rates must fail loudly rather than
    quietly add USD nominally into EUR. Single-currency tenants never reach
    this path — the identity short-circuit (``from == to``) returns before
    any rate lookup, so a pure-EUR portfolio needs zero FX rows.

    Args:
        currency: The currency whose rate could not be resolved.
        as_of_date: The date the rate was requested for.
        leg: Which side of a triangulated conversion failed — ``'from'``
            or ``'to'`` — or ``None`` for a direct
            :meth:`~services.fx.conversion.FxConverter.rate` lookup.
    """

    def __init__(
        self,
        currency: str,
        as_of_date: date,
        leg: str | None = None,
    ) -> None:
        leg_clause = f" ({leg} leg of the conversion)" if leg else ""
        super().__init__(
            f"No FX rate for {currency} at or before "
            f"{as_of_date.isoformat()}{leg_clause}. Rates are never "
            f"defaulted to 1:1 (ADR-0099 §3) — supply the missing rate."
        )
        self.currency = currency
        self.as_of_date = as_of_date
        self.leg = leg


class PlanHorizonInvalidError(ValidationError):
    """Raised when a plan horizon is not one of the offered horizons.

    Per ADR-0104 §6 the Planning Desk offers a horizon of 4, 8 (default) or
    12 quarters. The set is closed and the timeline assembly
    (:func:`services.investments.cash_flow_timeline.build_cash_flow_timeline`)
    validates against it rather than clamping to it: a horizon the operator
    did not choose is a wrong answer, not a near one, and a silently clamped
    12Q request would state a four-quarter funding gap as an eight-quarter
    one.

    A :class:`ValidationError` rather than a :class:`PlanWorldError` — the
    book is fine; the *parameter* is malformed on the way in. Mirrors
    :class:`IreneCadenceInvalid` (same base, same closed-vocabulary shape).
    """


class PlanWorldError(PortfoliFlowError):
    """Raised when the book cannot be assembled into a plan world.

    The root of the baseline-assembly conditions of ADR-0104 §1: the seam
    that reads Layer 1 (the persisted book) and returns the ``PlanFrames``
    the overlay transforms
    (:func:`services.investments.plan_world.assemble_plan_frames`). One
    class to catch, so the S2.3 Planning Desk route renders a single
    actionable message rather than branching on causes.

    These are *book-shape* conditions, not bugs: a tenant whose book carries
    no statement has no plan world to show, and the honest answer is to say
    so. Sibling of :class:`CoverageInputMissing` in spirit — the computation
    has no input, so it declines to invent one.
    """


class PlanSeamMissingError(PlanWorldError):
    """Raised when the book offers no plan/actual seam to project from.

    ``t₀`` is derived from the book — the latest ``nav_kind='actual'`` NAV
    date over the active universe, generalising the ADR-0103 §6 rule that
    *the anchor is the last actual statement* (ADR-0104 §1). Two book shapes
    leave that undefined: a tenant with no active investment at all, and one
    whose active investments carry no actual NAV row.

    The assembly refuses rather than substituting a clock: a seam taken from
    ``date.today()`` would make the plan world depend on when it was read,
    and the ADR-0104 §2 purity contract (a scenario is reproducible from
    *(book, parameters)* alone) would no longer hold end to end.
    """


class DuplicateCashPositionError(PlanWorldError):
    """Raised when one currency has two active cash positions.

    The plan frames carry **one cash path per currency** (ADR-0104 §1) —
    the settling side every hypothetical transaction and every re-paced flow
    resolves against. Two active cash positions in one currency leave "the
    EUR cash path" undefined, and every way of resolving it silently is
    wrong: picking one drops a funded balance out of Σ, and summing them
    projects two anchors onto one path.

    ADR-0103 §10 puts multi-custodian sub-balances out of scope, and
    :meth:`services.investments.cash_plan_materialisation.CashPlanMaterialisationService._settling_positions`
    resolves the shape *for writing* — the earliest-created position is
    projected onto, the others are ignored and warned about. That rule is
    safe there because the ignored position is merely not written to. It is
    **not** safe here: an assembly that ignored a position would omit real
    money from the plan world's cash, and the Planning Desk would state a
    funding gap the book does not have. So the reader refuses where the
    writer resolves — the same fact, at the one seam where it changes a
    number.
    """


# ---------------------------------------------------------------------------
# Case workflow (ADR-0107). The Cases area's typed failures. All five are
# ``ValidationError`` subclasses (they carry a ``field`` and report an
# input-or-state problem on the way in), matching how the codebase already
# models field violations (:class:`IreneResolutionInvalid`) and state
# conflicts (:class:`ValuationModeError`). The ``state`` / ``kind`` / ``actor``
# vocabularies are plain ``TEXT`` — validated here, never a SQL enum.
# ---------------------------------------------------------------------------


class CaseStateInvalid(ValidationError):
    """Raised for a bad case state, or a state-dependent op with no better fit.

    Per ADR-0107 §2 a case's ``state`` is ``open`` / ``closed`` (lowercase
    TEXT, application-enforced). This covers a value outside that vocabulary
    and the residual "operation on a case in the wrong state" where a more
    specific sibling (:class:`CaseClosedError`) does not apply — for example
    an entry-append or close aimed at a case id that does not exist in the
    active tenant context (RLS having hidden it).
    """


class CaseClosedError(ValidationError):
    """Raised when any write targets an already-closed case.

    Per ADR-0107 §4 closed cases are immutable **in their entirety** — no
    entry append, no attachment create, no re-close, no reopen. The close
    transition is the single permitted mutation of a case row (ADR-0107 §2),
    and once taken the case is a read-only record. Reports a conflict with
    the case's current state (the :class:`ValuationModeError` shape), not a
    malformed field.
    """


class CaseEntryKindInvalid(ValidationError):
    """Raised when a timeline entry carries a kind outside the vocabulary.

    Per ADR-0107 §2 an entry's ``kind`` is one of ``opened`` / ``note`` /
    ``pin`` / ``decision_record`` / ``closed``. Also raised when
    ``kind='closed'`` reaches
    :meth:`~core.repositories.case_repository.CaseRepository.append_entry`:
    the ``closed`` entry is written **only** by ``close()``, so accepting it
    on the generic append path would let a case be marked closed without the
    state transition that word implies.
    """


class CaseActorInvalid(ValidationError):
    """Raised when a timeline entry names an actor outside the vocabulary.

    Per ADR-0107 §2 an entry's ``actor`` is one of ``pm`` / ``shirley`` /
    ``system`` (lowercase TEXT, application-enforced). Mirrors
    :class:`CaseEntryKindInvalid` — same base, same closed-vocabulary shape.
    """


class CaseClosingNoteMissing(ValidationError):
    """Raised when a case is closed without a closing note.

    Per ADR-0107 §4 the closing note is mandatory at close — a closed case
    that does not say why it was closed is not a decision record. The
    repository strips the supplied note and raises this when what remains is
    empty; the note is enforced here in application code, not by a schema
    NOT NULL (the column is nullable so an open case has none).
    """


# ---------------------------------------------------------------------------
# Transactions / trade-ticket errors (ADR-0128).
#
# ``TicketNotFound`` is a plain :class:`PortfoliFlowError`: a missing id is
# not a field violation, it is an absent object — and under RLS "absent" and
# "another tenant's" are indistinguishable by design. ``TicketStateInvalid``
# is a :class:`ValidationError` in the Case-error tradition above: it carries
# a ``field`` and reports a state or vocabulary problem on the way in. The
# two are deliberately distinct so a caller can tell "no such ticket" from
# "that ticket is no longer a draft" — a silent no-op would conflate them.
# ---------------------------------------------------------------------------


class TicketNotFound(PortfoliFlowError):
    """Raised when no trade ticket with the given id exists in this tenant.

    Per ADR-0128 §1 trade tickets are tenant-scoped and RLS-protected, so a
    ticket belonging to another tenant is simply not visible: this error
    states absence and never distinguishes "does not exist" from "not
    yours".
    """


class TicketStateInvalid(ValidationError):
    """Raised for a bad ticket status, or a status-dependent op on a ticket.

    Per ADR-0128 §3 a ticket's ``status`` runs over ``draft`` · ``proposed``
    · ``approved`` · ``sent`` · ``acknowledged`` · ``executed`` · ``booked``
    · ``cancelled`` (lowercase TEXT, application-enforced, CHECK-backed —
    never a SQL enum). This covers a value outside that vocabulary, a value
    outside the ``effect_type`` vocabulary of ``trade_ticket_effects``, and
    an operation aimed at a ticket whose current status forbids it — a draft
    edit against a ticket that has already left ``draft``.

    It reports the *state*, not the *transition*: which transitions are
    legal is service policy (ADR-0128 §3), enforced above this layer.
    """


class TicketIncomplete(ValidationError):
    """Raised when a trade ticket is too incomplete, or too inconsistent, to leave ``draft``.

    The propose-time (and, from S2, book-time) completeness gate of ADR-0128
    §4. ``proposed`` means "complete and validated" (ADR-0128 §3), so the
    fields a flow needs must be present *before* the status flips — while
    ``draft`` may be arbitrarily sparse, because a draft exists from the
    first explicit gesture, however early (MD-2, MD-11: ``Save as draft``
    is never gated).

    This is a **transition guard, not a schema constraint** (decision record
    §2.8): ``investments.anlv_code`` stays nullable and the ticket columns
    stay nullable, because the same rows must be writable by the onboarding
    and correction paths that do not go through a ticket at all.

    ``identifier`` names the specific gap so the surface can pick its copy
    (MD-9) without parsing a message. It is one of
    :data:`services.transactions.constants.BLOCK_IDENTIFIERS` for the gaps
    the composer has fixed copy for — ``missing_price``, ``missing_anlv`` —
    and one of
    :data:`services.transactions.constants.COMPLETENESS_IDENTIFIERS` for the
    gaps the composer prevents structurally and therefore never renders.

    Distinct from :class:`TicketStateInvalid`, which reports the ticket's
    *status* being wrong for the operation; here the status is right and the
    content is not. Currency mismatches raise
    :class:`CurrencyMismatchError` and oversells
    :class:`NonNegativeHoldingsError` — the ledger's own errors, reused
    rather than shadowed, so one violation has one type across every write
    path.

    Args:
        message: Description of the gap.
        identifier: The machine-readable gap identifier.
        field: Optional offending field name, per :class:`ValidationError`.
    """

    def __init__(
        self,
        message: str = "",
        identifier: str = "",
        field: str | None = None,
    ) -> None:
        super().__init__(message, field)
        self.identifier = identifier
