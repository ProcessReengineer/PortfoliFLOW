# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The deterministic urgency floor and band derivation for Irene (ADR-0088).

This module is the **home of the materiality judgement**. The delta layer
(ADR-0087) decides *what is worth showing Irene*; Irene decides *how to
phrase and whether to surface* and proposes an ``urgency_suggestion``; this
module decides the **final** urgency and band by deterministic rules:

    final = clamp(suggestion, floor[trigger_type], cap[source or trigger])

and the band follows from the final urgency by fixed boundaries. The model
*suggests*, deterministic rules *decide* — so "why an 8?" always has a
rule-based answer (:func:`explain_urgency`).

Purity
------
Like :mod:`services.analytics.irene_delta` and
:mod:`services.analytics.rss_bucketing`, this module lives under
``services/analytics/`` and is held to the analytics purity contract
(ADR-0013 / ADR-0045 §3), enforced by
``tests/regression/test_analytics_layer_pure.py``: no database, no ORM, no
FastAPI, no Qt. Every function is pure — integers / enums / config in,
integers / enums out — with no I/O. The *impure* beat
(:mod:`services.irene.beat`) calls these functions with plain arguments.

Floor Config is the calibration surface
---------------------------------------
Trigger-type floors, source/trigger caps, band boundaries, the options
gate, and the ADR-0087 delta thresholds (``re_trigger_delta``, RSS window,
pinned embedding model, similarity threshold, tag→asset-class map) all live
in :class:`FloorConfig` — *configuration*, not scattered constants —
because materiality is an ongoing **calibration** concern, not a fixed
technical one (ADR-0088). ``FloorConfig`` is the single coherent
calibration object the beat threads through both the delta layer and the
floor; :mod:`services.irene.delta_config` re-exports it under the
historical name ``DeltaThresholds`` for the delta functions that predate
the merge. Floor Config is the primary calibration surface over the coming
months; changes to it are auditable configuration. Prompt 5 (ADR-0089)
surfaces some of it in the Calibration section; this module builds none
of that UI.

Implementation note (2026-07-02) — where the floor runs
-------------------------------------------------------
ADR-0088 §Consequences says "``run_synthesis`` must apply the floor after
collecting tool calls and before persisting." That locus is an erratum:
``run_synthesis`` (:mod:`services.ai_service_core`) returns a
``SynthesisResult`` and is **delta-agnostic** by design — it cannot know a
``subject_key`` was a falling edge or RSS-only, which the floor needs for
the trigger-type floor and the source cap. The only place with all three
inputs (the model's ``urgency_suggestion`` plus the ``trigger_type`` /
``source`` derived from the eligible findings) is the **beat's persistence
loop** (:func:`services.irene.beat.run_beat`). The floor is therefore
applied there, and ``run_synthesis`` stays a thin, delta-agnostic
transport. This note is mirrored in the ADR-0088 file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from collections.abc import Mapping

from services.analytics.irene_delta import KIND_FALLING_EDGE

# ---------------------------------------------------------------------------
# Enums (as module constants — one source of truth for callers to compare
# against, rather than string literals scattered through the beat).
# ---------------------------------------------------------------------------

# Trigger types — the first floor axis. Each maps to a minimum urgency the
# floor raises to. ``TRIGGER_FUND_CLOSURE`` is a defined-but-unreached seam:
# there is no fund-closure subject producer yet (ADR-0088). It carries a
# fixed level (floor = cap = 10) so that, when a producer lands, the floor
# already pins it to the top without a calibration change.
TRIGGER_LIMIT_BREACH: str = "limit_breach"
TRIGGER_LIMIT_ESCALATION: str = "limit_escalation"
TRIGGER_ALL_CLEAR: str = "all_clear"
TRIGGER_RSS_CLUSTER: str = "rss_cluster"
TRIGGER_FUND_CLOSURE: str = "fund_closure"  # seam: no producer yet

