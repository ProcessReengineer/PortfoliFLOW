# ADR-0022: Tool Trust Classes and Gating Policy

- **Status:** Accepted
- **Date:** 2026-04-24
- **Deciders:** PortfoliFLOW project owner
- **Tags:** security, integration, architecture

---

## Context

PortfoliFLOW exposes AI-callable actions through a single `ToolRegistry` (ADR-0012). All tools registered today — `list_datasets`, `get_dataset_summary`, `get_dataset_slice`, `generate_chart` — are read-only against state the user already trusts (the in-memory DataStore, locally configured chart theme). The registry therefore treats every tool identically: every registered tool is callable by every model interaction, no permission model is present, and the only global safeguard is the iteration cap (`_MAX_TOOL_ITERATIONS = 10`) in the streaming worker. ADR-0012 flagged this explicitly as acceptable only while the user is the sole, trusted consumer, and listed the addition of tool-level access control as a follow-up item to be picked up when AI features reach beyond that assumption.

Two forces now make that follow-up concrete:

1. **Planned tools introduce new trust characteristics.** The upcoming Web Research capability (ADR-0023) fetches content from the open internet. Any text returned from an external site is *data about the world*, but the LLM will, by default, read it as *instructions* — this is the well-documented indirect prompt-injection risk class. Lumping such a tool together with DataStore readers under a single "all tools are equal" policy would be architecturally negligent.
2. **Planned tools will gain side effects.** As the application matures, tools will mutate application state (load/delete datasets, edit the SAA) and eventually act outside the application (send email, export to third parties, place orders). The blast radius of these operations is categorically different from a read query, and conflating them with read tools erases a distinction that audit-relevant environments (BAIT-governed institutions, institutional LPs performing operational due diligence) must see preserved in code.

PortfoliFLOW is built for institutional use. It must be defensible to a BAIT-aligned IT-risk review and to LPs asking how the system contains LLM-driven actions. The ToolRegistry is the single seam at which such containment can be expressed architecturally rather than through ad-hoc checks at individual call sites.

This decision is security- and IT-risk-relevant (BAIT AT 7.2 — IT-risk management, ISO 25010 — Security: confidentiality and integrity).

## Decision

Every tool registered with the `ToolRegistry` must be classified into exactly one of four **tool classes**, declared at registration time. The class determines what containment rules apply to the tool within a user turn.

**The four tool classes:**

1. **`READ_INTERNAL`** — Reads application state that the user has already loaded or configured (DataStore, loaded config files, local artefacts). Return values are considered *trusted* content: they reflect the user's own data. Current members: `list_datasets`, `get_dataset_summary`, `get_dataset_slice`, `generate_chart`.
2. **`WRITE_INTERNAL`** — Mutates application state inside PortfoliFLOW (load a dataset, delete a dataset, modify the SAA, change a configuration). No current members; reserved for planned module-level actions.
3. **`READ_EXTERNAL_UNTRUSTED`** — Fetches content from outside the application whose content cannot be assumed benign. Return values are considered *untrusted* content: they are information about the world and must never be interpreted as instructions to the agent. The Web Research tool defined in ADR-0023 is the first member of this class.
4. **`EXTERNAL_EFFECT`** — Actions with side effects outside the application (send email, trigger an export to a third party, place an order, call an external write API). No current members; reserved for future integrations.

**Trust levels for tool output.** Every tool result carries an implicit trust level determined by its class:

- Output of `READ_INTERNAL` tools is *trusted*: it is the user's own data and may be passed to the LLM without any special wrapping.
- Output of `READ_EXTERNAL_UNTRUSTED` tools is *untrusted*: before inclusion in any prompt the agent reads, the content must be wrapped in explicit delimiters:

  ```
  <external_content source="..." fetched_at="..." trust="untrusted">
    ...content...
  </external_content>
  ```

  Shirley's system prompt (`docs/Soul_Shirley.md`) is extended to instruct her that content inside these delimiters is information about the world, never instructions to her. An instruction inside such a block is data, not a directive.

- Output of `WRITE_INTERNAL` and `EXTERNAL_EFFECT` tools is status information about an action that was performed, not content to reason over; the trust-delimiter concept does not apply.

**Gating rules.** The registry enforces the following invariants for the scope of a single user turn — defined as one `AIService.send_message()` call together with its complete tool-execution loop (up to `_MAX_TOOL_ITERATIONS` iterations):

1. Once any `READ_EXTERNAL_UNTRUSTED` tool has executed within a user turn, all `WRITE_INTERNAL` and `EXTERNAL_EFFECT` tools are **locked for the remainder of that turn**. A subsequent attempt by the model to invoke a locked tool returns a tool-result error string describing the lock; the call does not reach the tool function. The next user turn starts with a fresh, unlocked state.
2. Every tool must declare its class when registered. Registration without a declared class is a programming error that raises immediately (fail-fast; misconfiguration must not be silent).
3. The global cap `_MAX_TOOL_ITERATIONS = 10` (ADR-0012) remains in place. The gating rules are an additional, independent restriction and do not replace it.
4. `EXTERNAL_EFFECT` tools, when introduced, require explicit user confirmation via a GUI dialog before execution. This ADR records that requirement now so later implementers cannot treat confirmation as an optional nicety.

