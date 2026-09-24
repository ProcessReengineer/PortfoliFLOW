# P-UX-A1b2 — The three reported-flow composers onto A1b's shapes

**Strand:** UX A-1 (Transactions) · **Runs after:** P-UX-A1b
**Tip at session start:** `cae50f1` — *"feat(transactions): blotter and history
onto the pf-table vocabulary … (UX A-1, P-UX-A1a)"*.
**A1b is in the working tree, uncommitted** — see Deliberate deviations 1. Its
eleven templates, two sheets, route, seven test modules, scene file and
inventory are all `M`, and `docs/reports/P-UX-A1b-report.md` is untracked. Its
shapes are therefore all present and this prompt transcribes onto them as
specified; what it is *not* is a commit at the tip.
**Before image:** `docs/ux/atlas/2026-09-24/transactions/scenes/` — `git_head`
`cae50f1`, generated `2026-09-24T05:53Z`. Three composer scenes:
`composer-order.png`, `composer-commitment.png`, `composer-secondary.png`.
**There is no secondary-sale scene** — `atlas-scenes.json` defines three
composer scenes plus `wizard-step-1`, and `composer-secondary` clicks "Buy a
stake (secondary)", i.e. R-SEC-BUY. See §Flags 4.

**Scope as executed:** the twelve scope templates, one new
`_composer_head.html`, the one-line include in `_order_composer.html`, the
retirement and three re-homes in `components/transactions.css`, three test
modules, the regenerated inventory, this report.
**Not touched:** `docs/ux/atlas-scenes.json` (check 7 found no retired class in
the three composer scenes — only ids), `pf_components.css`, `layout.css`,
`web/routes/transactions.py`, `_effect_leg.html`, `_settlement.html`,
`_messages.html`, the wizard's own partials, the four S5 panels, any
`type="number"`.

---

## OPERATOR ACTION REQUIRED

**1. The commit is yours.** Nothing is staged. The command is at the end. It
covers **this** prompt's files only — A1b's own commit, whose command is at the
end of its report, is still outstanding and comes first.

**2. `portfoliflow bootstrap` before anything visual**, and a member user. The
gates here truncated the dev DB repeatedly.

**3. The after-image atlas run.** Three composer scenes change:

- `composer-commitment` — rebuilt wholesale: the crumb and head stamp replace
  the in-composer `tx-ticket__head`, the groups become real `<h3>` headings on
  a six-column grid, the On-booking block becomes a one-block rail on the
  right, and the action bar reverses into Discard · hint · draft · Propose ·
  **Book now**.
- `composer-secondary` (R-SEC-BUY) — the same, plus the three-block rail where
  A1b's `_settlement.html` finally sits framed. **This is where A1b's flag 5 is
  discharged.**
- `composer-order` — unchanged in content, but it now draws its head from
  `_composer_head.html`. If it regresses, §1 is the cause.
- `wizard-step-1` — its Classify step inherits `_master_data_fields.html`'s new
  `pf-field` markup inside the wizard's old `tx-grid`. Expect wider fields, not
  a break. §Flags 1.

R-SEC-SELL has no scene, so its rail and scope block are unphotographed.
Adding one is a scene-file edit this prompt did not make (§Flags 4).

**4. The browser round.** Open each of the three flows from the chooser; one
recalculation each (the rail must move and nothing else); `‹` back to the
chooser from each, with the plain "New transaction" heading and an empty stamp
restored. On the sale, click "Sell part of the stake" — the option must take
the red border and all four gestures must go flat.

---

## Verify-first

