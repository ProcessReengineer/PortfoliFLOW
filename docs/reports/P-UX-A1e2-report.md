<!-- SPDX-License-Identifier: AGPL-3.0-only -->
<!-- Copyright (c) 2025-2026 Sönke Pinkernelle -->

# P-UX-A1e2 — the impact panel, the negative-cash indicator, `tx-num`

**Strand:** UX A-1 · **Ran after:** P-UX-A1e (`8a907cd`, clean tree) ·
**Scope as delivered:** the six Transactions partials, `investments/detail.html`
(markup only), `pf_components.css` (one transcription), `transactions.css`
(reduced to its residue), `docs/ux/atlas-scenes.json`, five test modules, the
inventory, this report.

After this prompt **no `tx-` class is drawn anywhere in the tree** and
`components/transactions.css` declares **no `tx-` class selector at all** — it
is 9 scoped rule groups, 33 declaration lines, over shared families.

---

## OPERATOR ACTION REQUIRED

1. **Commit.** The command is at the foot of this report. Nothing is committed
   on your behalf.
2. **`portfoliflow bootstrap`, then a ticket that breaches.** The impact panel
   needs a *proposed or approved order* ticket and a book with a limit set;
   the after-image and the browser round below both ride on one. The seeded
   test book breaches by construction (5,000 listed equity against 1,000 cash,
   AnlV capped at 35 %), so a small SAA or AnlV set over a real book will
   produce a `BREACH` flag without contriving anything.
3. **After-image atlas run.** §5 adds `transactions-blotter-impact-open`. It
   drives the UI and seeds nothing, exactly as A1e's history scene does, so it
   records a miss rather than failing if the tenant has no non-draft ticket.
4. **Browser round**, on `minathena-capital.localhost:8000`:
   - **Transactions → Blotter → a proposed ticket → Impact.** The panel opens
     in the row's slot with no box of its own — `pf-panel`'s ground is the
     slot's ground now. Head: title, the "Before → after…" sub, the state
     chip, and an **icon Close** at the right. Press it: the slot empties.
   - **Three lenses side by side**, each a titled column with a rule under the
     title and no card. Check the columns line up *across rows within a lens*
     — that is the one thing the five-cell contract buys.
   - **Foot: "How to read this."** Open it; the three lens footnotes and the
     FX sentence are inside.
   - **The banner on `/transactions`** with a cash position below zero: an
     amber note, the figure in red, "Why this is flagged" closed. Open it.
   - **The same note on that position's `/investments/{id}`**, under the
     meta block, above the action bar. It should look like the banner, because
     it is the same block.
   - **A clean book**: the banner is *gone*, not a gap. (It used to hold 18 px
     open — see Deliberate deviations 5.)

---

## Verify-first

| # | Check | Found |
|---|---|---|
| 1 | tip · tree | `8a907cd` … `(UX A-1, P-UX-A1e)` · clean |
| 2 | `wc -l` | 232 / 75 / 74 / **92** / 163 / 26 / 973 — `_order_confirmation.html` is 92, not the 91 the prompt states. One line of drift, nothing else; no file was off. |
| 3 | `tx-btn` in `_impact_panel.html` | **3** — the last three in the tree |
| 4 | the projection family in the record | **`~/DC-UX-D/shared.css:309–321`**, not `~/DC-UX-D/mock-sources/shared.css` — the mock sources sit at the top of `~/DC-UX-D/`, and `~/DC-UX-D-bundle/` holds the written record. `impactPanel()` is `mock01.src.html:227–260`. |
| 5 | `pf-neg` / `pf-pos` / `pf-num` | `pf-neg` and `pf-pos` declared (A1a flag 3); `.pf-table .pf-num` is **right-aligned, tabular, table-cell-only** — confirmed unusable for inline prose, which decided §3 |
| 6 | the record's negative-cash indicator | **It draws it.** `mock01.src.html:203–204` `notice()`: `pf-note--warn`, `pf-note__inline`, `pf-neg`, and the two sentences behind `<details class="pf-more pf-note__sub"><summary>Why this is flagged</summary>`. `mock01.src.html:110` states the intent: "kept as the area notice above every Transactions view; the two explanatory sentences move behind *Why this is flagged*." So §2 had a record shape to take, on both surfaces. |
| 7 | `investments/detail.html` | six lines, 49–58 and 54 — the block was replaced whole (lines 42–64 → 42–71); nothing else in that file moved |
| 8 | `tx-new` | has a rule (`transactions.css:62`, flex column + 14px gap) — see §3 |
| 9 | `tx-state` / `tx-lens` | **the prompt's premise was wrong**: both have a live user, and it is `_impact_panel.html`. `tx-state` 12 rules, `tx-lens` 4, zero *other* template users — so they retire here with their one surface, not as dead rules |
| 10 | Close idiom / requests | `this.closest('td').innerHTML = ''` ×3, and **no `hx-*` at all** — the panel asks the server nothing. Unchanged. |
| 11 | close icon | `web/icons.py:45` → `"close": "x"` |

