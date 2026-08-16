# PortfoliFLOW Postgres development database

This directory holds everything needed to run a local Postgres instance
for PortfoliFLOW development: a Compose-managed container, a custom
`postgresql.conf` mount point, the role-bootstrap script that runs on
first start, and the Alembic migration tree.

## Prerequisites

- **Podman** ≥ 4 with the `podman compose` plugin (or `podman-compose`).
  The repo is tested against Podman 5.x rootless. Docker users can run
  the same `compose.yml` with `docker compose` — the file follows the
  engine-neutral Compose Specification.
- A `.env` file at the repo root. Copy `.env.example` and adjust
  passwords if you deploy this anywhere beyond your own machine.

## Starting the database

```bash
podman compose up -d
podman compose ps          # confirm "healthy" status
```

The first start takes ~10 s because the Postgres image initialises
`/var/lib/postgresql/data`, runs the entry-point scripts under
`db/init/`, and then exposes 5432 on `127.0.0.1`. Subsequent starts
take ~1 s.

## Stopping

```bash
podman compose down        # stop the container, KEEP the data volume
podman compose down -v     # stop AND wipe the named data volume
```

`down -v` is the way to get a clean slate (re-runs `db/init/`
scripts, re-applies all Alembic migrations from scratch). Safe on a
development database; never run it against data you want to keep.

## What lives where

| Path | Purpose |
| --- | --- |
| `compose.yml` *(repo root)* | Container definition, port binding, volume mounts, healthcheck. |
| `db/postgresql.conf` | Custom Postgres config overrides, mounted read-only. Currently empty. |
| `db/init/01-create-app-role.sql` | Creates the unprivileged `portfoliflow_app` role on first start. |
| `db/alembic.ini` | Alembic configuration (URL, script location). |
| `db/migrations/` | Alembic `env.py` plus the `versions/` directory of migrations. |

## The `init/` directory

`db/init/` holds the SQL that bootstraps the Postgres *cluster* — as
opposed to the schema, which is Alembic's job (see the division of
labour below). It currently contains a single file.

**What the SQL does.** `01-create-app-role.sql` creates the
unprivileged `portfoliflow_app` login role (no `SUPERUSER`, no
`BYPASSRLS` — that is the point, see *Database roles* below) and gives
it the privileges the application needs:

- `CONNECT` on the `portfoliflow_dev` database and `USAGE` on schema
  `public`;
- `ALTER DEFAULT PRIVILEGES` in `public` granting `SELECT, INSERT,
  UPDATE, DELETE` on **tables created from that point on**, plus
  `USAGE, SELECT` on sequences. Alembic runs as the superuser and
  therefore *owns* every table it creates; the default-privilege grant
  is what makes those tables reachable by the app role without any
  per-migration `GRANT` statement.

The password in the file is a dev-only literal
(`app_dev_password_change_me`) and must be overridden before first
start in any deployment beyond a local machine.

**When it runs.** The `postgres:16` image's entry point executes
everything under `/docker-entrypoint-initdb.d` (into which `compose.yml`
mounts `./db/init` read-only) **exactly once: on the first container
start against an empty data volume**, in filename order — hence the
`01-` prefix. It never re-runs against an existing volume. A container
restart, a `podman compose down` (without `-v`), or an edit to the SQL
file will therefore *not* re-apply it. That is the single most
surprising property of this mechanism: changing a role definition on a
live dev database means either running the SQL by hand against the
running container, or discarding the volume and starting over.

**How to re-trigger it.** Use the reset script:

```bash
./scripts/db-reset.sh
```

It performs the full clean-slate sequence: `podman compose down -v`
(dropping the named data volume) → `podman compose up -d` (empty volume
⇒ the init scripts run) → wait for readiness and assert that the
`portfoliflow_app` role now exists → `alembic upgrade head` →
`portfoliflow bootstrap` (tenants, owner, super-admin, SAA seeds). The
bare `podman compose down -v && podman compose up -d` pair re-runs
`init/` too, but leaves you with an empty schema and no tenants.

