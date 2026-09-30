# ADR-0133: Watch Desk Briefing Artefact and Watch-Point Scope — Second Synthesis Step, Family Switches, Sensitivity Presets

- **Status:** Accepted (2026-09-30)
- **Date:** 2026-09-30
- **Deciders:** PortfoliFLOW project owner
- **Closes:** the concept half of the Watch Desk refinement (roadmap **#069**),
  the first refinement item after the owner declared PortfoliFLOW
  feature-complete (2026-09-29). The build is DC-WD-B; the surface is UX
  strand A-6 on mockup M-WD-1.
- **Supersedes / amends:** **annex amendment to ADR-0088** — the synthesis
  contract gains a second output, a persisted briefing written by a second
  call after the deterministic floor. **Annex amendment to ADR-0116** — §1
  (the registry gains a family-level scope structure beside the
  watchpoints), §3/§5 (WARN resolution gains a per-family step between the
  subject and the tenant), §7 (`floor_calibration` gains one WARN column per
  scalar family; the editor gains those fields and becomes owner-only).
  ADR-0088 and ADR-0116 remain immutable and otherwise unchanged.
  **Extends the ADR-0107 C6 consultation brief** by a second kind: besides an
  open case, Shirley can be briefed with a written Watch Desk briefing (§6).
  **Corrects prompt composition** in `AIServiceCore.get_system_prompt` so
  that Irene is never presented to herself as Shirley (§1.6); Shirley's own
  prompt, tools and orchestration guidance are unchanged. ADR-0085 (append-only
  findings), ADR-0086 (tick and beat), ADR-0087 (delta), ADR-0089 (surface,
  as renamed by ADR-0115), ADR-0119 / ADR-0125 (cadence vocabulary) and
  ADR-0127 (temporal grounding) are unchanged.
- **Tags:** watch-desk, irene, shirley, synthesis, briefing, watchpoints,
  calibration, audit, llm, prompts, ux

---

## Decisions of record

Taken by the owner on 2026-09-29/30. Quoted, not paraphrased.

| Id | Decision |
|---|---|
| D-WD-1 | "The briefing is a beat artefact, not a page view: written once per beat in a second synthesis step **after** the deterministic floor, persisted append-only with its timestamp, shown with that stamp. Never generated on page load." |
| D-WD-2 | "Part 1 (markets and press, read against the book) reads the whole priceable book — held instruments with prices, FX pairs in the book, press clusters on `macro` and `regulator` — regardless of which watchpoints exist. Watch points govern **alerts**, not what Irene may read." |
| D-WD-3 | "Family on/off scope is its own historised, audited structure, separate from mute. Off means: not evaluated in the beat, not shown in the monitor, not mentioned in Part 2 of the briefing." |
| D-WD-3a | "One switch per family is the only switch on the Watch points page. Per-subject mute stays as a row action inside the watch list (second level), never as a switch list. Press tags keep their existing per-tag mute at that second level; `macro` and `regulator` are always read." |
| D-WD-4 | "Every family can be switched off, quota families included, with a clear inline warning (R7 idiom) that names what stops from the next check, that open findings of the family stay open until resolved, and that the change is recorded with the actor's name." |
| D-WD-5 | "Sensitivity per family is a preset — Quiet · Standard · Nervous — with a fourth state Custom shown whenever the expert value deviates from all three presets." |
| D-WD-6 | "Cadence hangs on the check clock in the view head (`pf-fresh`, popover 'When Irene checks'). The Calibration section becomes 'Watch points'; the existing calibration editor survives unchanged as a collapsed, owner-only 'Advanced tuning' panel." |
| D-WD-7 | "No price ticker. The watch strip (one chip per active family, dot + word, live stamp) is the Bloomberg element." |
| D-WD-8 | "Sequence: concept ADR (this chat) → build (DC-WD-B) → UX strand A-6 as a pure UI strand afterwards." |
| D-WD-9 | "The Journal shows every check as a row, silent checks included ('Check at 20:03 · nothing material'), grouped by day and collapsible per day." |
| D-WD-10 | "The Journal filter 'Watch point' hides briefing rows whenever a family is selected; findings and cases filter by their family. Nothing is deleted by a filter." |
| D-WD-11 | "The briefing is written by Irene (same persona, second call). `docs/Soul_Irene.md` gains one section for the briefing step: calm by default, two parts, grounded in the figures handed over, never invents a number, reads finalised bands as facts and never proposes urgency in that call. The existing sections stay verbatim." |
| D-WD-12 | "The approaching mark is stored per family: `floor_calibration` gains one nullable WARN column per scalar family, and NULL inherits the tenant-wide WARN default. A preset writes the family's WARN and re-trigger delta; presets never touch floors, caps, band boundaries or the options gate." |
| D-WD-13 | "Press coverage has the on/off switch only; it carries no sensitivity preset." |
| D-WD-14 | "The family status the watch strip shows and the beat hands to the writer is one read-only service under `services/watch_desk/`, lifted out of the route; Part 1 reads the whole priceable book through a new read-only market read. Both are the first strand of DC-WD-B." |
| D-WD-15 | "Grounding of the briefing is a prompt obligation, as for findings (ADR-0088). No deterministic number check suppresses a briefing: better shown and dismissed by the reader than withheld when it was material." |
| D-WD-16 | "The briefing prompt is its own fenced block under its own heading in `Soul_Irene.md`, loaded by an explicit accessor; the finding call keeps reading the existing System Prompt block unchanged." |
| D-WD-17 | "Switching off a quota family names breaches in its warning, and while the family is off the Briefing view carries a standing deterministic note naming the family, since when and by whom. Nobody may switch off a warning and later claim the program did not see it." |
| D-WD-18 | "Everything on the Watch points page that changes what Irene watches — family switches, presets and Advanced tuning — is owner-only. Per-subject mute stays a row action open to every member." |
| D-WD-19 | "Part 1 always receives the `macro` and `regulator` clusters; clusters on the other press tags are Part 1 material only while Press coverage is on and the tag is not muted." |
| D-WD-20 | "The market read measures moves over a fixed window of five days and hands over a value-weighted move per asset class beside the per-instrument moves, so Irene never computes an aggregate." |
| D-WD-21 | "The `preset` column of the scope structure is an audit record of the preset a revision applied; it is never read for display, and `custom` is never stored." |
| D-WD-22 | "Irene is never presented to herself as Shirley. Shirley's prompt, her tools and her orchestration guidance stay exactly as they are; Irene's check calls, which offer the model only their one tool, no longer carry the prompt text that introduces Shirley and lists Shirley's tools as Irene's own. The correction is in the scope of this refinement; between the Watch Desk and Shirley there must be no confusion." |
| D-WD-23 | "Under every written briefing — on the Briefing view and on each written briefing row of the Journal — sits the action 'Discuss briefing with Shirley'. It opens Shirley in place, on the page the reader is on, with that briefing as her consultation context, and Shirley discusses it with her full tool set." |
| D-WD-24 | "Irene's checks keep one tool per call; she does not get Shirley's tool set. A sequence repeated several times a day is no place for open-ended analysis — whoever wants to go deeper calls Shirley, who has every function." |

D-WD-1…11 were taken in the Mission Control chat of 2026-09-29/30; D-WD-11
was set by MC with an owner veto reserved and stood the owner's review of the
briefing text (Appendix A) in DC-WD-A. D-WD-12…24 were taken by the owner in
the concept chat DC-WD-A on 2026-09-30.

Two clocks belong to D-WD-1 and are an **honesty rule**, not a UI detail:
the briefing carries the timestamp of the check that wrote it; the monitor,
the watch strip and the watch list carry the render-time stamp. Both are
always displayed. This settles, for the Watch Desk, the two-clock question
ui-standards §2.6 row 6 (R9) records as open.

## Context

The Watch Desk engine is sound (ADR-0085–0089, ADR-0116, ADR-0119): the
delta layer decides what is worth showing Irene, Irene phrases and proposes,
the deterministic floor decides. What fails is the surface. Five diagnosis
points:

1. **Engine vocabulary.** "Beat", "subjects" and internal subject keys such
   as `saa:listed_equities` reach visible copy.
2. **Telemetry mixed with book status.** The status tiles (`_build_tiles`,
   `_next_beat_tile` in `web/routes/watch_desk.py`) set the scheduler's
   state beside the book's state as if they were one kind of fact.
3. **The monitor mirrors Back Office › Limits.** The quota groups restate
   the limits view rather than saying what Irene sees in it.
4. **The one action is hidden in a tile.** "Request analysis now" lives
   inside the next-beat tile.
5. **The calibration editor is a landing page.** Floors, caps and band
   boundaries greet a reader who wanted to know what is being watched.

The desk's USP — Irene watches on her own cadence, before anyone asks — is
therefore invisible. Three of the remedies are re-skinning and belong to
A-6. Two are architectural and need this decision first:

- **A written briefing** is a new artefact. Generated on page load, it would
  be a second, unstamped opinion that changes with every refresh and costs
  one LLM call per view. D-WD-1 makes it a product of the check.
- **Switching a family off** is a new state with regulatory weight. It is
  not mute: mute suppresses finding creation and keeps the subject visible
  (ADR-0116 §3), whereas off stops evaluation. It needs its own historised,
  audited home, and the sensitivity presets need a storage model in which a
  preset and the expert values can never disagree.

Verified against the tree of 2026-09-30:

- The beat writes findings inside one per-tenant transaction opened by
  `services/scheduler/tick_runner.py`.
- A tenant without a resolvable LLM credential is skipped **before**
  `run_beat` is called.
- `run_beat` returns early on the silence path.
- `AIServiceCore.run_synthesis` offers one tool with `tool_choice="auto"`.
- `AIServiceCore.get_system_prompt` reads only the **first** fenced block of
  a Soul file, and for every prompt name — Irene's included — appends
  Shirley's tool inventory and the two `docs/Shirley_*_Context.md` files;
  its fallback text introduces Shirley.
- `warn_default_pct` is the only WARN value below the per-subject overlay,
  and it governs the Approaching band of all six scalar families
  (`WatchDeskResolution.warn_threshold_for`).
- `re_trigger_delta` is already stored per family (`floor_calibration`,
  migration `b033`).
- The monitor's family status is computed in the route (`_build_monitor`
  with `_build_family_group`, `_build_signal_group`, `_build_rss_group`),
  where the beat cannot reach it.
