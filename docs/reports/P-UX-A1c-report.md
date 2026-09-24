# P-UX-A1c — Wizard frame + `pf-stepper` · steps Identify and Classify

**Strand:** UX A-1 (Transactions) · **Runs after:** P-UX-A1b2
**Tip at session start:** `6e85297` — *"feat(transactions): four composers on
the form vocabulary … (UX A-1, **P-UX-A1b+A1b2**)"*. A1b and A1b2 landed as
**one** commit, not two; see Deliberate deviations 1.
**Before image:** `docs/ux/atlas/2026-09-24-2/transactions/scenes/` —
`git_head` `6e85297`, generated `2026-09-24T10:19Z`. The scene this prompt
changes is `wizard-step-1.png`.

**Scope as executed:** the six scope templates, the `pf-stepper` transcription
in `components/pf_components.css`, the retirement in
`components/transactions.css`, the two `docs/ux/atlas-scenes.json` edits, two
test modules, the regenerated inventory, this report.
**Not touched:** `_wizard_order.html`, `_wizard_confirm.html`,
`_wizard_outcome.html`, `_wizard_context.html`, `_wizard_recalc.html`,
`_wizard_hidden.html` (A1c2), `_master_data_fields.html`, `_messages.html`,
`web/routes/transactions.py`, `layout.css`, the four S5 panels, any
`type="number"`.

---

## OPERATOR ACTION REQUIRED

**1. The commit is yours.** Nothing is staged. The command is at the end.

**2. `portfoliflow bootstrap` before anything visual.** The gates below
truncated the dev DB repeatedly.

**3. The after-image atlas run.** Two scenes:

- `wizard-step-1` — rebuilt wholesale. The framed `tx-ticket` card is gone: the
  head is now the Section's own `<h2>`, renamed out of band to the crumb
  *‹ New transaction › Buy a new instrument* with *New ticket · Unsaved* in the
  stamp beside it, exactly as the four composers render. Below it the stepper
  is the new `pf-stepper` — flat, no card — and the two Identify cards have
  become one titled six-column grid plus a `pf-more` disclosure. Expect the
  step to read *wider and flatter*, with the right-hand 360px column standing
  empty: that empty column is the point (§1).
- `composer-secondary-sale` — **new scene**, added by §5. R-SEC-SELL had no
  photograph; it has one now (A1b2 flag 4, discharged).

**4. The browser round.** Chooser → *Buy a new instrument* → Continue → Back →
Close. Watch three things: the crumb stays *‹ New transaction › Buy a new
instrument* throughout; the stamp flips *New ticket · Unsaved* → *Ticket #n ·
Draft* on the first Continue and **stays** on Back (Back is a GET that writes
nothing, D-AH); and the fields do not shift horizontally between step 1 and
step 2 — the stable column is what this prompt is for. Also open the `No public
identifier?` disclosure once: it must open in place without moving the action
bar off the bottom.

---

## Verify-first

Under `source .venv/bin/activate`.

