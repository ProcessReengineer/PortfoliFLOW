<!-- SPDX-License-Identifier: AGPL-3.0-only -->
<!-- Copyright (c) 2025-2026 Sönke Pinkernelle -->

# Full-suite report: PortfoliFLOW (AGPL), 2026-09-25 — end of A-1

Run for **P-UX-A1s**, the last prompt of strand UX A-1. Launched first in the
session, detached, on the A1e2 tip; the document work ran beside it with no
`pytest` of any kind invoked in parallel.

---

## OPERATOR ACTION REQUIRED

**None arising from this run.** Nothing failed, so nothing is proposed, and no
file was edited on the strength of it. The commit for this report is in
`docs/reports/P-UX-A1s-report.md`.

The dev DB was truncated by the run, as always — `portfoliflow bootstrap`
before anything visual.

## Verdict

**Green. Zero failures, zero errors.** 5,591 tests were selected;
**5,582 passed, 0 failed, 0 errors**, 8 skipped, 1 xfailed, 8 deselected.
**Step 1 exited 0 and step 2 exited 0**, as at 2026-09-17; the intervening
2026-09-23 run exited 1 on step 1, on three stale pins that are now gone.

**The three failures of 2026-09-23 are gone, and all three were fixed inside
the strand rather than by a housekeeping pass.** A-0 left them as "the next
prompt's to apply"; A-1's own prompts applied them in passing:

1. **`test_transactions_composer.py`** and 2. **`test_transactions_wizard.py`** —
   both asserted `section.count("<button") == 5`, which A0b broke by putting a
   palette-open button into the shared Section head. Both now count inside the
   **chooser** rather than the whole Section
   (`test_transactions_composer.py:447`, `test_transactions_wizard.py:475`),
   which is what the assertion always meant: *MD-1's five tiles are the only
   controls*. The fix rode in on A1b's `chooser_markup` helper — the same
   single-selector move that took the chooser from `.tx-flows` to `.pf-flows`.
3. **`test_ux_atlas_reveal.py`** hard-coded the three Transactions scenes and
   went stale when A0s added four. It now **reads the expected names from
   `docs/ux/atlas-scenes.json`** rather than restating them, so the four scenes
   A-1 went on to add (`composer-secondary-sale`, `wizard-step-3`,
   `history-detail-open`, `blotter-impact-open`) broke nothing. A census became
   a contract.

The two gates A-0 set for its successor both hold: **no test skipped for
"Cannot reach Postgres"** (zero hits in both logs), so the database-bound half
really ran; and the **JUnit totals reconcile with the console** for both steps.
The strict xfail is still xfailing — **zero `XPASS`** in either log.

## What was tested

- **Repo:** `/home/soenke/Code/PortfoliFLOW/PortfoliFLOW`
- **HEAD:** `76fea55` — *"feat(transactions): impact panel on the record's
  projection family … (UX A-1, P-UX-A1e2)"*. The tip subject ends
  `(UX A-1, P-UX-A1e2)`; no docs commit sits on top.
- **Uncommitted changes included in the run:** **none.** `git status
  --porcelain` was empty when the run was launched. The document edits this
  session made landed *after* the launch and are not in the tested tree —
  they touch only `docs/`, so the tested tree and the tree being committed are
  identical in every file the suite reads.
- **Python:** 3.13.14 from `.venv`; pytest 9.0.3; plugins anyio 4.13.0,
  qt 4.5.0, httpx 0.36.2, asyncio 1.3.0 (mode AUTO), timeout 2.4.0.
- **Database:** dev Postgres in the `portfoliflow-postgres` podman container.
  **Found stopped** (`Exited (0)`, two hours idle) and started with
  `podman start`; `select 1` over `DATABASE_URL_SUPERUSER` answered before the
  launch. Alembic head `b034_add_trade_tickets`. Both `DATABASE_URL` and
  `DATABASE_URL_SUPERUSER` present in `.env`.
