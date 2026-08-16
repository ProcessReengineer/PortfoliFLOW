# ADR-0000: Retrofit Report

- **Status:** Informational (not a decision)
- **Date:** 2026-04-24
- **Author:** PortfoliFLOW project owner (retrofit, AI-assisted)
- **Tags:** process, meta

---

## Summary

On 2026-04-24, twenty-one Architecture Decision Records (ADR-0001 through ADR-0021) were retrofitted onto the PortfoliFLOW codebase. The goal of the retrofit was to lift architectural decisions that had already been made — and were documented informally in `CLAUDE.md`, `docs/architecture.md`, and source-file docstrings — into a formal, auditable ADR format. No new decisions were taken; no code outside `docs/adr/` was modified. Each ADR includes a *Revision History* note stating that it was retrofitted from existing documentation and code on 2026-04-24, and that the underlying decision predates this ADR.

## Method

Sources read in full before drafting:

- `docs/adr/README.md` and `docs/adr/template.md` — the ADR process and structure.
- `CLAUDE.md` — primary source for conventions, the canonical glossary, dependency rules, the planned-architecture section, and the AI-collaboration workflow.
- `docs/architecture.md` — architectural rationale.
- `docs/module_spec_template.md` — module contract.
- `readme.md` — product framing and project structure.
- `pyproject.toml` — dependency choices (themselves decisions).
- Implementation: `core/base_module.py`, `core/data_store.py`, `core/exceptions.py`, `core/config.py`, `modules/module_registry.py`, `services/ai_service.py` (top-of-file architecture note read), `services/tool_registry.py`, `analytics/portfolio_optimizer.py`, `modules/front_office/data_import.py` (header), `gui/main_window.py` (header).
- `git log` — to confirm Conventional Commits is observed in practice.
- `config/chart_theme.json` — to confirm the chart-theme externalisation is real and structured.

Each ADR was authored by extracting the decision from those sources, restating the problem in neutral language, recording rationale already present in `CLAUDE.md` / `docs/architecture.md` in ADR voice, and adding an *Alternatives Considered* section. Where alternatives were not documented, the most plausible alternative was named and explicitly marked as "implicitly rejected — not formally evaluated"; sophisticated alternatives that were not actually considered were not fabricated.

File-path references inside ADRs were verified against the current working tree.

## Seed list deviations

The seed list contained 20 candidate ADRs. The retrofit produced 21 ADRs, with the following changes from the seed list:

- **No splits.** Each seed-list entry mapped to a single ADR.
- **No merges.** Each seed-list entry remained its own ADR. ADR-0019 (Multi-User readiness) and ADR-0017 (DataVault) are tightly coupled but kept separate as recommended in the prompt: they could change independently (multi-user might never happen; the audit fields would still be valuable for compliance).
- **No drops.** All 20 seed entries became ADRs.
- **No renames** beyond minor wording in titles to fit the kebab-case file-name convention.

## Additional ADRs

One ADR was added beyond the seed list:

- **ADR-0021 — Chart Theming Externalised to JSON.** The decision is real, embodied in `config/chart_theme.json`, `core/chart_theme.py` / `core/chart_helpers.py`, and is referenced explicitly in `docs/architecture.md` ("Cross-cutting services — Chart theming"). It also overlaps conceptually with the per-client Style Layer in the planned Reporting Engine (ADR-0020), which made it useful to capture in the retrofit so the cross-reference exists.

## Gaps identified

