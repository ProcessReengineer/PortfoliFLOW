# Full-suite report: PortfoliFLOW (AGPL), 2026-09-23 — end of A-0

**Type:** test run and report · **No code change.** The only file this session writes inside the
repository is this report.

## OPERATOR ACTION REQUIRED

Claude Code performed no git writes. Nothing is staged; nothing is committed.

```sh
git add docs/reports/full-suite-2026-09-23-report.md
git commit -m "docs(reports): full-suite baseline 2026-09-23 at the end of A-0, 5,464 passed at b1eced6 (UX A-0, P-UX-A0f)"
```

Nothing else is uncommitted: `git status --porcelain` was empty before the run and is empty after it
apart from this file.

## Verdict

**Three failures, all A-0's, all in the test rather than the product.** 5,476 tests were selected;
**5,464 passed, 3 failed, 0 errors**. Step 1 exited **1**, step 2 exited **0**.

The three are one finding wearing two hats, and neither hat is a product defect:

1. **Two `<button>`-count pins are stale** — `test_transactions_composer.py:435` and
   `test_transactions_wizard.py:447` both assert `section.count("<button") == 5`. A0b (`9581ed0`)
   put a palette-open button into the sticky view header of the *shared* Section template
   `web/templates/areas/_section.html:49`, which every Area includes, so the New-transaction
   Section now legitimately contains six buttons: the five flow tiles plus one piece of shell
   chrome. Both test files are byte-unchanged since the 09-17 baseline, where they were green.
2. **One atlas scene enumeration is stale** — `test_ux_atlas_reveal.py:554` hardcodes the three
   Transactions scenes. A0s (`b1eced6`) added four composer scenes to `docs/ux/atlas-scenes.json`,
   which is the deliverable that commit's subject announces, without updating the test that
   enumerates them.

Nothing was fixed; proposed fixes are in *Failures* below, and are the next prompt's to apply.

Two things went exactly to plan and are worth stating because they are the gates A-0 owed:
**no test skipped for "Cannot reach Postgres"** (gate 2: zero hits in both logs), so the
database-bound half of the suite really ran; and the **JUnit totals reconcile with the console**
for both steps.

One correction to the record: the 2026-09-17 report stated that no `PortfoliFLOW_Testdaten_v2*.xlsx`
exists anywhere under `$HOME`. It does. All three workbooks behind seven of the eight skips are on
this machine — see *What is not running*. The gap is a **local-data gap**, restorable with a
three-file copy, not a lost-data gap.

## What was tested

- **Repo:** `/home/soenke/Code/PortfoliFLOW/PortfoliFLOW`
- **HEAD:** `b1eced6` — `docs(ux): ui-standards.md, atlas scenes for Cases and the Transactions
  composers, dot-test retired, rail off the super-admin surface — end of A-0 (UX A-0, P-UX-A0s)`.
  The tip subject ends `(UX A-0, P-UX-A0s)` as check 1 expected; no docs commit sits on top.
- **Uncommitted changes included in the run:** **none.** `git status --porcelain` was empty at
  check 2 and the run did not alter it. Unlike 2026-09-17, the committed tree and the tested tree
  are identical, so this baseline needs no caveat.
- **Python:** 3.13.14 from `.venv`; pytest 9.0.3.
- **Database:** dev Postgres in the `portfoliflow-postgres` podman container, found **already
  running and healthy** (up 27 minutes, no start needed), `pg_isready` accepting connections, at
  Alembic head `b034_add_trade_tickets`. Both `DATABASE_URL` and `DATABASE_URL_SUPERUSER` present
  in `.env`.
- **Commands:** the two steps from `.github/workflows/full-suite.yml:74` and `:76`, with the
  selection strings copied byte-for-byte from the workflow. The only additions are reporting flags
  — `-v -rfEsxX --durations=25 --junitxml=…`.

  ```sh
  pytest -m "not integration and not timing" --ignore=tests/regression
  pytest -m "not integration and not timing" tests/regression
  ```

## Results

| Step | Command | Selected | Passed | Failed / Errors | Skipped | Xfailed | Time |
|---|---|---|---|---|---|---|---|
| 1 | `pytest -m "not integration and not timing" --ignore=tests/regression` | 5,312 | 5,300 | **3** / 0 | 8 | 1 | 2:04:04 |
| 2 | `pytest -m "not integration and not timing" tests/regression` | 164 | 164 | 0 / 0 | 0 | 0 | 0:02:17 |
| **Total** | | **5,476** | **5,464** | **3 / 0** | **8** | **1** | **2:06:21** (18:06:35 → 20:13:05) |