- **Commands:** the two steps from `.github/workflows/full-suite.yml:74` and
  `:76`, selection strings copied byte-for-byte. The only additions are
  reporting flags — `-v -rfEsxX --durations=25 --junitxml=…`.

  ```sh
  pytest -m "not integration and not timing" --ignore=tests/regression
  pytest -m "not integration and not timing" tests/regression
  ```

  `~/full-suite-2026-09-25/run.sh` is byte-identical to A0f's runner apart from
  the date, verified by `diff` with the two dates normalised.

## Results

| Step | Command | Selected | Passed | Failed / Errors | Skipped | Xfailed | Time |
|---|---|---|---|---|---|---|---|
| 1 | `pytest -m "not integration and not timing" --ignore=tests/regression` | 5,427 | 5,418 | **0** / 0 | 8 | 1 | 2:04:46 |
| 2 | `pytest -m "not integration and not timing" tests/regression` | 164 | 164 | 0 / 0 | 0 | 0 | 0:02:10 |
| **Total** | | **5,591** | **5,582** | **0 / 0** | **8** | **1** | **2:07:04** (00:45:20 → 02:52:24) |

Step 1 collected 5,435 items, deselected 8, selected 5,427. Exit codes, from
`run-meta.txt`: `step1 exit 0`, `step2 exit 0`.

**JUnit reconciliation.** `step1.xml` carries
`tests="5427" failures="0" errors="0" skipped="9" time="7486.307"` and
`step2.xml` `tests="164" failures="0" errors="0" skipped="0" time="129.799"`.

| | Console | XML | Reconciled |
|---|---|---|---|
| Step 1 selected | 5,418 + 8 + 1 = 5,427 | `tests="5427"` | ✅ |
| Step 1 failures | 0 failed | `failures="0"`, `errors="0"` | ✅ |
| Step 1 skipped | 8 skipped + 1 xfailed | `skipped="9"` | ✅ — JUnit records an expected failure as a skip, so the XML reads one higher, as the two earlier reports explain |
| Step 1 time | 7486.35 s | `time="7486.307"` | ✅ |
| Step 2 selected | 164 passed | `tests="164"` | ✅ |
| Step 2 time | 129.80 s | `time="129.799"` | ✅ |

Wall time is within 42 seconds of the 09-23 run's step 1 (7,444 s → 7,486 s)
while carrying 115 more tests, so per-test cost is unchanged and the machine
behaved the same.

## Failures

**None.** The four-verdict triage this report owes therefore classifies nothing:

| Verdict | Count |
|---|---:|
| 1 · pre-existing | 0 |
| 2 · environment | 0 |
| 3 · strand A-1's | 0 |
| 4 · unexplained | 0 |

Verdicts 1 and 2 still apply to the **non-passes** below, which are not
failures; verdicts 3 and 4 would have owed proposed fixes with file and line,
and there are none to write.

## Tests that did not run as normal passes

Seventeen: **8 skipped, 1 xfailed, 8 deselected** — the *same seventeen* the
2026-09-23 and 2026-09-17 reports counted, with the same reasons. Nothing was
added by strand A-1 and nothing resolved.

**Verdict 1 — pre-existing:** every row. **Verdict 2 — environment or
deliberate exclusion:** every row.

