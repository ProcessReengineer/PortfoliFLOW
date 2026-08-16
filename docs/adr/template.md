# ADR-NNNN: [Short Title of the Decision]

- **Status:** Proposed | Accepted | Deprecated | Superseded by ADR-XXXX
- **Date:** YYYY-MM-DD
- **Deciders:** [Names / roles of the people involved in the decision]
- **Tags:** [architecture | data | security | process | analytics | ui | integration | ...]

---

## Context

What is the issue, situation, or force that motivates this decision? Describe the problem in neutral, factual language. Include relevant constraints (technical, regulatory, organizational, budgetary). A reviewer or auditor should be able to understand the problem without having to read the code.

If this decision is relevant from a regulatory or audit perspective (e.g., BAIT/VAIT, DORA, SOC 2, ISO 25010 quality attributes), name the relevant area explicitly here.

## Decision

What did we decide to do? State it clearly and in the active voice: "We will ...", "PortfoliFLOW uses ...", "The DataStore is implemented as ...".

Keep this section short. The reasoning belongs in *Rationale*, the details belong in *Consequences* and *Implementation Notes*.

## Rationale

Why this decision and not an alternative? What drove the choice?

- Requirements satisfied
- Quality attributes prioritized (e.g., maintainability over raw performance, auditability over convenience)
- Constraints respected
- Assumptions made

## Alternatives Considered

List the options that were evaluated and rejected, with a brief reason for rejection each. This is often the most valuable section for an auditor, because it documents that the decision was made deliberately rather than by default.

- **Alternative A:** [Description]. Rejected because [reason].
- **Alternative B:** [Description]. Rejected because [reason].
- **Do nothing / status quo:** [If applicable] Rejected because [reason].

## Consequences

### Positive

- What becomes easier, safer, or more consistent as a result?

### Negative

- What becomes harder, slower, or more expensive?
- What technical debt or risk is accepted?

### Neutral / Follow-ups

- What needs to be monitored, reviewed, or revisited later?
- Are there follow-up ADRs that should be written?

## Implementation Notes

Concrete pointers into the codebase or process. Keep this factual and up to date:

- Affected modules / files: `path/to/module.py`, ...
- Related tests: `tests/...`
- Configuration: `config/...`
- Related documentation: `docs/...`

## Compliance & Audit Relevance

*Optional — fill in if the decision touches regulatory or audit-relevant areas.*

- **ISO 25010 quality attributes affected:** [Maintainability, Security, Reliability, ...]
- **Regulatory references:** [BAIT AT 7.2, VAIT, DORA Art. X, ...]
- **Audit evidence:** Where can an auditor verify that this decision is actually implemented? (Tests, logs, code inspection points, documentation.)

## References

- Related ADRs: ADR-XXXX, ADR-YYYY
- External standards, papers, blog posts, library documentation
- Issue tracker references, meeting notes, design discussions

---

## Revision History

| Date       | Author | Change                             |
|------------|--------|------------------------------------|
| YYYY-MM-DD | [name] | Initial draft                      |
| YYYY-MM-DD | [name] | Status changed from Proposed to Accepted |
