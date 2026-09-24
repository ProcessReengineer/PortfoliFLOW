# P-UX-A1a — Blotter and History onto `pf-table` · A-1 opens

**Strand:** UX A-1 (Transactions) · **Runs after:** P-UX-A0g
**Tip at session start:** `77c7958` — *"docs(reports): full-suite baseline
2026-09-23 at the end of A-0 … (UX A-0, P-UX-A0f)"*
**Before image:** `docs/ux/atlas/2026-09-23-5/` (commit `b1eced6`, the A-0 end
run) — bands `transactions/transactions--blotter.png`,
`transactions/transactions--history.png`, scenes
`transactions/scenes/{blotter,history}.png`

**Scope as executed:** the four list partials, one line plus its docstring in
`areas/_section.html`, one key in `web/icons.py`, four amended selectors in
`components/pf_components.css`, the retirement plus one scoped rule in
`components/transactions.css`, two context values in
`web/routes/transactions.py`, three test modules, the regenerated inventory,
this report. No panel, no composer, no wizard, no `shell.js`, no
`layout.css`, no `theme.css`.

---

## OPERATOR ACTION REQUIRED

**1. The commit is yours.** Nothing is staged. The command is at the end of
this report.

**2. P-UX-A0g is still uncommitted.** The tip reads `(UX A-0, P-UX-A0f)`, not
`(UX A-0, P-UX-A0g)`, and four A0g test files plus `docs/reports/P-UX-A0g-report.md`
sit dirty in the tree — see §Verify-first check 1. A0g is test-only and
touches no file this strand touches, so A1a was executed on top of it rather
than halted. **Commit A0g first, then A1a**, or the two land as one change.
The A0g report carries its own `git add` / `git commit` block.

**3. The after-image atlas run is due** — pixels changed on both bands.
`portfoliflow bootstrap` **first**: A0g left the dev DB empty and the gates
here truncated it again. The before image was itself captured against an
empty book (the blotter band reads *"Nothing in flight."*), so a bootstrap
that seeds no ticket will compare an empty `pf-empty` block against an empty
placeholder sentence and show almost nothing. To see the table, seed at least
one draft, one proposed and one booked ticket before the run.

---

## Verify-first

| # | Check | Result |
|---|---|---|
| 1 | `git log --oneline -1` · `git status --porcelain` | **FAILED, not blocking.** Tip is `77c7958 … (UX A-0, P-UX-A0f)`; the tree carries A0g's four test files plus its report, uncommitted. Escalated above; no overlap with this strand's files, so execution continued. |
| 2 | `wc -l` on the four partials | 42 / 71 / 100 / 71 — exact |
| 3 | `tx-btn` in the two row partials · tree total | 3 / 2 · **40** — exact |
| 4 | `pf-view__asof` | `_section.html:46` rendered only `{% if section_as_of %}`; `layout.css:461` the rule |
| 5 | `section_as_of=` in templates | nothing — no view filled the slot |
| 6 | the `pf-table` family · `is-open` writers | rules at `:119–129` and `:357–358`; **no writer** — `grep -rn 'is-open' web/static/js` empty |
| 7 | icon names | **none of `more`, `ellipsis`, `chevron-right`, `chevron-down` was a product name.** The stems `ellipsis.svg`, `chevron-right.svg`, `chevron-down.svg` are all vendored; the product names over them were `menu` → `ellipsis`, `forward` → `chevron-right`, `expand` → `chevron-down`. See §Deliberate deviations 1. |
| 8 | legacy class users outside the four | **none.** `tx-table`, `tx-row`, `tx-row__*`, `tx-filters`, `tx-blotter__hint` and `tx-detail-row` were used by the four partials and nothing else, so all of them could be retired. `tx-state` (9 templates) and `tx-num` (9) could not. |
| 9 | the product's as-of form | `overview.py:26` documents it; `_partials/overview_section.html:62` renders `As of {date} · Live data updated {HH:MM}` |
| 10 | `ls docs/ux/atlas/` | `2026-09-23-5` is the A-0 end run (`b1eced6`, 16:04Z) and is the before image; `-4` predates the A0s scene work |

