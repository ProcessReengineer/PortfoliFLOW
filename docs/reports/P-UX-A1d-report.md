# P-UX-A1d — Number entry (R10): one parser, one seam, 18 inputs, "Read as …"

**Strand:** UX A-1 (Transactions) · **Runs after:** P-UX-A1c2
**Tip at session start:** `712bef9` — *"feat(transactions): wizard steps Order
and Confirm on the form vocabulary … (UX A-1, **P-UX-A1c2**)"*.
**Before image:** none. This prompt changes behaviour, not pixels; the one
visual addition is a line of secondary-size text under a field.

**Scope as executed:** new `core/decimal_input.py`; the parsing seam and the
readings in `web/routes/transactions.py`; the six templates that carry the
inputs (`_order_composer`, `_wizard_order`, `_secondary_buy_composer`,
`_secondary_sale_composer`, `_commitment_composer`, `_settlement`); the five
recalc partials plus the new `_readings.html`; new
`tests/core/test_decimal_input.py`; three route-test modules and
`tests/web/test_pf_components.py`; the regenerated inventory; this report.
**Not touched:** the 20 `type="number"` outside Transactions, any CSS
(`pf-read` was already declared and had no user), the four S5 panels,
`ui-standards.md`, `_effect_leg.html`, `_messages.html`, any other Area.

---

## OPERATOR ACTION REQUIRED

**1. The commit is yours.** Nothing is staged. The command is at the end.

**2. `portfoliflow bootstrap` before anything visual.** The gates below
truncated the dev DB repeatedly.

**3. The browser round — Transactions → *Buy or sell units*.** Four things,
in this order:

- type `1.234,56` into **Units**. A line appears under it: *Read as
  **1,234.5600***. The rail's **Gross** follows on the same keystroke — before
  this prompt that body derived nothing at all and said nothing about why;
- type `1,234` into **Fees**. It reads as *1.23* — **one thousand two hundred
  and thirty-four is not what it says**. This is decision 2 made visible, and
  the reason the echo exists: a lone separator is the decimal separator, and
  the surface tells you so rather than guessing by digit count behind your
  back;
- delete the comma. The line under the field **clears** — the slot is
  re-rendered empty, not left standing;
- type `abc` into **Taxes**: *not read as a number*. The endpoint still
  refuses nothing (D-2) — the rail simply derives less, as it always did, and
  now says why.

Then the same on *Sell a stake (secondary)* — its one **Sale proceeds** field
behaves identically — and on the wizard's step 3.

**4. Where the caret goes.** The inputs are `type="text"` now, so the browser
no longer offers spinner arrows and no longer rejects a keystroke by locale.
Confirm that typing through a full number never loses the caret: the recalc
re-renders the reading *beside* the field and never the field itself.

---

## Verify-first

Under `source .venv/bin/activate`.

| # | Check | Result |
|---|---|---|
| 1 | tip / clean | `712bef9` (A1c2) at the tip. ✔ — **but the tree was not clean**: `M docs/reports/P-UX-A1c2-report.md` was already modified at session start. Left exactly as found and *not* staged by this prompt. |
| 2 | `type="number"` census | **18** under `_partials/transactions/` — `_secondary_sale_composer` 3, `_secondary_buy_composer` 4, `_order_composer` 4, `_settlement` 1, `_wizard_order` 4, `_commitment_composer` 2 — and **20** elsewhere (saa ×4, planning_desk ×4, watch_desk ×3, portfolio_analysis ×2, investments ×7). Tree total **38**. ✔ |
| 3 | the seam at `transactions.py:766` | `_decimal_or_none` was `Decimal(text)` inside a `try`, `_int_or_none` beside it. ✖ on the count: `grep -c '_decimal_or_none('` is **11**, not the stated 14 — one `def` plus **10** call sites (five in the order/settlement block, five in the P-4b block). `_int_or_none` has one. The stated 14 does not match the file; 11 is what is there. |
| 4 | the seam is one file's | Nothing outside `web/routes/transactions.py` names either helper. ✔ |
| 5 | the Blotter's amount formatter | `row.amount` is **pre-formatted in the route**, not filtered in Jinja: `_amount_of` → `_money` (`transactions.py:1267`), `f"{…:,.2f}"` with the typographic minus. Its sibling `_units` (`:1280`) does the same at four decimals and is what the rail already uses for a quantity and a price (`formula`, `reference_price`). Decision 6 resolved — see Deliberate deviations 1. |
| 6 | `pf-read` | Declared at `pf_components.css:343-344` (`.pf-read` and `.pf-read b`), **0 users**. ✔ No CSS change needed. |
| 7 | how `oob` reaches the partials | Not through `_derived_context` — it is a key of the **outer render context** that `post_recalc` builds beside `entered`, `flow`, `step` and `**derived`. The readings ride exactly there. ✔ |
| 8 | tests that post numbers | `grep … \| grep -c 'data='` = **3** in `test_transactions_*.py`; the real posting surface is the per-module `_recalc_form` / `_order_form` / `_form` helpers, whose default bodies carry `104.10`, `42.18`, `1850000`. All left untouched and green. ✔ |
| 9 | `ls core/ \| grep -c decimal` | **0** — the module is new. ✔ |