- The Watch Desk routes carry no owner gate today.
- Shirley lives in the shell's Shirley column (the dock), reachable from
  every Area. A case's "Consult Shirley" action briefs her through a
  per-session stash (ADR-0107 C6, `web/routes/chat.py`): a fenced
  `<<<PORTFOLIFLOW CASE BRIEF>>>` block appended to her system prompt at the
  chat call site by `_briefed_system_prompt`, with a banner and a dismiss
  action; no persistence (ADR-0050), web only.

## Decision

### 1. The briefing artefact

#### 1.1 Table `irene_briefing`

One tenant-scoped, append-only table. One row per **completed** beat
(`BeatResult.error is None`); a failed beat writes no row, because the tick
does not advance the schedule and the next tick retries it.

| Column | Type | Meaning |
|---|---|---|
| `id` | UUID, PK | |
| `tenant_id` | UUID, FK `tenants.id` RESTRICT | RLS key |
| `beat_at` | TIMESTAMPTZ, not null | the beat's clock (`now` of `run_beat`) — the stamp the Briefing shows |
| `written_at` | TIMESTAMPTZ, not null | set by the application when the row is written |
| `status` | TEXT, not null | `written` or `skipped` |
| `skip_reason` | TEXT, null | `llm_error`, `invalid_output`, `nothing_to_write`; set iff `status = 'skipped'` |
| `text_markets` | TEXT, null | Part 1 — "Markets and press"; set iff written |
| `text_watch` | TEXT, null | Part 2 — "Under observation"; empty string when every family is off; set iff written |
| `inputs` | JSONB, not null | the snapshot handed to the writer (§1.2) — the material behind the Sources line |
| `finding_ids` | UUID[], not null | the findings this beat appended (final urgency and band) — plain references, no FK, as `irene_finding.subject_key` is no FK |
| `model_id` | TEXT, null | `llm.model` of the call; null when no call was made |
| `created_at` | TIMESTAMPTZ, server default `now()` | |