---

## §1 — The impact panel

### The family, transcribed

`pf_components.css` gains the record's projection family verbatim, tokens
substituted — the strand's **eighth** component-layer addition:

| Class | `shared.css` |
|---|---|
| `.pf-lenses` | 309 |
| *(the container query)* | 310 — **omitted**, see below |
| `.pf-lens__title` / `span:last-child` | 311, 312 |
| `.pf-delta` | 313 |
| `.pf-delta__label` / `label small` | 314, 315 |
| `.pf-delta__before` / `__arrow` / `__after` | 316, 317, 318 |
| `.pf-delta__flag` / `--warn` / `--breach` | 319, 320, 321 |

Two readings the markup depends on, both recorded at the rule site:

* **`.pf-delta` is one grid per *lens*, not per row.** Its five tracks are
  `minmax(0,1fr) auto 14px auto minmax(52px,auto)` and its *row* gap is 6 px —
  a per-row grid would give every line its own tracks and lose the column
  alignment the family exists for. The prompt's §1 says "each row a
  `pf-delta`"; that is a misreading of the record, and the record won. A row
  therefore emits five cells always, including an **empty flag** where there
  is nothing to flag.
* **`.pf-lens` carries no rule.** The record's lens is a bare `<div>` whose
  only furniture is the rule-off title. So the template draws a bare `<div>`
  rather than a class with no stylesheet behind it.

The container query at `shared.css:310` is omitted with every other one in
that file — `.pf-main` declares no container, and `test_no_container_queries`
in `test_pf_components.py` enforces it. The narrow-width collapse lands with
the container declaration, as the P-UX-A0d report's flag already says.
`#FF6B6B` → `var(--ui-semantic-error)` and `var(--pf-warn)` →
`var(--ui-semantic-warn)`, the mappings the rest of the file already uses.

### The panel

| Was | Is |
|---|---|
| `.tx-impact` box (border, blue rail, own padding) | `pf-panel` — and the slot's `:has(> .tx-impact)` ground rule retires with it (A1e deviation 3, its named executioner) |
| `.tx-impact__head` + `__lead` + `__sub` | `pf-panel__head` / `__titles` / `__title` / `__sub` |
| `.tx-state--{status}` chip | `pf-state pf-state--{status}` |
| 3× `tx-btn tx-btn--sm tx-btn--ghost` "Close" in the foot | one **icon Close in the head** per state — `pf-btn pf-btn--quiet pf-btn--sm pf-btn--icon` with `pf_icon("close", "Close")`, the gesture byte-identical |
| `.tx-impact__basis` run-on `<dl>` | `pf-facts` |
| `.tx-lenses` / `.tx-lens` cards | `pf-lenses` + bare `<div>` |
| `.tx-lens__title` | `pf-lens__title` |
| `.tx-delta__label` + `.tx-delta__pair` (nested) | five flat cells, `pf-delta__label / __before / __arrow / __after / __flag` |
| `.tx-delta__badge--{ok,warn,breach}` | `pf-delta__flag` (+ `--warn` / `--breach`) |
| `.tx-delta__row--{warn,breach}` ink on the after value | **retired** — the flag carries the tone (deviation 2) |
| `.tx-msg--consequence` (the OP-07 date shift) | `pf-note pf-note--info` with `pf_icon("info")` |
| `.tx-msg--block` (the service refusal) | `pf-note pf-note--block` with `pf_icon("block")` |
| `.tx-msg--muted` (the scoped explanation) | the panel's own `pf-panel__sub` — see below |
| `.tx-msg--muted` (the FX sentence) + `.tx-lens__foot` ×3 + `.tx-impact__foot` prose | one `<details class="pf-more">` "How to read this", the record's own foot |
| `.tx-msg__code` (the engine string, mono) | a `pf-hint` line inside that disclosure — **no new mono member** |

**`tx-impact--none` → `pf-panel__sub`, not `pf-empty__lead`.** Both no-impact
states keep a *title* ("No impact panel for #1 · Buy …"), so there is
something for a sub to explain; `pf-empty` is the shape for a surface with
nothing on it at all and a way forward, which is not this. The `<strong>`
lead of each sentence is kept inside the sub. The `book_error` / `notice`
state keeps a **red note** rather than a sub, because that sentence is the
*service's own refusal wording* (A-7) and this Area states every refusal in
`pf-note--block`.

