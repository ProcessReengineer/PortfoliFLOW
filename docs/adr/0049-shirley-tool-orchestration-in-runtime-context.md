# ADR-0049: Shirley Tool-Orchestration Guidance in a Runtime-Appended Context File

- **Status:** Accepted
- **Date:** 2026-05-15
- **Deciders:** PortfoliFLOW project owner
- **Tags:** integration, ui, process

---

## Context

Phase-6 smoke testing of Shirley on the PortfoliFLOW web chat surface
showed that the two-axis chart pipeline introduced by ADR-0048 works
mechanically — `get_investment_data` returns a structured-data
envelope with a handle, `render_chart` consumes that handle and
renders a Plotly figure, and the SSE round-trip produced by ADR-0047's
tool-execution context propagation lights up the chart in the chat
surface. The handles are honoured, the column metadata is correct, the
rendered chart is the expected one.

What is *not* reliable is Shirley's *use* of the pipeline. Across
fresh sessions she frequently:

- asks a clarifying question on prompts that already specify a
  catalogue investment name and a chart subject ("which data did you
  mean?") rather than executing the two-step
  `get_investment_data → render_chart` flow;
- apologises for architectural facts as if they were limitations
  ("the data is in the investment database, not in the DataStore" —
  phrased as a problem rather than the intended web design per
  ADR-0041);
- fabricates non-existent tool features (claiming `render_chart`
  takes a `date_range` argument and falling back to inline-data
  construction it cannot actually do).

The diagnostic anchor is that none of these failures involve broken
tool calls. The tools are correct. The tool *descriptions* are
correct. What is missing is system-prompt guidance about how the
tools *fit together* on the web variant — that the Postgres-native
investment tools are the canonical web-side path, that
`get_investment_data → render_chart` is a self-driven two-step flow
expected to execute without intermediate confirmation, that
`render_chart` deliberately has no date filter, that the in-memory
DataStore tools are empty by design on the web variant.

`docs/Soul_Shirley.md` is the canonical identity-and-tone surface.
Its own *Evolution path* note explicitly says that runtime-appended
sections (dynamic tool listings, dataset context, user preferences)
should be appended programmatically rather than edited into the Soul
file. The orchestration layer fits squarely into that note's
intention.

`AIServiceCore.get_system_prompt` already has the extension seam:
it concatenates the Soul-file prompt with each entry in a hardcoded
`context_files` list, with a per-file existence check that gracefully
skips missing files. Today the list contains exactly one entry,
`Shirley_AnalysisResults_Context.md`. Adding a second entry is a
one-line change at the documented extension point.

## Decision

Shirley's tool-orchestration guidance lives in a runtime-appended
context file, **`docs/Shirley_ToolOrchestration_Context.md`**, that
`AIServiceCore.get_system_prompt` reads and concatenates onto the
Soul-file prompt with the literal `"\n\n"` separator. The file is
registered in the `context_files` list inside `get_system_prompt` and
is the second entry — `Shirley_AnalysisResults_Context.md` precedes
it, the Soul-file body precedes both.

`docs/Soul_Shirley.md` is **not** modified by this change. It remains
the user-presentable identity-and-role canon. The orchestration file
is mechanics — how Shirley *uses* her tools — and does not belong in
the Soul.

## Rationale

- **The Soul file's own design says so.** The *Evolution path* note
  in `Soul_Shirley.md` explicitly defers dynamic per-runtime sections
  (tool listings, dataset context, preferences) to programmatic
  appending. Tool-orchestration guidance is the same kind of
  cross-cutting runtime context.
- **The seam already exists, with the right semantics.** The
  `context_files` list and the per-file existence check in
  `get_system_prompt` already deliver insertion-ordered concatenation
  with graceful degradation for missing files. The cost of using it
  is one line.
- **Identity stays user-presentable.** The Soul file is the artefact
  shown in the *About Shirley* surface (and is the one a non-technical
  reader is most likely to see). Loading it with web-variant
  mechanics would dilute its readability without serving the Soul's
  purpose.
- **Mechanics evolve faster than identity.** The orchestration text
  is expected to be revised as bundles are added (portfolio
  aggregates, analysis bundles) and as multi-turn history lands.
  Keeping it in a separate file makes those revisions reviewable in
  isolation from the identity canon.

## Alternatives Considered

- **Edit `Soul_Shirley.md` directly.** Rejected because the Soul
  file's own *Evolution path* note explicitly forbids it, because the
  Soul is the user-presentable identity surface and should stay
  minimal, and because mechanics churn faster than identity.
- **Generate a dynamic tool listing at request time** (the longer-arc
  evolution the Soul file's note also mentions). Deferred, not
  rejected. The dynamic-listing direction is orthogonal to this
  decision: it would render *which tools exist*, not *how to chain
  them*. The orchestration context is the chain-and-conventions layer
  and is needed regardless of whether the listing is static or
  dynamic. This ADR establishes only the static-context extension
  point.
- **Embed the orchestration guidance into each tool's `description`
  field.** Rejected because cross-tool flow (the
  `get_investment_data → render_chart` chain, the "no date filter
  exists, frame the period in prose" convention) does not belong on a
  single tool's description and would either fragment across many
  tools or be duplicated.

## Consequences

### Positive

- Shirley's web-variant behaviour gets a documented orchestration
  layer without modifying the Soul file or any tool code.
- The mechanics layer is reviewable in isolation from identity and
  from tool implementations — three distinct surfaces for three
  distinct kinds of change.
- The pattern is reusable: future cross-cutting guidance (multi-turn
  conventions, dataset context, per-user preferences) can be added
  by appending another entry to the `context_files` list, with the
  same graceful-degradation semantics.

### Negative

- The composed system prompt is now assembled from three files
  (Soul + two context files) rather than one. A reader reasoning
  about what Shirley sees has to read all three. The order is fixed
  by the `context_files` list, and the test suite pins it.
- The orchestration file's "One turn at a time" paragraph is
  conversation-history-aware and therefore couples the file's
  content to a specific phase of the web variant. See
  *Neutral / Follow-ups* below.

### Neutral / Follow-ups

- **Multi-turn history revisit.** The "One turn at a time" paragraph
  in `Shirley_ToolOrchestration_Context.md` exists because the
  Phase-6 web variant currently has no conversation history.
  When the next prompt in the Phase-6 Shirley stream lands
  multi-turn history on the web surface, that paragraph must be
  removed (or reframed as positive guidance about how to use prior
  turns).
- **Deferred — dynamic tool listing.** The Soul file's *Evolution
  path* note also mentions a dynamic, runtime-generated tool
  listing. That direction remains deferred; this ADR establishes
  only the static-context extension point.

## Implementation Notes

- New file: `docs/Shirley_ToolOrchestration_Context.md` — the
  orchestration text appended to the Soul prompt at runtime.
- Modified: `services/ai_service_core.py` — one entry added to the
  `context_files` list inside `get_system_prompt`. The existing
  `if ctx_path.exists()` and `if ctx_text:` guards apply unchanged.
- Tests: `tests/characterization/test_ai_service_core.py` —
  three assertions pinning (a) the three-file concatenation order,
  (b) graceful fallback when the new file is absent, and (c) no
  trailing-separator addition when the file is empty.
- Not modified: `docs/Soul_Shirley.md`, every tool module under
  `services/tools/`, `web/routes/chat.py`, every chat template /
  static asset.

## References

- Related ADRs:
  - ADR-0048 (two-axis chart architecture) — the architectural
    context the orchestration guidance documents the user-facing
    surface of.
  - ADR-0047 (tool-execution context propagation) — the seam through
    which the Postgres-native tools become reachable on the web
    variant.
  - ADR-0041 (persistence entry-points strangler coexistence) — the
    reason the in-memory DataStore is empty on the web variant by
    design.
  - ADR-0011, ADR-0038 — the AIService core that owns the
    `get_system_prompt` extension point.
- `docs/Soul_Shirley.md` *Evolution path* note — the design
  intention this ADR realises for the orchestration layer.

---

## Revision History

| Date       | Author                     | Change        |
|------------|----------------------------|---------------|
| 2026-05-15 | PortfoliFLOW project owner | Initial draft (Accepted) |
| 2026-05-20 | PortfoliFLOW project owner | Maintainer name anonymised per project convention. |