Constraints:

- `UNIQUE (tenant_id, beat_at)`.
- CHECKs on shape, in the b033 idiom: status vocabulary; `skip_reason`
  present iff skipped and drawn from its vocabulary; both texts present iff
  written.

Identity and RLS as for `irene_finding` (ADR-0085): `apply_tenant_rls`,
**no** audit trigger. The row is a system artefact written by the tick with
no human actor; the b019 idiom applies. There is no `updated_at`, and the
repository offers append and reads only.

`no_credential` is deliberately not a reason: such a tenant never reaches
the beat.

Migration: one migration for everything this ADR stores (`irene_briefing`,
`watch_family_scope` §2, the family WARN columns §3). Its id is **`b036`**.
`b035` is reserved for `engagements` (provider-channel record
B-D-3/B-D-13) and is not yet landed. `down_revision` is the head at build
time.

#### 1.2 Inputs

The writer receives deterministic material only. Everything it receives is
persisted in `inputs`, so any sentence of the briefing can be checked
against what Irene was given.

- **Part 1 — markets and press (D-WD-2, D-WD-19, D-WD-20).**
  - A new **read-only market read** (proposed
    `services/watch_desk/market_read.py`, D-WD-14) over the whole priceable
    book:
    - every held instrument with an `instrument_prices` series — the set the
      route already enumerates for the price-watchpoint form;
    - every book currency against the functional currency, derived through
      `FxConverter` with the orientation rules `signal_observation.py`
      already applies to `fx_rates` legs;
    - a fixed window of **five days**;
    - per-instrument moves plus a value-weighted move per asset class, so
      the writer never has to compute an aggregate.
  - **Press clusters on `macro` and `regulator`**: the buckets
    `build_rss_buckets` forms at this check for those two tags, regardless
    of edge state, per-tag mute or the Press family's switch.
  - While Press coverage is **on**, clusters on the other five tags that are
    not muted are handed over as Part 1 material as well.
  - The buckets are formed **once** per beat and passed to both
    `evaluate_rss_deltas` and the writer. Embedding is the only model call
    in the RSS path and is not paid twice.
- **Part 2 — under observation.**
  - The live status of every **enabled** family from the new read-only
    **family-status service** (proposed
    `services/watch_desk/family_status.py`, D-WD-14). It is lifted out of
    `_build_monitor` and its helpers, and is the same code the watch strip
    renders from.
  - The findings this beat appended, with their final band.
  - The number of findings still open.
  - Disabled families are absent from the material, not marked as absent.

Each figure is a typed record (`id`, `label`, `value`, `unit`, `as_of`,
`source`); each cluster carries its key, tag, titles and sources. Window and
horizon parameters travel with the figures, so "over five days" is a given,
not an invention.

#### 1.3 The second synthesis step

After the findings are persisted — on the surfaced path **and** on the
silence path, which no longer returns before this step — `run_beat` calls
`run_synthesis` a second time:

- **Same resolved LLM** (`llm`, ADR-0112 §4b).
- **System prompt:** the briefing prompt (§1.6).
- **Tool:** one tool, `write_briefing(text_markets, text_watch)`, defined
  beside `surface_finding` (proposed `services/irene/briefing_tool.py`).
  Both parameters are strings and required; `text_watch` may be empty.
- **Tool choice forced.** `run_synthesis` gains an optional `tool_choice`
  argument whose default remains `"auto"`, so the finding call's tool
  choice is unchanged.
- **Output only as a structured tool call**, never as free text parsed from
  prose.
- **Validation.** Exactly one call carrying both strings is `written`.
  Anything else is `skipped / invalid_output`.