| # | Check | Result |
|---|---|---|
| 1 | tip / clean | **Deviation.** `6e85297` carries *both* A1b and A1b2 (`P-UX-A1b+A1b2`); A1b is **not** its own commit below it. Working tree clean; both reports (`P-UX-A1b-report.md`, `P-UX-A1b2-report.md`) are in that commit. The check's stated rationale — "this prompt transcribes from commits, not from a working tree" — is fully met, so no STOP was written. Deliberate deviations 1. |
| 2 | `wc -l` on the six | 68 / 19 / 144 / 89 / 20 / 19 — **exact match** |
| 3 | `tx-btn` per file · tree | 0 / 0 / 2 / 1 / 1 / 1 = **5** · tree **16** — exact match |
| 4 | `_order_title.html` | It is `{{ title }}` in a `<span class="pf-crumb__here" id="tx-ticket-title">` with an `oob` guard, **and nothing more**. The wizard's context supplies `title` (from `_derived_context`, "Buy a new instrument"), `ticket_number`, `ticket_status` and `oob` — every name `_composer_head.html` reads. **The include is verbatim; the head partial is not forked.** |
| 5 | `pf-stepper` here / in the record | 0 here. **0 in the record too.** `shared.css` has no `pf-stepper` family at all: its `.pf-step` (lines 471–480) is the **setup checklist's** row — `__mark`, `__name`, `__sub`, `__tag`, `__action`, one state (`is-done`) — already transcribed into `pf_components.css` as `pf-setup`/`pf-step`. What the record *does* have is a specification, in two prose files: `DC-UX-D_phase4_stress-tests.md:24` ("new component `pf-stepper`: numbered, done / active / pending, deliberately **not** interactive … Text glyphs ✓ ✕ ! become icons from the set") and `DC-UX-D_design-parameters.md:192` (catalogue row, *to build*, states "done, active, pending"). So the family is **authored from the specification, not transcribed** — and its item cannot be `.pf-step`. Deliberate deviations 2 and 3. |
| 6 | `tx-` in `atlas-scenes.json` | `.tx-stepper .tx-step.is-active` in `wizard-step-1` — the one selector, moved in §5. Scene list confirmed: `flow-chooser`, `composer-order`, `composer-commitment`, `composer-secondary`, `wizard-step-1` (plus `blotter`, `history`). |
| 7 | users of the retiring classes | `tx-ticket*` **classes**: only `_wizard.html` → none after this prompt. (`#tx-ticket-title` is an **id** and stays — `_order_title.html`, `_composer_head.html`, `_blotter.html`, five test modules.) `tx-stepper*`/`tx-step*`, `tx-idcard*`, `tx-identify`, `tx-idrow`, `tx-resolved*`, `tx-mono`: the six only. **`tx-wizard-body` is the exception** — `_wizard_order.html` and `_wizard_confirm.html` still draw it, so its rule stays. §Flags 1. The panels never carried `tx-ticket*` — verified. |
| 8 | the four orphans | Three exist as rules and are retired: `.tx-settle.is-unconfirmed`, `.tx-settle__option.is-selected`, `.tx-inline-create .tx-grid`. **`tx-detail-row` has no rule** — the single hit is a *comment* at what was line 789, explaining why `.pf-transactions .pf-table__slot > td:not(:empty)` stands in until A1e. The comment is load-bearing and stays. §Flags 2. |
| 9 | `tx-mono` precedent | A1b2 **dropped the class** and put the mono stack on the field's **id**, scoped in `transactions.css`, with the reason in the template: *"the record names no `pf-mono` family and P-UX-A1b2 does not invent one."* Same rule applied here to all three — `#tx-id-value`, `#tx-id-figi`, `#tx-currency` — declared in one selector list beside `#tx-md-currency`'s. |

---

## §1 — Frame and head (`_wizard.html`)

`<form class="pf-form" id="tx-wizard-form">` with the id, the three hidden
inputs and every `hx-*` unchanged; `_composer_head.html` included **before**
the form, verbatim; `<div class="pf-form__main">` holding the stepper and then
the active step body. `tx-ticket`, `__head`, `__ident`, `__id`, `__title` and
`__body` are gone, and the docstring's MD-2 paragraph now points at the shared
head.

The `tx-wizard-body` wrapper is gone from steps 1 and 2 — their `<section>`s
sit directly in `pf-form__main`, whose own `gap` replaces the wrapper's.
Steps 3 and 4 keep theirs for one commit, inside the new column (§Flags 1).

**The stable column.** `.pf-form` is `minmax(0, 1fr) 360px` on every step. The
form has exactly one child besides the hidden inputs — `pf-form__main` — so on
steps 1, 2 and 4 the second track is declared and empty, and the main column
keeps the width it will have on step 3. A1c2 adds `_order_derived.html` as the
form's second child on step 3 only; nothing in the main column moves when it
does. That is what R4's "one stable form column" means here.

