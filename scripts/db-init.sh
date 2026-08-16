#!/usr/bin/env bash
# scripts/db-init.sh
#
# First-time database initialisation for a fresh PortfoliFLOW checkout.
#
# Behaves like db-reset.sh but assumes the data volume is already empty
# (e.g. fresh clone, or after `podman compose down -v`). Does NOT drop
# anything. Safe to run on a system where the compose stack has never
# been started.
#
# If the compose stack is already running with data, this script will
# refuse to proceed — you want db-reset.sh for that case.
#
# Usage:
#     ./scripts/db-init.sh                       # interactive password
#     ./scripts/db-init.sh --password "test"     # non-interactive
#     ./scripts/db-init.sh --no-bootstrap        # migrations only
#
# Typical workflow on a new dev machine:
#     1. Clone the repo.
#     2. cp .env.example .env  &&  edit secrets.
#     3. python -m venv .venv  &&  source .venv/bin/activate
#     4. pip install -e .
#     5. ./scripts/db-init.sh

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly DEV_DB_NAME="portfoliflow_dev"
readonly COMPOSE_SERVICE="portfoliflow-postgres"

if [[ -t 1 ]]; then
    readonly C_BOLD=$'\033[1m'
    readonly C_GREEN=$'\033[32m'
    readonly C_YELLOW=$'\033[33m'
    readonly C_RED=$'\033[31m'
    readonly C_RESET=$'\033[0m'
else
    readonly C_BOLD="" C_GREEN="" C_YELLOW="" C_RED="" C_RESET=""
fi

log_step()  { echo "${C_BOLD}${C_GREEN}==>${C_RESET} ${C_BOLD}$1${C_RESET}"; }
log_warn()  { echo "${C_YELLOW}!! $1${C_RESET}" >&2; }
log_error() { echo "${C_RED}XX $1${C_RESET}" >&2; }

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

PASSWORD=""
RUN_BOOTSTRAP=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --password)       PASSWORD="$2"; shift 2 ;;
        --no-bootstrap)   RUN_BOOTSTRAP=0; shift ;;
        --help|-h)
            sed -n '/^# Usage:/,/^set /p' "$0" | head -n -2 | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            log_error "Unknown argument: $1"
            exit 2
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------

cd "$REPO_ROOT"

if [[ ! -f .env ]]; then
    log_error ".env not found in $REPO_ROOT"
    log_error "If you have .env.example, copy it: cp .env.example .env"
    exit 1
fi

if [[ -z "${VIRTUAL_ENV:-}" ]]; then
    if [[ -f .venv/bin/activate ]]; then
        # shellcheck source=/dev/null
        source .venv/bin/activate
    fi
fi

for cmd in podman alembic; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        log_error "Required command not found: $cmd"
        exit 1
    fi
done

if [[ $RUN_BOOTSTRAP -eq 1 ]] && ! command -v portfoliflow >/dev/null 2>&1; then
    log_error "portfoliflow CLI not found. Did you run 'pip install -e .' in the venv?"
    exit 1
fi

# ---------------------------------------------------------------------------
# Refuse to proceed if container already has data
# ---------------------------------------------------------------------------

# If the container is running AND has any user tables, this is not a
# first-time init — direct the user to db-reset.sh instead.
if podman ps --format '{{.Names}}' | grep -q "^${COMPOSE_SERVICE}\$"; then
    log_warn "Container '$COMPOSE_SERVICE' is already running. Checking for existing data…"
    if podman exec "$COMPOSE_SERVICE" psql -U postgres -d "$DEV_DB_NAME" \
            -tAc "SELECT COUNT(*) FROM information_schema.tables
                  WHERE table_schema = 'public';" 2>/dev/null \
            | grep -qv '^0$'; then
        log_error "Database '$DEV_DB_NAME' already has tables."
        log_error "This script is for FIRST-TIME init only."
        log_error "If you want a clean slate, use:  ./scripts/db-reset.sh"
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# Step 1 — Start the compose stack
# ---------------------------------------------------------------------------

log_step "Starting postgres container…"
podman compose up -d
echo "OK — container starting."

# ---------------------------------------------------------------------------
# Step 2 — Wait for postgres to be ready
# ---------------------------------------------------------------------------

log_step "Waiting for postgres to be ready…"
deadline=$(( $(date +%s) + 30 ))
until podman exec "$COMPOSE_SERVICE" pg_isready -U postgres -d "$DEV_DB_NAME" >/dev/null 2>&1; do
    if [[ $(date +%s) -gt $deadline ]]; then
        log_error "Postgres did not become ready within 30 seconds."
        log_error "Check logs with:  podman logs $COMPOSE_SERVICE"
        exit 1
    fi
    sleep 1
done
echo "OK — postgres ready."

sleep 1  # let init scripts settle

# ---------------------------------------------------------------------------
# Step 3 — Verify init script ran (app role exists)
# ---------------------------------------------------------------------------

log_step "Verifying init scripts ran…"
if ! podman exec "$COMPOSE_SERVICE" psql -U postgres -d "$DEV_DB_NAME" \
        -tAc "SELECT 1 FROM pg_roles WHERE rolname = 'portfoliflow_app';" \
        | grep -q '^1$'; then
    log_error "portfoliflow_app role was not created."
    log_error "Either db/init/01-create-app-role.sql is missing, or the volume"
    log_error "was not freshly created on container start."
    log_error
    log_error "Try:  ./scripts/db-reset.sh  (drops the volume and re-inits)."
    exit 1
fi
echo "OK — portfoliflow_app role present."

# ---------------------------------------------------------------------------
# Step 4 — Apply migrations
# ---------------------------------------------------------------------------

log_step "Applying Alembic migrations…"
alembic -c db/alembic.ini upgrade head
echo "OK — schema at head."

# ---------------------------------------------------------------------------
# Step 5 — Bootstrap
# ---------------------------------------------------------------------------

if [[ $RUN_BOOTSTRAP -eq 0 ]]; then
    log_warn "Skipping bootstrap (--no-bootstrap)."
else
    log_step "Bootstrapping sentinel tenant, user, and SAA seed data…"

    if [[ -z "$PASSWORD" ]]; then
        echo
        echo "Enter the password for the primary-tenant owner (OWNER_EMAIL / SENTINEL_EMAIL from .env)."
        read -rs -p "Password: " PASSWORD
        echo
    fi

    if [[ -z "$PASSWORD" ]]; then
        log_error "Empty password — bootstrap aborted."
        exit 1
    fi

    echo -n "$PASSWORD" | portfoliflow bootstrap --password-stdin
    echo "OK — bootstrap complete."
fi

# ---------------------------------------------------------------------------
# Step 6 — Final verification
# ---------------------------------------------------------------------------

log_step "Final state:"
podman exec "$COMPOSE_SERVICE" psql -U postgres -d "$DEV_DB_NAME" -c \
    "SELECT 'tenants' AS tbl, COUNT(*)::TEXT AS n FROM tenants
     UNION ALL SELECT 'users',         COUNT(*)::TEXT FROM users
     UNION ALL SELECT 'asset_classes', COUNT(*)::TEXT FROM asset_classes
     UNION ALL SELECT 'investments',   COUNT(*)::TEXT FROM investments;"

echo
log_step "Done. Start the app with:  portfoliflow-web"
