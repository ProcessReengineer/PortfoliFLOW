# Full-suite report: PortfoliFLOW (AGPL), 2026-09-17

**Type:** test run and report · **No code change.** The only file this session writes is this report.

## OPERATOR ACTION REQUIRED

Claude Code performed no git writes. Nothing is staged; nothing is committed.

```sh
git add docs/reports/full-suite-2026-09-17-report.md
git commit -m "docs(reports): full-suite baseline 2026-09-17 after SB-5 and SB-3b, 5,109 passed at 3a34537"
```

Note that three files from the previous housekeeping strand (PB-H5) are still uncommitted in the
working tree and are **part of what was tested** — see *What was tested* below. They carry their
own proposed commit in `docs/reports/PB-H5-report.md` and are deliberately left alone here.

## Verdict

**Green.** All 5,118 selected tests ran: **5,109 passed**, with **0 failures and 0 errors**. Both
CI steps exited with code **0**. The rerun-once flake exception is closed in `full-suite.yml`, so
each result comes from a single pass and no retry was available or needed.

Two expectation deviations, both benign and neither a test failure: the suite ran **21 tests more**
than projected (all attributable, see *Baseline delta*), and **6 tests skipped that passed on
2026-09-11** because the untracked sample workbooks they read are no longer on this machine (see
*Tests that did not run as normal passes*).

## What was tested

- **Repo:** `/home/soenke/Code/PortfoliFLOW/PortfoliFLOW`
- **HEAD:** `3a34537` — `docs(provider-channel): record B-D-13/15/17 addenda for the fetch client, §6 lessons, §7 coordinates post-SB-3b, #067 progress and change-log row (PB-D5)`.
  The tip subject ends `(PB-D5)`, not `(PB-H5)`: PB-H5 was written but never committed.
- **Uncommitted changes included in the run** (`git status --porcelain`, unchanged by the run):

  | Path | Change |
  |---|---|
  | `pyproject.toml` | ` M` — PB-H5: `services/provider_channel` and `services/provider_directory` added to the `[tool.pyright]` island set |
  | `tests/services/provider_channel/test_directory.py` | ` M` — PB-H5: `dict[str, Any]` annotation, one `# pyright: ignore[reportIndexIssue]`, one comment |
  | `docs/reports/PB-H5-report.md` | `??` — PB-H5's own report |

  Both code edits are typing-only and change no assertion, so the tree under test behaves exactly
  as a committed PB-D5 would. If PB-H5 is committed as it proposes, the committed tree is exactly
  what was tested here.
- **Python:** 3.13.14 from `.venv`.
- **Database:** dev Postgres in the `portfoliflow-postgres` podman container (found `Exited (0)`,
  started for this run; `pg_isready` after 2 s), at Alembic head `b034_add_trade_tickets`. Both
  `DATABASE_URL` and `DATABASE_URL_SUPERUSER` present in `.env`.
- **Commands:** the two steps from `.github/workflows/full-suite.yml` with the same test
  selection. The only additions are reporting flags — `-v -rfEsxX --durations=25 --junitxml=…`.
  The selection strings themselves are byte-for-byte CI's.

## Results

| Step | Command | Selected | Passed | Failed / Errors | Skipped | Xfailed | Time |
|---|---|---|---|---|---|---|---|
| 1 | `pytest -m "not integration and not timing" --ignore=tests/regression` | 4,966 | 4,957 | 0 / 0 | 8 | 1 | 1:58:15 |
| 2 | `pytest -m "not integration and not timing" tests/regression` | 152 | 152 | 0 / 0 | 0 | 0 | 0:02:17 |
| **Total** | | **5,118** | **5,109** | **0 / 0** | **8** | **1** | **2:00:42** (12:48:47 → 14:49:29) |

Exit codes, from `run-meta.txt`: `step1 exit 0`, `step2 exit 0`.

The JUnit XML totals agree with the console summaries: `step1.xml` carries
`tests="4966" failures="0" errors="0" skipped="9" time="7095.537"` and `step2.xml`
`tests="152" failures="0" errors="0" skipped="0" time="137.334"`. The XML's `skipped="9"` is the
8 skips plus the 1 xfail — JUnit records an expected failure as a skip, which is why it reads one
higher than the console's `8 skipped`. Neither log contains a single `FAILED` or `ERROR` line.