# The four signal-family trigger types (ADR-0116 §4), all reached since P5
# landed the last two producers. They were declared ahead of those
# producers so the calibration surface that stores per-trigger floors and
# caps (``floor_calibration``, migration b033) had meaningful columns from
# the moment it existed, rather than a set that grew under it later —
# unlike ``TRIGGER_FUND_CLOSURE``, which remains a genuine seam.
TRIGGER_PRICE: str = "price_trigger"
TRIGGER_FX: str = "fx_trigger"
TRIGGER_FRESHNESS: str = "freshness_trigger"
TRIGGER_LIQUIDITY: str = "liquidity_trigger"

# Sources — the second (cap) axis. RSS-only findings are capped low; an RSS
# item corroborating an internal edge has already been *merged* into that
# internal finding upstream by ``services.irene.correlation`` and so reaches
# the floor as ``SOURCE_INTERNAL`` (see the cap docstring).
SOURCE_INTERNAL: str = "internal"
SOURCE_RSS: str = "rss"

# The canonical final band vocabulary (ADR-0085 / ADR-0088), lowercase.
# These are the card's final bands, derived from the final urgency here.
# They are wholly distinct from the delta layer's *edge bands*
# (``services.analytics.irene_delta.edge_band_from_status`` → note / watch /
# act), which order coverage severity for edge detection only and never
# reach the persisted card.
BAND_INFORMATIONAL: str = "informational"
BAND_NOTEWORTHY: str = "noteworthy"
BAND_CRITICAL: str = "critical"

# Total order over the final bands (benign → severe), for the options gate.
_BAND_RANK: Mapping[str, int] = MappingProxyType(
    {BAND_INFORMATIONAL: 0, BAND_NOTEWORTHY: 1, BAND_CRITICAL: 2}
)

# The raw coverage status that pins an internal edge to ``limit_breach``.
_STATUS_BREACH: str = "BREACH"

# The signal families that have a producer, and the trigger type each one's
# non-benign edges floor to (ADR-0116 §4). All four since P5 — a mapping
# here is a claim that something reaches it, which is why the last two were
# added with their producers and not before. A falling edge is *not* looked
# up here at all — an all-clear stays ``all_clear`` whatever raised it,
# which is what keeps the "an all-clear is never itself urgent" invariant
# true for signal families too.
_TRIGGER_BY_SIGNAL_FAMILY: Mapping[str, str] = MappingProxyType(
    {
        "price": TRIGGER_PRICE,
        "fx": TRIGGER_FX,
        "freshness": TRIGGER_FRESHNESS,
        "liquidity": TRIGGER_LIQUIDITY,
    }
)

# The valid urgency range (ADR-0088: 1–10 scale, retained for ordering).
_URGENCY_MIN: int = 1
_URGENCY_MAX: int = 10


# ---------------------------------------------------------------------------
# Default calibration values. Plain data (stdlib types only) so the module
# stays purity-clean; ``FloorConfig`` composes them into one object.
# ---------------------------------------------------------------------------

# Floor: the minimum final urgency the trigger raises to. ADR-0088 fixes
# limit_breach ≥ 7 and fund_closure = 10 (a fixed level); the rest are
# calibration.
_DEFAULT_FLOOR: Mapping[str, int] = MappingProxyType(
    {
        TRIGGER_LIMIT_BREACH: 7,
        TRIGGER_LIMIT_ESCALATION: 5,
        TRIGGER_ALL_CLEAR: 1,
        TRIGGER_RSS_CLUSTER: 1,
        TRIGGER_FUND_CLOSURE: 10,  # seam: floor = cap = 10 (pinned)
        # ADR-0116 §4 v1 values, refinable per tenant via floor_calibration.
        # price and fx sit mid-scale: an adverse move worth a finding is not
        # noise, but it is not a rule violation either. liquidity floors
        # highest of the four — a coverage shortfall against projected calls
        # is the one signal family with a payment date behind it.
        TRIGGER_PRICE: 4,
        TRIGGER_FX: 4,
        TRIGGER_FRESHNESS: 3,
        TRIGGER_LIQUIDITY: 6,
    }
)