| # | Check | Result |
|---|---|---|
| 1 | `git log --oneline -2` · `git status --porcelain` | **Deviates.** Tip is `cae50f1` (P-UX-A1a); A1b is uncommitted in the tree, not at the tip. Tree not clean. Deliberate deviations 1. |
| 2 | `wc -l` on the twelve | 148 / 40 / 83 / 155 / 78 / 84 / 155 / 41 / 35 / 84 / 92 / 79 — **exact, all twelve** |
| 3 | `tx-btn` in the three outcome partials · tree total | 4 / 4 / 4 · **28** — exact |
| 4 | `type="number"` in the three composers | 2 / 4 / 3 — exact. All eight keep `type="number"` and gain `pf-input pf-input--num`. |
| 5 | who includes `_master_data_fields` | the two composers **and** `_wizard_classify.html:44` — the wizard inherits, as predicted |
| 6 | the crumb + stamp pair in `_order_composer.html` | lines **44–60** (17 lines): `<h2 class="pf-view__title pf-crumb" id="new-title" hx-swap-oob="outerHTML">` … `</h2>`, an explanatory comment, then `<span class="pf-view__asof" id="new-asof" hx-swap-oob="outerHTML">` … `</span>` |
| 7 | `tx-` in `atlas-scenes.json` | **ten hits, every one an id** — `#tx-composer-host` ×4, `#tx-order-form`, `#tx-commit-form`, `#tx-secbuy-form`, `#tx-wizard-form`, and `.tx-stepper .tx-step.is-active` at line 196, which belongs to the **wizard** scene (A1c's). **No retired class in the three composer scenes → the scene file is not edited.** |
| 8 | users of the twelve `tx-` families | Only three families lose their last user here: `tx-derived*` (the three rails, class use only — the `#tx-derived` **id** stays), `tx-fraction*` (`_secondary_sale_scope`) and `tx-nocash` (`_commitment_composer`). Everything else keeps a user: `tx-msg*` / `tx-messages` / `tx-actions*` / `tx-outcome` / `tx-grid` / `tx-field*` / `tx-mono` (the three wizard steps + `_reversal_report` + the four panels), `tx-block*` (wizard + `_history_detail`), `tx-legs*` / `tx-leg*` (`_wizard_confirm` + `_effect_leg` + `_history_detail`), `tx-context*` (`_wizard_context`), `tx-ticket*` (`_wizard.html`), `tx-state*` (`_wizard` + `_impact_panel`). |
| 9 | what `tx-nocash` is | **A notice with a lead sentence** — `border: 1px solid var(--ui-semantic-info)`, `strong` inked info, two paragraphs with `p + p` quieter and smaller. So `pf-note--info` with `pf-note__lead` and two `pf-note__sub`, not a `pf-hint` one-liner. §2.1. |

---

## §1 — One composer head — done

`_partials/transactions/_composer_head.html`, 48 lines: a 31-line docstring and
the 17 lines of check 6, **byte-identical** (extracted with `sed -n '44,60p'`
and appended, not retyped). `_order_composer.html` lost exactly those 17 lines
and gained one `{% include %}`; nothing else in it moved — including its
docstring, which still describes the crumb and the stamp it no longer holds.
§Flags 3.

All four composers include it as their first line, before the `<form>`, the
position `_order_composer.html` had it in. The OOB ids `new-title` and
`new-asof` are unchanged, `_order_title.html` is still *included* into the
crumb's `__here` slot with its own id and its own swap, and the three M-3
recalc partials still include `_order_title.html` directly — they refresh the
title without moving the head's number and state pill, which MD-2 requires.

The way back now covers all four composers by construction: `_chooser.html`
restores the plain heading and an empty stamp, `_order_confirmation.html` its
own pair. Neither was touched.

## §2 — The three composers — done

Each is `<form class="pf-form" id="tx-<flow>-form" …>` with every `hx-*`
attribute and every id unchanged, holding `<div class="pf-form__main">` of
`<section>` + `<h3 class="pf-block__title">` + `<div class="pf-grid">`, the
outcome partial last in the main column, and the derived partial as the form's
**second child** — so `.pf-form`'s own `minmax(0, 1fr) 360px` places the rail
with no rule of this Area's own, exactly as A1b's flag 2 found for the order
composer. `transactions.css` gained **no** layout rule in this commit.

Field mapping throughout: `tx-field` → `pf-field`, `--wide` → `pf-field--wide`,
`--numeric` → `pf-input--num` on the input (the wrapper keeps no modifier),
`tx-field__label` → `pf-label`, `__hint` → `pf-hint`, `__optional` →
`pf-optional`, selects `pf-select`, text/number/date inputs `pf-input`, dates
native. `tx-ticket`, `tx-ticket__head`, `__ident`, `__id` and `__body` are gone
from all three — the head is §1's.

1. **`_commitment_composer.html`** — `tx-nocash` → **`pf-note--info` with
   `pf-note__lead` and two `pf-note__sub`**, per check 9: it is a notice with a
   lead sentence, not a one-line hint. The lead drops its `<strong>` wrapper
   because `pf-note__lead` carries the weight itself; the copy is verbatim. The
   two paragraphs become two `__sub` rather than one, because the family places
   that slot at `grid-column: 2 / -1` and M-3's second paragraph is a separate
   thought. `_master_data_fields.html` per §3.
2. **`_secondary_buy_composer.html`** — as 1, with its four numeric fields
   (vintage, purchase price, acquired NAV, assumed unfunded) on
   `pf-input pf-input--num`.
3. **`_secondary_sale_composer.html`** — `_secondary_sale_context.html` →
   `<dl class="pf-context" id="tx-secsell-context" hx-swap-oob…>` in the main
   column, the `<div><dt><dd>` shape `_order_context.html` uses, id and OOB
   unchanged (A1b deviation 3 — an element carrying `hx-swap-oob` inside
   `#tx-derived` would be lifted out of the fragment before it lands).
   `_secondary_sale_scope.html` → a `<section>` keeping its id and OOB, with
   `pf-block__title` and the two options as `pf-choice`. `is-refused` stays a
   state class beside `pf-choice`, its one border rule re-homed to
   `.pf-transactions .pf-choice.is-refused` — the record has no refused
   variant, because no mock offers a choice that refuses. The sub-lines went to
   `pf-choice__figure`, **not** `pf-hint` — see Deliberate deviations 2.

## §3 — `_master_data_fields.html` — done

Six fields onto `pf-field` inside the including composer's `pf-grid`, with
`pf-label` / `pf-input` / `pf-select` / `pf-hint`. Two carries:

- **`tx-mono` → an id rule.** `pf_components.css` declares no mono class and
  `theme.css` only the `--ui-font-family-mono` token, so per §3 no `pf-mono`
  was invented. The stack rides on `.pf-transactions #tx-md-currency` in
  `transactions.css`, with a comment saying it goes when the record grows the
  family. `.tx-mono` itself **stays** — `_wizard_identify.html` has three uses,
  including its own separate `#tx-currency` field, and that file is A1c's.
- **`is-missing` re-homed.** `.tx-field.is-missing input, … select` →
  `.pf-transactions .pf-field.is-missing input, … select`. This file was its
  only user; `_wizard_confirm.html`'s `dd.is-missing` is a different rule
  (`.tx-sumcard dd.is-missing`) and is untouched.

The wizard's Classify step renders the new `pf-field` markup inside its old
`tx-grid` (`repeat(auto-fit, minmax(190px, 1fr))`), so the six fields carry
`grid-column: span 2` / `span 4` against an indefinite track count. A look, not
a break — every wizard test passes. §Flags 1.

## §4 — The three rails — done

All three are `<aside class="pf-rail-sum" id="tx-derived" aria-label="…">`, the
`_order_derived.html` shape. `tx-derived-host` is gone; the `#tx-derived` id and
the recalculation's `hx-target` are unchanged, so one swap still covers every
block in the rail — including `_settlement.html`, which is now inside it on the
two secondaries. **A1b's flag 5 is discharged.**

| | blocks | `aria-label` |
|---|---|---|
| `_commitment_derived.html` | **1** — On booking | "What booking will emit" |
| `_secondary_buy_derived.html` | 3 — Amounts · On booking · Settlement position | "Amounts and settlement" |
| `_secondary_sale_derived.html` | 3 — Amounts · On booking · Settlement position | "Amounts and settlement" |

The commitment rail has **one** block, not the two §4 predicted — see
Deliberate deviations 3. The `tx-derived__row--info` rows became `pf-sum`
**pairs**, not hints: "Price vs. acquired NAV" and "vs. last reported NAV" are
labelled figures like the rows above them, and a hint under the list would have
detached the label from its value. The `--total` row on the sale became
`pf-sum__total` on both `<dt>` and `<dd>`. The inline `tx-leg` rows in these
three files are the rails' own and migrated to `pf-leg`; `_effect_leg.html` did
not move.

The "Emitted together, or not at all:" lead stays a **visible** `pf-hint` above
the rows rather than going behind `pf-more` — Deliberate deviations 4. The D-3
pending sentences are `pf-hint`, as §4 asked.

## §5 — The three outcomes — done

Each `<div class="tx-outcome" …>` became `<div id="tx-<flow>-outcome"
hx-swap-oob…>` with no class of its own — the shape `_order_outcome.html` has.
`tx-messages` became the bare `_messages.html` include; the AnlV block on the
two creating flows stays each flow's own (M-2 and M-3 word the same gate
differently, and `_messages.html` has no branch for it) and took
`_messages.html`'s `pf-note--block` shape with `pf_icon("block")`,
`pf-note__lead` and `pf-note__sub`. **Nothing was duplicated:** MD-18's own
refusal already lives in `_messages.html` as the `partial_secondary_sale`
branch, so the sale outcome restates no block.

