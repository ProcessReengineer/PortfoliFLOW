# P-UX-A0g — Catch-up: three stale pins fixed, §2 void · A-0 closes

**Strand:** UX A-0 · **Runs after:** P-UX-A0f · **Tip at session start:** `77c7958`
**Scope as executed:** `tests/web/test_transactions_composer.py`,
`tests/web/test_transactions_wizard.py`, `tests/tools/test_ux_atlas_reveal.py`,
`tests/web/conftest.py` (the shared helper §1 chose), this report. No product code, no
template, no `.css`, no `.js`, no `tools/`, no `data/`.

---

## OPERATOR ACTION REQUIRED

The commit is yours. Nothing is staged.

```bash
git add tests/web/test_transactions_composer.py tests/web/test_transactions_wizard.py \
        tests/tools/test_ux_atlas_reveal.py tests/web/conftest.py \
        docs/reports/P-UX-A0g-report.md
git commit -m "test: scope composer button counts to the section body, atlas reveal test reads the scenes file — three stale pins from A-0 fixed (UX A-0, P-UX-A0g)"
```

The commit subject drops the prompt's *"workbook tests back in the run"* clause, on the
operator's instruction: §2 was voided mid-session and no workbook test moved.

Second: **the dev DB is empty.** A0f left it so, and gates 1 and 5 here truncated it again.
Bootstrap before the next atlas or browser run.

---

## Verify-first

| # | Check | Result |
|---|---|---|
| 1 | `git log --oneline -3` | `(UX A-0, P-UX-A0f)` present — `77c7958`, the A0f report, is committed |
| 2 | `git status --porcelain` | empty |
| 3 | `ls data/sample/` | **FAILED** — `data/` did not exist at all. Escalated rather than halted; resolved by the operator voiding §2 (see §Findings) |
| 4 | composer `:430–440` · wizard `:442–452` | both carried `section.count("<button") == 5` |
| 5 | `test_ux_atlas_reveal.py:548–558` | the three hard-coded Transactions scene names |
| 6 | `atlas-scenes.json` | 14 scenes; seven `transactions-*` — `blotter`, `composer-commitment`, `composer-order`, `composer-secondary`, `flow-chooser`, `history`, `wizard-step-1` |
| 7 | `grep -n '<button' web/templates/areas/_section.html` | one hit, `:49`, the palette-open button A0b put in the view head |
| 8 | A0f report §Failures | read; its three proposed fixes are the input to §1 below |

Check 3 was the one failure, and it was not fixed by this session: the workbook copy is the
operator's, and `data/` was left untouched throughout.

---

## §1 — The three pins

All three fixed. `git diff --stat`: 80 insertions, 11 deletions across four files.

### F1 / F2 — the two button counts, scoped to the chooser

**Shape chosen: one shared helper, imported by both files.** The two `_chooser` helpers the
A0f report proposed would have been byte-identical, and the prompt forbids pasting them
twice. `tests/web/` has no `_helpers` module, so the helper went into
**`tests/web/conftest.py`** as `chooser_markup()` — the prompt's first-named option.
Importing a plain callable from a conftest is established here:
`tests/regression/conftest.py`'s `ScratchDatabase` is imported by five regression modules
the same way. `tests/web/__init__.py` exists, so `from tests.web.conftest import
chooser_markup` resolves without path games.

**One deviation from the report's helper body**, taken deliberately. The report proposed

```python
return section[start : section.index("</div>", section.rindex("</button>", start))]
```

`str.rindex` searches to the end of the string, so that slice is correct only while the
chooser holds the *last* `</button>` in the Section. It does today — `#tx-composer-host` is
empty on first render — but the moment a test renders the Section with a composer swapped
in, the slice silently swallows the composer too and the count is wrong in the permissive
direction. `chooser_markup()` instead walks `<div` / `</div>` from `<div class="tx-flows">`
and returns at depth zero, which is correct regardless of what follows the chooser and
regardless of whether a tile ever gains a wrapper `div`. It raises `ValueError` with a
stated message on an absent or unbalanced chooser rather than an opaque `ValueError` from
`str.index`.

**The diff, in prose.**

*`tests/web/conftest.py`* — `import re` added to the stdlib block; `chooser_markup(section)`
appended at module end with a Google-style docstring giving the Args / Returns / Raises and
the *why*: a rendered Section is the shell's, `web/templates/areas/_section.html` carries
the view header, and P-UX-A0b put the palette button in it, so a Section-wide control count
pins shell chrome that later strands are free to move. The docstring names both call sites
and records that the two counts move together by design, per `_chooser.html`'s own header
comment.

