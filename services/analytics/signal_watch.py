# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The contract every signal-family producer implements (ADR-0116 §4).

ADR-0116 §4 states one *common contract* for the four defined signal
families and then four different measurements over it: "each family gets
a pure producer under ``services/analytics/`` (DB-free; the impure beat
fetches inputs and calls it), emits per-subject observations into the
existing watch-state/delta pipeline, and states its magnitude in **badness
units** — a scalar where larger is always worse". This module is that
sentence, made into types, so the producers hold the measurement and
nothing else.

It is deliberately small. The families are siblings, not instances of a
framework: :mod:`services.analytics.price_watch` and
:mod:`services.analytics.fx_watch` differ in exactly one expression each
(``max(0, decline)`` versus ``abs(move)``), and that difference is the
whole point of having two modules. What is shared here is the vocabulary
they would otherwise each invent — the observation and no-observation
result types, the window resolution, the status classification and the
human-facing status words — none of which any one family owns.

The other two families, :mod:`services.analytics.nav_freshness` and
:mod:`services.analytics.cash_coverage_watch`, measure something that is
not a move between two dated observations, so they use the classification
and the result types here and resolve no window at all. What they *do*
share is the shape of the answer: a badness magnitude against a threshold,
which is the only thing the delta layer ever asks of a producer.

What the window pair means, family by family
--------------------------------------------
:class:`SignalObservation` carries a ``(reference, latest)`` pair of dated
values. For ``price`` and ``fx`` that pair is the window the move was
measured across. The other two families fill it honestly rather than
decoratively, and each says so in its own module docstring:

* ``freshness`` — reference is the newest actual NAV (its date and its
  value); latest is the evaluation date carrying that *same* value
  forward, because by the ADR-0060 carry-forward rule the NAV in force
  today **is** that NAV. The magnitude is exactly the gap between the two
  dates.
* ``liquidity`` — reference is the cash on hand when the horizon opens;
  latest is the projected residual once the horizon's calls have settled.
  Both are values at the ends of the window the watchpoint names.

Nothing in the pair is ever a placeholder. A field that has no meaning for
a family would be a lie in an audit trail, and the audit trail is half of
why these observations are recorded at all.

Silence and calm are different answers
--------------------------------------
A producer that cannot evaluate its window returns :class:`NoObservation`,
never a magnitude of zero. A zero says "no adverse move" — a positive,
reassuring claim about the world. Missing data says nothing of the kind,
and a monitor that renders the two identically is lying about the second.
The beat logs a :class:`NoObservation` per subject and writes no
watch-state row for it, so the subject's acknowledged state cannot be
silently reset by an outage in its data supply.

Internal vocabulary, human vocabulary
-------------------------------------
Statuses stay ``OK`` / ``WARN`` / ``BREACH`` internally, so
:func:`services.analytics.irene_delta.edge_band_from_status` and the whole
edge machinery are reused verbatim (ADR-0116 §4). They are **never**
rendered in those words for a signal family: :func:`signal_status_label`
maps them to Calm / Approaching / Triggered, because "breach" is
regulatory language and stays reserved for the quota families. A price
watchpoint the operator set themselves is not violated when it fires — it
is triggered, which is what it is for.

Purity
------
Held to the analytics purity contract (ADR-0013 / ADR-0045 §3) like the
rest of this package: plain values in, frozen dataclasses out, no session
and no I/O. Everything that knows where a price or a rate comes from
lives in :mod:`services.irene.signal_delta`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from types import MappingProxyType
from uuid import UUID

from services.analytics.limit_coverage import classify_coverage_status

#: The four defined signal families (ADR-0116 §4), each with a pure producer
#: in this package. A family name in this module means "there is a producer
#: behind it" — which is why ``freshness`` and ``liquidity`` were absent
#: until P5 landed theirs.
FAMILY_PRICE: str = "price"
FAMILY_FX: str = "fx"
FAMILY_FRESHNESS: str = "freshness"
FAMILY_LIQUIDITY: str = "liquidity"

