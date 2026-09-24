# P-UX-A1b — Chooser and the order composer onto `pf-form` · the sub-surface pattern

**Strand:** UX A-1 (Transactions) · **Runs after:** P-UX-A1a
**Tip at session start:** `cae50f1` — *"feat(transactions): blotter and history
onto the pf-table vocabulary … (UX A-1, P-UX-A1a)"*, tree clean.
**A0g and A1a landed as one commit** — `cae50f1` carries both
`docs/reports/P-UX-A0g-report.md` and `docs/reports/P-UX-A1a-report.md`. There
is no commit whose subject ends `(UX A-0, P-UX-A0g)`.
**Before image:** `docs/ux/atlas/2026-09-23-5/` (commit `b1eced6`, the A-0 end
run) — scenes `transactions/scenes/composer-order.png`,
`composer-commitment.png`, `composer-secondary.png`. No post-A1a run exists:
A1a's own report also left the after-image run to the operator.

**Scope as executed:** the eleven scope templates, five transcriptions in
`components/pf_components.css`, the retirement in
`components/transactions.css`, one context dict in
`web/routes/transactions.py`, the `chooser_markup` selector in
`tests/web/conftest.py`, seven test modules, four selectors in
`docs/ux/atlas-scenes.json`, the regenerated inventory, this report.
**Not touched:** `layout.css` (see §4 — the container query is *not* declared),
`shell.js`, `theme.css`, the three reported-flow composers, the wizard,
`_effect_leg.html`, any `type="number"`, the panels.

---

## OPERATOR ACTION REQUIRED

**1. The commit is yours.** Nothing is staged. The command is at the end.

**2. `portfoliflow bootstrap` before anything visual.** The gates here
truncated the dev DB.

**3. The after-image atlas run is due.** The scene selectors were moved from
`.tx-flow` to `.pf-flow` in `docs/ux/atlas-scenes.json` — four scenes, the
three composers and wizard step 1 — so the run will click the right tiles;
without that edit it would have hung on the first step. What to expect in each:

- `composer-order` — rebuilt wholesale: crumb, head stamp, `pf-seg`, the rail,
  the action bar.
- `composer-commitment` and `composer-secondary` — changed only where they
  render this commit's shared partials (`_order_title.html`, `_messages.html`,
  and for the secondary buy also `_settlement.html`). Their frames are still
  `tx-ticket` and are A1b2's; the new settlement block will look unframed
  inside them. That is flag 5, not a regression.
- `wizard-step-1` — the stepper is untouched, but its Order step now draws the
  rail as a full-width panel. That is flag 4, and A1c's.

To see the rail carry numbers rather than its placeholder, seed a unitised
holding and one cash position before the run.

**4. The browser round.** Open a ticket from the Blotter — it must land on
`#new` with the crumb reading the ticket; `‹` must return the chooser and the
plain "New transaction" heading; one recalculation must move the rail and
nothing else; and A1a's flag 6 (the `pf-menu` self-close) closes here or not
at all.

---

## Verify-first

| # | Check | Result |
|---|---|---|
| 1 | `git log --oneline -3` · `git status --porcelain` | A1a at the tip (`cae50f1`), tree clean. **A0g is not its own commit** — it is inside `cae50f1` with A1a. |
| 2 | `wc -l` on the eleven | 63 / 11 / 167 / 12 / 39 / 91 / 98 / 19 / 79 / 150 / 65 — **exact, all eleven** |
| 3 | `tx-btn` in the eleven · tree total | **7 lines** (11 raw class tokens: 7 base + 4 modifiers), not the 8 the prompt predicted — there is no eighth line; the arithmetic `4 outcome + 2 confirmation + 1 settlement` comes to 7. Template-tree total **35 lines**, exactly as predicted (plus 12 lines in `transactions.css`). |
| 4 | the six-column field grid | **Confirmed transcription gap from A0d.** `pf_components.css:160–163` declares `.pf-field` span 2 / `--half` 3 / `--wide` 4 / `--full` 1/-1 with **no container**; `grep 'repeat(6'` returned nothing. The record has it at `shared.css:328` as **`.pf-grid`**. Transcribed under that name. |
| 5 | `pf-seg` / `pf-choice` | 0 here; `shared.css:342–346` and `374–378` there. Both on the standards' "still to build" list (§4). |
| 6 | OOB ids and swap targets | `#tx-derived` (main target), `#tx-ticket-title`, `#tx-context`, `#tx-outcome` — all four kept. `#tx-composer-host` is the composer-level target. |
| 7 | who else includes the shared partials | `_order_derived.html` → the **wizard** (`_wizard_order.html:63`, `_wizard_recalc.html:15`). `_settlement.html` → `_secondary_buy_derived.html:77`, `_secondary_sale_derived.html:83`. `_messages.html` → **six** other outcome/step partials (`_wizard_outcome`, `_wizard_classify`, `_wizard_identify`, `_commitment_outcome`, `_secondary_buy_outcome`, `_secondary_sale_outcome`). `_order_title.html` → the **three** M-3 composers and their three recalc partials. All inherit this commit's markup; their tests are in gate 1 and moved here where they pinned a class. |
| 8 | `chooser_markup` | `conftest.py:244` sliced on `<div class="tx-flows">`; moved to `<ul class="pf-flows">` in that one place. |
| 9 | `shell.js` after-swap | reacts to `#shell-main` only; nothing switched sections on a composer swap. |
| 10 | `container` in `layout.css` | `.pf-main` declares none (accepted deviation 4). **Still none after this commit** — see §4. |