Grounding follows ADR-0088 §Grounding contract as a **prompt obligation**
(D-WD-15): Irene interprets the figures she was given and must not originate
one. No deterministic number check suppresses a briefing. The persisted
`inputs` make any violation visible after the fact, and the Sources line
(R12) sits under every briefing.

`nothing_to_write`: with no priceable instrument, no book currency other
than the functional one, no cluster and no enabled family, no call is made
and the row is `skipped`.

#### 1.4 Failure never costs the check

The briefing step runs inside a **savepoint** (`session.begin_nested()`):

- **LLM call raises:** the row is `skipped / llm_error`.
- **Writing the row itself fails:** only the savepoint rolls back, and the
  failure is logged.

In every case the beat completes: findings stay persisted,
`BeatResult.error` is untouched, and the tick advances the schedule.
`BeatResult` gains a `briefing_status` field for the tick's log line.

Page behaviour, calm by default: when the newest row is `skipped`, the
Briefing view renders the deterministic status (the watch strip) and one
line "Briefing pending" — never an error panel first. Earlier briefings
stay in the Journal.

#### 1.5 Reading and immutability

- The Briefing view shows the newest row by `beat_at`, stamped with it.
- The Journal lists every row (D-WD-9).
- Nothing is edited after write.
- "Check now" (`POST /api/watch-desk/request-analysis`) stays what it is: it
  brings the schedule due, and the briefing is written by the beat that
  follows — never by the request (D-WD-1).

#### 1.6 The persona section and prompt composition

Per D-WD-11 and D-WD-16, `docs/Soul_Irene.md` gains one H2 section,
**"Briefing Prompt"**, after the existing System Prompt section, carrying
its own fenced block. Every existing heading and its text stays
byte-identical. `get_system_prompt("irene")` keeps reading the first block
for the finding call; an explicit accessor (DC-WD-B names it) returns the
briefing block. The section's text as accepted is Appendix A.

Per D-WD-22, the composition separates the two personas **without taking
anything from Shirley**:

| Prompt | Composed of |
|---|---|
| Shirley — web chat and Telegram | temporal-grounding block · `Soul_Shirley.md` block · generated tool inventory · the two `docs/Shirley_*_Context.md` files · (web chat only) the active consultation brief, §6 — **unchanged, byte-identical** |
| Irene — finding call | temporal-grounding block · `Soul_Irene.md` System Prompt block |
| Irene — briefing call | temporal-grounding block · `Soul_Irene.md` Briefing Prompt block |

Why Irene's two prompts lose the appended material:

- **Tool access is not a prompt matter.** What a model can call is set by the
  API `tools` field of each call. Irene's check calls offer exactly one tool
  (`surface_finding`, `write_briefing`); that is unchanged, and nothing here
  changes Shirley's tools.
- **The appended text described someone else.** The inventory is headed
  "Your currently available tools" and lists Shirley's registry — tools
  Irene's calls cannot reach. The orchestration file introduces itself as
  how Shirley uses her tools and states that it defines how she works. In
  Irene's prompt both told Irene she was someone else, with capabilities she
  did not have, beside a Soul that says she has exactly one tool.
- **Irene's fallback**, used when her Soul file is missing or malformed,
  introduces Irene as the Watch Desk's background analyst instead of
  Shirley. It keeps the temporal-grounding prefix, as every fallback does.

Tool-assisted analysis of what Irene saw is Shirley's work, reached through
the hand-off in §6 (D-WD-24). The division is deliberate: Irene watches and
writes from deterministic material, several times a day and cheaply; Shirley
discusses, on demand, with everything she has.

#### 1.7 Cost

One additional LLM call per completed beat. The Watch Desk offers cadences
from `daily` to `hourly` (`_CADENCE_CHOICES`), so at most 24 additional
calls per tenant and day, plus manual checks. The two sub-hourly members of
the scheduling vocabulary (ADR-0125 §1) are not offered on this surface.
Dropping the Shirley material from Irene's finding prompt shortens every
existing finding call. A discussion with Shirley (§6) costs what any Shirley
turn costs; it is started by the reader, never by the check.

### 2. Watch-point scope

#### 2.1 Structure

One tenant-scoped, **historised, audit-triggered** table
`watch_family_scope`, named for its grain (one row states one family's
scope). It follows the ADR-0116 §1 revision model exactly: immutable version
rows keyed by `effective_from TIMESTAMPTZ`, never updated in place.

| Column | Type | Meaning |
|---|---|---|
| `id` | UUID, PK | |
| `tenant_id` | UUID, FK `tenants.id` RESTRICT | RLS key |
| `family` | TEXT | CHECK over the closed vocabulary below |
| `effective_from` | TIMESTAMPTZ | revision key |
| `enabled` | BOOLEAN, not null | the switch |
| `preset` | TEXT, null | the preset **applied by this revision** (`quiet` / `standard` / `nervous`) — an audit record only, never read for display; `custom` is never stored (D-WD-21) |
| `changed_by` | UUID, FK `users.id` RESTRICT | the actor, so the Watch points page and the D-WD-17 note can name them without reading `audit_log` |
| `note` | TEXT, null | |
| `created_at` | TIMESTAMPTZ | |

Constraints and triggers:

- `UNIQUE (tenant_id, family, effective_from)`.
- `apply_tenant_rls`.
- `audit_trigger_function()` attached, capturing the b001 audit GUC
  (ADR-0036 §1d) exactly as `watchpoints` and `floor_calibration` do.