---

## §Decisions

The prompt's six, as implemented, plus two that had to be added.

1. **Both separators present → the last one is the decimal separator.**
   `1.234,56` → 1234.56, `1,234.56` → 1234.56. Implemented by comparing
   `rfind`: the decimal separator must then appear exactly **once**, and no
   grouping glyph may stand behind it.
2. **One separator, once → it is the decimal separator.** `1,234` → 1.234 and
   `1.234` → 1.234. Never grouping. The echo is what makes this safe.
3. **One separator, repeated → grouping.** `1.234.567` → 1234567. Mixed order
   (`1,234.56,7`) → not a number.
4. **Spaces, NBSP, thin and narrow space and `'` are grouping and stripped.**
   A leading `-` or `+` is a sign; a trailing separator (`12,`) reads as the
   integer; anything else is `None`. The permissive contract stands — the
   endpoint refuses nothing.
5. **A year is not an amount.** Both `md_vintage_year` inputs are
   `inputmode="numeric"` and keep `_int_or_none`: no reading, no grouping
   rule, no slot.
6. **The echo renders in the product's own notation**, through the formatters
   the route already has, and only where the text was *interpreted*.

**Added — 7. Grouping must be well formed.** Decision 3 alone would read
`12,34,56` as 123456, but §5 requires it to be `None`. So a repeated separator
is grouping **only** in runs of 1-3 digits then 3s. `1.234.567` ✔,
`12,34,56` ✖. This also governs the integer part of a mixed-notation value:
`1.23,45` is not a number.

**Added — 8. "Not a number" is broader than "digits but no reading".**
Decision 6 words the second case as *digits* present with no reading, but §5
asks `units=abc` to say *not read as a number*, and `abc` has no digits. The
implemented rule is the useful one: **any non-blank text that yields no
number** says so. A blank field still says nothing. `was_interpreted` itself
is exactly as §1 specifies and is not what carries this case.

---

## §1 — `core/decimal_input.py`

99 lines. Two public functions, no helper beyond them, no I/O, no `locale`,
no `float`, and **no project import at all** — which is the point of putting
it in `core/`: A-2, A-4 and A-6 delegate to it when they sweep their own
fields, and `core/` imports nothing, so nothing can grow a dependency through
it. `core/__init__.py` untouched.

`parse_decimal(raw: str | None) -> Decimal | None` implements decisions 1-4
and 7. The rules are stated in the module docstring as a table of ten
examples, and every row of that table is a case in the test module.

`was_interpreted(raw: str | None) -> bool` is decision 6's test: any of
`, . ' + -` or a grouping space present after `strip()`.

The value is built as a string and handed to `Decimal` once — `Decimal(f"…")`
on the cleaned digits — so nothing is rounded on the way in and no binary
float ever touches it. Guarded by an explicit character-set check rather than
by catching `InvalidOperation`, which is why `1e3` is `None` rather than 1000:
R10 is about two human notations, not about Python's constructor.

