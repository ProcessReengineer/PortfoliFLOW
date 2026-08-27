# ADR-0128: Transactions Area — Trade-Ticket Object Model and Record Flow

- **Status:** Proposed
- **Date:** 2026-08-26
- **Deciders:** PortfoliFLOW project owner
- **Implements roadmap item:** #061 — Transactions (modelling and analysis of
  portfolio changes); answers the four questions its Resolution names
- **Supersedes / amends:** extends the investment domain of ADR-0097/0098
  (ledger and materialisation unchanged); honours ADR-0099/0100/0103 (currency,
  explicit FX cash, cash as first-class) and the ADR-0104 §2 overlay contract
  (untouched); coexists with the per-investment ledger CRUD of ADR-0097 §7
- **Companion:** ADR-0129 (provider channel — relay, portal, encryption,
  engagement object). This ADR defines the shared lifecycle; ADR-0129 arms its
  hand-off states.
- **Working document:** `docs/concepts/transactions-record-flow-plan.md`
  (2026-08-26) — the field-by-field flow inventory this ADR decides against
- **Tags:** transactions, trade-ticket, area, schema, cash-settlement, rls,
  four-eyes, provenance

---

## Context

The platform models portfolio *state* and has no first-class notion of a
portfolio *change*. Roadmap #061 names the gap and requires a concept ADR
settling four questions: booked vs. proposed, the boundary against the
ADR-0097 ledger and the ADR-0104 overlay, the owning Area, and what "analyse a
change" concretely resolves to.

Verified facts the decision builds on (concept chat, 2026-08-24/26 snapshots):

- **F-1 — The manual ledger write is single-leg.** `investments_add_position`
  writes one `position_transactions` row on one investment
  (`ingest_origin='manual'`); no cash leg is booked anywhere. After a
  real-world trade, cash correctness depends on the user entering a second,
  unlinked transaction on the cash position by hand.
- **F-2 — Cash is unitised, explicit, per currency** (ADR-0100/0103): price
  ≡ 1.0000, balance = holdings, no implicit residual absorbs proceeds.
- **F-3 — Ledger constraints** (ADR-0097 §2): CHECK-enforced sign and price
  rules, currency equality, one `opening` per investment,
  `ingest_origin IN ('excel','live','manual')`, holdings as cumulative signed
  sum, `NonNegativeHoldingsError` on oversell.
- **F-4 — The overlay executor is pure and reusable** (ADR-0104 §2):
  `insert_transaction` is `frames → frames` with settle-against-cash
  semantics and by contract never writes the book.
- **F-5 — Reported investments have no units**: disposals and acquisitions on
  them are NAV/cashflow events.

The strategic context is recorded in ADR-0107: the original Execution-Network
brainstorm was cut to the case workspace; the provider-directory half stayed a
dormant concept, and the red line stands — PortfoliFLOW remains a software
provider, never a broker or advisor. This ADR builds the record layer that is
valuable standalone and that the ADR-0129 channel later feeds; nothing in it
touches the red line.

Operator decisions fixed in the concept chat (D-1…D-5) are recorded in the
working document; the load-bearing ones for this ADR: negative cash **warns,
never blocks** (D-2); the four-eyes seam exists from the first migration with
enforcement deferred (D-4); advisory contact is **not** a transaction (D-5,
object split recorded here, design in ADR-0129).

## Decision

### 1. The trade ticket — one object, one state machine

A new tenant-scoped, RLS-protected table **`trade_tickets`** records one
intended or recorded portfolio change per row. *Booked* and *proposed* are not
two object kinds but two stations of one lifecycle — the realised-vs-intended
comparison is precisely the analytical value, and splitting the object would
turn it into a join problem.

Naming: the object is a **trade ticket** (institutional vocabulary), avoiding
the collision of "transaction" with `position_transactions` and with database
transactions. The Area label stays **Transactions**.

Columns (shape as in the working document §1.1): `kind`
(`'order' | 'commitment' | 'secondary'`, CHECK), `direction`
(`'buy' | 'sell'`), `status` (§3), `investment_id` (nullable while a
new-instrument draft is mid-wizard), `trade_date`, `settlement_date`
(informational in v1), `units`, `price_per_unit`, `gross_amount`, `fees`,
`taxes`, `net_amount`, `currency`, `commitment_amount`, `note`, `source`,
`case_id` (nullable — the Watch Desk → Case → Transactions provenance chain),
`proposed_by`, `approved_by` (nullable), timestamps. Tenant denormalisation
and RLS follow the ADR-0035 §3 / ADR-0078 pattern; TEXT + CHECK, no SQL
enums (b019/b020 precedent).

Advisory contact is **not** a `kind`: it has no book effect and no settlement.
The invariant "every booked ticket's effects were emitted at booking time"
stays sharp; the *engagement* object is ADR-0129's.

### 2. `trade_ticket_effects` — emission linkage, one-way dependency

