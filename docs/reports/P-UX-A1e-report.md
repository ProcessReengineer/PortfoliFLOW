# P-UX-A1e — Slot panels and the History detail onto the vocabulary; Details closes

**Strand:** UX A-1 (Transactions) · **Runs after:** P-UX-A1d
**Tip at session start:** `c23d910` — *"feat(transactions): number entry per
R10 … (UX A-1, **P-UX-A1d**)"*. Tree clean.
**Before image:** the latest atlas run's blotter/history bands. The slot
panels were not photographed; §6 adds a scene that opens one, so the *next*
run has them.

**Scope as executed:** the six templates
(`_cancel_panel`, `_reverse_panel`, `_history_detail`, `_effect_leg`,
`_reversal_report`, `_wizard_identify`); `web/static/js/shirley.js` (one
guard); `web/static/css/components/transactions.css`;
`docs/ux/atlas-scenes.json` (one scene); five test modules; the regenerated
inventory; this report. **Plus two one-line docstring corrections** that this
change made false — see *Deliberate deviations* 6.
**Not touched:** `_impact_panel.html`, `_negative_cash.html`,
`investments/detail.html`, `pf_components.css`, `ui-standards.md`, any other
Area.

---

## OPERATOR ACTION REQUIRED

**1. The commit is yours.** Nothing is staged. The command is at the end.

**2. `portfoliflow bootstrap`, then make two tickets** — one **booked** and
one **proposed**. The gates truncated the dev DB repeatedly, and both the
browser round and the new atlas scene need a terminal row in History.

**3. The browser round.** Five things:

- **Transactions → History → click a ticket number.** The detail opens as a
  `pf-panel`: four facts across the top, then the legs. Press **Close** at
  the foot. The panel goes *and the row's chevron un-rotates* — that is the
  whole point of A1a flag 2, and it happens with no script: the button
  empties the cell and the `:has()` writer reads the emptied cell.
- **Blotter → a proposed ticket's menu → Cancel ticket….** The panel asks
  the question, the reason field now carries a **label** rather than a
  placeholder, and the two buttons are an outlined red *Cancel ticket* and a
  quiet *Keep*. Press **Keep**: the panel goes, the ticket stands.
- **History → a booked row's menu → Reverse booking….** Same shape. (Leave
  it — or reverse it and read the report that heads the re-rendered list.)
- **`/super-admin/tenants` → Ctrl J.** *Nothing happens.* Before this commit
  the shortcut wrote `data-shirley="docked"` onto a shell with no column to
  widen. Check the shell does not shift.
- **Transactions → *Buy a new instrument*.** On arrival, the second card —
  *No public identifier?* — is **already open**. It can still be collapsed.

**4. Re-run the atlas** for the after-image. The new scene is
`transactions-history-detail-open`; if History is empty it records
`ok: false` rather than failing the run, so check that row.

---

## Verify-first

| # | Check | Result |
|---|---|---|
| 1 | tip · tree | `c23d910` (A1d) · clean ✓ |
| 2 | `wc -l` on the six | 61 / 52 / 58 / 66 / 43 / 160 — the first five as the prompt stated; `_wizard_identify` 160 post-A1c |
| 3 | `tx-btn` in the panels · tree | 2 / 2 · **7** ✓ |
| 4 | destructive button in the record | **`pf-btn--danger` exists** — `shared.css:384`, and `pf_components.css:249` has it transcribed. §1.3's fallback is moot and **Flag 1 is answered, not carried**. |
| 5 | `pf-panel`, the fact list, a "done" note | `pf-panel` + `__head`/`__titles`/`__title`/`__sub`/`__foot`/`__chart` all declared, 0 users; the fact list is **`pf-facts`** (`margin: 0 0 space-5`, the in-panel one) beside `pf-context` (`margin: space-4 0 0`, the after-content one); **no `pf-note--done`** — the record names `--info`/`--warn`/`--block` and nothing else |
| 6 | the Keep idiom | `hx-on:click="this.closest('td').innerHTML = ''"` on both panels ✓ |
| 7 | how A0s kept the rail off super-admin | a **template** predicate: `super_admin/base.html:25` overrides `{% block shirley %}` to empty, dropping `#pf-side` (rail + dock host) and the stage together. No CSS rule. |
| 8 | the disclosure | `_wizard_identify.html:112` `<details class="pf-more">` — one attribute |
| 9 | the A1a transition rule | present at 512, its comment naming this prompt |
| 10 | users of the families | after §1–§4 only `_impact_panel.html` — it keeps `tx-msg*`, `tx-msg__code`, `tx-btn*`/`--sm`/`--ghost` |