---

## §1 — Blotter

`_blotter.html` (42 → 59 lines), `_blotter_row.html` (71 → 87).

1. **Table.** `<table class="pf-table">`; the header cells are unchanged text,
   the Amount header and cell carry `pf-num`, the currency is a
   `<span class="pf-table__unit">` beside the figure. `#tx-blotter` stayed on
   the wrapper — the cancel POST's swap target — and the wrapper lost its
   `tx-blotter` class, which had no CSS rule. Newest first, unchanged.
2. **Row.** `<tr class="pf-table__row" id="tx-row-{{ row.id }}">`; the id is
   the open gesture, on the family's `.pf-table__id button` control, with
   `aria-label="Open ticket #{n}"` — the record's own wording, and the only
   accessible name the button has once the visible text is just `#1045`. The
   trailing `pf_icon("forward")` closes the row (R6). See §Flags 1.
3. **Sub-lines** on `pf-table__sub`; the creating cell on
   `pf-table__creating`, `creating: <em>…</em>` verbatim.
4. **State.** `pf-state pf-state--{draft,proposed,approved}` — all three
   modifiers exist in the family by name.
   **`--dormant` has no user here.** The near-miss is `approved`: the v1
   lifecycle has no gesture that advances an approved ticket to booked, which
   is why the tests seed that status through `set_status`. But the *row* acts
   on it — Open, Impact and the cancellation are all offered — and §2.6.1
   reads dormant as a subject nothing acts on, not a status nothing advances.
   Its declared user stays the `auditor` role (§2.7.5).
5. **Actions.** One quiet secondary (Impact, `pf-btn pf-btn--quiet pf-btn--sm`,
   still absent on a draft per D-6f), then the cancellation as the single
   `pf-menu__item--danger` behind a `pf-menu`. The menu closes itself on the
   way out with `hx-on:click="this.closest('details').removeAttribute('open')"`.
   The panel that lands is untouched.
6. **Slot.** `<tr class="pf-table__slot"><td colspan="7" id="tx-detail-{id}"></td></tr>`
   — the id and the `closest('td')` contract the panels rely on are unchanged,
   and the cell is emitted with no whitespace inside so `:not(:empty)` holds.
   The occupant's ground is one scoped rule in `transactions.css` (§CSS below).
7. **`is-open` writer** — §CSS below.
8. **Hint.** The A-18 sentence is now
   `<details class="pf-more"><summary>When the cash effect shows</summary>`,
   sentence unchanged, in the same place above the table.
9. **Empty state.** `<div class="pf-empty"><p class="pf-empty__lead">Nothing in flight.</p></div>`
   — one sentence, no action. The record draws its `New transaction` primary
   here; the product's lives in the New section, and R1 keeps the accent off
   a list either way.

## §2 — History

`_history.html` (100 → 117), `_history_row.html` (71 → 81).

1. **Filters.** `pf-filters` with five `pf-filters__field`, `pf-label` above
   each control, `pf-select` on the three selects and `pf-input` on the two
   native `type="date"` inputs (§2.5.6 — R10's both-notations parser is for
   number entry and does not reach a date). Trigger, target and the absent
   submit button are unchanged.
2. **Row** as §1, with `id="tx-history-row-{id}"`. The open gesture is
   Details, in place: the id button carries a **leading**
   `<span class="pf-table__chev">`, which the family rotates 90° once a slot
   fills. See §Deliberate deviations 2 for what is inside that span.
3. **Outcome** on `pf-state pf-state--{booked,cancelled,reversed}` plus the
   `pf-table__sub` outcome line.
4. **Actions.** No quiet secondary; *Reverse booking…* is the one
   `pf-menu__item--danger`, rendered **only** when `row.reversible`. A row
   without one has no `<details>` at all. Target `#tx-reverse-{id}`, unchanged.