Step 1 collected 5,320 items, deselected 8, selected 5,312. Exit codes, from `run-meta.txt`:
`step1 exit 1`, `step2 exit 0`.

**JUnit reconciliation (gate 3).** `step1.xml` carries
`tests="5312" failures="3" errors="0" skipped="9" time="7444.147"` and `step2.xml`
`tests="164" failures="0" errors="0" skipped="0" time="137.985"`.

| | Console | XML | Reconciled |
|---|---|---|---|
| Step 1 selected | 3 + 5,300 + 8 + 1 = 5,312 | `tests="5312"` | ✅ |
| Step 1 failures | 3 failed | `failures="3"`, `errors="0"` | ✅ |
| Step 1 skipped | 8 skipped + 1 xfailed | `skipped="9"` | ✅ — JUnit records an expected failure as a skip, so the XML reads one higher, exactly as the 2026-09-17 report explains |
| Step 1 time | 7444.19 s | `time="7444.147"` | ✅ |
| Step 2 selected | 164 passed | `tests="164"` | ✅ |
| Step 2 time | 137.99 s | `time="137.985"` | ✅ |

## Tests that did not run as normal passes

All are skipped, expected to fail, or excluded on purpose. The set is **identical to 2026-09-17** —
same eight skips, same reasons, same single xfail, same eight deselected. Verdicts per §2:

**Verdict 1 — pre-existing (same outcome as the 2026-09-17 report):** every row below. The 09-17
report's *Tests that did not run as normal passes* lists all eight skips, the xfail and the eight
deselected with these exact reasons; nothing was added and nothing resolved.

**Verdict 2 — environment:** all eight skips and all eight deselected are environment or
deliberate-exclusion outcomes, not code outcomes.

| Outcome | Node | Reason (verbatim) | CI skips it too? |
|---|---|---|---|
| SKIPPED | `test_limit_coverage.py:1214::test_engine_matches_excel_reference_three_dates` | Reference XLSX not yet provided (expected at …`data/sample/PortfoliFLOW_Limit_Coverage_Reference_v1.xlsx`). See Kickoff #2 §6.3. | yes |
| SKIPPED | `test_investment_service_transform_limits_aum.py:146::test_v21_roundtrip_landing_investments_aum_and_limits` | v21 testdata not at …`data/sample/PortfoliFLOW_Testdaten_v21.xlsx`; skipping roundtrip. | yes |
| SKIPPED | `test_investment_service_transform_limits_aum.py:219::test_duplicate_import_raises_limit_validation_error` | v21 testdata not at …; skipping. | yes |
| SKIPPED | `test_data_import_phase7_wiring.py:86::test_web_import_persists_limits_and_anlv_but_not_aum` | v21 testdata not at …; skipping. | yes |
| SKIPPED | `test_data_import_phase7_wiring.py:86::test_web_dry_run_completes_with_phase7_workbook` | v21 testdata not at …; skipping. | yes |
| SKIPPED | `test_data_import_route_benchmarks.py:78::test_upload_endpoint_persists_benchmarks_when_sheets_present` | testdata not at …`PortfoliFLOW_Testdaten_v24.xlsx`; skipping. | yes |
| SKIPPED | `test_data_import_route_benchmarks.py:78::test_upload_endpoint_skips_benchmarks_when_sheets_absent` | testdata not at …`PortfoliFLOW_Testdaten_v21.xlsx`; skipping. | yes |
| SKIPPED | `test_accessibility.py:74::test_accessibility_minima_documentation_marker` | Accessibility minima are validated manually in Phase 2 (ADR-0037 §10). See this module's docstring for the procedure. | yes — deliberate |
| XFAIL | `test_chart_theme_alternatives.py:54::test_all_chart_themes_have_same_schema` | chart_theme_light.json lacks the ADR-0058 pf.* web-chrome keys; light-theme values to be authored with the Phase-B theme picker. | yes — deliberate |
| DESELECTED ×7 | `test_routing_eval.py:178::test_routing_selects_expected_tool[…]` | `@pytest.mark.integration` — excluded by `-m "not integration"` | yes — same selection string |
| DESELECTED ×1 | `test_local_password_backend.py:253::test_constant_time_unknown_user_vs_wrong_password` | `@pytest.mark.timing` — excluded by `-m "not timing"` | yes — same selection string |

