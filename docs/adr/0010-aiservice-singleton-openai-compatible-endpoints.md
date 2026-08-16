# ADR-0010: AIService as Singleton, OpenAI-Compatible Endpoints

- **Status:** Accepted
- **Date:** 2026-04-24
- **Deciders:** PortfoliFLOW project owner (retrofit)
- **Tags:** integration, architecture

---

## Context

PortfoliFLOW relies on LLMs for several Features (current: Shirley AI assistant, AI Settings; planned: Report Scraper, DD Support, Reporting Engine extensions, Portfolio Q&A). Without a central layer, each Feature would instantiate its own client, hardcode an endpoint, and duplicate concerns such as authentication, retry, model selection, conversation construction, and tool dispatch. Model and provider choice would then be a per-Feature change rather than a single point of update.

PortfoliFLOW must also be free to switch providers (router vs. direct provider, hosted vs. self-hosted) without rewriting every consumer. Cost was a secondary factor in choosing the integration approach.

## Decision

PortfoliFLOW exposes a single `AIService` class (`services/ai_service.py`) accessed via the `get_ai_service()` factory. Every module and widget that needs LLM capability goes through this singleton; no module instantiates its own `openai.OpenAI` client.

`AIService` targets any **OpenAI-compatible endpoint** — OpenRouter, direct OpenAI, local Ollama, or any other API that implements the OpenAI chat-completions surface. The endpoint base URL and API key are user-configured (via the AI Settings widget, persisted in `QSettings`); the model id is user-selectable from the model list returned by the endpoint.

The current public API includes: `connect()`, `disconnect()`, `send_message()`, `get_model()`, `set_model()`, `get_available_models()`, `load_saved_settings()`, `get_system_prompt()`, `get_status()`. Future additions (Feature-specific extraction / classification helpers) are planned in `CLAUDE.md` and will live behind the same singleton.

## Rationale

- A single integration point centralises model selection, error handling, and (planned) cost tracking. Changing provider becomes a one-place change.
- Targeting the OpenAI-compatible surface (rather than a single provider's bespoke SDK) keeps the door open for routers and self-hosted models without code changes — the user picks the endpoint and key.
- A singleton matches the actual usage pattern (the application has one user, one active session, one set of credentials).
- Storing endpoint / key / model via `QSettings` (rather than `.env`) allows the configuration to be edited at runtime through the GUI without restarting the app.

## Alternatives Considered

- **Per-Feature LLM clients:** Rejected — duplicates code, makes provider switches a multi-file change, prevents centralised cost tracking.
- **Hardcode one provider's SDK (OpenAI direct):** Rejected — locks the project to a single vendor; OpenRouter and local models are explicit options.
- **Bespoke abstraction layer (provider-agnostic API of our own design):** Rejected as over-engineered. The OpenAI-compatible surface is already a de-facto standard with good ecosystem coverage.
- **Configuration via `.env` only:** Rejected — runtime endpoint/model changes are a routine workflow; QSettings supports this without forcing application restart.

## Consequences

### Positive

- Single seam for any AI-related change (model upgrade, provider switch, cost-tracking introduction).
- Modules and widgets stay free of LLM-SDK details.
- The user can switch endpoint / model at runtime from the AI Settings UI.

### Negative

- Tying to the OpenAI-compatible API means non-compatible providers (e.g., bespoke Anthropic message format) require translation at the AIService boundary — exactly the problem the recent fix in commit `9b6371f` addressed.
- A singleton makes it harder to run two LLM configurations simultaneously (e.g., one model for Shirley, one for Report Scraper). Not a current requirement.

### Neutral / Follow-ups

- Centralised cost tracking is planned (`CLAUDE.md`) but not yet implemented.
- Feature-specific extraction helpers (`extract_fund_metrics`, `generate_key_messages`, `classify_document`, `answer_portfolio_question`) are planned additions; each will be a method on the singleton, not a new client.
- The PyQt6 dependency required for streaming via QThread/Signals is captured in ADR-0011 as an explicit, documented exception to the layering rules.

## Implementation Notes

- Implementation: `services/ai_service.py` (`AIService`, `_ModelsWorker`, `_StreamWorker`, `get_ai_service`).
- Data models: `services/ai_models.py` (`Message`, `Conversation`, `Attachment`, `MessageRole`, `ToolCall`, `ConnectionStatus`).
- Configuration UI: `gui/widgets/ai_settings_widget.py`; persistence via `QSettings` keyed by `_ORG = "PortfoliFLOW" / _APP = "PortfoliFLOW"`.
- System prompt: loaded from `docs/Soul_Shirley.md`.
- Tool execution loop: bounded by `_MAX_TOOL_ITERATIONS = 10`; integrates with `ToolRegistry` (ADR-0012).

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Maintainability (modifiability — provider swap is local), Security (single place to enforce credential handling and request limits), Compatibility (OpenAI-compatible interoperability).
- **Audit evidence:** `services/ai_service.py`; the absence of `openai.OpenAI(...)` constructor calls outside this file is checkable by grep.

## References

- ADR-0001 (Layered architecture)
- ADR-0011 (Acknowledged PyQt6 dependency in AIService)
- ADR-0012 (ToolRegistry as single seam for AI-callable tools)

---

## Revision History

| Date       | Author                                | Change                                                             |
|------------|---------------------------------------|--------------------------------------------------------------------|
| 2026-04-24 | PortfoliFLOW project owner (retrofit) | Initial draft. Retrofitted from existing code and documentation; the original decision predates this ADR. |
| 2026-05-03 | PortfoliFLOW project owner            | Phase 1 / Strang A1 (ADR-0038): the singleton lifecycle now lives on `AIServiceCore` (`services/ai_service_core.py`), accessed via `get_ai_service_core()`. The Qt adapter `AIServiceQt` (`services/ai_service_qt.py`) holds a reference to the singleton core via constructor injection from its own `get_ai_service_qt()` factory; non-GUI consumers (scraper, web research, future FastAPI) talk to the core directly. The legacy `services/ai_service.py` module is retained as a thin deprecation shim that re-exports `AIServiceQt as AIService` and `get_ai_service_qt as get_ai_service`. |
