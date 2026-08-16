# ADR-0105: Takahashi–Alexander Pacing Profiles — Ephemeral Generation for Plan-less Capital-Account Funds

- **Status:** Accepted
- **Date:** 2026-07-14
- **Deciders:** PortfoliFLOW project owner
- **Tags:** planning-desk, pacing, takahashi-alexander, capital-account, plan-world, pure-engine, phase-8
- **Depends on:** ADR-0104 (overlay contract, `repace_flows`, plan-world baseline), ADR-0103 (cash plan path, settle-against-cash), ADR-0082 (archetype resolver), ADR-0060 (plan/actual carry-forward)
- **Commissioned by:** ADR-0104 ("ADR-D"); roadmap #023 (TA slice only — the forward limit forecast remains #023's open remainder)
- **Context record:** `docs/handover/strand-2-adr-0104-closure.md` (esp. §3.15/§9.2), operator decisions E1–E4 of 2026-07-14

---

## Context

The Planning Desk's pacing rows (ADR-0104 §2, D18) render **disabled**
for any capital-account fund without remaining plan flows — visible,
honest, and useless. The manager plan is the profile the slider
time-scales; where none exists, nothing can be paced. D18 already
names the resolution: the Takahashi–Alexander model is "the generator
for missing plans" — and, explicitly, **never calibrated to reproduce
existing plans**.

Two operator decisions of 2026-07-14 shape this ADR: TA profiles are
**ephemeral** — generated at the plan-world assembly seam, visible
only where the plan world is consumed, never written to the book
(E2) — and TA parameters are **code defaults** per capital-account
investment type, no schema, no per-investment tuning in v1 (E3).
A third (E4) is recorded for coherence: `repace_flows` moves flows,
not plan-NAV paths; this ADR keeps the same posture for generated
profiles (§5).

## Decision

### 1. A pure, import-pure TA module

A new module (working name `ta_profile`; final home decided in the
implementation strand together with the closure-§3.7 package
re-evaluation — binding constraint: **import-pure** like the existing
`services/investments` computation submodules, no DB, no web, no Qt)
provides one entry point:

```
generate_remaining_profile(
    *, commitment, called_to_date, current_nav,
    t0, investment_type, currency, periodisation,
) -> list[GeneratedPlanFlow]
```

Deterministic: identical inputs yield identical output (golden-value
worked-example tests are part of the definition of done).

### 2. The model — classic deterministic Takahashi–Alexander

Annual model periods from `t₀` over the remaining lifetime `L_rem`,
mapped onto the requested periodisation:

- **Contributions:** `C_t = RC_t × (commitment − called_cum_t)` — the
  rate-of-contribution schedule `RC_t` applied to the *remaining*
  unfunded balance, so mid-life funds are picked up where they stand
  (the analogue of `repace_flows`' remaining-profile semantics).
- **NAV recursion (internal only, §5):**
  `NAV_t = NAV_{t−1} × (1 + G) + C_t − D_t`, seeded with the last
  actual NAV (`current_nav`).
- **Distributions:** `D_t = d_t × NAV_t × (1 + G)` with the bow rate
  `d_t = (t / L)^B`; a terminal distribution liquidates the residual
  NAV at `L`.
- Flows are emitted signed in the fund's position currency and settle
  against the cash path of that currency exactly like manager-plan
  flows (ADR-0103 §6 / closure §2.5 — no new settlement rule).
- **Deterministic, single path.** No Monte-Carlo, no scenario
  dependence, no stochastic parameters — the #023 roadmap question
  "deterministic first vs. Monte-Carlo later" is answered
  *deterministic* for this slice; distributions of outcomes are ADR-E+
  territory.

### 3. Parameters — code defaults per capital-account type (E3)

One constants module carries, per capital-account `investment_type`
(`private_equity`, `private_debt`, `real_estate`, `infra_equity`):
the `RC_t` schedule, growth `G`, bow `B`, and lifetime `L`. Values are
fixed in the implementation strand from standard published TA
parameterisations, cited in the module docstring; they are
deliberately coarse. No schema, no tenant/investment overrides, no
tuning UI in v1 — a future per-investment governance is a successor
ADR with a migration, not a quiet extension. `asset_class` refinement
within an archetype (ADR-0082) is likewise deferred.

### 4. Ephemeral integration at the plan-world seam (E2)

- The plan-world assembly (`plan_world.py`) generates a TA profile for
  exactly the funds the pacing surface reports as un-paceable — **the
  same predicate, imported, never restated** (a capital-account fund
  with no remaining non-exempt plan flows after t₀).
- Generated flows enter the assembled frames **labelled**
  (`profile_source='ta'` at the frame/DTO level); every consuming
  surface shows the badge ("TA-generated profile"). Funds with
  manager plans are never touched (D18's never-calibrate rule holds
  by construction: the generator only runs where there is nothing to
  calibrate to).
- **Nothing is written.** No `investment_cashflows` rows, no
  `investment_navs` rows, no `source` marker claimed — the Strand-1
  §2.6 disjointness registry is deliberately *not* extended, because
  there is no second system writer. The book's plan world remains
  manager-plan-only; coverage, limits, Irene, and every non-Planning-
  Desk consumer see no TA data. Materialisation (with its own source
  marker and its own ADR) is the named successor if the forward limit
  forecast (#023 remainder) is ever to see TA paths.
- Inputs come from the book at the seam: `commitment` from the
  investment row, `called_to_date` via the existing called-amounts
  read (actual `capital_call` flows), `current_nav` as the last
  actual NAV, `t₀` as the assembly's book-now seam.

### 5. Pacing activation — and what deliberately does not move

- Pacing rows for TA-profiled funds **enable**: mid-position = the
  generated profile exactly (the same bit-identity anchor as for
  manager plans, now over generated frames); `repace_flows` applies
  unchanged — it time-scales whatever remaining profile the frames
  carry, indifferent to `profile_source`.
- **The fund's plan-NAV path stays ADR-0060 carry-forward.** The §2
  NAV recursion is model machinery for sizing distributions, **not an
  output**: v1 surfaces TA *flows* only (cash lens, pacing,
  settle-against-cash), never a TA NAV path. This is the E4 posture
  applied consistently — re-pacing moves flows without asserting a
  NAV consequence, and generation asserts no NAV path either. A
  platform-asserted NAV trajectory (J-curve synthesis) would be a
  material modelling claim and is successor-ADR territory, alongside
  the repace-NAV question (closure §4.4, §9.4 — documented, unchanged).

### 6. Invariants (all regression-tested)

- Investor-flow exemption: the generator emits only plan
  `capital_call`/`distribution` flows; `OVERLAY_EXEMPT_FLOW_TYPES` is
  imported where filtering occurs, never restated.
- Purity: the TA module joins an import-purity guard (pattern of the
  existing guards).
- Determinism: golden worked examples per capital-account type.
- Book silence: assembling a plan world with TA-profiled funds
  performs zero writes (covered structurally by the seam's read-only
  repositories; asserted once explicitly).
- Non-interference: a fund **with** remaining plan flows produces
  byte-identical frames whether the TA module exists or not.

## Rationale

- **Ephemeral-first keeps the claim honest.** A materialised TA path
  is the platform asserting "this is the plan"; an ephemeral, badged
  profile is the platform saying "absent a plan, a standard model
  suggests this shape" — which is exactly the Planning Desk's
  epistemic register, and it costs no blast radius outside it.
- **Flows-only output keeps three decisions consistent** (E4, §5,
  closure §4.4): nothing in the system currently moves or invents
  plan-NAV paths; TA joining at the flow level means the chart pair,
  coverage deltas, and cash lens all stay explainable by one rule.
- **Code defaults are the v1-honest governance.** Any richer
  parameter home needs schema, provenance, and UI — none of which the
  demo path needs while the demo tenant has manager plans everywhere
  (closure §9.2); the constants module is one file to revisit.
- **The same-predicate rule** (§4) prevents the classic drift bug:
  the pacing surface saying "un-paceable" while the assembly quietly
  generates, or vice versa.

## Alternatives Considered

- **Materialised TA plan flows (`source='computed:ta-profile'`):**
  Rejected for v1 (E2) — extends the plan world for every consumer
  (coverage plan horizon, Irene) and makes a modelling claim in the
  book; named successor if #023's forward forecast needs it.
- **Per-investment / per-tenant parameters:** Rejected for v1 (E3) —
  schema + governance + UI for tuning nobody has asked to do yet.
- **Surfacing the TA NAV path:** Rejected (§5) — asserts a J-curve
  the platform cannot source; inconsistent with E4.
- **Monte-Carlo / stochastic pacing:** Rejected for this slice —
  deterministic-first per the #023 roadmap question; distributions of
  outcomes belong with scenario regimes (ADR-E+).
- **Calibrating TA to existing manager plans (hybrid smoothing):**
  Rejected — D18 forbids it explicitly; plans are truth, TA is
  fallback.
- **A Yale/alternative pacing model:** Rejected — TA is the
  documented industry baseline, parameter-light, and sufficient for a
  fallback generator; model pluralism without a consumer is
  featuritis.

## Consequences

### Positive

- Every capital-account fund is paceable; the disabled-row state
  remains only for genuinely un-modellable cases (e.g. missing
  commitment — surfaced as such).
- Zero book impact, zero migrations, zero new writers; the overlay
  and settlement machinery is reused unchanged.
- The #023 remainder (forward limit forecast) gains a ready pure
  engine to consume later, behind its own ADR.

### Negative / cost

- Two visually similar profiles with different epistemic status live
  on one surface — mitigated by the mandatory badge, but a user can
  still over-read a TA path; the docstring and UI copy must stay
  blunt ("standard model, not a plan").
- Coarse defaults will be wrong for atypical funds (evergreen,
  credit-heavy recycling); v1 accepts this visibly rather than
  hiding it.
- The demo tenant exercises the disabled→enabled transition only via
  synthetic fixtures (closure §9.2) — the feature ships demo-dark
  until a plan-less fund exists in demo data.

### Operator action required

- Accept this ADR; register in `docs/adr/README.md`; commit. The
  Strand-3+4 kickoff (produced next) embeds it as Phase-B
  specification.
- Optionally: add one plan-less synthetic fund to a future workbook
  version if the TA path should be demo-visible (not required for
  #049/#034).