# Cap: the maximum final urgency, keyed by BOTH source and trigger. The
# effective cap for a finding is ``min(cap[source], cap[trigger_type])``.
# - RSS-only is capped at the informational band's top (3) — a standalone
#   press cluster never outranks a quiet internal note (ADR-0087/0088).
# - all_clear (a falling edge) is capped at informational regardless of
#   source (ADR-0087) — an "all clear" is never itself urgent.
# - fund_closure = 10 (matches its floor: a pinned top level).
# - internal / limit_breach / limit_escalation impose no cap of their own
#   (10), so an internal finding's ceiling comes from its trigger cap.
# - freshness_trigger is capped at 5 (ADR-0116 §4): a stale NAV is a
#   data-quality problem and must never outrank a breach, however long the
#   staleness runs. The other three signal families impose no cap of their
#   own; their ceiling is the source cap.
_DEFAULT_CAP: Mapping[str, int] = MappingProxyType(
    {
        SOURCE_INTERNAL: 10,
        SOURCE_RSS: 3,
        TRIGGER_LIMIT_BREACH: 10,
        TRIGGER_LIMIT_ESCALATION: 10,
        TRIGGER_ALL_CLEAR: 3,
        TRIGGER_RSS_CLUSTER: 3,
        TRIGGER_FUND_CLOSURE: 10,
        TRIGGER_PRICE: 10,
        TRIGGER_FX: 10,
        TRIGGER_FRESHNESS: 5,
        TRIGGER_LIQUIDITY: 10,
    }
)

# Band boundaries: the two urgency cut points dividing 1–10 into the three
# final bands. ``(3, 6)`` means informational = 1–3, noteworthy = 4–6,
# critical = 7–10. Validated monotonic and covering 1–10 at construction.
_DEFAULT_BAND_BOUNDARIES: tuple[int, int] = (3, 6)

# The options gate: ``options`` are kept only at/above this band. Below it
# (an ``informational`` card) options are dropped — a level-1 card is pure
# fact, never advice (ADR-0088).
_DEFAULT_OPTIONS_MIN_BAND: str = BAND_NOTEWORTHY

# --- ADR-0087 delta thresholds, folded in (formerly DeltaThresholds). -----

# Per-subject-type magnitude re-trigger delta (native unit of the subject's
# magnitude; for limit subjects that is coverage percentage points). The
# ``rss`` entry is 0 because RSS subjects are non-scalar and never re-trigger
# by magnitude — it exists only so a lookup by subject type never raises.
# The four signal families (ADR-0116 §4) state their magnitude in badness
# units — larger is always worse — so the same direction-agnostic delta
# arithmetic applies and each needs an entry. The v1 values are deliberately
# the same 5.0 as the quota families: in every case the unit is "percentage
# points of the thing being watched" (price / FX move, coverage-ratio
# shortfall) except ``freshness``, whose unit is days beyond the age limit,
# where 5 days is a week of statement lag. All are per-tenant calibratable
# through ``floor_calibration`` (migration b033).
_DEFAULT_RE_TRIGGER_DELTA: Mapping[str, Decimal] = MappingProxyType(
    {
        "saa": Decimal("5.0"),
        "anlv": Decimal("5.0"),
        "rss": Decimal("0"),
        "price": Decimal("5.0"),
        "fx": Decimal("5.0"),
        "freshness": Decimal("5.0"),
        "liquidity": Decimal("5.0"),
    }
)

# The pinned embedding model (ADR-0087 Part B). Auditable configuration:
# changing it freezes open ``rss:cluster:*`` buckets (the key is a hash over
# membership, never over any vector, so a model change cannot re-form or
# re-key a frozen bucket).
_DEFAULT_EMBEDDING_MODEL: str = "openai/text-embedding-3-small"