**What stayed beside its lens.** The SAA lens keeps one `pf-hint` line —
`saa_foot`, the per-class headroom this trade moves. It is a fact about
*this* trade rather than standing prose, and a disclosure would bury it; the
"no SAA limit set" branch is a `pf-hint` on the same footing (the A1b2
deviation-4 precedent for a visible sentence with nothing of its own to say).
Every other footnote moved into the foot disclosure, where the record has it,
copy unchanged.

Every `hx-*` and every id is unchanged. The panel still makes no request.

---

## §2 — The negative-cash indicator, on both surfaces

The record draws this one (check 6), so both surfaces took its shape rather
than an inferred one. **No second variant was needed and `tx-indicator*` is
gone** — flag 2 of the prompt does not fire.

`_negative_cash.html`:

* `.tx-indicator` → `pf-note pf-note--warn` with `role="status"` (the
  record's) and `pf_icon("warn")` in place of the typed `!`.
* the count sentence → `pf-note__lead`, unchanged copy.
* **one position** → its line rides inline in the lead as `pf-note__inline`,
  the record's exact shape. **Several** → one `pf-note__sub` per line; the
  record draws only the one-position case and this is the only mechanism the
  family offers for the rest. One Jinja macro renders the line either way, so
  the two placements cannot word it differently.
* `.tx-num--neg` → `pf-neg`; `.tx-indicator__since` → folded into the
  sentence ("… since 2026-01-15."), which is how the record writes it.
* the two standing sentences (`_MD9` and the A-18 hint) →
  `<details class="pf-more pf-note__sub"><summary>Why this is flagged</summary>`.
  This is the **third** placement of the A-18 sentence behind a `pf-more` in
  the Area, after the Blotter's (A1a) and the impact panel's foot.
* the wrapper keeps `id="tx-negative-cash"` and every `hx-*` attribute, and
  **loses its class**: with a `pf-note` inside it, it is furniture.

`investments/detail.html`, lines 42–71 (was 42–64) and nothing else in the
file: the same block, same classes, same disclosure, `id="inv-negative-cash"`
kept. Its lead differs because the sentence does — one position, and it is
the page's subject, so "This position stands at …" rather than a count. The
compact variant is gone; the record has no small note and none was invented.

---

## §3 — `tx-num` and `--neg`

| File | Was | Is |
|---|---|---|
| `_messages.html:61` | `tx-num tx-num--neg` | `pf-neg` |
| `_order_confirmation.html:73` | `tx-num tx-num--neg` | `pf-neg` |
| `_settlement.html:146,150` | `tx-num--neg` | `pf-neg` |
| `_negative_cash.html` | `tx-num tx-num--neg` | `pf-neg` |
| `investments/detail.html` | `tx-num tx-num--neg` | `pf-neg` |

`tx-num` alone → **no class**. It set only `font-family: mono`, and `pf-num`
is a table-cell family (right-aligned, `white-space: nowrap`, scoped to
`.pf-table`) that would have wrecked five inline sentences. So five figures
lose their mono. **The mono question gains no new member here** — the
holdouts stay the five ids A1c/A1b2/A1e left (`#tx-id-value`, `#tx-id-figi`,
`#tx-currency`, `#tx-md-currency`, `#tx-reverse-cause`), now declared in one
rule; the impact panel's engine string lost its mono in the same way and is a
`pf-hint` (A1c2 flag 4 is unchanged in shape, larger in consequence).

`_new_section.html`: **`tx-new` retired, class dropped.** It set a flex column
with a 14 px gap, and the host holds exactly one visible child — the chooser's
`<ul class="pf-flows">`, a composer's `<form class="pf-form">`, or a
confirmation's `<div class="pf-form__main">`, each of which brings its own
column. The OOB fragments beside them are hoisted out by htmx before layout.
The rule had nothing to space. The id stays: it is the composer's target and
the indicator's `from:` selector.

---

## §4 — The sheet to its residue

**822 → 150 lines**; 33 non-comment declaration lines; **9 rule groups**;
**0 `tx-` class selectors**. Retired in this prompt: `tx-impact*` (11),
`tx-lens*` (4), `tx-delta*` (11), `tx-indicator*` (7), `tx-negative-cash*`
(2, re-homed), `tx-msg*` (8) and `tx-msg__code`, `tx-state*` (9), `tx-num*`
(2), `tx-new`, `tx-btn` / `:hover` / `:disabled` / `--ghost` / `--sm` /
`a.tx-btn` (6), and the `:has(> .tx-impact)` slot rule.

Every selector that remains, with the prompt that scoped it and the record
answer that retires it:

| # | Selector | Scoped by | Retired when |
|---|---|---|---|
| 1 | `.pf-transactions #tx-id-value, #tx-id-figi, #tx-currency, #tx-md-currency, #tx-reverse-cause` | A1b2 / A1c / A1e (merged into one rule here) | the record names a `pf-mono` family |
| 2 | `.pf-transactions .pf-grid > .pf-field:has(> .pf-btn)` | A1c | the record answers what an action inside a field row looks like |
| 3 | `.pf-transactions .pf-context dd.is-missing` | A1c2 | the record names a missing state on `pf-context` |
| 4 | `.pf-transactions .pf-choice.is-refused` | A1b2 | the record draws a choice that refuses |
| 5 | `.pf-transactions .pf-field.is-missing input, … select` | A1b2 | the record names an invalid field beyond `pf-invalid` |
| 6 | `.pf-transactions .pf-note + .pf-filters` | A1e | the record spaces a note above a filter bar |
| 7 | `.pf-transactions .pf-panel .pf-leg, … .pf-form__main .pf-leg` (the **124 px** track) | A1e | the record answers how wide a leg's type may be |
| 8 | `.pf-transactions #tx-negative-cash` (`margin-bottom`) | P-5c, re-homed here | `.pf-main`'s section flow spaces a banner itself |
| 9 | `.pf-transactions #tx-negative-cash:empty` | P-5c, re-homed here | as 8 |

The `pf-choice__figure` wrap the prompt asks after has **no rule** and never
had one — `_settlement.html` draws the class, `pf_components.css` declares it,
and this sheet says nothing about it. The three "Resolve `:has()`",
`is-refused` and `is-missing` entries are rules 2, 4 and 3/5 above.

Rules 1, 8 and 9 are **id** selectors. They keep the `tx-` prefix because it
is the element's id and not a component name — the precedent A1c and A1b2 set
with the mono fields, and the reason gate 4 reads "no `tx-` *selector*" as "no
`tx-` class selector". `test_the_transactions_stylesheet_declares_no_tx_class`
now pins exactly that, plus that every remaining selector starts
`.pf-transactions`.

No colour literal; `test_css_tokens.py` green.

---

## §5 — The atlas scene

Expressible, so it landed:

```json
{"name": "transactions-blotter-impact-open", "area": "transactions",
 "start": "/transactions", "session": "tenant", "section": "blotter",
 "steps": [{"wait": ".pf-table__row"},
           {"click": ".pf-table__actions .pf-btn--quiet:not(.pf-btn--icon)"},
           {"wait": ".pf-table__slot .pf-panel"}],
 "shot": "blotter-impact-open"}
```

The click selector separates the Impact button from the row's overflow
summary, which is the same `pf-btn--quiet pf-btn--sm` plus `--icon`. The wait
is on `.pf-panel` rather than `.pf-lenses` so that a scoped or refused state
is still photographed rather than timing out; A1e's answer applies unchanged —
no seed step, the scene rides on the bootstrapped tenant, and a failing step
records `ok: false` rather than breaking the run. The **shot content**
therefore depends on step 2 of OPERATOR ACTION: without a proposed *order*
ticket the panel will be a scoped state.

---

## §6 — Tests

| Module | What changed |
|---|---|
| `test_transactions_impact.py` | `_after_value`'s marker `tx-delta__after` → `pf-delta__after` (and its docstring: the panel no longer strikes the baseline through, it puts an arrow between); the `tx-msg tx-msg--block` pin → `pf-note pf-note--block`. **New**: `test_the_panel_is_a_pf_panel_on_the_projection_family` (panel, head, `pf-facts`, `pf-lenses`, 3 lens titles, 3 `pf-delta` grids, **five cells on every row** via `_delta_rows`, and none of the six retired families), `test_the_head_closes_the_panel_with_an_icon_button` (`pf-btn--icon`, `aria-label="Close"`, the `closest('td')` idiom, and exactly **one** `<button>` in the panel), `test_a_breached_class_says_so_in_words_on_its_flag` (every `--breach` flag has a non-empty word; `BREACH` present) |
| `test_transactions_negative_cash.py` | two module constants — `_BLOCK` (`class="pf-note pf-note--warn"`) and `_LINE` (`class="pf-neg"`) — replace the `tx-indicator` / `tx-indicator__line` pins at six sites. **New**: `test_the_indicator_is_the_record_s_warn_note` (the block, one `pf-neg`, `pf-note__inline`, the `pf-more pf-note__sub` disclosure and its summary, both sentences still in the flattened copy, none of the three retired families, and still no gesture), `test_the_empty_wrapper_really_is_empty` (see deviation 5) |
| `test_investments_routes.py` | `tx-indicator--compact` → the **same two strings** the banner test pins, with a comment naming the contract between the two surfaces |
| `test_transactions_composer.py` | the projected-balance pin `tx-num--neg` → `pf-neg` |
| `test_pf_components.py` | `_EXPECTED_CLASSES` gains the nine projection classes. **New**: `test_the_transactions_partials_carry_no_bespoke_class` (tree-wide over `_partials/transactions/*.html`, docstring citing the strand and excluding ids explicitly) and `test_the_transactions_stylesheet_declares_no_tx_class` (comments stripped; every surviving selector `.pf-transactions`-scoped) |

Run serially, per the prompt. **The A1e Sentinel-tenant isolation artefact did
not recur** in any of these runs.

---

## §Component-layer additions

The eighth. Running list, for A1s's `ui-standards.md` §4 Users column:

| # | Family | Prompt | Source |
|---|---|---|---|
| 8 | `pf-lenses`, `pf-lens__title`, `pf-delta` + `__label` / `__before` / `__arrow` / `__after` / `__flag` (+ `--warn`, `--breach`) | **A1e2** | `shared.css:309–321`, transcribed; container query omitted |

One user today (the impact panel), the same position `pf-stepper` was in at
A1c (flag 4). The Watch Desk (A-6) is the likely second — a before/after
across several lenses is what a watchpoint's calibration preview is.

---

## Measurements

| | Before | After |
|---|---|---|
| `tx-btn` class uses in the tree | 3 | **0** |
| `tx-` class sites under `_partials/transactions/` | 90 | **0** |
| `tx-` class sites anywhere in `web/templates` | 95 (90 in the Area + 5 on the investment detail) | **0** |
| `transactions.css` lines | 822 | **150** (33 declaration lines, 9 rule groups) |
| `transactions.css` `tx-` class selectors | 62 | **0** |
| `pf_components.css` lines | 438 | 469 (+31: 13 rules, 18 comment) |
| `pf-panel` users | 3 | **4** |
| `pf-facts` users | 1 | **2** — not the 3 the prompt projects: §2 put the indicator's lines on `pf-note__sub`, which is what the record draws, rather than on `pf-facts` |
| `pf-neg` users | **0** — declared since A1a, drawn by nothing | **5** (four partials + the investment detail) |
| `pf-more` users | 7 | **10** |
| `_impact_panel.html` | 232 | 247 |
| `_negative_cash.html` | 75 | 94 |
| `investments/detail.html` | 973 | 981 |

---

## Gates

| # | Gate | Result |
|---|---|---|
| 1 | targeted pytest, serially | **green.** `test_transactions_impact.py` 20 passed; `test_pf_components.py` + `test_transactions_negative_cash.py` 173 passed; `test_css_tokens.py` + `tests/tools` 148 passed; `test_investments_routes.py -k "negative_cash or detail"` 5 passed; `test_transactions_composer.py -k "negative or message or settlement"` 6 passed. **Scope note below.** |
| 2 | `ruff check` · `ruff format --check` | clean (two test files reformatted, then re-run green) |
| 3 | `grep -ro 'class="[^"]*\btx-[a-z]' web/templates \| wc -l` | **0** — tree-wide, not only in the Area |
| 4 | `grep -c 'tx-' transactions.css` | 19, of which **12 are comment prose** and 7 are the id selectors of rules 1, 8 and 9. `grep '\.tx-'` → nothing. |
| 5 | colour literals in the two sheets | none |
| 6 | `python tools/ux_inventory.py` | re-run; 220 templates, 1240 elements, 169 routes; `elements.csv` and `summary.md` committed |
| 7 | `git diff --name-only` | the six partials, `investments/detail.html`, the two sheets, the scenes file, five test modules, the two inventory artefacts, this report — and nothing else |

**Gate 1 scope.** The operator asked mid-prompt to keep testing light, since
the next prompt closes the strand with a full-suite run. So gate 1 ran as
five targeted selections over the modules this prompt touches rather than the
whole `tests/web/test_transactions_*.py tests/web/test_investments*.py`
sweep, and **`pytest tests/regression -q` was not run**. Nothing here reaches
Python, a route or a migration — the diff is templates, CSS, JSON and tests —
but that is an argument, not a run, and it is A1s's to settle.

---

## Deliberate deviations

1. **`pf-delta` is one grid per lens, not per row** (§1). The prompt says "each
   row a `pf-delta`"; the record's `shared.css:313` and `impactPanel()` both
   say otherwise, and the 6 px *row* gap only means anything on a multi-row
   grid. The record won.
2. **`tx-delta__row--{warn,breach}` has no successor.** It inked the *scenario
   value* of a row whose status had **moved**. The record puts the tone on the
   flag and nowhere else, so every WARN/BREACH row now carries a coloured
   flag whether or not the trade moved it, and the "which rows moved" emphasis
   is gone from the ink. The sentence that explained it survives verbatim in
   the foot disclosure. Visible change; recorded rather than worked around.
3. **No `pf-delta__flag--ok`.** The record names two tones. An `OK` badge
   rides on the base flag — tertiary ink, the word carrying the whole signal,
   which is §2.11.1's own preference and reads correctly beside an amber WARN
   and a red BREACH. The template emits the modifier only for `warn` and
   `breach`; `_COVERAGE_TONE`'s third value is simply not drawn as ink.
4. **"Breaches on horizon" is flagless.** It used to ink its after-value red
   when the count rose. Under the flag-carries-the-tone rule that would need a
   *word*, and no truthful one exists that the record sanctions ("breach" on a
   count row reads as a verdict on the count). The row states `0 → 1`, and the
   quota rows above it each carry their own `BREACH`. No copy was invented.
5. **The empty wrapper is now genuinely empty** (`_negative_cash.html`). The
   `:empty` rule that collapses a clean book's banner has been inert since
   P-5c: the template indented its `{% if %}` body, so the wrapper always held
   a whitespace text node and `:empty` matched nothing — the element kept its
   18 px margin open on every clean render. The `{% if %}` now opens flush
   against the `>` and closes flush against the `</div>`, the rule fires, and
   `test_the_empty_wrapper_really_is_empty` holds it there. A pre-existing
   bug, fixed because this prompt owns the file and re-homed the rule.
6. **The banner's gap is 24 px, was 18 px.** `--ui-space-5`, which is what
   rule 6 already gives the other note-to-next-thing gap in this Area. 18 px
   is not a token and a second spacing vocabulary for one margin is not worth
   it. 6 px, visible only against the first section.
7. **Five figures lose their mono** (§3) and **the engine string loses its
   mono** (§1). Both follow A1c's precedent: the record names no `pf-mono`
   family, so nothing gains one here.
8. **`#tx-md-currency` merged into the mono-ids rule.** It was a separate
   single-selector rule saying the same thing for the same reason. One rule,
   one comment.
9. **`_order_confirmation.html` is 92 lines, not the 91 in Verify-first.** No
   action; recorded so the next prompt's table is not re-derived from a wrong
   number.

---

## Flags

1. **The projection family has one user.** Transcribed for the impact panel
   alone — the position `pf-stepper` was in at A1c (flag 4). The Watch Desk
   (A-6) is its likely second. Carried.
2. **~~`tx-indicator` survives for A-4~~ — does not fire.** The record draws
   the indicator (check 6) and both surfaces took the same block, so
   `tx-indicator*` is gone and the Area has no `tx-` class selector left.
3. **`pf-neg` now colours signed figures in five templates** — four in this
   Area and one in Front Office. It had **no user at all** before this prompt:
   A1a declared it (flag 3) and nothing drew it, so A1e2 is the family's first
   use, and it arrives while ADR-0062 §2's wording (Q-UX-D-14) is still open. The strand followed the record; **the record should close the
   question**, and it is now a cross-Area question rather than an Area one.
   Carried, widened.
4. **The record's `pf-delta__flag` vocabulary is upper-case** (`WARN`), and so
   is ours (`BREACH`, `OK`), because `_quota_row_view` passes
   `row.scenario_status` straight through. The two `< 0` flags and the
   `unchanged` flag are lower-case. Three registers on one row family. Not
   worth a fix without a record answer; **open question for A1s's list.**
5. **`.pf-lenses` does not collapse on a narrow view.** Three fixed tracks
   until `.pf-main` declares `container: view / inline-size`. The impact panel
   is the first surface where a missing container query has a *layout*
   consequence rather than a cosmetic one — three columns of figures in a
   table cell. Joins the A0d container-query flag with a named victim.
6. **The `role="status"` on the indicator is new**, taken from the record.
   A live region that re-renders on every booking will be announced by a
   screen reader each time. The record asks for it; whether an every-booking
   announcement is wanted is an accessibility question for A1s.

---

## One line for A1s

The strand-end obligations now due: the **full suite as a home run** (and
`tests/regression`, not run here — see the gate-1 scope note);
`docs/ux/ui-standards.md` — §2.5 rows 2/4/6/7, the §4 **Users** column, the
`pf-kv`→`pf-facts` rename, the **eight** component-layer additions, the
`:has()` writers and the 124 px `pf-leg` track; the **A-1 end atlas run**; the
Sentinel-tenant isolation artefact (did not recur in A1e2); and the open
record questions collected since A1a — the mono family (A1c2 flag 4, now five
ids plus two demoted strings), `pf-neg` vs ADR-0062 §2 (flag 3 above),
`pf-leg`'s type width and its missing `__sub`, the buy/sell ink A1c2 dropped,
a `pf-choice` that refuses, an action inside a `pf-field` row, a missing state
on `pf-context`, the flag register (flag 4 above), the container declaration
(flag 5 above) and the live-region question (flag 6 above).

---

## Render check (test client)

### The impact panel, on a proposed ticket with a breached class

```html
<div class="pf-panel">
    <div class="pf-panel__head">
        <div class="pf-panel__titles">
            <p class="pf-panel__title">Impact of #1 · Buy 10.0000 Equity Fund on the plan world</p>
            <p class="pf-panel__sub">
                Before → after, evaluated on the plan horizon
                (8 quarters after 2026-03-31). Read-only; computed from the book and
                this ticket, held nowhere.
            </p>
        </div>
        <span class="pf-state pf-state--proposed">Proposed</span>
        <button class="pf-btn pf-btn--quiet pf-btn--sm pf-btn--icon" type="button"
        hx-on:click="this.closest('td').innerHTML = ''"><svg class="pf-icon" … role="img" aria-label="Close" focusable="false">…</svg></button>
    </div>
    <dl class="pf-facts">
        <div><dt>Fed to the overlay</dt><dd>buy 10.0000 @ 20.0000 · 2026-06-30 · EUR · consideration 200.00</dd></div>
        <div><dt>Value leg</dt><dd>+200.00 EUR</dd></div>
        <div><dt>Cash leg (EUR)</dt><dd>&minus;200.00 EUR</dd></div>
        <div><dt>Costs</dt><dd>none</dd></div>
    </dl>
    <div class="pf-lenses">
        <div>
            <p class="pf-lens__title"><span>SAA drift</span><span>at horizon</span></p>
                <div class="pf-delta">
                        <div class="pf-delta__label">Pd class</div>
    <span class="pf-delta__before">100.0 %</span>
    <span class="pf-delta__arrow">→</span>
    <span class="pf-delta__after">100.0 %</span>
    <span class="pf-delta__flag pf-delta__flag--breach">BREACH</span>
                </div>
                <p class="pf-hint">No class headroom moves on this trade.</p>
        </div>
        <div>
            <p class="pf-lens__title"><span>Limit headroom · AnlV</span><span>at horizon</span></p>
            <div class="pf-delta">
                    <div class="pf-delta__label">Listed equity</div>
    <span class="pf-delta__before">0.0 %</span>
    <span class="pf-delta__arrow">→</span>
    <span class="pf-delta__after">0.0 %</span>
    <span class="pf-delta__flag">OK</span>
                    <div class="pf-delta__label">Unallocated</div>
    <span class="pf-delta__before">100.0 %</span>
    <span class="pf-delta__arrow">→</span>
    <span class="pf-delta__after">100.0 %</span>
    <span class="pf-delta__flag"></span>
                    <div class="pf-delta__label">Tightest headroom <small>EUR</small></div>
    <span class="pf-delta__before">2,030.00</span>
    <span class="pf-delta__arrow">→</span>
    <span class="pf-delta__after">2,030.00</span>
    <span class="pf-delta__flag"></span>
                    <div class="pf-delta__label">Breaches on horizon</div>
    <span class="pf-delta__before">8</span>
    <span class="pf-delta__arrow">→</span>
    <span class="pf-delta__after">8</span>
    <span class="pf-delta__flag"></span>
            </div>
        </div>
        <div>
            <p class="pf-lens__title"><span>Liquidity · EUR cash</span><span>plan path</span></p>
            <div class="pf-delta">
                <div class="pf-delta__label">On trade date <small>2026-06-30</small></div>
    <span class="pf-delta__before">1,000.00</span>
    <span class="pf-delta__arrow">→</span>
    <span class="pf-delta__after">800.00</span>
    <span class="pf-delta__flag"></span>
                    <div class="pf-delta__label">At t&#8320; + 4 Q <small>EUR</small></div>
    <span class="pf-delta__before">800.00</span>
    <span class="pf-delta__arrow">→</span>
    <span class="pf-delta__after">600.00</span>
    <span class="pf-delta__flag"></span>
                    <div class="pf-delta__label">AUM <small>Σ NAV incl. cash</small></div>
    <span class="pf-delta__before">5,800.00</span>
    <span class="pf-delta__arrow">→</span>
    <span class="pf-delta__after">5,800.00</span>
    <span class="pf-delta__flag">unchanged</span>
            </div>
        </div>
    </div>
    <details class="pf-more">
        <summary>How to read this</summary>
        <p>
            Rows unchanged by the trade are shown for context; only the
            moved rows carry a status change. The booking is never refused
            for a breach (D-2 / MD-5): this is the consequence, stated
            before the trade.
        </p>
        <p>
            A hypothetical trade re-allocates; it does not fund. The
            negative balance is the same fact the composer's negative-cash
            warning states at proposal, now placed in time.
        </p>
        <p>
            <strong>FX exposure unchanged.</strong> An order settles in the
            investment's own currency, so value and cash move in one
            currency and the exposure mix cannot shift. Same figures the
            Planning Desk would show for this trade as a hypothetical.
        </p>
        <p class="pf-hint">
            Engine: insert_transaction → plan frames → coverage
            · ADR-0104 §2/§5
        </p>
    </details>
</div>
```

The SAA lens breaches (one class holding 100 % against an 80 % ceiling), the
AnlV lens shows a calm row, an unallocated row with no flag and two context
rows, and the liquidity lens shows the cash draw and an unchanged AUM. Eight
rows, eight flags, five cells each.

### The indicator on `/api/transactions/negative-cash`

```html
<div id="tx-negative-cash"
     hx-get="/api/transactions/negative-cash"
     hx-trigger="load, htmx:afterRequest from:#tx-composer-host, htmx:afterRequest from:#history"
     hx-target="this"
     hx-swap="outerHTML">
        <div class="pf-note pf-note--warn" role="status">
            <svg class="pf-icon" … aria-hidden="true" focusable="false">…</svg>
            <p class="pf-note__lead">A cash position is below zero. <span class="pf-note__inline"><a href="/investments/c43f58a7-…">EUR Cash — Commerzbank</a> stands at
    <span class="pf-neg">−108,900.00 EUR</span>
    since 2026-01-15.</span>
            </p>

            <span></span>

            <details class="pf-more pf-note__sub">
                <summary>Why this is flagged</summary>
                <p>
                    The position stays flagged until the balance is back at
                    zero or above. A booking's cash effect is in the book at
                    once; the cash position's balance shows it from the next
                    price date on.
                </p>
            </details>
        </div>
    </div>
```

With a clean book the same endpoint returns
`…hx-swap="outerHTML"></div>` — no whitespace, so `:empty` matches.

### The same block on `/investments/{id}`

```html
<div class="pf-note pf-note--warn" role="status" id="inv-negative-cash">
                <svg class="pf-icon" … aria-hidden="true" focusable="false">…</svg>
                <p class="pf-note__lead">This position stands at
                    <span class="pf-neg">−108,900.00 EUR</span>
                    since 2026-01-15.</p>
                <span></span>
                <details class="pf-more pf-note__sub">
                    <summary>Why this is flagged</summary>
                    <p>
                        The position stays flagged until the balance is back at
                        zero or above. A booking's cash effect is in the book at
                        once; the cash position's balance shows it from the next
                        price date on.
                    </p>
                </details>
            </div>
```

---

## Commit

```bash
git add web/templates/_partials/transactions/_impact_panel.html \
        web/templates/_partials/transactions/_negative_cash.html \
        web/templates/_partials/transactions/_messages.html \
        web/templates/_partials/transactions/_order_confirmation.html \
        web/templates/_partials/transactions/_settlement.html \
        web/templates/_partials/transactions/_new_section.html \
        web/templates/investments/detail.html \
        web/static/css/components/pf_components.css \
        web/static/css/components/transactions.css \
        docs/ux/atlas-scenes.json \
        docs/ux/inventory/elements.csv docs/ux/inventory/summary.md \
        tests/web/test_transactions_impact.py \
        tests/web/test_transactions_negative_cash.py \
        tests/web/test_transactions_composer.py \
        tests/web/test_investments_routes.py \
        tests/web/test_pf_components.py \
        docs/reports/P-UX-A1e2-report.md

git commit -m "feat(transactions): impact panel on the record's projection family (pf-lenses/pf-delta transcribed), negative-cash indicator shared with the investment detail on pf-note, tx-num retired and signed figures on pf-neg; no tx- class left in the Area, transactions.css reduced to its scoped residue (UX A-1, P-UX-A1e2)"
```

---

## Not this prompt's

`docs/ux/ui-standards.md` and its §4 Users column, the D-UX-S entries, the
full suite and `tests/regression`, the A-1 end atlas run, the mono family
(A1s / the record).