The ticket records what it booked; **the ledger stays ignorant of the layer
above it**. No new column on `position_transactions`, `investment_cashflows`,
or `investment_navs`. A second table `trade_ticket_effects`
(`ticket_id`, `effect_type IN ('position_txn','cashflow','nav',
'investment_update')`, `effect_id`) enumerates the emitted rows, making
effects reversible (§6) and provenance machine-readable.

Consequently (**Q-1 decided**): emitted ledger rows reuse
`ingest_origin='manual'`. The ADR-0092 triple stays intact across all
write-path families; `trade_ticket_effects` is the authoritative linkage, and
the human-readable `source` field on emitted rows carries the ticket id.

### 3. Lifecycle

`status IN ('draft','proposed','approved','sent','acknowledged','executed',
'booked','cancelled')` — **the full vocabulary is CHECK-defined from day one;
`sent`, `acknowledged`, `executed` are unreachable in v1** (no transition
writes them). ADR-0129 arms them; a provider confirmation (`executed`)
pre-fills the booking step and lands in `booked` through exactly this ADR's
machinery — the channel is strictly a front-end to the record flow.

- `draft` → assembling, never visible to analysis;
- `proposed` → complete and validated, carries `proposed_by`;
- `approved` → the decision of record, carries `approved_by`;
- `booked` → effects emitted atomically with the status flip;
- `cancelled` → terminal.

**Q-6 decided:** in v1 the "Book now" gesture traverses
`proposed → approved → booked` implicitly, writing both actor columns
(`approved_by = proposed_by` permitted per D-4). The distinct approval gesture
arrives with four-eyes enforcement — a later, tenant-scoped setting; the
columns and transitions exist now so that enforcement is a rule change, not a
migration.

### 4. Record flows and emissions

The six flows of the working document §2 are adopted as specified there
(U-SELL, U-BUY, U-NEW, R-SEC-SELL, R-COMMIT, R-SEC-BUY), including the
validation matrix (§3 there) under the block-vs-warn split of D-2. Decisions
of record lifted out of it:

- **Two-leg atomic settlement** is the core mechanic: a unitised buy/sell
  emits the instrument leg *and* the cash leg (price 1.0000 on the cash
  position of the instrument's currency) in one DB transaction. A missing
  cash position is offered for inline creation, never silently converted
  (ADR-0099 conversion stays at the reporting seam).
- **R-1:** a first purchase of a new instrument is a `buy`, not an `opening`.
  `opening` remains reserved for pre-existing stock of undocumented origin
  (Excel synthesis, onboarding); the one-opening index is untouched.
- **R-2:** partial secondary sales are out of v1 — proportional restatement of
  NAV *and* unfunded commitment reaches into the plan world; named successor.
- **R-3:** a commitment ticket books once (investment + `commitment_amount`,
  no cash leg); capital calls remain ordinary cashflows and are not ticket
  effects.
- **Q-2 decided:** a negative cash balance is entirely permitted. The
  mechanism is a cash-aware guard: `NonNegativeHoldingsError` is not raised
  for investments of `investment_type='cash'` when the write originates from
  the ticket emission path (explicit capability flag on the service API,
  default off); the instrument leg keeps the guard unconditionally. Beyond
  the one-time warning at booking, a negative balance is a **surfaced state**:
  a persistent indicator marks the affected cash position **until the balance
  returns to ≥ 0** (resolution needs no acknowledgement gesture — the state
  clears itself with the book). Candidate surfaces: the cash position's
  detail view and a banner in the Transactions area; the exact placement is
  fixed at the mockup checkpoint, not in this ADR.
- **Q-3 decided:** R-SEC-SELL proceeds book as `flow_type='distribution'`
  (`flow_kind='actual'`): economically a realisation event, so DPI/TVPI stay
  truthful by construction. A dedicated `sale_proceeds` flow type is a named
  successor for when attribution wants to distinguish exit proceeds from GP
  distributions; provenance is already exact via `trade_ticket_effects`.
- **Q-4 decided:** the price-plausibility warning uses a fixed constant
  (5 % deviation from the nearest `instrument_prices` row) in v1 — no
  coupling to the watchpoint machinery, and never a block: the stored price
  may simply be stale, and the user's execution price is the better fact.
- Trade-date booking for both legs (`settlement_date` informational);
  valuta-accurate cash booking is a named successor (working doc §4.1).
- Backdated bookings trigger the same computed-NAV re-materialisation seam
  the existing write path uses (ADR-0098), for both affected investments —
  one trigger path, verified and reused at implementation time, never a
  second one.

### 5. Boundaries (the #061 questions, answered)

- **Against the ADR-0097 ledger:** distinct object *above* the ledger with an
  emission relationship; the ledger remains the single source of truth for
  unit counts and gains no knowledge of tickets.
- **Against the ADR-0104 overlay:** untouched in both directions. The
  pre-trade impact preview feeds a `proposed` ticket read-only through the
  pure overlay executor and the existing `limit_coverage` machinery (SAA
  drift, limit headroom, liquidity, FX exposure); nothing is persisted, no
  overlay ever writes the book. A later "promote scenario to ticket"
  affordance is a named successor, not part of v1.
- **"Analyse a change" resolves to:** pre-trade impact preview at `proposed`
  (v1 for `order` kinds; fast-follow for reported kinds, whose preview is a
  NAV/cash restatement), and post-trade realised-vs-intended comparison over
  the ticket's captured intent vs. its emitted effects (surface staged after
  the record flow is in use).
