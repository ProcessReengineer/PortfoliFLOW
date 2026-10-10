# ADR-0135: The Test Suite's Own Database, Held in RAM

- **Status:** Accepted (2026-10-10)
- **Date:** 2026-10-10
- **Deciders:** PortfoliFLOW project owner
- **Tags:** process, testing, data, developer-experience

---

## Context

Database-backed tests ran against the development database, the PostgreSQL
16 container of `compose.yml`, through `DATABASE_URL` and
`DATABASE_URL_SUPERUSER` in `.env`. An autouse fixture truncates every domain
table before and after each database-backed test (`tests/_db_fixtures.py`),
so a full run issues tens of thousands of truncations and re-seeds. Three
costs followed from sharing that database:

- **Disk wear.** The container keeps its data in the named volume
  `portfoliflow_postgres_data` on the developer's SSD. Every truncation and
  seed is a durable write with its WAL and `fsync`. The suite's write volume
  was large enough to concern the owner about SSD wear.
- **Speed.** The full suite took about two hours forty on the development
  machine (P-WD-B15c, 2026-10-08). The same tests on a tmpfs-backed copy of
  the container ran at roughly four times the rate (P-UX-A6-s2, 2026-10-10:
  3960 tests in 38 minutes).
- **A run destroyed the development data.** Every database-backed run emptied
  the development database, and `portfoliflow bootstrap` had to restore it
  afterwards (`AGENTS.md`, `docs/testing.md`). A browser walk and a test run
  could not share a session.

`compose.yml` is also the installer's database (ADR-0124: `scripts/install.sh`
and the named volume), so it must stay durable.

## Decision

PortfoliFLOW's test suite uses **its own PostgreSQL container with its data
directory on tmpfs**, and never the development database.

1. **`compose.test.yml`** defines it: Compose project `portfoliflow-test`,
   container `portfoliflow-postgres-test`, the same image, `postgresql.conf`
   and `db/init/` script as `compose.yml`, fixed loopback-only test
   credentials, `127.0.0.1:5433` (`POSTGRES_TEST_PORT`), and
   `tmpfs: /var/lib/postgresql/data` capped at 2 GB. No volume. Restart policy
   `always`.
2. **The suite points itself at it.** `tests/conftest.py`, on import, calls
   `select_test_database` (`tests/_test_database.py`). That function writes
   the test URLs into `DATABASE_URL` and `DATABASE_URL_SUPERUSER` in the
   process environment before any test module loads. Every `load_dotenv`
   in the tree keeps an existing variable, and `WebSettings` prefers the
   environment over `.env`. The application under test, the fixtures, the run
   lock and every subprocess therefore see the test database only.
   - The URLs come from `TEST_DATABASE_URL` and `TEST_DATABASE_URL_SUPERUSER`
     (environment, then `.env`; both or neither), else from defaults that
     match `compose.test.yml`.
   - The defaults apply only where a database is configured at all
     (`DATABASE_URL*` set). Elsewhere, such as a bare checkout or the fast CI
     tier, nothing is set and database-bound modules skip, as before.
   - A run whose test URLs name the development database of `.env` (same
     host class, port and database name) stops before collection with a
     `pytest.UsageError`.
3. **The run migrates the database itself.** A tmpfs cluster is empty after
   every start. The pool listener that takes the run lock then runs
   `alembic upgrade head` once per run (`HeadMigration`). A failed
   migration stops the run as a lock refusal does. No bootstrap is needed:
   the tests seed what they use, as in CI.
4. **CI** keeps its single service container. `full-suite.yml` sets the two
   `TEST_DATABASE_URL*` variables to it; the fast tier sets nothing.

## Rationale

- tmpfs removes the disk from the loop entirely. Durability is worthless for
  a database the suite truncates before every test.
- A separate container leaves `compose.yml`, the installer's container,
  durable. It also ends the "a run empties the development database" rule.
- Rebinding the two existing variables, rather than teaching every fixture a
  new name, covers the ~80 modules that read `DATABASE_URL*` directly and the
  application's own settings with one assignment, in one place, before
  collection.
- Migrating on first connection rather than at session start keeps
  database-free runs free of any database contact. They take no lock and pay
  no migration, which keeps the run-lock design of `tests/_run_lock.py`.

## Alternatives Considered

- **tmpfs in `compose.yml` itself.** Rejected: `compose.yml` is the installer's
  database. An installed or development database would lose all its data on
  every container restart.
- **A compose override file layered onto `compose.yml` for test sessions.**
  Rejected: the developer would have to remember to switch the development
  database in and out of RAM. The development data is lost on each switch,
  and tests would still share the database with the browser.
- **Tuning the durable database (`fsync=off`, `synchronous_commit=off`,
  unlogged tables).** Rejected: it reduces writes but does not remove them,
  it leaves the development database unsafe by configuration, and the suite
  would still empty it.
- **Starting the container from the test run.** Rejected for now: pytest
  would have to drive Podman or Docker and know which engine is installed.
  Starting it once per session, or with `podman-restart.service` at boot, is
  enough.

## Consequences

### Positive

- The suite's database writes go to RAM: no SSD wear from tests, and a much
  shorter full run.
- A test run leaves the development database untouched. `portfoliflow
  bootstrap` after a run, and the rule asking for it, are gone.
- The guard makes "tests against the development database" a refusal rather
  than a convention.

### Negative

- One more container to start. A forgotten start shows up as
  `Cannot reach Postgres` skips and some errors, as a stopped development
  container did before.
- The test container takes RAM: about 50 MB idle, bounded at 2 GB by the
  tmpfs cap.
- Each database-backed run pays one `alembic upgrade head` at its first
  connection, about a second at head.

### Neutral / Follow-ups

- Docstrings of older test modules still call the service database "the
  shared dev database". The rationale they record (scratch databases for
  migration roundtrips, truncation lists) holds unchanged for the test
  database.
- `tests/web/test_accessibility.py`'s manual procedure and the browser walks
  keep using the development database, which is where a human looks.

## Implementation Notes

- `compose.test.yml`, `tests/_test_database.py`, `tests/conftest.py`,
  `.github/workflows/full-suite.yml`.
- Documentation: `docs/testing.md` ("The database", "Running the full suite
  locally"), `db/README.md` ("The test database"), `.env.example`,
  `AGENTS.md` (Tests and toolchain).