5. **Two slots**, both `pf-table__slot`, ids unchanged.
6. **Empty states** — both on `pf-empty`, sentences unchanged.
7. The reversal report include is untouched and still on `tx-*`.

## §3 — The as-of stamp (R9, §2.6.6)

1. `areas/_section.html:46` renders the span **unconditionally**, with
   `id="{{ section_slug }}-asof"`; the `As of` prefix moved inside the
   `{% if %}`. **`layout.css` needed no change.** `.pf-view__title-row` is a
   left-aligned flex box that is wider than its content, so an empty trailing
   flex item adds a 12 px column gap with nothing after it to push — there is
   no gap to see and no `:empty` rule to write. The docstring of the
   parameter says so, so the next reader does not re-derive it.
2. `_blotter.html` and `_history.html` each carry, as their first node,
   `<span class="pf-view__asof" id="{blotter,history}-asof" hx-swap-oob="outerHTML">As of {{ as_of }}</span>`.
   htmx lifts an out-of-band node out of the response before the main swap, so
   both the lazy `outerHTML` reveal and the cancel POST's `#tx-blotter` swap
   keep working unchanged.
3. `web/routes/transactions.py`, two lines:
   * **`:4270`** — `return _render(request, "_blotter.html", {"rows": rows, "as_of": _stamp(_now())})`
   * **`:4708`** — `"as_of": _stamp(_now()),` added to `_history_context`'s
     returned dict, which covers the filter GET and the reversal POST alike.

   `_stamp` and `_now` are the module's own single clock read (ADR-0127) and
   its own `YYYY-MM-DD HH:MM` station format, so the stamp reads exactly like
   every other timestamp in this Area. Both docstrings gained a paragraph
   saying the value is the **render moment**, not a property of the data.

## §4 — Tests

| Module | Moved | Added |
|---|---|---|
| `test_transactions_blotter.py` | `"118,080.00 EUR"` → `'118,080.00<span class="pf-table__unit">EUR</span>'` (twice) | `test_blotter_renders_the_table_family_and_no_primary`, `test_blotter_draft_row_has_no_impact_and_discards_from_its_menu`, `test_blotter_body_stamps_the_section_head_out_of_band`; two lines on the empty-state test (`pf-empty`, no primary) |
| `test_transactions_history.py` | four `class="tx-state tx-state--…"` pins → `pf-state` | `test_history_renders_the_table_and_filter_families`, `test_only_a_reversible_row_carries_a_menu`, `test_history_body_stamps_the_section_head_out_of_band`; two lines on the empty-state test |
| `test_transactions_area.py` | — | `test_every_section_head_carries_an_as_of_slot` — **this is where the shell pins live**, next to the section-anchor pin, because the slot is the section block's shape rather than one section's content |

Every id pin is unchanged by design: `tx-row-`, `tx-history-row-`,
`tx-blotter`, `tx-history`, `tx-detail-`, `tx-reverse-`. Each new test's
docstring names the rule it serves.

---

## The two rendered rows

Both captured through the test client against a live Postgres, with one
draft, one proposed, one booked and one cancelled ticket seeded.

**Blotter — the proposed row** (units, a station line, Impact present):

