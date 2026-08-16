# ADR-0013: Analytics Layer — Pure, Stateless, No GUI or DataStore Dependencies

- **Status:** Accepted
- **Date:** 2026-04-24
- **Deciders:** PortfoliFLOW project owner (retrofit)
- **Tags:** architecture, analytics

---

## Context

PortfoliFLOW performs computational work that several different consumers need: GUI widgets (e.g., `PortfolioAnalysisWidget`) for interactive visualisation, business modules for back-office workflows, and AI tools (via the ToolRegistry, ADR-0012) for assistant-driven analysis. If the same computation lived inside a Module or a Widget, every other consumer would have to import that Module / Widget, dragging in DataStore, configuration, and GUI dependencies that have nothing to do with the computation.

A separate, pure layer for computational engines avoids this. It also makes the engines unit-testable in isolation and reusable in headless contexts (CLI, AI tool, future server-side execution).

## Decision

PortfoliFLOW has a dedicated `analytics/` layer for pure computational engines. Engines in this layer:

- Receive all inputs as constructor arguments (numpy arrays, plain numbers, dataclasses).
- Return structured result objects (frozen dataclasses).
- Are stateless across calls.
- Have **no GUI**, **no DataStore**, and **no configuration** coupling.
- Import only `core.exceptions` (and third-party numerical libraries).

The first engine is `analytics/portfolio_optimizer.py` (mean-variance optimisation built on `scipy.optimize.minimize`, returning `PortfolioResult` / using `PortfolioConstraints` / `GroupConstraint`). New analytics engines are added as new files under `analytics/` without touching existing files.

## Rationale

- The same computation is callable from a Module, a Widget, and an AI tool when it has no incoming dependency on any of them.
- Pure engines are unit-testable in isolation — no `QApplication`, no DataStore singleton reset, no config fixture required.
- Limiting `analytics/` to `core.exceptions` keeps the layer's import graph trivial and forces engines to surface errors through the shared exception hierarchy (ADR-0005).
- A future server-side or batch-execution context can import `analytics/` directly without dragging in PyQt6.

## Alternatives Considered

- **Embed analytics inside Modules:** Rejected — every other consumer (GUI widget, AI tool) would have to import the Module, contradicting the additive-extension philosophy and the no-sibling-imports rule.
- **Embed analytics inside Widgets:** Rejected — couples computation to PyQt6 and prevents headless / AI use.
- **Single shared utility module under `core/`:** Rejected — `core/` is reserved for infrastructure, not domain computation; mixing the two would erode `core/`'s minimal-and-stable property.
- **Use a third-party portfolio optimisation library wholesale (e.g., PyPortfolioOpt):** Implicitly rejected for the first engine; building on `scipy.optimize` directly gives full control over constraints (`PortfolioConstraints`, `GroupConstraint`) needed for fund-of-funds mandates. Not formally evaluated.

## Consequences

### Positive

- Single source of truth for each computation.
- Engines are unit-testable with plain pytest.
- Engines are AI-tool-callable (via ToolRegistry) without restructuring.
- Adding an engine is fully additive — no existing files change.

### Negative

- Callers must extract data from the DataStore and shape it into the engine's input types themselves. The engine cannot help with data plumbing.
- Two consumers passing slightly different inputs to the same engine can produce different outputs without anyone realising; consistency belongs to the caller.

### Neutral / Follow-ups

- Each new engine should follow the same shape: typed constructor, frozen result dataclass, no statefulness.
- A small `analytics/` README listing engines and their public APIs would help discoverability as the layer grows.

### Consumer-driven cost

**Consumer-driven cost.** Analytics functions remain pure and stateless. Where a function is expensive (e.g. `compute_rolling_irr_since_inception` runs Brent's method per NAV observation), the *aggregator* — not the analytics function itself — may expose an opt-out keyword. See `InvestmentService.get_charts_data(include_irr=...)` for the canonical pattern: keyword-only, default `True` for backward compatibility, explicit `False` at the call site where the cost is demonstrably wasted.

## Implementation Notes

- Layer: `analytics/`.
- First engine: `analytics/portfolio_optimizer.py` — `PortfolioOptimizer`, `PortfolioResult`, `PortfolioConstraints`, `GroupConstraint`.
- Consumers: `gui/widgets/portfolio_analysis_widget.py` (interactive frontier viz); `modules/back_office/saa.py` (Strategic Asset Allocation); intended for future AI tools via ToolRegistry.
- Documented in: `docs/architecture.md` ("Layer responsibilities — analytics/"), `CLAUDE.md` ("Portfolio Optimizer").

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Maintainability (modularity, reusability), Testability, Portability.
- **Audit evidence:** Imports inside `analytics/*.py` reference only `core.exceptions` and third-party packages — checkable by grep.

## References

- ADR-0001 (Layered architecture and strict one-way dependencies)
- ADR-0005 (Typed exception hierarchy — engines surface errors through it)
- ADR-0012 (ToolRegistry — exposes analytics engines as AI tools)

---

## Revision History

| Date       | Author                                | Change                                                             |
|------------|---------------------------------------|--------------------------------------------------------------------|
| 2026-04-24 | PortfoliFLOW project owner (retrofit) | Initial draft. Retrofitted from existing code and documentation; the original decision predates this ADR. |
