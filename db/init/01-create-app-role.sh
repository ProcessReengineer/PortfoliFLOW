#!/bin/bash
# Create the unprivileged application role used by the PortfoliFLOW
# application and by Alembic at runtime.
#
# This role MUST NOT have BYPASSRLS or SUPERUSER. Repository tests
# and the application both connect through this role to ensure RLS
# policies are evaluated as they would be in production.
#
# The Postgres superuser (`postgres`) is reserved for migration runs
# and ad-hoc admin tasks.
#
# This script is executed automatically by the postgres image on FIRST
# container start (via /docker-entrypoint-initdb.d). Subsequent starts
# skip it; a full reset requires `podman compose down -v` to drop the
# named data volume.
#
# DEV ONLY — production deployments must override this password
# before first container start. The init script runs once on the
# first container start (cf. /docker-entrypoint-initdb.d) and is
# not re-applied on subsequent starts, so a literal value here is
# bounded in blast radius. A Phase-5 init-container will read the
# value from the deployment's secret manager and substitute it
# before this script executes.
#
# Since ADR-0124 §1.2 the password and the database name are read from
# the container environment (APP_DB_PASSWORD, POSTGRES_DB), so
# overriding the default no longer requires editing this tracked file.

set -e

: "${APP_DB_PASSWORD:=app_dev_password_change_me}"

# The role needs basic schema usage and connect privileges.
#
# Default privileges cover tables created in future migrations.
# Alembic runs as superuser, so it owns the tables; we grant the app
# role read/write/delete on tables created from now on.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
	CREATE ROLE portfoliflow_app WITH LOGIN PASSWORD '${APP_DB_PASSWORD}';
	GRANT CONNECT ON DATABASE "${POSTGRES_DB}" TO portfoliflow_app;
	GRANT USAGE  ON SCHEMA public TO portfoliflow_app;
	ALTER DEFAULT PRIVILEGES IN SCHEMA public
	  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO portfoliflow_app;
	ALTER DEFAULT PRIVILEGES IN SCHEMA public
	  GRANT USAGE, SELECT ON SEQUENCES TO portfoliflow_app;
SQL
