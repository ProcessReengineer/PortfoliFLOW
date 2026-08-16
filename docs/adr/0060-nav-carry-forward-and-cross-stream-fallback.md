# ADR-0060: NAV Carry-Forward with Cross-Stream Fallback in the Limit-Coverage Engine

- **Status:** Accepted
- **Date:** 2026-05-21
- **Deciders:** PortfoliFLOW project owner
- **Tags:** engine-contract, limits, nav, anlagegrenzen, phase-7

---

## Context

ADR-0043 (Investment Domain Schema and Excel Transformation Pathway)
introduces the parallel NAV streams with
``UNIQUE(investment_id, as_of_date, nav_kind)`` and ``nav_kind IN
('actual', 'plan')``. The limit-coverage engine introduced by
ADR-0055 / ADR-0056 reads from the actual stream up to a global
``cut_over`` date and from the plan stream beyond. Its original
semantics required an **exact-match entry** in the selected stream
at every evaluation date ``t``; any missing entry raised
``CoverageInputOutOfRange``.

In practice this contract is too strict for the realistic
Versorgungswerks data profile (and the V21 test workbook that mirrors
it). That profile does **not** carry plan NAVs for liquid
investments — listed-equity mandates, fixed-income mandates, and
similar daily-valued positions are not forecasted. Plan NAVs exist
exclusively for illiquid investments (private equity, real estate)
where quarterly reporting lag and MOIC paths make forecasting
genuinely useful.

The original engine contract therefore made the coverage section at
``/back-office#limits`` unusable for any evaluation window extending
beyond ``cut_over``: a single investment without plan NAVs aborted
the whole computation with
``CoverageInputOutOfRange: Investment ...: no plan NAV at <date>``.

The roadmap's "Forecasting and Benchmarks" feature set will
introduce explicit plan-NAV generation for all investments —
including a documented forecast methodology for liquid positions
(forward curves, risk-free-rate paths). Until that feature lands the
engine must define a sensible fallback that lets the surface render
against the realistic data profile.

A secondary motivation is internal consistency. The engine's
``_resolve_aum`` function already implements carry-forward over the
AUM time series (ADR-0055 §"Missing days"). Applying the same
discipline to the NAV streams aligns the two sister-functions in the
same module.

---

## Decision

The engine's NAV resolution rule is replaced by the following
algorithm. For evaluation date ``t`` and investment ``inv``:

1. **Stream preference by cut-over.** For ``t <= cut_over`` the
   **actual** stream is preferred and ``plan`` is the secondary; for
   ``t > cut_over`` the **plan** stream is preferred and ``actual`` is
   the secondary.

2. **Carry-forward within the preferred stream.** If the preferred
   stream contains any entry at or before ``t``, the engine uses the
   value at the latest such date.

3. **Cross-stream fallback.** If the preferred stream has no entry at
   or before ``t``, the engine consults the secondary stream under the
   same carry-forward rule.

4. **Hard error.** ``CoverageInputOutOfRange`` is raised only when
   **both** streams have no entry at or before ``t``. This condition
   means the investment did not exist at ``t``; there is no
   historical value to carry forward.

5. **No interpolation, no zero-extrapolation.** The rule never
   averages adjacent entries; liquidations remain expressed by an
   explicit ``nav_value == 0`` entry per ADR-0043 §1 and propagate
   faithfully through carry-forward.

The engine's function signature is unchanged. ``_resolve_nav`` still
takes ``t``, ``investment_id``, ``cut_over``,
``actual_nav_lookup``, ``plan_nav_lookup`` and returns ``Decimal``.
The change is internal; downstream consumers
(``LimitsCoverageService``, ``web/routes/limits.py``, the section
templates) need no modification.

---

## Rationale

### Why primary-first ordering rather than newest-across-streams

Primary-first respects the cut-over intention: beyond ``cut_over``
the plan world is the authoritative view, and the engine should
favour a plan-side value over an older actual-side value when both
are present. Sorting by recency across both streams would let an
older actual entry shadow a newer plan entry, contradicting the
cut-over semantics that ADR-0055 / ADR-0056 wrote into the engine.

### Why carry-forward rather than exact-match-or-miss

Carry-forward aligns the NAV behaviour with the existing AUM
behaviour in ``_resolve_aum`` — the engine now applies the same
"use the latest observation at or before ``t``" rule to both
denominator (AUM) and numerator (per-investment NAVs). This
internal consistency is small but worth having: any future reader
or maintainer learns one rule, not two.

### Why cross-stream fallback rather than coercing imports to fill plan-NAVs

The realistic data profile does not produce plan NAVs for liquid
investments and there is no operationally sensible default value to
synthesise at import time. Forcing the importer to fabricate plan
NAVs would push a forecasting decision into a layer that has no
forecasting context. Cross-stream fallback puts the decision where
it belongs: the engine acknowledges "no forecast available, use the
last known actual value as the best approximation."