**Over the stated budget.** §1 asks for ≤ 80 lines including the docstring;
this is 99. See Deliberate deviations 2.

---

## §2 — The seam (`web/routes/transactions.py`)

`_decimal_or_none` is now one line:

```python
    return parse_decimal(raw)
```

Its docstring keeps the D-2 paragraph verbatim and gains one sentence naming
the module, R10 and `_reading` as the rule's other half. `_int_or_none` is
untouched, and so are all ten call sites. `InvalidOperation` left the import
line with the `try` block it guarded.

**`_ComposerForm.readings`** is built in `__init__`, immediately after
`self.entered` and off the same raw strings:

```python
        self.readings: dict[str, str] = {
            name: reading
            for name in _AMOUNT_FIELDS
            if (reading := _reading(name, self.entered[name])) is not None
        }
```

`_AMOUNT_FIELDS` is the decimal half of the form's inventory, all ten names
including `md_purchase_price` (which has no control anywhere — D-U makes it a
mirror of `gross_amount` — and is listed so the reading is built from the
whole contract rather than from the fields that happen to have an input).

`_reading(name, raw)` sits in the **Presentation helpers** block beside the
formatters it picks from:

```python
    value = parse_decimal(raw)
    if value is None:
        return "" if raw.strip() else None
    return _READING_FORMAT.get(name, _money)(value) if was_interpreted(raw) else None
```

Three answers, not two. `None` is *say nothing* — the common case, and what
keeps a sentence from appearing under every field on every keystroke. The
empty string is *this text carries no number*, which the slot renders as its
own sentence. Anything else is the formatted value. `_READING_FORMAT` maps
`units` and `price_per_unit` to `_units` and everything else to `_money`.

**How it reaches the templates.** Exactly the way `entered` and `oob` do — a
key of the outer render context, in all three builders:

- `_composer_context` → `"readings": form.readings` (line 2541)
- `_wizard_context` → `"readings": form.readings` (line 2819)
- `post_recalc` → `"readings": form.readings` **and**
  `"readings_fields": spec.readings_fields`

`_derived_context` is unchanged. It is the wrong home for this: it knows the
ticket *kind*, not the *flow*, and the order composer and the wizard share one
kind while being two flows. The flow table already exists and already says
which partials draw each flow, so the slot list is a sixth field on it:

```python
    readings_fields: tuple[str, ...]
```

`""` and `new_instrument` → `("units", "price_per_unit", "fees", "taxes")`;
`secondary_sale` → `("gross_amount", "fees", "taxes")`; `secondary_buy` →
`("gross_amount", "md_acquired_nav", "md_assumed_unfunded")`; `commitment` →
`("commitment_amount",)`. Fifteen slots; `cash_opening_balance` is the
sixteenth and is deliberately on none of the lists — §3.

---

## §3 — The inputs (six templates)

All 16 amount inputs: `type="number" … step="any"` → `type="text"
inputmode="decimal" autocomplete="off"`. `pf-input pf-input--num` kept, `id`,
`name` and `value` untouched. Both `md_vintage_year` inputs →
`type="text" inputmode="numeric"`, no slot.

Each amount input is followed, inside the same `pf-field`, by

```html
<span class="pf-read" id="tx-read-units"></span>
```

— keyed on the input's `name`, not on its `id`, so the route can address a
slot without knowing which template drew it. The ids are unique per rendered
composer because **only one composer is ever in `#tx-composer-host`**: the
chooser replaces the host wholesale, and the wizard's step 3 and the order
composer — which give their inputs the same `tx-units` / `tx-price` ids —
therefore never co-exist in one document.

The static slots render **empty**, and stay empty on a gesture re-render even
where the form carries values. That is deliberate and not an oversight: the
echo answers *what did I just read from what you just typed*. On a resume,
`_form_from_ticket` reconstructs `entered` from the saved row as `100.0000`
and `1234.56` — text that contains a separator and would therefore be echoed,
for a value nobody typed. A reading with no typing behind it is noise.

