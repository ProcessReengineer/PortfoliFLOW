# P-UX-H1 — Four copy leaks out of the baseline

**Track:** UX overhaul (hygiene). Answers P-UX-0 OQ-6.
**Status:** Complete. Five files changed, copy only; no behaviour, no markup
structure, no CSS, no schema.
**Ran:** after P-UX-2 (`639eded`) and P-UX-0b (`775731e`); before the inventory
re-run and P-UX-1.

---

## Operator action block

1. **Review and commit.** Proposed message:

   ```
   fix(web): remove internal references from four user-facing strings (P-UX-H1)
   ```

   Nothing is staged — `git add` was not run.

2. **Re-bootstrap the dev database before using the dev server again.** The web
   tests in this run tore down seeded tenant data. Run:

   ```
   portfoliflow bootstrap
   ```

3. **Re-run the UX inventory after committing.** Four of the extracted strings
   changed, so the P-UX-0b atlas baseline is now one commit stale. This is the
   intended ordering (the leaks leave the baseline before it is frozen), not a
   regression.

4. **Note one deviation from the Definition of Done:** `pyright` on
   `web/routes/benchmarks_attribution.py` is **not** clean. It carries one
   pre-existing error, unrelated to this change and outside the CI typing
   island. Proof and detail in F-UX-H1.4 — no action required, but the DoD
   checkbox cannot be ticked as written.

---

## Verify-first

Executed against a clean tree at `775731e`. All six checks pass; the four target
strings sat at exactly the lines the prompt names.

| # | Check | Expectation | Actual | Verdict |
|---|-------|-------------|--------|---------|
| 1 | Clean tree | empty | empty | pass |
| 2 | ADR pointer | 1 hit, line 42 | `42: see ADR-0061 for the expected Excel format.` | pass |
| 3 | CLI hint | 1 hit, line 16 | `16: Or run <code>portfoliflow bootstrap</code> to install three` | pass |
| 4 | UUID message | 1 hit, line 452 | `452: message="Selected investment id is not a valid UUID.",` | pass |
| 5 | Build label | `title="Build SHA"` at 42 | `42: <span class="pf-statusbar__build" title="Build SHA">`; `43: {{ build_sha or "dev" }}`; `39:` AGPL §13 source link; `15:` docstring "short build SHA" | pass |
| 6 | Tests asserting the strings | exactly one web assertion | one assertion (`tests/web/test_saa_routes.py:323`); two non-assertions, see below | pass |

**Check 6 detail.** Three hits in `tests/web/`, one of them an assertion:

- `tests/web/test_saa_routes.py:323` — `assert "portfoliflow bootstrap" in body`.
  The anticipated assertion; rewritten in this run.
- `tests/web/test_accessibility.py:24` — module docstring of local setup steps.
  Untouched, as the prompt states.
- `tests/web/test_shell_sidebar_and_areas.py:388` — a source **comment**,
  `# Build SHA placeholder.`, above `assert "pf-statusbar__build" in body`. The
  assertion is on the CSS class, not on the string, so this is not a second web
  assertion and check 6 stands. Not anticipated by the prompt; recorded as
  F-UX-H1.3.

**After the edits**, the four §1 greps return zero hits and the `build_sha`
variable survives:

| Grep | Result |
|---|---|
| `ADR-0061` in `benchmarks_attribution_stage_a.html` | zero hits |
| `portfoliflow bootstrap` in `saa_empty_state.html` | zero hits |
| `not a valid UUID` in `benchmarks_attribution.py` | zero hits |
| `build sha` (case-insensitive) in `statusbar.html` | zero hits |
| `build_sha` in `statusbar.html` | 3 hits — docstring 15, AGPL §13 link 39, render 43 |

---

## The four edits

### E-1 — `web/templates/_partials/benchmarks_attribution_stage_a.html`

Took the prompt's **fallback** wording, because the conditional resolved against
the longer variant: the Data Import section documents no format (F-UX-H1.1).