Rendered head, step 1 (SVG elided):

```html
<h2 class="pf-view__title pf-crumb" id="new-title" hx-swap-oob="outerHTML">
    <button type="button" class="pf-crumb__back"
            hx-get="/api/transactions/chooser"
            hx-target="#tx-composer-host"
            hx-swap="innerHTML"><svg …/>New transaction</button>
    <span class="pf-crumb__sep" aria-hidden="true">›</span>
    <span class="pf-crumb__here" id="tx-ticket-title">Buy a new instrument</span>
</h2>
<span class="pf-view__asof" id="new-asof" hx-swap-oob="outerHTML">New ticket
    <span class="pf-state pf-state--unsaved">Unsaved</span>
</span>
```

and the same slots after the first Continue — MD-2 by construction:

```html
<span class="pf-view__asof" id="new-asof" hx-swap-oob="outerHTML">Ticket #1
    <span class="pf-state pf-state--draft">Draft</span>
</span>
```

## §2 — `pf-stepper`

Authored into `pf_components.css` from the record's specification (check 5),
the family's rules only, tokens only — the **sixth** component-layer addition
of the strand. Eleven declarations: the list, the item, the connector, the dot,
the label, and the two states' two rules each.

The markup is an `<ol class="pf-stepper" aria-label="Steps">` of
`<li class="pf-stepper__step">`, `aria-current="step"` on the active item,
`pf-stepper__dot` and `pf-stepper__label` inside. **Non-interactive**: no
`hx-*`, no `<button>`, no `<a>`, and the sheet styles no hover, focus or
pointer. The connector is the later step's `::before` rather than a
`.tx-stepper__bar` sibling, because an `<ol>` admits no `<div>` between its
items.

Never state by colour alone (§2.11.1): the done step swaps its number for
`pf_icon("done")` (labelled `Done`, so it is announced and not merely drawn),
and the active step carries the label's weight as well as the filled dot.

```html
<ol class="pf-stepper" aria-label="Steps">
    <li class="pf-stepper__step is-done">
        <span class="pf-stepper__dot"><svg …/></span>
        <span class="pf-stepper__label">Identify</span>
    </li>
    <li class="pf-stepper__step is-active" aria-current="step">
        <span class="pf-stepper__dot">2</span>
        <span class="pf-stepper__label">Classify</span>
    </li>
    <li class="pf-stepper__step">
        <span class="pf-stepper__dot">3</span>
        <span class="pf-stepper__label">Order</span>
    </li>
    <li class="pf-stepper__step">
        <span class="pf-stepper__dot">4</span>
        <span class="pf-stepper__label">Confirm</span>
    </li>
</ol>
```

## §3 — Identify (`_wizard_identify.html`)

The three D-AK/D-AL deviations are product decisions and are unchanged; only
the vocabulary moved.

1. **Public identifier** — `<section>` + `<h3 class="pf-block__title">` + the
   explanatory sentence as `pf-hint`, then a `pf-grid` with Scheme
   (`pf-select`), Value (`pf-input`, mono on the id) and **Resolve** as a
   default `pf-btn` in the same row. Every id, name and `hx-*` kept.
2. **Resolve error** → `pf-note--block` with `pf-note__lead`, the shape
   `_messages.html` uses. **Resolved** → a second `pf-block__title` + `pf-grid`
   with FIGI and Name as editable `pf-field`s (D-AL) and the storage sentence
   as a `pf-hint` on a `pf-field--full`.
3. **No public identifier** → `<details class="pf-more">` with the summary
   `No public identifier?` — the one line of new copy in this prompt — holding
   M-2's two paragraphs verbatim. (MD-9 gives the card the heading "No public
   identifier"; the disclosure needs a question, so the summary adds the mark.)
4. **Currency** — its own `<section>` + `pf-grid`, `pf-input` with mono on the
   id, `pf-hint`.