```html
<tr class="pf-table__row" id="tx-row-fae6b21e-0af3-4790-8d4e-ba096fdfcb0c">
    <td class="pf-table__id">
        <button type="button"
                aria-label="Open ticket #2"
                hx-get="/api/transactions/ticket/fae6b21e-0af3-4790-8d4e-ba096fdfcb0c"
                hx-target="#tx-composer-host"
                hx-swap="innerHTML">#2</button>
    </td>
    <td>Order · Buy</td>
        <td>
            iShares Core MSCI World
            <span class="pf-table__sub">1,200.0000 units @ 98.4000</span>
        </td>
    <td class="pf-num">118,080.00<span class="pf-table__unit">EUR</span></td>
    <td>2026-03-02</td>
    <td>
        <span class="pf-state pf-state--proposed">Proposed</span>
        <span class="pf-table__sub">by A. Weber · 2026-03-02</span>
    </td>
    <td>
        <div class="pf-table__actions">
                <button class="pf-btn pf-btn--quiet pf-btn--sm" type="button"
                        hx-get="/api/transactions/ticket/fae6b21e-0af3-4790-8d4e-ba096fdfcb0c/impact"
                        hx-target="#tx-detail-fae6b21e-0af3-4790-8d4e-ba096fdfcb0c"
                        hx-swap="innerHTML">Impact</button>
            <details class="pf-menu">
                <summary class="pf-btn pf-btn--quiet pf-btn--sm pf-btn--icon"><svg class="pf-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" role="img" aria-label="More actions for ticket #2" focusable="false"><circle cx="12" cy="12" r="1" />
  <circle cx="19" cy="12" r="1" />
  <circle cx="5" cy="12" r="1" /></svg></summary>
                <div class="pf-menu__list">
                    <button class="pf-menu__item pf-menu__item--danger" type="button"
                            hx-get="/api/transactions/ticket/fae6b21e-0af3-4790-8d4e-ba096fdfcb0c/cancel"
                            hx-target="#tx-detail-fae6b21e-0af3-4790-8d4e-ba096fdfcb0c"
                            hx-swap="innerHTML"
                            hx-on:click="this.closest('details').removeAttribute('open')">Cancel ticket&hellip;</button>
                </div>
            </details>
            <svg class="pf-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="m9 18 6-6-6-6" /></svg>
        </div>
    </td>
</tr>
<tr class="pf-table__slot"><td colspan="7" id="tx-detail-fae6b21e-0af3-4790-8d4e-ba096fdfcb0c"></td></tr>
```

**History — the booked row** (leading chevron, no quiet secondary, no
trailing chevron, the menu). Its Amount cell reads `&mdash;` because the
seed helper books through the service without a `net_amount`, not because
the cell lost anything:

```html
<tr class="pf-table__row" id="tx-history-row-4d0578a5-fcc1-4791-bc27-e0713b40435d">
    <td class="pf-table__id">
        <button type="button"
                aria-label="Details for ticket #3"
                hx-get="/api/transactions/history/4d0578a5-fcc1-4791-bc27-e0713b40435d"
                hx-target="#tx-detail-4d0578a5-fcc1-4791-bc27-e0713b40435d"
                hx-swap="innerHTML"><span class="pf-table__chev" aria-hidden="true">&rsaquo;</span>#3</button>
    </td>
    <td>Order · Buy</td>
        <td>
            iShares Core MSCI World
            <span class="pf-table__sub">10.0000 units @ 10.0000</span>
        </td>
    <td class="pf-num">&mdash;</td>
    <td>2026-03-02</td>
    <td>
        <span class="pf-state pf-state--booked">Booked</span>
        <span class="pf-table__sub">by A. Weber · 2026-03-02 09:00</span>
    </td>
    <td>
        <div class="pf-table__actions">
                <details class="pf-menu">
                    <summary class="pf-btn pf-btn--quiet pf-btn--sm pf-btn--icon"><svg class="pf-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" role="img" aria-label="More actions for ticket #3" focusable="false"><circle cx="12" cy="12" r="1" />
  <circle cx="19" cy="12" r="1" />
  <circle cx="5" cy="12" r="1" /></svg></summary>
                    <div class="pf-menu__list">
                        <button class="pf-menu__item pf-menu__item--danger" type="button"
                                hx-get="/api/transactions/ticket/4d0578a5-fcc1-4791-bc27-e0713b40435d/reverse"
                                hx-target="#tx-reverse-4d0578a5-fcc1-4791-bc27-e0713b40435d"
                                hx-swap="innerHTML"
                                hx-on:click="this.closest('details').removeAttribute('open')">Reverse booking&hellip;</button>
                    </div>
                </details>
        </div>
    </td>
</tr>
<tr class="pf-table__slot"><td colspan="7" id="tx-detail-4d0578a5-fcc1-4791-bc27-e0713b40435d"></td></tr>
<tr class="pf-table__slot"><td colspan="7" id="tx-reverse-4d0578a5-fcc1-4791-bc27-e0713b40435d"></td></tr>
```