**The strict xfail is still xfailing.** `test_all_chart_themes_have_same_schema` carries
`strict=True`, so an XPASS would fail the suite. Neither log contains a single `XPASS`; the
light-theme `pf.*` keys are still unauthored and the marker is still telling the truth.

No skip says "Cannot reach Postgres", so every database-backed test really ran (gate 2).

## Failures

Three, all **verdict 3 — A-0's**, and in all three **the test is wrong**, not the product. Nothing
was edited; the fixes below are the next prompt's, to be decided on this report.

### F1 · `tests/web/test_transactions_composer.py:435`

`test_chooser_renders_five_tiles_with_the_shipped_ones_live` — `assert 6 == 5`.

- **What changed.** Not this file: `git log --oneline 3a34537..HEAD -- tests/web/test_transactions_composer.py`
  is empty, so the test is byte-identical to the 09-17 baseline where it passed. The product moved
  under it: **`9581ed0`** (`feat(shell): one section per view … sticky view header …`, UX A-0,
  P-UX-A0b) added

  ```html
  <button type="button" class="pf-view__search" data-pf-palette-open aria-keyshortcuts="Control+K">
  ```

  to `web/templates/areas/_section.html:49` — the Section template every Area body `{% include %}`s.
- **Which is wrong.** The test. The five flow tiles are all still there and all five `hx-get`
  assertions on the previous five lines pass; the sixth button is shell chrome that A0b deliberately
  put on every Section. The assertion's intent — *"MD-1's five flows are the only controls"* — is
  about the chooser, but `_new_section()` (line 359) slices the whole `<section>`, header included,
  so the assertion now reads the shell as well as the composer.
- **Proposed fix.** Narrow the slice to the composer host, keeping the assertion's wording and
  meaning. `_new_section` is defined locally in this file and used by this test only (line 417), so
  the change is contained. Add beneath it:

  ```python
  def _chooser(section: str) -> str:
      """Slice the flow chooser out of the Section, without the view header."""
      start = section.index('<div class="tx-flows">')
      return section[start : section.index("</div>", section.rindex("</button>", start))]
  ```

  and change line 435 to

  ```python
  assert _chooser(section).count("<button") == 5, "MD-1's five flows are the only controls"
  ```

  The three `token not in section` assertions on lines 439–440 (`<form`, `<input`, `<a `) still
  pass against the full Section and can stay as they are.

### F2 · `tests/web/test_transactions_wizard.py:447`

`test_chooser_arms_the_new_instrument_tile` — `assert 6 == 5`. Same cause, same evidence
(`git log 3a34537..HEAD` empty for this file too), same shape of fix: this file has its own
`_new_section` at line 334, used only by this test at line 444.

- **Proposed fix.** The same `_chooser` helper, and line 447 becomes

  ```python
  assert _chooser(section).count("<button") == 5, "MD-1's five tiles are the only controls"
  ```

  Note this test's own docstring already says *"the counts moved with each arming … and this is one
  of the places they are stated"* — the count is intentionally duplicated here, so both sites must
  move together.

### F3 · `tests/tools/test_ux_atlas_reveal.py:554`

`TestShippedScenesFile::test_every_transactions_scene_names_its_section` — the assertion expects
exactly three Transactions scenes; the shipped file now has seven.

- **What changed.** The test arrived with **`88739c3`** (P-UX-A0t); `docs/ux/atlas-scenes.json` was
  then extended by **`b1eced6`** (P-UX-A0s), whose subject says so in as many words: *"atlas scenes
  for Cases and the Transactions composers"*. The four additions are
  `transactions-composer-order`, `transactions-composer-commitment`,
  `transactions-composer-secondary` and `transactions-wizard-step-1` — every one of them carrying
  `"section": "new"`, which is correct: all four photograph a composer inside the New Section.
- **Which is wrong.** The test. A0s shipped the scenes on purpose; A0t's gate command
  (`pytest tests/regression/… tests/tools -q`) was not re-run by A0s, so the stale enumeration
  reached `main`. The comment above the assertion states the contract the test actually cares
  about — *"a scene that lost it would silently photograph the landing Section three times"* — and
  that contract is about the `section` key being present and correct, not about the scene count.
  Hardcoding the map made it a census as well as a contract, and the census went out of date.