#: The ``freshness`` watchpoint's own subject key. The family is a
#: **singleton**: one watchpoint per tenant, whose ``max_age_days`` applies
#: to every investment, while the producer emits one
#: ``freshness:{investment_id}`` subject per investment — the enumeration
#: pattern the quota families already use (ADR-0116 §4). The wildcard says
#: "all subjects of this family", which is what a singleton covering the
#: whole book means, and it is what the sensitivity lookups fall back to for
#: an enumerated subject.
FRESHNESS_WILDCARD_SUBJECT_KEY: str = "freshness:*"

#: The ``liquidity`` watchpoint's subject key — one watchpoint, one subject,
#: the book's cash coverage against its projected calls (ADR-0116 §4). It
#: needs no wildcard: the singleton *is* the subject.
LIQUIDITY_SUBJECT_KEY: str = "liquidity:cash_coverage"

#: Where a singleton family's sensitivity settings live, keyed by family.
#: An enumerated subject (``freshness:{id}``) carries no registry row of its
#: own, so mute, WARN override and re-trigger delta resolve through the
#: singleton's key. Per-investment overrides are deliberately not v1: half a
#: per-subject registry is worse than none (ADR-0116 §Commissions).
SINGLETON_SUBJECT_KEY_BY_FAMILY: Mapping[str, str] = MappingProxyType(
    {
        FAMILY_FRESHNESS: FRESHNESS_WILDCARD_SUBJECT_KEY,
        FAMILY_LIQUIDITY: LIQUIDITY_SUBJECT_KEY,
    }
)

#: The internal status vocabulary, shared verbatim with the quota families
#: so the edge machinery needs no branch (ADR-0116 §4).
STATUS_OK: str = "OK"
STATUS_WARN: str = "WARN"
STATUS_TRIGGERED: str = "BREACH"

#: The human-facing words for those statuses. The only place the mapping is
#: stated; see the module docstring for why it exists at all.
_LABEL_BY_STATUS: dict[str, str] = {
    STATUS_OK: "Calm",
    STATUS_WARN: "Approaching",
    STATUS_TRIGGERED: "Triggered",
}

#: Percentage-point figures are quantised like every other magnitude the
#: delta layer compares (``services.analytics.limit_coverage._quantize``), so
#: a re-trigger delta means the same thing whichever family it is measured on.
_QUANTUM: Decimal = Decimal("0.0001")
_HUNDRED: Decimal = Decimal("100")
_ZERO: Decimal = Decimal("0")


@dataclass(frozen=True)
class DatedValue:
    """One dated observation of whatever the family measures.

    The producers take this rather than a repository DTO on purpose. A
    price row and an FX row are different objects in the database, and an
    ``fx`` *pair* rate is not a stored row at all — it is derived from two
    legs quoted against the dataset's reference currency. Reducing all of
    them to "a value on a date" is what lets the two producers be twins
    and keeps the pure layer ignorant of storage.

    Attributes:
        as_of_date: The observation's date.
        value: The observed value — a per-unit price, or a pair rate in
            the watchpoint's own ``BASE/QUOTE`` orientation.
    """

    as_of_date: date
    value: Decimal


@dataclass(frozen=True)
class SignalObservation:
    """One evaluated window for one signal subject.

    Attributes:
        subject_key: The subject the watchpoint defines or enumerates
            (``price:{id}`` / ``fx:{BASE}/{QUOTE}`` /
            ``freshness:{investment_id}`` / ``liquidity:cash_coverage``).
        magnitude: The observation in **badness units** — larger is always
            worse, and zero means "nothing adverse", never "no data" (that
            is :class:`NoObservation`). The unit is the family's own:
            percentage points for ``price`` / ``fx`` / ``liquidity``, whole
            days for ``freshness``.
        status: ``OK`` / ``WARN`` / ``BREACH`` — the internal vocabulary.
            Render it through :func:`signal_status_label`, never raw.
        threshold_pct: The trigger threshold the magnitude was measured
            against, in the same unit (``drop_pct`` for ``price``,
            ``move_pct`` for ``fx``, ``max_age_days`` for ``freshness``,
            a fixed 100 for ``liquidity``).
        window_days: The observation window in days.
        reference_value: The value at the window's start.
        reference_date: The date ``reference_value`` was observed — for
            ``price`` / ``fx`` the latest observation at or before
            ``as_of - window_days``, so it may predate the window start
            when the series is sparse. See the module docstring for what
            the pair means in the other two families.
        latest_value: The value at the window's end.
        latest_date: The date ``latest_value`` was observed.
    """

    subject_key: str
    magnitude: Decimal
    status: str
    threshold_pct: Decimal
    window_days: int
    reference_value: Decimal
    reference_date: date
    latest_value: Decimal
    latest_date: date