**Check 4 is the one that reshaped this prompt.** Unzipping
`~/DC-UX-D-bundle/DC-UX-D_mock-sources.zip` turned up more than a button:
`mock01.src.html` **draws two of these three panels outright** —
`cancelPanel()` at line 245 with *our copy verbatim*, and `detailPanel()` at
line 250. §1 and §2 were written as inference; the record had already
answered. Where the two differ the record won, and each difference is
registered below.

---

## §1 — The two reason panels

Both are now the record's `cancelPanel()`, one for one:

```
pf-panel
  pf-panel__head > pf-panel__titles > pf-panel__title  (the question)
                                    > pf-panel__sub    (the consequence)
  [pf-note--block                                      (a service refusal)]
  form > pf-grid > pf-field--half > pf-label + pf-textarea
       > pf-panel__foot > pf-btn--danger  (confirm)
                        > pf-btn--quiet   (Keep)
```

- **Ids, `hx-*` and the Keep idiom are unchanged.** The endpoints, targets
  and swaps are byte-identical; only the vocabulary moved.
- **The lead and sub are `pf-panel__title`/`__sub`, not a `pf-note--warn`.**
  §1.2 asked for a note; the record's own drawing of this panel with this
  copy does not use one. The panel *is* the question — an amber-railed note
  above a question the panel is already asking states it twice.
  §1.2 also asked which tone the reverse panel's lead takes: it states a
  **consequence**, not a refusal ("The rows listed above are deleted in one
  transaction and the ticket becomes cancelled"), so under the panel head it
  is `pf-panel__sub` and neither `--warn` nor `--block`.
- **The error branch is `pf-note--block`** with `pf_icon("block")` and the
  service sentence as `__lead` — the shape `_messages.html` gives every
  refusal (A-7). `pf_components.css:432` already declares
  `.pf-panel > .pf-note { margin-bottom }`, so the record anticipated a note
  inside a panel.
- **`tx-msg__code` → the reverse panel's `pf-note__sub`, mono by id.** The
  strand's settled treatment (P-UX-A1b2 for `#tx-md-currency`, P-UX-A1c for
  the three wizard id fields): the record names no `pf-mono`, so the stack
  rides on the id. The id carries no ticket — `cause` is set only on the
  refusal render and that render replaces `#tx-history` whole, so exactly one
  cause line can stand on a page. The reason *field* has no such guarantee
  (two rows can each hold an open panel) and keeps the ticket in its id.
- **The reason is a `pf-label`, not a placeholder.** The record's, and a
  placeholder never was a label. The copy is unchanged: *Reason (required)* /
  *Reason (optional)*, the optional half on `pf-optional` as every other
  optional field in this Area writes it.
- **Copy is verbatim** throughout — the em dashes, middots and quotes are the
  literal characters the register uses, not entities. (Three tests pin those
  bytes; the first run caught an entity slip, which is exactly what they are
  for.)

## §2 — History detail and its legs

`_history_detail.html` is the record's `detailPanel()`: `pf-panel`, the four
facts as **`pf-facts`** (the `<dl>` keeps its `<div><dt><dd>` pairs), then the
legs.

- **The heading is `pf-block__title`.** The record borrows
  `pf-rail-sum__title` here; `_order_confirmation.html` heads the *identical*
  list with `pf-block__title`, and the two surfaces `_effect_leg.html` exists
  to keep identical should not head their one list two ways. Registered as
  deviation 1.
- **The group headings are `pf-hint` lines.** §2.1 asked which: the record's
  leg list carries **no** group headings, so there is no family to take and
  the `pf-hint` weight stands. The never-booked sentence is a `pf-hint` too —
  the P-UX-A1b2 deviation-4 precedent for a visible sentence.
- **The dashed box is gone.** `.tx-legs` framed the legs inside what is now a
  `pf-panel`; §2.2.7's "no card inside a card" makes a second frame one too
  many.
- **`_effect_leg.html` is `pf-leg`** — `pf-leg__type`, a bare middle `<span>`
  with its `<em>`, `pf-leg__amount` — drawn exactly as `_order_derived.html`
  draws a leg. The commitment line rides **inside** the middle span as a
  nested `pf-hint`; `pf-leg` declares no `__sub` and A1c2 deviation 4
  established that inventing one is not this strand's to do. The
  buy/sell/info **ink** on the type is lost, as it was on Confirm — one
  `pf-leg` question for the record, not two (Flag 2).
