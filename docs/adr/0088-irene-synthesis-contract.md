# ADR-0088: Irene Synthesis Contract — `surface_finding` Schema, Deterministic Urgency Floor, and Bands

- **Status:** Accepted
- **Date:** 2026-07-02
- **Deciders:** PortfoliFLOW project owner
- **Implements roadmap item:** Feature #033 (Decision Console / Irene)
- **Tags:** decision-console, irene, synthesis, function-calling, materiality, urgency, grounding, analytics-purity

---

## Context

This ADR fixes the contract by which Irene turns eligible deltas
(ADR-0087) into surfaced findings, and the deterministic layer that
governs their materiality. It is the home of the materiality judgement.
The delta layer decides *what is worth showing Irene*; this ADR decides
*how it is phrased, how urgent it is, and whether it carries advice*.

Three forces shape the contract.

**Structured output, not free text.** Irene receives a single tool whose
parameters *are* the card schema. Her task is to decide **whether and how
often** to call it. Zero calls = silence; the "nothing material" case
falls out natively (ADR-0086, `tool_choice="auto"`). There is no
free-text path.

**The urgency (materiality) judgement must not be the LLM's to set
alone.** A non-deterministic model would give the same event a 6 one run
and an 8 the next. Yet the model is well-suited to *phrasing* and to
*proposing* a level. The resolution: the model **suggests**, deterministic
rules **decide**. "Why an 8?" must have a rule-based answer.

**Bands, not raw numbers, drive behaviour and UI.** An LLM does not
reliably distinguish 6 from 7, but the behaviourally meaningful unit is
the band (`informational` / `noteworthy` / `critical`). The 1–10 scale is
retained for sorting; the bands are deterministic.

**Grounding.** Numbers come deterministically from `services/analytics/`
(ADR-0013/0045 purity invariant). Irene *interprets* numbers; she never
invents them.

## Decision

### The `surface_finding` tool schema

Irene is given exactly one function-calling tool. Field names are English
(ADR-0008):

- `subject_key` (string, **required**) — the deterministic key
  **assigned** to Irene (ADR-0085/0087), never formed by her. She
  references it; she does not mint it.
- `trigger` (string, required) — short description of what the beat
  observed.
- `finding` (string, required) — the informing statement. **Always
  present.** This is the "inform" half and is never gated.
- `basis` (string, required) — the derivation/grounding: which numbers,
  which source. Enables the card to show the deterministic figure beside
  the narrative.
- `urgency_suggestion` (integer 1–10, required) — Irene's *proposal*.
  Named "suggestion" so that code and audit make explicit that the LLM
  does not have the last word. Overridden/capped by the floor.
- `options` (array, optional) — action options ("advise" half). **Gated
  by band:** discarded below a configured band threshold even if Irene
  fills it. An `informational` card is pure fact, never advice.
- `evidence_refs` (array, optional) — references to `watch_state` entries
  / RSS bucket ids, for the audit trail.

The schema separates *informing* (`finding`, always present) from
*advising* (`options`, urgency-gated), per the design principle that a
level-1 card is fact, not counsel.

### The urgency floor is deterministic post-processing, not a prompt
### constraint

Irene calls `surface_finding` with `urgency_suggestion`. A deterministic
layer then computes the **final** urgency:

- **Floor per trigger type** — e.g. fund closure = 10; limit breach ≥ 7;
  pure information capped low. The floor *raises* to a minimum.
- **Cap per source** — RSS-only findings capped at `informational`
  (ADR-0087); correlation with internal state removes the cap.
- **Falling-edge / all-clear** deterministically capped at `informational`
  (ADR-0087).
- Final urgency = clamp(`urgency_suggestion`, floor, cap). Within the
  allowed band, Irene's suggestion is honoured and she supplies the
  justification.

This is post-processing precisely because a prompt constraint would be
non-deterministic — the very failure mode being avoided. "Why an 8?"
answers as: "limit-breach floor = 7; Irene suggested 8; 8 > 7; final 8."

### Bands are derived deterministically from final urgency

- `informational` / `noteworthy` / `critical` are computed from the final
  urgency by fixed boundaries (configuration), never set by the LLM.
- Bands drive UI treatment and the `options` gate. The 1–10 value is
  retained only for ordering within a band.
- Irene never sees her own resulting band; she only proposes.

### Floor configuration is a calibration interface