@dataclass(frozen=True)
class NoObservation:
    """A window that could not be evaluated, and why.

    Distinct from a magnitude of zero by construction — see the module
    docstring. The beat logs it and writes no watch-state row.

    Attributes:
        subject_key: The subject that could not be evaluated.
        reason: A short deterministic explanation, for the beat's log and
            (in P6) the monitor's "no data" row.
    """

    subject_key: str
    reason: str


#: What a producer returns: an evaluated window, or a stated inability to
#: evaluate one. Callers discriminate with ``isinstance``.
SignalResult = SignalObservation | NoObservation


def freshness_subject_key(investment_id: UUID) -> str:
    """Return the ``freshness`` subject key for one investment.

    The enumerated counterpart of :data:`FRESHNESS_WILDCARD_SUBJECT_KEY`:
    the registry holds one singleton row, the producer emits one subject
    per active investment (ADR-0116 §4). Formed here rather than in the
    beat so the key the finding carries and the key the sensitivity lookup
    falls back from are the same string by construction.

    Args:
        investment_id: The investment whose NAV age is being watched.

    Returns:
        ``freshness:{investment_id}``.
    """
    return f"{FAMILY_FRESHNESS}:{investment_id}"


def signal_status_label(status: str) -> str:
    """Return the human-facing word for an internal signal status.

    ``OK`` → ``Calm``, ``WARN`` → ``Approaching``, ``BREACH`` →
    ``Triggered`` (ADR-0116 §4). Every human-facing surface for a signal
    family goes through here; the raw status is an implementation detail
    of the edge machinery and must not reach a card, a note or a monitor
    row.

    Args:
        status: One of ``OK`` / ``WARN`` / ``BREACH``.

    Returns:
        The human-facing label.

    Raises:
        ValueError: If ``status`` is not a signal status.
    """
    try:
        return _LABEL_BY_STATUS[status]
    except KeyError:
        raise ValueError(
            f"signal_status_label: unmapped status {status!r}; expected one "
            f"of {sorted(_LABEL_BY_STATUS)}."
        ) from None


def classify_signal_status(
    magnitude: Decimal,
    threshold_pct: Decimal,
    warn_threshold_pct: Decimal,
) -> str:
    """Classify a badness magnitude against a watchpoint's threshold.

    The same machinery the quota families classify with, with
    ``max_pct := threshold_pct`` (ADR-0116 §4) — WARN means "within the
    warn fraction of the trigger threshold", exactly as it means "within
    the warn fraction of the ceiling" for a limit.

    **One boundary is ours, and deliberately so.**
    :func:`~services.analytics.limit_coverage.classify_coverage_status`
    tests the ceiling *strictly* (``coverage > max``), because a limit set
    at 50% is not breached at exactly 50%. A watchpoint is the opposite:
    ADR-0116 §4 defines the trigger as ``move >= drop_pct``, so at exactly
    the threshold the watchpoint has fired — that is what the operator set
    it for. The trigger comparison is therefore made here and only the
    OK/WARN split is delegated, which leaves the coverage classifier's
    contract untouched rather than forking it.

    Args:
        magnitude: The observed badness in percentage points (>= 0).
        threshold_pct: The trigger threshold in percentage points (> 0).
        warn_threshold_pct: The subject's effective WARN fraction, as a
            percentage of ``threshold_pct``.

    Returns:
        ``OK`` / ``WARN`` / ``BREACH``.
    """
    if magnitude >= threshold_pct:
        return STATUS_TRIGGERED
    # Strictly below the threshold, so the delegated call can only answer
    # OK or WARN — the ceiling branch it owns is unreachable from here.
    return classify_coverage_status(magnitude, threshold_pct, warn_threshold_pct)