| Outcome | Node | Reason (verbatim) | CI skips it too? |
|---|---|---|---|
| SKIPPED | `test_limit_coverage.py::test_engine_matches_excel_reference_three_dates` | Reference XLSX not yet provided (expected at …`data/sample/PortfoliFLOW_Limit_Coverage_Reference_v1.xlsx`). See Kickoff #2 §6.3. | yes |
| SKIPPED | `test_investment_service_transform_limits_aum.py::test_v21_roundtrip_landing_investments_aum_and_limits` | v21 testdata not at …`data/sample/PortfoliFLOW_Testdaten_v21.xlsx`; skipping roundtrip. | yes |
| SKIPPED | `test_investment_service_transform_limits_aum.py::test_duplicate_import_raises_limit_validation_error` | v21 testdata not at …; skipping. | yes |
| SKIPPED | `test_data_import_phase7_wiring.py::test_web_import_persists_limits_and_anlv_but_not_aum` | v21 testdata not at …; skipping. | yes |
| SKIPPED | `test_data_import_phase7_wiring.py::test_web_dry_run_completes_with_phase7_workbook` | v21 testdata not at …; skipping. | yes |
| SKIPPED | `test_data_import_route_benchmarks.py::test_upload_endpoint_persists_benchmarks_when_sheets_present` | testdata not at …`PortfoliFLOW_Testdaten_v24.xlsx`; skipping. | yes |
| SKIPPED | `test_data_import_route_benchmarks.py::test_upload_endpoint_skips_benchmarks_when_sheets_absent` | testdata not at …`PortfoliFLOW_Testdaten_v21.xlsx`; skipping. | yes |
| SKIPPED | `test_accessibility.py::test_accessibility_minima_documentation_marker` | Accessibility minima are validated manually in Phase 2 (ADR-0037 §10). See this module's docstring for the procedure. | yes — deliberate |
| XFAIL | `test_chart_theme_alternatives.py::test_all_chart_themes_have_same_schema` | chart_theme_light.json lacks the ADR-0058 pf.* web-chrome keys; light-theme values to be authored with the Phase-B theme picker. | yes — deliberate |
| DESELECTED ×7 | `test_routing_eval.py::test_routing_selects_expected_tool[…]` | `@pytest.mark.integration` — excluded by `-m "not integration"` | yes — same selection string |
| DESELECTED ×1 | `test_local_password_backend.py::test_constant_time_unknown_user_vs_wrong_password` | `@pytest.mark.timing` — excluded by `-m "not timing"` | yes — same selection string |

**The strict xfail is still xfailing.** `test_all_chart_themes_have_same_schema`
carries `strict=True`, so an XPASS would fail the suite. Neither log contains a
single `XPASS`.

**No skip says "Cannot reach Postgres"**, so every database-backed test really
ran — A0f's gate 2, met.

## Baseline delta

**2026-09-23 → 2026-09-25: selected 5,476 → 5,591 (+115), passed 5,464 →
5,582 (+118).** Passed grows by **three more** than selected, and those three
are exactly the failures A-0 left behind. The skip/xfail set is unchanged at
8 + 1 and contributes nothing.

The delta is **exact, not reconstructed**: both runs' `step1.xml` and
`step2.xml` survive on this machine, and every figure below is a per-file
difference between the two JUnit files.

**One new test file (+41):**

| File | Base → now | Δ | Landed in |
|---|---|---:|---|
| `tests/core/test_decimal_input.py` | 0 → 41 | +41 | `c23d910` A1d |

Its 41 are 28 notation cases (`_CASES`), 11 "was it read" cases and two
property tests, over four functions. A1d's own report calls this "41", which
is the collected count, not the length of `_CASES` — worth noting, because the
tuple has 28 entries and a reader counting rows would get a different number.

**Modified test files (+74):**