# Fixed cosine-similarity threshold for nearest-open-bucket assignment.
_DEFAULT_SIMILARITY_THRESHOLD: float = 0.83

# The auditable tag → internal asset-class correspondence for the
# correlation lift (ADR-0087 Part B §1.5). Keyed by curated RSS tag, mapping
# to zero or more internal class tokens. A tag mapping to the empty tuple
# (macro / regulator / swiss_finance) is broad and never corroborates a
# single asset class, so its buckets always stand alone.
_DEFAULT_TAG_ASSET_CLASS_MAP: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "equities": ("equities", "listed_equity"),
        "credit": ("listed_bonds", "private_debt"),
        "real_estate": ("real_estate",),
        "private_markets": (
            "private_equity",
            "private_debt",
            "real_estate",
            "infra_equity",
        ),
        "macro": (),
        "regulator": (),
        "swiss_finance": (),
    }
)


@dataclass(frozen=True)
class FloorConfig:
    """The single calibration object for Irene's materiality layer (ADR-0088).

    Holds both the deterministic-floor axes (floors, caps, band
    boundaries, options gate) and the ADR-0087 delta thresholds
    (``re_trigger_delta`` and the RSS clustering parameters). It is
    ``DeltaThresholds`` under its historical name (re-exported from
    :mod:`services.irene.delta_config`), so the delta functions keep
    receiving what they need unchanged while the beat threads one coherent
    config.

    Construction validates the invariants so a miscalibration cannot
    silently invert the clamp or leave the band scale uncovered:

    * ``floor[t] <= cap[t]`` for every trigger type ``t`` (the tight case
      is ``fund_closure``: floor = cap = 10). A configuration violating
      this raises at construction.
    * ``band_boundaries`` is strictly monotonic and covers 1–10 with three
      non-empty bands.
    * ``options_min_band`` is a valid final band.

    Attributes:
        floor: Minimum final urgency per trigger type (the floor *raises*).
        cap: Maximum final urgency, keyed by source AND trigger; the
            effective cap is ``min(cap[source], cap[trigger_type])``.
        band_boundaries: The two urgency cut points ``(b0, b1)`` splitting
            1–10 into informational (≤ b0), noteworthy (≤ b1), critical.
        options_min_band: The lowest final band at which ``options`` are
            kept; below it the beat drops them.
        re_trigger_delta: ADR-0087 per-subject-type magnitude threshold.
        rss_time_window_hours: RSS bucket width in hours (v0: 24h calendar
            day buckets from midnight UTC).
        embedding_model: Pinned embedding model id (auditable; freezes open
            buckets on change).
        similarity_threshold: Fixed cosine-similarity clustering threshold.
        tag_asset_class_map: Auditable tag → asset-class correspondence for
            the correlation lift.
    """

    floor: Mapping[str, int] = field(default_factory=lambda: _DEFAULT_FLOOR)
    cap: Mapping[str, int] = field(default_factory=lambda: _DEFAULT_CAP)
    band_boundaries: tuple[int, int] = _DEFAULT_BAND_BOUNDARIES
    options_min_band: str = _DEFAULT_OPTIONS_MIN_BAND
    re_trigger_delta: Mapping[str, Decimal] = field(
        default_factory=lambda: _DEFAULT_RE_TRIGGER_DELTA
    )
    rss_time_window_hours: int = 24
    embedding_model: str = _DEFAULT_EMBEDDING_MODEL
    similarity_threshold: float = _DEFAULT_SIMILARITY_THRESHOLD
    tag_asset_class_map: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: _DEFAULT_TAG_ASSET_CLASS_MAP
    )

    def __post_init__(self) -> None:
        """Validate the floor/cap and band-boundary invariants (ADR-0088).

        Raises:
            ValueError: If any ``floor[t] > cap[t]`` (the clamp could
                invert), or the band boundaries are not strictly monotonic
                and covering 1–10, or ``options_min_band`` is not a final
                band.
        """
        for trigger, floor_value in self.floor.items():
            cap_value = self.cap.get(trigger)
            if cap_value is None:
                raise ValueError(
                    f"FloorConfig: trigger {trigger!r} has a floor "
                    f"({floor_value}) but no cap entry."
                )
            if floor_value > cap_value:
                raise ValueError(
                    f"FloorConfig: floor[{trigger!r}]={floor_value} exceeds "
                    f"cap[{trigger!r}]={cap_value}; the clamp would invert. "
                    "A trigger's floor must never exceed its cap."
                )

        b0, b1 = self.band_boundaries
        if not (_URGENCY_MIN <= b0 < b1 <= _URGENCY_MAX - 1):
            raise ValueError(
                "FloorConfig: band_boundaries must be strictly monotonic and "
                f"cover 1–10 with three non-empty bands (expected "
                f"{_URGENCY_MIN} <= b0 < b1 <= {_URGENCY_MAX - 1}); got "
                f"{self.band_boundaries}."
            )

        if self.options_min_band not in _BAND_RANK:
            raise ValueError(
                f"FloorConfig: options_min_band {self.options_min_band!r} is "
                f"not a final band; expected one of {sorted(_BAND_RANK)}."
            )