- **Proposed fix.** Assert the contract, not the census. Replace lines 554–558 with:

  ```python
  by_name = {scene["name"]: scene["section"] for scene in scenes}
  assert all(by_name.values()), "every Transactions scene names a section"
  assert set(by_name.values()) == {"new", "blotter", "history"}, (
      "the three Transactions Sections are covered, and no scene names a fourth"
  )
  assert by_name["transactions-blotter"] == "blotter"
  assert by_name["transactions-history"] == "history"
  ```

  That keeps the failure mode the comment describes (a scene that lost its `section`, or gained a
  slug that is not a Section) while letting A-1 add composer scenes without editing the test. If the
  operator prefers the census to stay a census, the alternative is to add the four names to the
  literal — but then every future scene addition re-breaks this test, which is what just happened.

### The proposed fixes were checked, not applied

Each fix above was exercised in a scratch script against the real inputs — the F1/F2 helper against
`web/templates/_partials/transactions/_chooser.html` composed into a Section carrying A0b's view
header (6 buttons in the Section, **5** in the chooser slice, and the surviving `<form` / `<input` /
`<a ` assertions still hold), and the F3 assertions against `docs/ux/atlas-scenes.json` as shipped
(all seven scenes name a section; the section set is exactly `{new, blotter, history}`). **No test
file was edited and no fix was applied** — this only removes the risk of handing the next prompt a
fix that does not compile.

**Verdict 4 — unexplained: none.** Every non-pass outcome is classified; no test needed an isolated
rerun.

## Baseline delta

**2026-09-17 → 2026-09-23: selected 5,118 → 5,476 (+358), passed 5,109 → 5,464 (+355).** Passed
grows by 3 fewer than selected, and those 3 are exactly the failures above. The skip/xfail set is
unchanged at 8 + 1, so it contributes nothing to the delta.

This delta is **exact, not reconstructed**: `~/full-suite-2026-09-17/step1.xml` and `step2.xml`
survive on this machine and total 5,118 testcases, matching that report's headline. Every figure
below is a per-file difference between the two runs' JUnit files, not an estimate from
`grep -c 'def test_'`.

**New test files (+350):**

| File | Base → now | Δ | Landed in |
|---|---|---|---|
| `tests/web/test_pf_components.py` | 0 → 139 | +139 | `fca0d37` A0d |
| `tests/web/test_css_tokens.py` | 0 → 85 | +85 | `c83cd7a` A0d2 |
| `tests/tools/test_ux_atlas_reveal.py` | 0 → 63 | +63 | `c8a0800`/`2378788`/`5328291` (P-UX-1…1c) + `88739c3` A0t |
| `tests/web/test_shell_catalogue.py` | 0 → 30 | +30 | `9581ed0` A0b, extended by `ee007e2` A0c |
| `tests/web/test_icons.py` | 0 → 10 | +10 | `e2fd660` A0v |
| `tests/web/test_shell_sections.py` | 0 → 9 | +9 | `9581ed0` A0b |
| `tests/regression/test_shirley_shell_element.py` | 0 → 8 | +8 | `8fb91a8` A0e |
| `tests/web/test_shirley_dock.py` | 0 → 6 | +6 | `8fb91a8` A0e |

**Modified test files (+8):**

| File | Base → now | Δ | Landed in |
|---|---|---|---|
| `tests/regression/test_section_catalogue_matches_body_partials.py` | 6 → 10 | +4 | `ee007e2` A0c |
| `tests/web/test_tenant_users_routes.py` | 40 → 43 | +3 | `ee007e2` A0c (role-aware catalogue) |
| `tests/web/test_static_assets.py` | 10 → 12 | +2 | `e2fd660` A0v |
| `tests/web/test_super_admin_routes.py` | 20 → 21 | +1 | `b1eced6` A0s (the rail test) |
| `tests/web/test_assistants_embedding.py` | 5 → 3 | **−2** | `8fb91a8` A0e (rewritten, net two fewer) |

350 + 8 = **+358. No residual.** Every test the A-0 reports name is present at the count they
claim, and four of their gate figures reproduce exactly against this run:

| A-0 report claim | This run | |
|---|---|---|
| A0c: `test_shell_catalogue.py` → 30 passed | 30 | ✅ |
| A0e: `test_css_tokens.py` stays green at 85 passed | 85 | ✅ |
| A0e: `test_shirley_dock.py` (6 tests), `test_shirley_shell_element.py` (8 tests) | 6, 8 | ✅ |
| A0v: `test_static_assets.py` + `test_icons.py` → 22 passed | 12 + 10 = 22 | ✅ |
| A0s: `tests/regression` + `test_cases_area` + `test_transactions_area` + `test_super_admin_routes` → 218 passed | 164 + 29 + 4 + 21 = 218 | ✅ |