## Tests that did not run as normal passes

All of these are skipped, expected to fail, or excluded on purpose.

**Skipped (8)** — six more than on 2026-09-11, and all six are the same cause:

| File:line | Reason | vs. baseline |
|---|---|---|
| `tests/services/analytics/test_limit_coverage.py:1214` | Reference XLSX not provided (`data/sample/PortfoliFLOW_Limit_Coverage_Reference_v1.xlsx`) | same as baseline |
| `tests/web/test_accessibility.py:74` | Accessibility minima are validated manually (ADR-0037 §10) | same as baseline |
| `tests/services/test_investment_service_transform_limits_aum.py:146` | v21 testdata absent | **new** |
| `tests/services/test_investment_service_transform_limits_aum.py:219` | v21 testdata absent | **new** |
| `tests/web/test_data_import_phase7_wiring.py:86` (×2) | v21 testdata absent | **new** |
| `tests/web/test_data_import_route_benchmarks.py:78` | v24 testdata absent | **new** |
| `tests/web/test_data_import_route_benchmarks.py:78` | v21 testdata absent | **new** |

The six new skips are a **local-data** difference, not a code regression. `data/` is gitignored
(`.gitignore:9`), `data/sample/` does not exist on this machine any more, and no
`PortfoliFLOW_Testdaten_v2*.xlsx` is anywhere under `$HOME`. All three test files are byte-identical
to the baseline commit (`git diff 2a2be75..HEAD` lists none of them), so the tests did not change —
their input vanished. Because the workbooks are untracked, **CI skips these six on every run too**,
which makes today's 8 skips the CI-shaped number and the 2026-09-11 run of 2 the outlier: that
machine still had the workbooks in `data/sample/`.

No skip says "Cannot reach Postgres", so every database-backed test really ran.

**Expected failure (1):** `tests/core/test_chart_theme_alternatives.py::test_all_chart_themes_have_same_schema`
— `chart_theme_light.json` still lacks the ADR-0058 `pf.*` web-chrome keys. Same as baseline.

**Deselected (8), same as CI and baseline:**

- 7 routing-eval tests in `tests/assistants/test_routing_eval.py`, marked `integration` because
  they call a live external service.
- 1 timing test, `tests/auth/test_local_password_backend.py::test_constant_time_unknown_user_vs_wrong_password`.

## Warnings

**148 in step 1, 0 in step 2** (baseline: 161 and 0). None affected a result. Two kinds only —
the three-kind list of 2026-09-11 has become a two-kind list:

| Count | Kind | Where |
|---|---|---|
| 135 | numpy / scipy `RuntimeWarning` — degrees of freedom ≤ 0, divide by zero, invalid value in divide/multiply, catastrophic-cancellation precision loss in `skew`/`kurtosis` | edge-case tests for statistics and FX conversion; `services/analytics/statistics.py:167,197` and numpy internals |
| 13 | `DeprecationWarning`: `'HTTP_422_UNPROCESSABLE_ENTITY' is deprecated. Use 'HTTP_422_UNPROCESSABLE_CONTENT' instead.` | `web/routes/watch_desk.py` and `fastapi/routing.py` |
| **0** | **pytest warnings about `@pytest.mark.asyncio` on non-async tests** | **gone — see below** |

**The 13-vs-0 statement.** The 13 pytest warnings are **0**. This is the observed number, from a
run with Postgres up and all 20 tenant-resolution tests actually executing — the confirmation
PB-H3 could not obtain on 2026-09-13, when Postgres was down and the file reported `20 skipped,
0 warnings`. The module-level `pytestmark = pytest.mark.asyncio` that produced them (line 80 of
`tests/services/test_tenant_resolution.py` at the baseline commit) is gone from the tree; the
removal reached `main` in `cebef13`. The arithmetic closes cleanly: 161 − 13 = 148, consistent
with the numpy and HTTP-422 populations being unchanged — the 2026-09-11 report named its three
kinds but split out only the 13, so today's 135/13 cannot be diffed against it per kind. The item
is settled affirmatively.

## State after the run

- **Schema:** still `b034_add_trade_tickets (head)` — the regression step's per-test scratch
  databases left the service database at head, as designed.