**`_settlement.html` is the one exception.** Its `cash_opening_balance` slot
is **filled in place**, from `readings`, because the block renders inside
`#tx-derived` — the rail the recalculation replaces wholesale. An OOB swap of
an element the same response is about to overwrite would race its own answer.
So: **`_readings.html` skips it**, and no flow's `readings_fields` names it.
`min="0"` went with `step="any"` (Deliberate deviations 3).

---

## §4 — The echo (`_readings.html`, five recalc partials)

34 lines, one macro and one loop:

```jinja
{% macro sentence(text) -%}
    {%- if text %}Read as <b>{{ text }}</b>{% else %}not read as a number{% endif -%}
{%- endmacro %}
{% for name in readings_fields | default([]) -%}
<span class="pf-read" id="tx-read-{{ name }}" hx-swap-oob="outerHTML">{% if name in readings %}{{ sentence(readings[name]) }}{% endif %}</span>
{% endfor -%}
```

The loop is over **`readings_fields`**, the flow's own fixed list from
`_Flow` — *not* over the dict's keys. That is what clears a slot: deleting the
comma out of `1,234` takes the key away, and a loop over `readings` would
leave the last sentence standing under a field that no longer says it. Every
slot is rendered on every keystroke, filled or empty.

The sentence lives in a **macro** so its copy has one home, and
`_settlement.html` imports it (`{% from … import sentence %}`) rather than
restating it. The `| default([])` on the loop is what makes that import safe:
Jinja's `{% from %}` executes the template body with a fresh context, where
`readings_fields` is not bound, and an unguarded `for` over an `Undefined`
raises.

`{% include "_partials/transactions/_readings.html" %}` is **last** in all
five recalc partials, and each partial's header comment says why: it is the
only fragment in the response that speaks about an *input* rather than about a
derivation. The caret rule holds — the inputs themselves are still never
re-rendered; the slot is a sibling.

---

## §5 — Tests

**`tests/core/test_decimal_input.py`** — 117 lines, **41 tests**, DB-free.
A parametrised table of 28 cases covering every row of the module docstring
and every example §5 lists (`""`, `None`, `"   "`, `"1200"`, `"-12,5"`,
`"+7"`, `"1 234 567,89"`, `"1'234.56"`, `"1.234.567,89"`, `"1,234,567.89"`,
`"12,"`, `"abc"`, `"1e3"`, `"12,34,56"`, `".5"`, `",5"`, plus NBSP / thin /
narrow-space variants and the bare `-` and `.`), then three named tests: the
result is a `Decimal` and not a `float` and equals its literal **exactly**
(`as_tuple()`, not just `==`), both notations read to one value, and
`was_interpreted` over eleven inputs.

**Route tests** — four new in `test_transactions_composer.py`, one in
`test_transactions_wizard.py`, one in `test_transactions_secondary_sale.py`:

- the order recalc with `units=1.234,56` and `price_per_unit=10,5` derives
  `<dd>12,962.88</dd>` — 1234.56 × 10.5 through `derive_cash_effect`, so the
  German notation reaches the **derivation** and not only the echo — and
  returns both slots filled, asserted as whole elements;
- the English notation on the same body reaches the same figure;
- `units=1200` leaves **all four** slots present and empty, and `Read as`
  appears nowhere in the response;
- `units=abc` returns 200, the slot says *not read as a number*, and the rail
  still derives nothing;
- the wizard's step-3 recalc, same two fields, plus one empty slot to pin the
  clearing behaviour;
- the secondary sale states the claim at its strongest: the **whole derived
  rail is byte-identical** whether the proceeds arrive as `1850000` or as
  `1.850.000,00`. Only the echo differs, which is the one thing that should.

**Template assertions** — three new in `test_pf_components.py` (the DB-free
template-source module gate 1 already runs): no `type="number"` survives
anywhere under `_partials/transactions/`; each of the six templates draws
exactly its stated number of `inputmode="decimal"` inputs, each of them
`type="text"`, each carrying no `step`, and each with a matching
`id="tx-read-{name}"` slot in the same file; and the two `inputmode="numeric"`
years have no slot at all.

