# ADR-0089: Decision Console Briefing UI and Action Model — Calm-by-Default Card Feed and Resolution Actions

- **Status:** Accepted
- **Date:** 2026-07-02
- **Deciders:** PortfoliFLOW project owner
- **Implements roadmap item:** Feature #033 (Decision Console / Irene)
- **Tags:** decision-console, irene, ui, htmx, action-model, audit, calibration

---

## Context

The Decision Console is a new top-level surface (alongside Front Office /
Back Office) with sub-sections **Briefing**, **Journal**, **Watchlist**,
**Scenarios**. This ADR fixes the v0 user-facing surface and the action
model that closes the audit loop. It is the read/write face of the
persistence, delta, and synthesis layers (ADR-0085/0087/0088); it
introduces no new materiality logic.

The design thesis is *suppression*: on a quiet day the Console stays
quiet. The UI must make that visible, not merely tolerate it — a calm day
should *look* calm, not like an empty error state. The findings and their
final urgency/band are already computed and persisted (ADR-0088); the UI
renders them and records the PM's response.

The stack is FastAPI/Jinja2/HTMX with the existing dark theme
(`config/chart_theme.json`, crimson `#E8304A`). The action model must
complete the append-only audit trail in `irene_finding` (ADR-0085):
resolutions are **Acted / Dismissed / Acknowledged**.

## Decision

### Briefing — the heart, calm by default

- A quiet day renders a **green status line** plus a **collapsed**
  "lower-priority notices" strip. Silence is a
  first-class, deliberate state, not an empty list.
- Findings render as a **card feed** ordered by final urgency (ADR-0088),
  then recency.
- **Band drives colour, restrained** — bands (`informational` /
  `noteworthy` / `critical`) map to increasing visual weight; the raw
  1–10 is not shown as a badge, only used for ordering.
- A manual re-trigger button ("Request analysis") requests a beat
  out-of-cadence. It enqueues a due beat for the tenant (ADR-0086); it
  does not run synthesis inline in the request.

### Card layout — fixed ordering

Each card renders in the order:

**Trigger → computed materiality → options → action → derivation.**

- The **recommendation never stands without the deterministic figure
  beside it**: the computed materiality (`basis`, the grounded number
  from ADR-0088) is shown above/with any options.
- `options` render only when present (band-gated upstream, ADR-0088); an
  `informational` card shows fact and derivation, no options.
- **Derivation** (`basis` + `evidence_refs`) is shown last, collapsible,
  so the audit path is always reachable without cluttering the glance
  view.

### Action model — closing the audit loop

- Three actions per open finding: **Acted**, **Dismissed**,
  **Acknowledged**.
- An action writes `resolution`, `resolved_at`, `resolved_by` on the
  `irene_finding` row (ADR-0085), moving it out of the open feed into the
  Journal. Findings are otherwise immutable.
- Actions are HTMX partial updates (no full reload); the card leaves the
  Briefing feed and appears in the Journal.
- A dismissal in v0 **does not** train suppression (ADR: learned
  suppression is explicitly out of v0; the Watchlist is the only tuning
  surface). Dismissal is a pure audit-trail resolution.

### Sub-sections in v0

- **Briefing** — the card feed above (primary deliverable).
- **Journal** — audit/history view over resolved `irene_finding` rows;
  read-only, filterable.
- **Watchlist** — what is monitored and at which thresholds; the single
  explicit, auditable tuning surface in v0. Minimal scope: view/edit the
  monitored subjects and their thresholds.
- **Scenarios** — placeholder anchor for Feature #034; minimal in v0.

### Cadence settings

- The per-tenant `irene_schedule` (ADR-0085) is edited from a settings
  view within the Console (or under Admin): cadence, preferred hour,
  timezone, enabled. This is the domain-level calibration interface, not
  a deployment artifact (ADR-0086). Per-user configuration is deferred
  (the `user_id` seam exists but is unused in v0).

## Consequences

- Silence renders as an affirmative calm state, making the suppression
  thesis visible to the PM.
- The fixed card ordering enforces the grounding principle at the UI
  layer: no recommendation appears without its computed figure.
- The action model completes the append-only audit trail; every surfaced
  card ends in a recorded resolution.
- HTMX partials keep the Briefing↔Journal transition lightweight and
  consistent with the existing web layer.
- Watchlist is the only tuning surface exposed in v0, keeping calibration
  explicit and auditable (no learned suppression).

## Alternatives Considered

- **Empty list on a quiet day.** Rejected: reads as an error/empty state;
  a deliberate calm status line is the point of the product.
- **Showing the raw 1–10 urgency as a badge.** Rejected: implies a
  precision the LLM does not have; bands are the behavioural unit.
- **Inline synthesis on "Request analysis".** Rejected: would run a beat
  inside a web request under uncontrolled timing; enqueue a due beat
  instead (ADR-0086).
- **Mutable findings / edit-in-place.** Rejected: breaks the append-only
  audit guarantee (ADR-0085); only resolution is recorded.
- **Dismissal as a suppression signal in v0.** Rejected: learned
  suppression is an auditability risk deferred beyond v0; the Watchlist
  is the explicit tuning surface.

## Compliance & Audit Relevance

- **Decision authority (MaRisk):** the UI presents decision support; the
  PM acts and records the resolution. The recommendation is always shown
  beside its deterministic figure, so no card nudges without grounding.
- **Complete audit trail (BAIT/VAIT):** every finding terminates in a
  recorded resolution (acted/dismissed/acknowledged) with actor and
  timestamp; the Journal is the immutable history.
- **Explicit, auditable calibration:** the Watchlist and cadence settings
  are the only tuning surfaces; there is no opaque learned suppression in
  v0, so system behaviour changes are attributable to explicit
  configuration.
- **Tenant isolation (DORA):** all reads/writes go through the
  RLS-scoped repositories (ADR-0078); the Console surfaces only the
  active tenant's findings.

## Revision History

- 2026-07-02 — Proposed.
- 2026-07-11 — Accepted against the shipped code. Implemented 2026-07-02:
  Decision Console established as the sixth Area, with `modules/decision_console/`
  (`briefing`, `journal`, `watchlist`, `scenarios`) and
  `web/routes/decision_console.py`.
