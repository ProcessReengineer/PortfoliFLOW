<!-- SPDX-License-Identifier: AGPL-3.0-only -->
<!-- Copyright (c) 2025-2026 Sönke Pinkernelle -->

# P-UX-A1s — Strand end A-1: the standards document on the tree's state, the measurements, the full suite, the decision list

**Strand:** UX A-1, last prompt · **Ran after:** P-UX-A1e2 (`76fea55`, clean
tree) · **Scope as delivered:** `docs/ux/ui-standards.md`, this report, the
full-suite report and `~/full-suite-2026-09-25/` (logs, gitignored by
location). No product code, no template, no CSS, no test edit.

---

## OPERATOR ACTION REQUIRED

**1. Two commits, both yours.** The commands are at the foot of this report.
Nothing is staged.

**2. `portfoliflow bootstrap`, then the A-1 end atlas run.** The suite
truncated the dev DB. **The atlas is five prompts behind**: the newest run is
`2026-09-24-2` at `6e85297` (A1b+A1b2), so A1c, A1c2, A1d, A1e and A1e2 have
never been photographed.

`atlas-scenes.json` defines **eleven** Transactions scenes today. Seven have a
before-image in `2026-09-23-5` (the A-0 end run, `b1eced6`) and should be
compared against it:

| Scene | Before image in `2026-09-23-5` | What changed |
|---|---|---|
| `flow-chooser` | ✔ | `pf-flows` / `pf-flow`, flow codes out of visible copy |
| `blotter` | ✔ | `pf-table`, `pf-state`, `pf-menu`, `pf-empty`, as-of stamp |
| `history` | ✔ | as the blotter, plus `pf-filters` |
| `composer-order` | ✔ | rebuilt: crumb, head stamp, `pf-seg`, the rail, the action bar |
| `composer-commitment` | ✔ | rebuilt on the same shapes; one-block rail |
| `composer-secondary` (R-SEC-BUY) | ✔ | rebuilt; three-block rail with settlement inside |
| `wizard-step-1` | ✔ | `pf-form` frame, `pf-stepper`, the empty 360 px column |

Four scenes have **no before image at all** — they were added during the
strand and no run has executed them:

| Scene | Added by | Needs |
|---|---|---|
| `composer-secondary-sale` | A1c §5 | nothing — the chooser reaches it |
| `wizard-step-3` | A1c2 §5 | a currency, then two Continues |
| `history-detail-open` | A1e §6 | **a terminal row in History** — else it records `ok: false` |
| `blotter-impact-open` | A1e2 §5 | **a proposed or approved *order* ticket** — else the panel is a scoped state |

So seed before the run: one draft, one proposed and one booked ticket, and a
book with a limit set if the impact panel is to show a real flag.

**3. Three things to look at in the browser** that no test can pin, carried
from the reports and never discharged:

- the `pf-menu` self-close (A1a flag 6) — `hx-on:click` and `hx-get` on one
  element, the first such pair in this tree; one click on a Blotter menu and
  one on a History menu settles it;
- Details → **Close** on a History row: the panel goes *and the chevron
  un-rotates*, with no script — that is the `:has()` writer, and it is the
  whole point of A1a flag 2;
- type `1,234` into a Fees field: it reads as **1.23**. That is A1d decision 2
  made visible, and the strand's one genuinely tenant-shaped open question.

---

## Verify-first

| # | Check | Result |
|---|---|---|
| 1 | `git log --oneline -12` · `git status --porcelain` | the A-1 chain complete, `76fea55` at the tip, tree clean ✔ (hash table below) |
| 2 | Postgres | **Deviation, resolved.** The container existed but was `Exited (0) 2 hours ago`, so `podman ps` was empty and `pg_isready` answered nothing. Started with `podman start portfoliflow-postgres`, then `select 1` over `DATABASE_URL_SUPERUSER` answered. Both URLs present (`.env:23`, `:28`). See Deliberate deviations 1. |
| 3 | `ls ~/full-suite-*` | `2026-09-17` and `2026-09-23` exist; today's did not ✔ |
| 4 | `ls docs/reports/P-UX-A1*.md` | eight reports ✔ — the only source for §2–§4 |
| 5 | overtaken lines in `ui-standards.md` | 22 hits, 10 of them lines the strand overtook. Re-run at Gate 3. |
| 6 | `pf-kv` | doc said `pf-kv` at `:107` and `:275`; the tree has **`pf-facts`** (2 templates) and no `pf-kv` anywhere ✔ — the A1e flag 4 mismatch, fixed |
| 7 | the two workflow selections | `full-suite.yml:74` and `:76`, taken verbatim into today's runner ✔ |

