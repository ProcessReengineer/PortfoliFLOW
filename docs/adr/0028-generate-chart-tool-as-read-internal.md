# ADR-0028: `generate_chart` Tool as `READ_INTERNAL` — Member Extension to ADR-0012

- **Status:** Accepted
- **Date:** 2026-04-27
- **Deciders:** PortfoliFLOW project owner
- **Tags:** integration, security, ui

---

## Context

ADR-0012 introduced the ToolRegistry and listed three DataStore-reading tools
(`list_datasets`, `get_dataset_summary`, `get_dataset_slice`) as the initial
members. ADR-0022 introduced the four tool-trust classes (`READ_INTERNAL`,
`WRITE_INTERNAL`, `READ_EXTERNAL_UNTRUSTED`, `EXTERNAL_EFFECT`) and required
that every tool registration declare its class explicitly.

A new tool, `generate_chart`, has been added under
`services/tools/chart_tools.py`. It produces themed matplotlib charts (line,
bar, grouped_bar, scatter, pie, donut) from data that already lives in the
DataStore (or, alternatively, from inline data the agent supplies in the same
turn) and returns the rendered chart as a Base64 PNG envelope that the
streaming worker detects and surfaces in the chat as an image bubble.

This is intentionally a short ADR. It is a **member extension** to ADR-0012
under the trust taxonomy of ADR-0022, not a re-decision of either. The
audit-relevant question is exclusively "which trust class does this new
tool inhabit, and why?".

## Decision

The `generate_chart` tool is registered with class `READ_INTERNAL` via
`ToolRegistry.register_tool(..., tool_class=ToolClass.READ_INTERNAL)`. The
registration call is verifiable in `services/tools/chart_tools.py`.

The tool's inputs are entirely under the user's control:

- **DataStore data path** (when `data_source="datastore"`): reads a
  named DataFrame from `get_data_store()` — the same data source already
  served by the original `READ_INTERNAL` tools of ADR-0012.
- **Inline data path** (when `data_source="inline"`): consumes JSON the
  agent assembled inside the same turn, sourced from prior tool outputs
  or from text the user already provided.
- **Chart theme**: `core/chart_theme.py` reads the active theme JSON
  (ADR-0021) from `config/`.

The tool produces only a chart image returned to the agent; it does not
mutate state and does not reach the open internet. By the definitions in
ADR-0022 the appropriate class is therefore `READ_INTERNAL`. The tool
inherits all ADR-0012 properties (single-seam registration, exception
catch in `execute_tool`, bounded tool-execution loop) and all ADR-0022
properties of `READ_INTERNAL` (no gating implications: can run alongside
`WRITE_INTERNAL` and `EXTERNAL_EFFECT` tools in the same turn; not
locked by a prior `READ_EXTERNAL_UNTRUSTED` call within the turn).

## Rationale

- **No external read.** Neither code path fetches anything from the
  network or from the filesystem outside `config/` (the chart theme
  is treated as part of the trusted application configuration).
- **No state mutation.** The function returns a chart artefact; it
  does not write to the DataStore, to disk, or to any external system.
- **Same data trust level as the existing `READ_INTERNAL` tools.** The
  DataStore content is the user's already-loaded portfolio data;
  rendering it is a strictly weaker action than reading it, which is
  already classified `READ_INTERNAL`.

## Alternatives Considered

- **Classify as `WRITE_INTERNAL`.** Rejected. The tool does not
  mutate state. Treating chart rendering as a write would dilute the
  meaning of `WRITE_INTERNAL` and would make the tool subject to
  unnecessary gating (it would be locked after a
  `READ_EXTERNAL_UNTRUSTED` call within the same turn — which has no
  relationship to its actual risk).
- **Classify as `READ_EXTERNAL_UNTRUSTED`.** Rejected. Neither the
  DataStore read path nor the inline-JSON path is external. The
  trust-marked delimiter mechanism for untrusted content does not
  apply; routing the output through it would mislead the agent
  about what the chart actually represents.

## Consequences

Minor. The tool inherits ADR-0012 / ADR-0022 properties; there are no
gating implications. CLAUDE.md and ADR-0012 cross-references are updated
to name the tool and its class. No new operational concerns.

## Implementation Notes

- Tool function and registration: `services/tools/chart_tools.py`
  (`generate_chart`, registered at module import time with
  `tool_class=ToolClass.READ_INTERNAL`).
- Package wiring: `services/tools/__init__.py` documents the current
  tool modules — `datastore_tools`, `chart_tools`, `web_research_tool`.
- Active chart theme: `core/chart_theme.py` (ADR-0021).
- Tests: `tests/assistants/test_chart_tools.py`.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Functional Suitability
  (the agent can render visualisations of user data on demand);
  Maintainability (registration follows the same single-seam pattern
  as every other tool).
- **Regulatory references:** Low. This is a member-extension ADR
  inheriting the audit posture of ADR-0012 / ADR-0022; it adds no new
  external-data path and no new write authority.
- **Audit evidence:** The `register_tool(..., tool_class=
  ToolClass.READ_INTERNAL)` call site at the bottom of
  `services/tools/chart_tools.py` is the reviewable artefact; the
  tool list returned by `ToolRegistry.list_tools()` exposes the
  declared class at runtime for inspection.

## References

- ADR-0012 (ToolRegistry — the registration mechanism this tool uses)
- ADR-0022 (Tool Trust Classes and Gating Policy — the class
  taxonomy this tool's classification follows)
- ADR-0021 (Chart theming — the visual parameters consumed by the
  builder; same theme used by the in-app reporting engine of
  ADR-0026)

---

## Revision History

| Date       | Author                       | Change        |
|------------|------------------------------|---------------|
| 2026-04-27 | PortfoliFLOW project owner   | Initial draft. Records the addition of `generate_chart` to the ToolRegistry under class `READ_INTERNAL`. Code already implemented and in use. |