`tests/tools/test_ux_atlas_reveal.py`'s 63 split across eleven classes:
`TestLoadingPlaceholderPattern` 14, `TestBandRects` 8, `TestAdvanceQuietWindow` 7, `TestIsTruncated`
6, `TestPngHeight` 5, `TestManifestEntry` 5, `TestWriteIndexSections` 5, `TestSectionShotStem` 5,
`TestSectionHeading` 4, `TestScenesCarryASection` 3, `TestShippedScenesFile` 1. A0v's gate 3
(`… tests/tools -q` → 46 passed) pins the pre-A0t figure at 6 + 40, so A0t contributed +23 of the 63.

**Two files A-0 modified heavily without changing their test count**, which answers a loose end the
2026-09-17 report left open: `tests/web/test_section_navigation.py` stays at **64** (148 lines
changed) and `tests/web/test_shell_sidebar_and_areas.py` at **66** (12 lines changed). A0b rewrote
assertions inside existing parametrised functions rather than adding functions, so the 09-17
report's speculation about parametrisation amplification does not apply to this interval.

**Step 2 alone: 152 → 164 (+12)** — `test_section_catalogue_matches_body_partials` +4 and the new
`test_shirley_shell_element` +8. Nothing else in `tests/regression` moved.

## What is not running

Seventeen tests did not execute as ordinary passes: **8 skipped, 1 xfailed, 8 deselected** — the
same 8 + 1 + 8 the 2026-09-17 report counted. None is new today. The table below carries one row per
test, seventeen in all; the eight deselected node ids are the eight that
`pytest --collect-only -m "integration or timing"` reports.

