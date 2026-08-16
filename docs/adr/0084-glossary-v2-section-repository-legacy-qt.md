# ADR-0084: Glossary v2 — Section and Repository as First-Class Terms; Widget/Panel Demoted to Legacy Qt

- **Status:** Accepted
- **Date:** 2026-07-01
- **Deciders:** PortfoliFLOW project owner
- **Tags:** process, architecture, glossary, web-migration

---

## Context

ADR-0002 fixed a seven-term, GUI-centric canonical glossary — *Area /
Module / Feature / Function / Widget / Panel / Service* — in which "one
Area = one Panel in the GUI". That vocabulary described the PyQt6 desktop
application faithfully at the time it was written (2026-04-24).

Since then the system has been rebuilt around a different surface. The
web migration (ADR-0033, ADR-0037) made a FastAPI/Jinja2/HTMX web
application the primary surface, and ADR-0058 introduced its information
architecture — sidebar plus long-scroll Areas subdivided into
anchor-addressable **Sections**. The persistence work (ADR-0018,
ADR-0034, ADR-0035, ADR-0041) introduced the async, tenant-scoped
data-access layer under `core/repositories/` — the **Repository**. The
multi-tenant activation (ADR-0063, ADR-0064) added a whole Tenant term
family (Tenant, Primary/System Tenant, Super-admin, Tenant role, Tenant
Resolver), and the AI-tool work (ADR-0012, ADR-0022) added the
Tool-trust vocabulary. Meanwhile **Widget** and **Panel** — both defined
by ADR-0002 in terms of `QWidget` and the Qt panel container — are now
confined to the sunset `gui/` tree (roadmap B2); the web variant renders
Section partials directly and has no Panel construct.

The result is a documentation conflict. `CLAUDE.md` (the glossary loaded
into every AI session) and `docs/architecture.md` (the canonical
narrative) already carry the evolved vocabulary: Section and Repository
are first-class, Widget/Panel are marked "Legacy Qt". But the *ADR of
record* for the glossary — ADR-0002 — still fixes the old seven terms
with "Area = one Panel in the GUI". Under the project's standing "where a
rule conflicts, the ADR wins" doctrine, the authoritative record now
points at a definition that no longer describes the system as built. That
is precisely the drift ADR-0002 was written to prevent, only now the
stale source is the glossary ADR itself.

This decision is audit-relevant: canonical terminology is the shared
vocabulary that keeps prompts, reviews, and generated code aligned with
the architecture (ISO 25010 analysability). Its evolution must be
recorded deliberately, not applied silently by editing prose while the
decision record lags.

## Decision