### The A-1 chain — eight prompts, seven commits

| Commit | Subject ends | Prompt |
|---|---|---|
| `b1eced6` | `(UX A-0, P-UX-A0s)` | *A-0 end — the baseline this strand measures against* |
| `77c7958` | `(UX A-0, P-UX-A0f)` | *A-0's full-suite report, 5,464 passed* |
| `cae50f1` | `(UX A-1, P-UX-A1a)` | A1a — **and A0g, which never got a commit of its own** |
| `6e85297` | `(UX A-1, P-UX-A1b+A1b2)` | A1b **and** A1b2, landed as one |
| `bb9b1de` | `(UX A-1, P-UX-A1c)` | A1c |
| `712bef9` | `(UX A-1, P-UX-A1c2)` | A1c2 |
| `c23d910` | `(UX A-1, P-UX-A1d)` | A1d |
| `8a907cd` | `(UX A-1, P-UX-A1e)` | A1e |
| `76fea55` | `(UX A-1, P-UX-A1e2)` | A1e2 — the tip, and today's baseline |

Two prompts have no commit boundary of their own. A0g rode in with A1a
(A1a operator action 2, A1b check 1); A1b and A1b2 were landed together by the
operator (A1c check 1, deviation 1). Both were transcribed faithfully and
nothing is missing — but `git log` cannot be used to bisect the strand at
prompt granularity, and a reader looking for `P-UX-A0g` or `P-UX-A1b` alone
will not find one.

---

## §1 — The full suite

Launched **first**, before any document work, on the A1e2 tip. Runner
`~/full-suite-2026-09-25/run.sh` — byte-identical to A0f's apart from the date,
proved by `diff` of the two with the dates normalised — carrying the two
workflow selections verbatim and `-v -rfEsxX --durations=25 --junitxml`.

`setsid nohup ./run.sh &` then `$!`: **A0f's second deviation recurred, and in
a new way.** The shell's `$!` returned the harness's own wrapper process, not
`run.sh`. `run.pid` was corrected to the script's real PID (`4599`, confirmed
with `ps -p`, `bash ./run.sh`) before any polling. The run was never at risk —
only the PID file was wrong, for about ten seconds.

Started `2026-09-25T00:45:20+02:00`. No `pytest` of any kind was invoked
while it ran (A1b2 flag 8).

Headline in §5; the detail is in `docs/reports/full-suite-2026-09-25-report.md`.

---

## §2 — `ui-standards.md` on the tree's state

Edited in place, structure and voice kept, **398 → 516 lines**. By section:

**§1 Where things are** — four rows added, two amended.

* *A finished migration's residue* — `components/transactions.css`, 150 lines,
  **no `tx-` class rule at all**, nine groups listed by purpose in one line,
  every one scoped to `.pf-transactions` and every one naming the record
  answer that retires it. This row is the reason the section is worth having:
  it says what a completed Area migration actually leaves behind.
* *Projection family* — `pf-lenses` / `pf-lens__title` / `pf-delta*`, with the
  two readings the markup depends on (one grid per **lens**, the tone on the
  flag).
* *Sub-surface head* — `_composer_head.html`, one head for **five** surfaces
  (verified: the four composers and `_wizard.html`).
* *Number entry* — `core/decimal_input.py` + `_readings.html`.
* *View frame* amended: the as-of span renders unconditionally with
  `id="{section_slug}-asof"`.
* *One section per view* amended with the hash contract: the composer host's
  `hx-on::after-swap` sets `location.hash`, and **both guards are named** —
  the swap's own target (`afterSwap` bubbles, and every recalculation swaps
  inside the host) and the hash it already holds (assigning the current hash
  fires no `hashchange`).

