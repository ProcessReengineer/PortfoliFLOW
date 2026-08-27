# Transactions (#061) — Implementation Kickoff Handover

**Date:** 2026-08-27
**Governing ADRs:** ADR-0128 (Accepted — trade-ticket object model, record flow,
ninth Area), ADR-0129 (Accepted — provider channel; only **Stage A** is in this
track's scope)
**Working document:** `docs/concepts/transactions-record-flow-plan.md`
**Baseline at drafting:** migration head `b034` reserved for S1 (head was
`b033`); next free ADR 0130; eight Areas live; CI green on `2026.08.x`.

This document seeds the implementation chats. Each chat opens with a fresh
Repomix snapshot; this handover travels alongside it. Facts below were
verified against the 2026-08-26 snapshot — every strand's verify-first phase
re-checks the ones it depends on and **stops and reports** on mismatch.

---

## 1. Process rules (binding, from ADR-0128/0129 Consequences)

1. **Operator control.** This feature is central to market acceptance and
   monetisation. Implementation proceeds in operator-gated sub-strands with
   **deliberate pause points**: the strands marked ⏸ below are discussed or
   mocked up *before* anything is built. Pace is traded for precision.
2. **One concern per prompt.** Each Claude Code prompt covers exactly one
   logical concern, with an explicit not-in-scope list and a verify-first
   phase with hard stop-and-report conditions.
3. **Git hygiene.** Claude Code never commits, branches, tags, or pushes.
   The operator stages and commits after review; Conventional Commits;
   two-commit pattern where docs and implementation separate cleanly.
4. **Browser walks.** Claude Code covers ASGI/structural tests; visual
   verification in the browser is always the operator's, at the end of every
   UI sub-strand.
5. **Environment.** Rootless Podman does not auto-start containers after
   reboot — `podman compose up -d` before starting the application.
   Alembic config lives at `db/alembic.ini`. Revision ids stay short
   (VARCHAR(32)). Typechecker is pyright (ADR-0110).

## 2. Verified baseline facts (re-verify per strand)

- **F-1** `investments_add_position` (web/routes/investments.py) writes one
  single-leg `position_transactions` row, `ingest_origin='manual'`; no cash
  leg anywhere. This CRUD **coexists** with the new flows (ADR-0128 §5).
- **F-2** Cash positions are unitised first-class investments per currency
  (`investment_type='cash'`), price ≡ 1.0000, balance = holdings.
- **F-3** Ledger CHECKs: sign rules, price required for buy/sell, currency
  equality, one `opening` (partial unique index), origin triple
  `('excel','live','manual')`; ordering `(trade_date, created_at, id)`;
  `NonNegativeHoldingsError` guards oversell.
- **F-4** ADR-0104 §2 `insert_transaction` executor is pure
  `frames → frames`, settle-against-cash, never writes the book; the
  analytics-purity regression test enforces `services/analytics/` stays
  DB-free/FastAPI-free.
- **F-5** Reported investments: no units; NAVs + `investment_cashflows`
  (`flow_type IN ('capital_call','distribution','fee','carry','dividend',
  'coupon','other','investor_flow')`).
- **F-6** Eight Areas in `web/shell.py` `_AREAS`; section catalogue mirrored
  by `tests/regression/test_section_catalogue_matches_body_partials.py`;
  Cases (ADR-0107) is the precedent for adding an Area.

## 3. Strand plan

Order: S1 → S2 → S3 → S4 → S5 → S6; S7 independent, any time after S1.
⏸ marks a pause point **before** build.

### S1 — Schema & substrate (1–2 prompts)

**Scope:** migration `b034`: `trade_tickets` + `trade_ticket_effects` per
ADR-0128 §1–§3 (full status vocabulary in CHECK, incl. the unreachable
`sent/acknowledged/executed`; four-eyes columns; tenant denorm + RLS per
ADR-0035 §3 / ADR-0078 pattern, tested under `portfoliflow_app`); SQLAlchemy
models; repositories; ticket service skeleton (create/update draft, propose,
cancel-before-booked — **no emission yet**).
**Not in scope:** emission, reversal, any route or template, Area, preview.
**Checkpoint:** none — the ADR determines everything here.
**Verify:** F-3 CHECK wording unchanged; migration head is `b033`.

### S2 — Emission engine (2–3 prompts) ⏸ after S2a

Service layer only; no UI. The core of the feature.

- **S2a — order kinds:** U-SELL / U-BUY emission: atomic two-leg booking
  (instrument leg + cash leg price 1.0000, same currency), net/fee/tax
  arithmetic, cash-position resolution (missing → structured error the UI
  will later turn into inline creation; never silent conversion),
  cash-aware guard (capability flag, `investment_type='cash'` only,
  instrument leg keeps `NonNegativeHoldingsError`), re-materialisation via
  the **existing** seam for both investments (verify where the current write
  path triggers it; one trigger path, never a second), `booked` flip +
  effects rows in one DB transaction, implicit `proposed → approved → booked`
  traversal writing both actor columns.
  **⏸ Pause after S2a:** operator reviews the service API signatures before
  S2b extends them.
- **S2b — reported kinds:** R-SEC-SELL (proceeds as
  `flow_type='distribution'`, `flow_kind='actual'`; NAV → 0 `'manual'`;
  investment inactive; cash leg), R-COMMIT (investment + commitment, **no**
  cash leg, no call tracking), R-SEC-BUY (investment + opening NAV +
  commitment + cash out). Partial secondary sale refused (R-2).
- **S2c — reversal:** effect enumeration → atomic delete + `cancelled` with
  reason; blocked when any effect row was modified/consumed since emission;
  U-NEW draft cancellation removes an orphaned investment shell only if
  otherwise empty.

**Not in scope:** UI, preview, Area, U-NEW wizard logic beyond the shell
clean-up hook.
**Regression anchors:** two-leg atomicity (both or neither); reversal
enumeration + modified-effect block; guard behaviour split cash/instrument.

### S3 — Ninth Area (1 prompt)

**Scope:** `transactions` Area between Cases and Admin in `_AREAS` (comment:
ninth by order of introduction, ADR-0128 §7); routes + shell wiring; section
catalogue (`new`, `blotter`, `history` — placeholder bodies); navigation +
catalogue tests; **eight→nine reconciliation** of CLAUDE.md,
`docs/architecture.md`, ADR-0084 glossary; roadmap #061 → `in-progress` with
ADR references (operator commit).
**Not in scope:** any functional surface content.
**Checkpoint:** none (mechanical; Cases precedent).

### S4 — Record-flow surfaces (3–4 prompts) ⏸ first

**⏸ Wizard mockup precedes everything in S4** — static HTML mockups in the
platform's visual language (ACCENT_RED `#E8304A` family) for: order form
(sell/buy, existing instrument), the new-instrument wizard (embedded
investment creation, AnlV category as hard finish-gate, ISIN → FIGI path),
and the reported flows. Operator review, then:

- **S4a — order forms** (U-SELL/U-BUY): investment picker (unitised, active),
  derived gross/net display, warning surfaces (price deviation 5 % constant,
  negative cash with resulting balance, net ≤ 0, future trade date),
  inline cash-position creation offer, book gesture.
- **S4b — new-instrument wizard** (U-NEW): investment creation embedded as
  wizard step (draft ticket may lack `investment_id`), then U-BUY; first
  purchase books as `buy` (R-1).
- **S4c — reported flows** (R-SEC-SELL / R-COMMIT / R-SEC-BUY) incl.
  full-disposal inactive offer.

**Not in scope:** blotter/history bodies, preview panel, any channel state.
**Each sub-strand ends with an operator browser walk.**

### S5 — Blotter, history, negative-cash indicator (1–2 prompts) ⏸ first

**⏸ Light mockup first**, chiefly for the **placement of the persistent
negative-cash indicator** (ADR-0128 Q-2 leaves surfaces to this checkpoint;
candidates: cash-position detail, Transactions-area banner). Then: blotter
(draft/proposed/approved), history (booked/cancelled, filterable), the
indicator (self-clearing at balance ≥ 0, no acknowledgement gesture).
**Not in scope:** preview, analytics.

### S6 — Pre-trade impact preview (1–2 prompts) ⏸ first

**⏸ Panel mockup first.** Then: `proposed` order tickets feed read-only
through the pure overlay executor + `limit_coverage` (SAA drift, limit
headroom, liquidity, FX); nothing persisted; analytics-purity test must stay
green (F-4). Reported-kind preview is a **named fast-follow**, not in this
strand.

### S7 — ADR-0129 Stage A (1 prompt; independent after S1)

**Scope:** versioned message/confirmation schemas (fill payload: units,
price, fees/taxes, currency, dates, optional ISIN), directory document
format, signature-verification code + publishing public key placeholder
wiring, regression tests: tampered/unsigned directory refused; contract test
"`executed` never books — it pre-fills; only a user action books"; suite
green with the channel feature flag off (no runtime dependency on the
central service).
**Not in scope:** any service, relay, portal, network code beyond
verification of a local document.

## 4. Effort calibration (operator-agreed, 2026-08-27)

Against the recent cadence (`b031` Cases 21.07 → `b032` 03.08 → `b033`
10.08): **S1–S6 ≈ three to four weeks** at the usual session rhythm, roughly
one week of which is the pause-point discipline (accepted by design);
critical path S2, volume peak S4. **S7:** two–three days, parallelisable.
**Stage B** (directory service, relay, portal) is a separate project:
two–three dedicated concept chats (directory format & publishing-key
lifecycle; relay API & retention & polling contract; provider identity,
onboarding, portal auth & UX), then ≈ four–six weeks build. Stage C sits
behind the legal-counsel gate and is not on the development path.

## 5. Kickoff-chat agenda (next chat)

1. Fresh Repomix; verify F-1…F-6 and the `b033` head.
2. **⏸ Wizard mockup session (S4 checkpoint pulled forward)** — settle the
   flow UX before S1 starts, so the schema/service work proceeds while
   surface decisions are already made.
3. Produce the S1 prompt as a downloadable `.md`.
4. Operator: commit rhythm as usual; this handover travels to every
   follow-up chat until the track closes.