# The single default calibration instance the beat threads through the
# delta layer and the floor (re-exported as ``DEFAULT_DELTA_THRESHOLDS``).
DEFAULT_FLOOR_CONFIG: FloorConfig = FloorConfig()

# The tenant-wide WARN default: coverage strictly above
# ``ceiling * warn_threshold_pct / 100`` is WARN. It is not a ``FloorConfig``
# field — the coverage engine has always taken it as a call parameter
# (:func:`services.analytics.limit_coverage.evaluate_coverage`) and the
# monitor route restates it — but ADR-0116 §7 makes it per-tenant
# calibratable, so the canonical value is named here, alongside the rest of
# the calibration surface, for the calibration write path to compare against.
# Threading the *composed* value through the engine and the route is P3's
# work; this constant does not change either of them today.
DEFAULT_WARN_THRESHOLD_PCT: Decimal = Decimal("90.0")


def validate_pinned_invariants(config: FloorConfig) -> None:
    """Assert the four honesty invariants that are never tenant knobs.

    ADR-0088 distinguishes fixed levels from calibration; ADR-0116 §7 pins
    that distinction down and makes it enforceable, because the Calibration
    editor now lets a tenant move band boundaries, floors and caps. Each
    invariant below survives a boundary edit only if it is *re-checked*
    against the edited boundaries — which is exactly what this function is
    for, and why the calibration write path runs it before persisting: the
    beat must never be the first to discover an inverted configuration
    (ADR-0116 §5).

    The four invariants:

    1. ``fund_closure`` floor = cap = 10 — a pinned level, not calibration.
       It has no column in ``floor_calibration`` at all, so a tenant cannot
       reach it; this check catches a bad *default*.
    2. ``floor[limit_breach] >= band_boundaries[1] + 1`` — a regulatory
       breach can never render below the critical band, whatever the
       boundaries were moved to.
    3. ``cap[SOURCE_RSS] <= band_boundaries[0]`` — a standalone press
       cluster never outranks an internal finding (ADR-0087/0088).
    4. ``cap[all_clear] <= band_boundaries[0]`` — an all-clear is never
       itself urgent.

    This is deliberately **not** folded into :meth:`FloorConfig.__post_init__`:
    ADR-0116 §7 reuses the constructor's validation unchanged, and the
    pinned invariants are a separate, additional gate applied at the
    calibration seam. Composition
    (:func:`services.analytics.floor_composition.compose_floor_config`) runs
    both.

    Args:
        config: The candidate configuration — typically defaults composed
            with a tenant's calibration deviations.

    Raises:
        ValueError: On the first violated invariant, naming the offending
            values and the rule they broke.
    """
    b0, b1 = config.band_boundaries

    closure_floor = config.floor.get(TRIGGER_FUND_CLOSURE)
    closure_cap = config.cap.get(TRIGGER_FUND_CLOSURE)
    if closure_floor != _URGENCY_MAX or closure_cap != _URGENCY_MAX:
        raise ValueError(
            f"FloorConfig: {TRIGGER_FUND_CLOSURE} is a pinned level, not "
            f"calibration — floor and cap must both be {_URGENCY_MAX}; got "
            f"floor={closure_floor}, cap={closure_cap}."
        )

    breach_floor = config.floor[TRIGGER_LIMIT_BREACH]
    if breach_floor < b1 + 1:
        raise ValueError(
            f"FloorConfig: floor[{TRIGGER_LIMIT_BREACH!r}]={breach_floor} lies "
            f"below the critical band (which starts at {b1 + 1} for band "
            f"boundaries {config.band_boundaries}). A regulatory breach can "
            "never render below critical — raise the floor or lower the "
            "upper band boundary."
        )

    rss_cap = config.cap[SOURCE_RSS]
    if rss_cap > b0:
        raise ValueError(
            f"FloorConfig: cap[{SOURCE_RSS!r}]={rss_cap} exceeds the "
            f"informational band's top ({b0}). A standalone press cluster "
            "never outranks an internal finding."
        )

    all_clear_cap = config.cap[TRIGGER_ALL_CLEAR]
    if all_clear_cap > b0:
        raise ValueError(
            f"FloorConfig: cap[{TRIGGER_ALL_CLEAR!r}]={all_clear_cap} exceeds "
            f"the informational band's top ({b0}). An all-clear is never "
            "itself urgent."
        )