### Why raise only when both streams are dry

The remaining failure mode is operationally meaningful: an evaluation
date earlier than the investment's first observation in either
stream means the investment did not yet exist at ``t``. The engine
has no historical value to carry forward, so raising
``CoverageInputOutOfRange`` is the honest answer. This is distinct
from a forgotten NAV import: a forgotten import surfaces as a longer
carry-forward window, not as a missing value.

---

## Consequences

### Positive

- The coverage surface becomes usable for the realistic V21 data
  profile out of the box, without requiring synthetic plan NAVs for
  liquid investments.
- NAV semantics align with the existing AUM carry-forward — the
  engine becomes internally consistent. A maintainer reading
  ``limit_coverage.py`` learns one resolution rule that applies to
  both the denominator (AUM) and the numerator (per-investment NAVs).
- The contract is robust against incomplete plan-NAV streams: a
  single missed monthly observation no longer aborts the entire
  computation. Renderers can present a continuous picture.

### Negative / Trade-offs

- A liquid investment for which no plan NAV is ever produced will
  carry its last actual NAV indefinitely into the forecast. This is
  an explicit, conservative approximation — not a model of the
  investment's true future value. Operators viewing far-out
  forecasts must understand this. **Mitigation:** a V2 follow-up
  will surface the applied carry-forward in the section's status
  table (e.g. an info badge on classes whose contributing NAVs were
  last refreshed > N days before the evaluation date).
- The rule slightly weakens the engine's strictness: a forgotten
  plan-NAV import no longer fails loudly. **Mitigation:** the
  Forecasting/Benchmarks feature will add a coverage-completeness
  signal that surfaces the share of carry-forward use per class.

### Neutral

- The engine signature is unchanged; downstream services
  (``LimitsCoverageService``), web routes
  (``web/routes/limits.py``), and section templates need no
  modifications.
- The ``UNALLOCATED`` bucket — investments without an asset-class
  for the active family — runs through the same ``_resolve_nav``
  path and inherits the new behaviour without a special case.

---

## Alternatives Considered

- **Strict exact-match (status quo).** Rejected because it makes the
  coverage surface unusable for the realistic Versorgungswerks data
  profile. A single liquid investment without plan NAVs aborts the
  computation for the entire evaluation window.

- **Filling synthetic plan NAVs at import time.** Rejected because
  it hides the methodological question (how do you forecast a liquid
  position?) behind a default rule that lives in import logic, which
  is the wrong architectural place. Forecasting belongs to a
  forecasting feature, not an importer.

- **Forward-projection (extrapolate beyond the last known value via
  some growth model).** Rejected because it builds a forecast inside
  the limit-coverage engine, which is out of scope. Forecasting
  belongs to the Forecasting/Benchmarks feature set, where its
  methodology can be debated on its own terms.

- **Interpolation between adjacent entries.** Rejected because it
  silently fabricates values not present in the data. Carry-forward
  is the more honest approximation: it never invents a value the
  source didn't carry.

- **Newest-across-streams ordering (ignore the cut-over preference
  and just pick the chronologically latest entry across both
  streams).** Rejected because it contradicts the cut-over semantics
  established by ADR-0055 / ADR-0056 — beyond ``cut_over`` the plan
  view is the authoritative one and should win over an older actual
  entry.

---

## Related ADRs

- ADR-0043 — Investment Domain Schema and Excel Transformation
  Pathway (defines the ``nav_kind`` discriminator and the
  ``actual``/``plan`` parallelism that this ADR refines).
- ADR-0055 — Cash as Residual in AUM Coverage Engine (defines the
  AUM-side carry-forward this ADR aligns NAV semantics with).
- ADR-0056 — Limit-Set Historisierung via ``effective_from``
  (engine contract; unchanged by this ADR).
- ADR-0057 — AnlV classification as 1:1 investment attribute
  (engine contract; unchanged by this ADR).

---

## Implementation pointers

- ``services/analytics/limit_coverage.py``:
  ``_resolve_nav`` rewritten per the algorithm above;
  ``_latest_at_or_before`` introduced as a small private helper
  shared with the primary/secondary lookups. Module docstring,
  ``compute_coverage`` docstring, and the
  ``CoverageInputOutOfRange`` docstring in ``core/exceptions.py``
  updated to reflect the new contract.
- ``tests/services/analytics/test_limit_coverage.py``: the two
  existing tests pinning the old strict semantics are adapted (both
  streams must be empty at or before ``t`` for the error to fire);
  new tests cover the carry-forward rule, the cross-stream fallback
  in both directions, the boundary case at ``cut_over``, the
  liquidation pass-through, and the both-streams-empty hard-error
  path.
- The naive ``max``-over-list-comprehension lookup is O(n) per call.
  At the V1 grid scale (monthly evaluation × daily NAVs across the
  V21 portfolio) this remains sub-second. Switching to ``bisect``
  over presorted streams is a follow-up if a future scale demands
  it.
