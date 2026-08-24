# ADR-0127 — Temporal grounding: current-date injection and actuals-first as-of default for limit coverage

Date: 2026-08-24
Status: Accepted

## Context

An observed Shirley dialogue (limits headroom question ahead of a board
call) produced a confident, well-structured answer whose every figure
was wrong for the question asked: the reported Stichtag was
**2030-12-31** — the end of the planning horizon — presented as the
current state of the book ("the numbers are fresh"). PE headroom,
the equities breach, and the WARN lines were all **plan-stream
projections at the horizon end**, not today's coverage.

A code review of the snapshot confirmed two independent causes:

1. **Shirley has no notion of the current date.**
   `AIServiceCore.get_system_prompt` (`services/ai_service_core.py`)
   composes the prompt from the `Soul_Shirley.md` fence, the generated
   tool inventory (ADR-0012 B8), and the two hand-authored context
   files. No date appears anywhere in the composition, and none of the
   composed sources carries one. Without a reference point, the model
   cannot classify a tool-reported Stichtag as past, present, or
   future — it cannot even notice the contradiction. Both consumers of
   the prompt — the web chat route (`web/routes/chat.py`, via
   `_briefed_system_prompt`) and the Telegram bot
   (`bot/telegram_bot.py`) — call this one method, so a fix at this
   seam covers both surfaces.

2. **`get_limit_coverage` defaults its upper bound to the book's
   horizon, which the Planning Desk has moved into the future.**
   The tool (`services/tools/analysis_tools.py`) passes
   `to_date=None` when the model omits the argument.
   `LimitsCoverageService._resolve_date_range`
   (`services/limits/limits_coverage_service.py`) resolves `None` to
   `max(as_of_date)` across **both** the actual and plan NAV streams —
   a deliberate design ("Plan NAVs carry it into the future exactly
   as the AUM forecast used to", ADR-0103 §2) that is correct for the
   Back Office coverage view but wrong as the default answer to
   "where do we stand". With plan rows seeded through the planning
   horizon, `latest_as_of_date` lands on the horizon end. Because
   `cut_over` defaults to today, every evaluation date past it draws
   from the **plan stream** (`services/analytics/limit_coverage.py`,
   ADR-0060 semantics) — and the tool's summary then labels the
   result "Limit coverage as of 2030-12-31 (present and
   historical …)". The tool mislabels plan data as present fact.

The review also established what this is **not**:

- There is **no shared as-of resolver** and no platform-wide
  contamination. `StatisticsService`, `PortfolioReviewService` (and
  through it the Front Office overview) load `nav_kind="actual"`
  explicitly; `get_saa_hypothetical_comparison` defaults its cut-off
  to today. The defect is specific to the limits path.
- The repository already provides the right primitive:
  `InvestmentNavRepository.latest_actual_as_of_date`.
- The web Limits route (`web/routes/limits.py`) shares the
  `to_date=None` default, so the Back Office KPI strip also lands on
  the plan horizon by default. There the Stichtag is at least
  displayed ("As-of date …") and the step chart shows the trajectory,
  making it defensible as a planning view — but the plan/actual
  character of the headline strip is not labelled. That is a related
  but separate concern (see §T3).

## Decision

One decision in three strands. T1 and T2 together close the observed
failure; T3 is accepted in principle and deferred to the roadmap.

### T1 — Current-date injection at prompt assembly

`AIServiceCore.get_system_prompt` prepends a short temporal-grounding
block as the **first** content of every composed prompt, generated
fresh at each assembly:

```
Current date: 2026-08-24 (Monday).
Treat any data dated after this as plan/forecast data, not observed fact.
```

- The date is `date.today()` in the server's local timezone. A
  per-tenant timezone is **out of scope**: `get_system_prompt` is a
  synchronous, tenant-free seam, and no tenant timezone attribute was
  verified for this purpose. Should tenant-local time become
  necessary (e.g. for "next Tuesday" arithmetic across timezones), a
  successor ADR threads tenant context through the seam.
- The minimal fallback prompt (soul file missing/malformed) receives
  the same block — temporal grounding must not depend on the soul
  file being intact.