@dataclass(frozen=True)
class UrgencyDecision:
    """The auditable decomposition of one final-urgency computation.

    Makes "why an 8?" reconstructable (ADR-0088 explainability): the final
    urgency is exactly ``max(floor, min(suggestion, cap))``, and ``reason``
    states which of the three inputs bound the result.

    Attributes:
        suggestion: Irene's proposed urgency (already clamped to 1–10).
        floor: The trigger-type floor applied.
        cap: The effective cap (``min(cap[source], cap[trigger_type])``).
        final: The resulting final urgency.
        reason: A short deterministic explanation of the binding input.
    """

    suggestion: int
    floor: int
    cap: int
    final: int
    reason: str


def clamp_suggestion(suggestion: int) -> int:
    """Clamp a raw model suggestion to the valid 1–10 urgency range.

    The model *should* propose 1–10, but is non-deterministic; a missing or
    out-of-range value is clamped rather than trusted (ADR-0088). This runs
    before :func:`final_urgency` so the floor/cap arithmetic always sees a
    valid input.

    Args:
        suggestion: The raw ``urgency_suggestion`` from the model.

    Returns:
        ``suggestion`` clamped into ``[1, 10]``.
    """
    return max(_URGENCY_MIN, min(_URGENCY_MAX, suggestion))


