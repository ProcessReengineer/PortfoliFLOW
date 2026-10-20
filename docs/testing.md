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
`.venv/bin/pytest`, `.venv/bin/ruff` and
`.venv/bin/pyright --pythonpath .venv/bin/python` directly. Pyright needs
`--pythonpath` when the venv is not activated; otherwise it may resolve
another interpreter and report installed packages as unresolved imports.

## Tiers

| Tier | Where it runs | What it covers |
|---|---|---|
| Fast tier | `.github/workflows/ci.yml` — every push and pull request | `ruff check`, `ruff format --check`, the pyright typing islands, and every test that needs no database. `tests/repositories`, `tests/services`, `tests/auth` and `tests/web` are ignored; any other database-bound test skips itself. `tests/regression` stays in: its purity guards need no database, and only the migration roundtrips skip. |
| Full suite | `.github/workflows/full-suite.yml` — pushes to `main` and manual dispatch | Everything except the two opt-in markers below, against a PostgreSQL 16 service container migrated to head. Two steps; the regression guards and migration roundtrips run last, as the public architectural contract (ADR-0109 §4). |
| `integration` marker | on demand only | Tests that call a live external service (the model routing eval). Deselected by default through `addopts = "-m 'not integration'"` in `pyproject.toml`; run deliberately with `pytest -m integration` and valid credentials. |
| `timing` marker | on demand only | Tests that depend on wall-clock timing and are flaky under shared load. Excluded by the selection string of both workflows. |

Lint, the typing islands and the full suite run once, on Ubuntu 24.04 with
Python 3.11 (the `requires-python` floor). The fast tier runs on the
reference platforms: Ubuntu 24.04 with Python 3.11 and 3.13, and macOS 26
with Python 3.13. Runner images are pinned by name and actions by commit
SHA; `.github/dependabot.yml` proposes action updates weekly. The one
exception is `installer.yml`, which deliberately follows `ubuntu-latest`
and `macos-latest` as an early warning for runner-image drift (see its
header).

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

Database-backed tests run against **their own PostgreSQL container, held in
RAM** (`compose.test.yml`, ADR-0135), never against the development database
of `compose.yml`. Start it once per session; it listens on `127.0.0.1:5433`:

```sh
podman compose -f compose.test.yml up -d      # empty, in RAM
podman compose -f compose.test.yml down       # stop; its data is gone
```

- **The suite points itself at the test database.** Importing
  `tests/conftest.py` sets `DATABASE_URL` and `DATABASE_URL_SUPERUSER` to the
  test container's URLs before any test module loads
  (`tests/_test_database.py`), so the application, the fixtures and every
  subprocess (Alembic, the CLI) see only the test database. The URLs default
  to the values in `compose.test.yml`; `TEST_DATABASE_URL` and
  `TEST_DATABASE_URL_SUPERUSER` (environment or `.env`, both or neither)
  override them. A run whose test URLs name the development database of
  `.env` stops before collection. On a checkout with no `DATABASE_URL*` at
  all, nothing is set and the database-bound modules skip, as in the fast
  CI tier.
- **The run migrates the test database itself.** The container keeps its
  data in tmpfs, so every start is an empty cluster. The first database
  connection of a run, once it holds the run lock, runs
  `alembic upgrade head` (about a second when already at head). A failed
  migration stops the run. No bootstrap is needed: the tests seed what they
  use.
- **A run empties the test database, not the development one.** An autouse
  fixture truncates every domain table before and after each
  database-backed test (`tests/_db_fixtures.py`). The development database
  keeps its data; no `portfoliflow bootstrap` is needed after a run.
- **Why RAM.** Those truncations are the bulk of the suite's database writes.
  On a volume they wear the disk; in tmpfs they cost neither wear nor
  `fsync`, and the database-backed tiers run several times faster.
- **Never run two database-backed pytest processes at once.** The second
  truncates the schema under the first. Typical symptoms are a spurious
  `303` where a `200` was expected (the session the first run created is
  gone) and an `IntegrityError` on `users_tenant_id_fkey` (a tenant row
  vanished between two inserts). This holds for a single-module run as much
  as for the full suite. `tests/conftest.py` enforces it: the first time a
  run opens a database connection, it takes a Postgres advisory lock and
  keeps it until the run ends; a second run stops at its first connection
  with a message naming the run that holds the lock. A run that never opens
  a connection takes no lock. While the container is up, every module under
  `tests/services/` opens one, because the package conftest makes the
  truncating fixture autouse. Other database clients are not covered: do not
  point another database command at the test container during a test run.
  Commands against the development database (`portfoliflow bootstrap`, the
  web server) no longer collide with a run.
- **A skip is not a pass.** With no database configured, database-bound
  modules skip at module level; with the test container not running, most
  database-bound tests skip with a reason that begins `Cannot reach Postgres`
  and some modules error instead. A green run proves the database half only
  if its log contains no such skip. The usual cause is a test container that
  was not started.
- The migration-roundtrip guards in `tests/regression` run on per-test scratch
  databases and leave the service database at head
  (`tests/regression/conftest.py`).

## Running the full suite locally

A full run takes about 38 minutes on the RAM-held test container (37 min 41 s,
2026-10-19; ADR-0135) and took about two hours forty (2 h 39 min, 2026-10-08) on
the development machine while the tests used the disk-backed development
database. Treat it as a session of its own.

1. Start the test container (`podman compose -f compose.test.yml up -d`)
   and confirm that no other pytest process is running (`pgrep -af pytest`
   prints nothing).
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
5. Nothing to restore: the run used the test container, and the
   development database is untouched.

## Deliberately kept skips and expected failures

| Test | Mechanism | Why it stays |
|---|---|---|
| `tests/web/test_accessibility.py::test_accessibility_minima_documentation_marker` | unconditional `pytest.skip` | Accessibility minima are validated manually (ADR-0037 §10). The module docstring holds the procedure; the test keeps it discoverable in pytest's output. |
| `tests/auth/test_local_password_backend.py::test_constant_time_unknown_user_vs_wrong_password` | `@pytest.mark.timing` plus an unconditional `@pytest.mark.skip` | Timing comparisons are flaky under variable load, so the test is kept for manual verification only. `-m timing` alone still skips it; a manual check means lifting the skip locally for that run. |
| `tests/core/test_chart_theme_alternatives.py::test_all_chart_themes_have_same_schema` | `xfail(strict=True)` | `chart_theme_light.json` lacks the ADR-0058 `pf.*` web-chrome keys until the light theme is authored. Being strict, the test turns red on the day it passes; remove the marker then. |

No other test skips for a missing file. Tests that need a realistic book
import the committed `sample_data/PortfoliFLOW_example_portfolio.xlsx`;
tests that need only a valid workbook build one in memory
(`tests/fixtures/workbooks.py`).

## What the suite does not prove

The web tests are ASGI-level: they assert rendered markup, CSS rules and
endpoint shapes. Behaviour that exists only in a browser — fragment-switched
views, keyboard shortcuts, focus handling, HTMX triggers firing — is checked
in an operator browser walk, not by pytest.