- **Details closes.** One `pf-btn--quiet` **Close** in `pf-panel__foot`,
  last in the panel, carrying the Keep idiom verbatim. The panel is still a
  projection (A-17): one button, no form, no CSRF. `is-open` clears itself —
  the `:has()` writer reads the emptied `td`.

## §3 — Reversal report

The wrapper is gone (`.pf-note + .pf-note` is the gap). Both messages are
`pf-note--info`: `pf_icon("done")` + `__lead` + two `__sub` lines for the
tally, and `pf_icon("info")` + `__lead` for the retained shell. Where it
renders (`_history.html`, heading the re-rendered list) is unchanged.

The gap the retired `.tx-messages--report` kept between the report and the
filter bar below it is re-homed onto the two shared families it now sits
between — `.pf-transactions .pf-note + .pf-filters`. `.pf-filters` carries a
bottom margin and no top one, so without it the last note butts the bar.

## §4 — Identify open, the sheet

**1.** `<details class="pf-more" open>`. The docstring's deviation 1 gains a
paragraph saying why: M-2 draws the second card visible, and a disclosure
that starts closed would have quietly narrowed "both cards stand open" to
"the second path exists behind a click".

**2. The sheet: 949 → 822 lines.** Retired outright — `.tx-reason` and its
six (`--reverse`, `__lead`, `__sub`, `__form`, `textarea`, `__actions`);
`.tx-kv` and its three; `.tx-legs`, `.tx-legs__pending`, `.tx-legs__group`
and its `:first-child`; the whole `.tx-leg*` family (11 rules);
`.tx-messages`; `.tx-msg__sub`; `.tx-msg--done` and its glyph;
`.tx-messages--report`; `.tx-block__title`; `.tx-btn--danger` and its hover.

**Stayed, with a comment naming A1e2** — `.tx-msg`, `.tx-msg__glyph`,
`.tx-msg--block`, `.tx-msg--consequence`, `.tx-msg strong`, `.tx-msg__code`,
`.tx-btn`, `.tx-btn:hover`, `.tx-btn:disabled`, `.tx-btn--ghost`,
`.tx-btn--sm`: `_impact_panel.html` still draws every one of them. So did
`.tx-num` and `.tx-state`, which were already on that footing.

**Added** — one id joins the mono holdouts (`#tx-reverse-cause`, the fifth
and the first non-field); the `pf-note`→`pf-filters` gap above; and one
scoped rule for the leg's type track (deviation 3).

**The A1a slot-padding rule is narrowed, not retired** — deviation 2.

## §5 — Ctrl J off the surface without a rail

One guard, in `shirley.js`'s `toggle()`, before the state write:

```js
if (!document.querySelector(".pf-side .pf-rail")) {
    return;
}
```

`toggle()` rather than `section_nav.js`'s handler, per the prompt's
preference: it covers every caller of the public API, not only the hotkey.
`.pf-side .pf-rail` rather than the bare class — `cases_detail.html` uses
`pf-rail` for a case's own aside, so the bare selector would have found one
on `/cases/{id}` and meant nothing there.

