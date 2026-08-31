# Transactions — Record-Flow Plan (Part a)

**Status:** Concept working document — pre-ADR
**Date:** 2026-08-26
**Roadmap item:** #061 — Transactions (modelling and analysis of portfolio changes)
**Successor documents:** ADR-T1 (Transactions area, object model, record flow — this
document's target), ADR-T2 (provider channel: relay, provider portal, encryption —
separate concept ADR, separate maturity)
**Related:** ADR-0097/0098 (position model & materialisation), ADR-0099/0100/0103
(functional currency, explicit FX cash, cash as first-class asset class), ADR-0104 §2
(overlay contract — untouched), ADR-0107 (Cases; the recorded regulatory red line),
ADR-0092 (ingest-origin triple)

---

## 0. Purpose and boundaries of this document

This document walks every record flow of the Transactions area's **part (a)** —
processing a portfolio change that happened *outside* PortfoliFLOW — field by field
and booking by booking, so that the concept ADR can be written against a complete
information inventory rather than discovering gaps during implementation.

It also fixes the object model far enough that part (b) — the order flow toward
suggested providers — snaps onto the same object later without rework. Part (b)'s
*channel* (central relay, provider portal, end-to-end encryption) is deliberately
**not** designed here; it belongs to ADR-T2. Part (b) appears here only as a set of
lifecycle states that are defined but unused in v1.

### Decisions already fixed by the operator (2026-08-26 concept chat)

These are inputs to this document, not open questions:

- **D-1 — Channel target picture:** central relay on portfoliflow.com plus a
  provider **web portal** (no native app), payloads end-to-end encrypted with a
  per-provider public key, e-mail used as **notification only**, never as
  transport. Modern encryption primitives (e.g. age / libsodium sealed boxes)
  are preferred over OpenPGP; the "public key per provider" concept stays.
  The centrally hosted provider list must itself be **signed** so key
  substitution via a compromised list fetch is impossible.
- **D-2 — Negative cash warns, never blocks.** The users are professional
  portfolio managers; the platform surfaces the consequence and steps aside.
- **D-3 — Two ADRs.** ADR-T1 (area + object model + record flow) and ADR-T2
  (channel) are separate documents.
- **D-4 — Four-eyes seam from day one.** `proposed_by` and `approved_by` columns
  and the corresponding state transition exist from the first migration;
  v1 permits `proposed_by == approved_by`; enforcement is a later, separate
  feature (likely a tenant-scoped setting).
- **D-5 — Advisory contact is not a transaction.** It is a separate, generic
  *engagement* object (no book effect, no settlement) sharing the provider
  list and the encrypted channel. It is an ADR-T2 concern except for the
  object split, which ADR-T1 records.

### Verified codebase facts this plan builds on

- **F-1 — The manual ledger write is single-leg.** `investments_add_position`
  (web/routes/investments.py) writes one `position_transactions` row on one
  investment with `ingest_origin='manual'`. No cash leg is booked anywhere.
  Cash correctness after a real-world trade currently depends on the user
  manually entering a second transaction on the cash position.
- **F-2 — Cash is unitised, per currency, explicit.** Per ADR-0100/0103, cash
  positions are first-class investments with `valuation_mode='unitised'`,
  price ≡ 1.0000, balance = holdings. A currency without a cash position has
  **no** implicit residual to absorb proceeds.
- **F-3 — Ledger constraints.** `position_transactions` CHECK-enforces sign
  rules (`buy` > 0, `sell` < 0), price presence for `buy`/`sell`, currency
  equality with the investment (route-level 400), at most one `opening` per
  investment (partial unique index), and the ingest-origin triple
  `('excel','live','manual')`. Holdings derive as a cumulative signed sum
  ordered `(trade_date, created_at, id)`; `NonNegativeHoldingsError` guards
  oversells.
- **F-4 — The Planning Desk overlay is reusable and untouchable.** The
  `insert_transaction` executor (ADR-0104 §2) is a pure `frames → frames`
  function with settle-against-cash semantics. It may be reused read-only as
  the pre-trade impact preview; it never writes the book, and nothing in this
  plan changes that contract.