- **Existing CRUD coexists:** in-kind `transfer`s, ledger corrections and
  Excel restatements stay on the per-investment CRUD. Cash-to-cash FX
  conversions are a named successor (entangled with the ADR-0099 seam).

### 6. Cancellation and correction

Before `booked`: status flip (a U-NEW draft additionally removes an orphaned
investment shell only if otherwise empty). After `booked`: **reversal, not
mutation** — the enumerated effects are deleted in one DB transaction and the
ticket moves to `cancelled` with a reason note; blocked if any effect row was
modified or consumed since emission, in which case correction happens through
the CRUD and the ticket is annotated. Corrections are cancel + re-enter; no
in-place restatement of a booked ticket.

### 7. The ninth Area

**Transactions** joins the sidebar between Cases and Admin, completing the
provenance chain the ADR-0122 order narrates: the Watch Desk raises the
question, a Case carries it to a documented decision, a Transaction executes
it. Sections in v1: **New transaction** (the flows), **Blotter**
(draft/proposed/approved), **History** (booked/cancelled, filterable).
Mutations are owner-gated like the investment CRUD. The eight→nine
documentation reconciliation (CLAUDE.md, `docs/architecture.md`, ADR-0084
glossary) runs **with the implementation, not before it** (ADR-0107
precedent).

## Alternatives considered

- **Reuse `position_transactions` with a status column** — rejected: the
  ledger is a valuation construct whose rows are facts; a lifecycle on it
  would leak intent states into holdings derivation and break the ADR-0097
  determinism contract.
- **Two objects (proposal + booking)** — rejected: the realised-vs-intended
  link is the analytical point; one state machine keeps it structural.
- **A `transactions` section inside Front Office or a case type inside
  Cases** — rejected: the surface set (flows, blotter, history, later the
  channel) outgrows a section, and a case is a question, not an order;
  cases *link* tickets instead.
- **Extending the ingest-origin triple with `'transaction'`** — rejected
  (Q-1): honest but touches every CHECK across the write-path families and
  ADR-0092's uniformity claim, for provenance the effects table already
  carries authoritatively.
- **Blocking negative cash** — rejected by operator decision D-2: the book
  often trails reality; professional users are warned, not stopped.

## Consequences

**Positive.** The single-leg cash gap (F-1) closes; every recorded trade
settles atomically against cash. The decision trail Watch Desk → Case →
Ticket → emitted bookings is machine-readable end to end — a genuine
differentiator for the regulated target audience. The channel (ADR-0129)
snaps onto defined, tested lifecycle states without rework.

**Negative / accepted.** A ninth Area grows the IA again; the reconciliation
cost is known and bounded. The reversal semantics (§6) add a class of
integrity checks the CRUD never needed. `distribution` temporarily carries
secondary-exit proceeds (Q-3) until the successor flow type exists.

**Process (binding for the implementation track).** Implementation proceeds
in operator-gated sub-strands with **deliberate pause points**: each surface
(wizard flows, blotter, preview) is discussed or mocked up before it is
built. The pace trade-off is accepted in exchange for hitting the target
picture precisely — this feature is central to market acceptance and to the
monetisation path, and the operator retains tight control of its development
process. The implementation kickoff produces a handover document defining the
sub-strands; each sub-strand is one Claude Code prompt with verify-first
phase, stop-and-report gates, and no git operations.

**Commissions (recorded, not designed):** partial secondary sales (R-2);
`sale_proceeds` flow type (Q-3); valuta-accurate cash booking (§4);
cash-to-cash FX conversion tickets (§5); four-eyes enforcement as a
tenant-scoped setting (§3); "promote scenario to ticket" (§5); post-trade
TCA over the captured fee fields.

## Compliance / verification

- Migration number claimed at implementation time (head `b033` at drafting);
  RLS tested under the unprivileged `portfoliflow_app` role for both new
  tables.
- Regression: an emission test asserts two-leg atomicity (both legs or
  neither); a reversal test asserts effect enumeration and the
  modified-effect block; the ADR-0104 purity guard extends over any new
  preview wiring (nothing under `services/analytics/` gains DB/FastAPI
  imports).
- `tests/regression/test_section_catalogue_matches_body_partials.py` covers
  the new area's catalogue; the eight→nine reconciliation lands with the
  implementation commit.
- Roadmap: #061 gains this ADR reference and moves `open` →
  `in-progress` at implementation kickoff; the commissions above are raised
  as items only when scheduled (featuritis discipline).