5. **Messages** — the `tx-messages` wrapper became **no wrapper**. No test
   addressed it, and `_messages.html` already emits `pf-note` blocks whose own
   `.pf-note + .pf-note` margin does the spacing the wrapper was doing.
6. **Action bar** — `pf-actionbar`, hint first, then Close, then Continue as
   the one `pf-btn--primary` with its `hx-vals` unchanged.

R3's leading slot is the way back, and **step 1 has none**: Close is the way
*out* of the wizard, not a step move, so it stays beside Continue at the
trailing end on every step. The bar therefore opens with its hint, whose
`flex: 1` is the same spacer the four composers' bars use.

```html
<div class="pf-actionbar">
    <p class="pf-actionbar__hint">
        Continue creates the draft ticket. It appears in the Blotter and you can
        come back to it any time.
    </p>
    <button class="pf-btn pf-btn--quiet" type="button"
            hx-get="/api/transactions/chooser"
            hx-target="#tx-composer-host"
            hx-swap="innerHTML">Close</button>
    <button class="pf-btn pf-btn--primary" type="button"
            hx-post="/api/transactions/draft"
            hx-include="closest form"
            hx-vals='{"step": "2"}'
            hx-target="#tx-composer-host"
            hx-swap="innerHTML">Continue</button>
</div>
```

Resolved block, rendered against the stubbed resolver:

```html
<h3 class="pf-block__title">Resolved</h3>
<div class="pf-grid">
    <div class="pf-field">
        <label class="pf-label" for="tx-id-figi">FIGI</label>
        <input class="pf-input" type="text" id="tx-id-figi" name="md_figi"
               value="BBG013T5K5M8">
    </div>
    <div class="pf-field pf-field--wide">
        <label class="pf-label" for="tx-id-name">Name</label>
        <input class="pf-input" type="text" id="tx-id-name" name="md_name"
               value="Meridian European Mid-Cap Equity Fund">
    </div>
    <div class="pf-field pf-field--full">
        <span class="pf-hint">
            Both identifiers (isin, figi) will be stored on the new investment.
        </span>
    </div>
</div>
```

## §4 — Classify, the two nav buttons, the sheet

1. `<section>` + `pf-block__title` "Master data" + **`pf-grid`** holding the
   `_master_data_fields.html` include and Manager / Region as
   `pf-field` + `pf-input` + `pf-optional`. The `with` block and its six hint
   strings are unchanged. **A1b2 flag 1 closes here** — the six fields now sit
   on the same six-column track as next door.
2. The AnlV gate → `pf-note--warn` with `pf-note__lead` and `pf-note__sub`,
   copy verbatim, `pf_icon("warn")` through the registry as `_messages.html`
   does.
3. `pf-actionbar`: Back leading (R3), the hint slot present but **empty** — no
   copy invented, and the element is what right-aligns the pair — then Close
   quiet, Continue primary.

```html
<div class="pf-actionbar">
    <button class="pf-btn pf-btn--quiet" type="button"
            hx-get="/api/transactions/wizard?step=1&ticket_id=…"
            hx-target="#tx-composer-host"
            hx-swap="innerHTML">Back</button>
    <p class="pf-actionbar__hint"></p>
    <button class="pf-btn pf-btn--quiet" type="button"
            hx-get="/api/transactions/chooser"
            hx-target="#tx-composer-host"
            hx-swap="innerHTML">Close</button>
    <button class="pf-btn pf-btn--primary" type="button"
            hx-post="/api/transactions/draft"
            hx-include="closest form"
            hx-vals='{"step": "3"}'
            hx-target="#tx-composer-host"
            hx-swap="innerHTML">Continue</button>
</div>
```

and the gate note:

```html
<div class="pf-note pf-note--warn">
    <svg …/>
    <p class="pf-note__lead">The AnlV category is not set.</p>
    <span></span>
    <p class="pf-note__sub">
        You can continue — but this ticket cannot be proposed or booked
        until the category is set. Regulatory reporting depends on it.
    </p>
</div>
```

**4. `transactions.css`: −144 lines net (1,230 → 1,086).** Retired by name:

| Block | Rules retired |
|---|---|
| Ticket frame | `.tx-ticket`, `.tx-ticket__head`, `.tx-ticket__ident`, `.tx-ticket__id`, `.tx-ticket__title`, `.tx-ticket__body` |
| Stepper | `.tx-stepper`, `.tx-step`, `.tx-step__dot`, `.tx-step__label`, `.tx-step.is-done .tx-step__dot`, `.tx-step.is-done .tx-step__label`, `.tx-step.is-active .tx-step__dot`, `.tx-step.is-active .tx-step__label`, `.tx-stepper__bar` |
| Identify | `.tx-identify` (+ its 860px media query), `.tx-idcard`, `.tx-idcard.is-active`, `.tx-idcard__title`, `.tx-idcard__hint`, `.tx-idrow`, `.tx-resolved`, `.tx-resolved__head`, `.tx-resolved__row` (+ its 620px media query), `.tx-resolved__note`, `.tx-mono` |
| Orphans | `.tx-settle.is-unconfirmed`, `.tx-settle__option.is-selected`, `.tx-inline-create .tx-grid` |

Two rules were **added**, both scoped to `.pf-transactions` and both named for
what retires them:

- `#tx-id-value, #tx-id-figi, #tx-currency { font-family: … mono }` — the A1b2
  precedent extended to the wizard's three mono fields, joining
  `#tx-md-currency`. All four go when the record grows a `pf-mono` family.
- `.pf-grid > .pf-field:has(> .pf-btn) { justify-content: flex-end; }` — a
  field holding a control rather than an input stretches to the grid row's
  height, so Resolve must be pushed to the baseline Scheme and Value sit on.
  M-2 puts the three in one row and this is what keeps them there. Retired when
  the record answers what an action inside a field row looks like.

`.tx-wizard-body` stays (two users left, §Flags 1); `.tx-state`, `.tx-num` and
the `.pf-table__slot` stand-in stay as before.

## §5 — Scenes

Both edits made, as a **text** edit rather than a JSON round-trip — the file's
compact one-line `steps` style is not what `json.dump` emits, and a reformat
would have buried two changes in 235 lines. Diff: **+14 / −1**.