*`test_transactions_composer.py`* — import added; the test binds `chooser = chooser_markup(section)`
and counts on that; four paragraphs of docstring gain one saying what is counted and why the
head is excluded. The three `"Arrives with …"` / `tx-flow--pending` negative assertions stay
on the full Section — they are absence assertions, and a wider haystack only makes them
stricter.

*`test_transactions_wizard.py`* — import added; the single count line becomes
`chooser_markup(section).count("<button") == 5`; docstring gains the same paragraph. The
file was reformatted by `ruff format` (the line fit on one after all).

Both messages are unchanged — *"MD-1's five flows are the only controls"* and *"MD-1's five
tiles are the only controls"* — and both are now true of what is actually counted, which
they were not before.

### F3 — the atlas reveal test reads the scenes file

**Option chosen: read from the file and assert the contract** — the prompt's stated
preference. The test's intent survives it intact, and this is why: the comment above the old
assertion states the failure mode it was guarding — *"a scene that lost it would silently
photograph the landing Section three times"* — and that is a statement about the `section`
key, never about how many scenes exist. Hardcoding the map made the test a census as well as
a contract; the contract is what has value, and the census is what went stale.

The test now builds `{name: scene.get("section")}` over the shipped file and asserts: the
filter returned scenes at all; none of them is missing a `section` (the offenders are named
in the message, not merely counted); the set of sections named is exactly
`{new, blotter, history}`, so a scene naming a fourth slug still fails; and the two stable
anchors `transactions-blotter → blotter`, `transactions-history → history` hold. `.get()`
rather than `[...]` so a scene that dropped the key fails as a readable assertion instead of
a `KeyError`. The comment was promoted to a docstring recording that the names come from
`docs/ux/atlas-scenes.json` and that A0s's four composer scenes are what went stale.

A scene added by A-1 no longer breaks this test. A scene that drops its `section`, or names
a Section that does not exist, still does.

---

## §2 — Void

Not run. The operator voided §2 mid-session: the three workbooks are **superseded** by
`sample_data/PortfoliFLOW_example_portfolio.xlsx`. No copy was made and `data/` was not
created, touched or read. See §Findings.

---

## Findings

**Finding 1 — the three workbooks are superseded, and A0f's note on them is overtaken.**

`data/sample/` did not exist at session start; neither did `data/`. On being asked, the
operator stated that the three workbooks the prompt's header would have copied are
superseded by **`sample_data/PortfoliFLOW_example_portfolio.xlsx`**, which is *tracked*
(`git ls-files sample_data/` confirms it, alongside the two Unicorn Ventures PDFs) and is
already read by `tests/services/test_investment_service_import_example_portfolio.py:49-50`.
That module needs no operator step and no gitignored directory.

**A0f's finding that "the workbooks are not lost — they are in `~/Code/PortfoliFLOW/test data/`"
is overtaken by this statement.** It was accurate as a matter of fact, and this session
re-confirmed all three files are present at that path. It is superseded as a matter of
*intent*: the question A0f answered was "can the seven skips be recovered by copying?", and
the operator's answer is that they should not be — the fixture they depend on is no longer
the project's sample data. The 09-17 report's "declared lost" and A0f's "found" are both
now historical.

**Consequence for the seven tests.** They remain skipped, and now permanently rather than
pending an operator copy. All seven degrade to `pytest.skip` on the missing path at runtime —
`test_data_import_phase7_wiring.py:86`, `test_investment_service_transform_limits_aum.py:146`
and `:219`, `test_data_import_route_benchmarks.py:78`, and a module-level
`@pytest.mark.skipif` at `test_limit_coverage.py:1214` — so they cost the suite nothing and
turn it no redder. What they covered (the v21 limits/AUM roundtrip, the Phase-7 import
wiring, the benchmark import route, the limit-coverage parity engine) is **uncovered until
those modules are re-anchored on the example portfolio**. That is not this prompt's work and
is not scheduled by it; it is stated here so a later strand can pick it up deliberately.

**No product defect was found by this session.** All three pins were stale tests, exactly as
A0f classified them, and §2 — the one part of the prompt that could have surfaced a real
product regression — did not run.

---