def derive_trigger_type(
    *, source: str, kind: str | None, status: str | None, family: str | None = None
) -> str:
    """Derive the floor's trigger-type axis from an eligible finding.

    Deterministic mapping of ``(source, kind, status, family)`` — all
    rule-formed by the delta layer, never an LLM label — to a trigger type
    (ADR-0088 §0.3, extended by ADR-0116 §4):

    * an RSS-sourced finding → ``rss_cluster`` (kind/status are ``None``);
    * a falling edge → ``all_clear`` (capped low regardless of status,
      source or family). Checked before the family, so an eased ``price``
      watchpoint is an all-clear exactly as an unwound limit is — the
      "an all-clear is never itself urgent" invariant (ADR-0116 §7) holds
      across all families because there is one branch for it;
    * a **signal** family's non-benign edge → that family's own trigger
      (``price_trigger`` / ``fx_trigger`` / ``freshness_trigger`` /
      ``liquidity_trigger``). A watchpoint the operator defined has its own
      floor and is never described as a limit breach — and its own cap,
      which is how a stale NAV stays below a breach however long the
      staleness runs;
    * any other internal edge whose current status is ``BREACH`` (a rising
      edge into a breach, or a magnitude re-trigger *within* a breach) →
      ``limit_breach``. Status is prioritised over kind here so a re-trigger
      inside a breach is not silently downgraded to an escalation — a breach
      floors to the critical band whether it just formed or moved again;
    * any other internal edge (a rising edge into WARN, or a within-WARN
      re-trigger) → ``limit_escalation``.

    There is no ``fund_closure`` branch: no subject produces that trigger
    yet (ADR-0088 seam). It lives in the config table so that, when a
    producer lands, the floor already pins it.

    Args:
        source: :data:`SOURCE_INTERNAL` or :data:`SOURCE_RSS`.
        kind: The internal delta kind (``KIND_*`` in
            :mod:`services.analytics.irene_delta`), or ``None`` for RSS.
        status: The internal status (``OK`` / ``WARN`` / ``BREACH``), or
            ``None`` for RSS. For a signal family ``BREACH`` is the
            internal spelling of *Triggered* (ADR-0116 §4).
        family: The subject's family, for the signal families that carry
            their own trigger type. ``None`` — the quota case — keeps the
            pre-ADR-0116 behaviour exactly.

    Returns:
        One of the ``TRIGGER_*`` constants.
    """
    if source == SOURCE_RSS:
        return TRIGGER_RSS_CLUSTER
    if kind == KIND_FALLING_EDGE:
        return TRIGGER_ALL_CLEAR
    signal_trigger = _TRIGGER_BY_SIGNAL_FAMILY.get(family or "")
    if signal_trigger is not None:
        return signal_trigger
    if status == _STATUS_BREACH:
        return TRIGGER_LIMIT_BREACH
    return TRIGGER_LIMIT_ESCALATION


def explain_urgency(
    *, suggestion: int, trigger_type: str, source: str, config: FloorConfig
) -> UrgencyDecision:
    """Compute the final urgency and its auditable decomposition.

    ``final = clamp(suggestion, floor[trigger_type], eff_cap)`` where
    ``eff_cap = min(cap[source], cap[trigger_type])``. The clamp is written
    ``max(floor, min(suggestion, eff_cap))``, which is inversion-proof even
    if a source cap dipped below a trigger floor for a pairing that should
    not occur: the ``floor[t] <= cap[t]`` invariant (validated at
    construction) guarantees the intended clamp for the ``(trigger,
    source)`` pairs that actually arise. In particular a limit breach always
    reaches the floor as ``SOURCE_INTERNAL`` — a corroborating RSS item was
    already merged into the internal finding upstream by
    :mod:`services.irene.correlation`, and standalone RSS is only ever
    ``rss_cluster`` (floor 1 ≤ RSS cap 3) — so the RSS source cap never
    clashes with the breach floor.

    Args:
        suggestion: Irene's proposed urgency (clamp to 1–10 first).
        trigger_type: One of the ``TRIGGER_*`` constants.
        source: :data:`SOURCE_INTERNAL` or :data:`SOURCE_RSS`.
        config: The active :class:`FloorConfig`.

    Returns:
        The :class:`UrgencyDecision` — suggestion, floor, cap, final, and
        the binding reason.
    """
    floor_value = config.floor[trigger_type]
    cap_value = min(config.cap[source], config.cap[trigger_type])
    final = max(floor_value, min(suggestion, cap_value))

    if floor_value > suggestion and final == floor_value:
        reason = (
            f"raised to the {trigger_type} floor {floor_value} (suggestion {suggestion} < floor)"
        )
    elif cap_value < suggestion and final == cap_value:
        reason = (
            f"capped at {cap_value} for source={source}/{trigger_type} "
            f"(suggestion {suggestion} > cap)"
        )
    else:
        reason = f"suggestion {suggestion} honoured within [{floor_value}, {cap_value}]"
    return UrgencyDecision(
        suggestion=suggestion,
        floor=floor_value,
        cap=cap_value,
        final=final,
        reason=reason,
    )