| Test (file:line, node id) | Since when | Why (reason string, verbatim) | Covers | Runs in CI? | To bring it back |
|---|---|---|---|---|---|
| `tests/services/analytics/test_limit_coverage.py:1214`<br>`::test_engine_matches_excel_reference_three_dates` | 09-11 (and earlier) | *Reference XLSX not yet provided (expected at …/data/sample/PortfoliFLOW_Limit_Coverage_Reference_v1.xlsx). See Kickoff #2 §6.3.* | Parity of the limit-coverage engine against the hand-validated reference workbook at three stichtage (2020-12-31, 2023-06-30, 2026-03-31), row by row, ±0.01 EUR / ±0.0001 pp | **no** — `data/` is gitignored (`.gitignore:9`), so CI never has it | `cp ~/Code/PortfoliFLOW/"test data"/PortfoliFLOW_Limit_Coverage_Reference_v1.xlsx data/sample/` — **the file is on this machine**. Note the body is a skeleton: `_reference_loader.load_reference` must also be implemented before the test asserts anything |
| `tests/services/test_investment_service_transform_limits_aum.py:146`<br>`::test_v21_roundtrip_landing_investments_aum_and_limits` | 09-17 | *v21 testdata not at …/data/sample/PortfoliFLOW_Testdaten_v21.xlsx; skipping roundtrip.* | v21 workbook → `data_uploads` row → `transform_upload_to_investments` with the Phase-7 repositories wired: landing, investments, AUM and limits end to end | no | `cp ~/Code/PortfoliFLOW/"test data"/PortfoliFLOW_Testdaten_v21.xlsx data/sample/` |
| `tests/services/test_investment_service_transform_limits_aum.py:219`<br>`::test_duplicate_import_raises_limit_validation_error` | 09-17 | *v21 testdata not at …; skipping.* | A second import of the same workbook raises the limit validation error rather than double-writing | no | same copy |
| `tests/web/test_data_import_phase7_wiring.py:86`<br>`::test_web_import_persists_limits_and_anlv_but_not_aum` | 09-17 | *v21 testdata not at …; skipping.* | The web import **write** branch wires the Phase-7 repositories into the service call — limits and AnlV persist, AUM does not | no | same copy |
| `tests/web/test_data_import_phase7_wiring.py:86`<br>`::test_web_dry_run_completes_with_phase7_workbook` | 09-17 | *v21 testdata not at …; skipping.* | The **dry-run** branch accepts a v21 workbook and writes nothing | no | same copy |
| `tests/web/test_data_import_route_benchmarks.py:78`<br>`::test_upload_endpoint_persists_benchmarks_when_sheets_present` | 09-17 | *testdata not at …/PortfoliFLOW_Testdaten_v24.xlsx; skipping.* | v24 workbook → upload endpoint → benchmark tables populated (ADR-0061) | no | `cp ~/Code/PortfoliFLOW/"test data"/PortfoliFLOW_Testdaten_v24.xlsx data/sample/` |
| `tests/web/test_data_import_route_benchmarks.py:78`<br>`::test_upload_endpoint_skips_benchmarks_when_sheets_absent` | 09-17 | *testdata not at …/PortfoliFLOW_Testdaten_v21.xlsx; skipping.* | A pre-benchmark (v21) workbook writes no benchmark rows and raises nothing | no | same v21 copy |
| `tests/web/test_accessibility.py:74`<br>`::test_accessibility_minima_documentation_marker` | 09-11 (and earlier) | *Accessibility minima are validated manually in Phase 2 (ADR-0037 §10). See this module's docstring for the procedure.* | Nothing executable — it is a discoverable marker so `pytest` lists the manual a11y procedure and a future automation pass can fill the body without renaming the file | no — skips everywhere by design | **Deliberate, keep.** Retiring it means writing the automated a11y procedure (ADR-0037 §10) and replacing the body |
| `tests/core/test_chart_theme_alternatives.py:54`<br>`::test_all_chart_themes_have_same_schema` (XFAIL, `strict=True`) | 09-11 (and earlier) | *chart_theme_light.json lacks the ADR-0058 pf.\* web-chrome keys; light-theme values to be authored with the Phase-B theme picker.* | Every shipped chart theme exposes the identical leaf-key set | xfails everywhere | Author `chart_theme_light.json`'s `pf.*` keys with the Phase-B theme picker, then delete the marker. **`strict=True`** means the suite fails the moment the keys land and the marker stays — it will tell you itself |
| `tests/assistants/test_routing_eval.py:178`<br>`::test_routing_selects_expected_tool[get_portfolio_overview0]` | 09-11 (and earlier) | `@pytest.mark.integration`, excluded by the selection string | The live model routes *"how big is the portfolio — what is our total AUM and IRR right now?"* to `get_portfolio_overview` | **no** — CI uses the identical `-m "not integration and not timing"` | `pytest -m integration` **plus** `OPENROUTER_API_KEY` and `SHIRLEY_MODEL` in the environment; without both the test skips itself at line 186. Deliberate — it costs a live external call per case |
| `tests/assistants/test_routing_eval.py:178`<br>`::test_routing_selects_expected_tool[get_saa_configuration]` | 09-11 (and earlier) | `@pytest.mark.integration`, excluded by the selection string | The live model routes *"what does our SAA assume for expected returns across the asset classes?"* to `get_saa_configuration` | **no** — CI uses the identical `-m "not integration and not timing"` | `pytest -m integration` **plus** `OPENROUTER_API_KEY` and `SHIRLEY_MODEL` in the environment; without both the test skips itself at line 186. Deliberate — it costs a live external call per case |
| `tests/assistants/test_routing_eval.py:178`<br>`::test_routing_selects_expected_tool[get_saa_hypothetical_comparison]` | 09-11 (and earlier) | `@pytest.mark.integration`, excluded by the selection string | The live model routes *"would we have done just as well holding the SAA weights instead?"* to `get_saa_hypothetical_comparison` | **no** — CI uses the identical `-m "not integration and not timing"` | `pytest -m integration` **plus** `OPENROUTER_API_KEY` and `SHIRLEY_MODEL` in the environment; without both the test skips itself at line 186. Deliberate — it costs a live external call per case |
| `tests/assistants/test_routing_eval.py:178`<br>`::test_routing_selects_expected_tool[get_investment_data]` | 09-11 (and earlier) | `@pytest.mark.integration`, excluded by the selection string | The live model routes *"give me Investment H's NAV history"* to `get_investment_data` | **no** — CI uses the identical `-m "not integration and not timing"` | `pytest -m integration` **plus** `OPENROUTER_API_KEY` and `SHIRLEY_MODEL` in the environment; without both the test skips itself at line 186. Deliberate — it costs a live external call per case |
| `tests/assistants/test_routing_eval.py:178`<br>`::test_routing_selects_expected_tool[get_limit_coverage]` | 09-11 (and earlier) | `@pytest.mark.integration`, excluded by the selection string | The live model routes *"are we in breach on any investment limit?"* to `get_limit_coverage` | **no** — CI uses the identical `-m "not integration and not timing"` | `pytest -m integration` **plus** `OPENROUTER_API_KEY` and `SHIRLEY_MODEL` in the environment; without both the test skips itself at line 186. Deliberate — it costs a live external call per case |
| `tests/assistants/test_routing_eval.py:178`<br>`::test_routing_selects_expected_tool[get_portfolio_statistics]` | 09-11 (and earlier) | `@pytest.mark.integration`, excluded by the selection string | The live model routes *"is H earning its fee or just paying for beta?"* to `get_portfolio_statistics` | **no** — CI uses the identical `-m "not integration and not timing"` | `pytest -m integration` **plus** `OPENROUTER_API_KEY` and `SHIRLEY_MODEL` in the environment; without both the test skips itself at line 186. Deliberate — it costs a live external call per case |
| `tests/assistants/test_routing_eval.py:178`<br>`::test_routing_selects_expected_tool[get_portfolio_overview1]` | 09-11 (and earlier) | `@pytest.mark.integration`, excluded by the selection string | The live model routes *"here's a new PE fund we're looking at — how does it fit our portfolio?"* to `get_portfolio_overview` | **no** — CI uses the identical `-m "not integration and not timing"` | `pytest -m integration` **plus** `OPENROUTER_API_KEY` and `SHIRLEY_MODEL` in the environment; without both the test skips itself at line 186. Deliberate — it costs a live external call per case |
| `tests/auth/test_local_password_backend.py:253`<br>`::test_constant_time_unknown_user_vs_wrong_password` | 09-11 (and earlier) | `@pytest.mark.timing`, excluded by the selection string | The unknown-user login path is timing-comparable to the wrong-password path (no user enumeration via response time) | **no** — same selection string | ⚠️ **`-m timing` is not enough.** The test carries `@pytest.mark.timing` *and* `@pytest.mark.skip` (line 254, *"timing comparisons are flaky in CI. Kept here for manual verification"*). `pytest -m timing` selects it and the skip marker then skips it; the decorator must be commented out as well. Deliberate — keep, but the two-marker arrangement is worth knowing before someone tries the documented one-liner |