- **F-5 — Reported investments have no units.** Private-markets positions carry
  NAVs and cashflows (`investment_cashflows`); disposals and acquisitions on
  them are NAV/cashflow events, not ledger events.

---

## 1. Object model

### 1.1 `transaction` — the intent/record object

One row per intended or recorded portfolio change. **Not** a ledger row; it sits
above the ledger and *emits* booking primitives when it reaches `booked`.

Proposed shape (names indicative; the ADR fixes them):

| Field | Type | Notes |
|---|---|---|
| `id` | UUID | |
| `tenant_id` | UUID | RLS, denormalised per ADR-0035 §3 pattern |
| `kind` | TEXT CHECK | `'order'` \| `'commitment'` \| `'secondary'` |
| `direction` | TEXT CHECK | `'buy'` \| `'sell'` (for `commitment`: always `'buy'` in v1) |
| `status` | TEXT CHECK | see §1.3 |
| `investment_id` | UUID NULL | NULL only while a new-instrument wizard is in `draft` |
| `trade_date` | DATE | statement-day semantics, the booking date of both legs |
| `settlement_date` | DATE NULL | informational in v1 (§4.1) |
| `units` | NUMERIC NULL | unitised kinds; unsigned here, sign applied at emission |
| `price_per_unit` | NUMERIC NULL | execution price, instrument currency |
| `gross_amount` | NUMERIC NULL | derived: units × price; stored for the reported kinds where no units exist |
| `fees` | NUMERIC NULL | transaction costs |
| `taxes` | NUMERIC NULL | optional split from fees; both default 0 |
| `net_amount` | NUMERIC | the settlement cash effect (sign per direction) |
| `currency` | TEXT | must equal the investment's currency (F-3) |
| `commitment_amount` | NUMERIC NULL | `commitment` kind only |
| `note`, `source` | TEXT NULL | free text, mirrors the ledger fields |
| `case_id` | UUID NULL | optional provenance link (Watch Desk → Case → Transaction) |
| `proposed_by` | UUID | D-4 |
| `approved_by` | UUID NULL | D-4; set at the `proposed → approved` transition |
| `created_at`, `updated_at` | TIMESTAMPTZ | |

### 1.2 `transaction_effect` — the emission linkage

The transaction records what it booked; the ledger stays ignorant of the layer
above it (ADR-0001 one-way dependency discipline — **no** new column on
`position_transactions`).

| Field | Notes |
|---|---|
| `transaction_id` | FK to `transaction` |
| `effect_type` | `'position_txn'` \| `'cashflow'` \| `'nav'` \| `'investment_update'` |
| `effect_id` | UUID of the emitted row |

Cancellation (§4.3) resolves through this table: the effects of a transaction
are enumerable, therefore reversible.

### 1.3 Lifecycle

```
draft ──► proposed ──► approved ──► booked
                │           │
                │           ├──► sent ──► acknowledged ──► executed ──► booked   (part b, defined, unused in v1)
                │           │
                ▼           ▼
            cancelled   cancelled
```

- **`draft`** — being assembled; may lack an `investment_id` (new-instrument
  wizard mid-flight). Never visible to analysis.
- **`proposed`** — complete and validated; carries `proposed_by`.
- **`approved`** — the decision of record; carries `approved_by`. v1 allows
  self-approval (D-4). For part (a), `approved` and the booking action are
  one user gesture; the states are still distinct rows in the audit trail.
- **`sent` / `acknowledged` / `executed`** — part (b) states, **defined in the
  CHECK constraint from day one but unreachable in v1** (no transition writes
  them). ADR-T2 arms them. `executed` means the provider confirmation arrived
  with fill data; the booking step then pre-fills from it and lands in
  `booked` through exactly the part-(a) machinery — part (b) is strictly a
  front-end to part (a).
- **`booked`** — the effects are emitted, atomically, in one DB transaction
  together with the status flip. A `commitment` reaches `booked` when the
  investment and commitment are recorded; its capital calls remain ordinary
  cashflows and are *not* effects of the transaction (§3.5).
- **`cancelled`** — terminal. From `booked`, cancellation is a distinct
  reversal operation (§4.3), not a status flip alone.

### 1.4 `engagement` — out of scope here, recorded for the split