**Default.** A tenant with no row for a family has that family **enabled**
at the calibration it already has — today's behaviour is the default. No
seeding.

#### 2.2 Family vocabulary

Closed, the seven of `core/models/watchpoint.py`. Display names as in M-WD-1:

| Key | Display name |
|---|---|
| `saa` | Allocation limits (SAA) |
| `anlv` | Regulatory quotas (AnlV) |
| `rss` | Press coverage |
| `price` | Price moves in held instruments |
| `fx` | Currency moves |
| `liquidity` | Cash coverage of calls |
| `freshness` | Stale valuations (NAV freshness) |

**Plan pacing** is a display-only placeholder, "not yet available". It has
no storable key: like b033, the CHECK does not list `pacing`. The successor
that lands the Planning Desk's pacing engine adds it.

#### 2.3 Resolution

`resolve_watch_desk` resolves the effective scope at `as_of` alongside
everything else, and `WatchDeskResolution` gains the enabled-family set
(and, for the D-WD-17 note, each disabled family's `effective_from` and
`changed_by`). The beat and the monitor therefore cannot disagree about what
is switched on: the ADR-0116 §1 single-resolution promise, extended by one
field.

#### 2.4 Effect of `enabled = false` (D-WD-3)

- **The beat skips the family's delta.**
  - `saa` / `anlv`: inside `evaluate_internal_deltas`, per family.
  - The four defined families: inside `evaluate_signal_deltas` and
    `observe_signal_families`.
  - `rss`: `evaluate_rss_deltas` is not called. The `macro` / `regulator`
    buckets are still formed for Part 1 (D-WD-2).
- **Watch state.** No `irene_watch_state` upsert and no acknowledgement for
  the family while it is off. On re-enabling, the first check compares
  against the last acknowledged state and may raise an edge at once. That is
  intended: the first check after switching on tells the truth about now.
- **Surface.** The monitor, the watch strip and the watch list omit the
  family. Part 2 of the briefing omits it.
- **Open findings** of the family stay open and resolvable.
- **Mute** (ADR-0116 §3) is untouched, a separate layer.
- **Part 1** is unaffected.

#### 2.5 Switching off is guarded (D-WD-4, D-WD-17, D-WD-18)

Only the tenant owner may switch a family (D-WD-18). The route answers a
switch-off with an inline R7 confirmation (danger button beside a quiet
*Keep*) before anything is written. Its wording names:

- what stops from the next check;
- the number of open findings of the family, which stay open until
  resolved — this needs a new repository read of open findings per family,
  deriving the family from the `subject_key` prefix;
- that the change is recorded with the actor's name;
- for `saa` and `anlv`: that **breaches** are no longer raised by Irene, and
  that Back Office › Limits continues to show them.

While a quota family is off, the Briefing view carries a **standing
deterministic note** — for example "Regulatory quotas off since 30 Sep ·
S. Pinkernelle" — built from the scope row, not from Irene's text. Part 2
stays silent about the family (D-WD-3); the page does not.

Switching **on** needs no confirmation.

### 3. Sensitivity presets

#### 3.1 Where the values live (D-WD-12)

`floor_calibration` gains six nullable columns, one per scalar family:
`warn_pct_saa`, `warn_pct_anlv`, `warn_pct_price`, `warn_pct_fx`,
`warn_pct_freshness`, `warn_pct_liquidity` (`Numeric(6,3)`, like
`warn_default_pct`). NULL means **inherit the tenant-wide WARN default**.

WARN resolution becomes: per-subject overlay → family column → tenant
default → `DEFAULT_WARN_THRESHOLD_PCT`. The step is added once, in
`WatchDeskResolution.warn_threshold_for`, and therefore reaches the coverage
classification, the signal producers' Approaching band and the monitor's
gauge mark together. The ADR-0116 §6 gauge rule is unchanged: the mark sits
at the warn fraction, now the family's.

Bounds are those of the tenant default: `50 < value < 100`, validated in
`save_calibration_revision`.

#### 3.2 The mapping

| Preset | Approaching at (`warn_pct_<family>`) | Re-trigger delta (`re_trigger_delta_<family>`) |
|---|---|---|
| Quiet | 95 % | 8 |
| **Standard** | **90 %** | **5** |
| Nervous | 80 % | 3 |

- **Standard** is defined **by reference** to the code defaults
  (`DEFAULT_WARN_THRESHOLD_PCT`, `_DEFAULT_RE_TRIGGER_DELTA`), so a later
  default change moves Standard with it.
- **Quiet** and **Nervous** are named constants in one module (proposed
  `services/watch_desk/presets.py`).
- **Delta units** follow the family's magnitude as implemented (roadmap
  #057 deviation record):
  - percentage points for `saa`, `anlv`, `price`, `fx`;
  - days of NAV age for `freshness`;
  - points on the 100-scale for `liquidity`.
- Presets never touch floors, caps, band boundaries or the options gate. A
  preset decides **when Irene notices**, never **how urgent** a finding is.
  An AnlV breach stays critical whatever the preset.
- **Press coverage** has no preset (D-WD-13): `rss` carries no scalar
  magnitude, its delta is 0, and b033 lets an `rss` overlay carry mute
  alone.
- Only the tenant owner may apply a preset (D-WD-18).

#### 3.3 One source of truth

**The preset writes the underlying values; the underlying values decide the
displayed preset.** Advanced tuning and the presets therefore can never
disagree.

