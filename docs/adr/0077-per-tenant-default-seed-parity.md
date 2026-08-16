# ADR-0077: Per-Tenant Default-Seed Parity Between `bootstrap` and `create-tenant`

- **Status:** Accepted
- **Date:** 2026-06-05
- **Deciders:** PortfoliFLOW project owner
- **Implements roadmap item:** N/A (provisioning-parity defect fix; relates to ADR-0040 bootstrap CLI, ADR-0043 unclassified asset class, ADR-0063/0064 multi-tenant activation & super-admin CLI operations)
- **Tags:** tenant-provisioning, seeding, data-import, bootstrap-parity, governance

---

## Context

PortfoliFLOW provisions tenants through two code paths that are meant to
produce equivalent, import-ready tenants:

1. **`portfoliflow bootstrap`** — initialises the system tenant and the
   primary tenant (`Minathena Capital`). Its seed-installation
   sequence runs, against `PRIMARY_TENANT_ID`, the SAA seeds, the
   `unclassified` **asset class** (`install_unclassified_asset_class`),
   the Phase-7 default asset-class catalogue
   (`install_default_asset_classes`), the `unclassified` sector, and the
   default regions.
2. **`portfoliflow create-tenant`** (and the equivalent super-admin web
   route, ADR-0064) — provisions any further tenant at runtime, then
   calls `services.super_admin.seed_tenant_defaults` for its per-tenant
   seed data.

These two paths have drifted. `seed_tenant_defaults` installs only the
SAA seeds, the `unclassified` **sector**, and the default regions. It
does **not** install the `unclassified` **asset class**, nor the Phase-7
default asset-class catalogue. Both of those are installed exclusively by
`bootstrap`, hard-wired to `PRIMARY_TENANT_ID`.

Two concrete defects follow:

- **ADR-0043 invariant violated for non-primary tenants.** ADR-0043
  requires that *every* tenant carries an asset class with code
  `unclassified` as the Excel-import fallback bucket. A tenant created
  via `create-tenant` does not. The Excel import (`InvestmentService.
  transform_upload_to_investments`) hard-requires this row and raises
  `ValueError` ("Bootstrap fault: the 'unclassified' asset class is
  missing …") when it is absent — so a freshly created tenant can never
  import data, only the bootstrapped primary tenant can.
- **The error message misdirects.** It instructs the operator to run
  `portfoliflow bootstrap`. For any non-primary tenant this is a no-op
  (bootstrap targets `PRIMARY_TENANT_ID` only) and does not repair the
  affected tenant, sending the operator down a dead end.

A further functional gap: the Phase-7 default asset classes back the
AnlV classification and the Anlagegrenzen / limit-set surfaces
(ADR-0055–0060). Without them, a `create-tenant` tenant behaves
differently from the primary tenant in exactly those surfaces, which
undermines demo fidelity and, as the project approaches productive
operation, correctness for real tenants.

This was surfaced while provisioning a dedicated screenshot tenant
("Minathena Capital") and attempting an Excel import against it.

## Decision

`seed_tenant_defaults` is the **authoritative per-tenant default-seed
routine** for every tenant provisioned at runtime, and MUST install the
same per-tenant catalogue that `bootstrap` installs for the primary
tenant. Concretely, `seed_tenant_defaults` is extended to additionally
install, in tenant-scoped, independently-committing steps mirroring the
bootstrap isolation pattern:

1. The `unclassified` asset class, via
   `cli.bootstrap.install_unclassified_asset_class` — restoring the
   ADR-0043 invariant for all tenants.
2. The Phase-7 default asset-class catalogue, via
   `cli.bootstrap.install_default_asset_classes` — restoring AnlV /
   Anlagegrenzen parity.

Both installers already operate on the active tenant (`app.tenant_id`)
and are idempotent on the asset-class code, so they compose cleanly with
re-runs. The new steps run in the order bootstrap uses (unclassified
first, then the default catalogue) so the fallback row is always present
even if the catalogue is later edited.

Because `create-tenant` calls `seed_tenant_defaults` unconditionally
after the (idempotent) tenant-creation transaction, **re-running
`portfoliflow create-tenant` with the same `--subdomain` repairs an
already-existing under-seeded tenant idempotently.** This is the
sanctioned, CLI-driven, idempotent remedy — consistent with the
project's hard "no manual SQL fixes" principle.

The importer's error message in
`InvestmentService.transform_upload_to_investments` is corrected to stop
recommending `portfoliflow bootstrap` and instead point to the accurate
remedy (re-run `portfoliflow create-tenant --subdomain <subdomain> …` to
reinstall the tenant's default seeds).

**Non-goal (deferred):** Collapsing the two provisioning paths so that
`bootstrap` itself calls `seed_tenant_defaults` (true single-source DRY).
`bootstrap` carries primary-/system-tenant-specific logic (fixed UUIDs,
drift detection, super-admin creation) that makes this a larger refactor
with its own risk surface. It is recorded here as a candidate for a
future ADR, not undertaken now (YAGNI on the larger refactor; the
parity defect is fixed by the minimal shared-installer reuse above).

## Consequences

**Positive**
- The ADR-0043 `unclassified` invariant holds for every tenant, not just
  the bootstrapped primary tenant.
- Excel import works on any `create-tenant`-provisioned tenant out of the
  box; the screenshot/demo tenant path is unblocked.
- AnlV / Anlagegrenzen surfaces behave identically across tenants.
- The remedy is a clean, idempotent, CLI-driven re-run — no manual SQL,
  no schema surgery.
- The misleading error message no longer sends operators to a dead end.

**Negative / costs**
- Two provisioning paths still install the same catalogue via different
  call sites (bootstrap's `SENTINEL_TENANT_ID` wrappers vs.
  `seed_tenant_defaults`'s direct installer calls). The shared underlying
  installer functions bound the duplication, but full single-source
  consolidation is deferred (see Non-goal).
- A small, deliberate `services → cli` import remains
  (`seed_tenant_defaults` importing from `cli.bootstrap`), extending the
  pre-existing pattern used for `install_default_regions`. Mitigated by a
  function-scope import to avoid the load-time circular dependency.

**Neutral**
- No schema change, no migration, no API change. Behavioural change is
  limited to the contents of a newly provisioned tenant and one
  corrected error string.

## Alternatives Considered

- **Minimal fix — install only the `unclassified` asset class.**
  Rejected (Option A in deliberation). It unblocks the import but leaves
  `create-tenant` tenants diverging from the primary tenant on the
  default asset-class catalogue, i.e. silently different Anlagegrenzen /
  AnlV behaviour. Unacceptable as the project approaches productive
  operation.
- **Document the manual remedy and leave the seed path as-is.** Rejected:
  it institutionalises a per-tenant defect and would, in practice, invite
  manual SQL — a violation of a hard project principle.
- **Make `bootstrap` call `seed_tenant_defaults` (single source now).**
  Rejected for this change as scope creep over a defect fix; recorded as
  a deferred non-goal.

## Compliance & Audit Relevance

The change strengthens control consistency: every provisioned tenant now
receives an identical, fixture-backed default catalogue, which is the
basis for the AnlV classification and Anlagegrenzen limit checks. Tenant
provisioning remains entirely CLI-/service-driven and auditable (the
super-admin audit row is written in the tenant-creation transaction per
ADR-0064; seed steps are best-effort and logged). No customer data,
authentication, or authorisation behaviour changes. The corrected
operator-facing error message improves operational transparency by
naming the correct, idempotent remedy.

## Revision History

- 2026-06-05 — Initial version. Records the decision to bring
  `seed_tenant_defaults` to full per-tenant seed parity with `bootstrap`
  (unclassified asset class + Phase-7 default asset-class catalogue),
  restore the ADR-0043 invariant for all tenants, and correct the
  importer's misleading remedy message. Status: Accepted.