Advisory/legal/fund-selection contact (D-5): own object, no `kind`/`units`/
booking machinery, optional `investment_id` and `case_id` references, same
lifecycle prefix (`draft → proposed → approved → sent → …`) minus the booking
tail. Fully specified in ADR-T2; ADR-T1 only records the split and reserves the
name.

---

## 2. The record flows, case by case

Every flow ends in the same atomic emission step; they differ in what must be
collected first and what is emitted. "Block" and "warn" follow D-2: blocks are
reserved for inputs that would corrupt the book's invariants; everything that is
merely *probably unwise* warns.

### 2.1 Flow U-SELL — sell units of an existing unitised investment

*The 200-fund-shares example.*

**Inputs**

| Field | Required | Validation |
|---|---|---|
| Investment | yes | unitised; active |
| Trade date | yes | any date (backdating allowed, §4.2) |
| Units sold | yes | > 0; **block** if > holdings at trade date (F-3 guard) |
| Execution price | yes | > 0; **warn** if it deviates > x % from `instrument_prices` at/near trade date (threshold an ADR detail; suggest 5 %) |
| Fees / taxes | optional | ≥ 0, default 0 |
| Settlement date | optional | informational (§4.1) |
| Note / source | optional | |

**Derived:** gross = units × price; net proceeds = gross − fees − taxes.
**Warn** if net ≤ 0.

**Cash-position resolution:** the cash position in the investment's currency is
looked up. If none exists, the flow offers to create it inline (F-2; the
ADR-0100 explicit-FX principle — never silently convert, conversion lives at
the reporting seam per ADR-0099). Declining aborts the booking.

**Emission (atomic):**

1. `position_transactions`: `sell`, −units, execution price,
   `consideration` = +net, on the fund.
2. `position_transactions`: `buy`, +net units, price 1.0000, on the cash
   position (satisfies the F-3 price-required CHECK).
3. Two `transaction_effect` rows.

Both ledger rows carry `ingest_origin` per Q-1 (§5) and `source` referencing
the transaction id for human-readable provenance.

**Full disposal:** if units sold equal the entire holding, offer (not force)
setting the investment inactive.

### 2.2 Flow U-BUY — buy units of an existing unitised investment

Mirror of U-SELL. Cash leg: `sell`, −(gross + fees + taxes) units on the cash
position. **Warn, never block**, when the cash position would go negative (D-2);
the warning states the resulting balance. A negative balance additionally
becomes a **surfaced state**: a persistent indicator on the affected cash
position remains active until the balance returns to ≥ 0 (self-clearing, no
acknowledgement gesture; exact surfaces fixed at the mockup checkpoint). This requires the emission path to
bypass or sequence around `NonNegativeHoldingsError` **for cash positions
only** — the guard remains fully in force for the instrument leg. (Mechanism —
flag on the write API vs. cash-aware guard — is an ADR-T1 implementation
decision; the *behaviour* is fixed here.)

### 2.3 Flow U-NEW — buy a not-yet-known instrument (wizard)

Prerequisite step before U-BUY: create the investment. Master-data inventory,
drawn from the existing create/identifier/valuation-mode paths:

| Field | Required | Notes |
|---|---|---|
| Name | yes | tenant-unique |
| Investment type | yes | one of the eight CHECK values |
| Asset class | yes | tenant catalogue |
| **AnlV category** | yes for the target audience | regulatory correctness over convenience; the wizard must not let this dangle |
| Currency | yes | becomes the transaction currency |
| ISIN (or other identifier) | strongly recommended | routed through the ADR-0090 FIGI normalisation; enables market data |
| `valuation_mode` | fixed `'unitised'` for this flow | |
| Manager, region, sector/country weights | optional | completable later |

Then U-BUY runs. **The first buy is a `buy`, not an `opening`** (recommendation
R-1): holdings cumulate from zero, and the semantic split stays sharp —
`opening` = pre-existing stock of undocumented origin (Excel synthesis,
onboarding), `buy` = documented acquisition. The one-opening index is untouched.

The wizard's investment creation is itself a `transaction_effect`
(`'investment_update'`) so that cancelling an un-booked draft can clean up an
orphaned investment shell — **only** if the investment has no other data.