Asserted two ways in `tests/web/test_super_admin_routes.py`: the existing
no-rail test now also pins that `class="pf-rail"` is absent on the
super-admin pages (the guard's predicate), and a new test reads `shirley.js`
and pins that the guard is in `toggle` and stands *before* `setState`. The
behaviour itself is a browser one; the browser round proves it.

## §6 — The slot scene

Expressible in a handful of lines, so it landed:

```json
{"name": "transactions-history-detail-open", "area": "transactions",
 "start": "/transactions", "session": "tenant", "section": "history",
 "steps": [{"wait": ".pf-table__row"},
           {"click": ".pf-table__id button"},
           {"wait": ".pf-table__slot .pf-panel"}],
 "shot": "history-detail-open"}
```

No seed step exists or was needed: the Cases scenes seed by *driving the UI*,
and a booked ticket cannot be driven up in three steps. This scene rides on
the tenant the operator bootstraps — hence step 2 of OPERATOR ACTION. A
failing step records `ok: false` with a reason rather than breaking the run
(`ux_atlas.py:1700`), so an empty History degrades to a recorded miss.

## §7 — Tests

| Module | What changed |
|---|---|
| `test_transactions_blotter.py` | the `tx-msg--block` pin → `pf-note--block`; **new** `test_cancel_panel_is_a_pf_panel_with_a_danger_confirm` — the six families, one danger, one quiet, zero primary, the Keep idiom, and none of the three retired families |
| `test_transactions_history.py` | the `tx-leg__type` pin → `pf-leg`; **new** `test_history_detail_is_a_pf_panel_that_closes` (panel, `pf-facts`, two `pf-leg` rows, exactly one `<button>`, quiet, the Keep idiom, `>Close<`) and **new** `test_reverse_panel_is_a_pf_panel_with_a_danger_confirm` |
| `test_transactions_secondary_sale.py` | the two `tx-leg__type` leg pins → `pf-leg__type` (the confirmation panel shares `_effect_leg.html`) |
| `test_transactions_wizard.py` | the disclosure pin gains `open`, with the reason in the docstring |
| `test_super_admin_routes.py` | the no-rail test gains the `pf-rail` pin; **new** `test_shirley_toggle_is_inert_without_a_rail` |

---

## Measurements

| Measure | Before | After |
|---|---:|---:|
| `tx-btn` in templates | 7 | **3** (all `_impact_panel.html`) |
| `transactions.css` | 949 | **822** (−127) |
| `tx-` class tokens in the six | 70 | **0** |
| `pf-panel` users | 0 | **3** |
| `pf-panel__foot` users | 0 | **3** |
| `pf-facts` users | 0 | **1** |
| `pf-leg` users | 5 | **6** |
| `pf-actionbar` users | 8 | **8** (deviation 4) |
| inventory rows | 1247 | 1248 |
| inventory placeholders | 44 | **42** (two became labels) |
| inventory form labels | 188 | **190** |

---

## Gates

| # | Gate | Result |
|---|---|---|
| 1 | tests | **Run narrowed at the operator's request** (see below) — all green |
| 2 | `ruff check` · `ruff format --check` | clean · 942 files formatted |
| 3 | `tx-btn` tree total | **3**, all in `_impact_panel.html` ✓ |
| 4 | `tx-` classes in the six | **0** ✓ |
| 5 | colour literals in `transactions.css` | **none** (two matches are prose in comments) ✓ |
| 6 | `python tools/ux_inventory.py` | re-run; `elements.csv` + `summary.md` updated ✓ |
| 7 | `git diff --name-only` | the six + two docstring corrections, one JS file, the sheet, the scenes file, five test modules, the inventory, this report — **not** `_impact_panel.html`, **not** `_negative_cash.html`, **not** `investments/detail.html` ✓ |

### Gate 1, as run

The operator asked mid-prompt to keep testing light, since a full-suite run
follows the next prompt. Run instead of the full gate selection:

| Selection | Result |
|---|---|
| `test_transactions_history.py` + `test_transactions_blotter.py` | **53 passed**, 1m50s |
| `test_transactions_secondary_sale.py` + `test_transactions_impact.py` | **35 passed**, 1m19s |
| `test_transactions_composer.py` + `test_transactions_wizard.py` + `test_pf_components.py` + `test_css_tokens.py` + `tests/tools` | 379 passed, 2 failed, 2 errors, 3m20s — see the note |
| `test_transactions_wizard.py` **alone** | **38 passed**, 1m21s |
| `test_super_admin_routes.py::test_super_admin_surface_carries_no_shirley_rail` + `::test_shirley_toggle_is_inert_without_a_rail` | **2 passed** |
| `tests/regression/{test_shirley_shell_element,test_section_catalogue_matches_body_partials,test_no_matplotlib_in_web}.py` | **20 passed**, 1.8s |

**The four non-passes are a module-isolation artefact, not this change.**
All four are `IntegrityError … users_tenant_id_fkey: Key
(tenant_id)=(…0001) is not present in table "tenants"` — a sibling module's
teardown removed the Sentinel tenant inside the shared process. The wizard
module run on its own is 38/38. The rest of `tests/regression` (the migration
round-trips) was **not** run; it is untouched by this change and the
next prompt's full-suite run covers it.

### Render check

Both through the test client. `GET /api/transactions/ticket/{id}/cancel` on a
**proposed** ticket:

```html
<div class="pf-panel">
    <div class="pf-panel__head">
        <div class="pf-panel__titles">
                <p class="pf-panel__title">Cancel ticket #1?</p>
                <p class="pf-panel__sub">
                    A proposed
                    ticket is a decision others may have seen. A reason is required.
                </p>
        </div>
    </div>
    <form>
        <input type="hidden" name="csrf_token" value="nN5Zwcaew0oNmTWSOKic2GyUPkd8jbf-VEsxMCQD-YE">
        <div class="pf-grid">
            <div class="pf-field pf-field--half">
                <label class="pf-label" for="tx-cancel-reason-3e826e05-d1b9-4bc6-833a-da5bc8fb20ed">
                    Reason (required)
                </label>
                <textarea class="pf-textarea" id="tx-cancel-reason-3e826e05-d1b9-4bc6-833a-da5bc8fb20ed"
                          name="reason" rows="2"
                          required></textarea>
            </div>
        </div>
        <div class="pf-panel__foot">
            <button class="pf-btn pf-btn--danger" type="button"
                    hx-post="/api/transactions/ticket/3e826e05-d1b9-4bc6-833a-da5bc8fb20ed/cancel"
                    hx-include="closest form"
                    hx-target="#tx-blotter"
                    hx-swap="outerHTML">Cancel ticket</button>
            <button class="pf-btn pf-btn--quiet" type="button"
                    hx-on:click="this.closest('td').innerHTML = ''">Keep</button>
        </div>
    </form>
</div>
```

`GET /api/transactions/history/{id}` on a **booked** order:

```html
<div class="pf-panel">
    <dl class="pf-facts">
        <div>
            <dt>Stations</dt>
            <dd>created 2026-03-02 09:00 · proposed by A. Weber 2026-03-02 09:00 · approved by A. Weber 2026-03-02 09:00 · booked by A. Weber 2026-03-02 09:00</dd>
        </div>
        <div>
            <dt>Settled against</dt>
            <dd>Cash EUR · Main custody</dd>
        </div>
        <div>
            <dt>Note</dt>
            <dd>quarterly rebalance</dd>
        </div>
        <div>
            <dt>Provenance</dt>
            <dd>ticket #1</dd>
        </div>
    </dl>

    <p class="pf-block__title">What this booking wrote</p>
            <p class="pf-hint">position transactions</p>
<div class="pf-leg">
        <span class="pf-leg__type">buy</span>
        <span>
            iShares Core MSCI World
            <em>@ 10.0000</em>
        </span>
        <span class="pf-leg__amount">+10.0000 units</span>
</div>
<div class="pf-leg">
        <span class="pf-leg__type">sell</span>
        <span>
            Cash EUR · Main custody
            <em>@ 1.0000</em>
        </span>
        <span class="pf-leg__amount">−100.0000 units</span>
</div>

    <div class="pf-panel__foot">
        <button class="pf-btn pf-btn--quiet" type="button"
                hx-on:click="this.closest('td').innerHTML = ''">Close</button>
    </div>
</div>
```

(Jinja whitespace trimmed for the paste; the bytes are otherwise as served.)

---

## Deliberate deviations

**1. The lead/sub, the action row and the button order follow the record,
not §1.2/§1.3.** §1 prescribed `pf-note--warn` for the lead, `pf-actionbar`
for the actions and Keep in the leading slot (R3). The record's
`cancelPanel()` draws `pf-panel__title`/`__sub`, `pf-panel__foot`, and
danger-then-Keep. The record won on three grounds: it draws *this* panel with
*this* copy, so nothing is inferred; `pf-actionbar` is §2.3.4's **form
outcome** bar — sticky, hinted, `background: --ui-background-primary` — and
inside a `pf-panel` on `--ui-background-secondary` it paints a mismatched
strip that sticks to the viewport bottom while the table scrolls behind it;
and R3's leading slot is that bar's rule, not `pf-panel__foot`'s, which has
no hint spacer to lead. The same reasoning gives the History detail's Close a
`pf-panel__foot` rather than the `pf-actionbar` §2.3 asked for, so all three
panels close the same way. `pf-actionbar` users therefore stay at 8, not 11.

**2. The History detail's heading is `pf-block__title`, not the record's
`pf-rail-sum__title`.** The record borrows the rail's title class for a small
block title inside a panel. `_order_confirmation.html` — the other user of
`_effect_leg.html`, and a list of the same rows — heads it with
`pf-block__title`, and an element class borrowed from a family the element is
not inside is the kind of thing this strand has otherwise refused (A1c2
deviation 4). One question for the record: *what heads a block inside a
`pf-panel`?*

**3. The A1a slot-padding rule is narrowed, not retired.** §4.2 said retire
it. Three of the four slot occupants are `pf-panel` now and bring the same
padding and ground, so the general rule had to go — kept, it would double the
padding. But `_impact_panel.html` is A1e2's, still draws a `tx-impact` box,
and `.pf-table__slot > td { padding: 0 }` would have left it flush against
the table edges. The declarations are A1a's unchanged behind a
`:has(> .tx-impact)`, so that panel is pixel-identical across this commit and
the rule dies with it in A1e2. Retiring it outright would have regressed a
surface this prompt's scope excludes.

**4. One scoped rule widens `pf-leg`'s type track on the two effect-leg
surfaces.** `pf-leg` is drawn `34px` in the first track, which is what `buy`
and `sell` need — the four M-3 rails and the Confirm step say nothing longer
and keep the record's width untouched. `_effect_leg.html` names rows by what
they are, so its vocabulary runs to `distribution`, `investor_flow` and, on
the missing-row branch, `investment_update`; in a 34px track those overrun
the name beside them. Each `pf-leg` is its own grid, so a content-sized track
would give every line a different one — the column has to be fixed to stay a
column. `124px`, scoped to `.pf-panel` and `.pf-form__main`. This is the
sheet's sanctioned exception (a shared family in a state the record does not
name) and it travels with Flag 2.

