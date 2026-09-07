# Transactions — Mockup Decision Record (M-4)

**Status:** Draft for operator review and commit
**Date:** 2026-08-30
**Roadmap item:** #061 — Transactions
**Session:** S4 checkpoint pulled forward (kickoff T-0, Mission Control 2026-08-27)
**Refines:** ADR-0128 (trade-ticket object model and record flow), ADR-0129
(area placement, provider channel staging),
`docs/concepts/transactions-record-flow-plan.md` (working document),
`docs/concepts/transactions-implementation-kickoff-handover.md`
**Mockup files (committed alongside this record):**
`docs/handover/transactions-order-form-mockup-m1.html` (M-1),
`docs/handover/transactions-new-instrument-wizard-mockup-m2.html` (M-2),
`docs/handover/transactions-reported-flows-mockup-m3.html` (M-3)

---

## 0. Purpose and authority

This record fixes every user-facing decision the T-0 mockup session settled, so
that S1 (schema) encodes them and S4 (surfaces) builds against an approved
picture. The mockup HTML files are the **binding visual specification**
(precedent: `docs/handover/cases-area-mockup-v2.html` for the Cases area);
this record is the machine-readable extract implementation prompts cite.

Where a decision refines the working document or an ADR, the refinement is
recorded here; accepted ADRs remain untouched (corrections travel in successor
ADRs if ever needed — none is needed: every decision below is compatible with
ADR-0128's text).

All decisions were made by the operator in the T-0 review rounds of
2026-08-28/29 unless marked otherwise. Exactly one item (MD-18) carried a
**confirm-at-commit** marker; it was **confirmed on 2026-09-04** and is
final.

---

## 1. Decisions

### Area and composer frame

**MD-1 — Five flow entry points; direction lives inside the order form.**
The "New transaction" chooser offers: *Buy or sell units* (U-BUY/U-SELL, one
form with a direction toggle), *Buy a new instrument* (U-NEW), *New
commitment* (R-COMMIT), *Buy a stake — secondary* (R-SEC-BUY), *Sell a stake —
secondary* (R-SEC-SELL). Refines working document §2 (six flows → five
surfaces).

**MD-2 — Ticket persistence on first explicit gesture.**
The draft ticket row — and with it the per-tenant `ticket_number` (D-3,
`case_number` precedent) — is created on the first explicit user gesture:
**Continue** (wizard step 1), **Save as draft**, **Propose**, or **Book now**.
Never on opening the composer. Until then the header reads "New ticket ·
Unsaved". Consequence: no orphaned drafts in the blotter, no burnt numbers; a
mid-wizard draft is an ordinary, resumable blotter row.

**MD-3 — Settlement position: three states, always explicitly confirmed
(answers OP-02).**
The ticket carries an explicit `cash_investment_id` (D-1). The order surface
shows:
- *Exactly one match:* pre-filled and visible, but the primary action stays
  inactive until the user ticks "Settle against this position". One deliberate
  click on every order; no silent default.
- *Several matches:* radio picker with balances and the projected resulting
  balance on the selected row; **no default**.
- *None:* inline creation offer ("PortfoliFLOW never converts on your
  behalf"); the mini-form creates the cash position (opening balance row);
  declining leaves the ticket unbookable.
Refines D-1, ADR-0128 §4.

**MD-4 — `settlement_date` is visible, optional, informational.**
Captured on every cash-moving flow with the hint "Recorded only. Cash books on
the trade date." Both legs book at `trade_date` in v1 (working document §4.1);
valuta-accurate booking stays a named successor. The column exists from S1.

**MD-5 — OP-06 struck: nothing is ever refused because cash is negative.**
Booking is never refused and manual-ledger CRUD edits are never refused,
regardless of any cash balance. A ticket that takes a cash position negative
produces (a) the warning with the resulting balance at composition time and
(b) the persistent indicator on the cash position until the balance is ≥ 0
(S5 placement, OP-10). The intended path for portfolio changes is the ticket;
the CRUD remains the correction and onboarding path (ADR-0128 §6 depends on it
staying open) — a convention, not a technical lock. Contents of transactions
remain the fund manager's responsibility (D-2). Named successors, outside
#061: a Watch Desk watchpoint on negative cash; **external portfolio
contributions/withdrawals** (money into or out of the mandate) as their own
flow type outside the trade-ticket object.

**MD-6 — The ledger effect is shown before booking.**
Every cash-moving composer renders a "Ledger effect" block listing both legs
("two legs, booked together or not at all") with signed units and prices,
including the cash leg at price 1.0000. ADR-0128 §2's atomicity is a visible
teaching element, not an implementation detail.

**MD-7 — Message vocabulary: block / warning / consequence.**
Blocks (red, `#FF6B6B` left border) disable the primary actions; warnings
(amber, `#FFC107`) never disable anything; consequences (blue, `#4A9BD9`) are
informational statements of what booking will do. Full disposal on **U-SELL**
is a *consequence* carrying the optional checkbox "Set ‹investment› inactive
after booking" — selling all units is a choice there, so inactivation is
offered, not imposed. (Contrast MD-17.)

**MD-8 — Currency mismatch has no surface in the composers.**
Currency is derived from the selected/created investment and shown read-only
in the context strip; the working-document §3 block remains a service-layer
guard with no reachable UI state.

**MD-9 — Message copy is fixed by the mockups.**
The mockup wording of the four warnings (price deviation with reference price
and date; negative cash with resulting balance, "Booking is allowed — the
trade is your call", and the flag notice; net ≤ 0; future trade date "The book
records facts. Post-dating is allowed but unusual.") and of the blocks
(oversell with holding and date; missing price; AnlV finish gate; R-2 refusal)
is the binding copy. S4 lifts it verbatim.

### U-NEW wizard

**MD-10 — Wizard structure and draft visibility.**
Four steps (Identify → Classify → Order → Confirm) with a stepper. Completing
step 1 is the first explicit gesture (MD-2): from there the draft exists,
appears in the blotter without an `investment_id`, and reopens the wizard
where it stopped. Blotter row layout is explicitly **not** fixed by M-2
(S5 designs it).

**MD-11 — AnlV gate at the finish; applies to Propose and Book now.**
Step 2 permits continuing with the category unset (amber notice); step 4
refuses **Book now and Propose** with a red block and a jump-back link, while
**Keep as draft** stays available. Rationale: `proposed` means "complete and
validated" (ADR-0128 §3), so only the draft may dangle.
`investments.anlv_code` stays nullable; the gate is a service-layer transition
guard, not a schema constraint.

**MD-12 — Ticket-or-booking: the investment row is an emission effect.**
The wizard's master data (and R-COMMIT's / R-SEC-BUY's, see MD-15) lives **on
the ticket as payload** until booking; the `investments` row is created only
by the booking emission, alongside the legs. Discarding a draft deletes the
ticket and nothing else — no orphaned investment shells exist, so the
working-document §2.3 cleanup clause is never exercised by this design (it
remains valid as written for any future path that creates rows earlier).
Exactly two semantic states, per the operator: *"this is the data"* (draft)
and *"use it"* (booked). Half-created instruments never appear in pickers,
reports, or market-data routing.

**MD-13 — Identifier paths.**
*Public identifier:* scheme (isin/ticker/cusip) + value, resolved via the
existing FIGI normalisation; the resolved panel shows FIGI, name, currency;
name and currency pre-fill step 2 and stay editable; **both** identifier rows
(entered scheme + figi) are stored on the investment at emission.
*No public identifier:* free-form path for Spezial-AIF share classes, club
deals, notes; no synthetic internal code is forced at creation time.

**MD-14 — R-1 surfaced quietly.**
The instrument leg of a first purchase is annotated "first purchase" in the
ledger-effect block. The wizard never writes an `opening` row.

### Reported flows

**MD-15 — Compact single-page forms.**
R-SEC-SELL, R-COMMIT and R-SEC-BUY are single-page forms sharing M-1's block
order (what happened → amounts → on booking → settlement → messages →
actions), not wizards. Their master data follows MD-12 (payload on ticket;
investment row at emission). Reported-flow payload inventory: vintage year,
commitment amount (R-COMMIT); purchase price, acquired NAV, assumed unfunded
commitment, vintage year (R-SEC-BUY); proceeds with optional fees/taxes split
(R-SEC-SELL).

**MD-16 — "On booking" lists every emission row.**
Each reported form renders the full emission as typed rows (create / flow /
nav / status / commit / buy / sell) before the user acts. Nothing the booking
does is a surprise afterwards.

**MD-17 — R-SEC-SELL consequences are emission rows, not options.**
NAV → 0 (manual origin) and "investment set inactive" are inherent to a full
sale and appear as rows in the On-booking block plus a blue consequence
notice — no checkbox (contrast MD-7's U-SELL case). Proceeds book as
`distribution` / `actual` (ADR-0128's Q-3 resolution), shown literally in the
flow row. The derived "vs. last reported NAV" line is neutral information.

**MD-18 — R-2 refusal blocks even the draft.** *(confirmed 2026-09-04)*
"Sell part of the stake" is selectable and selecting it produces the red
refusal (proportional NAV and unfunded restatement, plan-flow interaction;
successor named). While selected, **Book now, Propose and Save as draft are
all disabled** — a partial-sale ticket cannot exist in v1, not even as a
draft, because a draft whose flow does not exist would be a blotter corpse
with no possible exit. This is deliberately stricter than the AnlV gate
(where only a field value is missing, so the draft may dangle).

**MD-19 — R-COMMIT books no cash and explains why.**
No settlement block; in its place a blue panel: no cash moves with this
ticket; capital calls remain ordinary cashflows outside it; the ticket books
once at the commitment date (= trade date) and stays the provenance anchor
(R-3 upheld — no drawdown container).

**MD-20 — Discount/premium is context, never judgement.**
R-SEC-BUY derives "price vs. acquired NAV" and R-SEC-SELL derives "proceeds
vs. last reported NAV" as neutral info rows in the Amounts block. Secondary
discounts are ordinary economics; they never warn.

**MD-21 — AnlV gate scope across flows.**
The gate (MD-11 semantics) applies to the investment-creating flows — U-NEW,
R-COMMIT, R-SEC-BUY — on both Propose and Book now. R-SEC-SELL and
U-BUY/U-SELL touch existing investments and carry no gate.

---

## 2. Schema-relevant outcomes for S1

The kickoff requires this list explicitly; S1 encodes it.

1. **`ticket_number`** — per-tenant, allocated when the draft row is created
   (= first explicit gesture, MD-2). `NOT NULL`, unique per
   `(tenant_id, ticket_number)`; `case_number` precedent for sequence
   mechanics. There is no "unsaved" row state to model.
2. **`cash_investment_id`** — explicit nullable FK on the ticket (MD-3).
   Null in drafts; the draft→proposed and draft→booked transitions require it
   for cash-moving flows. No default-selection logic exists anywhere.
3. **`settlement_date`** — nullable date column, captured, informational in
   v1 (MD-4). Both legs book at `trade_date`.
4. **No OP-06 schema.** No edit-refusal flags, no stored negative-cash
   marker: the S5 indicator derives from the current balance at read time
   (MD-5).
5. **Master-data payload on the ticket** (MD-12, MD-15): the ticket must be
   able to carry the full U-NEW / R-COMMIT / R-SEC-BUY master-data inventory
   (name, type, asset class, currency, AnlV code, identifier scheme+value,
   resolved FIGI, manager/region; vintage year, commitment amount, purchase
   price, acquired NAV, assumed unfunded) **without** an `investments` row
   existing. `investment_id` on the ticket is nullable until booking.
   Representation (JSONB payload vs. typed nullable columns) is S1's call;
   the semantics above are fixed.
6. **`set_inactive` choice** — the U-SELL full-disposal option (MD-7) needs a
   home on the ticket (boolean field or emission parameter). For R-SEC-SELL
   inactivation is unconditional (MD-17) and needs no field.
7. **No fraction column** (MD-18): v1 models full secondary sales only.
8. **AnlV gate is a transition guard, not a constraint** (MD-11, MD-21):
   `investments.anlv_code` stays nullable; enforcement sits on the
   draft→proposed and draft→booked transitions of investment-creating flows.
9. **Named constants** (single module, referenced by services and templates):
   `PRICE_DEVIATION_WARN_RATIO = 0.05` (ADR-0128 Q-4); warning identifiers
   `price_deviation`, `negative_cash`, `net_non_positive`,
   `future_trade_date`; block identifiers `currency_mismatch`,
   `oversell`, `missing_price`, `missing_anlv`, `partial_secondary_sale`.
   Message copy per MD-9.
10. **Lifecycle unchanged** from ADR-0128 §3: Book now traverses
    `proposed → approved → booked` implicitly; `proposed_by == approved_by`
    permitted in v1 (D-4). The four-eyes variant remains an annotated future
    state only.

## 3. Named successors (recorded, not scheduled)

- Watch Desk watchpoint on negative cash positions (outside #061).
- External portfolio contributions / withdrawals as an own flow type on the
  mandate level, outside the trade-ticket object (MD-5).
- Partial secondary sales (R-2).
- Valuta-accurate settlement booking (moves only the cash leg's date).
- Four-eyes enforcement with a distinct approve gesture (V-8 annotation).
- FX cash-to-cash conversion (working document §2.7, restated).

## 4. Handover to next strand (T-1 / S1)

S1 must encode items **2.1–2.10** above. The mockups themselves answer any
surface question S1 hits that this record does not name; where mockup and
record disagree, the record wins and the mismatch is reported to Mission
Control before proceeding.

## 5. Operator actions

1. Review this record; strike the confirm-at-commit marker on MD-18 (or amend
   MD-18 and the M-3 mockup).
2. Copy the three mockup HTML files to `docs/handover/` under the names in
   the header; place this file at
   `docs/concepts/transactions-mockup-decisions.md`.
3. Single docs commit, suggested message:
   `docs(transactions): add T-0 mockups M-1..M-3 and decision record (#061)`
4. Report MD-2, MD-3, MD-4, MD-5, MD-12, MD-18 and §2 to Mission Control as
   the schema-touching set; Mission Control releases kickoff T-1 with them.

## 6. Addenda — decisions of record from implementation (S2–S5)

Mirrored from the T-2 and T-4 reports so this record stays the single
place downstream strands read. Sources: T-2 closing report (2026-09-01),
T-4 S4a interim note (2026-09-04), T-5 M-5 checkpoint (2026-09-07),
Mission Control board.

- **A-1 · Creation invariant (resolves the D-I question).** An
  `investment_update` effect with `prior_state IS NULL` means "this
  investment row was created by this booking"; a dict before-image means
  "updated" (only `is_active` is ever changed by an emission). No fifth
  effect type; pinned by S2b/S2c tests.
- **A-2 · Block vocabulary widened (§2.9 note).** `BLOCK_IDENTIFIERS` now
  carries eight identifiers: the §2.9 five plus S2b's
  `nav_exists_at_trade_date`, `duplicate_investment_name`,
  `investment_inactive`.
- **A-3 · MD-17 confirmed against a contrary kickoff line.** R-SEC-SELL
  deactivates the investment unconditionally; `set_inactive` is a U-SELL
  choice only (T-2 D-S).
- **A-4 · Reversal causes.** `TicketReversalBlocked.cause ∈ {modified,
  consumed, holdings_consumed, unrestorable, referenced_by_ticket}`. Live
  references (draft/proposed/approved tickets on a booking-created
  investment) block with `referenced_by_ticket`; exclusively terminal
  references degrade to the retained-shell path. `retained_because`
  carries ticket numbers or the retaining table's plain-language name for
  user rows — surfaces render it verbatim (T-2 D-AC, T-4 OP-17).
- **A-5 · Retained shells.** Reversing a creating flow deletes the shell
  iff only platform artefacts remain; user rows retain it with
  `is_active=False` and the ticket stays linked — an inactive investment
  the operator owns and S5 surfaces.
- **A-6 · Settlement position hygiene (refines MD-3).**
  `inactive_cash_position` is its own block identifier and must not
  trigger the inline-creation offer; only `missing_cash_position` does
  (T-2 D-E/D-F).
- **A-7 · Refusal copy, uniform rule (refines MD-9).** Every typed
  service error renders `str(exc)` as the red block — service sentences
  are the copy; routes invent no wording. Mockup copy remains binding
  where it exists (T-4 D-5).
- **A-8 · Inline cash creation (refines MD-3).** The mini-form's
  `opening_date` is the composer's trade date, via the sanctioned
  `InvestmentService.create_cash_position` seam (T-4 W-1, P-0c).
- **A-9 · Draft gating (refines MD-2/MD-11).** Save as draft requires
  only direction, investment/currency and trade date, and stays available
  under warnings and blocks; Propose/Book keep full gating. MD-18 is the
  named exception and lands as a block-aware `draft_enabled` in S4c
  (T-4 W-3).
- **A-10 · Preview discipline.** `TicketService.preview` is read-only and
  previews only `oversell` in v1; adding a previewable block is a
  decision, not a follow-up (T-4 D-2/P-0b).

Sources for A-11…A-19: T-5 M-5 checkpoint (2026-09-07), Mission Control
board. The T-5 D-register letters (D-A…D-I) are given in brackets so the
closing report and this record cross-reference.

- **A-11 · Negative-cash indicator placement (fixes ADR-0128 Q-2's open
  point, OP-10) [T-5 D-A].** Both candidate surfaces: a banner in the
  Transactions area between the area header and the first section, and a
  block on the cash position's detail page beneath the header and above
  Positions. One line per negative cash position, each linking to its
  detail page; no sum across currencies. Derived live from the ledger via
  the pure helpers `holdings_as_of` (balance as of today) and
  `first_negative_holding_date` ("since"); no stored flag, no
  acknowledgement gesture, no schema. Absent — not struck through — at
  ≥ 0. Informational on every surface; never blocks (ADR-0130). The
  "last taken below zero by ticket #n" line is **not** in v1 (named
  successor: it needs an effects lookup the derivation should not carry).
- **A-12 · Resume mechanism (OP-25) [T-5 D-B].** One
  `GET /api/transactions/ticket/{ticket_id}` opens any in-flight ticket.
  It resolves the ticket to its flow through a single reverse lookup
  `_flow_of(ticket)` (kind, direction, `is_investment_creating`) onto
  `_FLOWS`, and renders `_FLOWS[flow].composer`; for the wizard
  (`composer is None`) it renders exactly what
  `GET /api/transactions/wizard?ticket_id=` renders. The per-flow opening
  GETs stay as the chooser's entry points and gain no `ticket_id`. The
  blotter row's open gesture is agnostic of the flow. `_Flow` gains a
  `label` — the human flow name the blotter and history show — so the
  routing table is also the labelling table.
- **A-13 · Cancel and reverse home [T-5 D-C].** Gestures live on the
  list row: the blotter row offers *Open* and *Cancel ticket…* (a draft:
  *Discard draft…*); the history row offers *Details* and, on a booked
  ticket only, *Reverse booking…*. Neither the opened composer header nor
  the history detail carries the gesture in v1. The reason step is an
  inline panel beneath the row, never a modal: required for
  `proposed`/`approved` (the service's `CANCEL_REASON_REQUIRED_STATUSES`),
  optional for `draft`, always required for a reversal. A row ⋯ menu is
  a polish matter (OP-23), not a v1 shape.
- **A-14 · Refusal rendering for reversal (refines A-7) [T-5 D-D].**
  `TicketReversalBlocked` renders `str(exc)` verbatim as the red block,
  for all five causes; the raw row UUIDs the service sentences carry are
  accepted as they are. No additive resolved-name line in v1.
- **A-15 · Reversal report rendering [T-5 D-E].** A `ReversalReport`
  renders as a done block — "Ticket #n reversed. k rows undone; the
  ticket is now cancelled — "<reason>"." with a per-type count from
  `reversed` — followed, when `shell` is retained, by a consequence
  block "Investment retained, inactive — because: <retained_because>"
  with `retained_because` verbatim (A-4/A-5).
- **A-16 · Actor display on History [T-5 D-F].** The station columns
  (`proposed_by`, `approved_by`, `booked_by`) resolve to display names
  through `UserRepository`, the Cases `_resolve_owner_names` precedent.
  The cancel/reverse actor is **not shown** in v1: there is no
  `cancelled_by` column (T-1 D-5) and `AuditLogRepository` reads only
  `has_update_since`; deriving the actor from the audit log is a named
  successor (one read-only repository method), not an S5 deliverable.
- **A-17 · Terminal-state labelling [T-5 D-I].** Both terminal endings
  share `status='cancelled'`; surfaces label a row **Reversed** iff
  `booked_at IS NOT NULL` and **Cancelled** otherwise. Reversed rows keep
  their effect list; each effect renders as "This row is no longer in the
  book" (the existing `_effect_rows` missing shape), which is correct and
  is what History is for.
- **A-18 · OP-22 visibility [T-5 D-G].** The hint is shown, one line, in
  two places: above the blotter table and as the third line of the
  detail-page indicator. Copy (D-register, no mockup anchor): "A
  booking's cash effect is in the book at once; the cash position's
  balance shows it from the next price date on."
- **A-19 · History filter set v1 [T-5 D-H].** Status (given; "reversed"
  is a derived option on the same column), kind, investment, and
  trade-date range. No free text, no pagination. `list_by_status` grows
  read-only keyword parameters for exactly these; ordering stays
  `ticket_number DESC`.

Sources for A-20…A-26: the S5 closing reports P-5b/P-5c and the T-5
strand closing report (2026-09-07). The T-5 D-register letters are given
in brackets so the closing report and this record cross-reference.

- **A-20 · Reversal answer shape (refines A-15) [T-5 D-O].** The reverse
  `POST` answers with one swap: the refreshed History list
  (`#tx-history`, `outerHTML`) carrying the reversal report as a done
  block above the filter bar. Not a report-in-slot plus an out-of-band
  list refresh — htmx performs out-of-band swaps before the main swap, so
  an OOB list refresh would detach the report's target. The report's
  content is A-15's.
- **A-21 · History detail copy and timestamps [T-5 D-T, D-U].** A
  cancelled ticket that was never booked shows, in place of the effects
  list, "Nothing was written — this ticket was never booked." History
  renders timestamps as `YYYY-MM-DD HH:MM` (stations, outcome line); the
  blotter stays date-only. Group headings in the effects list: "position
  transactions", "cashflows", "NAVs", "investment". Settled against: "no
  cash leg" for a commitment. Empty filtered list: "Nothing matches these
  filters."
- **A-22 · Indicator "since" (refines A-11) [T-5 D-W].** "since <date>"
  is the start of the *current* run of negative end-of-day holdings, as
  of today, via the pure `negative_since(transactions, on)` in
  `services/investments/holdings.py`. `first_negative_holding_date` is
  the write-time check and returns the first date holdings ever went
  negative; the two answer different questions and both stay.
- **A-23 · Indicator refresh (refines A-11) [T-5 D-X, D-AA].** The banner
  re-derives itself on page load and after any request that completes
  inside the composer host (`#tx-composer-host`) or the History section
  (`#history`) — the two places a booking or a reversal ends. No polling,
  no acknowledgement, no change to the composer or History routes; a
  History filter change also refreshes it, which is one cheap read.
  `#history` rather than `#tx-history` because the latter is lazily
  revealed and does not exist when htmx binds the trigger.
- **A-24 · Inactive cash positions (refines A-11) [T-5 D-Y].** An
  inactive cash position below zero is listed, its name suffixed
  "· inactive". An overdraft on a deactivated position is still a
  liability the book records.
- **A-25 · Derivation seam (refines A-11) [T-5 D-Z].**
  `InvestmentService.list_negative_cash(on=)` and
  `negative_cash_for(investment_id, on=)` are the one place a
  negative-cash state is derived; the Area banner and the cash position's
  detail page both read it, and nothing else computes one. It is live,
  per read, over `holdings_as_of(…, on)` and `negative_since(…, on)`;
  `get_position_summary`'s holdings figure (last ledger point, possibly
  future-dated) is not the indicator's number. S6's preview reads this
  seam with the same `on`.
- **A-26 · Banner line markup (refines the M-5 copy register) [T-5
  D-AD].** Per position: "<name> stands at <balance> <ccy>" with "since
  <date>" in its own span, no comma — the mockup's markup; the register's
  prose sentence is superseded for the banner. The detail-page sentence
  is unchanged.

**Copy fixed at the M-5 checkpoint (binding for S5 templates):**

- Blotter columns: *Ticket · Flow · Investment · Amount · Trade date ·
  Status*. History replaces *Status* with *Outcome*.
- Flow labels (`_Flow.label`): "Order · Buy", "Order · Sell", "New
  instrument", "Commitment", "Secondary sale", "Secondary purchase".
- Creating rows: "creating: <name>" in the Investment column.
- Empty blotter: "Nothing in flight." Empty history: "Nothing booked or
  cancelled yet."
- Cancel panel, proposed/approved: lead "Cancel ticket #n?", sub "A
  proposed ticket is a decision others may have seen. A reason is
  required." (for `approved`: "An approved ticket …"). Button "Cancel
  ticket"; "Keep" closes the panel.
- Cancel panel, draft: lead "Discard draft #n?", sub "A draft is private
  workspace. A reason is optional. The ticket number stays allocated and
  the ticket appears in History as cancelled." Button "Discard draft".
- Reverse panel: lead "Reverse booking #n?", sub "The rows listed above
  are deleted in one transaction and the ticket becomes cancelled. A
  reason is always required. The effect list stays on the ticket so
  History still says what it once did." Button "Reverse booking".
- Indicator, banner: lead "A cash position is below zero." / "Two cash
  positions are below zero." (n > 2: "<n> cash positions are below
  zero."), then per position "<name> stands at <balance> <ccy>, since
  <date>". Second line MD-9 verbatim: "The position stays flagged until
  the balance is back at zero or above."
- Indicator, detail page: "This position stands at <balance> <ccy> since
  <date>." then the MD-9 sentence, then the A-18 hint.

**Fixed during S5 implementation:**

- "Nothing matches these filters."
- "Nothing was written — this ticket was never booked."
- "<n> cash positions are below zero." for n > 2.
- "· inactive" suffix on an inactive cash position's name in the banner.