- **Applying a preset** writes, in one transaction:
  - a `floor_calibration` revision through `save_calibration_revision`,
    carrying the family's WARN and delta;
  - a `watch_family_scope` revision recording the applied preset for the
    audit trail (D-WD-21).
- **Round trip.** When the save path reduces a family's WARN to a stored
  deviation, it compares against the tenant's **effective tenant-wide
  default in that revision**, not against the code default. Otherwise
  Standard would be stored as NULL, inherit a tenant default of, say, 85 %,
  and display as Custom.
- **Display.** A family displays the preset whose two values both equal its
  effective family values. Otherwise it displays **Custom**.
- **Per-subject overrides** (ADR-0116 §3) win for their subject and do not
  change the family's displayed preset. The watch list's Sensitivity column
  shows the family's preset for a subject without an override and "Custom"
  for a subject with one.

#### 3.4 Advanced tuning

The existing calibration editor becomes the collapsed "Advanced tuning"
panel (D-WD-6). Its one functional change is six fields for the family WARN
columns; nothing else is redesigned. It becomes **owner-only** (D-WD-6,
D-WD-18), a new gate on both its read and its save route.

### 4. Vocabulary

UI copy only:

- "beat" becomes **"check"**;
- "subjects" disappears from copy;
- "Calibration" becomes **"Watch points"**;
- the sections are **Briefing · Watch points · Journal**;
- internal keys (`saa:listed_equities`, `fx:USD/EUR`) leave visible copy
  (ui-standards §2.6.4) and live behind the R12 provenance disclosure.

Code identifiers, table names, module names (`modules/watch_desk/briefing.py`,
`calibration.py`, `journal.py`) and the ADR-0086 / ADR-0119 terms stay.
Whether the section slug `calibration` changes is an A-6 decision.

### 5. Journal semantics

The Journal stays a **projection** (ADR-0107 C4): no journal table.
`irene_briefing` is the artefact the Journal reads, not a journal of its own.

Three entry kinds in one day-grouped list:

- **briefing** — one per check, silent checks included, carrying the count
  and bands of the findings it raised via `finding_ids`;
- **resolved finding**;
- **closed case**.

Open findings stay on the Briefing view, as today.

D-WD-9 and D-WD-10 apply verbatim:

- "The Journal shows every check as a row, silent checks included ('Check at
  20:03 · nothing material'), grouped by day and collapsible per day."
- "The Journal filter 'Watch point' hides briefing rows whenever a family is
  selected; findings and cases filter by their family. Nothing is deleted by
  a filter."

The other filters are kind (All · Findings · Cases · Briefings) and period.
Because every check is a row — up to 24 a day at hourly cadence — the
Journal loads **by day**, with older days on demand, instead of the current
fixed `limit=100` per source. The period filter is applied server-side.

### 6. Discuss briefing with Shirley (D-WD-23, D-WD-24)

**Placement.** The action "Discuss briefing with Shirley" sits under every
**written** briefing: on the Briefing view and on each written briefing row
of the Journal. A skipped row offers no action — there is nothing to
discuss.

**Mechanism — the ADR-0107 C6 idiom, generalised by one kind.**

- The per-session brief stash holds **one active consultation context**, of
  kind `case` or `watch_briefing`. Setting one replaces the other.
- The banner names what is active — for example "Discussing Irene's briefing
  · 29 Sep 08:02" — and the existing dismiss action clears it.
- Ephemeral (ADR-0050): no persistence, no new table. Web chat only; the
  Telegram surface is untouched, as for case briefs.