---

## §1 — The switch (Q-UX-A-5) — done

`_new_section.html` host carries

```html
hx-on::after-swap="if (event.detail.target === this && location.hash !== '#new') location.hash = 'new'"
```

Verified against the vendored htmx **1.9.12**, not assumed: the attribute
walker (`jt`) maps `hx-on::after-swap` → `htmx:after-swap`, and the dispatcher
(`ce`) fires every event twice, camelCase then the kebab alias produced by
`$t()`. Both halves of the shorthand are live in this pin.

Both guards are load-bearing and both are pinned by a test:

- `event.detail.target === this` — `htmx:afterSwap` bubbles, and every
  recalculation swaps `#tx-derived` *inside* the host. Without it a keystroke
  would move the fragment.
- `location.hash !== '#new'` — assigning the hash it already holds fires no
  `hashchange`, so without it the *second* ticket opened from the Blotter
  would never switch.

`hashchange` → `shell.js` `show(resolve())`. The shell remains the only thing
that touches `hidden`. **`js calls` in the inventory stays 0.**

## §2 — Chooser — done

`<ul class="pf-flows">`, five `<li>`, each a `<button class="pf-flow">` with
`pf-flow__name`, `pf-flow__hint` and a trailing `pf_icon("forward")` (the
family's own `.pf-flow .pf-icon` rule places and inks it — no new CSS). M-1's
copy verbatim.

The five flow codes left visible copy into `data-flow` (§2.6.4). The test now
pins both halves: `data-flow="U-BUY / U-SELL"` present **and**
`>U-BUY / U-SELL<` absent. The inventory measures the effect directly — the
`transactions` **abbrev** column falls **13 → 3**.

The chooser endpoint additionally returns the plain heading out of band:

```html
<h2 class="pf-view__title" id="new-title" hx-swap-oob="outerHTML">New transaction</h2>
```

and, beside it, an empty `<span class="pf-view__asof" id="new-asof" hx-swap-oob="outerHTML"></span>`
— both behind `{% if oob %}`, because the same partial is the area page's
static markup and a second `id="new-title"` in one document is a collision.
The heading string comes from `pf_section_title(active_area, "new")`; the route
supplies `oob=True` and `active_area="transactions"` and nothing else.

The empty stamp is not decoration. A composer fills that slot with the
ticket's ident and state (§3), so without it a discarded ticket would leave
"New ticket · Unsaved" standing in the head over the chooser. `areas/_section.html`
renders the span empty for a section with no `section_as_of`, so restoring it
empty restores exactly what was there. A test pins it.

## §3 — The order composer — done

**Frame.** `<form class="pf-form" id="tx-order-form">` — trigger, target and
swap unchanged — holding `<div class="pf-form__main">` with four `<section>`
groups (What happened · Execution · Provenance · outcome) and then the rail.
Group headings are `<h3 class="pf-block__title">`, transcribed from
`shared.css:327`; `h3` and not the record's `h2` because the section head's own
`<h2>` is the crumb, and two `h2`s would flatten the outline.

**Fields.** `pf-field` with `--wide` / `--full`, `pf-label` above, `pf-hint`
below, `(optional)` in `pf-optional` verbatim, `pf-input` / `pf-select`,
`pf-input--num` on Units, Price, Fees, Taxes. `type="number"` stays — A1d's.
Dates native (§2.5.6).

**Direction** is `pf-seg` with the two radios kept as radios. Checked state
comes from the record's own rule — `background: var(--ui-border-default)`
**and** `font-weight: medium` — so §2.11.1 is met without a rule of this
Area's own, and nothing was added for it.

**Crumb (§2.1.6).** Rendered:

```html
<h2 class="pf-view__title pf-crumb" id="new-title" hx-swap-oob="outerHTML">
    <button type="button" class="pf-crumb__back"
            hx-get="/api/transactions/chooser"
            hx-target="#tx-composer-host"
            hx-swap="innerHTML"><svg class="pf-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="m15 18-6-6 6-6" /></svg>New transaction</button>
    <span class="pf-crumb__sep" aria-hidden="true">›</span>
    <span class="pf-crumb__here" id="tx-ticket-title">Sell units</span>
</h2>
```

`_order_title.html` keeps its id and its own `oob` swap and is *included* into
the crumb — it is **not** itself the crumb. That is what keeps the three M-3
composers (A1b2's) working unchanged: they go on including the same span into
their own in-composer heads, and their recalc partials go on addressing
`#tx-ticket-title` wherever it currently lives. `.pf-view__title-row` is
`display: flex; gap: 4px 12px`, so the crumb and the stamp are siblings with a
gap. `aria-labelledby="new-title"` keeps resolving because the element that
carries the id is the one that renames.

**Ident and state** go to the head's as-of slot, out of band:

```html
<span class="pf-view__asof" id="new-asof" hx-swap-oob="outerHTML">New ticket <span class="pf-state pf-state--unsaved">Unsaved</span></span>
```

This is a **third** placement, beside the two the prompt offered — see
Deliberate deviations 1.

**Rail (§2.5.3).** `#tx-derived` *is* the `pf-rail-sum` aside, the form's
second child, so `.pf-form`'s own `minmax(0,1fr) 360px` places it with no rule
from this Area — no host wrapper and no `display: contents`. Rendered, after
one recalculation:

```html
<aside class="pf-rail-sum" id="tx-derived" aria-label="Amounts and settlement">
    <div>
        <p class="pf-rail-sum__title">Amounts</p>
        <dl class="pf-sum">
            <span class="pf-sum__formula">400.0000 units × 104.1000</span>
            <dt>Gross</dt>
            <dd>41,640.00 EUR</dd>
            <dt>Fees</dt>
            <dd>−180.00 EUR</dd>
            <dt>Taxes</dt>
            <dd>−45.00 EUR</dd>
            <dt class="pf-sum__total">Net proceeds</dt>
            <dd class="pf-sum__total">+41,415.00 EUR</dd>
        </dl>
    </div>
    <div>
        <p class="pf-rail-sum__title">Ledger effect</p>
        <div class="pf-leg">
            <span class="pf-leg__type">sell</span>
            <span>Alpha Global Equity Fund <em>@ 104.1000</em></span>
            <span class="pf-leg__amount">−400.0000 units</span>
        </div>
        <div class="pf-leg">
            <span class="pf-leg__type">buy</span>
            <span>EUR Cash — Commerzbank <em>@ 1.0000</em></span>
            <span class="pf-leg__amount">+41,415.0000 units</span>
        </div>
        <details class="pf-more">
            <summary>Two legs</summary>
            <p>Ledger effect — two legs, booked together or not at all.</p>
        </details>
    </div>
    <div>
        <p class="pf-rail-sum__title">Settlement position</p>
        <p class="pf-hint">One EUR cash position on the book. Confirm it or pick another.</p>
        <label class="pf-choice is-selected">
            <input type="radio" name="cash_investment_id" value="b0962b9e-…" checked>
            <span>EUR Cash — Commerzbank</span>
            <span class="pf-choice__figure"><span>412,500.00</span> EUR → <span>453,915.00</span></span>
        </label>
        <label class="pf-check">
            <input type="checkbox" name="settle_confirm" value="1" checked>
            <span>Settle against this position.</span>
        </label>
    </div>
</aside>
```

The settlement panel is a **rail block**, not a main-column group — see
Deliberate deviations 2. The four context facts stay in the main column — see
Deliberate deviations 3. `hx-include="closest form"` is intact: every control
in the rail is inside `#tx-order-form`, and a test asserts both that and that
`#tx-context` sits inside the form ahead of the rail.

**Settlement (`_settlement.html`).** Four states, copy unchanged. Controls onto
`pf-input` / `pf-select` / `pf-choice` / `pf-check`; the candidate rows take
the record's `pf-choice` shape with the projection in `pf-choice__figure`. The
one `tx-btn` became a **default** `pf-btn`, not a primary — the view's one
primary is Book now. The two "nothing to settle against" states became
`pf-note--block`, which is what they are: they stop the ticket.

**Messages (`_messages.html`).** `pf-note` with `--block` / `--warn`,
`pf-note__lead`, `pf-note__sub`, and the registry's own icon names —
`pf_icon("block")` (`octagon-alert`) and `pf_icon("warn")`
(`triangle-alert`). **No icon was added**: the prompt's `"blocked"` /
`"warning"` are already in `web/icons.py` under those two keys. The empty
`<span></span>` between lead and sub fills the family's third grid column, the
slot an inline action would take — the mock's own shape.

The full-disposal consequence is `pf-note--info` with its checkbox as
`pf-choice`, placed in a second `pf-note__sub` rather than inside the
sentence: the family already puts that slot at `grid-column: 2 / -1`, so the
offer lines up under the prose with no rule added.

**Action bar (§2.3.4, R1, R3).** Rendered:

```html
<div class="pf-actionbar">
    <button class="pf-btn pf-btn--quiet" type="button"
            hx-get="/api/transactions/chooser"
            hx-target="#tx-composer-host"
            hx-swap="innerHTML">Discard</button>
    <p class="pf-actionbar__hint">Booking records the decision and both ledger legs in one step.</p>
    <button class="pf-btn" type="button"
            hx-post="/api/transactions/draft"
            hx-include="closest form"
            hx-target="#tx-composer-host"
            hx-swap="innerHTML">Save as draft</button>
    <button class="pf-btn" type="button"
            hx-post="/api/transactions/propose"
            hx-include="closest form"
            hx-target="#tx-composer-host"
            hx-swap="innerHTML">Propose</button>
    <button class="pf-btn pf-btn--primary" type="button"
            hx-post="/api/transactions/book"
            hx-include="closest form"
            hx-target="#tx-composer-host"
            hx-swap="innerHTML">Book now</button>
</div>
```

R3 order (the way back leads), the five hint sentences unchanged, `disabled`
states and endpoints unchanged, `tx-actions__spacer` retired — the hint's
`flex: 1` does its work. The `new` section's head primary slot stays empty.
`#tx-outcome` keeps its id and its OOB and now wraps the strip and the bar as
one region with no class of its own.

**Confirmation (`_order_confirmation.html`, MD-16).** Content unchanged
(§2.5.7). It OOBs its own crumb (`‹ New transaction › Booked on <date>`) and
its own head stamp, so the head does not keep saying "Sell units" over a
booked ticket. Facts strip: none needed — the panel states its provenance in a
sentence, so no `pf-facts` and **no `pf-kv`** was taken. Notice is
`pf-note--warn`. **Primary: "New transaction."** A fact has no way back, so
R3's leading slot is empty and the hint leads; of the two ways onward, the
next ticket is the one that stays on this surface, where "Open …" leaves the
Area. Legs stay `tx-leg` — Deliberate deviations 4.

**Recalc (`_order_recalc.html`).** Shape unchanged. All four OOB regions
resolve: `#tx-ticket-title` (now in the head's crumb), `#tx-context` (main
column), `#tx-derived` (the rail, the contiguous target), `#tx-outcome`.
Verified in the rendered recalc response, pasted above in two halves.

## §4 — Container query: **not declared**

Measured, not estimated. `.pf-main` padding-inline `var(--ui-space-6)` = 32 px
a side; `.pf-form` gap `var(--ui-space-6)` = 32 px; rail 360 px; `.pf-grid`
gap `var(--ui-space-4)` = 16 px over six tracks. Shell columns from
`theme.css`: nav 252 / 56, rail 44, dock 380.

| Content column | `.pf-main` inner | form main column | one of six tracks | `pf-field` (span 2) | `--wide` (span 4) |
|---|---:|---:|---:|---:|---:|
| **1280 px** (the §2.2.4 minimum) | 1216 | **824** | 124 | **264** | 544 |
| **968 px** — 1600 − 252 − 380, Shirley docked, nav open | 904 | **512** | 72 | **160** | 336 |
| 1164 px — the same with the nav collapsed to 56 | 1100 | **708** | 105 | **225** | 470 |

**Decision: do not declare `container: view / inline-size` on `.pf-main`.**
Three reasons, in order of weight.

1. At the 1280 px content column the two-column form is comfortable — a
   824 px main column, 264 px fields. No collapse is needed there, and 1280 px
   is the width §2.2.4 names as the minimum the layout answers for.
2. The 968 px case is **already below that minimum**, and the remedy the
   standards name for it is not a form collapse but the shell's own
   nav auto-collapse (§2.2.1, "**not yet** — strand A-5"). With the nav
   collapsed the main column returns to 708 px and the two-column form holds
   at 225 px fields. Collapsing `.pf-form` here would paper over a shell gap
   inside a component.
3. The record's `@container` rules **are not transcribed** — accepted
   deviation 4 says so explicitly, and `grep -c '@container'` on
   `pf_components.css` returns 0 both before and after this commit. Declaring
   the container would therefore fire nothing and would carry only the risk
   deviation 4 warns about (`container-type: inline-size` changing how
   `position: sticky` resolves on the view head). Reversing a decided
   deviation to activate nothing is not a trade worth making in a component
   strand.

`layout.css` is untouched. When A-5 lands the nav collapse, the container
declaration and the twelve `@container` rules become one coherent piece of
work rather than a lone declaration — that is the note to carry forward.

## §5 — Tests

Moved: the `chooser_markup` selector (one place), `_actions()`'s action-bar
selector, the five flow-code pins (now on `data-flow`, with the visible-copy
negative beside them), the settlement projection pin (from an arrow's class to
the projected figure and the one `is-selected` row), two `pf-leg__type`
negatives, `tx-msg--consequence` → `pf-note--info`, and **thirteen**
`tx-msg--block` → `pf-note--block` across seven modules — every one of them
traced to `_messages.html` before it was moved.

Two pins were deliberately **not** moved, because their markup is not this
prompt's: `test_transactions_blotter.py`'s cancel refusal (written by
`_cancel_panel.html`, A-7's) and `test_transactions_secondary_sale.py`'s own
consequence and leg pins (A1b2's).

Added, seven tests, each naming its rule in the docstring:

| Test | Rule |
|---|---|
| `test_the_composer_renames_the_section_head_into_a_crumb` | §2.1.6 |
| `test_leaving_the_composer_puts_the_plain_section_title_back` | §2.1.6, the stamp reset, and the id-collision guard |
| `test_the_composer_carries_exactly_one_primary` | R1 (§2.3.2) |
| `test_the_composer_host_switches_the_shell_to_the_new_section` | Q-UX-A-5, both guards |
| `test_the_direction_is_a_segmented_control_of_two_radios` | §2.5.2, §2.11.1 |
| `test_the_message_strip_is_one_note_pattern_in_two_tones` | §2.6.2 |
| `test_the_derived_region_is_the_summary_rail_inside_the_form` | §2.5.3 |

**One stale pin from A1a was corrected here**, not in a commit of its own:
`test_transactions_negative_cash.py` still asserted
`class="tx-blotter__hint"`, a class A1a retired when it put the A-18 sentence
behind `pf-more`. Proof it pre-dates this prompt:
`git grep tx-blotter__hint HEAD -- web/` is empty, and this commit touches
neither `_blotter.html` nor `_negative_cash.html`. The pin now reads
`class="pf-more"`.

---

## Component-layer additions

Five, all transcribed from `~/DC-UX-D/shared.css` onto `--ui-*`, none invented.
The prompt anticipated three; the fourth and fifth are named below with why.

| Family | Record source | Why |
|---|---|---|
| `.pf-grid` | `shared.css:328` | **The A0d transcription gap check 4 predicted.** A0d took `.pf-field`'s `span 2 / 3 / 4` without the `repeat(6, …)` container they are counted against, so the spans meant nothing until now. Named as the record names it. |
| `.pf-seg` (+ `input`, `label`, `:checked + label`, `:focus-visible + label`) | `shared.css:342–346` | §2.5.2, "still to build". |
| `.pf-choice` (+ `+`, `.is-selected`, `input`, `__figure`) | `shared.css:374–378` | §2.5.1/§2.5.3, "still to build". The combined `.pf-choice input, .pf-check input` rule was split — `.pf-check input` already exists. |
| `.pf-block__title` | `shared.css:327` | §3.1 asked for the record's group heading by name; this is it. Not in the A0d catalogue and not on the "still to build" list — a second, smaller gap of the same kind as `.pf-grid`. |
| `.pf-context` (+ `dt`, `dd`) | `shared.css:347–349` | "still to build". It differs from `.pf-facts` only in which way the margin runs (`s4 0 0` against `0 0 s5`), which is exactly what the facts strip needs sitting *under* a field grid. A scoped override in `transactions.css` would have been the alternative; the record already names the family, so the family was taken. |

**`pf-kv` was not taken** — the confirmation panel needed no key/value strip.

The §4 **Users** column in `ui-standards.md` is not edited here; per the strand
plan that is the A-1 end's single pass.

### Retired from `components/transactions.css`

**35 rules**, every one verified to have no remaining user in `web/` or
`tests/` — including the interpolated ones, which were checked by pattern and
kept: `tx-state--*` (five templates build it), `tx-leg__type--*` (five), and
`tx-delta__*` (`_impact_panel.html`).

`tx-flows` · `tx-flow` · `button.tx-flow:hover` · `tx-flow--pending` ·
`tx-flow__name` · `tx-flow__hint` · `tx-flow__code` · `tx-flow__pill` ·
`tx-direction` (+ 5 combinators) · `tx-derived__row--formula span:first-child`
· `tx-settle` (+ 13) · `tx-msg__choice` (+ input) · `tx-inline-create` (+
`input[readonly]`) · `tx-confirm__lead` (+ em). Two section banners left empty
by the sweep went with them, and the sheet's head comment was rewritten to say
what is left and who still renders it.

`-259 / +12` lines, 271 changed in all.

---

## Measurements

| | Before | After |
|---|---:|---:|
| the eleven templates, lines | 794 | 888 |
| `tx-btn` lines in the eleven | 7 | **0** |
| `tx-btn` lines, template tree | 35 | **28** |
| `tx-*` **classes** in the eleven | many | **0** (12 remaining hits are `tx-` **ids** and comment prose) |
| `components/transactions.css`, lines | 1565 | **1318** |
| `pf_components.css`, lines | 384 | **412** |
| `@container` rules in `pf_components.css` | 0 | **0** |

Inventory, `transactions` row:

| | templates | element rows | headings | buttons | HTMX triggers | links | form labels | pills | tiles | routes | **js calls** | distinct nouns |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| before | 32 | 219 | 4 | 49 | 49 | 2 | 69 | 7 | 0 | 23 | **0** | 15 |
| after | 34 | 237 | **11** | 51 | 51 | 2 | 69 | 7 | 0 | 23 | **0** | **10** |

Second table (findings), `transactions`: **abbrev 13 → 3** — the flow codes
leaving visible copy, measured. `dynamic` 46 → 67 (the `pf-note` tone and
`pf-state` modifier interpolations), `from:aria-label` 2 → 3 (the rail's
`aria-label`), `duplicate-label` unchanged at 43.

Headings 4 → 11 is the group titles becoming real headings (`<p>` → `<h3>`);
distinct nouns 15 → 10 is the bespoke families going.

---

## Gates

| # | Gate | Result |
|---|---|---|
| 1 | `pytest tests/web/test_transactions_*.py tests/web/test_pf_components.py tests/web/test_css_tokens.py tests/web/test_icons.py tests/regression -q` | **green — 582 passed, 0 failed, in 9:13.** Collection reconciles exactly: 418 in the web selection (411 before, plus the 7 added here) and 164 under `tests/regression`. `tests/tools/test_ux_atlas_reveal.py` was run separately over the edited scenes file — **63 passed**, so 645 in all. Two failures were met and resolved on the way: the A1a stale pin (§5) and one of my own renames, reverted because `_cancel_panel.html` writes its own strip. |
| 2 | `ruff check` · `ruff format --check` | **All checks passed** · **940 files already formatted** |
| 3 | `tx-btn` tree total | check 3 gave **7** in the eleven; tree total **35 → 28** |
| 4 | `tx-` classes in the eleven | **0.** The 12 remaining hits are ids (`#tx-derived`, `id="tx-context"`, `id="tx-derived"`, `id="tx-ticket-title"`, `id="tx-direction-buy"` / `-sell` and their two `for=`) and four mentions in comment prose. |
| 5 | colour literals in the two sheets | **none new** — the two `grep` hits are the string `#061` in a comment and the word `rgba()` in another |
| 6 | `python tools/ux_inventory.py` | re-run and committed; the row is above |
| 7 | `git diff --name-only` | the eleven · the two sheets · `web/routes/transactions.py` · `tests/web/conftest.py` · seven test modules · `docs/ux/atlas-scenes.json` · the three inventory files · this report. **`layout.css` absent, by §4's decision.** |

Render check: done through the test client with an investment picked and
units/price entered — the crumb, the rail and the action bar are pasted in §3
from that render, not hand-written.

---

## Deliberate deviations

1. **The ident and state pill go to the head's as-of slot, not to the rail
   head or the first line of the main column.** §3.4 offered those two and
   asked for the mock's placement; the mock's placement is in fact a third
   thing — `pf-view__stamp` in the view head's right slot (`mock01.src.html:278`).
   The as-of slot is the product's equivalent of that slot, R9 (§2.6.6) already
   sends a stamp there, and P-UX-A1a already built the out-of-band seam for it
   (`#{slug}-asof`). The rail was the wrong home for a second reason: the rail
   is the recalculation's swap target, and the ident is not derived — it would
   have had to be threaded into `_derived_context` to survive a keystroke.
   The cost of the deviation is one extra out-of-band element on the way back:
   the chooser and the confirmation panel each restate the slot, so a discarded
   ticket leaves no state pill behind. Both are pinned.
2. **The settlement panel is a rail block, not a main-column group in its DOM
   spot.** §3.5/§3.6 asked for `display: contents` on the host so its two
   children become grid items of `.pf-form`, with settlement staying in the
   main column. That is not constructible: for `grid-column: 2` to reach the
   rail, `#tx-derived` must be a direct child of `.pf-form`, which auto-places
   the settlement sibling at column 1 **row 2** — below `.pf-form__main`, and
   therefore below the sticky action bar, which is the last thing in that
   column. Moving the bar outside the form, as the mock does
   (`mock01.src.html:307`), would break `hx-include="closest form"` on all four
   gestures; the mock has no server. The record's own composer resolves it the
   other way — `<aside class="pf-rail-sum" id="tx-derived">` containing
   Amounts, Ledger effect **and** Settlement position — which satisfies
   §2.5.3, keeps one swap over all three (§3.5's actual requirement), needs no
   scoped rule at all, and leaves every control inside the form.
3. **The four context facts stay in the main column**, as `<dl class="pf-context"
   id="tx-context">` under the "What happened" grid — the mock's placement
   (`mock01.src.html:283`) — rather than joining the rail as `pf-facts`. The
   decisive reason is mechanical: on a recalculation `oob` is true throughout,
   so a copy of `_order_context.html` inside `#tx-derived` would render
   carrying `hx-swap-oob`, and htmx lifts every such element **out of the
   fragment** before the fragment lands — the rail would arrive without its
   facts. Keeping the strip where it is preserves §3.10's four-region contract
   exactly and costs one redundant swap less, not more.
4. **The confirmation panel's legs stay `tx-leg`.** They are rendered by
   `_effect_leg.html`, which is not in scope and is shared with the History
   detail and the two M-3 derived panels. Moving it would move three surfaces
   this prompt explicitly excludes; it goes with them in A1b2/A1e.
5. **Provenance is a plain `<section>`, not the mock's `<details class="pf-group">`.**
   Collapsing an optional group is a behaviour change §3.1 did not ask for, and
   §2.5.2's "optional groups collapse" is a rule the A-1 end can apply across
   the Area at once rather than on one group here.
6. **`_order_title.html` is included into the crumb rather than becoming it.**
   §3.4 reads as if the partial itself moves to the head; making it the `<h2>`
   would have stripped the in-composer head off the three M-3 composers, which
   are A1b2's. Including it into the crumb's `__here` slot keeps its id and its
   OOB contract identical for all four callers.
7. **The legs sentence sits behind `pf-more`**, per the mock's rail and §2.6.4,
   rather than staying the visible `tx-legs__lead` line. Copy verbatim.
8. **The action bar's order follows the prompt, not the mock.** The mock puts
   the hint first and makes Save as draft quiet; §2.3.4's R3 gives the leading
   slot to the way back, and §2.3.3 makes peer answers default buttons. The
   standards document is the rule where they differ.

---

## Flags

1. **`‹` returns to the chooser, not to the Blotter the ticket was opened
   from.** Carried from the prompt. The crumb names the parent *surface*, which
   is New transaction; a return-to-origin would need the host to remember a
   fragment. **Record question, unchanged.**
2. **The rail's placement needed no rule of this Area's own** — flag 2 of the
   prompt is discharged rather than carried: because `#tx-derived` *is* the
   `pf-rail-sum` and the form's second child, `.pf-form`'s own
   `minmax(0,1fr) 360px` places it. `transactions.css` gained **no** scoped
   rule in this commit. The record places it the same way
   (`shared.css:324`, `mock01.src.html:298`).
3. **A1a's flag 6 (the `pf-menu` self-close) closes on the browser round**, not
   here.
4. **The wizard's Order step now renders the rail as a full-width panel.**
   `_wizard_order.html:63` and `_wizard_recalc.html:15` include
   `_order_derived.html`, and `_wizard.html` has no `.pf-form` around it, so
   `.pf-rail-sum` lays out as a plain elevated block rather than a rail. Every
   test passes and the content is intact; it is a *look*, not a break, and
   **A1c owns it** — that strand gives the wizard `pf-form` and `pf-stepper`
   together.
5. **The two M-3 reported composers render the new `_settlement.html` unframed.**
   Same mechanism: their derived hosts are still `tx-derived-host`, so the new
   block's `pf-rail-sum__title` and `pf-choice` rows render correctly but
   without a rail around them. **A1b2 owns it**, and takes this prompt's rail
   wholesale.
6. **`.tx-detail-row` is a pre-existing orphan** in `transactions.css` — no
   user anywhere in `web/` or `tests/`, and not one the order flow left. Left
   in place rather than swept, because it is not this prompt's; worth a line in
   whichever strand touches the History detail.
7. **`docs/ux/atlas-scenes.json` was edited outside the stated scope** — four
   `.tx-flow:has-text(…)` selectors → `.pf-flow:has-text(…)`. Without it the
   after-image run this report asks the operator for would hang on its first
   click. `tests/tools/test_ux_atlas_reveal.py` reads scene *names*, not
   selectors, so no test covered this; nothing would have caught it but the
   run itself.

---

## For P-UX-A1b2 — the three reported-flow composers

`_commitment_composer.html`, `_secondary_buy_composer.html` and
`_secondary_sale_composer.html` with their `_*_derived.html` and
`_*_outcome.html` partials take this prompt's shapes verbatim:

- **all three** — the `pf-form` / `pf-form__main` / `pf-block__title` /
  `pf-grid` frame, the crumb + head-stamp pair (they already include
  `_order_title.html`, so only the wrapping moves), the `pf-actionbar` with
  one primary, and the retirement of their own `tx-actions` / `tx-ticket__head`;
- **`_commitment_derived.html`** — the rail with Amounts and Ledger effect;
  no settlement block (R-COMMIT moves no cash), so no `pf-choice`;
- **`_secondary_buy_derived.html`** and **`_secondary_sale_derived.html`** —
  the full rail: Amounts, Ledger effect, and `_settlement.html` as a rail
  block, which is where flag 5 is discharged;
- **`_secondary_sale_outcome.html`** — the MD-17 consequence note as
  `pf-note--info` with its choice as `pf-choice`, the shape §3.7 gave
  U-SELL's;
- the facts strips (`_secondary_sale_context.html`, `_secondary_buy_context.html`)
  onto `pf-context` in the main column, per Deliberate deviations 3.

---

```bash
git add \
  web/templates/_partials/transactions/_chooser.html \
  web/templates/_partials/transactions/_new_section.html \
  web/templates/_partials/transactions/_order_composer.html \
  web/templates/_partials/transactions/_order_title.html \
  web/templates/_partials/transactions/_order_context.html \
  web/templates/_partials/transactions/_order_derived.html \
  web/templates/_partials/transactions/_order_outcome.html \
  web/templates/_partials/transactions/_order_recalc.html \
  web/templates/_partials/transactions/_order_confirmation.html \
  web/templates/_partials/transactions/_settlement.html \
  web/templates/_partials/transactions/_messages.html \
  web/static/css/components/pf_components.css \
  web/static/css/components/transactions.css \
  web/routes/transactions.py \
  tests/web/conftest.py \
  tests/web/test_transactions_composer.py \
  tests/web/test_transactions_wizard.py \
  tests/web/test_transactions_commitment.py \
  tests/web/test_transactions_secondary_buy.py \
  tests/web/test_transactions_secondary_sale.py \
  tests/web/test_transactions_negative_cash.py \
  tests/web/test_transactions_blotter.py \
  docs/ux/atlas-scenes.json \
  docs/ux/inventory/elements.csv \
  docs/ux/inventory/routes.csv \
  docs/ux/inventory/summary.md \
  docs/reports/P-UX-A1b-report.md

git commit -m "feat(transactions): chooser and the order composer on the form vocabulary — crumb in the view head, pf-seg direction, sticky pf-rail-sum with facts, sum and legs, pf-actionbar with one primary, pf-note messages; composer swaps switch the shell to #new (UX A-1, P-UX-A1b)

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```
