# ADR-0130 — Non-Negative Holdings Guard: Cash Investments Are Exempt

## Status

Accepted (2026-08-31). Decider: PortfoliFLOW project owner.
Supersedes the **mechanism sentence** of ADR-0128 Q-2 (capability flag on
the ticket-emission path). ADR-0128 Q-2's *behavioural* decision — a
negative cash balance is entirely permitted, surfaced by warning and
persistent indicator — stands unchanged.

## Context

ADR-0097 §4 established the non-negativity invariant: no ledger write may
drive derived holdings below zero on any date, enforced at write time via
`first_negative_holding_date` (`services/investments/holdings.py`, pure)
from `InvestmentService.add_position_transaction` and
`update_position_transaction`. Its purpose is to reject impossible states:
you cannot sell units you do not hold, and short positions are out of
scope.

ADR-0128 Q-2 then decided that a negative **cash** balance is entirely
permitted — an overdraft is not an impossible state but an economic fact
the book must be able to record — and armed it with a capability flag
scoped to the ticket-emission path only, default off, with the instrument
leg keeping the guard unconditionally.

The mockup decision record (MD-5, striking OP-06) requires more:
"manual-ledger CRUD edits are never refused, regardless of any cash
balance," with the explicit rider that the per-investment CRUD remains the
correction and onboarding path ADR-0128 §6 depends on.

These two texts conflict mechanically, discovered by the T-2/S2
verify-first phase (2026-08-31). `first_negative_holding_date` scans the
**entire** candidate ledger and reports any negative stretch regardless of
whether the candidate row caused it. Under the Q-2 flag scope, the moment
a ticket legitimately books a cash position negative — precisely the
behaviour Q-2 arms — the unconditional guard on the CRUD path refuses
every subsequent add/update on that cash investment that does not fully
cure the stretch from its first day. Refused in particular: recording a
deposit dated *after* the stretch began (the canonical correction), and
byte-identical restatements. Each is a CRUD refusal *because cash is
negative* — the literal thing MD-5 forbids — and together they close the
§6 correction path exactly when a negative balance most needs correcting.

A path-scoped flag therefore cannot satisfy MD-5. The underlying reason is
principled, not accidental: once negative cash is declared an entirely
permitted state of the book, a guard whose sole purpose is to reject
impossible states has no remaining protective function on cash targets —
on *any* write path. Keeping it alive path-dependently produces exactly
the inconsistency above.

## Decision

The ADR-0097 §4 non-negativity guard **does not apply to writes targeting
investments of `investment_type='cash'`**, on any write path.

1. **Scope of the exemption.** `InvestmentService.add_position_transaction`
   and `update_position_transaction` skip the
   `first_negative_holding_date` rejection when the target investment's
   `investment_type` equals `CASH_TYPE` (`services/investments/aum.py`).
   This covers the ticket-emission cash leg, the per-investment manual
   CRUD (ADR-0097 §7), and the Excel cash-statement synthesis alike — one
   rule, no path discrimination.
2. **No capability flag.** The flag mechanism specified in ADR-0128 Q-2 is
   not built. The service API keeps its current signature; callers do not
   opt in or out of the invariant.
3. **Non-cash targets are untouched.** For every other `investment_type`
   the guard applies unconditionally, on every write path, exactly as
   ADR-0097 §4 states. The instrument leg of a ticket emission is
   therefore guarded with no exception — unchanged from Q-2's intent.
4. **The pure helper is untouched.** `first_negative_holding_date`,
   `derive_holdings`, and `holdings_as_of` keep their semantics and their
   purity; the exemption is a service-layer decision about *whether to
   raise*, not a change to the derivation. The `holdings_as_of` docstring
   caveat ("negative only for a malformed ledger") is corrected in
   passing: a negative cash holding is a legitimate, surfaced state.
5. **Surfacing is unchanged.** The composition-time warning with resulting
   balance (MD-5, `negative_cash`) and the persistent read-time indicator
   (S5 placement, derived from the live balance, no stored flag) remain
   the sole surfacing mechanisms, per ADR-0128 Q-2 and the decision
   record §2.4.

## Consequences

- **MD-5 is satisfied in full.** No write path refuses anything because a
  cash balance is or would become negative. The §6 correction path stays
  open in every state of the book.
- **S2a shrinks.** The emission engine writes both legs through
  `add_position_transaction` with no flag plumbing; the guard split it
  must prove becomes "cash exempt everywhere, instrument unconditional
  everywhere" — a simpler and stronger regression anchor than the
  path-scoped variant.
- **The Excel cash-statement synthesis may write a negative balance.**
  Accepted knowingly: the import is the book of record and must mirror
  reality; refusing a true overdraft would be worse than recording it.
  The S5 indicator surfaces it like any other negative balance.
- **A deliberate short position on a cash "instrument" is not prevented.**
  Cash rows are only written at price 1.0000 by the sanctioned paths;
  a manually mis-entered cash row producing a spurious negative balance is
  visible immediately via the indicator and correctable via CRUD — which
  is precisely the path this ADR keeps open.
- **ADR-0128 Q-2 mechanism sentence superseded.** Per the immutability
  rule ADR-0128 is not edited; this ADR is the correction. Future readers
  resolve the conflict in this ADR's favour.
- **Named successors unchanged.** Watch Desk watchpoint on negative cash;
  external portfolio contributions/withdrawals as their own flow type;
  valuta-accurate booking.