**The workbooks are not lost.** The 2026-09-17 report concluded that "no `PortfoliFLOW_Testdaten_v2*.xlsx`
is anywhere under `$HOME`" and treated the six new skips as possibly permanent. Today's
`find ~ -name 'PortfoliFLOW_Testdaten_v2*'` returns eleven hits. All three files the skips name
exist:

| File the skips want | Found at | sha256 |
|---|---|---|
| `PortfoliFLOW_Testdaten_v21.xlsx` | `~/Code/PortfoliFLOW/test data/` **and** `~/Code/PortfoliFLOW/PortfoliFLOW-old/data/sample/` | `5c162da1…` — **byte-identical in both locations** |
| `PortfoliFLOW_Testdaten_v24.xlsx` | same two locations | `d6a939be…` — **byte-identical in both** |
| `PortfoliFLOW_Limit_Coverage_Reference_v1.xlsx` | `~/Code/PortfoliFLOW/test data/` | `c4ad89c0…` |

So restoring seven of the eight skips is a three-file `cp` into `data/sample/`, which does not exist
in this repo yet. The directory name `test data` contains a space, which is the likeliest reason the
09-17 search missed them. This report does not perform the copy — that is a change to the test
environment and belongs to the operator, and it would in any case have to happen after the run, not
during it.

CI is unaffected either way: `data/` is gitignored, so CI skips these seven on every run regardless.
Restoring them locally means the local suite becomes *stricter* than CI, not equal to it.

## Not blocking, but worth passing on

1. **The three failures are a gate-scope lesson, not a code problem.** Both stale pins were
   reachable from commands the authoring prompts already knew about: A0b changed the shared Section
   template but ran no Transactions test, and A0s edited `atlas-scenes.json` but ran no
   `tests/tools` test — the very command A0v's own gate 3 used. A strand that edits
   `web/templates/areas/_section.html` touches all nine Areas by construction, and a strand that
   edits `docs/ux/atlas-scenes.json` has exactly one test file enumerating it. Both are cheap to
   add to a gate list.
2. **The `<button>` count is asserted in two files that must move together**, and
   `test_transactions_wizard.py`'s docstring says so explicitly. Whoever fixes F1 must fix F2 in the
   same edit or the next full suite reports one of them again.