**One existing assertion had to move.**
`test_transactions_wizard.py::test_the_recalculation_still_answers_in_three_parts`
counted `body.count('hx-swap-oob="outerHTML"') == 2`. The response now carries
four more OOB fragments by design, so the claim is restated per element — the
rail (the swap target) carries no OOB marker, and the two named hosts do. The
intent is unchanged and is now pinned more precisely than a count could.

Everything else that posts numbers is untouched and green.

---

## Render check

`POST /api/transactions/recalc` with `units=1.234,56`, `price_per_unit=10,5`,
`fees=1 200`, `taxes=abc`, through the test client. The readings fragment,
verbatim:

```html
<span class="pf-read" id="tx-read-units" hx-swap-oob="outerHTML">Read as <b>1,234.5600</b></span>
<span class="pf-read" id="tx-read-price_per_unit" hx-swap-oob="outerHTML">Read as <b>10.5000</b></span>
<span class="pf-read" id="tx-read-fees" hx-swap-oob="outerHTML">Read as <b>1,200.00</b></span>
<span class="pf-read" id="tx-read-taxes" hx-swap-oob="outerHTML">not read as a number</span>
```

and the rail it went with:

```html
<dl class="pf-sum">
            <span class="pf-sum__formula">1,234.5600 units × 10.5000</span>
            <dt>Gross</dt>
            <dd>12,962.88</dd>

            <dt>Fees</dt>
            <dd>−1,200.00</dd>
            <dt>Taxes</dt>
            <dd>—</dd>

            <dt class="pf-sum__total">Net proceeds</dt>
            <dd class="pf-sum__total">+11,762.88</dd>
```

Four fields, four notations — German decimal, German decimal, space grouping,
and text — and every one of them answered.

---

## Measurements

| | Before | After |
|---|---|---|
| `type="number"` under `_partials/transactions/` | 18 | **0** |
| `type="number"`, whole template tree | 38 | **20** |
| `inputmode="decimal"` inputs in the Area | 0 | **16** (4 / 4 / 3 / 3 / 1 / 1) |
| `inputmode="numeric"` inputs in the Area | 0 | **2** |
| files rendering a `class="pf-read"` element | 0 | **7** |
| `pf_components.css` | untouched | untouched |
| `core/decimal_input.py` | — | 99 lines, 2 public functions |
| `_readings.html` | — | 34 lines |
| tests added | — | 41 + 6 + 3 = **50** |
| templates read by the inventory | 219 | **220** |

The **7** files are the six that carry inputs plus `_readings.html`, which
renders the same element out of band. The prompt predicted 6; the seventh is
the OOB renderer, which could not have been fewer. `grep -rln pf-read` returns
12 because the five recalc partials now *mention* the class in their header
comments.

---

## Gates