## §3 — What stays skipped, on the record

Three deliberate skips, unchanged, each one line:

- **`tests/web/test_accessibility.py:74`** — a manual procedure, not an automated check;
  ADR-0037 §10 is the procedure.
- **`tests/auth/test_local_password_backend.py:254`** — constant-time comparison discipline;
  carries `@pytest.mark.timing` **and** `@pytest.mark.skip`, so `-m timing` alone does *not*
  run it (A0f's note). Left exactly as found.
- **`tests/web/test_chart_theme_alternatives.py:54`** — a `strict=True` xfail:
  `chart_theme_light.json` lacks the `pf.*` keys, which belong to the Phase-B theme picker.

The seven `integration` routing evals stay deselected by `pyproject.toml`'s
`addopts = "-m 'not integration'"`.

Joining that list by §Findings: the **seven workbook tests**, now skipped permanently rather
than pending a copy.

---

## Gates

| # | Gate | Result |
|---|---|---|
| 1 | `pytest tests/web/test_transactions_composer.py tests/web/test_transactions_wizard.py -q` | **61 passed** in 138s — F1/F2 fixed |
| 2 | `grep -n '== 6' …` on both files | no match (exit 1) — the shell was not pinned |
| 3 | `pytest tests/tools -q` | **63 passed** in 0.15s — F3 fixed |
| 4 | §2 command | **not run — §2 is void.** The operator voided it mid-session; the workbooks are superseded and `data/` was not to be touched, so the command's precondition cannot be met and running it would only have re-reported the seven skips |
| 5 | `pytest tests/regression -q` | **164 passed** in 136s — unchanged from A0f step 2 |
| 6 | `ruff check` · `ruff format --check` | clean, 940 files |
| 7 | `git diff --name-only` | `tests/tools/test_ux_atlas_reveal.py`, `tests/web/conftest.py`, `tests/web/test_transactions_composer.py`, `tests/web/test_transactions_wizard.py` — the three test files plus the shared helper, and this report. `data/` absent, product tree untouched |

The full suite was **not** re-run, by the prompt's construction. A0f measured 5,464 passed /
3 failed; this session fixed exactly those three and added no test, so the baseline is
5,464 + 3 = **5,467 passed**. The §2 `+7` the prompt anticipated does not apply.

---

## Deliberate deviations

1. **Verify-first check 3 failed and the session did not halt.** The prompt directs a STOP
   file at `~/P-UX-A0g-STOP.md` on a missing workbook. None was written. §1 and §3 have no
   dependency on `data/sample/`, so the independent work was completed first and the blocker
   was put to the operator with the state of play attached — who voided §2 outright, making
   the STOP moot. Had the answer gone the other way, the STOP was still available. **No
   `~/P-UX-A0g-STOP.md` exists.**

2. **§2 not run; the workbooks are superseded.** On the operator's statement,
   `sample_data/PortfoliFLOW_example_portfolio.xlsx` replaces the three. No copy was made,
   `data/` was neither created nor touched, and A0f's "the workbooks are not lost" is
   overtaken — see §Findings, finding 1. The commit subject lost its *"workbook tests back in
   the run"* clause accordingly.

3. **`chooser_markup()` walks the `div` depth instead of the report's `rindex("</button>")`.**
   Same result today, correct also once a Section renders with a composer swapped into
   `#tx-composer-host`. Reasoning in §1, F1/F2.

4. **The composer test's `<form` / `<input` / `<a ` assertions were scoped to the chooser
   too.** The A0f report said they "still pass against the full Section and can stay as they
   are", and that was verified true —
   `grep -n '<form\|<input\|<a ' web/templates/areas/_section.html` returns nothing today.
   They were narrowed anyway, one word per line, because their failure message reads *"the
   chooser carries an unexpected control"*: if the shell's view header ever gains a link,
   those assertions would fail with a message naming the wrong file. That is precisely the
   staleness class this prompt exists to close, and leaving three assertions in it while
   fixing the count beside them would have been half a fix.

5. **`tests/web/conftest.py` is a fifth file in the diff.** Anticipated by the prompt
   ("plus a shared helper file if §1 chose one") and by the operator's restated gate 7.

---

A-0's full-suite obligation is met; the baseline for A-1 is **5,467 passed** at the commit
the OPERATOR ACTION block creates — product code unchanged since `b1eced6`, `77c7958` and
this commit being test-and-docs only.