The following decisions appear *not yet made* or *not yet documented* in any retrievable form. They are flagged here as candidates for future ADRs but were **not** themselves drafted in this retrofit (the retrofit's mandate is to formalise existing decisions, not to invent new ones).

1. **Authentication strategy.** Single-user today (ADR-0019). No design exists for what authentication will look like in any future multi-user mode (local accounts, OIDC / SSO, OS-integrated, …). Decide before any networked deployment.

2. **Authorisation / RBAC model.** No documented model of who can do what. Will become urgent as soon as multi-user is introduced; relevant earlier for AI-tool access control (ADR-0012 currently lets any user invoke any registered tool).

3. **Secrets management.** API keys for the AIService are stored in `QSettings` (per ADR-0010); `.env` is supported for general configuration. There is no stated policy on encryption at rest, key rotation, or which secrets are allowed where. Audit-relevant.

4. **Logging retention and audit-trail design.** `core/logging_setup.py` initialises logging, but retention, rotation, and the distinction between operational logs and an audit trail are not documented. The DataVault (ADR-0017) addresses provenance for *data*; a separate decision is needed for *actions* (who did what when).

5. **Data retention and deletion.** Once the DataVault (ADR-0017) is in place, GDPR-style and contractual retention rules will apply to LP and investment data. Retention policy is not documented today.

6. **Backup and disaster recovery.** The DataVault file is currently planned at `~/.portfoliflow/datavault.duckdb` (ADR-0017). No backup, restore, or DR procedure is documented. For institutional use this will need to be specified.

7. **Error reporting / telemetry policy.** No decision documented about whether errors are reported anywhere beyond local logs (e.g., Sentry, internal endpoint), nor about user consent for any such telemetry.

8. **Versioning and release strategy.** `pyproject.toml` currently pins version `0.1.0` and the `BaseModule.version` default is also `0.1.0`. No SemVer policy, release cadence, or compatibility commitment is documented.

9. **Licence choice.** `readme.md` says "Proprietary — all rights reserved." No `LICENCE` / `LICENSE` file exists in the repository. For institutional review and any third-party redistribution this should be made explicit.

10. **Input validation policy for externally-sourced data (scraped reports).** The Excel V2 importer (ADR-0009) has explicit validation. The planned Report Scraper will ingest GP reports of varying quality and AI-extracted content; the validation, sanitisation, and human-review policy for that pipeline is not yet documented.

11. **Reproducibility of analytics outputs.** `analytics/portfolio_optimizer.py` (ADR-0013) is pure, but consumer-side reproducibility (random seeds for `random_portfolios`, exact input snapshots for an optimisation run, ability to re-run a quarter-end calculation later) is not yet a documented end-to-end concern. Audit-relevant.

12. **Dependency / supply-chain policy.** `pyproject.toml` pins lower bounds only (e.g., `pandas>=2.0`). No lock file, no policy on updating dependencies, no SCA scanning. For institutional use, a policy will be needed.

13. **Configuration UI vs. `.env` boundary.** `docs/architecture.md` mentions a future admin "configuration UI" that edits `.env` values at runtime and refreshes the `Settings` singleton. The boundary between `.env`-managed config, `QSettings`-managed config (already used by AIService), and runtime-editable config is not yet decided.

## Suggested follow-up ADRs

In rough priority order, with one-line justification each:

- **Logging and audit-trail policy** — needed before DataVault (ADR-0017) writes start producing institutionally relevant records.
- **Secrets management policy** — already a real concern today (AIService API keys in `QSettings`).
- **Versioning / release strategy** — small now, expensive later.
- **Backup and disaster recovery for the DataVault** — required for institutional use.
- **Authentication and authorisation strategy** — write *before* any multi-user or networked deployment work begins (cross-references ADR-0019).
- **Reproducibility of analytics outputs** — clarify what an institutional auditor can re-derive and from what snapshot.
- **Input-validation and human-review policy for the planned Report Scraper** — write alongside the Report Scraper implementation; do not let the policy emerge implicitly from code.
- **Licence ADR** — formalises the proprietary-licence statement now in `readme.md`.

## Cross-document follow-ups (not edited in this run)

The constraint for this retrofit was to modify only files under `docs/adr/`. The following non-ADR edits are recommended for a separate change:

- Reference relevant ADRs from `CLAUDE.md` (e.g., the "Dependency rules" section could link ADR-0001 / ADR-0011; the "Glossary" could link ADR-0002).
- Reference ADR-0001 from `docs/architecture.md` ("Dependency rules" section).
- Reference ADR-0003 from the docstring of `core/base_module.py` and `modules/module_registry.py`.
- Reference ADR-0011 from the existing "Architecture note" comment at the top of `services/ai_service.py`.
