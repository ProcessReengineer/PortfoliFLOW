# P-UX-A1c2 — Wizard steps Order and Confirm, the outcome bar · the rail on the figure step only

**Strand:** UX A-1 (Transactions) · **Runs after:** P-UX-A1c
**Tip at session start:** `bb9b1de` — *"feat(transactions): wizard on the form
vocabulary … (UX A-1, **P-UX-A1c**)"*. Working tree clean.
**Before image:** the post-A1c atlas run. `wizard-step-1` is the only wizard
scene photographed; steps 3 and 4 are not — §5 changes that for step 3.

**Scope as executed:** the five scope templates, the retirement in
`components/transactions.css` (one re-homed state rule), one new scene in
`docs/ux/atlas-scenes.json`, `tests/web/test_transactions_wizard.py`, the
regenerated inventory, this report.
**Not touched:** `_wizard_hidden.html`, `_wizard_recalc.html`,
`_order_derived.html`, `_messages.html`, `_effect_leg.html`, the four S5
panels, `web/routes/transactions.py`, `pf_components.css`, `layout.css`, any
`type="number"` (A1d).

---

## OPERATOR ACTION REQUIRED

**1. The commit is yours.** Nothing is staged. The command is at the end.

**2. `portfoliflow bootstrap` before anything visual.** The gates below
truncated the dev DB repeatedly.

**3. The after-image atlas run.** Two scenes:

- `wizard-step-1` — unchanged in intent, but re-photograph it: the step's
  action bar is untouched while the *form element above it* gained a
  conditional attribute block, and the empty 360px column must still read as
  empty.
- `wizard-step-3` — **new scene** (§5). This is the one to look at. The Order
  step no longer draws the amounts-and-settlement block full-width inside the
  main column; it is a sticky `pf-rail-sum` in column 2, beside the fields,
  exactly where the M-1 composer puts the same rail. The instrument facts sit
  at the top of the main column as a `pf-context` strip.

**4. The browser round.** Chooser → *Buy a new instrument* → type a currency →
Continue → Continue → type units and price → **watch the rail refresh** →
Continue → Back → Close.

Four things to watch:

- the rail recalculates on a keystroke in **Units** or **Price per unit** (the
  trigger moved from the step body up to the `<form>`; see Deliberate
  deviations 1);
- it also recalculates when you pick a **settlement position radio** or tick
  **Confirm**, which are *inside the rail*. This is the regression the trigger
  move prevents — on a bare step-3 wrapper those controls would no longer have
  reached it;