The MD-17 consequence is `pf-note--info` with `pf-note__lead` and
`pf-note__sub` and **no `pf-choice`** — see Deliberate deviations 5.

The action bars are `_order_outcome.html`'s order exactly — Discard/Close
`pf-btn--quiet` · `pf-actionbar__hint` · Save as draft `pf-btn` · Propose
`pf-btn` · Book now `pf-btn--primary`. `tx-actions__spacer` retired; the hint's
`flex: 1` does its work. Every hint sentence, every `disabled` expression and
every endpoint is unchanged.

---

## Render check — through the test client, per flow, with values entered

Captured from live `POST /api/transactions/recalc` responses against a seeded
dev DB (one stake, one EUR cash position, M-3's own figures), not hand-written.
Jinja's blank control-flow lines are collapsed; every markup line is verbatim.

### The three rails

**R-COMMIT rail — one block, no settlement (MD-19)**

```html
<aside class="pf-rail-sum" id="tx-derived" aria-label="What booking will emit">
    <div>
        <p class="pf-rail-sum__title">On booking</p>
        <p class="pf-hint">Emitted together, or not at all:</p>
                <div class="pf-leg">
                    <span class="pf-leg__type">create</span>
                    <span>Investment · Nordwind Private Credit Fund II <em>reported</em></span>
                    <span class="pf-leg__amount"></span>
                </div>
                <div class="pf-leg">
                    <span class="pf-leg__type">commit</span>
                    <span>Commitment recorded <em>vintage 2026</em></span>
                    <span class="pf-leg__amount">5,000,000.00 EUR</span>
                </div>
    </div>
</aside>
```
**R-SEC-BUY rail — three blocks, settlement inside (A1b flag 5, discharged)**

```html
<aside class="pf-rail-sum" id="tx-derived" aria-label="Amounts and settlement">
    <div>
        <p class="pf-rail-sum__title">Amounts</p>
        <dl class="pf-sum">
            <dt>Purchase price (cash out)</dt>
            <dd>−2,208,000.00 EUR</dd>
            <dt>Acquired NAV</dt>
            <dd>2,400,000.00 EUR</dd>
            <dt>Price vs. acquired NAV</dt>
            <dd>−8.0 % (discount)</dd>
        </dl>
    </div>
    <div>
        <p class="pf-rail-sum__title">On booking</p>
        <p class="pf-hint">Emitted together, or not at all:</p>
                <div class="pf-leg">
                    <span class="pf-leg__type">create</span>
                    <span>Investment · Halstenbek Infrastructure Partners II <em>reported</em></span>
                    <span class="pf-leg__amount"></span>
                </div>
                <div class="pf-leg">
                    <span class="pf-leg__type">nav</span>
                    <span>Opening NAV at trade date <em>manual origin</em></span>
                    <span class="pf-leg__amount">2,400,000.00 EUR</span>
                </div>
                <div class="pf-leg">
                    <span class="pf-leg__type">commit</span>
                    <span>Unfunded commitment assumed <em>vintage 2022</em></span>
                    <span class="pf-leg__amount">600,000.00 EUR</span>
                </div>
                <div class="pf-leg">
                    <span class="pf-leg__type">sell</span>
                    <span>EUR Cash — Commerzbank <em>@ 1.0000</em></span>
                    <span class="pf-leg__amount">−2,208,000.0000 units</span>
                </div>
    </div>
    <div>
        <p class="pf-rail-sum__title">Settlement position</p>
            <p class="pf-hint">
                    One EUR cash position on the book. Confirm it or pick another.
            </p>
                <label class="pf-choice is-selected">
                    <input type="radio" name="cash_investment_id"
                           value="8884156f-6c37-4ee8-a889-5771d1a47394"
                           checked>
                    <span>EUR Cash — Commerzbank</span>
                    <span class="pf-choice__figure">
                        <span>2,750,000.00</span>
                        EUR
                            →
                            <span>542,000.00</span>
                    </span>
                </label>
            <label class="pf-check">
                <input type="checkbox" name="settle_confirm" value="1"
                       checked>
                <span>Settle against this position.</span>
            </label>
    </div>
</aside>
```
**R-SEC-SELL rail — three blocks, the total and the MD-20 pair**

```html
<aside class="pf-rail-sum" id="tx-derived" aria-label="Amounts and settlement">
    <div>
        <p class="pf-rail-sum__title">Amounts</p>
        <dl class="pf-sum">
            <dt>Sale proceeds</dt>
            <dd>1,850,000.00 EUR</dd>
            <dt>Fees</dt>
            <dd>−12,500.00 EUR</dd>
            <dt>Taxes</dt>
            <dd>—</dd>
            <dt class="pf-sum__total">Net proceeds</dt>
            <dd class="pf-sum__total">+1,837,500.00 EUR</dd>
            <dt>vs. last reported NAV</dt>
            <dd>−4.3 %</dd>
        </dl>
    </div>
    <div>
        <p class="pf-rail-sum__title">On booking</p>
        <p class="pf-hint">Emitted together, or not at all:</p>
                <div class="pf-leg">
                    <span class="pf-leg__type">flow</span>
                    <span>Cinder Ridge Buyout Fund III <em>distribution · actual</em></span>
                    <span class="pf-leg__amount">+1,837,500.00 EUR</span>
                </div>
                <div class="pf-leg">
                    <span class="pf-leg__type">nav</span>
                    <span>NAV set to zero at trade date <em>manual origin</em></span>
                    <span class="pf-leg__amount">0.00 EUR</span>
                </div>
                <div class="pf-leg">
                    <span class="pf-leg__type">status</span>
                    <span>Investment set inactive <em>full disposal</em></span>
                    <span class="pf-leg__amount"></span>
                </div>
                <div class="pf-leg">
                    <span class="pf-leg__type">buy</span>
                    <span>EUR Cash — Commerzbank <em>@ 1.0000</em></span>
                    <span class="pf-leg__amount">+1,837,500.0000 units</span>
                </div>
    </div>
    <div>
        <p class="pf-rail-sum__title">Settlement position</p>
            <p class="pf-hint">
                    One EUR cash position on the book. Confirm it or pick another.
            </p>
                <label class="pf-choice is-selected">
                    <input type="radio" name="cash_investment_id"
                           value="b4a016e8-3723-496a-89ad-5b3037218f9e"
                           checked>
                    <span>EUR Cash — Commerzbank</span>
                    <span class="pf-choice__figure">
                        <span>2,750,000.00</span>
                        EUR
                            →
                            <span>4,587,500.00</span>
                    </span>
                </label>
            <label class="pf-check">
                <input type="checkbox" name="settle_confirm" value="1"
                       checked>
                <span>Settle against this position.</span>
            </label>
    </div>
</aside>
```

### The three action bars

**R-COMMIT**

```html
<div class="pf-actionbar">
        <button class="pf-btn pf-btn--quiet" type="button"
                hx-get="/api/transactions/chooser"
                hx-target="#tx-composer-host"
                hx-swap="innerHTML">Discard</button>
        <p class="pf-actionbar__hint">Booking creates the investment and records the commitment. No cash moves.</p>
        <button class="pf-btn" type="button"
                hx-post="/api/transactions/draft"
                hx-include="closest form"
                hx-target="#tx-composer-host"
                hx-swap="innerHTML"
                >Save as draft</button>
        <button class="pf-btn" type="button"
                hx-post="/api/transactions/propose"
                hx-include="closest form"
                hx-target="#tx-composer-host"
                hx-swap="innerHTML"
                >Propose</button>
        <button class="pf-btn pf-btn--primary" type="button"
                hx-post="/api/transactions/book"
                hx-include="closest form"
                hx-target="#tx-composer-host"
                hx-swap="innerHTML"
                >Book now</button>
    </div>
```
**R-SEC-BUY**

```html
<div class="pf-actionbar">
        <button class="pf-btn pf-btn--quiet" type="button"
                hx-get="/api/transactions/chooser"
                hx-target="#tx-composer-host"
                hx-swap="innerHTML">Discard</button>
        <p class="pf-actionbar__hint">Booking creates the investment, opening NAV, commitment and cash leg in one step.</p>
        <button class="pf-btn" type="button"
                hx-post="/api/transactions/draft"
                hx-include="closest form"
                hx-target="#tx-composer-host"
                hx-swap="innerHTML"
                >Save as draft</button>
        <button class="pf-btn" type="button"
                hx-post="/api/transactions/propose"
                hx-include="closest form"
                hx-target="#tx-composer-host"
                hx-swap="innerHTML"
                >Propose</button>
        <button class="pf-btn pf-btn--primary" type="button"
                hx-post="/api/transactions/book"
                hx-include="closest form"
                hx-target="#tx-composer-host"
                hx-swap="innerHTML"
                >Book now</button>
    </div>
```
**R-SEC-SELL**

```html
<div class="pf-actionbar">
        <button class="pf-btn pf-btn--quiet" type="button"
                hx-get="/api/transactions/chooser"
                hx-target="#tx-composer-host"
                hx-swap="innerHTML">Discard</button>
        <p class="pf-actionbar__hint">Booking emits all four effects in one step.</p>
        <button class="pf-btn" type="button"
                hx-post="/api/transactions/draft"
                hx-include="closest form"
                hx-target="#tx-composer-host"
                hx-swap="innerHTML"
                >Save as draft</button>
        <button class="pf-btn" type="button"
                hx-post="/api/transactions/propose"
                hx-include="closest form"
                hx-target="#tx-composer-host"
                hx-swap="innerHTML"
                >Propose</button>
        <button class="pf-btn pf-btn--primary" type="button"
                hx-post="/api/transactions/book"
                hx-include="closest form"
                hx-target="#tx-composer-host"
                hx-swap="innerHTML"
                >Book now</button>
    </div>
```

### The secondary sale's scope block

```html
<section id="tx-secsell-scope" >
    <h3 class="pf-block__title">Scope of the sale</h3>
    <label class="pf-choice is-selected">
        <input type="radio" id="tx-secsell-frac-full" name="fraction" value="full"
               checked>
        <span>Sell the entire stake</span>
        <span class="pf-choice__figure">NAV, unfunded commitment and the position close together.</span>
    </label>
    <label class="pf-choice">
        <input type="radio" id="tx-secsell-frac-partial" name="fraction" value="partial"
               >
        <span>Sell part of the stake</span>
        <span class="pf-choice__figure">Not supported in v1.</span>
    </label>
</section>
```

---

## Measurements

| | Before (post-A1b tree) | After |
|---|---:|---:|
| `tx-btn` lines, template tree | 28 | **16** |
| `tx-btn` lines, the three outcome partials | 4 / 4 / 4 | **0 / 0 / 0** |
| `tx-` **classes** in the twelve + `_composer_head.html` | many | **0** |
| the twelve, lines | 1,074 | 1,107 |
| `_order_composer.html`, lines | 186 | **170** (−17 +1) |
| `_composer_head.html` | — | **48** (new) |
| `components/transactions.css`, lines | 1,318 | **1,230** (−88) |
| rules in `transactions.css` | — | **18 retired**, 2 of them re-homed, 1 added |
| `pf_components.css`, lines | 412 | **412** (untouched) |
| `type="number"`, tree total | 38 | **38** — A1d's |
| `type="number"`, `_partials/transactions/` | 18 | **18** — A1d's |

Shared-family users, by file:

| Family | Before | After | Who joined |
|---|---:|---:|---|
| `pf-form` | 1 | **4** | the three composers |
| `pf-rail-sum` | 1 | **4** | the three derived partials |
| `pf-actionbar` | 2 | **5** | the three outcome partials |
| `pf-context` | 1 | **2** | `_secondary_sale_context` |
| `pf-choice` | 2 | **3** | `_secondary_sale_scope` |
| `pf-sum` | 1 | **3** | the two secondary rails |
| `pf-leg` | 1 | **4** | the three rails |
| `pf-grid` | 2 | **5** | the three composers |
| `pf-block__title` | 3 | **6** | the three composers (+ the scope section) |
| `pf-note` | 5 | **8** | the three outcomes + the commitment composer |

`pf-actionbar` is 2 → 5, not 1 → 4: `_order_confirmation.html` already carried
one (A1b). `pf-choice` reaches 3 by files, not more, because MD-17's
consequence carries no control — Deliberate deviations 5.

Inventory, `transactions` row (post-A1b → post-A1b2):

| | templates | element rows | headings | buttons | HTMX triggers | links | form labels | pills | tiles | routes | js calls | distinct nouns |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| before | 34 | 237 | 11 | 51 | 51 | 2 | 69 | 7 | 0 | 23 | **0** | 10 |
| after | **38** | **250** | **21** | 51 | 51 | 2 | 69 | 7 | 0 | 23 | **0** | 10 |

Both moves reconcile exactly. **Templates +4** — `_composer_head.html` plus the
three `*_derived.html`, which become inventoried because the rail carries an
`aria-label` where `tx-derived-host` carried nothing. **Headings +10** — the
group titles becoming real `<h3>`: three on the commitment, three on the
secondary buy, three on the sale plus the scope section's own. Buttons, triggers
and form labels are unchanged, which is the point: this is a transcription.

Second table (findings), `transactions`: `from:aria-label` **3 → 6**, the three
rails' labels, measured. `abbrev` 3, `duplicate-label` 43 and `dynamic` 67 are
all unchanged.

---

## Gates

| # | Gate | Result |
|---|---|---|
| 1 | `pytest tests/web/test_transactions_*.py tests/web/test_pf_components.py tests/web/test_css_tokens.py tests/tools -q` · `pytest tests/regression -q` | **green — 481 passed, 0 failed, in 10:14**, and **164 passed, 0 failed, in 4:02** under `tests/regression`. **645 in all.** Collection reconciles: 471 before (the ten transactions modules, `test_pf_components`, `test_css_tokens` and `tests/tools`) plus the 10 added here. The first attempt at this gate returned four failures; every one was a concurrency artefact and is accounted for in §Flags 8. |
| 2 | `ruff check` · `ruff format --check` | **All checks passed** · **940 files already formatted** |
| 3 | `tx-btn` tree total | **16** — exactly 28 − 12. The twelve that left were the three outcome partials' four apiece; the sixteen that remain are the wizard's ten and the four S5 panels' nine … eight: `_wizard_outcome` 4, `_wizard_identify` 2, `_wizard_back` 1, `_wizard_classify` 1, `_wizard_close` 1, `_cancel_panel` 2, `_reverse_panel` 2, `_impact_panel` 3. |
| 4 | `tx-` classes in the thirteen | **0.** Every remaining `tx-` string is an **id**, an `hx-target`, a `for=` or docstring prose — listed in full below. |
| 5 | colour literals in `transactions.css` | **none new.** The two `grep` hits are the string `#061` in the head comment and the word `rgba()` in another comment. |
| 6 | `python tools/ux_inventory.py` | re-run and written; the row is above |
| 7 | `git diff --name-only` | this prompt's files are exactly the twelve, `_composer_head.html`, `_order_composer.html`, `transactions.css`, **three** test modules, the three inventory files and this report. `tests/web/test_transactions_wizard.py` is `M` in the tree but its diff is A1b's — mine was reverted — so it is not in the commit. The tree additionally carries **A1b's** uncommitted files — deviation 1 — and the commit command below excludes them. |

**Gate 4 in full.** Every `tx-` string left in the thirteen files:

| File | Remaining `tx-` strings |
|---|---|
| `_composer_head.html` | `#tx-composer-host` (the back button's target), `tx-ticket-title` (prose) |
| `_commitment_composer.html` | `tx-commit-form` and the six field ids (`tx-commit-amount`, `-vintage`, `-date`, `-case`, `-source`, `-note`), `#tx-composer-host`, `#tx-derived` |
| `_commitment_derived.html` | `id="tx-derived"`, `tx-commit-form` (prose) |
| `_commitment_outcome.html` | `id="tx-commit-outcome"`, `#tx-composer-host` |
| `_secondary_buy_composer.html` | `tx-secbuy-form` and its eight field ids, `#tx-composer-host`, `#tx-derived` |
| `_secondary_buy_derived.html` | `id="tx-derived"`, `tx-secbuy-form` (prose) |
| `_secondary_buy_outcome.html` | `id="tx-secbuy-outcome"`, `#tx-composer-host` |
| `_secondary_sale_composer.html` | `tx-secsell-form` and its eight field ids, `#tx-composer-host`, `#tx-derived` |
| `_secondary_sale_context.html` | `id="tx-secsell-context"`, `#tx-derived` (prose) |
| `_secondary_sale_scope.html` | `id="tx-secsell-scope"`, `tx-secsell-frac-full`, `tx-secsell-frac-partial` |
| `_secondary_sale_derived.html` | `id="tx-derived"`, `tx-secsell-form` (prose) |
| `_secondary_sale_outcome.html` | `id="tx-secsell-outcome"`, `#tx-composer-host` |
| `_master_data_fields.html` | the five field ids (`tx-md-name`, `-type`, `-asset-class`, `-currency`, `-anlv`), `tx-grid` (prose, about the wizard) |

The two state classes that survive are **not** `tx-`-prefixed: `is-refused` on
`_secondary_sale_scope.html` and `is-missing` on `_master_data_fields.html`,
both re-homed onto the `pf-` families in `transactions.css`.

---

## §7 — Tests

**Moved**, eleven pins across three modules, each traced to the template that
writes it before it was touched:

| Module | Pins moved |
|---|---|
| `test_transactions_commitment.py` | `_actions()`'s `<div class="tx-actions">` → `pf-actionbar`; `_HINT_OPEN` → `pf-actionbar__hint`; one docstring mention of `tx-nocash` → "the info note" |
| `test_transactions_secondary_buy.py` | the same two helpers, plus three derived-row assertions from `<span>…</span> <span>…</span>` to `<dt>…</dt> <dd>…</dd>` (cash out, acquired NAV, and the MD-20 pair in both the discount and the premium test) |
| `test_transactions_secondary_sale.py` | `_actions()`'s selector; `"tx-derived__row--info"` → `"<dt>vs. last reported NAV</dt>"`; `"tx-msg--consequence"` → `"pf-note pf-note--info"` |
| `test_transactions_wizard.py` | **none, in the end.** See below. |

**One pin was moved and then reverted.** `test_transactions_wizard.py:553`
asserts `name="currency" class="tx-mono" maxlength="3"`, and the shape of that
string made it look like `_master_data_fields.html`'s currency field. It is not:
it is `_wizard_identify.html`'s own `#tx-currency` field, on the Identify step,
which this prompt does not touch. The pin is back as it was and the module is
green untouched — the only test-file change in the wizard module is none at all.

**Added**, ten tests across the three modules, each naming its rule:

| Test | Rule |
|---|---|
| `test_the_commitment_composer_renames_the_section_head_into_a_crumb` | §2.1.6, through `_composer_head.html` |
| `test_the_commitment_composer_carries_exactly_one_primary` | R1 (§2.3.2) |
| `test_the_commitment_derived_region_is_a_summary_rail` | §2.5.3 + MD-19 — one block, no settlement, no `pf-choice` |
| `test_the_secondary_buy_composer_renames_the_section_head_into_a_crumb` | §2.1.6 |
| `test_the_secondary_buy_composer_carries_exactly_one_primary` | R1 |
| `test_the_secondary_buy_derived_region_is_a_summary_rail_with_settlement` | §2.5.3 — three blocks in order, A1b flag 5 discharged |
| `test_the_secondary_sale_composer_renames_the_section_head_into_a_crumb` | §2.1.6 |
| `test_the_secondary_sale_composer_carries_exactly_one_primary` | R1 |
| `test_the_secondary_sale_derived_region_is_a_summary_rail_with_settlement` | §2.5.3, plus the facts strip staying ahead of the rail in the form |
| `test_the_scope_is_two_choices_and_the_consequence_one_note` | MD-18 (two `pf-choice`, `tx-fraction` gone) and MD-17 (`pf-note--info`, and **no `<input>`** inside it) |

`test_transactions_composer.py`'s seven A1b head tests are green untouched and
now exercise the shared partial, which is the check §7 asked for.

One assertion of my own needed correcting on first run: the scope count read
`scope.count('class="pf-choice')`, which matches `pf-choice__figure` too and
returned 4. It now counts `'<label class="pf-choice'`.

---

## Deliberate deviations

1. **A1b is transcribed from the working tree, not from a commit.** Check 1
   expected A1b at the tip and a clean tree; the tip is A1a and A1b's whole
   diff is uncommitted, its report untracked. Every shape this prompt copies —
   `pf-form`, `pf-rail-sum`, `pf-actionbar`, `pf-note`, `pf-choice`,
   `_composer_head.html`'s source pair, the five `pf_components.css`
   transcriptions — is present and was read from the tree, so the transcription
   is faithful. The cost is that the commit command below is the **second** of
   two: A1b's must land first, or `git log` will show A1b2 depending on shapes
   no commit introduced. Nothing was staged for either.
2. **The scope options' sub-lines are `pf-choice__figure`, not `pf-hint`.**
   §2.3 asked for `pf-hint`. A bare `pf-hint` as a direct child of `.pf-choice`
   auto-places into the 16 px radio column, so it would have needed a new
   `grid-column: 2` rule — and §Scope says `transactions.css` is *retire only*.
   `pf-choice__figure` already **is** `grid-column: 2` and is the family's own
   second-row slot, the one `_settlement.html` puts its projection in. Cost:
   the family sets `white-space: nowrap` on that slot, so the longer of the two
   sentences (55 characters) cannot wrap. It fits the main column at every width
   §2.2.4 answers for, but it is a latent constraint and belongs in the record
   rather than in a scoped override here. §Flags 5.
3. **The commitment rail has one block, not the two §4 predicted.** R-COMMIT
   nets nothing — MD-19, no cash moves — so `_derived_context` hands this rail
   `effect_rows` and no amounts at all. There is no `gross`, `net` or formula to
   put in a `pf-sum`, and inventing an Amounts block would have meant inventing
   figures. Its `aria-label` says what the one block is ("What booking will
   emit") rather than borrowing "Amounts and settlement" from the other two.
4. **The rails' lead sentence stays visible, not behind `pf-more`.** §4 asked
   for the order rail's treatment, where "Ledger effect — two legs, booked
   together or not at all." sits in a `<details><summary>Two legs</summary>`.
   The two are not the same object: the order rail's sentence *explains* a pair
   already on screen and follows it, while M-3's "Emitted together, or not at
   all:" is a **lead-in** and precedes the rows. Hiding it would leave the rows
   unintroduced, and a `<summary>` for it would have to be copy MD-9 does not
   contain — I wrote "Two rows" / "Four rows" on the first pass and reverted
   them for exactly that reason. It is a `pf-hint` above the rows, which is what
   `tx-legs__lead` was.
5. **MD-17's consequence carries no `pf-choice`.** §5 asked for "its control as
   `pf-choice` — the shape `_order_outcome.html` gives U-SELL's". R-SEC-SELL has
   no control to give: MD-17 makes the sale a full disposal *by definition*, the
   server fixes `set_inactive` to False, and `_secondary_sale_outcome.html`'s
   own docstring says the absence "is the whole point". U-SELL's checkbox offers
   a choice; this note states a fact. It is `pf-note--info` + `pf-note__lead` +
   `pf-note__sub`, and a test asserts there is no `<input>` inside it.
6. **MD-9 copy is verbatim even where it is now spatially wrong.** The
   consequence still reads "shown **above** as emission rows", but the rows
   moved to the rail beside it. Rewriting binding mockup copy is a mockup
   decision, not a transcription one — A1b registered its copy gaps rather than
   closing them, and this is registered the same way. §Flags 6.
7. **`docs/ux/atlas-scenes.json` is not edited.** §6 made the edit conditional
   on check 7, and check 7 found only ids in the three composer scenes. The one
   `tx-` class in the file, `.tx-stepper .tx-step.is-active`, belongs to
   `wizard-step-1` and is A1c's to move.
8. **The commitment and secondary-buy AnlV blocks stay each flow's own.** §5's
   "remove any duplicate rendering here" was checked and found not to apply:
   `_messages.html` has no `missing_anlv` branch — the identifier fires at
   Propose, one layer down — so these blocks are the surfaces' live answer while
   the form is being filled, and each mockup words it for its own surface. They
   took `_messages.html`'s shape, not its place.

---

## §Flags

1. **The wizard's Classify step draws the new fields on the old grid.**
   `_wizard_classify.html` includes `_master_data_fields.html` inside its own
   `tx-grid` (`repeat(auto-fit, minmax(190px, 1fr))`), so the six `pf-field`
   spans count against an indefinite track count. Expect wider fields; every
   wizard test passes. **A1c owns it**, and it is the second half of A1b's
   flag 4.
2. **Three rules in `transactions.css` are orphans A1b left, not this
   prompt's.** `.tx-settle.is-unconfirmed`, `.tx-settle__option.is-selected`
   and `.tx-inline-create .tx-grid` — compound selectors whose base families
   A1b retired, so its sweep did not catch them. No user anywhere in `web/` or
   `tests/`. Left in place, because §6 says retire what *this* prompt orphaned;
   they belong in whichever strand next touches the sheet, beside A1b's own
   flag 6 (`.tx-detail-row`).
3. **`_order_composer.html`'s docstring still describes the head it no longer
   holds.** Two paragraphs — the crumb and the MD-2 stamp — now document
   `_composer_head.html`. §1 says the file "shrinks by the lines the include
   replaces and nothing else in it moves", so nothing else moved. A one-line
   pointer would close it; it is a deliberate non-edit, not an oversight.
4. **R-SEC-SELL has no atlas scene.** `atlas-scenes.json` defines
   `composer-order`, `composer-commitment`, `composer-secondary` (which clicks
   "Buy a stake (secondary)", i.e. R-SEC-BUY) and `wizard-step-1`. The sale's
   rail, its scope block and its consequence note are therefore never
   photographed, and this prompt's before/after images cover two of the three
   flows it changed. Adding a fifth scene is a scene-file edit §6 did not
   authorise. **Worth doing in A1e or at the A-1 end.**
5. **`pf-choice__figure` sets `white-space: nowrap`.** Deviation 2's cost. The
   record uses that slot for a projected balance, which should not wrap; the
   scope block uses it for a sentence, which should. One of the two wants a
   wrapping variant, and that is a question for the record, not for this Area.
6. **One MD-9 copy gap registered**, per deviation 6: the consequence's "shown
   above" now points sideways. Add it to the walk's copy list beside the
   Provenance blocks the three M-3 forms already carry against their mockups.
7. **The A1b report's hand-off names a file that does not exist.** It lists
   "the facts strips (`_secondary_sale_context.html`,
   `_secondary_buy_context.html`)"; there is no `_secondary_buy_context.html` in
   the tree and never was — R-SEC-BUY has no picker, so it has no instrument to
   state facts about. One facts strip moved here, not two.
8. **Two DB-backed pytest processes must not run at once.** Gate 1's first run
   came back with four `303 == 200` failures on `/transactions` — sessions lost
   because a second, concurrent run truncated the schema under it. Every one of
   those four passed in serial runs before and after. The gate below is a clean,
   sole-occupant re-run. This is the concurrency hazard the migration-guard note
   already records, met from the other direction.

---

## For P-UX-A1c — what the wizard inherits from this commit

`_master_data_fields.html`'s new `pf-field` / `pf-label` / `pf-select` /
`pf-hint` markup renders in the Classify step today, and `_order_derived.html`
already renders as a rail in the Order step (A1b's flag 4) — both inside
`_wizard.html`'s `tx-ticket` frame with no `.pf-form` around them, so both lay
out as full-width blocks until A1c gives the wizard `pf-form` and `pf-stepper`
together. Nothing is broken; both are waiting for their container.

---

```bash
git add \
  web/templates/_partials/transactions/_composer_head.html \
  web/templates/_partials/transactions/_order_composer.html \
  web/templates/_partials/transactions/_commitment_composer.html \
  web/templates/_partials/transactions/_commitment_derived.html \
  web/templates/_partials/transactions/_commitment_outcome.html \
  web/templates/_partials/transactions/_secondary_buy_composer.html \
  web/templates/_partials/transactions/_secondary_buy_derived.html \
  web/templates/_partials/transactions/_secondary_buy_outcome.html \
  web/templates/_partials/transactions/_secondary_sale_composer.html \
  web/templates/_partials/transactions/_secondary_sale_context.html \
  web/templates/_partials/transactions/_secondary_sale_scope.html \
  web/templates/_partials/transactions/_secondary_sale_derived.html \
  web/templates/_partials/transactions/_secondary_sale_outcome.html \
  web/templates/_partials/transactions/_master_data_fields.html \
  web/static/css/components/transactions.css \
  tests/web/test_transactions_commitment.py \
  tests/web/test_transactions_secondary_buy.py \
  tests/web/test_transactions_secondary_sale.py \
  docs/ux/inventory/elements.csv \
  docs/ux/inventory/routes.csv \
  docs/ux/inventory/summary.md \
  docs/reports/P-UX-A1b2-report.md

git commit -m "feat(transactions): commitment, secondary-buy and secondary-sale composers on the form vocabulary — one shared composer head, three pf-rail-sum rails (settlement inside the two secondaries), pf-actionbar with one primary each, scope as pf-choice, master-data fields on pf-field (UX A-1, P-UX-A1b2)

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```
