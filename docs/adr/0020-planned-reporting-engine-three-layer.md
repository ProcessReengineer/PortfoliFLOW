# ADR-0020: Planned Reporting Engine — Three-Layer Design (Data / Template / Style)

- **Status:** Proposed
- **Date:** 2026-04-24
- **Deciders:** PortfoliFLOW project owner (retrofit)
- **Tags:** architecture, ui, integration

---

## Context

PortfoliFLOW will generate LP-ready reports automatically (PDF and PowerPoint). Three concerns must coexist in any reporting system: (a) *what data* the report contains, (b) *how the data is structured* into a sequence of pages / sections, and (c) *how the result looks visually* (fonts, colours, logos — typically per-client branding). Mixing these concerns produces reports that are painful to retemplate, painful to rebrand, and painful to maintain when source data changes.

Without an explicit decomposition, every new client would require either a fork of the whole reporting code or pervasive `if client == ...` branching. Both are unacceptable for a fund-of-funds shop with multiple LPs.

## Decision

The PortfoliFLOW Reporting Engine will be designed as three independent layers that compose at render time:

1. **Data Layer** — pulls the data the report needs from the DataVault (ADR-0017) via Repositories (ADR-0018). Outputs typed DTOs / dataclasses per report type.
2. **Template Layer** — defines report structure: what sections appear, in what order, with what charts and tables. Templates consume DTOs from the Data Layer; they do not query persistence directly.
3. **Style Layer** — encapsulates per-client branding: fonts, colours, logos, page geometry. The same Template can be rendered with different Styles to produce visually distinct outputs from identical content.

Output formats: PowerPoint (`.pptx`) and PDF.

This ADR is `Proposed` because no Reporting Engine code exists yet; the three-layer split is the design constraint that future implementation work must respect.

## Rationale

- Three independent layers map cleanly to the three independent reasons reports change: source data evolves, report structure evolves, client branding evolves. Each can change without the others.
- Pulling data through Repositories (ADR-0018) rather than directly from DuckDB keeps the Reporting Engine portable across persistence changes.
- A separate Style Layer is the only way to support per-client branding without code duplication or template forks.
- Two output formats (PPTX, PDF) are required: PPTX for editable handover to client teams, PDF for final distribution.

## Alternatives Considered

- **Single monolithic templating layer (e.g., Jinja2 generating a complete document):** Rejected — entangles content, structure, and style; per-client variations become forks.
- **One template per client:** Rejected — duplicates structural changes across N templates whenever the report layout evolves.
- **Off-the-shelf BI reporting tool (Power BI, Tableau, etc.):** Rejected for in-application reporting; introduces an external dependency that is hard to brand and hard to embed in PortfoliFLOW's controlled distribution.
- **Output PDF only:** Rejected — client teams routinely want PowerPoint they can edit.
- **Output PowerPoint only, convert to PDF externally:** Implicitly rejected; uncontrolled conversion damages branding.

## Consequences

### Positive

- New clients add a Style; new report types add a Template; data changes touch only the Data Layer.
- The Style Layer is the single place to enforce brand consistency.
- Report unit tests can target each layer independently.

### Negative

- Three layers are more files than a monolithic template; the conceptual overhead is higher for very small reports.
- The three boundaries (DTO between Data and Template; Style application at render time) must be designed deliberately; getting them wrong produces leaks (e.g., colours hardcoded in templates).
- PowerPoint and PDF generation each have their own quirks; the renderer abstraction must hide those without losing fidelity.

### Neutral / Follow-ups

- Decide on the underlying libraries (`python-pptx`, `reportlab`, or alternatives) at implementation time.
- Decide on the location of Style assets (likely a per-client directory under `config/` or `data/`).
- Cross-reference with the planned chart-theme externalisation (ADR-0021) — chart styles can be a special case of Style assets.
- Reporting Engine implementation depends on DataVault (ADR-0017) and Repository layer (ADR-0018) being in place.

## Implementation Notes

- Not yet implemented.
- Documented in: `CLAUDE.md` ("Planned Feature Modules — Reporting Engine").
- Will live under: `modules/back_office/` and/or `modules/investor_communication/` (Module split to be decided), with a Service Layer per ADR-0018 and likely a renderer in `services/`.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Maintainability (modularity, modifiability — three-axis change without coupling), Reusability, Reliability (typed DTOs at the Data/Template boundary).
- **Audit evidence (once implemented):** Generated reports are reproducible from a given DataVault snapshot + Template + Style; reproducibility is a function of all three layers being deterministic.

## References

- ADR-0013 (Analytics layer pure and stateless — analytics outputs feed the Data Layer)
- ADR-0017 (Planned DataVault — source of report data)
- ADR-0018 (Planned Service / Repository layering — Reporting Engine consumes Repositories)
- ADR-0021 (Chart theming externalised to JSON — overlaps with Style Layer concerns)

---

## Revision History

| Date       | Author                                | Change                                                             |
|------------|---------------------------------------|--------------------------------------------------------------------|
| 2026-04-24 | PortfoliFLOW project owner (retrofit) | Initial draft. Retrofitted from "Planned Feature Modules" notes in `CLAUDE.md`; no implementation yet. |
| 2026-04-27 | PortfoliFLOW project owner            | Phase-1 implementation has been delivered separately as ADR-0026 (`Phase-1 Reporting Engine — In-App Multi-Tile Rendering`). ADR-0020 is **not** superseded; the three-layer Data/Template/Style design with PDF/PPTX export remains the long-term target. ADR-0026 documents the deliberate intermediate step. |