3. **Seven of eight skips are one `cp` away** (above). Worth an explicit decision now rather than a
   third report describing the same gap: restore the three workbooks to `data/sample/` and accept
   that the local suite is stricter than CI, or record the gap as accepted and stop re-litigating
   it each run. The coverage at stake is the v21 limits/AUM transform roundtrip, the Phase-7 import
   wiring (both branches) and the benchmark import route — none of it exercised on any machine,
   CI included, since the workbooks left `data/sample/`.
4. **Warnings are unchanged at 148 in step 1, 0 in step 2** — the same split as 2026-09-17, to the
   warning:

   | Count | Kind | Where |
   |---|---|---|
   | 135 | numpy / scipy `RuntimeWarning` — DoF ≤ 0 (29), divide-by-zero (29), invalid value in multiply (29), invalid value in divide (19 + 19), catastrophic-cancellation precision loss in `skew` (5) and `kurtosis` (5) | statistics and FX-conversion edge-case tests; `services/analytics/statistics.py:167,197` and numpy internals |
   | 13 | `DeprecationWarning`: `'HTTP_422_UNPROCESSABLE_ENTITY' is deprecated. Use 'HTTP_422_UNPROCESSABLE_CONTENT' instead.` | `fastapi/routing.py:328` (7) and `web/routes/watch_desk.py:3192, :3139, :3414` (1 + 1 + 4) |

   The HTTP-422 deprecation is a one-line-per-site rename in `web/routes/watch_desk.py` and would
   take the count to 7 (the FastAPI-internal ones are not ours). Not urgent; it has been stable for
   three reports.
5. **Runtime is flat.** Step 1 took 2:04:04 against 1:58:15 on 2026-09-17 while running 346 more
   tests — about 6 minutes more for 7% more tests, so per-test cost is essentially unchanged. Step 2
   is 2:17 both times. The slowest calls are the same two real-workbook imports as last time
   (`test_investment_service_import_example_portfolio.py` at 9.37 s and 8.68 s, against 9.43 s and
   9.24 s), and the rest of the `--durations=25` list is 4–6 s `setup`/`teardown` entries. The cost
   is still the per-test database fixture repeated ~5,300 times, not any individual assertion.
6. **The container was already up.** Unlike 2026-09-17, no container start was needed, so this run
   has no cold-start caveat.

## State after the run

- **Schema:** still `b034_add_trade_tickets (head)` — the regression step's per-test scratch
  databases left the service database at head, as designed.
- **Working tree:** unchanged by the run; only this report is new.
- **Local data:** the web and taxonomy suites truncated the dev database, as every DB-bound fixture
  does. **`portfoliflow bootstrap` has not been run** — per this prompt's *Not this prompt's*, the
  re-bootstrap plus one member user is the operator's step before the next browser check or atlas
  run (P-UX-A0b addendum).
- **Container:** left running.

## Differences from CI

- **Python:** 3.13.14 here; CI uses 3.11. A known, documented difference.
- **Postgres:** the long-lived local dev container here, where CI starts a fresh `postgres:16`
  service. The container was already running and healthy.
- **Sample workbooks:** absent from `data/sample/` in both environments, so the seven data-dependent
  skips are expected on CI as well — this run and CI agree. They are *present elsewhere on this
  machine*, which is a local-restoration opportunity, not a current difference.

## Where the logs are

`~/full-suite-2026-09-23/` — `step1.log`, `step2.log`, `step1.xml`, `step2.xml`, `run-meta.txt`
(both exit codes and the end timestamp), `started.txt`, `run.sh`, `run.pid`, `DONE`, `nohup.out`.

The 2026-09-17 logs are still at `~/full-suite-2026-09-17/` and were used for the exact per-file
baseline delta above. Keeping both directories is what made that delta exact rather than estimated;
worth keeping them until the A-1 baseline is taken.

## Deliberate deviations

1. **`run.pid` was corrected after launch.** The prompt's `echo $! > run.pid` recorded the shell
   wrapper's PID (32865) rather than `run.sh`'s (32867), because of how this session's tool harness
   wraps a backgrounded command. The file was overwritten with 32867 so the prompt's own liveness
   check (`ps -p $(cat run.pid)`) would answer correctly. No effect on the run.
2. **The proposed commit message carries no `Co-Authored-By` trailer.** This session's default is to
   append one; the last 30 commits in this repository carry none, and the prompt specifies the
   message verbatim. House style won. Add the trailer at commit time if the operator prefers it.
3. **Nothing else.** The two `pytest` selection strings are byte-for-byte the workflow's, the
   reporting flags are exactly those the prompt lists, no test was edited, no fix was applied, no
   suite was re-run, and no git write was performed.