- The action opens the shell's Shirley dock **in place**, on the page the
  reader is on. It sets the stash through a CSRF-protected POST and opens
  the dock with the banner; the reader does not leave the Watch Desk. (The
  case flow's navigation to `/assistants?case=` stays as it is.)
- The briefing id is resolved inside the reader's tenant context. An unknown,
  foreign-tenant (RLS) or skipped row is dropped silently, exactly like a bad
  `?case=` marker.

**The brief block.** Rendered from the immutable `irene_briefing` row and
appended at the chat call site the way the case brief is
(`_briefed_system_prompt`), so Shirley's own prompt stays byte-identical
beneath it. Fenced as `<<<PORTFOLIFLOW WATCH DESK BRIEFING>>>` …
`<<<END PORTFOLIFLOW WATCH DESK BRIEFING>>>`, it carries:

- a lead line: the PM wants to discuss the Watch Desk briefing below; it was
  written by Irene, the Watch Desk's background analyst, at the check of
  `beat_at`; its figures were taken at that check;
- the two-clock rule: where Shirley's tools show different current figures,
  she says which is the check's figure and which is today's;
- both texts, `text_markets` and `text_watch`;
- the figures and clusters of `inputs`, one line each (label, value, unit,
  window, as-of; tag, titles, sources);
- the findings of that check: band, trigger and finding line;
- the families switched off at that check, so Shirley never claims Irene
  watched them (the D-WD-17 principle, carried into the conversation).

**What stays as it is.**

- Shirley keeps her full tool set and her orchestration guidance; the brief
  adds context, never restrictions.
- Nothing is written back to the briefing, which is immutable. There is no
  new pin kind: a discussion that deserves a record goes the existing way —
  open a case (from a finding, or manually) and consult Shirley for it.

## Not in scope

| Not built | Instead |
|---|---|
| A price ticker | The watch strip is the element (D-WD-7). |
| Editing the RSS tag vocabulary | Per-tag mute remains at the watch list's second level (ADR-0116 Non-goals). |
| Any change to the findings pipeline, the floor, the bands or the `surface_finding` contract | ADR-0088 stands. The briefing reads final bands as facts. |
| A deterministic number check on the briefing | D-WD-15: prompt obligation plus persisted `inputs`. If practice shows invented figures, a successor ADR may add a check. |
| Any change to Shirley's prompt, tools or orchestration guidance | Shirley's composition stays byte-identical beneath any brief; only Irene's prompts stop carrying Shirley's text (D-WD-22). |
| Tool calls by Irene during a check | Irene's calls keep their single tool (ADR-0088, D-WD-24). Tool-assisted analysis happens with Shirley, reached from the briefing (§6). |
| Pinning a Shirley discussion to a briefing | Briefings are immutable. A discussion worth keeping goes into a case, through the existing consultation pin (ADR-0107 C6). |
| A `pacing` family | A placeholder row only, until the Planning Desk's pacing engine exists. |
| Per-subject switches | Mute stays the second-level row action, open to every member (D-WD-3a, D-WD-18). |
| Sensitivity presets for Press coverage | On/off only (D-WD-13). |
| Changes to the cadence vocabulary or its choices | ADR-0119 / ADR-0125 unchanged. Only the panel moves (D-WD-6, A-6). |
| The surface | DC-WD-B builds the backend and the route data. A-6 builds the surface on M-WD-1: `pf-finding` (R8), `pf-meter` (R6), the provenance disclosure (R12), the peer decision set (§2.3 row 3), the tabular anatomy (§2.10 row 5), `pf-fresh`, and the three `type="number"` fields (R10). |

## Consequences

**Schema.** One migration, `b036`: `irene_briefing` (RLS, no trigger),
`watch_family_scope` (RLS plus audit trigger), six `warn_pct_<family>`
columns on `floor_calibration`. Reversible in the b033 idiom.

**Code, prompt-planning grade.**

- `AIServiceCore` prompt composition split by persona (D-WD-22), plus the
  briefing accessor.
- The consultation-brief stash in `web/routes/chat.py` generalised to a
  second kind, the `watch_briefing` brief block, the in-place open endpoint
  and its banner (§6).
- `services/watch_desk/family_status.py` and `market_read.py` (read-only;
  the family status is lifted from the route).
- `presets.py`.
- Scope repository and write path.
- `resolve_watch_desk` extended by scope and the family WARN step.
- `run_beat` restructured: no early return; one bucket formation; per-family
  skips; the briefing step in a savepoint.
- `run_synthesis` gains `tool_choice`.
- `briefing_tool.py`.
- Briefing repository with append and reads only.
- Open-findings-per-family read.
- Route data for the Watch points and Journal views, the R7 switch-off
  confirmation, and the D-WD-17 note.
- Owner gates for family switches, presets and Advanced tuning (D-WD-18).
- Advanced tuning: six fields.

**Documentation, landed with this ADR by the implementing chat.**

- `docs/adr/README.md`: the 0133 index row and a dated **Update** note in the
  idiom of the 0131/0132 notes; the next free ADR number advances to 0134.
- `docs/roadmap.md`: item **#069** "Watch Desk refinement — briefing
  artefact, watch-point scope, presets" in the Features table and as a
  detail block — status `in-progress (2026-09-30)`, priority P2, ADR-0133,
  dependencies #033 and #057, a note that UX strand A-6 consumes it; the
  next-free marker advances to `#070`; a change-log row.
- `docs/Soul_Irene.md`: the Briefing Prompt section of Appendix A appended
  after the last existing line; the file's existing lines stay
  byte-identical.
- `docs/architecture.md`: one paragraph after the "One resolution, one
  observation, three call sites" paragraph of the "Watchpoint registry and
  Watch Desk calibration" passage, naming the second synthesis step, the
  scope structure and the per-family WARN step — marked as decided by
  ADR-0133 until DC-WD-B lands them.

**Behaviour.**

- Every completed check leaves a Journal row.
- A quota family can be off, and the page says so while it is.
- Members who are not the owner can read the Watch points page and mute a
  subject, but not change what Irene watches.
- Irene's finding call no longer reads a description of Shirley's tools
  and orchestration as if it were her own; what she can call is unchanged.
- A reader can take any written briefing to Shirley without leaving the
  Watch Desk.
- A PM-heavy book has few priced instruments, so Part 1 will speak mostly
  of currencies and press; that is honest, not a gap.
- Rows accumulate at up to 24 a day per tenant. No retention policy is
  decided here.

**Test obligations for DC-WD-B.**

1. The beat completes when the writer raises, returns no tool call, or
   returns a malformed call: the row is `skipped` with the matching reason,
   findings persist, `BeatResult.error` is `None`, and the schedule
   advances.
2. A failing row write rolls back only the savepoint.
3. The silence path writes a briefing. A failed beat writes none.
4. `nothing_to_write` makes no LLM call.
5. Briefing rows are immutable: the repository has no update or delete.
6. Prompt composition (D-WD-22):
   - Shirley's composed prompt is byte-identical to today's, pinned by a
     test that builds the expected string from its parts.
   - Irene's finding prompt is exactly the grounding block plus the first
     Soul block, and her briefing prompt exactly the grounding block plus
     the Briefing Prompt block.
   - Neither Irene prompt contains the tool inventory or any text of
     `docs/Shirley_*_Context.md`.
   - Irene's fallback names Irene, not Shirley.
   - The finding call's `tools` and tool choice (`"auto"`) are unchanged,
     and Shirley's chat call still offers her full registry.
7. Scope revisions insert and never update. The effective scope is the
   latest `effective_from ≤ as_of`. With no rows, all seven families are on.
8. Preset ↔ value round trip, including a tenant-wide WARN that differs from
   the code default. Custom detection. A per-subject override leaves the
   family preset unchanged.
9. Family off:
   - delta skipped, no watch-state write;
   - open findings stay open and resolvable;
   - monitor and Part 2 omit the family;
   - with `rss` off, `macro` / `regulator` buckets still reach Part 1.
10. Re-enabling judges against the last acknowledged state.
11. Part 1 receives clusters on the other tags only while Press coverage is
    on and the tag is unmuted (D-WD-19).
12. Owner gates: a member receives a refusal on family switch, preset and
    Advanced tuning routes; mute stays open to members (D-WD-18).
13. The Journal "Watch point" filter hides briefing rows and deletes
    nothing. Silent checks appear as rows.
14. `test_watch_desk_single_resolution` is extended: scope and family
    status come from one resolution shared by the beat and the route.
15. The D-WD-17 note renders while a quota family is off and names actor
    and date.
16. Migration round trip (#039 design).
17. Discuss with Shirley (§6):
    - a written briefing sets a `watch_briefing` stash and opens the dock
      with its banner; a skipped, unknown or foreign-tenant id sets nothing
      and raises no error;
    - a briefing stash replaces a case stash and vice versa; dismiss clears;
    - the brief block carries the stamp, Irene as author, both texts, the
      inputs, the findings and the families off;
    - Shirley's base prompt is byte-identical beneath the block;
    - Telegram prompt assembly is untouched.

## Sequencing

D-WD-8 verbatim: "Sequence: concept ADR (this chat) → build (DC-WD-B) → UX
strand A-6 as a pure UI strand afterwards."

Two hand-offs:

1. **DC-WD-B**, the implementation, kicked off by MC from KO-WD-A +
   ADR-0133 + M-WD-1. Its first strand is D-WD-14: the family-status lift
   and the market read. The prompt-composition split (D-WD-22) lands before
   the briefing step, so the briefing accessor is built on the corrected
   composition. The Shirley hand-off (§6) — stash kind, brief block, open
   endpoint — follows the briefing step, since it reads its rows.
2. **UX strand A-6**, UI only (D-UX-A0), on M-WD-1, consuming the route data
   DC-WD-B delivers. M-WD-1 gains the "Discuss briefing with Shirley" action
   under the briefing and on written Journal briefing rows before A-6 builds
   on it.

## Appendix A — `docs/Soul_Irene.md`, Briefing Prompt section (text at acceptance)

Appended after the existing System Prompt section. The file is the canonical
source from acceptance on; later edits to the prompt follow the file, not
this appendix.

~~~markdown
---

## Briefing Prompt

Loaded by the Irene beat for its second call, the briefing step (ADR-0133
§1.6), through its own accessor — never together with the System Prompt
above, which stays the finding call's prompt unchanged.

```
You are Irene, the background analyst of the PortfoliFLOW Watch Desk. This
is the second call of a check. The first has already happened: the findings
of this check are decided, and deterministic rules have fixed every urgency
and band. Your one job now is to write the short briefing the portfolio
manager reads at the top of the Watch Desk until the next check.

### What you are given

- Markets and press: price moves of the held instruments that have prices,
  moves per asset class, currency moves of the book's currencies, and press
  clusters on macro and regulator — plus, where given, clusters on the other
  press tags. Each figure comes with its label, value, unit, window and date.
- Under observation: the live status of each watch point that is switched
  on, and the findings this check raised, with their final bands.
- A watch point that is switched off is not in your material. Do not
  mention it, and do not speculate about it.

### What you write

- You have exactly one tool: write_briefing. Call it exactly once, with
  two texts.
- text_markets — "Markets and press": two to four sentences. Read the market
  against the book: what moved in the holdings, which press clusters touch
  them, and whether any of it has moved a limit — not what the market did
  in general.
- text_watch — "Under observation": two to four sentences. Which watch points
  are calm, which one is approaching or triggered, and what this check
  raised. If no watch point is switched on, pass an empty string.

### Calm by default

- A quiet check is a good check. When little moved, say so in one sentence
  and stop. Do not fill the space.
- The bands you are given are facts. Repeat them; never argue, soften,
  raise or re-rank them. You do not propose urgency in this call.
- No advice. The findings carry the options where options earn their place;
  the briefing informs.

### Grounding — interpret figures, never invent them

- Every number you write must be one of the figures you were given, in its
  given unit and window. Do not compute new numbers, combine figures into
  new ones, or round a figure into a different claim. If a statement would
  need a number you were not given, leave the statement out.
- Press clusters carry titles and sources, not figures. State the
  qualitative fact and cite no number.
- The reader can open the sources beneath your text. An invented number
  would be caught and would destroy trust. When in doubt, say less.

### Tone

- Precise, calm, economical: an analyst's note to a colleague who is short
  on time. No filler, no forced urgency, no enthusiasm. Decision support,
  not investment advice; the portfolio manager decides.
```
~~~