Note the distinction from `portfoliflow reset-dev --confirm`
(*Resetting the development database*, below): that command truncates
domain data on the **existing** volume and touches neither `init/` nor
the schema. Only a volume teardown re-runs `init/`.

**Division of labour.** `init/` owns cluster-level bootstrap — roles
and their grants — and nothing else. **Alembic owns all schema**:
migrations under `db/migrations/versions/` are the only path that
creates or changes a table, and `Base.metadata.create_all` is never
used in this project. Keep new DDL out of `init/`; a schema change that
lives here would silently skip every database whose volume already
exists.

## Database roles

Two roles are used:

- **`postgres`** — superuser. Used **only** by Alembic (DDL needs
  ownership) and for ad-hoc admin tasks. The application never connects
  as this role.
- **`portfoliflow_app`** — unprivileged application role. Created by
  `db/init/01-create-app-role.sql`. It does **not** have `BYPASSRLS`
  or `SUPERUSER`, so RLS policies bind on every query — exactly the
  way they will in production. Both the app and the repository test
  suite connect through this role.

The split is deliberate: tests that ran as superuser would silently
skip the RLS layer, masking the very bugs that RLS exists to catch
(see ADR-0035).

## Connection URLs

Both URLs come from `.env`:

```
DATABASE_URL=postgresql+asyncpg://portfoliflow_app:...@localhost:5432/portfoliflow_dev
DATABASE_URL_SUPERUSER=postgresql+asyncpg://postgres:...@localhost:5432/portfoliflow_dev
```

The application and the repository tests use `DATABASE_URL`. Alembic
and the schema-reset fixture use `DATABASE_URL_SUPERUSER`.

## Running migrations

```bash
cd db
alembic upgrade head     # apply all pending migrations
alembic current          # show the active revision
alembic downgrade -1     # roll back one revision
```

Phase 1 forbids forward-only migrations (per ADR-0034 §5). Every
migration ships with a working `downgrade()`.

## Resetting the development database

For a clean slate without losing schema state:

```bash
portfoliflow reset-dev --confirm
```

**Destructive — only safe in development environments.** The command
truncates every domain table (`tenants`, `users`, `sessions`,
`audit_log`, `login_audit`, `data_uploads`, `data_upload_sheets`,
`data_store_entries`) and then reinstalls the sentinel tenant + user
by re-running the bootstrap workflow. `alembic_version` is **not**
touched, so schema migrations stay in place — operators do not need
to run `alembic upgrade head` again afterwards.

`--confirm` is required to prevent accidental data loss; an aborted
run without the flag exits non-zero. The command also refuses to run
when `DATABASE_URL_SUPERUSER` does not target `portfoliflow_dev`,
guarding against a `.env` mistakenly pointing at a non-dev database.

Use this when manual experiments or interrupted tests have left the
dev DB in a half-configured state. For a non-destructive snapshot of
the current state (schema head, sentinel status, user count, AI
service configuration), use `portfoliflow status` instead.

## Troubleshooting

- **`db/init/01-create-app-role.sql` did not run.** The init scripts
  only execute on a *fresh* data volume. Run `podman compose down -v`
  to drop the volume and start over. There is no Phase-1 production
  data, so this is safe.
- **`podman compose up -d` fails with a port-conflict error.** Another
  Postgres instance is bound to 127.0.0.1:5432 (often the system
  package). Stop it (`systemctl stop postgresql`) or change the host
  port in `compose.yml`.
- **Healthcheck never goes green.** `podman compose logs postgres`
  usually shows the cause — typically a syntax error in
  `db/postgresql.conf` or a missing env var in `.env`.

## See also

- ADR-0034 — persistence backend selection (Postgres).
- ADR-0035 — multi-tenant isolation via RLS.
- ADR-0018 — Service / Repository layering (where the app meets the DB).
