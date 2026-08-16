# ADR-0012: ToolRegistry as Single Seam for AI-Callable Tools

- **Status:** Accepted
- **Date:** 2026-04-24
- **Deciders:** PortfoliFLOW project owner (retrofit)
- **Tags:** integration, architecture

---

## Context

LLM-powered Features in PortfoliFLOW (current Shirley assistant; planned Report Scraper, DD Support, Portfolio Q&A) need to invoke application-side actions: list datasets, fetch a slice of data, render a chart, run an analytics computation, eventually trigger module-level actions. Each such action must be discoverable by the LLM (in OpenAI function-calling format), executable safely (a failing tool must not crash the chat loop), and registrable without modifying the AIService or the GUI.

Without a central registry, each tool would either be hardcoded into the AIService or scattered across modules with bespoke wiring. Adding a tool would touch the chat-completion call site every time, exactly the kind of structural change PortfoliFLOW's additive-extension philosophy (ADR-0016) is designed to prevent.

## Decision

PortfoliFLOW exposes a single `ToolRegistry` (`services/tool_registry.py`) as the seam between the AIService and AI-callable tools. Tools are plain Python functions registered via `registry.register_tool(name, function, description, parameters)`, where `parameters` is a JSON Schema object in OpenAI function-calling format and `function` returns a string.

The AIService:

1. Queries `ToolRegistry.get_tool_definitions()` to populate the `tools` parameter of each chat-completion request.
2. Dispatches model-emitted tool calls through `ToolRegistry.execute_tool(name, arguments)`.
3. Bounds the tool-execution loop at `_MAX_TOOL_ITERATIONS = 10` iterations per user message.

`ToolRegistry.execute_tool` deliberately catches every exception raised by a tool and returns it as an error string — tool failures must surface to the model as tool output, not as application crashes.

Adding a new tool requires only writing a function under `services/tools/` and a single `registry.register_tool(...)` call (typically at module import time). No changes to AIService or GUI code are required.

## Rationale

- A single seam makes "expose a function to the AI" a one-file change, mirroring the additive-module pattern in ADR-0003 / ADR-0016.
- A bounded tool-execution loop prevents pathological model behaviour (calling tools forever) from hanging the application.
- Catching exceptions inside `execute_tool` and returning them as strings is the correct behaviour for tool-using LLMs: the model should observe and reason about failure rather than receive an unhandled exception.
- Plain-string return values match the OpenAI tool-result protocol exactly and impose no internal abstraction.

## Alternatives Considered

- **Hardcode tools inside AIService:** Rejected — every new tool would require editing AIService; the registry's seam disappears.
- **Per-module tool registration (tools live with the module that owns them):** Rejected for now because the tools are currently cross-cutting (DataStore access, chart rendering); module-owned tools can still register through the same `ToolRegistry` later without a structural change.
- **Use a third-party agent framework (LangChain, LlamaIndex):** Rejected — heavyweight dependency, opinionated about prompt format and provider, conflicts with ADR-0010's narrow OpenAI-compatible interface.
- **Re-raise tool exceptions to the chat loop:** Rejected — would crash the streaming worker on any tool bug; the model loses the chance to recover or report the error.

## Consequences

### Positive

- Adding a tool is one function + one registration line.
- Tool failures are observable (logged + returned to the model), not catastrophic.
- The set of tools available to the model can be inspected at runtime via `registry.list_tools()`.

### Negative

- The broad `except Exception` in `execute_tool` is exactly the catch pattern ADR-0005 forbids elsewhere; it is intentional here and marked with `# noqa: BLE001` in the code.
- There is no permission model: every registered tool is callable by every model interaction. This is acceptable while the user is the only consumer; revisit before any multi-user or untrusted-prompt deployment.
- Tools must accept and return JSON-compatible primitives (currently: string return); structured types must be serialised by the tool implementation.

### Neutral / Follow-ups

- Add tool-level access control if/when AI features are exposed beyond the trusted single user (cross-reference ADR-0019).
- Consider a per-tool budget (max calls per conversation) in addition to the global iteration limit.

## Implementation Notes

- Implementation: `services/tool_registry.py` (`ToolRegistry`, `get_tool_registry`).
- Current tools: `services/tools/datastore_tools.py` (`list_datasets`, `get_dataset_summary`, `get_dataset_slice`); `generate_chart` (themed chart rendering).
- Integration: `services/ai_service.py` (`_StreamWorker.run` — multi-turn tool execution loop, `_MAX_TOOL_ITERATIONS = 10`).
- Documented in: `CLAUDE.md` ("ToolRegistry").

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Maintainability (modularity — additive tools), Reliability (bounded loops, tool-failure isolation).
- **Audit evidence:** Source code of `ToolRegistry`; the AIService streaming loop; the bounded `_MAX_TOOL_ITERATIONS` constant.

## References

- ADR-0001 (Layered architecture)
- ADR-0003 (BaseModule contract — analogous registry pattern)
- ADR-0005 (Exception hierarchy — broad catch documented as a deliberate exception)
- ADR-0010 (AIService singleton)
- ADR-0019 (Planned multi-user readiness — relevant when tool access control becomes necessary)

---

## Revision History

| Date       | Author                                | Change                                                             |
|------------|---------------------------------------|--------------------------------------------------------------------|
| 2026-04-24 | PortfoliFLOW project owner (retrofit) | Initial draft. Retrofitted from existing code and documentation; the original decision predates this ADR. |
| 2026-04-27 | PortfoliFLOW project owner            | Registered-tool list extended: `generate_chart` (class `READ_INTERNAL`, see ADR-0028) and `web_research` (class `READ_EXTERNAL_UNTRUSTED`, see ADR-0023, ADR-0024) have been added to the registry since this ADR was written. The seam decision recorded here is unchanged. |
