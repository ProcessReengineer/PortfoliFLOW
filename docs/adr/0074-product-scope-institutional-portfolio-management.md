# ADR-0074: Product Scope — Institutional Portfolio Management Platform

- **Status:** Accepted
- **Date:** 2026-06-03
- **Deciders:** Soenke Pinkernelle
- **Implements roadmap item:** N/A (cross-cutting product-scope decision)
- **Tags:** product-scope, positioning, documentation, governance

---

## Context

PortfoliFLOW was originally framed as a tool for a fund-of-funds (FoF)
boutique — purpose-built for the back office of a small alternatives
allocator. Earlier decision records and definitional documents reflect
that framing (e.g. ADR-0001, ADR-0033, ADR-0067, ADR-0072, the `readme.md`
headline, the `CLAUDE.md` project description, and the `pyproject.toml`
metadata).

In practice the platform was built on layers that are not FoF-specific:
DB-free pure analytics (NAV time series, IRR, multiples, statistics,
mean-variance optimisation, efficient frontier), a multi-tenant data
model with row-level security, an Excel-driven import path, SAA, limits
(Anlagegrenzen), benchmarks & attribution, and investor communication.
These capabilities serve institutional portfolio management generally —
Versorgungswerke, family offices, endowments, and asset managers — of
which fund-of-funds and alternative investments are one important domain,
not the whole.

The product identity therefore lags the implemented reality. This ADR
records the repositioning so that future contributors, prompts, and
audit reviewers reason from the correct scope.

## Decision

PortfoliFLOW is positioned as an **AI-native platform for institutional
portfolio management**. Its scope explicitly **includes, but is not
limited to**, fund-of-funds and alternative-investment mandates.

Concretely:

1. Forward-facing identity statements (`readme.md`, `CLAUDE.md`,
   `pyproject.toml`) describe the product as serving institutional
   investors broadly, with FoF/alternatives named as an included case.
2. This ADR is the **canonical, authoritative statement of product
   scope** and acts as the arbiter where older documents still carry the
   narrower FoF-boutique framing.
3. Pre-existing **Accepted ADRs are not retro-edited.** Their `Context`
   and `Decision` sections remain historically accurate as written. Where
   an older ADR's target-audience framing is materially broadened by this
   decision (notably ADR-0067 and ADR-0072), a non-destructive note may be
   appended to that ADR's Revision History pointing to ADR-0074; the body
   is left untouched.
4. Domain data values that legitimately reference FoF structures (e.g. the
   AnlV asset-class taxonomy in `anlv_categories.json`) are unaffected —
   "fund-of-funds" remains a valid asset class within a broader scope.

## Consequences

**Positive**
- Documentation matches the implemented capability surface; no overclaim
  and no underclaim (consistent with the project's honest-claims posture).
- A single authoritative scope record exists for onboarding, audits, and
  go-to-market / positioning materials.
- Broadens the addressable framing (Versorgungswerke, family offices,
  endowments, asset managers) without requiring code changes.

**Negative / costs**
- Some older ADRs retain narrower phrasing in their bodies; readers must
  understand that ADRs are point-in-time records and that ADR-0074 governs
  current scope. This is the intended trade-off in favour of audit
  integrity over a rewritten history.

**Neutral**
- No runtime behaviour, schema, or API change. The change is to identity
  documentation and a small number of over-narrow docstrings/comments.

## Alternatives Considered

- **Retro-edit the framing in all affected ADRs.** Rejected: rewriting
  accepted ADRs to match later thinking destroys the historical record
  that ADRs exist to preserve, and undermines audit traceability.
- **Leave documentation as-is and rely on tribal knowledge.** Rejected:
  the gap between documented FoF-boutique framing and the institutional
  capability surface would mislead onboarding, audits, and positioning.
- **Reposition exclusively as an "alternatives" platform.** Rejected: the
  analytics and back-office layers (statistics, SAA, limits, benchmarks)
  apply to institutional portfolio management beyond alternatives; an
  alternatives-only label would still understate the scope.

## Compliance & Audit Relevance

This ADR is governance- and audit-relevant in that it establishes the
authoritative product-scope statement and the principle that prior ADRs
are not rewritten. It strengthens, rather than weakens, the audit trail:
the historical framing is preserved, and the current framing is recorded
with its rationale. No regulatory claim changes; resilience and control
framings stated elsewhere (e.g. DORA-aligned wording) are unaffected.

## Revision History

- 2026-06-03 — Initial version. Records the repositioning from
  fund-of-funds boutique tool to institutional portfolio management
  platform (incl. FoF and alternatives). Status: Accepted.
