# ADR-0120: "Open case →" Gated by Band, Not by Options Presence

Status: Accepted (2026-08-13)
Date: 2026-08-13
Supersedes: nothing (ADR-0107 remains Accepted and unedited; this ADR revises
its binding decision 4's placement rule for the option-less case)
Related: ADR-0088 (surface_finding contract), ADR-0089 (band-coloured cards),
ADR-0106 ("Open case →" preview), ADR-0107 (Cases, C4 open-case composition)

## Context

ADR-0107 D1 (Gate-C0 A) decided: "'Open case →' stays band-gated inside the
Possible-moves block of higher-band cards; informational findings are
acknowledged, not case-opened." The shipped template implements the gate as
`{% if card.options %}` — the affordance renders only when the Possible-moves
block renders.

That gate is stricter than the decision. `options` is an **optional** member of
the ADR-0088 `surface_finding` contract ("when there is a genuine, actionable
choice…"); the synthesis model may legitimately omit it on a higher-band card.
A critical finding phrased as pure statement — e.g. a first-time SAA ceiling
breach — then carries no case path at all. In the BAIT/VAIT context, "breach
without a case affordance" is precisely the wrong gap: case-worthiness follows
from the band, not from whether Irene authored advisory prose.

## Decision

### §1 Gate

The "Open case →" affordance renders on every card with
`band ∈ {noteworthy, critical}`, regardless of `options` presence.
Informational cards never render it (D1 semantics unchanged:
informational is acknowledged-only; manual "New case" covers the rest).

### §2 Placement

When the Possible-moves block is present, the affordance stays inside it —
binding decision 4 is preserved for that case. When the block is absent on a
higher-band card, a slim standalone variant of the same form renders in the
card footer (same endpoint, same HTMX target/swap semantics: the card replaces
itself, success HX-Redirects to the pre-filled case).

### §3 Server-side enforcement

The C4 endpoint (`POST /api/watch-desk/findings/{id}/open-case`) enforces the
band gate server-side: an informational finding is rejected, independent of
what any template renders (defence in depth). The implementation prompt
verifies first whether such a check already exists and adds it only if absent.

## Consequences

- Template change in the briefing card (condition + footer variant), minimal
  CSS for the standalone form.
- Router tests: option-less noteworthy and critical cards render the
  affordance; informational cards do not; the endpoint rejects an
  informational finding with the appropriate status.
- No contract change: ADR-0088's payload stays untouched; `options` remains
  optional and band-gated for its own rendering.
- ADR-0107 stays Accepted and unedited; this ADR is the recorded revision of
  the placement rule for the option-less case.

## Revision History

| Date | Author | Change |
|---|---|---|
| 2026-08-13 | PortfoliFLOW project owner | Drafted (Proposed). |
| 2026-08-13 | PortfoliFLOW project owner | Accepted; index status updated. |