- the Execution and Provenance blocks keep an even gap between them (they are
  now direct children of `pf-form__main` and take that column's own `gap`);
- Continue to step 4: the two summary blocks are fact lists in the **main**
  column and the 360px column is empty again. That is deliberate — Flag 1.

---

## Verify-first

Under `source .venv/bin/activate`.

| # | Check | Result |
|---|---|---|
| 1 | tip / clean | `bb9b1de` (A1c) at the tip; `git status --porcelain` empty. ✔ |
| 2 | `wc -l` on the five | `_wizard` **68**, `_wizard_order` **103**, `_wizard_confirm` **107**, `_wizard_outcome` **111**, `_wizard_context` **41**. All four stated figures match; `_wizard.html` post-A1c is 68. ✔ |
| 3 | `tx-btn` per file · tree | 0 / 0 / 0 / **4** / 0 · tree **11**. ✔ |
| 4 | `type="number"` in `_wizard_order` | **4**. ✔ |
| 5 | where `_order_derived.html` sits | The form's **second child**, after `</div>` of `pf-form__main` and immediately before `</form>` — a bare `{% include %}` with no wrapper. That is the shape §1 copies. |
| 6 | which fact-list family A1b gave | **`pf-context`** — `_order_context.html:22` is `<dl class="pf-context" id="tx-context" …>`, each pair wrapped in a bare `<div>`. `_order_confirmation.html` has no fact list at all (it is the post-booking panel), so `pf-context` is the only candidate and §3's two cards take it. |
| 7 | does the record's `pf-note` have an action slot | **Yes.** `pf_components.css:92` — `.pf-note { grid-template-columns: 16px minmax(0, 1fr) auto; }`. The third `auto` column *is* the slot, and `_messages.html`'s own comment names it: *"The empty `<span>` between them fills the family's third grid column — the slot an inline action would take."* So §4.1's first branch applies: the step-2 button goes in the slot. No `pf-note__action` class exists; it is a position, not a class (Flag 2). |
| 8 | users after the five migrate | Per class, `class=` occurrences only (ids excluded): `tx-wizard-body` wizard only → **retire**; `tx-summary`, `tx-sumcard*` confirm only → **retire**; `tx-context*` wizard-context only (the `_order_context.html` hit is the *id* `#tx-context`) → **retire**; `tx-outcome`, `tx-actions*` wizard-outcome only (the `_order_outcome.html` hit is the *id* `#tx-outcome`) → **retire**; `tx-grid`, `tx-field*` `_wizard_order` only → **retire**; `tx-block` `_wizard_order` only → **retire**, but **`tx-block__title` stays** (`_history_detail.html`); `tx-legs__lead` confirm only → **retire**, but **`tx-legs`, `tx-legs__pending`, `tx-legs__group` stay** (`_history_detail.html`); `tx-msg*` stays (three panels + `_reversal_report.html`); `tx-messages` stays (`_reversal_report.html`); `tx-leg*` stays (`_effect_leg.html`). `tx-btn--primary` was **not** on the list and turns out to be wizard-only → retired too. |
| 9 | the recalc trigger | `_wizard_order.html:21`, on the step wrapper. It survives — but **not on that element**; see Deliberate deviations 1. |
| 10 | what a scene may do | Scenes are a flat JSON list of 15. `transactions-wizard-step-1` uses `wait` and `click`. `tools/ux_atlas.py:1729 apply_step` accepts **four** verbs: `click`, `fill` (`{selector, value}`), `wait`, `wait_ms`. A form fill is expressible, so §5 is on rather than flagged. |

---

## §1 — The rail as the form's second child

`_wizard_order.html` dropped its `{% include "_order_derived.html" %}`.
`_wizard.html` includes it instead, inside `<form>` and after the closing
`</div>` of `pf-form__main`, guarded `{% if step == 3 %}`:

```jinja
    </div>

    {# The one rail, on the one step that has figures to put in it. … #}
    {% if step == 3 %}
        {% include "_partials/transactions/_order_derived.html" %}
    {% endif %}
</form>
```

Rendered, step 3's `#tx-wizard-form` has exactly two element children:

```
[depth 0] <form class="pf-form" id="tx-wizard-form"
[depth 1]   <div class="pf-form__main">
[depth 1]   <aside class="pf-rail-sum" id="tx-derived" aria-label="Amounts and settlement">
[depth 1] </form>
```

Column 2 is filled on step 3 and empty on 1, 2 and 4 — R4's stable column with
its one rail. `#tx-derived` keeps its id, so `_wizard_recalc.html`'s first
include still lands on it by `outerHTML`; `hx-include="closest form"` on the
rail's settlement controls still resolves to `#tx-wizard-form` because the rail
is inside the form. Both are asserted in §6.

## §2 — Order

The step body's wrapper is gone **entirely** rather than becoming a bare
`<div>`, and the recalculation attributes moved verbatim onto the form. Why, in
full, is Deliberate deviations 1 — the short version is that the rail holds the
settlement radios and the cash mini-form, they carry no `hx-*` of their own,
and after §1 they are no longer inside the step body, so a trigger on the step
body would never fire for them.

The form's opening tag on step 3, as rendered:

```html
<form class="pf-form" id="tx-wizard-form"
      hx-post="/api/transactions/recalc"
      hx-trigger="input delay:300ms, change delay:300ms"
      hx-include="closest form"
      hx-vals='{"step": "3"}'
      hx-target="#tx-derived"
      hx-swap="outerHTML">
```

On steps 1, 2 and 4 the same tag renders as `<form class="pf-form"
id="tx-wizard-form">`, byte for byte what it was before.

Inside, in order: the context strip (§4.3) · `<section>` + `pf-block__title`
*Execution* + `pf-grid` with the six fields on `pf-field` / `pf-label` /
`pf-hint` / `pf-optional`, the four numerics `pf-input pf-input--num`, the two
dates native `pf-input` · `<section>` *Provenance*, the order composer's shape
verbatim (`pf-select` for the case, `pf-input` for Source and Note, Note
`--wide`) · the hidden-inputs include · the outcome include.
`tx-field--wide` → `pf-field--wide`, as A1b mapped it.

## §3 — Confirm

Two cards → two `<section>`s with `pf-block__title` *New investment* and
*Order*, each holding its facts as a `<dl class="pf-context">` — check 6's
family. The `dt`/`dd` pairs and their copy are verbatim; each pair gained the
bare `<div>` wrapper the family's own markup uses (`_order_context.html`), which
is what makes it wrap rather than stack. `is-missing` on a `dd` stays a state
class and its rule is re-homed (§4.4).

```html
<h3 class="pf-block__title">Order</h3>
<dl class="pf-context">
    <div><dt>Direction</dt><dd>buy (first purchase)</dd></div>
    <div><dt>Trade date</dt><dd>2026-09-18</dd></div>
    <div><dt>Units</dt><dd>950</dd></div>
    …
    <div><dt>Settles against</dt><dd>EUR Cash</dd></div>
</dl>
```

The legs are `pf-leg` rows exactly as `_order_derived.html` and the M-3 rails
draw them — `pf-leg__type`, a bare middle `<span>`, `pf-leg__amount`. The
record's family has **no** `--{{ txn_type }}` modifier and no `__what`, so
§3's conditional resolves to "it does not"; both are dropped rather than
invented (Deliberate deviations 4). The `create` row keeps its empty amount.
The lead "On booking, in one step:" is a `pf-hint` above the rows and the
pending sentence a `pf-hint` in the `else` branch. That is not an invention for
this step: `_secondary_sale_derived.html:60-80` (A1b2) already draws exactly
this — a visible `pf-hint` lead, then `pf-leg` rows with a bare middle `<span>`
carrying an `<em>`, then a `pf-hint` in the `else`. The Confirm block is a
one-for-one match:

```html
<p class="pf-hint">On booking, in one step:</p>
<div class="pf-leg">
    <span class="pf-leg__type">create</span>
    <span>Investment · Meridian European Mid-Cap Equity Fund</span>
    <span class="pf-leg__amount"></span>
</div>
<div class="pf-leg">
    <span class="pf-leg__type">buy</span>
    <span>Meridian European Mid-Cap Equity Fund <em>@ 42.1800</em></span>
    <span class="pf-leg__amount">+950.0000 units</span>
</div>
```

The summary stays in the **main column**. Flagged for the record (Flag 1) and
argued in the template comment: a `pf-rail-sum` is the companion to inputs, a
review step has none, and a 360px column holding the whole content would leave
the main column empty on the one step whose job is to be read.

## §4 — Outcome and context

**1. Messages.** `<div id="tx-wizard-outcome" …oob…>` with no class. The
`_messages.html` include is unchanged. The step-4 AnlV block is a
`pf-note--block` with its button **in the family's third grid column** — check
7 found the slot, so §4.1's first branch applies. Every `hx-*` on the button is
unchanged.

```html
<div class="pf-note pf-note--block">
    <svg class="pf-icon" …>…</svg>
    <p class="pf-note__lead">This wizard cannot finish without an AnlV category.</p>
    <button class="pf-btn pf-btn--quiet pf-btn--sm" type="button"
            hx-get="/api/transactions/wizard?step=2&ticket_id=e79f0ae3-…"
            hx-target="#tx-composer-host"
            hx-swap="innerHTML">Set the category in step 2</button>
    <p class="pf-note__sub">
        The regulatory classification is required for booking and for
        proposing. The ticket keeps everything you entered as a draft.
    </p>
</div>
```

The unconfirmed-position block is a `pf-note--warn`, its third cell the empty
`<span>` the family uses when no action belongs there. Glyphs go through the
icon registry as `_messages.html` does (`pf_icon("block")` / `pf_icon("warn")`)
— the typed `✕` and `!` are gone.

**2. The action bar** is `pf-actionbar`: Back leading (R3), the hint carrying
the middle as the bar's `flex: 1` spacer, then Close, then the step's gestures.
One primary per step (R1): Continue on step 3, Book now on step 4. The two `if`
ladders are verbatim; `disabled` logic, `hx-vals` and endpoints unchanged.
Step 4, as rendered:

```html
<div class="pf-actionbar">
    <button class="pf-btn pf-btn--quiet" type="button"
            hx-get="/api/transactions/wizard?step=3&ticket_id=2ca33f6c-…"
            hx-target="#tx-composer-host"
            hx-swap="innerHTML">Back</button>
    <p class="pf-actionbar__hint">Booking creates the investment and both ledger legs in one step.</p>
    <button class="pf-btn pf-btn--quiet" type="button"
            hx-get="/api/transactions/chooser"
            hx-target="#tx-composer-host"
            hx-swap="innerHTML">Close</button>
    <button class="pf-btn" type="button"
            hx-post="/api/transactions/draft" … >Keep as draft</button>
    <button class="pf-btn" type="button"
            hx-post="/api/transactions/propose" … >Propose</button>
    <button class="pf-btn pf-btn--primary" type="button"
            hx-post="/api/transactions/book" … >Book now</button>
</div>
```

**3. The context strip** is `<dl class="pf-context" id="tx-wizard-context"
…oob…>` in the shape of `_order_context.html`, the four items and their copy
verbatim, at the top of the step-3 body — the main column, never the rail
(A1b deviation 3: an element carrying `hx-swap-oob` inside the swap target is
lifted out of the fragment by htmx before the fragment lands).

```html
<dl class="pf-context" id="tx-wizard-context" >
    <div><dt>Instrument</dt><dd>Meridian European Mid-Cap Equity Fund</dd></div>
    <div><dt>Holding on trade date</dt><dd>0.0000 units</dd></div>
    <div><dt>Currency</dt><dd>EUR</dd></div>
    <div><dt>Last known price</dt><dd>— none yet —</dd></div>
</dl>
```

**4. `transactions.css`.** Retired, by name:

| Retired | Why it was orphaned |
|---|---|
| `.tx-block` | both wizard steps; they are `<section>` in `pf-form__main` now |
| `.tx-grid` | `_wizard_order` only → `pf-grid` |
| `.tx-field`, `.tx-field--wide`, `.tx-field__label`, `.tx-field__optional`, `.tx-field__hint`, `.tx-field--numeric input`, the four typed-input selectors and the two `:focus` selectors | `_wizard_order` only → `pf-field` / `pf-field--wide` / `pf-label` / `pf-optional` / `pf-hint` / `pf-input--num` |
| `.tx-context`, `.tx-context__item`, `.tx-context__label`, `.tx-context__value` | `_wizard_context` only → `pf-context` |
| `.tx-legs__lead` | confirm only → `pf-hint` |
| `.tx-outcome` | wizard outcome only; `.pf-note + .pf-note` and `pf-actionbar`'s own `margin-top` supply the rhythm |
| `.tx-actions`, `.tx-actions__spacer`, `.tx-actions__hint` | wizard outcome only → `pf-actionbar` / `pf-actionbar__hint` |
| `.tx-btn--primary`, `.tx-btn--primary:hover:not(:disabled)` | **not on §4.4's list**; the wizard's Continue and Book now were their last users, and the four S5 panels declare no primary |
| `.tx-wizard-body` | A1c flag 1's named executioner, discharged |
| `.tx-summary` + its 860px media query, `.tx-sumcard`, `.tx-sumcard h4`, `.tx-sumcard dl`, `.tx-sumcard dt`, `.tx-sumcard dd` | confirm only → two `<section>`s over `pf-context` |
| `.tx-msg__link`, `.tx-msg__link:hover` | the AnlV jump-back only → `pf-btn--quiet pf-btn--sm` in the note's slot |

**Re-homed** (one rule, within the prompt's budget of two):

```css
.pf-transactions .pf-context dd.is-missing {
    color: var(--ui-semantic-warn);
}
```

was `.tx-sumcard dd.is-missing { color: var(--ui-semantic-warning); font-family: inherit; }`.
The `font-family: inherit` went with the mono `dd` it was undoing — `pf-context
dd` is not mono, so there is nothing to undo. The token moved to the record's
`--ui-semantic-warn`, the one the `pf-note--warn` family beside it uses.

**Kept, against §4.4's expectation, because they still have users:**
`.tx-block__title` and `.tx-legs`, `.tx-legs__pending`, `.tx-legs__group`
(`_history_detail.html`); `.tx-messages` (`_reversal_report.html`); `.tx-msg*`
(the three panels and the reversal report); `.tx-leg*` (`_effect_leg.html`);
`.tx-btn` and `.tx-btn--sm` / `--danger` / `--ghost` (the four S5 panels). All
are A1e's.

Count: **1086 → 949** lines (−137), **143 → 108** top-level selectors (−35).
The file's header paragraph, its scoping bullets and its `.pf-transactions`
count were updated to match.

## §5 — The step-3 scene

Added, because check 10 found `fill` in the DSL. The walk was verified
server-side before it was written — a temporary test posted exactly what each
rendered step's form carries, and a draft with **currency alone** reaches step
3 (the trade date rides as a hidden input from the first render, and neither a
name nor an asset class is needed to *save* a draft, only to finish one).

```json
{
  "name": "transactions-wizard-step-3",
  "area": "transactions",
  "start": "/transactions",
  "session": "tenant",
  "section": "new",
  "steps": [
    {"wait": "#tx-composer-host"},
    {"click": ".pf-flow:has-text(\"Buy a new instrument\")"},
    {"wait": "#tx-wizard-form"},
    {"fill": {"selector": "#tx-currency", "value": "EUR"}},
    {"click": "#tx-wizard-form .pf-btn--primary"},
    {"wait": "#tx-md-manager"},
    {"click": "#tx-wizard-form .pf-btn--primary"},
    {"wait": "#tx-derived"},
    {"wait": "#tx-wizard-context"}
  ],
  "shot": "wizard-step-3"
}
```

The two Continue clicks are scoped to `#tx-wizard-form` rather than to
`.pf-actionbar`, since "the one primary inside the wizard form" is exactly what
§6 pins. `#tx-md-manager` between them is the step-2 marker — Manager is the
one field only step 2 draws. The DSL needed no extension, so Flag 3 is not
raised.

## §6 — Tests

`grep -ln 'wizard' tests/web` finds six modules; only
`tests/web/test_transactions_wizard.py` pins wizard markup, and it held **no**
class pins on steps 3, 4 or the outcome to move — the A1c pins are all on steps
1 and 2. One helper and six tests were added:

- `_depth_between()` — how many `<div>`s are open at a marker, counted from an
  opener. `0` means sibling, `1` means inside. Structural, because "after the
  column" and "at the end of the column" read alike in a substring search.
- `test_the_rail_is_the_forms_second_child_on_the_figure_step_only` — step 3
  has `pf-rail-sum#tx-derived` at depth 0 from `.pf-form__main` and
  `#tx-wizard-context` at depth 1; steps 1, 2 and 4 have no `#tx-derived` and
  still declare the column; `cash_investment_id` and `settle_confirm` are
  inside the form.
- `test_the_recalculation_still_answers_in_three_parts` — the recalc response
  carries `#tx-derived`, `#tx-wizard-context` and `#tx-wizard-outcome`, with
  exactly two `hx-swap-oob` (the rail is the target, not an OOB).
- `test_the_last_two_steps_each_offer_exactly_one_primary` — one
  `pf-btn--primary` on step 3 (Continue) and on step 4 (Book now); both bars
  lead with Back; no `tx-btn`.
- `test_the_anlv_gate_is_a_note_carrying_its_way_back` — `pf-note--block` with
  `pf-btn--quiet pf-btn--sm` and the step-2 URL inside the note; no `tx-msg`;
  armed, the note is gone.
- `test_confirm_states_the_ticket_as_two_fact_lists_and_the_legs` — two
  `pf-block__title`s, two `<dl class="pf-context">`, three `pf-leg` rows, no
  `#tx-derived`, no `tx-leg`/`tx-sumcard`.
- `test_no_step_draws_a_body_wrapper_of_its_own` — walks all four steps and
  asserts no `tx-wizard-body`, `tx-block `, `tx-grid`, `tx-field`, `tx-context`,
  `tx-outcome` or `tx-actions` anywhere.

---

## Measurements

| | Before | After |
|---|---:|---:|
| `tx-btn` in `web/templates` | 11 | **7** |
| — `_wizard_outcome.html` | 4 | 0 |
| — `_impact_panel.html` | 3 | 3 |
| — `_cancel_panel.html` | 2 | 2 |
| — `_reverse_panel.html` | 2 | 2 |
| `components/transactions.css` lines | 1086 | **949** |
| `components/transactions.css` selectors | 143 | **108** |
| `pf-context` users (files) | 2 | **4** |
| `pf-leg` users (files) | 4 | **5** |
| `pf-actionbar` users (files) | 7 | **8** |

`pf-context` gained **two** files, not one: `_wizard_context.html` (the strip)
and `_wizard_confirm.html` (the two summary lists, §3). `pf-actionbar` was
already at 7 after A1c, not 5 — the prompt's "5 → 6" predates A1c's two step
bars.

Per file, after:

* `pf-context` — `_order_context.html`, `_secondary_sale_context.html`,
  `_wizard_context.html`, `_wizard_confirm.html`
* `pf-leg` — `_order_derived.html`, `_secondary_buy_derived.html`,
  `_secondary_sale_derived.html`, `_commitment_derived.html`,
  `_wizard_confirm.html`
* `pf-actionbar` — `_order_outcome.html`, `_order_confirmation.html`,
  `_commitment_outcome.html`, `_secondary_buy_outcome.html`,
  `_secondary_sale_outcome.html`, `_wizard_identify.html`,
  `_wizard_classify.html`, `_wizard_outcome.html`

Template line counts: `_wizard` 68 → 95, `_wizard_order` 103 → 106,
`_wizard_confirm` 107 → 133, `_wizard_outcome` 111 → 125, `_wizard_context`
41 → 47. The growth is comment, not markup: each file states why its family
changed, and §3 and §4.1 each carry a decision the record does not yet make.

Inventory, `transactions` row (elements table):

```
before | transactions | 39 | 254 | 24 | 51 | 51 | 2 | 69 | 7 | 0 | 23 | 0 | 10 |
after  | transactions | 40 | 256 | 26 | 51 | 51 | 2 | 69 | 7 | 0 | 23 | 0 | 10 |
```

Templates 39 → 40 and headings 24 → 26: `_order_derived.html` is now reached
from `_wizard.html` directly, and Confirm's two `<h4>` cards became two `<h3
class="pf-block__title">` headings the tool counts. Buttons, HTMX triggers,
links, form labels and pills are all unchanged — nothing gained or lost a
control. The flag table moved `dynamic` 67 → 66 and gained one `icon-only` and
one `no-accessible-name`, both the same row: Flag 3.

---

## Gates

| # | Gate | Result |
|---|---|---|
| 1 | targeted suites, then `tests/regression`, serially | `tests/web/test_transactions_*.py tests/web/test_pf_components.py tests/web/test_css_tokens.py tests/tools` — **496 passed** in 437s. Then `tests/regression` — **164 passed** in 135s. Both green, run serially, exit 0. The 496 includes the wizard module's **37** (31 before, 6 added by §6). |
| 2 | `ruff check` · `ruff format --check` | **clean** — "All checks passed!", "940 files already formatted" |
| 3 | `tx-btn` tree total | **7** — `_impact_panel` 3, `_cancel_panel` 2, `_reverse_panel` 2 ✔ |
| 4 | `tx-` classes in the five | **0**. Remaining `tx-` strings are ids and their references only: `_wizard` — `tx-wizard-form`, `tx-derived`, `tx-composer-host`; `_wizard_order` — `tx-trade-date`, `tx-units`, `tx-price`, `tx-fees`, `tx-taxes`, `tx-settlement-date`, `tx-case`, `tx-source`, `tx-note`; `_wizard_confirm` — **none at all**; `_wizard_outcome` — `tx-wizard-outcome`, `tx-composer-host`; `_wizard_context` — `tx-wizard-context`. ✔ |
| 5 | colour literals in `transactions.css` | none new — `tests/web/test_css_tokens.py` green; the one added rule uses `var(--ui-semantic-warn)` ✔ |
| 6 | `python tools/ux_inventory.py` | re-run and committed; row above ✔ |
| 7 | `git diff --name-only` | exactly ten tracked paths — the five templates, `components/transactions.css`, `docs/ux/atlas-scenes.json`, `tests/web/test_transactions_wizard.py`, `docs/ux/inventory/elements.csv`, `docs/ux/inventory/summary.md`. `git status --porcelain` adds one untracked path, this report. Nothing else. ✔ |

---

## Deliberate deviations

**1. The recalculation trigger moved to the `<form>`, not onto a bare step-3
`<div>`.** §2 said the wrapper "keeps its `hx-post` / `hx-trigger` /
`hx-include` / `hx-vals` / `hx-target` / `hx-swap` verbatim … a bare `<div>`
with the attributes". The attributes are kept verbatim; the element they sit on
is not the step body but `#tx-wizard-form`, guarded `{% if step == 3 %}`.

§1 forces it. `_order_derived.html` includes `_settlement.html`, whose
settlement radios (`cash_investment_id`) and confirm checkbox
(`settle_confirm`) carry **no `hx-*` of their own** — they rely on an ancestor
trigger, and today that ancestor is `tx-wizard-body`, which contains the rail.
Move the rail to the form's second child and those controls are no longer
inside the step body: their `change` events stop reaching the trigger and
picking a settlement position would silently stop recalculating. Inclusion is
not the issue (htmx includes the closest form for any non-GET regardless);
*triggering* is.

The form is the element that contains both columns, and it is M-1's own
arrangement — `_order_composer.html:45-49` puts the same attributes on
`<form id="tx-order-form">`. So the two surfaces now recalculate through one
shape instead of two, which is what this strand is for.

`hx-include="closest form"` was kept verbatim even though it is a no-op on a
form element: htmx 1.9.12's `getInputValues` processes the closest form first
for a non-GET (`htmx.js:2603`) and `processInputValue` skips anything already
in `processed`, so nothing is posted twice. Verified by reading the bundled
source, not assumed.

**2. Steps 3 and 4 have no wrapper element at all.** A consequence of 1: with
the trigger gone, the wrapper had no job, and a *classless* `<div>` would have
had a cost — `pf-form__main` is `display: flex; flex-direction: column; gap:
var(--ui-space-6)`, so a plain block wrapper makes its children one flex item
and the Execution grid would butt directly against the Provenance title. Both
step bodies now put their blocks straight into the column, exactly as steps 1
and 2 have since A1c, and take that column's own gap.

**3. Confirm's summary is in the main column, not a `pf-rail-sum`.** §3's own
instruction, executed; registered here because it is a record question, not a
local one. Flag 1.

**4. `pf-leg__type` carries no `--{{ txn_type }}` modifier, and there is no
`pf-leg__what`.** §3 made both conditional on the family having them. It does
not: `pf_components.css:240-244` declares `.pf-leg`, `.pf-leg__type`,
`.pf-leg em` and `.pf-leg__amount` and nothing else, and `_order_derived.html`
draws the middle cell as a bare `<span>`. The old `tx-leg__type--buy` /
`--sell` colouring is therefore *lost* on this one surface — the Confirm step's
two legs now read in the base grey. `_effect_leg.html` keeps it, so the History
detail is unaffected. Worth a look in the after-image; if the colour is wanted,
it is a record question about `pf-leg`, not an Area rule to re-add.

**5. `pf-context` pairs are wrapped in `<div>`s.** §3 said the `<dl>` keeps its
`dt`/`dd` pairs and copy verbatim, and it does — the wrapper is the family's
own markup (`_order_context.html`), and without it the flex layout has no items
to wrap.

**6. The `tx-messages` wrapper was dropped from the outcome**, though §4.4 did
not name it. The `.tx-messages` *rule* stays (the reversal report uses it); only
the wizard's use of it went, because `.pf-note + .pf-note { margin-top:
var(--ui-space-2) }` already spaces the notes.

**7. `.tx-btn--primary` was retired though §4.4 did not list it**, and
`.tx-block__title`, `.tx-legs`, `.tx-legs__pending` and `.tx-legs__group` were
**kept** though §4.4 listed `tx-block*` and `tx-legs*` for retirement.
`_history_detail.html` still draws all four; §4.4's own "only if the panels no
longer use it — check" rule applied, and the check said keep.

**8. `pf-input--num` does not set the mono stack** where `.tx-field--numeric
input` did. The record's rule is `text-align: right` alone. The four numeric
inputs on the Order step therefore render in the body face. Not corrected here:
the record names no `pf-mono` family, which is the same open question the A1c
note about `#tx-id-value` / `#tx-id-figi` / `#tx-currency` records. Flag 4.

---

## Flags (carry)

1. **Is a review step's summary a rail or content?** Confirm's two fact lists
   are in the main column and the 360px column stands empty on step 4. The
   argument is in `_wizard_confirm.html`'s header comment: `pf-rail-sum` is the
   companion to *inputs*, and a review step has none. The record should say
   which, because the next stepped surface will ask again.
2. **An action inside a `pf-note`.** The record *does* have a slot — the third
   `auto` grid column, which `_messages.html` fills with an empty `<span>` and
   names in prose. What it does not have is a name for it. This prompt put a
   `pf-btn--quiet pf-btn--sm` there; the record should either bless that pair
   or grow a `pf-note__action`.
3. **The inventory flags `_wizard.html`'s `<form>` `icon-only` and
   `no-accessible-name`.** An artefact of the guarded `hx-post`: the tool sees
   an HTMX trigger whose own template carries no static text, because all four
   step bodies are includes. `_order_composer.html`'s form escapes it only by
   holding its fields inline. A `<form>` is not a control and needs no
   accessible name — A1s should suppress or explain the row rather than chase
   it.
4. **`pf-input--num` is alignment only, not mono.** Joins the three A1c mono
   holdouts (`#tx-id-value`, `#tx-id-figi`, `#tx-currency`) and
   `#tx-md-currency`: five ids waiting for the record to name a mono family.
5. **`.tx-btn` and its three modifiers are the last bespoke button family in
   this Area**, in the four S5 panels only. A1e retires them and `tx-btn`
   reaches 0.

---

## For A1d

The 18 `type="number"` inputs in the Transactions Area, by file:

| File | Count | Fields |
|---|---:|---|
| `_order_composer.html` | 4 | `units`, `price_per_unit`, `fees`, `taxes` |
| `_wizard_order.html` | 4 | `units`, `price_per_unit`, `fees`, `taxes` |
| `_secondary_buy_composer.html` | 4 | `md_vintage_year`, `gross_amount`, `md_acquired_nav`, `md_assumed_unfunded` |
| `_secondary_sale_composer.html` | 3 | `gross_amount`, `fees`, `taxes` |
| `_commitment_composer.html` | 2 | `commitment_amount`, `md_vintage_year` |
| `_settlement.html` | **1** | `cash_opening_balance` — **the 18th**, in the inline cash-position mini-form, shared by every composer *and*, since this prompt, by the wizard's rail |

Worth noting before A1d writes a parser: **two of the 18 are not amounts.**
`md_vintage_year` appears twice (secondary buy, commitment) and is a year, so a
"Read as 1 234 567,89" echo would be wrong for it. Sixteen are money or units.

The four recalc partials that will carry "Read as …": `_order_recalc.html`,
`_commitment_recalc.html`, `_secondary_buy_recalc.html`,
`_secondary_sale_recalc.html` — plus `_wizard_recalc.html`, which is a fifth
and takes the same treatment, since after this prompt its three includes are
the same three regions the composer's recalc refreshes.

---

## The commit

```bash
git add web/templates/_partials/transactions/_wizard.html \
        web/templates/_partials/transactions/_wizard_order.html \
        web/templates/_partials/transactions/_wizard_confirm.html \
        web/templates/_partials/transactions/_wizard_outcome.html \
        web/templates/_partials/transactions/_wizard_context.html \
        web/static/css/components/transactions.css \
        docs/ux/atlas-scenes.json \
        tests/web/test_transactions_wizard.py \
        docs/ux/inventory/elements.csv \
        docs/ux/inventory/summary.md \
        docs/reports/P-UX-A1c2-report.md

git commit -m "feat(transactions): wizard steps Order and Confirm on the form vocabulary — rail as the form's second child on the figure step only, Confirm's summary as fact lists and pf-leg rows, outcome on pf-note and pf-actionbar with one primary per step; tx-wizard-body retired (UX A-1, P-UX-A1c2)"
```