```diff
@@ -38,8 +38,7 @@
         No investment-vs-benchmark rows available. Either there are
         no active investments yet, or no asset class carries a
         benchmark mapping. Import benchmarks via the
-        <a href="/admin#data-import">Data Import</a> section &mdash;
-        see ADR-0061 for the expected Excel format.
+        <a href="/admin#data-import">Data Import</a> section.
     </p>
```

The link markup is unchanged; the `&mdash;` went with the clause it introduced.

### E-2 — `web/templates/_partials/saa_empty_state.html`

```diff
@@ -13,8 +13,8 @@
     <p class="saa-empty-state__cli-hint">
-        Or run <code>portfoliflow bootstrap</code> to install three
-        seed templates (Conservative Multi-Strategy, Balanced
-        Institutional, Growth Private Markets).
+        Or ask your administrator to install the three seed templates
+        (Conservative Multi-Strategy, Balanced Institutional,
+        Growth Private Markets).
     </p>
```

The `<code>` element is gone; the class `saa-empty-state__cli-hint` stays and no
CSS changed. The line break was placed **after** "templates" deliberately: the
route test matches raw `response.text`, so the phrase the assertion looks for has
to be contiguous in the rendered source. A break in the middle of that phrase
would have passed review and failed the test.

### E-3 — `web/routes/benchmarks_attribution.py`

```diff
@@ -449,7 +449,7 @@ async def get_stage_a_investment_detail(
     except ValueError:
         return _render_stage_a_empty_state(
             request,
-            message="Selected investment id is not a valid UUID.",
+            message="Selected investment is not valid.",
         )
```

`message=` only. The `UUID(investment_id)` parse, the `except ValueError` path and
the `_render_stage_a_empty_state` call are untouched.

### E-4 — `web/templates/_partials/statusbar.html`

```diff
@@ -12,7 +12,7 @@
-     build_sha    — short build SHA for diagnostic correlation;
+     build_sha    — short build identifier for diagnostic correlation;
                     falls back to "dev" when unset.
@@ -39,7 +39,7 @@
-        <span class="pf-statusbar__build" title="Build SHA">
+        <span class="pf-statusbar__build" title="Build">
             {{ build_sha or "dev" }}
         </span>
```

The rendered `{{ build_sha }}` value and the AGPL §13 source link (line 39) are
byte-identical. The docstring edit is the variant the prompt permits, and it
takes the case-insensitive `build sha` grep to zero as well.

### Test

```diff
@@ -320,7 +320,7 @@ async def test_get_section_empty_renders_empty_state(
     assert "No SAA configurations yet" in body
-    assert "portfoliflow bootstrap" in body
+    assert "ask your administrator to install the three seed templates" in body
```

`tests/web/test_statusbar.py` was **not** touched — see F-UX-H1.2.

---

## Gates

| Gate | Result |
|---|---|
| `ruff check` | `All checks passed!` |
| `ruff format --check` | `911 files already formatted` |
| `pyright web/routes/benchmarks_attribution.py` | `1 error, 0 warnings, 0 informations` — pre-existing, see F-UX-H1.4 |
| `tests/web/test_saa_routes.py`, `test_statusbar.py`, `test_benchmarks_attribution_routes.py`, `test_data_import_route_benchmarks.py` | `69 passed, 2 skipped in 139.97s` |
| `tests/web/test_shell_sidebar_and_areas.py` (statusbar coverage, added) | `66 passed in 131.03s` |

The two skips are fixture guards (`testdata not at {path}`) for the Excel
workbooks that left the repository in the August 2026 transition. They are
pre-existing and independent of copy.

`ls tests/web/ | grep -i benchmark` resolved the DoD's wildcard to two files,
both run above. `test_shell_sidebar_and_areas.py` was added to the selection
because it renders the statusbar partial E-4 edits; the DoD list did not name it.

---

## Findings