- The block is prepended, not appended: date salience must not
  compete with the tool inventory and orchestration context for
  positional attention.
- The Irene synthesis prompt path (`services/irene/synthesis_tool.py`)
  is a separate seam and **out of scope** here; whether Irene needs
  the same grounding is a follow-on question (findings are
  timestamped by the persistence layer, so the case is weaker).

### T2 — Actuals-first default in `get_limit_coverage`

The tool — not the service — changes its default upper bound:

- When the model omits `to_date`, the tool passes
  `to_date=date.today()` instead of `None`. The evaluation grid then
  ends at the last month-end Stichtag on or before today; with the
  unchanged `cut_over` default (also today), that Stichtag lies in
  actual territory and resolves per the established carry-forward /
  cross-stream fallback semantics (ADR-0060). No repository query is
  needed and no engine or service signature changes.
- The plan horizon remains reachable: an explicit `to_date` past
  today behaves exactly as before.
- The summary line drops the blanket "(present and historical)"
  claim and instead states the resolved Stichtag **and its
  territory**: when the reported `latest_as_of_date` lies after the
  effective cut-over, the summary is prefixed with an explicit
  plan-territory notice (e.g. "NOTE: this Stichtag lies beyond the
  plan/actual cut-over — figures are plan-stream projections, not
  observed coverage."). No silent relabeling in either direction.
- The tool docstring (which feeds the API `tools` field and the B8
  inventory) is updated to match: default = today's coverage;
  future `to_date` = plan projection, flagged as such.

The alternative — changing `LimitsCoverageService`'s own `None`
resolution — was **rejected**: it would silently change the Back
Office web view's default range in the same commit, coupling two
surfaces with different requirements into one behavioral change.
The service's horizon semantics remain as designed in ADR-0103.

### T3 — Web KPI-strip plan-territory labelling (deferred)

The Back Office Limits KPI strip should visibly distinguish a
plan-territory Stichtag (e.g. an "As-of date … (plan)" suffix or a
badge when `latest_as_of_date > cut_over`). This is accepted in
principle, deferred as a roadmap entry (next free ID), and not a
gate for `2026.09.0`.

## Consequences

**Positive.**

- Shirley can date-classify every tool result; "where do we stand
  today" resolves to actual-territory coverage by default on both
  chat surfaces (web + Telegram) through the single prompt seam.
- Plan-based limit projections remain available but are always
  labelled as such — no capability is removed, only mislabelling.
- The web Limits view is untouched in this ADR; no regression surface
  there.
- The fix is small: one prompt-assembly change, one tool-default
  change, one summary-labelling change.

**Negative / accepted trade-offs.**

- The prompt grows by two lines per assembly (negligible).
- Server-local "today" may differ from a tenant's local date around
  midnight; accepted until a tenant timezone attribute exists.
- A book whose actuals lag far behind today will report a Stichtag
  correspondingly in the past (with carry-forward per ADR-0060) —
  which is the honest answer, but may surprise users expecting the
  plan trajectory they see in the Back Office view. The territory
  labelling in both the tool summary (T2) and, later, the web strip
  (T3) is the mitigation.
- Two dialogue-history messages produced before this fix may persist
  with horizon-dated figures; no remediation is attempted.

**Testing.**

- Extend `tests/assistants/test_system_prompt_grounding.py`: the
  composed prompt (and the fallback prompt) begins with the
  current-date block; the date matches `date.today()`.
- New tool tests in `tests/assistants/test_analysis_tools.py`:
  omitted `to_date` yields a Stichtag ≤ today in actual territory;
  explicit future `to_date` yields the plan-territory notice;
  existing behavior for explicit past ranges is unchanged.

## References

- ADR-0012 — ToolRegistry as single seam (B8 prompt grounding)
- ADR-0060 — NAV carry-forward and cross-stream fallback
- ADR-0069 — Shirley back-office analysis tools (`get_limit_coverage`)
- ADR-0103 — §2 book-horizon range resolution (plan NAVs bound the range)
- ADR-0126 — precedent for correcting a false premise in a successor ADR
