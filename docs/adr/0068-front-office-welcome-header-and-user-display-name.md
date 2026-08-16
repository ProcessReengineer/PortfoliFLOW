# ADR-0068: Front Office Welcome Header and `users.display_name`

- **Status:** Accepted
- **Date:** 2026-05-29
- **Deciders:** PortfoliFLOW project owner
- **Tags:** frontend, ui, web, front-office, greeting, schema, users, auth, theming, phase-6

---

## Context

The Front Office Overview section (ADR-0067) gives the area a portfolio-level headline. To make
the landing personal as well as informative, the area should open with a greeting that names the
operator and the tenant: `Welcome back, {first name} — {tenant name} portfolio` (for the dev owner,
`Welcome back, Alex — Minathena Capital portfolio`).

Two facts in the current code shape the decision. First, the `users` table carries only `email` —
there is no human name to greet by. Second, every user is created through one of several paths: the
`cli/bootstrap.py` primary-owner insert, the create-tenant initial-owner insert in
`services/super_admin/operations.py`, `create_user_idempotent`, and `create_super_admin_idempotent`
(plus the `UserRepository.create` method used by fixtures). A name field that only some of these
populate would be a half-feature. The project has no production tenants or users yet, so adding the
field now incurs no backfill.

A subordinate design choice concerns the accent colour for the tenant name. PortfoliFLOW externalises
all chart visual tokens to `config/chart_theme.json` (ADR-0021); the primary series colour
(`colours.primary`, currently `#E8304A`) is emitted as the CSS variable `--chart-colours-primary`.

## Decision

### Welcome header

The Front Office area opens with a server-rendered greeting as its first element, **above** the
Overview section: `Welcome back, {first name} — {tenant name} portfolio`. It is rendered server-side
(not inside the lazy-loaded Overview section) so it appears immediately rather than flashing in after
an HTMX reveal. The generic "Front Office" title block is replaced; a small muted "Front office"
kicker is retained above the greeting for area identity. The greeting is the largest text on the area.

- **First name** derives from `users.display_name` (its first whitespace-delimited token), falling
  back to a safe capitalized email local-part (only when it reads as a single name — no dots/digits),
  and finally to omitting the name (`Welcome — {tenant} portfolio`). A raw or mangled email is never
  shown. Deriving from the email as the *primary* source was rejected: it misfires on `s.surname@`
  or `info@`, and a wrong name on the very first line is worse than no name.
- **Tenant name** is resolved for `session.tenant_id`. This also retires the `"Sentinel Tenant"`
  placeholder on this surface.
- **Copy is English** (ADR-0008); the conversational German phrasing that motivated the feature is not
  used as a UI string.

### `users.display_name`

`users` gains a nullable `display_name TEXT` column (new reversible migration `b015`). An **optional**
`display_name` (default `None`) is threaded through every user-creation path — `UserRepository.create`,
the three `operations.py` inserts, the `bootstrap` owner insert, and the `create-user`,
`create-super-admin`, `create-tenant` CLIs. No path requires it. For local development the owner's name
is seeded from a new `OWNER_DISPLAY_NAME` environment variable consumed by `bootstrap`; because
`db-reset.sh` sources `.env`, the value reaches the bootstrap subprocess without a script change. The
column is reusable beyond the greeting (status bar, investor communication). `email` is untouched, and
`display_name` is excluded from bootstrap drift detection (it is mutable and optional).

### Accent colour tracks `chart_theme.json`

The tenant name is accented with `var(--chart-colours-primary)` (fallback chain
`var(--chart-colours-primary, var(--ui-accent-primary, #E8304A))`). Using the chart-theme token rather
than a hardcoded hex means the greeting accent re-themes automatically with the charts (including the
light and print chart-theme variants) and stays consistent with the dominant chart series colour.

## Consequences

### Positive

- The area opens on a personal note; `users.display_name` is a small, reusable asset for later surfaces.
- The greeting accent is theme-driven, not hardcoded, consistent with ADR-0021.

### Negative

- The name field is captured in several code paths; the change is wide but shallow (one optional
  parameter and one column per insert), and fully additive.

### Neutral

- The `b015` migration is the only schema write; it is reversible and greenfield (no backfill).
- The header reads the tenant name per Front Office render. A future iteration may lift this into the
  shared shell context and retire the `"Sentinel Tenant"` placeholder everywhere.

## Implementation pointers

- Schema: `b015` migration; `core/models/user.py`; `UserDTO` + `_to_dto`; `UserRepository.create`.
- Creation paths: `services/super_admin/operations.py` (three inserts + signatures); `cli/bootstrap.py`
  (owner insert + `OWNER_DISPLAY_NAME`; super-admin via `SUPER_ADMIN_DISPLAY_NAME`); `cli/create_user.py`,
  `cli/create_super_admin.py`, `cli/create_tenant.py` (new `--display-name` / `--owner-display-name`).
- Seed wiring: `.env.example` (`OWNER_DISPLAY_NAME`); `scripts/db-reset.sh` / `scripts/db-init.sh`
  (verify the var reaches `bootstrap`).
- Web: `web/routes/areas.py::front_office_view` (resolve first name + tenant name → `extra_context`);
  `web/templates/_partials/areas/_front_office_body.html` (header markup);
  `web/static/css/components/overview.css` (`.fo-welcome*` rules + accent var).

## Compliance and audit relevance

**Low.** Adds one nullable, optional user attribute and a presentation surface. No calculation,
authorisation rule, or RLS policy changes; `display_name` is written under the active tenant context
on the same inserts as today and is excluded from drift detection. The decision is non-confidential.

## Related ADRs

- ADR-0067 — Front Office Overview KPI strip (the section this header sits above)
- ADR-0021 — Chart theming externalised to JSON (source of the `--chart-colours-primary` accent token)
- ADR-0008 — English as the sole codebase language (the greeting copy is English)
- ADR-0063 — Multi-tenant activation, role model (the user-creation paths display_name threads through)
- ADR-0040 — Sentinel bootstrap, CLI-driven (the bootstrap owner-seed path extended here)

## Revision history

| Date       | Revision | Note                                                      |
| ---------- | -------- | --------------------------------------------------------- |
| 2026-05-29 | 1.0      | Initial Accepted status; authored before implementation. |