### 2.4 Flow R-SEC-SELL — secondary sale of a reported (private-markets) stake

No units exist (F-5). Inputs: investment, trade date, **sale proceeds** (net,
with fees/taxes optionally split out), **fraction sold** (v1: full sale only —
see R-2), settlement date, note.

**Emission (full sale, atomic):**

1. `investment_cashflows`: proceeds as an inflow on the investment (flow-type
   mapping to the existing vocabulary is an ADR detail; it must not collide
   with the `investor_flow` invariants of ADR-0103 §5).
2. `investment_navs`: NAV → 0 at trade date (`'manual'` origin).
3. Investment set inactive.
4. Cash leg: `buy` +net on the cash position, as in U-SELL.
5. `transaction_effect` rows for all of the above.

**R-2 (recommendation):** **partial** secondary sales are explicitly deferred
out of v1. A partial sale must proportionally restate NAV *and* unfunded
commitment (the buyer assumes proportional unfunded), and the unfunded split
interacts with the plan world (ADR-0104 plan flows) — a genuinely separate
design. v1 refuses the fraction field; the ADR names the successor.

### 2.5 Flow R-COMMIT — primary private-markets commitment

Inputs: the U-NEW wizard with `valuation_mode='reported'` (plus vintage year,
commitment amount — both first-class here), trade date = commitment date.

**Emission:** investment created with `commitment_amount`; **no cash leg** —
money moves with the capital calls, which remain ordinary cashflows entered
through the existing paths (or a future statement-ingest) and are *not*
transaction effects. The transaction is `booked` when the commitment is
recorded; it remains the provenance anchor a Case can point at, but it does
not track the drawdown (the Planning Desk and pacing machinery already own
that view).

**R-3 (recommendation):** resist making the commitment transaction a
long-running container for calls. One object, one booking event, sharp
invariant ("every booked transaction's effects were emitted at booking time").

### 2.6 Flow R-SEC-BUY — secondary purchase of a reported stake