| File | Base → now | Δ | Landed in |
|---|---|---:|---|
| `tests/web/test_pf_components.py` | 139 → 163 | +24 | `bb9b1de` A1c +4, `c23d910` A1d +8, `76fea55` A1e2 +12 |
| `tests/web/test_transactions_wizard.py` | 26 → 38 | +12 | `bb9b1de` A1c +5, `712bef9` A1c2 +6, `c23d910` A1d +1 |
| `tests/web/test_transactions_composer.py` | 35 → 46 | +11 | `6e85297` A1b +7, `c23d910` A1d +4 |
| `tests/web/test_transactions_history.py` | 23 → 28 | +5 | `cae50f1` A1a +3, `8a907cd` A1e +2 |
| `tests/web/test_transactions_secondary_sale.py` | 13 → 18 | +5 | `6e85297` A1b2 +4, `c23d910` A1d +1 |
| `tests/web/test_transactions_blotter.py` | 21 → 25 | +4 | `cae50f1` A1a +3, `8a907cd` A1e +1 |
| `tests/web/test_transactions_secondary_buy.py` | 13 → 16 | +3 | `6e85297` A1b2 +3 |
| `tests/web/test_transactions_commitment.py` | 10 → 13 | +3 | `6e85297` A1b2 +3 |
| `tests/web/test_transactions_impact.py` | 17 → 20 | +3 | `76fea55` A1e2 +3 |
| `tests/web/test_transactions_negative_cash.py` | 8 → 10 | +2 | `76fea55` A1e2 +2 |
| `tests/web/test_transactions_area.py` | 4 → 5 | +1 | `cae50f1` A1a +1 |
| `tests/web/test_super_admin_routes.py` | 21 → 22 | +1 | `8a907cd` A1e +1 |

41 + 74 = **+115. No residual.** Every per-commit split above is an exact
static count of test functions × parametrised cases at that commit, and each
file's endpoint reproduces its JUnit total, so the attribution is measured
rather than read off the reports.

**Per prompt**, which is how the strand's own claims are checked:

| Prompt | Δ | The report claimed | |
|---|---:|---|---|
| A0g (inside `cae50f1`) | **0** | test-only, pins moved not added | ✅ |
| A1a | **+7** | 3 blotter + 3 history + 1 area | ✅ |
| A1b | **+7** | "Added, seven tests" | ✅ |
| A1b2 | **+10** | "Added, ten tests across the three modules" | ✅ |
| A1c | **+9** | 5 wizard tests + 4 names in `_EXPECTED_CLASSES` | ✅ |
| A1c2 | **+6** | "one helper and six tests were added" | ✅ |
| A1d | **+55** | "41 + 6 + 3 = **50**" | ⚠️ under by 5 |
| A1e | **+4** | 1 blotter + 2 history + 1 super-admin | ✅ |
| A1e2 | **+17** | 3 impact + 2 negative-cash + 2 new in `test_pf_components` + 10 class names | ✅ |
| | **+115** | | |

**A1d is the one discrepancy, and it is an under-count, not an error.** Its
three "template assertions" in `test_pf_components.py` collect as **eight**:
`test_every_amount_input_has_its_reading_slot` is parametrised over the six
templates that carry amount inputs, and the report counted the function rather
than its cases. 41 + 6 + 8 = 55.

**Step 2 alone: 164 → 164.** `tests/regression` is byte-unchanged across the
strand — A-1 touched no migration, no section catalogue and no shell element,
and the regression suite says so.

## What is not running

Unchanged from 2026-09-23, and the A0g finding that qualifies it still stands:
**seven of the eight skips are not a data loss.** The three workbooks they look
for (`PortfoliFLOW_Testdaten_v21.xlsx`, `…_v24.xlsx`) are present at
`~/Code/PortfoliFLOW/test data/`, but they are **superseded** by
`sample_data/PortfoliFLOW_example_portfolio.xlsx`, which is *tracked in the
repository* and is what the current import tests use. A0f asked "can the seven
skips be recovered by copying?" and answered yes; A0g asked "should they be?"
and answered no. Copying the three files would resurrect tests written against
a workbook format the product has moved past.