Trigger-type floors, source caps, band boundaries, and
`re_trigger_delta` (ADR-0087) live in **Floor Config** — a configuration
table/interface, not hardcoded constants — because materiality is an
ongoing *calibration* concern, not a fixed technical one. The floor logic
is DB-free and must pass `test_analytics_layer_pure.py` if placed under
`services/analytics/`.

### Grounding contract

- The deterministic analytics layer fills the numeric slots consumed by
  `basis`; Irene fills narrative and option selection.
- Irene interprets figures; she must not originate them. The card always
  shows the deterministic number beside any recommendation
  (recommendation never stands without its computed figure — ADR-0089 UI
  ordering).

## Consequences

- Materiality is reproducible and auditable: identical inputs yield an
  identical final urgency and band regardless of LLM variance.
- The "nothing material" case needs no special handling: zero tool calls
  is silence.
- `irene_finding.urgency`/`band` (ADR-0085) store the **final** values;
  `urgency_suggestion` lives in the payload — the discrepancy between
  suggestion and final is itself auditable.
- The floor layer is the primary calibration surface over the coming
  months; treating it as configuration from day one avoids a later
  refactor from constants.
- `run_synthesis` (ADR-0086) must apply the floor **after** collecting
  tool calls and **before** persisting findings.

**Implementation note (2026-07-02) — erratum on the floor's locus.** The
bullet above places the floor in `run_synthesis`; that locus is wrong
against the code. `run_synthesis`
(`services/ai_service_core.py`) returns a `SynthesisResult(tool_calls,
raw_text)` and is **delta-agnostic** by design — it has no knowledge of a
`subject_key`'s trigger type (falling edge, breach, re-trigger) or source
(internal vs RSS-only), which the floor needs for the trigger-type floor
and the source cap. The only place with all three inputs (the model's
`urgency_suggestion`, plus the `trigger_type`/`source` derived from the
eligible findings) is the **beat's persistence loop**
(`services/irene/beat.py:run_beat`, after `run_synthesis` returns and
before `IreneFindingRepository.append`). The floor is therefore applied
**in the beat**, and `run_synthesis` stays a thin, delta-agnostic
transport. The floor logic itself lives in
`services/analytics/irene_floor.py` (pure, purity-guarded) and is called by
the beat with plain arguments. This note is mirrored in the
`services/analytics/irene_floor.py` and `services/irene/beat.py` module
docstrings.

## Alternatives Considered

- **LLM sets final urgency directly.** Rejected: non-deterministic,
  unauditable, unstable across runs.
- **Urgency floor as a prompt instruction.** Rejected: a prompt cannot
  guarantee determinism; the model may ignore or misapply it.
- **Free-text findings parsed post-hoc.** Rejected: brittle extraction;
  the function-calling schema *is* the card and gives silence for free.
- **Bands set by the LLM.** Rejected: models do not reliably distinguish
  adjacent levels; bands must be deterministic to drive behaviour.
- **`options` always present.** Rejected: low-urgency cards must be pure
  fact; ungated advice dilutes the suppression thesis.

## Compliance & Audit Relevance

- **Materiality accountability (MaRisk):** the decision-support urgency is
  a rule-based judgement with a deterministic floor; the human PM retains
  the decision. Irene provides decision support, not regulated
  advice, and low-urgency cards are explicitly fact-only.
- **Explainability (BAIT/VAIT):** every final urgency decomposes into
  suggestion + floor + cap, all recorded; an examiner can be shown why any
  card carries the band it does.
- **Grounding integrity (ADR-0013/0045):** numbers originate in the
  deterministic analytics layer; the LLM interprets but never fabricates
  figures, preserving the analytics-purity invariant.
- **Calibration transparency:** floor config changes are recorded
  configuration, so shifts in materiality behaviour over time are
  attributable rather than hidden in code or model drift.

## Revision History

- 2026-07-02 — Proposed.
- 2026-07-02 — Implemented (Prompt 4). Added the erratum recording that the
  deterministic floor is applied in the beat, not in `run_synthesis`
  (`run_synthesis` is delta-agnostic). Floor logic lives in the
  purity-guarded `services/analytics/irene_floor.py`; the calibration is
  the `FloorConfig` object (aliased `DeltaThresholds`).
- 2026-07-11 — Accepted against the shipped code. Implemented 2026-07-02:
  `services/irene/synthesis_tool.py` (the `surface_finding` schema) and
  `services/irene/beat.py` (deterministic urgency floor and bands).