- (a) `wizard-step-1`'s `.tx-stepper .tx-step.is-active` →
  `.pf-stepper .pf-stepper__step.is-active` (Deliberate deviations 3 for the
  item's name).
- (b) `composer-secondary-sale` added after `composer-secondary`: same area,
  start, session and section, clicking `.pf-flow:has-text("Sell a stake
  (secondary)")` — the exact `pf-flow__name` from `_chooser.html` — and waiting
  on `#tx-secsell-form`.

`pytest tests/tools -q` is green with the fifth New-section Transactions scene
(gate 1).

## §6 — Tests

`grep -ln 'wizard' tests/web` finds one module, `test_transactions_wizard.py`.

**Moved:** the `_step()` helper now reads the active `<li>` through a compiled
`_ACTIVE_STEP` regex instead of matching a `tx-step` div-and-dot string; the
currency pin reads `id="tx-currency" name="currency" maxlength="3" value=""`
where it read `class="tx-mono"`.

**Added** — a new section, *The shared form vocabulary (P-UX-A1c)*:

| Test | Pins |
|---|---|
| `test_the_wizard_renames_the_section_head_out_of_band` | `#new-title` and `#new-asof` both present with `hx-swap-oob="outerHTML"`; the crumb trail, `#tx-ticket-title`, the flow name; MD-2's unsaved state; `tx-ticket__` absent |
| `test_the_stepper_is_an_indicator_and_never_a_control` | `<ol class="pf-stepper" aria-label="Steps">`; one `<li>` per `_WIZARD_STEPS`; exactly one `aria-current="step"`; **no `hx-`, no `<button>`, no `<a>` between the list's tags**; the active dot's number; no `is-done` on step 1 |
| `test_the_first_two_steps_each_offer_exactly_one_primary` | `pf-btn--primary` count is 1 on step 1 and on step 2, and it is Continue both times; Resolve is a default `pf-btn`; no `tx-btn` |
| `test_the_action_bar_leads_with_the_way_back` | step 1's bar opens with `pf-actionbar__hint` and holds **no** Back; step 2's opens with the quiet Back button and carries the empty `<p class="pf-actionbar__hint"></p>` |
| `test_the_second_identify_card_is_a_disclosure` | `<details class="pf-more"> <summary>No public identifier?</summary>`, both paragraphs verbatim, `tx-idcard` absent |

`test_pf_components.py` gains four names in `_EXPECTED_CLASSES` —
`pf-stepper`, `pf-stepper__step`, `pf-stepper__dot`, `pf-stepper__label` — with
a comment recording that the family was authored, not transcribed.

Steps 3 and 4 keep their existing pins untouched.

---

## Component-layer additions

| Family | Source | Lines |
|---|---|---|
| `pf-stepper` (`__step`, `__dot`, `__label`, `is-done`, `is-active`) | **Authored** from `DC-UX-D_phase4_stress-tests.md:24` and `DC-UX-D_design-parameters.md:192`. `shared.css` has **no** stepper rules; its `.pf-step` (471–480) is the setup checklist's, already in the sheet. | +26 in `pf_components.css` |

Sixth of the strand, after A1b's `pf-grid`, `pf-seg`, `pf-choice`,
`pf-block__title` and `pf-context`. `ui-standards.md` §2.5 row 4 and its §4
"still to build" list both still say `pf-stepper` is **not built** — updating
them is not in this prompt's scope (§Flags 5).

## Measurements

| | Before | After |
|---|---|---|
| `tx-btn` in `web/templates` | 16 | **11** |
| — remaining in | — | `_wizard_outcome` 4, `_impact_panel` 3, `_cancel_panel` 2, `_reverse_panel` 2 |
| `components/transactions.css` | 1,230 | **1,086** (−144) |
| `components/pf_components.css` | 412 | **438** (+26) |
| `pf-form` users | 4 | **5** |
| `pf-stepper` users | 0 | **1** |
| `pf-more` users in this Area | 5 | **6** |
| the six templates | 359 | 397 |
| inventory: `transactions` templates / elements / headings | 38 / 250 / 21 | **39 / 254 / 24** |

The template count rises because `_wizard_stepper.html` now carries an
inventoried element (the labelled `<ol>`); it had none before. The three new
headings are the `pf-block__title` `<h3>`s — Public identifier, Resolved,
Master data.

## Gates

| # | Gate | Result |
|---|---|---|
| 1 | `pytest tests/web/test_transactions_*.py tests/web/test_pf_components.py tests/web/test_css_tokens.py tests/tools -q`, then `pytest tests/regression -q`, **serially** | **490 passed** (7:02) · **164 passed** (2:15). No two DB-backed runs overlapped. |
| 2 | `ruff check` · `ruff format --check` | clean · 515 files already formatted |
| 3 | `tx-btn` tree total | **11** (16 − 5) ✓ |
| 4 | `tx-` classes in the six | **0**. Remaining `tx-` strings, all ids or hx targets: `_wizard` — `tx-composer-host`, `tx-wizard-form`; `_wizard_stepper` — none; `_wizard_identify` — `tx-composer-host`, `tx-id-scheme`, `tx-id-value`, `tx-id-figi`, `tx-id-name`, `tx-currency`; `_wizard_classify` — `tx-composer-host`, `tx-md-manager`, `tx-md-region`, plus `tx-grid` **in the docstring** (prose, recording what the step drew for one commit); `_wizard_back`, `_wizard_close` — `tx-composer-host`. |
| 5 | colour literals in the two sheets | **none new**. The two grep hits are prose: `#061` in the file header and the word `rgba()` in a comment at line 600. |
| 6 | `python tools/ux_inventory.py` | re-run, committed. `transactions` row 38/250/21 → 39/254/24. |
| 7 | `git diff --name-only` | the six templates, the two sheets, the scenes file, two test modules, the inventory (3 files), this report — **nothing else**. `_wizard_order.html`, `_wizard_confirm.html` and `_wizard_outcome.html` are untouched. |

---

## Deliberate deviations

**1. Check 1 passed on substance, not on form — no STOP was written.** The
check asks for A1b2 at the tip *and* A1b as its own commit below it. The tip is
`6e85297`, whose subject ends `(UX A-1, P-UX-A1b+A1b2)`: the operator landed
both prompts in one commit. A1b is therefore not a separate commit — but
nothing of A1b is missing, both reports are in the tree, and `git status` is
clean. The check's own stated reason is *"this prompt transcribes from commits,
not from a working tree"*, and that condition holds exactly. Stopping would
have bought a commit boundary, not a correctness property.

**2. `pf-stepper` is authored, not transcribed.** §2 says to transcribe the
family from `shared.css`. There is nothing there to transcribe: the record
*names* `pf-stepper` in two prose files and lists it as *to build*, and its
`.pf-step` rules belong to the setup checklist. The eleven declarations are
written here from the record's specification — numbered, three states, no
navigation, glyphs from the icon set — and every value is a `--ui-*` token. The
"still to build" list in `ui-standards.md` §4 says the same thing in the
product's own words: *"in the record, absent here"* is, for this one family,
"specified in the record, drawn nowhere".

**3. The item is `.pf-stepper__step`, not `.pf-step`.** §2 and §5 name
`pf-step`, `pf-step__dot` and `pf-step__label`. `.pf-step` is **already
defined** in `pf_components.css` (line 288 before this prompt, 314 after) as the
setup checklist's row: `display: grid`, `grid-template-columns: 24px minmax(0,
1fr) auto`, a `border-top`, padding, and `counter-increment: step`. A stepper
item placed on that class inherits all of it and must override each property to
look like a dot beside a label — and would then silently change the next time
the checklist's row is edited. Two families that share a block name share a
fate. The sub-element names the prompt gives (`__dot`, `__label`) do not
collide and are kept, re-based on the new block. The §5 scene selector is
`.pf-stepper .pf-stepper__step.is-active` accordingly. *(The prompt's check 5
expectation — "states: done / active / upcoming" — reads like the setup
checklist's `.pf-step` grep output was taken for the stepper's. That is the
misread this deviation corrects.)*

**4. The tick is `pf_icon("done")`, not `pf_icon("check")`.** §2 names
`pf_icon("check")`; the registry key is `"done"` (→ Lucide `check`). `pf_icon`
raises `LookupError` on an unknown name, so `"check"` would fail the render
rather than fall back. The icon is given the label `Done` rather than being
decorative, because "this step is finished" is state and the only other place
it is stated is the dot's colour — which §2.11.1 does not allow to carry it
alone.

**5. One new CSS rule beyond the transcription.**
`.pf-transactions .pf-grid > .pf-field:has(> .pf-btn)` bottom-aligns the
Resolve button with the two inputs beside it. §3 asks for the button "in the
same row"; a `pf-field` is a stretched flex column, so without this its button
sits at the top of the row. The alternative was an `&nbsp;` label spacer in the
markup, which is a lie in the accessibility tree. Scoped to the Area, `:has()`
as A1a's `pf-table` writer already uses in this sheet, and named for what
retires it.

---

## Flags

**1. `tx-wizard-body` survives this commit, by design.** Check 7 finds it in
`_wizard_order.html` and `_wizard_confirm.html`, both A1c2's. §4.4 lists it
among the rules to retire, but retiring it here would strip the flex column and
gap from steps 3 and 4 for one commit. The rule stays with a comment naming
A1c2 as its executioner. Everything else on §4.4's list is gone.

**2. `tx-detail-row` is not a rule.** Check 8 names four orphans; only three
exist. The fourth string is inside the comment that explains why
`.pf-transactions .pf-table__slot > td:not(:empty)` stands in until A1e — it
describes the rule the panels *used to* take their padding from. Deleting it
would remove the reason for a rule that is still live. It stays.

**3. The `tx-messages` wrapper became no wrapper.** §3.5 asks which. No test
addressed it, and `_messages.html`'s own `.pf-note + .pf-note` margin supplies
the spacing the wrapper's gap was providing, so the element is gone rather than
neutralised.

**4. `pf-stepper` is a one-user family.** It exists for the wizard alone. That
is the same position `pf-seg` and `pf-choice` were in after A1b, and the record
puts it in the catalogue, so it belongs in the component layer rather than in
`transactions.css`. Worth a second look if no second stepped surface appears.

**5. `ui-standards.md` still says `pf-stepper` is not built.** §2.5 row 4
("`pf-stepper` is **not built**; the wizard runs on `tx-stepper` — strand A-1")
and the §4 "still to build" list are both now stale. Updating them is a
documentation edit outside this prompt's scope (`ui-standards.md` is not in
§Scope), and it wants doing once with the rest of the strand's rows — several
of which A1b and A1b2 also left stale.

**6. The `pf-more` disclosure starts closed.** M-2 draws the second card open
beside the first. §2.6.4's disclosure is closed until opened, so the "no public
identifier" path is now one click less visible than the mockup makes it. The
copy that tells an operator what to do — *"Leave the fields beside this card
empty and continue"* — is inside it. If the operator judges that too quiet, the
fix is `<details class="pf-more" open>`, one attribute.

---

## For A1c2

Steps 3 and 4 and the outcome render **inside the new frame on their old
classes**, which is expected and is what §Scope reserved. Concretely, today:
`_wizard_order.html` and `_wizard_confirm.html` still open with
`<div class="tx-wizard-body">`, now nested inside `pf-form__main`; step 3's
`_order_derived.html` rail therefore draws **in the main column**, stacked
under the fields rather than beside them, and the 360px second track stands
empty on all four steps; step 4's Confirm card is still `tx-sumcard` and its
bar still `tx-actions` with `tx-btn`s (the eleven that remain are four in
`_wizard_outcome.html` plus the seven in the S5 panels). A1c2 makes
`_order_derived.html` the **form's second child on step 3 only** — which is the
whole reason the column is declared on every step here — retires
`tx-wizard-body` with steps 3 and 4, and moves the outcome onto `pf-actionbar`.

---

```bash
git add web/templates/_partials/transactions/_wizard.html \
        web/templates/_partials/transactions/_wizard_stepper.html \
        web/templates/_partials/transactions/_wizard_identify.html \
        web/templates/_partials/transactions/_wizard_classify.html \
        web/templates/_partials/transactions/_wizard_back.html \
        web/templates/_partials/transactions/_wizard_close.html \
        web/static/css/components/pf_components.css \
        web/static/css/components/transactions.css \
        docs/ux/atlas-scenes.json \
        tests/web/test_transactions_wizard.py \
        tests/web/test_pf_components.py \
        docs/ux/inventory/ \
        docs/reports/P-UX-A1c-report.md

git commit -m "feat(transactions): wizard on the form vocabulary — pf-form frame with one stable column, non-interactive pf-stepper, shared composer head, Identify and Classify on pf-field/pf-note/pf-more, pf-actionbar with Back leading and one primary; secondary-sale atlas scene added (UX A-1, P-UX-A1c)"
```