The **eighth** skip is different and is the one still worth closing: the
limit-coverage reference stub
(`test_engine_matches_excel_reference_three_dates`) waits for a reference XLSX
that has never been produced (Kickoff #2 §6.3). It is a missing *artefact*, not
a superseded one, and it is the only test in the tree that would check the
limit engine against an independently computed answer.

| | 09-17 | 09-23 | 09-25 |
|---|---:|---:|---:|
| Skipped | 8 | 8 | 8 |
| Xfailed | 1 | 1 | 1 |
| Deselected | 8 | 8 | 8 |

## Not blocking, but worth passing on

1. **`PyQt6 6.11.0 -- Qt runtime 6.11.0` is printed in the session header, and
   neither package is a declared dependency.** `pytest-qt` 4.5.0 and PyQt6
   6.11.0 are both installed in `.venv`, and **neither appears in
   `pyproject.toml`** — not in `[project.dependencies]`, not in the `dev`
   extra, not anywhere; there is no `requirements*.txt` either. They are
   leftovers from before ADR-0094 Stage 1, surviving because the venv was
   never rebuilt.

   Two things follow, and the second is the one that matters. The banner is
   the plugin talking, not the product, so it is merely misleading — a fresh
   `pip install -e '.[dev]'` would not install either package and CI's header
   does not print it. But it also means **this machine cannot detect a
   reintroduced PyQt6 import the way CI would**: a stray `import PyQt6` in
   product code would resolve locally and fail only in CI.
   `tests/regression/test_ai_service_core_qt_free.py` covers one module by
   reading source rather than by import, so it is unaffected — the exposure is
   anything that guard does not name. Rebuilding the venv is the close; it is
   not this prompt's.
2. **148 warnings**, unchanged in character from the previous runs. All come
   from four modules — `test_analysis_tools`, `test_statistics_fx_conversion`,
   `test_statistics_service`, `test_statistics_section_routes` — plus
   `test_watch_desk`, and are numpy `RuntimeWarning`s about degenerate
   covariance slices. **None is raised by a Transactions module.**
3. **The slowest test is 10.5 s** —
   `test_example_portfolio_cash_positions_and_money_market_land_correctly`,
   a real Excel import. The top of the `--durations=25` list is import tests
   and fixture setup, not anything A-1 touched; the strand's own modules
   appear only at 5.2–5.5 s of *setup*, which is the tenant fixture.

## State after the run

- The dev DB was truncated repeatedly, as every DB-backed run does.
  `portfoliflow bootstrap` restores it.
- `git status --porcelain` at the moment of launch was empty; the tree is
  otherwise untouched by the run itself.
- No file was edited on the strength of this run, because nothing failed.

## Differences from CI

None material. The two selection strings are byte-identical to
`.github/workflows/full-suite.yml`; the added flags are reporting only. CI runs
the same 8 deselections (the marks are in the selection string) and would skip
the same 8 tests, since the workbooks are absent there too.

## Where the logs are

`~/full-suite-2026-09-25/` — gitignored by location, not committed:

| File | |
|---|---|
| `run.sh` | the runner, A0f's with today's date |
| `started.txt`, `run-meta.txt`, `DONE` | `2026-09-25T00:45:20+02:00`; `step1 exit 0`, `step2 exit 0`, `2026-09-25T02:52:24+02:00` |
| `step1.log` / `step1.xml` | 615,686 B / 748,303 B |
| `step2.log` / `step2.xml` | 22,933 B / 23,965 B |
| `run.pid` | `4599` — see the deviation below |

## Deliberate deviations

1. **The Postgres container was started rather than treated as a STOP.** It
   existed and had exited cleanly two hours earlier, so `podman ps` was empty
   and `pg_isready` answered nothing. A stopped container is not the condition
   that check guards against; `podman start` plus a `select 1` restored it in
   seconds.
2. **`run.pid` was written after the fact, not from `$!`.** A0f recorded that
   `$!` after `setsid nohup script &` gives the script; in this harness it gave
   the harness's own `bash -c` wrapper, because the whole compound command is
   what gets backgrounded. The PID was recovered with `pgrep -af run.sh` and
   confirmed with `ps -p 4599 -o cmd=` → `bash ./run.sh`, about ten seconds
   after launch. The run was never at risk; only the file was briefly wrong.