**F-UX-H1.1 — the Data Import section documents no Excel format (decided E-1).**
E-1 was conditional on whether Data Import describes the expected format anywhere
a user can see. It does not.
`web/templates/_partials/data_import_section.html` contains no occurrence of
"benchmark", and its single include,
`web/templates/_partials/data_import_upload_form.html`, is an upload control plus
a past-uploads table; its only format-adjacent strings are control labels
("Upload an Excel file", "Excel workbook (.xlsx)", the `accept` attribute) and a
`Format` column rendering the persisted `format_version`. There is no format
documentation there for any workbook, benchmarks included.

So E-1 took the fallback wording. The substantive gap remains: the Excel format
is specified only in ADR-0009 / ADR-0061, with no in-product help, and a user who
follows the link now arrives at an upload box with no guidance on what to upload.
Before this change they were pointed at an ADR they cannot read either — the leak
was masking the gap, not filling it. Belongs to an area loop or the IA brief, not
to a hygiene pass.

**F-UX-H1.2 — E-4 required no test change.**
`tests/web/test_statusbar.py` contains no occurrence of `Build SHA`, `Build`, or
`title=`; the `title` attribute is unasserted. §3's conditional sixth file
therefore stayed untouched and the run changed five files.

**F-UX-H1.3 — one stale comment survives E-4.**
`tests/web/test_shell_sidebar_and_areas.py:388` reads `# Build SHA placeholder.`
and now describes a label the template no longer renders. It is a comment above
an assertion on the CSS class `pf-statusbar__build`; nothing fails and no
behaviour depends on it. It sits outside §3's exhaustive file list, so it was
left alone deliberately rather than overlooked. Natural home: whichever pass next
edits that file.

**F-UX-H1.4 — pre-existing pyright error in the edited route file.**
`pyright web/routes/benchmarks_attribution.py` reports one error:

```
web/routes/benchmarks_attribution.py:295:21 - error: Argument of type
"SAAHypotheticalEffects | None" cannot be assigned to parameter "effects" of
type "_Effects | None" in function "build_benchmark_saa_hypothetical_spec"
  ... "actual_cumulative_endpoint" is not read-only in protocol
```

It is a protocol read-only variance mismatch at line 295. It is pre-existing, on
two independent grounds:

1. `git show HEAD:web/routes/benchmarks_attribution.py | diff -` against the
   working copy yields exactly one hunk, the line-452 string literal. A string
   literal cannot produce a `reportArgumentType` error 157 lines above it.
2. `web/routes/` is not in the pyright typing island. `[tool.pyright]` in
   `pyproject.toml` sets `include = ["services/overlay", "services/market_data"]`
   (ADR-0109 §3 as amended by ADR-0110), so CI never type-checks this file; the
   DoD's invocation reaches it only by naming the path explicitly.

No action taken: fixing an island-external protocol variance is well outside a
copy-only hygiene pass, and touching the file further would break the "five files,
copy only" property. Recorded so the DoD's unticked box is a known quantity.

**F-UX-H1.5 — `saa-empty-state__cli-hint` is now a misnomer.**
The paragraph no longer hints at a CLI. The prompt pins the class (CSS unchanged)
and that was honoured, so this is a note for a later CSS/markup pass, not a
defect: the name is internal, never rendered, and renaming it would have pulled a
stylesheet into a copy-only change.

---

## Open questions

**OQ-H1.1 — where does in-product Excel format help land (from F-UX-H1.1)?**
Three candidates: a help affordance on the Data Import surface itself; a
format-reference doc linked from it; or accepting that format guidance is
operator knowledge and dropping the implied promise from empty-state copy
altogether. This is adjacent to the 22 `Data Import` deep links already routed to
the vocabulary/IA brief (F-UX-0.5), so deciding both together would keep that
surface's open items in one place.

**OQ-H1.2 — is "Selected investment is not valid." the final register for E-3?**
The wording is fixed by the prompt and was applied verbatim. It is a
defensive-path message a user reaches only via a hand-edited or stale query
parameter, and it now says what is wrong without saying what to do about it
("choose an investment from the list" would). Flagging it for the Back Office
area loop rather than changing it here, since error-message register is exactly
the kind of cross-surface vocabulary decision this hygiene pass was scoped to
stay out of.