---

## Measurements

**Gate 3 — `tx-btn` in `web/templates`: 40 → 35.** The five retired are the
three on the blotter row (Open, Impact, the cancellation) and the two on the
History row (Details, Reverse booking…). The remaining 35 are all in panels,
composers, the wizard and the outcome screens — 12 of them in the four panels
A1e takes.

**Gate 4 — legacy classes in the four partials: 0**, the `tx-` ids excepted.
The only `tx-` strings left are `id="tx-blotter"`, `id="tx-history"`,
`id="tx-row-…"`, `id="tx-history-row-…"`, `id="tx-detail-…"`,
`id="tx-reverse-…"`, the `#tx-composer-host` target, and two mentions in
template comments.

**Component users** (templates carrying the family, the §4 catalogue's unit):

| Family | Before | After | Which |
|---|---:|---:|---|
| `pf-table` | 0 | 4 | the two bodies and the two row partials — **2 surfaces** |
| `pf-menu` | 0 | 2 | the two row partials |
| `pf-filters` | 0 | 1 | `_history.html` |
| `pf-empty` | 1 | 3 | `base.html` plus the two bodies |
| `pf-more` | 0 | 1 | `_blotter.html` |
| `pf-state` | 0 | 2 | the two row partials |
| `pf-label` / `pf-select` / `pf-input` | 0 | 1 | `_history.html` |
| `pf-btn` | 8 | 10 | plus the two row partials |

`ui-standards.md` §4's Users column is not edited here — Flag 5 sends the
whole component-layer bookkeeping to the A-1 end edit.

**Gate 7 — `python tools/ux_inventory.py`, the `transactions` row:**

| | templates | element rows | headings | buttons | HTMX triggers | links | form labels | pills | tiles | routes | js calls | distinct nouns |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| before | 31 | 212 | 4 | 47 | 49 | 2 | 69 | 7 | 0 | 23 | 0 | 15 |
| after | 32 | 219 | 4 | **49** | 49 | 2 | 69 | 7 | 0 | 23 | 0 | 15 |

Flags for the Area: `dynamic` 38 → 46 and `from:aria-label` 0 → 2; every other
flag column is unchanged, **`icon-only` included, still 0** — the two menu
triggers are icon-only controls and get their accessible name from
`pf_icon("more", …)`, which is what keeps them off that list. `templates`
went up by one because `_blotter.html` now yields a row of its own: the
`pf-empty` block is recognised as an `empty_state` element, where the
`pf-section__placeholder` paragraph it replaced yielded nothing. That is the
tool seeing the empty state for the first time, not a new string.

**What moved in pixels, beyond the new controls.** The family's table differs
from the M-5 transcription it replaces in four ways the after-image will show:
header cells lose the uppercase + letter-spacing treatment for
`--ui-font-scale-xs` in tertiary ink; cell borders go from
`--ui-border-default` to `--ui-border-soft`; cells go
`vertical-align: middle` → `top`, so a row with a sub-line now hangs from its
first line; and row hover paints `--ui-background-secondary` rather than
`--ui-background-section`. All four are §2.4.1, not choices made here.

---

## CSS

**`pf_components.css` — the `is-open` writer, four amended selectors, no new
rule.** The prompt's shape, with the row's two relative selectors folded into
one forgiving relative selector list rather than repeated per rule:

```css
.pf-table__row:has(+ .pf-table__slot > td:not(:empty),
                   + .pf-table__slot + .pf-table__slot > td:not(:empty))
```

carried onto the row's background rule (`:125`), its border rule (`:126`) and
the chevron rotation (`:363`); and `.pf-table__slot:has(> td:not(:empty))`
onto the slot's border rule (`:134`). A five-line comment at `:120` says why.
The class survives untouched beside it, so a later script writer changes
nothing. A History row reads open when *either* of its two slots is filled;
a Blotter row is never followed by two slots, so its second selector cannot
fire.

**`transactions.css` — 123 lines removed, 20 added.** Retired:
`.tx-table` and its five descendant rules, `.tx-row__id`,
`.tx-row__creating` (+ `em`), `.tx-row__sub`, `.tx-row__actions`,
`.tx-detail-row td`, `.tx-detail-row > td:empty`, the whole `.tx-filters`
block, and `.tx-blotter__hint`. Kept: `.tx-state*` and `.tx-num`, which nine
other partials in this Area still use. One rule added:

```css
.pf-transactions .pf-table__slot > td:not(:empty) {
    padding: var(--ui-space-4) var(--ui-space-5) var(--ui-space-5);
    background: var(--ui-background-secondary);
}
```

The impact panel's comment, which named the retired `.tx-detail-row td` as
the source of its ground, now names this rule.

---

## Gates

| # | Gate | Result |
|---|---|---|
| 1 | `pytest tests/web/test_transactions_{blotter,history,area}.py tests/web/test_{pf_components,css_tokens,icons}.py tests/regression -q` | **453 passed in 391 s** — green |
| 2 | `ruff check` · `ruff format --check` | clean |
| 3 | `grep -ro 'class="[^"]*\btx-btn\b' web/templates \| wc -l` | **35** |
| 4 | legacy classes in the four partials | **0** each, the `tx-` ids excepted |
| 5 | colour literals in the two sheets | none new — the two greps hit only prose in comments (`transactions.css:1`, `:1087`), which the token tests strip before counting |
| 6 | `git diff --name-only` | the 12 files of the scope plus `docs/ux/inventory/` (3 generated files) and this report — **plus A0g's five**, see Deviation 5 |
| 7 | `python tools/ux_inventory.py` | re-run, committed, numbers above |

`tests/web/test_icons.py` was added to gate 1 beyond the prompt's list: the
strand renames a key in `web/icons.py` and that module's own tests are the
ones that would catch a stem the vendored set does not ship.

---

## Deliberate deviations

1. **The icon names are not the prompt's.** `pf_icon("more", …)` is real —
   `web/icons.py` **renames** `"menu"` (zero users) to `"more"` rather than
   adding a second product name over the same `ellipsis` stem: the trigger of
   a `pf-menu` is a *More actions* control, which is what both the record
   (`I.more`) and the prompt call it. `pf_icon("chevron-right")` is **not**
   added: naming a vendored file from a template is the one thing the
   module's docstring forbids, and the two chevrons in this change are two
   different roles, not one glyph. The blotter's trailing one is
   `pf_icon("forward")` — R6 trailing = navigates, and `forward` is the
   navigation name the registry already holds. The History one is a glyph,
   not an icon — see 2. Net change to `web/icons.py`: one line.
2. **The History chevron is the record's `›` glyph, not an inline SVG.**
   `.pf-table__chev` is a 12 px inline-block, transcribed in A0d from a mock
   that puts a text character in it; a 16 px `pf_icon` inside it overflows,
   and the rule that would fix it (a size or an alignment) is a second
   component-layer edit this prompt's scope allows only for the `is-open`
   writer. `&rsaquo;` is what `.pf-more > summary::before` already draws, so
   the two disclosures in this Area now match. One line to change if the
   record wants the SVG.
3. **The `pf-menu` trigger keeps `pf-btn--quiet`.** The prompt's class list
   is `pf-btn pf-btn--icon pf-btn--sm`; the record's is
   `pf-btn pf-btn--quiet pf-btn--sm pf-btn--icon`. Without `--quiet` the ⋯
   trigger takes a border and a filled ground beside a borderless Impact
   button, which inverts the hierarchy R1 is about. Followed the record.
4. **The slot's padding is `pf-panel`'s, not the old pixels.** `.tx-detail-row td`
   gave its occupant `14px 18px 18px`. No token scale carries 14 or 18, and
   the destination is known: `.pf-panel` is `var(--ui-space-4) var(--ui-space-5)
   var(--ui-space-5)` = 16/24/24, which keeps the "more at the bottom" shape
   and makes A1e's retirement of the scoped rule a no-op rather than a second
   pixel change. Panels in the slots grow ~2/6/6 px of surround.
5. **Gate 6 cannot read clean.** Four A0g test files and the A0g report are
   in the diff because A0g is uncommitted — see OPERATOR ACTION 2. Nothing
   else outside the stated scope is touched.

---

## Flags

1. **Whole-row click** *(the prompt's)* — the record draws the `<tr>` as the
   gesture; the product puts it on the id button, because a row handler would
   also swallow the menu's `<summary>` and the Impact button. Whether a
   delegated handler (`pf_table.js`, ignoring `__actions` and `<details>`) is
   wanted is a record question — D-UX-S entry.
2. **Details does not close** *(the prompt's)* — the History detail panel is
   a projection with no gesture (A-17). With `is-open` now visible and the
   chevron rotated, a second click re-fetches the same panel and nothing
   moves, which reads as a bug. Either the panel gains a quiet *Close* (the
   Keep idiom) or the chevron toggles. Panel change, so A1e's.
3. **Signed figures** *(the prompt's)* — amounts on both lists are
   magnitudes; the direction is the flow label. `pf-pos` / `pf-neg` have no
   user here and ADR-0062 §2's wording question (Q-UX-D-14) stays open.
4. **As-of semantics for live lists** *(the prompt's)* — render time, by this
   prompt's decision, in the module's own `YYYY-MM-DD HH:MM` and in **UTC**,
   because `_now()` is UTC and this Area has no timezone of its own. The
   Overview renders its `HH:MM` in the market-data schedule's timezone
   instead, so the book now states two clocks. Ratify or replace at D-UX-S.
5. **`:has()` as the `is-open` writer** *(the prompt's)* — a component-layer
   decision for the record. `ui-standards.md` §4's `pf-table` row and §1's
   "where things are" should say so at the A-1 end edit, together with the
   Users column above.
6. **The menu's self-close is unverified in a browser.** `hx-on:click` and
   `hx-get` now sit on the *same* element for the first time in this tree —
   the three existing `hx-on:click` sites are all on buttons with no request.
   Both listeners fire on the one click and htmx does not check visibility
   before issuing, so it should hold; nothing server-side can pin it. Worth
   one click each on the blotter and History menus during the after-image run.
7. **The trailing chevron takes the row's primary ink.** `.pf-table__actions`
   gives its children no colour, so a bare `pf_icon` there reads louder than
   the `--quiet` buttons beside it. The record solves the same problem for
   `pf-flow` with a scoped `.pf-flow .pf-icon { color: … tertiary }`; the
   equivalent here is a second component-layer rule this prompt's scope did
   not allow. One declaration at the A-1 end edit.

---

## What A1b inherits

The composer host still lives in the hidden `new` section, so the Blotter's
Open now navigates — chevron and all — into a section the shell is not
showing: the ticket loads out of sight until Q-UX-A-5's `afterSwap` switch,
which is A1b's first item.

---

```bash
git add web/templates/_partials/transactions/_blotter.html \
        web/templates/_partials/transactions/_blotter_row.html \
        web/templates/_partials/transactions/_history.html \
        web/templates/_partials/transactions/_history_row.html \
        web/templates/areas/_section.html \
        web/icons.py \
        web/static/css/components/pf_components.css \
        web/static/css/components/transactions.css \
        web/routes/transactions.py \
        tests/web/test_transactions_blotter.py \
        tests/web/test_transactions_history.py \
        tests/web/test_transactions_area.py \
        docs/ux/inventory \
        docs/reports/P-UX-A1a-report.md
git commit -m "feat(transactions): blotter and history on pf-table — ticket number as the open gesture, one quiet secondary, destructive acts in a pf-menu, pf-state dots, pf-filters, pf-empty, as-of stamp filled out of band; CSS writer for pf-table is-open via :has() (UX A-1, P-UX-A1a)"
```

`tests/web/test_transactions_area.py` is added to the prompt's list: §4's
shell pin landed there.