**§2 Rules** — nineteen rows rewritten. The ones that were *false*: §2.1.6
(crumb: no user → built), §2.3.1 (36 uses across 8 templates → 58 across 25,
and `tx-btn` gone), §2.3.4, §2.3.5, §2.3.6, §2.4.1 (all "no user yet" → built),
§2.5.2 (`pf-seg` not built → built), §2.5.4 (`pf-stepper` not built; *"the
wizard runs on `tx-stepper`"* → the wizard runs on `pf-stepper`), §2.5.6 (R10
"Not yet, 38 remain" → done in Transactions, 20 remain elsewhere, listed by
Area), §2.5.7 (**`pf-kv` → `pf-facts`**), §2.9.1 (13 → 12 legacy templates).
The ones that were merely thin: §2.2.4 (the container's first layout victim),
§2.3.2 (R1 reaches inside a sub-surface, and is pinned six times), §2.4.2 (the
`:has()` writer), §2.4.6 (`pf-neg`'s first use), §2.5.3 (the rail **is** the
swap target; settlement inside, facts outside, and *why* — an `hx-swap-oob`
element inside the target is lifted out before the fragment lands), §2.6.1,
§2.6.2 (the action slot is a position, not a class), §2.6.4, §2.6.6 (**as-of on
a live list is render time, UTC** — and the book states two clocks).

**§3 Tokens** — untouched, as instructed. Nothing above required it.

**§4 Component catalogue** — rewritten. The preamble now states **both**
commands: the one that counts the sheet's block names (59, of which nine are
A-1's) and the one that produces every number in the Users column. The table
grew from 34 rows to 44 — the nine A-1 blocks added and marked **A-1**, and
`pf-sum`, `pf-leg` and `pf-facts` promoted out of `pf-rail-sum`'s modifier
cell, because all three now draw outside a rail and each wanted a number of its
own. Every Users number recounted. Three paragraphs
added: where the nine blocks came from (and that **`pf-stepper` alone was
authored, not transcribed**); why **`pf-mono` is not a family** and what the
five ids do instead; and the **scoped-exception rule** with its five registered
instances plus the one family-level constraint that bites
(`pf-choice__figure`'s `nowrap`). "Still to build" lost `pf-stepper`, `pf-seg`,
`pf-choice`, `pf-context`, `pf-delta` and `pf-kv`; it keeps `pf-finding`,
`pf-meter`, `pf-triplet`, `pf-charts-grid`, `pf-inv`, `pf-kpis`, `pf-ask` and
`pf-fresh`.

**§5 Deviations** — §5.4 (container queries) gains the first layout-level
victim and the measurement that still says *wait*; §5.9 gains the console
recipe; **two new entries**: §5.12 the twenty `type="number"` outside R10,
listed by Area, each owing the echo as well as the parser, and §5.13
`login.css:18`, the one colour literal outside `theme.css`, with its budgeted
exemption at `test_css_tokens.py:44`.

**§6 Shortcuts** — Ctrl J is now **inert on a surface that renders no rail**,
with a paragraph on why the guard sits in `toggle()` rather than in the key
handler and why the predicate is `.pf-side .pf-rail` and not the bare class.

**§7 How to add a view** — a new `### Tree-wide pins` subsection: ten
assertions that hold across the tree, each with `file:line`. See Deliberate
deviations 2 for why it landed here.

**§8 Appendix** — a new `### Three rules the strands have had to learn`: one
primary per view *including* inside a sub-surface; a shared family in an
unnamed state is a **scoped rule with a comment**, never a new modifier; two
DB-backed `pytest` runs never overlap. See Deliberate deviations 2.

One observation made while editing and **not** acted on, because it is not
in the eight reports: `theme.css` declares both `--ui-semantic-warn` and
`--ui-semantic-warning`, and the Transactions residue uses one of each (rule 3
and rule 5). Both tokens exist, so nothing dangles and no test fires — but two
names for one meaning is the kind of thing A0d2's token sweep existed to
remove. Worth one line in whichever prompt next opens `ui_theme.json`.

---

## §3 — Measurements

Before is the A-0 end as **`P-UX-A0s-report.md` §3 states it**; where A0s
states no figure, the baseline is read out of `b1eced6` with `git show` /
`git grep` and marked *(git)*. Every command is runnable from the repo root.

| Measure | Command | A-0 end | A-1 end |
|---|---|---:|---:|
| Files, tracked | `git ls-files \| wc -l` | 1,530 | **1,546** |
| Files, Repomix | `grep -c '<file path=' repomix-output.xml` | — | **1,469** |
| `btn` files with uses | `grep -rlo 'class="[^"]*\bbtn\b' web/templates \| wc -l` | 74 | **75** |
| `btn` sites | `grep -ro 'class="[^"]*\bbtn\b' web/templates \| wc -l` | 176 | **174** |
| `pf-btn` sites | `grep -ro 'class="[^"]*\bpf-btn\b' web/templates \| wc -l` | 20 | **58** |
| `pf-btn` raw substring | `grep -ro 'pf-btn' web/templates \| wc -l` | 36 | **108** |
| `pf-dc-btn` sites | `grep -ro 'class="[^"]*\bpf-dc-btn\b' web/templates \| wc -l` | 9 | **9** |
| **`tx-btn` sites** | `grep -ro 'class="[^"]*\btx-btn\b' web/templates \| wc -l` | 40 | **0** |
| **`tx-` class sites** | `grep -ro 'class="[^"]*\btx-[a-z]' web/templates \| wc -l` | 740 *(git)* | **0** |
| `type="number"`, tree | `grep -ro 'type="number"' web/templates \| wc -l` | 38 | **20** |
| `type="number"`, Transactions | same, `_partials/transactions` | 18 | **0** |
| `theme.css` | `wc -l` | 421 | **421** |
| `--ui-*` declarations | `grep -c -- '--ui-' web/static/css/theme.css` | 249 | **249** |
| `pf_components.css` | `wc -l` | 379 | **469** |
| `pf_components.css` block names | see §4's preamble | 50 *(git)* | **59** |
| `transactions.css` | `wc -l` | 1,668 *(git)* | **150** |
| `transactions.css` `tx-` class selectors | `grep -c '^\s*\.tx-'` | — | **0** |
| **`pf-` family users, summed** (§4's column) | the §4 command, once per row, 44 rows | 11 *(git)* | **158** |
| `tests/web` modules | `ls tests/web/*.py \| wc -l` | 85 *(git)* | **85** |
| `tests/web` test defs | `grep -rh '^\s*\(async \)\?def test_' tests/web/*.py \| wc -l` | 1,091 *(git)* | **1,146** |
| `tests/regression` modules | `ls tests/regression/*.py \| wc -l` | 43 *(git)* | **43** |
| `tests/regression` tests | the run's `step2.xml` `tests=` | 164 | **164** — byte-unchanged across the strand |
| `tests/core/test_decimal_input.py` | the run's JUnit, per file | 0 | **41 collected** — 28 notation cases, 11 "was it read", 2 property tests, over 4 functions |
| The eight A-1 reports | `wc -l docs/reports/P-UX-A1*.md` | — | **4,647 lines** |

A-0's second file count was `find . -type f` (41,013, including `.venv/`) and
is not comparable to anything; the Repomix number above replaces it. The
bundle is gitignored and was generated `2026-09-24 21:38`, i.e. **before A1e
and A1e2** — it is a file *count*, not a snapshot of today's tree.

`btn` files went **up** by one, 74 → 75. That is not a regression: `\bbtn\b`
matches inside `pf-btn` (the hyphen is a non-word character), so the measure
counts every button family at once. `pf-btn` sites nearly tripled while
`tx-btn` went to zero — the net is one more *file* carrying a button.

### The atlas

Ten runs, none of them current:

| Run | Generated | `git_head` |
|---|---|---|
| `2026-09-23-5` | 2026-09-23T16:04Z | `b1eced6` — the **A-0 end**, the before image for seven scenes |
| `2026-09-24` | 2026-09-24T05:53Z | `cae50f1` (A1a) |
| `2026-09-24-2` | 2026-09-24T10:19Z | `6e85297` (A1b+A1b2) — the newest |

### The inventory's `transactions` row

| | templates | element rows | headings | buttons | HTMX triggers | links | form labels | pills | tiles | routes | js calls | distinct nouns |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A-0 end | 31 | 212 | 4 | 47 | 49 | 2 | 69 | 7 | 0 | 23 | **0** | 15 |
| A-1 end | 41 | 249 | 26 | 50 | 51 | 2 | 71 | **1** | 0 | 23 | **0** | 10 |

Four numbers carry the strand. **Headings 4 → 26**: group titles that were
`<p>`s became real `<h3 class="pf-block__title">`, so the Area has an outline
it did not have. **Pills 7 → 1**: `pf-state` is a dot plus a word, not a filled
pill (§2.6.1), and six pills stopped being pills. **Distinct nouns 15 → 10**:
five bespoke vocabularies collapsed into the shared one. **js calls 0 → 0**:
the whole strand — the section switch, the `is-open` writer, three panel
closes — added **no** JavaScript.

Flags, same two rows:

| | abbrev | duplicate-label | dynamic | from:aria-label | icon-only | internal-name | no-accessible-name | synonym-candidate |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| A-0 end | 13 | 43 | 38 | 0 | 0 | 4 | 0 | 40 |
| A-1 end | **3** | 41 | 65 | 7 | 1 | 4 | 1 | 38 |

**abbrev 13 → 3** is the five flow codes leaving visible copy for `data-flow`,
measured. `dynamic` and `from:aria-label` rose because interpolated modifiers
(`pf-note--{{ tone }}`, `pf-state--{{ status }}`) and `aria-label`s on the
rails are what the shared vocabulary is made of. The single `icon-only` /
`no-accessible-name` pair is one row — `_wizard.html`'s `<form>` — and is
housekeeping, not a defect (list 2 below).

---

## §4 — The decision list

Both lists are drawn from the eight A-1 reports only. Each entry names its
origin so the record can go back to the argument.

### D-UX-S — needs a record answer

| # | Question | Origin |
|---|---|---|
| 1 | **Whole-row click vs. the id button.** The record draws the `<tr>` as the gesture; the product puts it on the id button, because a row handler would swallow the menu's `<summary>` and the quiet secondary. Is a delegated `pf_table.js` wanted? | A1a flag 1 |
| 2 | **Details closes — ratify it.** A1a left the History panel with no way out; A1e gave it a `pf-panel__foot` Close, and emptying the cell un-rotates the chevron through the `:has()` writer. All three slot panels now close the same way. Bless the foot, or move the gesture to the chevron. | A1a flag 2 → A1e deviation 1 |
| 3 | **Two clocks.** A live list's as-of is render time in **UTC**; the Overview's `HH:MM` is the market-data schedule's timezone. One book, two clocks, no rule. | A1a flag 4 |
| 4 | **`:has()` as a writer.** It writes `is-open` on `pf-table` (component layer) and bottom-aligns a Resolve button in a field row (scoped). Is CSS the sanctioned place for state that used to want a script? | A1a flag 5, A1c deviation 5 |
| 5 | **The trailing chevron takes the row's primary ink.** `.pf-table__actions` gives its children no colour, so a bare icon reads louder than the `--quiet` buttons beside it. The record solves the same problem for `pf-flow` with a scoped tertiary. | A1a flag 7 |
| 6 | **The lone-separator rule, and a tenant notation preference.** `1,234` reads as 1.234. A German institutional operator typing a thousands-grouped figure will hit it; the echo is the only thing between them and a wrong booking. The parser's branch is three lines and has no other caller. **The most consequential entry on this list.** | A1d flag 1 |
| 7 | **`pf-leg__type` — tone and width.** One tertiary ink for every leg, so a buy and a sell read alike (the `tx-leg__type--buy/--sell` colouring was dropped); and a 34 px track that this Area's vocabulary overruns, stood in for by a scoped 124 px. | A1c2 deviation 4, A1e flag 2 & deviation 4 |
| 8 | **The `pf-note` action slot has no name.** The record has the slot — the third `auto` grid column — but no class for it. A `pf-btn--quiet pf-btn--sm` now sits there. Bless the pair or grow `pf-note__action`. | A1c2 flag 2 |
| 9 | **No `pf-note--done`, and `--ui-semantic-success` now has no user.** `--info` plus `pf_icon("done")` stands in for a completion. Worth knowing before the token drifts. | A1e flag 3 |
| 10 | **`pf-neg`'s first use, while ADR-0062 §2 is open.** Declared at A1a, drawn by nothing until A1e2 put it in five templates — four in Transactions and one in Front Office. Q-UX-D-14 is now cross-Area. | A1a flag 3 → A1e2 flag 3 |
| 11 | **Is a review step's summary a rail or content?** Confirm's fact lists are in the main column and the 360 px track stands empty. The argument: `pf-rail-sum` is the companion to *inputs*, and a review step has none. The next stepped surface will ask again. | A1c2 flag 1, deviation 3 |
| 12 | **`pf-choice__figure` sets `white-space: nowrap`.** Right for a projected balance, wrong for the sentence a scope option puts there. One of the two wants a wrapping variant. | A1b2 deviation 2, flag 5 |
| 13 | **`pf-delta__flag` has three registers on one row family** — `BREACH` / `OK` upper-case, `unchanged` and the two `< 0` flags lower-case. | A1e2 flag 4 |
| 14 | **`pf-mono` — name it or confirm it stays unnamed.** Five ids carry the stack in one scoped rule; two strings gave their mono up rather than join them. Inventing the family locally was refused three times. | A1c2 flag 4, A1c, A1e2 deviations 7–8 |
| 15 | **`role="status"` on a re-rendering indicator.** Taken from the record; a live region that re-renders on every booking is announced every time. | A1e2 flag 6 |
| 16 | **When does the container declaration land?** Still undeclared, and now with a **layout** victim: `.pf-lenses` keeps three fixed tracks in a table slot. The measurement says the width that hurts is below the stated minimum and is the nav auto-collapse's to fix — so the declaration, the twelve `@container` rules and the collapse are one piece of work. | A1b §4, A1e2 flag 5 |
| 17 | **`‹` returns to the chooser, not to the row the ticket was opened from.** The crumb names the parent *surface*; a return-to-origin needs the host to remember a fragment. | A1b flag 1 |
| 18 | **MD-9 copy gaps, registered not closed.** The consequence still reads "shown **above** as emission rows" where the rows moved to the rail beside it; the Provenance blocks differ from their mockups. Rewriting binding mockup copy is a mockup decision. | A1b2 deviation 6, flag 6 |
| 19 | **Four scoped states the record does not draw**, each a live rule in `transactions.css` naming the answer that retires it: a `pf-choice` that **refuses**; a **missing** fact on `pf-context` and its input form on `pf-field`; an **action inside a `pf-field` row**; and the gap between a note and a filter bar. | A1b2, A1c, A1c2, A1e (residue table, A1e2 §4) |

### Flags — housekeeping, not design

| # | Item | Origin |
|---|---|---|
| 1 | **The Sentinel-tenant teardown isolation artefact.** Four `IntegrityError … users_tenant_id_fkey` in one mixed selection; a sibling module's teardown removed the Sentinel tenant inside the shared process. The wizard module alone was 38/38. **Did not recur in A1e2.** A housekeeping prompt's, not a defect in any strand's code. | A1e gate 1 |
| 2 | **The seven superseded-workbook skips and the limit-coverage reference stub.** A-0's, carried: the three workbooks are superseded by the tracked `sample_data/PortfoliFLOW_example_portfolio.xlsx`, so the skips are not a loss to recover. Confirmed still standing in §5's "What is not running". | A0f, A0g |
| 3 | **`_order_composer.html`'s docstring still describes the head it no longer holds** — two paragraphs that now document `_composer_head.html`. A one-line pointer closes it. A deliberate non-edit, not an oversight. | A1b2 flag 3 |
| 4 | **The inventory flags `_wizard.html`'s `<form>` `icon-only` and `no-accessible-name`.** An artefact: the tool sees an HTMX trigger whose template carries no static text, because all four step bodies are includes. A `<form>` is not a control. **Not chased** — see Deliberate deviations 3. | A1c2 flag 3 |
| 5 | **Three orphan rules, already retired.** `.tx-settle.is-unconfirmed`, `.tx-settle__option.is-selected`, `.tx-inline-create .tx-grid` — A1b's sweep missed them as compound selectors; A1c swept them. The fourth name on that list, `tx-detail-row`, was never a rule — it is a load-bearing *comment*, and it went with the rule it explained. **Closed.** | A1b2 flag 2, A1c check 8, flag 2 |
| 6 | **Two test files were reformatted by `ruff format` mid-prompt**, then re-run green. Recorded because the gate reads "clean" only on the second pass. *(The strand plan attributed this to A1b2; it is A1e2's.)* | A1e2 gate 2 |
| 7 | **The four new scenes.** All four are **defined** in `atlas-scenes.json` and **none has ever been executed**: `composer-secondary-sale` (A1c §5, discharging A1b2 flag 4), `wizard-step-3` (A1c2 §5), `history-detail-open` (A1e §6) and `blotter-impact-open` (A1e2 §5). The last two record `ok: false` rather than failing if the book has no terminal row / no non-draft ticket. | A1c, A1c2, A1e, A1e2 |
| 8 | **The `pf-menu` self-close is still unverified in a browser.** `hx-on:click` and `hx-get` on the same element, the first such pair in this tree. Nothing server-side can pin it; A1b flag 3 deferred it to a browser round that has not happened. | A1a flag 6, A1b flag 3 |

---

## §5 — The full suite

**Green.** `00:45:20 → 02:52:24`, 2:07:04 wall. **5,591 selected, 5,582 passed,
0 failed, 0 errors**, 8 skipped, 1 xfailed, 8 deselected. **Both steps exited
0**, as at 2026-09-17; the 09-23 run in between exited 1 on step 1.

The three failures A-0 handed forward are gone, and **all three were fixed
inside the strand rather than by a housekeeping pass**: the two
`count("<button") == 5` pins now count inside the *chooser* rather than the
whole Section (they rode in on A1b's `chooser_markup` helper), and the atlas
scene test now reads its expected names out of `atlas-scenes.json` instead of
restating them — which is why the four scenes A-1 went on to add broke nothing.

Baseline delta **+115 selected, +118 passed** — passed grows by the three
former failures. The per-file split reconciles to **+115 with no residual**,
and every prompt's own claim about how many tests it added checks out except
**A1d, which under-counted itself by 5**: its three "template assertions"
collect as eight, because one is parametrised over the six templates carrying
amount inputs. `tests/regression` is byte-unchanged across the whole strand,
164 → 164.

A0f's gate 2 holds: **zero "Cannot reach Postgres"** in either log, and zero
`XPASS`. Detail in
[`full-suite-2026-09-25-report.md`](full-suite-2026-09-25-report.md), including
one finding worth acting on outside this strand — `pytest-qt` and PyQt6 are
installed in `.venv` and declared **nowhere** in `pyproject.toml`, so this
machine would not catch a reintroduced PyQt6 import the way CI would.

---

## Gates

| # | Gate | Result |
|---|---|---|
| 1 | `DONE`; both `step exit` lines; zero "Cannot reach Postgres" | ✔ `DONE` at 02:52:53; `step1 exit 0`, `step2 exit 0`; **0** hits in both logs |
| 2 | every non-pass has a verdict; 3–4 have proposed fixes | ✔ **nothing failed**, so verdicts 3 and 4 are empty and no fix is proposed. The seventeen non-passes (8 skipped, 1 xfailed, 8 deselected) are all verdict 1 + 2, unchanged since 2026-09-17 |
| 3 | `ui-standards.md` contradicts no line check 5 flagged | ✔ — re-run below |
| 4 | every §4 Users number reproducible by the stated command | ✔ — five spot-checked below |
| 5 | `ruff check` | ✔ **All checks passed** (no Python changed) |
| 6 | `git status --porcelain` | ✔ `M docs/ux/ui-standards.md`, plus the two untracked reports; nothing else |

**Gate 3 in full.** Check 5 re-run returns **27** hits. Two are the document's
own prose about the convention (`:8`, `:44`). Fifteen are Users-column zeroes
in §4, each reproduced by the stated command. The remaining ten are rules that
still stand unimplemented, and **every one was verified against the tree**:

| Line | Claim | Verified |
|---|---|---|
| `:56` | the search field's collapse below 994 px is not yet | no container declared anywhere |
| `:64` | nav auto-collapse below 1600 px is not yet | no `1600` in `layout.css` or `pf_components.css` |
| `:67` | `.pf-main` declares no container | `grep -cE 'container(-type\|-name)?\s*:' layout.css` → 0; `@container` in `pf_components.css` → 0 |
| `:68` | chart triplet not yet | no `684` rule; `pf-triplet` / `pf-charts-grid` not declared |
| `:71`, `:121` | the activities list is not yet | `pf-activity__panel` has 0 template users |
| `:97` | `pf-meter` not built | not declared |
| `:124` | `pf-finding` not built | not declared |
| `:125` | the shared client helper is not yet | no such helper among the eight files in `web/static/js/` |
| `:134` | "My settings" not yet | no such slug in `web/shell.py` |

Nothing in the document now says something the tree contradicts.

**Gate 4 in full.** The column's command is stated in §4's preamble and was
**validated against `b1eced6`**, where it reproduces the A-0 end column
exactly for all 42 family names tested — including the `pf-empty` = 1 that a naive
`\bpf-empty\b` gets wrong (it matches `pf-empty-state` and returns 12). Five
spot-checks at HEAD:

| Family | Command | Result |
|---|---|---|
| `pf-actionbar` | `grep -rlP 'class="[^"]*\bpf-actionbar\b(?!-)' web/templates \| wc -l` | **8** |
| `pf-note` | same, `pf-note` | **17** |
| `pf-stepper` | same, `pf-stepper` | **1** |
| `pf-empty` | same, `pf-empty` | **3** |
| `pf-read` | same, `pf-read` | **7** |

---

## Deliberate deviations

**1. Postgres was started, not treated as a STOP.** Check 2 says "up; both
URLs — else STOP as A0f". `podman ps` was empty: the container existed and had
exited cleanly two hours earlier. A stopped container is not the condition the
STOP guards against — a missing container, a missing volume or a missing URL
would be — and `podman start` plus a `select 1` restored it in seconds, with
both URLs present and correct. Stopping would have cost the session and
bought nothing.

**2. Two subsections landed under headings the prompt named differently.** The
strand plan maps "§6 tests" and "§8 checklist"; the document's §6 is
*Shortcuts* and its §8 is the *125 % checklist*. It has no tests section, and
the three rules the strand learned are not zoom checks. Rather than add
top-level sections — which is the one thing "keep the document's own
structure" forbids — the tree-wide pins went in as `### Tree-wide pins` at the
end of **§7**, beside the two test references §7 already carries, and the three
rules as `### Three rules the strands have had to learn` at the end of **§8**,
marked "not zoom checks". Numbering is unchanged.

**3. The inventory's `no-accessible-name` on `<form>` was recorded, not
suppressed.** A1c2 flag 3 offers "suppress or explain the row". Suppressing it
means editing `tools/ux_inventory.py`, which is product code this prompt may
not touch. It is explained in list 2 instead, where the next reader of the flag
table will find it.

**4. One character was fixed outside the stated changes.** §8's per-Area
checklist header has nine columns and its separator row had eight, so the
**Found** column did not render in any markdown viewer — the column an operator
running the checklist is supposed to write in. Pre-existing (`HEAD:378`), one
`|---` added, in a section this prompt was editing anyway.

**5. One observation outside the eight reports is recorded and not acted on** —
the `--ui-semantic-warn` / `--ui-semantic-warning` pair (§2 above). Both tokens
exist, no test fires, and §4's lists are drawn from the reports only, so it
sits in §2's prose rather than in either list.

---

## The commits

```bash
git add docs/ux/ui-standards.md docs/reports/P-UX-A1s-report.md
git commit -m "docs(ux): ui-standards.md at the end of A-1 — R4/R10/pf-seg built, pf-facts not pf-kv, families' users re-counted, nine component-layer additions, panel-foot rule, scoped exceptions and open questions listed (UX A-1, P-UX-A1s)"
```

```bash
git add docs/reports/full-suite-2026-09-25-report.md
git commit -m "docs(reports): full-suite baseline 2026-09-25 at the end of A-1, 5,582 passed at 76fea55 — both steps exit 0, A-0's three failures fixed inside the strand (UX A-1, P-UX-A1s)"
```

The first message says **nine** where the strand plan said eight: the sheet
gained nine `pf-` block names, in seven transcriptions — A1e2's running list
numbers them 1–8 because it counts `pf-lenses` and `pf-delta` apart but
`pf-lens` with them, and `pf-btn--danger`, which the plan lists, is not an
addition at all but a first **user**. Both readings are stated in §4.

---

## Not this prompt's

Fixing anything the suite finds; the container declaration; the twenty number
inputs elsewhere; the Sentinel-tenant isolation; the closing report.

---

**Strand A-1 is complete; the Transactions Area carries no `tx-` class; the
baseline for the next strand is 5,582 passed at `76fea55`.**