| # | Gate | Result |
|---|---|---|
| 1 | `pytest tests/core/test_decimal_input.py tests/web/test_transactions_*.py tests/web/test_pf_components.py -q` | **403 passed in 7m32s**. ✔ |
| 1b | `pytest tests/regression -q` | see the line below the table |
| 2 | `ruff check .` · `ruff format --check .` | **All checks passed**; **942 files already formatted**. ✔ mypy: **not in the toolchain** — no `[tool.mypy]` in `pyproject.toml`, no config file, no CI step. The typechecker here is **pyright** on the ADR-0110 typing islands, and `core/` is not one of them (`services/overlay`, `services/market_data`, `services/provider_channel`, `services/provider_directory`), so no typecheck gate applies to the new module. |
| 3 | `grep -rc 'type="number"' web/templates/_partials/transactions/ \| grep -v ':0'` | **nothing**. 18 → 0; tree 38 → **20**. ✔ |
| 4 | `inputmode` per template | `_order_composer` 4, `_wizard_order` 4, `_secondary_buy_composer` 3, `_secondary_sale_composer` 3, `_commitment_composer` 1, `_settlement` 1 = **16** decimal; **2** numeric. ✔ (a repo-wide `grep -c 'inputmode="decimal"'` under the Area returns 17 — the seventeenth is the string inside `_readings.html`'s header comment.) |
| 5 | `grep -rn 'float(' core/decimal_input.py web/routes/transactions.py` | nothing in the new module; **nothing new** in the route — the one hit, `transactions.py:5209`, is pre-existing and outside this diff. ✔ |
| 6 | `git diff --name-only` | the module, the route, the six input templates, the five recalc partials, `_readings.html`, four test modules, the inventory, this report. No CSS, no `pf_components.css`, no other Area. ✔ (`docs/reports/P-UX-A1c2-report.md` also shows modified — it was already so at session start and is not staged.) |
| 7 | `python tools/ux_inventory.py` | re-run and committed. The `transactions` controls row is **unchanged** — a `pf-read` span is not a control, and the tool counts none. The only substantive change in `summary.md` is *templates read* 219 → **220**, the new `_readings.html`; everything else in `elements.csv` / `routes.csv` is line-number drift from the edits above. ✔ |

**Gate 1b:** `pytest tests/regression -q` → **164 passed in 2m13s**. Run
serially, after the first selection. ✔

---

## Deliberate deviations

**1. Two formatters for the echo, not one.** Decision 6 says the echo goes
through "the same formatter the Blotter uses for `row.amount`", which is
`_money` — grouped, two decimals. Applied to all sixteen fields that would
make the **Units** echo round `1,2345` to `1.23` and state a quantity the
server is not holding. An echo whose purpose is to say which number was read
must not round it into a different one.

So `units` and `price_per_unit` echo through **`_units`** (four decimals) and
the other fourteen through **`_money`**. This is still "do not write a second
one": both already exist, both are already what the rail states *these same
figures* with — `formula` is `_units(units) units × _units(price)` and the
last-price row is `_units` too — and the effect is that the echo can never
disagree with the number standing beside it. A third, exact-precision
formatter would have been the thing worth refusing, and it was refused: see
Flag 3 for what that leaves open.

**2. 99 lines, not ≤ 80.** The first draft hit 80 by folding the branch that
picks the separator into a walrus chain and dropping the grouping check into
an expression. It was correct and it was unreadable in a module whose whole
job is a rule a reader has to be able to check. The docstring table (20 lines,
which §1 asks for) and the Google docstrings ADR-0007 requires on both public
functions account for roughly half the file. Clarity kept; the budget is the
thing that gave.

**3. `min="0"` removed from `cash_opening_balance`, not only `step="any"`.**
§3 names `step` alone. But `min` is number-field validation apparatus in
exactly the same way, and a `type="text"` input ignores it — the constraint is
gone the moment the type changes, whether the attribute stays or not. Leaving
it would have been dead markup claiming a guard that no longer holds. The
guard itself was never the browser's alone:
`InvestmentService.create_cash_position` raises `ValidationError` on a
negative opening balance (`investment_service.py:1723`), and the surface shows
that refusal verbatim (D-5). Nothing is less guarded than it was.

**4. The reading's "no number" case is an empty string in the dict, and the
sentence lives in the template.** §2 words the dict value as *"the formatted
value or the literal `not read as a number`"*. Putting that sentence in Python
would have meant either a string comparison against a literal in Jinja — two
copies of user-facing copy in two languages, which drift — or an extra context
key carried through three builders. The dict is still `dict[str, str]`; `""`
is the marker; the sentence sits once, in `_readings.html`'s macro, which is
where this codebase keeps copy. `_settlement.html` imports that macro rather
than restating it.

**5. The slot list is on `_Flow`, not from `_derived_context`.** §4 offers
either a `with` in each recalc partial or a `readings_fields` value from
`_derived_context`, and asks which. Neither, exactly: it is a sixth field on
the `_Flow` table, surfaced to the recalc response as `readings_fields`.
`_derived_context` does not know the flow — it knows the *kind*, and the order
composer and the wizard are one kind and two flows — while `_Flow` is already
the single table that says what each flow builds and which partials draw it.
Five `with` blocks would have been the fourth near-copy that table exists to
prevent.

---

## §Flags (carry)

1. **Decision 2 is the tenant-shaped one.** A lone separator reads as the
   decimal separator, so `1,234` is one-point-two-three-four. A German
   institutional operator typing a thousands-grouped figure without decimals
   will hit this, and the echo is the only thing standing between them and a
   wrong booking. A tenant-level notation preference would move the rule —
   **D-UX-S**. The parser is ready for it: the branch is three lines and has
   no other caller.
2. **The 20 `type="number"` outside Transactions now have a parser to delegate
   to.** `core/decimal_input.py` imports nothing and is not Transactions-shaped;
   each Area's strand sweeps its own — A-2 (saa ×4, planning_desk ×4), A-4
   (investments ×7), A-6 (watch_desk ×3), and `portfolio_analysis` ×2 with
   whichever strand takes the Front Office. Each sweep needs the *echo* too,
   not only the parser: a field that accepts two notations and says nothing is
   worse than one that accepts one.
3. **Both echo formatters round.** `_money` at two decimals, `_units` at four.
   A fee typed as `1,005` is held as 1.005 and echoed as *1.00*. It is the
   same rounding the rail beside it does, so the surface is internally
   consistent and the operator is never shown two different numbers for one
   value — but the echo is not a statement of full precision, and R10's other
   half (the as-of / locale question A1a raised about the stamp) is the same
   **D-UX-S** conversation: whether this product states figures exactly, and
   at whose locale.
4. **The typographic minus is not an input glyph.** The product *renders* U+2212
   (`_MINUS`) in the rail, the blotter and every signed figure, and decision 4
   admits only ASCII `-` and `+` as a sign. Copying a negative figure off the
   surface and pasting it into a field therefore reads as *not read as a
   number*. One character in `_BODY`/`_INTERPRETED` would fix it; it is not in
   this prompt's decisions, so it is recorded rather than done.
5. **A gesture re-render clears every reading.** Save as draft, Propose, a
   refused Book now — all re-render the composer, whose static slots are
   empty. §3 explains why (a resumed form's `entered` is route-formatted text
   nobody typed, and echoing it would be noise). If the browser round shows
   the blank line reading as a *loss* rather than as silence, the fix is to
   fill the static slot from `readings` and suppress the reading on the
   resume path specifically — a decision, not a bug.

---

## For A1e — what is still bespoke in this Area

After this prompt the Transactions Area is on the shared vocabulary
everywhere the operator types or reads a figure. What remains bespoke:
**`tx-btn` ×7**, all of them in the four S5 panels (`_cancel_panel`,
`_reverse_panel`, `_impact_panel`, `_negative_cash`); the `tx-leg*`,
`tx-msg*` and `tx-messages` rules in `components/transactions.css`; the two
`:has()` transition rules; and `.pf-more` on Identify, to be opened per the
operator's decision. None of them is touched here.

---

## The commit

```bash
git add core/decimal_input.py web/routes/transactions.py \
  web/templates/_partials/transactions/_order_composer.html \
  web/templates/_partials/transactions/_wizard_order.html \
  web/templates/_partials/transactions/_secondary_buy_composer.html \
  web/templates/_partials/transactions/_secondary_sale_composer.html \
  web/templates/_partials/transactions/_commitment_composer.html \
  web/templates/_partials/transactions/_settlement.html \
  web/templates/_partials/transactions/_order_recalc.html \
  web/templates/_partials/transactions/_wizard_recalc.html \
  web/templates/_partials/transactions/_secondary_buy_recalc.html \
  web/templates/_partials/transactions/_secondary_sale_recalc.html \
  web/templates/_partials/transactions/_commitment_recalc.html \
  web/templates/_partials/transactions/_readings.html \
  tests/core/test_decimal_input.py \
  tests/web/test_transactions_composer.py \
  tests/web/test_transactions_wizard.py \
  tests/web/test_transactions_secondary_sale.py \
  tests/web/test_pf_components.py \
  docs/ux/inventory docs/reports/P-UX-A1d-report.md

git commit -m "feat(transactions): number entry per R10 — core/decimal_input.py reads both notations server-side, _decimal_or_none delegates, 16 amount inputs become type=text inputmode=decimal and two years inputmode=numeric, recalc echoes 'Read as …' in pf-read slots (UX A-1, P-UX-A1d)"
```