def final_urgency(*, suggestion: int, trigger_type: str, source: str, config: FloorConfig) -> int:
    """Return the deterministic final urgency for a surfaced finding.

    The thin scalar wrapper over :func:`explain_urgency` — same rules, just
    the resulting integer. Use :func:`explain_urgency` when the audit
    decomposition ("why an 8?") is needed.

    Args:
        suggestion: Irene's proposed urgency (clamp to 1–10 first).
        trigger_type: One of the ``TRIGGER_*`` constants.
        source: :data:`SOURCE_INTERNAL` or :data:`SOURCE_RSS`.
        config: The active :class:`FloorConfig`.

    Returns:
        The final urgency ``max(floor, min(suggestion, cap))``.
    """
    return explain_urgency(
        suggestion=suggestion,
        trigger_type=trigger_type,
        source=source,
        config=config,
    ).final


def band_from_final_urgency(urgency: int, config: FloorConfig) -> str:
    """Derive the canonical final band from the final urgency (ADR-0088).

    Fixed boundaries, never the LLM: ``informational`` (≤ b0) /
    ``noteworthy`` (≤ b1) / ``critical`` (> b1), where ``(b0, b1) =
    config.band_boundaries``. The 1–10 value is retained only for ordering
    within a band.

    Args:
        urgency: The final urgency (the output of :func:`final_urgency`).
        config: The active :class:`FloorConfig`.

    Returns:
        One of :data:`BAND_INFORMATIONAL` / :data:`BAND_NOTEWORTHY` /
        :data:`BAND_CRITICAL`.
    """
    b0, b1 = config.band_boundaries
    if urgency <= b0:
        return BAND_INFORMATIONAL
    if urgency <= b1:
        return BAND_NOTEWORTHY
    return BAND_CRITICAL


def options_allowed(band: str, config: FloorConfig) -> bool:
    """Return whether ``options`` may be kept at this final band.

    The advise-half gate (ADR-0088): ``options`` are kept only at/above
    ``config.options_min_band``. Below it — an ``informational`` card —
    the beat drops ``options`` even if Irene supplied them, because a
    level-1 card is pure fact, never advice.

    Args:
        band: A final band (:func:`band_from_final_urgency`'s output).
        config: The active :class:`FloorConfig`.

    Returns:
        ``True`` when the band is at or above ``options_min_band``.

    Raises:
        ValueError: If ``band`` is not a canonical final band.
    """
    try:
        band_rank = _BAND_RANK[band]
    except KeyError:
        raise ValueError(
            f"options_allowed: unknown band {band!r}; expected one of {sorted(_BAND_RANK)}."
        ) from None
    return band_rank >= _BAND_RANK[config.options_min_band]


__all__ = [
    "BAND_CRITICAL",
    "BAND_INFORMATIONAL",
    "BAND_NOTEWORTHY",
    "DEFAULT_FLOOR_CONFIG",
    "DEFAULT_WARN_THRESHOLD_PCT",
    "SOURCE_INTERNAL",
    "SOURCE_RSS",
    "TRIGGER_ALL_CLEAR",
    "TRIGGER_FRESHNESS",
    "TRIGGER_FUND_CLOSURE",
    "TRIGGER_FX",
    "TRIGGER_LIMIT_BREACH",
    "TRIGGER_LIMIT_ESCALATION",
    "TRIGGER_LIQUIDITY",
    "TRIGGER_PRICE",
    "TRIGGER_RSS_CLUSTER",
    "FloorConfig",
    "UrgencyDecision",
    "band_from_final_urgency",
    "clamp_suggestion",
    "derive_trigger_type",
    "explain_urgency",
    "final_urgency",
    "options_allowed",
    "validate_pinned_invariants",
]