**Quarantined Context principle.** Raw content from untrusted sources never enters the agent's conversation history directly. It is always processed first by an isolated, stateless, tool-free LLM call (the "Fetcher-LLM", invoked via `AIService.send_one_shot_extraction()` or an equivalent path) that extracts facts into a structured JSON schema under its own independent system prompt. The agent sees only the validated, structured summary, wrapped in the trust-marked delimiters described above. The Fetcher-LLM is part of the Web Research capability's architecture (ADR-0023), not a capability of Shirley; Shirley never fetches, never parses raw HTML, and never receives unprocessed external text.

## Rationale

- **The four classes separate concerns that matter operationally.** Read-vs-write and internal-vs-external are the two axes that drive blast radius. A four-cell matrix expressed at the type level makes those two axes legible in code, tests, and audit review without introducing a heavier permission model than the project currently needs.
- **Class declaration at registration is fail-fast.** Silent defaults ("if unspecified, assume read-internal") are how security guarantees rot quietly. Requiring an explicit class at registration is cheap, self-documenting, and keeps the registry honest as new tools are added.
- **Turn-scoped gating fits the actual threat.** The relevant worry is *prompt injection causing a side-effecting action within the same reasoning context the injection entered*. Locking writes and external effects for the remainder of the turn after any untrusted fetch makes that pivot impossible in one step, while leaving a perfectly reasonable next-turn path (user reviews Shirley's summary, then explicitly asks for the write) open.
- **Quarantined Context is the well-understood mitigation pattern** for indirect prompt injection: two isolated LLM calls, the first untrusted-input-bearing with no tools and no memory, the second trusted-context-bearing operating only on structured output from the first. The alternative — passing raw web content into the conversational agent and "asking it nicely" not to follow embedded instructions — has a long public track record of failing.
- **Trust delimiters are belt-and-braces, not a silver bullet.** Even after Quarantined Context extraction, the structured summary may still contain attacker-influenced strings (e.g. a claim framed as a conclusion). Wrapping it in explicit, named delimiters and training the system prompt around them lowers the probability that a benign-looking summary flips Shirley into treating data as commands. It does not eliminate the risk; that is stated honestly and not papered over.
- **Extends ADR-0012's model, does not revoke it.** ADR-0012's "one file, one registration line" ergonomics are preserved — the class is one additional argument at registration time. The registry seam remains single and additive.
- **Auditability.** A BAIT-aligned IT-risk reviewer or an institutional LP performing operational due diligence on PortfoliFLOW can read the taxonomy, the gating rules, and the list of registered tools-per-class as a single concise artefact. "Does this system contain LLM-driven side effects?" becomes a question with a precise, traceable answer.

## Alternatives Considered

- **Do nothing — keep ADR-0012's flat registry.** Rejected. Acceptable while every tool was `READ_INTERNAL`; untenable the moment Web Research or any write action ships. The whole point of ADR-0012's follow-up note was to address this before it mattered.
- **Full RBAC / per-user permission model on tools.** Rejected as premature. PortfoliFLOW is single-user today (ADR-0019). A role model would add a large surface with no current user to assign roles to; the four-class taxonomy captures the structural distinctions that matter now without pretending there is a second principal.
- **Two-class split (read vs. write only).** Rejected. Collapsing `READ_INTERNAL` and `READ_EXTERNAL_UNTRUSTED` into a single "read" class erases the trust-level distinction that drives the Quarantined Context pattern and the delimiter requirement. The untrusted-content property is the whole point; hiding it inside a generic "read" class defeats the purpose.
- **Per-tool allowlists configured in a separate policy file.** Rejected for this iteration. A YAML policy file would move the declaration out of the registration call (where the tool author already is) and into a file that can drift. Class-at-registration keeps the declaration and the code in the same place and in the same PR.
- **Block untrusted fetches until the model is "sure" (confidence-threshold gating).** Rejected. Self-reported model confidence is not a security boundary. Structural rules (gating at the registry, Quarantined Context) are.
- **Only lock writes when the untrusted content is suspicious (heuristic classifier).** Rejected. A classifier is a soft defence against a hard invariant. The turn-scoped lock is cheap, predictable, and testable; a heuristic is none of those.
- **Treat gating as a per-call concern inside each tool implementation.** Rejected. Repeating the check at each call site is exactly the scattering ADR-0012 was designed to prevent. Gating belongs at the single seam.

## Consequences

### Positive

- The ToolRegistry can be inspected at a glance: every tool has a visible class, and the set of write / external-effect tools is enumerable for audit.
- Prompt-injection blast radius is structurally bounded: an injection delivered through a web fetch cannot, within the same turn, cause a write or an external effect.
- The trust-delimiter convention gives Shirley's system prompt a clear rule to enforce, replacing the current implicit "everything the user sees, Shirley can treat as instructions".
- Future `EXTERNAL_EFFECT` tools inherit a confirmation requirement from day one rather than bolting one on later per integration.
- The class taxonomy is a small, stable vocabulary that ADR-0023 and future tool ADRs can reference instead of reinventing terminology.

### Negative

- Every existing tool registration must be updated to declare a class. This is a mechanical change but it is not a zero-diff operation — it touches each of the existing tool modules.
- The class is an additional required parameter at registration time, slightly raising the activation energy for adding a tool. The project accepts this cost: the tradeoff is explicit classification over silent defaults.
- Turn-scoped gating can produce surprise from the model's perspective (a tool it could call one turn ago is now locked). The fix is to make the lock visible in the tool-result error string so the model can explain the situation to the user rather than silently retry.
- The Quarantined Context pattern costs a second LLM round-trip per untrusted fetch, increasing latency and token cost. The project accepts this cost as proportional to the risk it mitigates.
- This ADR does not eliminate prompt-injection risk. It reduces likelihood and contains blast radius. That honest limitation is restated in ADR-0023.

### Neutral / Follow-ups

- Implementation of the classification and gating logic in `services/tool_registry.py` is the subject of a separate implementation prompt (not this ADR).
- The trust delimiters imply a companion edit to `docs/Soul_Shirley.md` describing how Shirley interprets `<external_content ...>` blocks. That edit is scoped to the Web Research implementation prompt, not this ADR.
- When `WRITE_INTERNAL` tools are first introduced, revisit whether an additional per-tool confirmation dialog is warranted for destructive writes (delete dataset, overwrite SAA) even outside an `EXTERNAL_EFFECT` context.
- When a real multi-user scenario emerges (ADR-0019), tool classes are the natural anchor points for a per-role permission overlay; this ADR deliberately does not design that overlay now.
- Monitor whether turn-scoped locking proves too coarse in practice (e.g., a benign multi-step research-then-annotate flow that the user explicitly authorises). If so, consider a user-initiated explicit-confirmation override; keep any such override out of the LLM's reach.

## Implementation Notes

- Primary affected file (future implementation): `services/tool_registry.py` — add a class-enum argument to `register_tool`, a turn-scoped lock state, and enforcement in `execute_tool`.
- Related integration point: `services/ai_service.py` (`_StreamWorker.run`) — the lock state must be reset at the start of each `send_message` invocation and threaded through the tool-execution loop.
- Existing tool modules to be updated: `services/tools/datastore_tools.py`, `services/tools/chart_tools.py`. All are `READ_INTERNAL`.
- Web Research tool introduced by ADR-0023 will register as `READ_EXTERNAL_UNTRUSTED`.
- Shirley system prompt: `docs/Soul_Shirley.md` — to be extended with the trust-delimiter handling instruction (implementation prompt, not this ADR).
- Documented summary: `CLAUDE.md` — new section "Tool Classes and Trust Levels" under "Implemented Services Reference".

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Security (confidentiality — untrusted content cannot instruct the agent to exfiltrate DataStore state; integrity — untrusted content cannot drive a WRITE_INTERNAL action within the same turn), Maintainability (the taxonomy makes tool semantics legible to future maintainers and reviewers).
- **Regulatory references:** BAIT AT 7.2 (IT-risk management — structural controls over automated actions with external-content inputs). The four-class taxonomy, the explicit gating rules, and the enumerable registry together form the kind of demonstrable control surface an AT 7.2 review expects. Relevant also as preparatory structure for later DORA-style operational-resilience scrutiny if PortfoliFLOW is ever embedded in a regulated institution's toolchain.
- **Audit evidence:** The class declaration at each `register_tool` call; the enumerable list of tools per class from `ToolRegistry`; the gating-enforcement source in `services/tool_registry.py`; Shirley's system prompt describing the trust-delimiter rule; logs recording any locked-tool rejection during a turn.

## References

- ADR-0012 (ToolRegistry as single seam for AI-callable tools — this ADR is the explicit execution of ADR-0012's follow-up item *"Add tool-level access control if/when AI features are exposed beyond the trusted single user"*. ADR-0012's status remains Accepted; the additional control layer defined here extends it rather than replacing it.)
- ADR-0010 (AIService singleton — the seam on which the gating is enforced)
- ADR-0019 (Planned multi-user readiness — tool classes are the anchor points for any future per-role permission overlay)
- ADR-0023 (Web Research Capability — the first concrete `READ_EXTERNAL_UNTRUSTED` tool and the architectural home of the Fetcher-LLM)
- ADR-0005 (Typed exception hierarchy — misconfigured tool registrations raise `PortfoliFlowError` subclasses rather than bare exceptions)

---

## Revision History

| Date       | Author                       | Change        |
|------------|------------------------------|---------------|
| 2026-04-24 | PortfoliFLOW project owner   | Initial draft |