PortfoliFLOW supersedes ADR-0002. The canonical glossary is the extended
table maintained in **`CLAUDE.md`** ("Glossary — canonical
terminology"), mirrored in narrative form in `docs/architecture.md`
("Canonical terminology"). Specifically:

1. **Section** (ADR-0058) and **Repository** (ADR-0018/0034/0035/0041)
   are first-class canonical terms. A Section is a long-scroll
   subdivision of an Area's web page, addressable by anchor
   (e.g. `/front-office#charts`). A Repository is an async, tenant-scoped,
   audit-aware data-access class under `core/repositories/`.
2. **Widget** and **Panel** are retained **only as legacy Qt terms**,
   scoped to the `gui/` tree. They are not used to describe the web
   variant, which renders Section partials from
   `_partials/areas/<area>_body.html` directly.
3. The **Tenant term family** (Tenant, Primary Tenant, System Tenant,
   Super-admin, Tenant role, Tenant Resolver — ADR-0063, ADR-0064) and
   the **Tool-trust vocabulary** (ToolRegistry, Tool Trust Class —
   ADR-0012, ADR-0022) are part of the canonical set.
4. The **five core terms — Area, Module, Feature, Function, Service —
   are unchanged in meaning** from ADR-0002. The change is additive, plus
   the Widget/Panel demotion, plus the removal of the "Area = one Panel
   in the GUI" identity: an Area now maps to one directory under
   `modules/` and one URL `/{area-name}` in the web surface; the Panel
   identity is legacy.
5. **Source-of-truth precedence is unchanged.** `CLAUDE.md` is the
   authoritative glossary for AI-runtime use; `docs/architecture.md` is
   the canonical narrative; where the two differ on a *rule*, `CLAUDE.md`
   wins. This ADR does not alter that doctrine; it re-points the
   "ADR wins" pointer at the current, correct table rather than the stale
   seven-term one.

ADR-0002 stays in the repository unchanged (apart from its status line
and a Revision-History supersede row) as the historical record of the
original Qt-era glossary.

## Rationale

- **Keep the glossary matched to the code as built.** The whole value of
  a canonical glossary is that it describes the actual system. A glossary
  ADR that still says "Area = one Panel in the GUI" for a web-first
  product actively misleads AI prompts and code review — the failure mode
  ADR-0002 was created to prevent.
- **Make the terminology evolution auditable.** The web migration and the
  persistence/multi-tenant work already changed the vocabulary in the
  prose docs. Recording that change as a superseding ADR turns a silent
  drift into a deliberate, traceable decision.
- **Preserve ADR-0002 intact.** Per the ADR immutability principle
  (`docs/adr/README.md`, *Lifecycle*), an accepted ADR is not rewritten;
  a successor supersedes it and the original stays as the point-in-time
  record. The seven-term Qt glossary remains a faithful description of the
  desktop era.
- **Single home for the canonical table.** `CLAUDE.md` is already loaded
  into every AI session; keeping the authoritative glossary there (rather
  than introducing a new file) means the vocabulary the assistants read
  is the vocabulary of record.

## Alternatives Considered

- **Edit ADR-0002 in place** to add Section/Repository and demote
  Widget/Panel. Rejected: it violates the ADR immutability principle
  (ADRs are point-in-time records; superseding, not rewriting, is the
  mechanism for a changed decision) and would erase the evidence that the
  vocabulary changed with the web migration.
- **Leave ADR-0002 canonical and only patch the prose docs.** Rejected:
  this is the current, defective state. It leaves the "ADR wins" rule
  pointing at a stale definition, so any conflict between the prose and
  the ADR resolves *against* the correct vocabulary.
- **Introduce a standalone `docs/glossary.md` as the source of truth.**
  Rejected: it adds a third place terminology can live and drift.
  `CLAUDE.md` is already the glossary the AI assistants read every
  session and is the natural, single home; ADR-0002's own follow-up note
  anticipated mirroring the glossary rather than relocating it.

## Consequences

### Positive

- The documentation set and the code-as-built are re-aligned: Section and
  Repository are canonical, matching ADR-0058 and the repository layer.
- The "ADR wins" conflict rule now resolves in favour of the current
  vocabulary instead of the stale Qt-era one.
- The terminology change is auditable: the ADR-0002 → ADR-0084 supersede
  chain is the evidence that the glossary evolved deliberately.

### Negative

- Contributors must treat **Widget** and **Panel** as legacy-only terms,
  valid only when discussing the `gui/` tree. Using them for the web
  variant is now a terminology error rather than merely imprecise.
- The canonical glossary is larger than the original seven terms;
  onboarding reads a longer table (offset by the table matching reality).

### Neutral / Follow-ups

- If a future non-Qt UI construct is introduced (a new web-side structural
  concept beyond Area/Section), it gets its own successor ADR rather than
  an in-place edit here.
- No code, schema, or API change follows from this ADR — it governs
  vocabulary and documentation only.

## Implementation Notes

- Canonical glossary table: `CLAUDE.md`, section "Glossary — canonical
  terminology" (already carries Area, Section, Module, Feature, Function,
  Service, Repository, the Tenant family, and the Tool-trust terms).
- Canonical narrative: `docs/architecture.md`, "Canonical terminology"
  (already marks Widget and Panel as "Legacy Qt — see §Legacy").
- Superseded record: `docs/adr/0002-canonical-glossary.md` — status set to
  "Superseded by ADR-0084", body preserved.
- ADR index: `docs/adr/README.md` — 0084 row added, 0002 row status
  updated, next-free number advanced to 0085.
- `readme.md` glossary sentence updated to name the five core terms plus
  Section and Repository as first-class and Widget/Panel as legacy Qt,
  pointing at `CLAUDE.md` as canonical.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Maintainability
  (analysability — a shared, current vocabulary lowers the cognitive load
  of code review and keeps generated code in the right layer/Area).
- **Audit evidence:** the ADR-0002 → ADR-0084 supersede chain, recorded
  in both ADRs' Revision History and in the ADR index, is the traceable
  evidence that the canonical terminology changed deliberately with the
  web migration rather than by silent drift (BAIT/VAIT change-management
  traceability).

## References

- ADR-0002 — Canonical Glossary (superseded by this ADR)
- ADR-0058 — Web Information Architecture (defines Section)
- ADR-0018 / ADR-0034 / ADR-0035 / ADR-0041 — service/repository layering
  and Postgres persistence (define Repository)
- ADR-0063 / ADR-0064 — multi-tenant activation and super-admin surface
  (Tenant term family)
- ADR-0012 / ADR-0022 — ToolRegistry and Tool Trust Classes (Tool-trust
  vocabulary)
- ADR-0033 / ADR-0037 — web migration (PyQt6 desktop → FastAPI web) that
  motivated the vocabulary change
- ADR-0074 — Product Scope (institutional portfolio management framing)

---

## Revision History

| Date       | Author | Change        |
|------------|--------|---------------|
| 2026-07-01 | PortfoliFLOW project owner | Initial draft; accepted. Supersedes ADR-0002 — glossary evolved for the web variant (Section, Repository first-class; Widget/Panel demoted to legacy Qt; "Area = one Panel" identity removed). |