**5. The reason field is `pf-field--half`, not `pf-field--full`.** §1.3 said
`--full`. The record caps the field at 560px with an inline style, which the
strand drops along with the mocks' other inline styles; `--half` is three of
`pf-grid`'s six tracks and is the record's own nearest declared span, so the
cap rides on the grid rather than on a rule of this Area's. Full width would
have put a two-row textarea across the whole table.

**6. Two files outside gate 7's list were touched, one line each.** Both were
made false by this change and both describe the files it changed:
`_order_confirmation.html`'s docstring said "the legs stay `_effect_leg.html`'s
`tx-leg` rows", and `_history.html`'s said "the reversal report keeps its own
`tx-` classes … the panels migrate together in A1e". Shipping either as
written would have left a docstring asserting the opposite of the code. Six
lines total, no markup changed in either.

---

## Flags (carried)

**1. ~~Destructive confirmation button~~ — answered.** The record has
`pf-btn--danger` (`shared.css:384`), transcribed at `pf_components.css:249`
and declared in `ui-standards.md` §2.3.6 as R7's implementation. Both panels
use it; the `aria-describedby` fallback §1.3 described is not needed.

**2. `pf-leg` was drawn for `buy` and `sell`.** Two halves, one question.
*Ink:* `pf-leg__type` is one tertiary colour, so Confirm (A1c2) and now the
History detail and the confirmation panel state a buy and a sell in the same
grey. *Width:* the 34px track does not hold this Area's effect vocabulary,
and deviation 4 is a scoped stand-in. The record should say whether
`pf-leg__type` takes a tone and how wide it may be.