U-NEW wizard (`'reported'`), plus: purchase price (net cash out), acquired NAV
(the stake's value at transfer — opening NAV row), assumed unfunded commitment
(→ `commitment_amount`). Emission: investment + opening NAV + cash leg
(`sell` −net on cash) + effects. Purchase price and opening NAV legitimately
differ (secondary discount/premium); both are captured, and the difference is
exactly what post-trade analysis will want.

### 2.7 Out of scope of the transaction object (v1)

In-kind `transfer`s, ledger corrections, and Excel-origin restatements stay on
the existing per-investment CRUD (F-1), which **coexists** with the new area.
FX conversions between cash positions ("sell EUR cash, buy USD cash") are a
named successor — they are a two-cash-leg transaction with an FX rate, close to
this machinery but entangled with ADR-0099's conversion seam; do not bolt them
on in v1.

---

## 3. Validation matrix (consolidated)

| Condition | Behaviour |
|---|---|
| Currency ≠ investment currency | **block** (F-3 CHECK; no silent conversion) |
| Oversell of the instrument leg | **block** (`NonNegativeHoldingsError`) |
| Cash position would go negative (U-BUY, R-SEC-BUY) | **warn** (D-2), show resulting balance; persistent indicator on the cash position until balance ≥ 0 |
| No cash position in the currency | **offer inline creation**; abort if declined |
| Execution price deviates > x % from known price | **warn** |
| Net proceeds ≤ 0 on a sell | **warn** |
| Trade date in the future | **warn** (the book records facts; a future-dated fact is suspicious but a PM may post-date deliberately) |
| Missing AnlV category in U-NEW | **block for the wizard's finish**, not for saving a `draft` |
| Second `opening` | **block** (existing index; unreachable if R-1 holds) |

---

## 4. Cross-cutting decisions

### 4.1 Trade date vs. settlement date

Both are captured on the transaction. **v1 books both legs at `trade_date`**
(consistent with the ledger's statement-day semantics, F-3 ordering); `settlement_date`
is informational. Valuta-accurate cash booking is a named successor — it would
move only the cash leg's date and is a one-line change at the emission point
once wanted.

### 4.2 Backdating and re-materialisation

Backdated bookings are allowed (statement-day model). The emission must trigger
the same computed-NAV re-materialisation the existing write path uses
(ADR-0098) for **both** affected investments — the instrument and the cash
position. *Implementation note: verify at implementation time where the
existing `add_position_transaction` path triggers materialisation and reuse
that seam; do not introduce a second trigger path.*

### 4.3 Cancellation and correction

- Before `booked`: status flip to `cancelled`; a U-NEW draft additionally
  removes an orphaned investment shell (only if otherwise empty).
- After `booked`: **reversal**, not mutation — the effects enumerated in
  `transaction_effect` are deleted in one DB transaction and the transaction
  moves to `cancelled` (with a reason note). Blocked if any effect row has
  been modified or consumed since emission (e.g. the NAV row edited); in that
  case the user corrects through the CRUD, and the transaction is annotated,
  not silently reconciled.
- Corrections = cancel + re-enter. No in-place restatement of a booked
  transaction; the audit trail stays append-friendly.

### 4.4 Pre-trade impact preview

At `proposed`, the surface renders the effect on SAA drift, limit headroom,
liquidity and FX exposure by feeding the transaction read-only through the
pure overlay executor (F-4) and the existing `limit_coverage` machinery.
Nothing is persisted; the ADR-0104 contract is untouched. This is a v1 feature
for `order` kinds and a fast-follow for the reported kinds (whose preview is a
NAV/cash restatement rather than an `insert_transaction`).

### 4.5 Area and roles

Ninth area **Transactions**, sidebar between Cases and Admin (the Watch Desk →
Cases → Transactions provenance chain). Surfaces in v1: **New transaction**
(the flows above), **Blotter** (open: draft/proposed/approved), **History**
(booked/cancelled, filterable). Mutations owner-gated like the investment
CRUD; the four-eyes columns exist per D-4. The eight→nine documentation
reconciliation runs with the implementation, not before it (ADR-0107
precedent).

---

## 5. Open questions for ADR-T1 (decisions of record needed)

- **Q-1 — `ingest_origin` of emitted ledger rows.** Reuse `'manual'` (+
  `source` = transaction id) vs. extend the ADR-0092 triple with a fourth
  value (e.g. `'transaction'`). Extending is honest provenance but touches
  every CHECK across the write-path families and ADR-0092's uniformity claim;
  reusing `'manual'` keeps the triple intact and relies on
  `transaction_effect` for machine-readable provenance. *Tendency: reuse
  `'manual'`; the effects table is the authoritative linkage anyway.*
- **Q-2 — Mechanism for the cash-leg negative-balance allowance** (flag on the
  write API vs. cash-aware guard), per §2.2. Behaviour is fixed (D-2);
  mechanism is not.
- **Q-3 — Flow-type mapping for R-SEC-SELL proceeds** within the existing
  cashflow vocabulary, respecting the ADR-0103 §5 `investor_flow` invariants.
- **Q-4 — Price-deviation warning threshold** (fixed 5 % vs. tenant-calibrated
  via the Watch Desk's watchpoint machinery — *tendency: fixed constant in v1,
  no coupling to watchpoints*).
- **Q-5 — Naming.** `transaction` collides linguistically with
  `position_transactions` and with DB transactions. Candidate names for the
  object: `trade_ticket`, `deal`, `portfolio_transaction`, `txn_intent`. The
  area label stays **Transactions** regardless.
- **Q-6 — Does `approved` require a distinct click in v1** (two gestures:
  approve, then book) or does "Book now" implicitly traverse
  `proposed → approved → booked` writing both actor columns? *Tendency:
  implicit traversal in v1; the distinct gesture arrives with four-eyes
  enforcement.*

## 6. What ADR-T2 owns (recorded here only as the boundary)

The provider directory schema and its signing; the relay service and its
zero-knowledge property; the provider portal (web, not native; e-mail as
notification only); the encryption primitive (age/libsodium-family, not
OpenPGP); the structured confirmation payload that pre-fills the booking step;
the `engagement` object; user-own provider lists (explicitly step 2 within
ADR-T2's own staging); and the monetisation structure, which additionally
requires legal counsel before implementation (the ADR-0107 red line:
PortfoliFLOW remains a software provider — never a broker or advisor).