- **Working tree:** unchanged by the run — the same three PB-H5 paths, nothing else.
- **Local data:** the web and taxonomy suites cleared it (0 users, 0 investments after the run).
  `portfoliflow bootstrap` was run and succeeded: 2 tenants, 2 users, region memberships, the
  market-data system actor and schedule (disabled), the Irene schedule (enabled) and the two
  watchpoint singletons (freshness, liquidity). **Investments are still 0** — re-import a workbook
  before using the app locally; the only workbook on the machine is
  `sample_data/PortfoliFLOW_example_portfolio.xlsx`.
- **Container:** left running, per the handover.

## Differences from CI

- **Python:** 3.13.14 here; CI uses 3.11. A known, documented difference.
- **Postgres:** the long-lived local dev container here, where CI starts a fresh `postgres:16`
  service. The container had been stopped for 21 hours and was started for this run.
- **Sample workbooks:** neither environment has them, so the six data-dependent skips above are
  expected on CI as well. This run and CI agree; the 2026-09-11 run did not.

## Baseline delta

**2026-09-11 → 2026-09-17: selected 4,930 → 5,118 (+188), passed 4,927 → 5,109 (+182).** The
passed figure grows by 6 fewer than selected because the six workbook tests moved from pass to
skip. Every one of the +188 is attributable:

| Source | Tests | Evidence |
|---|---|---|
| SB-5 — sealed box and export envelope | +54 | `tests/services/provider_channel` 101 → 155 |
| SB-3b — fetch client, cache, provenance | +89 | `tests/services/provider_directory`, a new package |
| SB-3b — operator commands | +24 | `tests/cli/test_directory_commands.py`, a new file |
| P-UX-era commits (section-catalogue heading work and its siblings) | +21 | nine existing test files modified since the baseline; +4 of these land in step 2 |
| | **+188** | |

The P-UX residual is the +21 the run exceeded the projected 5,097 by. Its step-2 half is exact and
independently visible: `tests/regression` went 148 → 152, which is precisely the growth of
`test_section_catalogue_matches_body_partials.py` (2 → 6 test functions). The remaining +17 sit in
five `tests/web` files, amplified by Area parametrisation — `test_section_navigation.py` now
expands 16 functions to 64 tests and `test_shell_sidebar_and_areas.py` 13 to 66, so one new
function there is worth several tests. A file-by-file split of those 17 would need the baseline
tree checked out, which this session did not do.

## Not blocking, but worth passing on

1. **The six lost workbook tests.** They are silent: they skip, the suite stays green, and the
   coverage they provided — the v21 limits/AUM transform roundtrip, the Phase-7 import wiring and
   the benchmark import route — is simply not being exercised on any machine, CI included, and has
   not been since the workbooks left `data/sample/`. Worth a decision: restore the workbooks
   somewhere durable, or accept the gap explicitly.
2. **PB-H5 is still uncommitted** two prompts later, and the pyright island set it adds is
   therefore not yet in force in CI. Its proposed commit is in `docs/reports/PB-H5-report.md`.
3. **The rerun-once exception is retired in the workflow**, resolving the "worth passing on" item
   from the 2026-09-11 report: `full-suite.yml:11-14` now records both signatures as closed — the
   roundtrip downgrade flake ended with the scratch databases, the AI-service singleton flake with
   `services/ai_service.py` (ADR-0094). No further action.
4. **Step 1 is 19 minutes faster than on 2026-09-11** (1:58:15 vs 2:17:54) while running 184 more
   tests. Nothing was tuned; likely machine conditions. No flaky-looking retries appear in either
   log.
5. **Slowest tests** (`--durations=25`) are unremarkable and dominated by fixture setup/teardown,
   not assertions: the two slowest calls are
   `test_investment_service_import_example_portfolio.py` at 9.43 s and 9.24 s (real workbook
   import), then a long tail of ~3–7 s `tests/web` setup/teardown entries. No single test
   dominates the two hours; the cost is the per-test database fixture, repeated ~5,000 times.

## Where the logs are

`~/full-suite-2026-09-17/` — `step1.log`, `step2.log`, `step1.xml`, `step2.xml`, `run-meta.txt`
(exit codes and end time), `started.txt`, `run.pid`.