**3. `pf-note--done`.** The reversal report's completion has no tone of its
own; `--info` stands in and `pf_icon("done")` carries the meaning. With
`.tx-msg--done` retired, `--ui-semantic-success` now has **no user in this
Area** — worth the record knowing before the token drifts.

**4. `ui-standards.md` §2.5.7 says `pf-kv`.** The declared family is
`pf-facts` (and the inventory at line 241 lists it correctly under
`pf-rail-sum`). A doc-only mismatch, A1s's to fix; this prompt did not touch
`ui-standards.md`.

**5. The family inventory's "users" column is stale.** `pf-panel`, `pf-facts`
and `pf-note` are all listed at 0 users and now have 3, 1 and many. A1s.

---

## For A1e2

The last `tx-` surfaces in this Area, in one line: **the impact panel's shape**
— `tx-delta` before/after pairs with `--ok`/`--warn`/`--breach` badges, three
`tx-lens` columns and a `tx-impact__basis` fact list; the record draws it
(`mock01.src.html` `impactPanel()`) as `pf-panel` + `pf-panel__head` with an
**icon Close in the head**, `pf-facts` for the basis, `pf-lenses` /
`pf-lens__title` / `pf-delta` / `pf-delta__before` / `__arrow` / `__after` /
`__flag` — a family `pf_components.css` does **not** yet carry, so A1e2 either
transcribes it or the projection table has no family; **the negative-cash
indicator** (`tx-indicator*`), shared with `investments/detail.html`, so it
moves both surfaces or neither; and **`tx-num--neg` → `pf-neg`** (ADR-0062 §2)
— `tx-num` is the mono stack that the mono-holdout ids have otherwise
replaced, and `--neg` is a semantic the record may already name.