def latest_at_or_before(series: Sequence[DatedValue], cutoff: date) -> DatedValue | None:
    """Return the latest observation at or before ``cutoff``, or ``None``.

    The carry-forward lookup both producers resolve their window with,
    mirroring the ADR-0060 NAV rule and the FX converter's: a series with
    weekend and holiday gaps must still answer for a Saturday. Order-blind
    — it takes the maximum rather than assuming the caller sorted — so a
    producer cannot be made to lie by an unsorted fetch.

    Args:
        series: The observations to search. May be empty or unsorted.
        cutoff: The date to look back from, inclusive.

    Returns:
        The applicable observation, or ``None`` when the series holds
        nothing at or before ``cutoff``.
    """
    candidates = [observation for observation in series if observation.as_of_date <= cutoff]
    if not candidates:
        return None
    return max(candidates, key=lambda observation: observation.as_of_date)


def window_start(as_of: date, window_days: int) -> date:
    """Return the window's start date — ``as_of`` less ``window_days``."""
    return as_of - timedelta(days=window_days)


def percentage_move(reference: Decimal, latest: Decimal) -> Decimal:
    """Return the signed move from ``reference`` to ``latest``, in pp.

    Positive when the value rose. The families turn this into their own
    badness unit: ``price`` keeps only the decline, ``fx`` takes the
    absolute value (ADR-0116 §4).

    Args:
        reference: The value at the window's start (non-zero).
        latest: The value at the window's end.

    Returns:
        ``(latest - reference) / reference * 100``, quantised.
    """
    return quantize_pp((latest - reference) / reference * _HUNDRED)


def quantize_pp(value: Decimal) -> Decimal:
    """Quantise a percentage-point figure to four places, banker's rounding."""
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)


def resolve_window(
    series: Sequence[DatedValue],
    *,
    as_of: date,
    window_days: int,
    subject_key: str,
    quantity: str,
) -> tuple[DatedValue, DatedValue] | NoObservation:
    """Resolve a window's reference and latest observations, or say why not.

    The three ways a window fails to resolve, each named rather than
    collapsed into one silence: an empty series, no observation at or
    before the window start, and a reference value of zero (against which
    a percentage move is undefined — the tables' positivity CHECKs make
    this unreachable in practice, and unreachable is not the same as
    unhandled in a pure function).

    Args:
        series: The family's observations.
        as_of: The evaluation date — the window's end.
        window_days: The window width in days.
        subject_key: The subject, for the :class:`NoObservation` it may
            return.
        quantity: The word for what is being observed (``price`` /
            ``rate``), so the stated reason reads in the family's own
            terms.

    Returns:
        ``(reference, latest)`` when the window resolves, else the
        :class:`NoObservation` explaining what was missing.
    """
    if not series:
        return NoObservation(
            subject_key=subject_key,
            reason=f"no {quantity} observations at all",
        )

    start = window_start(as_of, window_days)
    reference = latest_at_or_before(series, start)
    if reference is None:
        return NoObservation(
            subject_key=subject_key,
            reason=(
                f"no {quantity} observation at or before {start.isoformat()} "
                f"(the start of the {window_days}-day window)"
            ),
        )

    latest = latest_at_or_before(series, as_of)
    if latest is None:  # pragma: no cover - implied by `reference` resolving
        return NoObservation(
            subject_key=subject_key,
            reason=f"no {quantity} observation at or before {as_of.isoformat()}",
        )

    if reference.value == _ZERO:
        return NoObservation(
            subject_key=subject_key,
            reason=(
                f"the reference {quantity} on {reference.as_of_date.isoformat()} is "
                "zero — a percentage move against it is undefined"
            ),
        )

    return reference, latest


__all__ = [
    "FAMILY_FRESHNESS",
    "FAMILY_FX",
    "FAMILY_LIQUIDITY",
    "FAMILY_PRICE",
    "FRESHNESS_WILDCARD_SUBJECT_KEY",
    "LIQUIDITY_SUBJECT_KEY",
    "SINGLETON_SUBJECT_KEY_BY_FAMILY",
    "STATUS_OK",
    "STATUS_TRIGGERED",
    "STATUS_WARN",
    "DatedValue",
    "NoObservation",
    "SignalObservation",
    "SignalResult",
    "classify_signal_status",
    "freshness_subject_key",
    "latest_at_or_before",
    "percentage_move",
    "quantize_pp",
    "resolve_window",
    "signal_status_label",
    "window_start",
]
