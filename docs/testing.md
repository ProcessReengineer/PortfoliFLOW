<!-- SPDX-License-Identifier: AGPL-3.0-only -->
<!-- Copyright (c) 2025-2026 Sönke Pinkernelle -->

# Testing

How PortfoliFLOW's test suite is organised, how to run it, and the rules that
keep a run trustworthy. This page holds procedure and rules only. It carries
no test counts or per-run figures, because those go stale with the next
commit; a run's own log and JUnit files are the place for them.

The suite lives under `tests/` and runs on pytest with `pytest-asyncio` in
`auto` mode (`[tool.pytest.ini_options]` in `pyproject.toml`). Install the
development extra first: `pip install -e ".[dev]"`. In a local checkout the
tools live in the virtual environment — activate it, or call
`.venv/bin/pytest`, `.venv/bin/ruff` and `.venv/bin/pyright` directly.

## Tiers

| Tier | Where it runs | What it covers |
|---|---|---|
| Fast tier | `.github/workflows/ci.yml` — every push and pull request | `ruff check`, `ruff format --check`, the pyright typing islands, and every test that needs no database. `tests/repositories`, `tests/services`, `tests/auth` and `tests/web` are ignored; any other database-bound test skips itself. `tests/regression` stays in: its purity guards need no database, and only the migration roundtrips skip. |
| Full suite | `.github/workflows/full-suite.yml` — pushes to `main` and manual dispatch | Everything except the two opt-in markers below, against a PostgreSQL 16 service container migrated to head. Two steps; the regression guards and migration roundtrips run last, as the public architectural contract (ADR-0109 §4). |
| `integration` marker | on demand only | Tests that call a live external service (the model routing eval). Deselected by default through `addopts = "-m 'not integration'"` in `pyproject.toml`; run deliberately with `pytest -m integration` and valid credentials. |
| `timing` marker | on demand only | Tests that depend on wall-clock timing and are flaky under shared load. Excluded by the selection string of both workflows. |

CI runs on Python 3.11, the `requires-python` floor.

The selection strings, as the workflows run them:

```sh
# Fast tier (ci.yml)
pytest -m "not integration and not timing" \
  --ignore=tests/repositories --ignore=tests/services \
  --ignore=tests/auth --ignore=tests/web

# Full suite (full-suite.yml), in this order
pytest -m "not integration and not timing" --ignore=tests/regression
pytest -m "not integration and not timing" tests/regression
```

## The database

Database-backed tests run against the development PostgreSQL container
(`compose.yml`) and need `DATABASE_URL` and `DATABASE_URL_SUPERUSER` in `.env`
(see `.env.example`).

- **A run empties the development database.** An autouse fixture truncates
  every domain table before and after each database-backed test
  (`tests/_db_fixtures.py`). After any such run, restore the seed with
  `portfoliflow bootstrap`, and create a member user with
  `portfoliflow create-user` before any browser walk (both commands are
  described in `docs/operator-handbook.md`).
- **Never run two database-backed pytest processes at once.** The second
  truncates the schema under the first. The typical symptom is a spurious
  `303` where a `200` was expected: the session the first run created is
  gone. This holds for a single-module run as much as for the full suite.
- **A skip is not a pass.** With the two URLs unset, database-bound modules
  skip at module level; with the container unreachable, each database-bound
  test skips with a reason that begins `Cannot reach Postgres`. A green run
  proves the database half only if its log contains no such skip.
- The migration-roundtrip guards in `tests/regression` run on per-test scratch
  databases and leave the service database at head
  (`tests/regression/conftest.py`).

## Running the full suite locally

A full run takes about two hours on the development machine; `tests/web`
alone accounts for roughly 85 minutes. Treat it as a session of its own.

1. Start the database container and confirm that no other pytest process is
   running (`pgrep -af pytest` prints nothing).
2. From the repository root, launch the two workflow steps detached, with all
   output written **outside** the repository:

   ```sh
   run_dir=~/full-suite-$(date +%F)
   mkdir -p "$run_dir"
   RUN_DIR="$run_dir" setsid nohup bash -c '
     sel="not integration and not timing"
     .venv/bin/pytest -m "$sel" --ignore=tests/regression -rfEsxX \
       --junitxml="$RUN_DIR/step1.xml" > "$RUN_DIR/step1.log" 2>&1
     echo "step1 exit $?" > "$RUN_DIR/run-meta.txt"
     .venv/bin/pytest -m "$sel" tests/regression -rfEsxX \
       --junitxml="$RUN_DIR/step2.xml" > "$RUN_DIR/step2.log" 2>&1
     echo "step2 exit $?" >> "$RUN_DIR/run-meta.txt"
     touch "$RUN_DIR/DONE"
   ' > /dev/null 2>&1 &
   ```

   Keep the selection strings identical to `full-suite.yml` and add reporting
   flags only. Do not rely on `$!` for the process id — depending on the shell
   it can name a wrapper process; find the run with `pgrep -af pytest`.
3. Run no other pytest while it is going, not even a single module.
4. When `DONE` appears, read both exit codes in `run-meta.txt`, then check the
   logs: no `Cannot reach Postgres` skip and no `XPASS`. JUnit files record an
   expected failure as a skip, so their `skipped` attribute reads one higher
   per xfail than the console summary.
5. Restore the database with `portfoliflow bootstrap`.

## Deliberately kept skips and expected failures

| Test | Mechanism | Why it stays |
|---|---|---|
| `tests/web/test_accessibility.py::test_accessibility_minima_documentation_marker` | unconditional `pytest.skip` | Accessibility minima are validated manually (ADR-0037 §10). The module docstring holds the procedure; the test keeps it discoverable in pytest's output. |
| `tests/auth/test_local_password_backend.py::test_constant_time_unknown_user_vs_wrong_password` | `@pytest.mark.timing` plus an unconditional `@pytest.mark.skip` | Timing comparisons are flaky under variable load, so the test is kept for manual verification only. `-m timing` alone still skips it; a manual check means lifting the skip locally for that run. |
| `tests/core/test_chart_theme_alternatives.py::test_all_chart_themes_have_same_schema` | `xfail(strict=True)` | `chart_theme_light.json` lacks the ADR-0058 `pf.*` web-chrome keys until the light theme is authored. Being strict, the test turns red on the day it passes; remove the marker then. |

The other skips wait for sample workbooks under the untracked `data/sample/`
(and for a limit-coverage reference workbook); they are being resolved in
cleanup strand DC-CL-D.

## Known hazard

Order-dependent teardown can remove the Sentinel tenant inside one shared
pytest process. It shows as an `IntegrityError` on `users_tenant_id_fkey` in a
mixed selection while every module passes on its own. It is under
investigation in DC-CL-D; until it is closed, re-run the affected module alone
before treating such a failure as a defect.

## What the suite does not prove

The web tests are ASGI-level: they assert rendered markup, CSS rules and
endpoint shapes. Behaviour that exists only in a browser — fragment-switched
views, keyboard shortcuts, focus handling, HTMX triggers firing — is checked
in an operator browser walk, not by pytest.
