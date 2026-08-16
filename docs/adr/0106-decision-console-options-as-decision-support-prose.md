# ADR-0106: Decision Console `options` Rendered as Decision-Support Prose

- **Status:** Accepted
- **Date:** 2026-07-20
- **Deciders:** PortfoliFLOW project owner
- **Implements roadmap item:** Decision Console UI Refresh ("One Glass", B-final-v3), prompt DC3
- **Tags:** decision-console, irene, synthesis, options, presentation, prompt-contract, analytics-purity
- **Refines / clarifies:** ADR-0088 (Irene synthesis contract). ADR-0088 remains
  Accepted and unedited; its `options` field stays `array<string>`, optional,
  band-gated. This ADR records a *presentation and prompt* decision layered on
  top of that contract — it changes neither the schema, the storage, nor the
  band gate.
- **Honours:** ADR-0089 (Decision Console briefing UI — the card idiom, calm by
  default), ADR-0087 (delta mechanics — the band that gates `options`),
  ADR-0085 (findings immutability), ADR-0008 (English-only codebase),
  ADR-0013/0045 (analytics purity)

---

## Context

ADR-0088 fixed the `surface_finding` contract. Its `options` field — the
"advise" half, distinct from the always-present `finding` that "informs" — was
specified as an optional `array<string>`, discarded below a configured band
threshold so that a low-urgency card stays pure fact rather than counsel. That
contract is sound and stands.

What ADR-0088 did **not** fix was the *rhetorical form* of `options`. In practice
the array was produced and rendered as a bulleted list of discrete imperatives
("Sell X.", "Reclassify Y."). The Decision Console refresh (B-final-v3) is a
trust intervention: the console must read as decision **support**, not as a
machine issuing orders. A bulleted imperative menu works against that — it reads
as instruction divorced from the figures, and it invites the manager to treat
Irene's list as authoritative rather than as an interpretation to weigh.

The refresh therefore re-tells `options` as a short prose block ("Possible
moves") that interprets the card's computed basis: what the figures imply and
what the realistic moves are, grounded in the numbers the analytics layer
supplied. This is a change to *how the same payload is asked for and shown*, not
to *what the payload is*. Because the resulting synthesis prompt now reads
noticeably differently from the field description quoted in ADR-0088, the "why"
needs a decision of record rather than living only in a commit message — hence
this ADR.

The presentation also introduces an "Open case →" affordance as a **disabled
v2 preview**. Its target — a case workspace — is the separate Execution Network
workstream and is explicitly not built here; the button is inert by design so
that "no-op" is a fact of the DOM, not an unfulfilled promise.

## Decision

### 1. `options` is authored and rendered as decision-support prose

- The `surface_finding` tool's `options` **property description** instructs the
  model to write **1–3 connected sentences** that interpret the computed basis —
  what the figures imply for the manager and what the realistic moves are —
  grounded in the supplied numbers, never inventing quantities. Prose, not a
  bulleted list of imperatives.
- The Decision Console card renders `options` as the **"Possible moves"** block
  (quiet tile, info-blue left edge) between Computed materiality and the
  resolution buttons, as a single paragraph.

### 2. The wire contract is unchanged (ADR-0088 preserved)

- `options` remains `array<string>`, optional, stored in the opaque JSONB
  payload. **No schema change, no migration.**
- The route projection joins the array into one paragraph for display
  (`options_prose`) while still exposing the raw list; a payload of several
  strings — whether the model emits one sentence-string or three, or a legacy
  finding holds terse bullets — joins into a single readable paragraph. The
  join is defensive: a non-conforming model does not break rendering.
- Findings already persisted under ADR-0088 render correctly without
  backfill (immutability honoured, ADR-0085).

### 3. The band gate is untouched

- `options` remains gated by `options_min_band` in the deterministic floor
  (ADR-0087/0088). An informational card reaches the console with `options`
  already dropped by the beat and therefore renders **no** "Possible moves"
  block. The gate is the unobtrusiveness mechanism and is not relocated,
  duplicated, or re-tuned by this decision.

### 4. "Open case →" is a disabled preview only

- The card shows an "Open case →" control as an inert, `disabled` v2 preview
  with no handler, route, or target. The case workspace, case model, and
  provider directory belong to the Execution Network workstream and are out of
  scope here. This ADR commissions nothing in that direction; it only reserves
  the visual affordance.

### 5. What this ADR does not change

- The `finding` field ("inform" half), always present, is unaffected.
- `urgency_suggestion` and the deterministic urgency floor are unaffected: the
  model still proposes, the floor still decides.
- Analytics purity is unaffected: no analytics-layer code participates in this
  change; the prose is authored by the model under a revised description and
  joined in the web layer.

## Consequences

**Positive**

- The console reads as decision support grounded in figures, not as an order
  list — the trust thesis of the refresh (ADR-0089) is served at the card level.
- No migration, no persistence change; the ADR-0088 contract and all existing
  findings remain valid.
- The band gate continues to keep informational cards fact-only, unchanged.

**Negative / risks**

- Prose is softer than a bulleted list and can, if the model underperforms,
  read as vague. Mitigation: the description ties the prose explicitly to the
  computed basis and forbids invented quantities; the grounded figure remains
  visible in Computed materiality directly above, so the prose never stands
  without its numbers.
- The `finding`/`options` boundary is now rhetorically closer (both are prose).
  The distinction is preserved by role and gating — `finding` always present and
  informational; `options` band-gated and advisory — not by list-vs-prose form.

**Neutral**

- A future case workspace (Execution Network) will give "Open case →" a target;
  until then it is a deliberate preview. Enabling it is a separate decision.

## Compliance / verification

- ADR-0088 file unchanged (no edit to an accepted ADR).
- `surface_finding` `options` schema remains `array<string>`; only its
  description and the tool's top-level summary changed.
- Migration head unchanged; nothing added under `services/analytics/`.
- A Briefing test asserts: multiple option strings join into one ordered
  paragraph; an informational card renders no "Possible moves" block; the
  "Open case →" control is `disabled` with no handler or target.
