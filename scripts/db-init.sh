#!/usr/bin/env bash
# scripts/db-init.sh
#
# First-time database initialisation for a fresh PortfoliFLOW checkout.
#
# Behaves like db-reset.sh but assumes the data volume is already empty
# (e.g. fresh clone, or after `compose down -v`). Does NOT drop
# anything. Safe to run on a system where the compose stack has never
# been started.
#
# If the compose stack is already running with data, this script will
# refuse to proceed — you want db-reset.sh for that case.
#
# Usage:
#     ./scripts/db-init.sh                          # interactive password
#     ./scripts/db-init.sh --password "test"        # non-interactive
#     ./scripts/db-init.sh --password-stdin         # password read from stdin
#     ./scripts/db-init.sh --no-bootstrap           # migrations only
#     ./scripts/db-init.sh --engine docker          # force a container engine
#
# Container engine (ADR-0124 §1.1):
#     Podman and Docker are both supported. The engine is resolved in
#     order: --engine podman|docker, then the PORTFOLIFLOW_ENGINE
#     environment variable, then podman on PATH, then docker on PATH.
#     A resolved engine with no Compose provider is a hard error naming
#     the package that would fix it — never a silent fallback.
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
# Container engine resolution (ADR-0124 §1.1)
# ---------------------------------------------------------------------------
#
# Resolves the engine and its Compose provider exactly once, into two
# indexed arrays; every call site below goes through those and never
# names an engine directly.
#
#     ENGINE_CMD   e.g. (podman)          or (docker)
#     COMPOSE_CMD  e.g. (podman compose)  or (docker compose)
#
# Order: --engine podman|docker, then $PORTFOLIFLOW_ENGINE, then podman
# on PATH, then docker on PATH. Podman first preserves the repo's stated
# preference (rootless by default) without excluding Docker. Every
# failure is a hard error naming the cause and the remedy — there is no
# silent fallback between engines or providers.

resolve_engine() {
    local requested="" origin=""

    if [[ -n "$ENGINE_FLAG" ]]; then
        requested="$ENGINE_FLAG"
        origin="--engine"
    elif [[ -n "${PORTFOLIFLOW_ENGINE:-}" ]]; then
        requested="$PORTFOLIFLOW_ENGINE"
        origin="PORTFOLIFLOW_ENGINE"
    fi

    if [[ -n "$requested" ]]; then
        if [[ "$requested" != "podman" && "$requested" != "docker" ]]; then
            log_error "$origin names an unknown container engine: '$requested'."
            log_error "Supported values are 'podman' and 'docker'."
            exit 2
        fi
        if ! command -v "$requested" >/dev/null 2>&1; then
            log_error "$origin requested '$requested', but '$requested' is not on PATH."
            log_error "Install $requested, or drop $origin to use whichever engine is present."
            exit 1
        fi
        ENGINE_CMD=("$requested")
    elif command -v podman >/dev/null 2>&1; then
        ENGINE_CMD=(podman)
    elif command -v docker >/dev/null 2>&1; then
        ENGINE_CMD=(docker)
    else
        log_error "No container engine found. PortfoliFLOW needs Podman or Docker"
        log_error "to run its PostgreSQL 16 container."
        log_error "  Podman (rootless, the repo default): https://podman.io/docs/installation"
        log_error "  Docker:                              https://docs.docker.com/engine/install/"
        exit 1
    fi

    # Compose-provider probe for the resolved engine; first that answers wins.
    if [[ "${ENGINE_CMD[0]}" == "podman" ]]; then
        if podman compose version >/dev/null 2>&1; then
            COMPOSE_CMD=(podman compose)
        elif podman-compose --version >/dev/null 2>&1; then
            COMPOSE_CMD=(podman-compose)
        else
            log_error "Podman is installed, but no Compose provider is available."
            log_error "Podman ships no Compose implementation of its own — 'podman compose'"
            log_error "delegates to one that must be installed separately. Install either:"
            log_error "  - podman-compose            (pip install podman-compose, or your package manager)"
            log_error "  - the Docker Compose plugin (package 'docker-compose-plugin')"
            log_error "Or use Docker instead:  $0 --engine docker"
            exit 1
        fi
    else
        if docker compose version >/dev/null 2>&1; then
            COMPOSE_CMD=(docker compose)
        elif docker-compose --version >/dev/null 2>&1; then
            COMPOSE_CMD=(docker-compose)
        else
            log_error "Docker is installed, but no Compose provider is available."
            log_error "Install the Docker Compose plugin (package 'docker-compose-plugin'),"
            log_error "or the standalone 'docker-compose' binary."
            log_error "  https://docs.docker.com/compose/install/"
            exit 1
        fi
    fi
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

PASSWORD=""
PASSWORD_GIVEN=0
PASSWORD_STDIN=0
RUN_BOOTSTRAP=1
ENGINE_FLAG=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --password)       PASSWORD="$2"; PASSWORD_GIVEN=1; shift 2 ;;
        --password-stdin) PASSWORD_STDIN=1; shift ;;
        --no-bootstrap)   RUN_BOOTSTRAP=0; shift ;;
        --engine)         ENGINE_FLAG="$2"; shift 2 ;;
        --help|-h)
            # Renders the header block from '# Usage:' to the first
            # non-comment line. awk rather than `head -n -N`, which BSD
            # head rejects (ADR-0124 §1.4).
            awk '/^# Usage:/ { f = 1 }
                 f && !/^#/  { exit }
                 f           { sub(/^# ?/, ""); print }' "$0"
            exit 0
            ;;
        *)
            log_error "Unknown argument: $1"
            exit 2
            ;;
    esac
done

if [[ $PASSWORD_GIVEN -eq 1 && $PASSWORD_STDIN -eq 1 ]]; then
    log_error "--password and --password-stdin cannot be combined."
    exit 2
fi

# Read the owner password from stdin so no secret appears on a command
# line (the installer of ADR-0124 §2 pipes it in).
if [[ $PASSWORD_STDIN -eq 1 ]]; then
    IFS= read -r PASSWORD || true
fi

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

for cmd in alembic; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        log_error "Required command not found: $cmd"
        exit 1
    fi
done

resolve_engine
log_step "Container engine: ${ENGINE_CMD[*]} (compose: ${COMPOSE_CMD[*]})"

if [[ $RUN_BOOTSTRAP -eq 1 ]] && ! command -v portfoliflow >/dev/null 2>&1; then
    log_error "portfoliflow CLI not found. Did you run 'pip install -e .' in the venv?"
    exit 1
fi

# ---------------------------------------------------------------------------
# Refuse to proceed if container already has data
# ---------------------------------------------------------------------------

# If the container is running AND has any user tables, this is not a
# first-time init — direct the user to db-reset.sh instead.
if "${ENGINE_CMD[@]}" ps --format '{{.Names}}' | grep -q "^${COMPOSE_SERVICE}\$"; then
    log_warn "Container '$COMPOSE_SERVICE' is already running. Checking for existing data…"
    if "${ENGINE_CMD[@]}" exec "$COMPOSE_SERVICE" psql -h 127.0.0.1 -U postgres -d "$DEV_DB_NAME" \
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
"${COMPOSE_CMD[@]}" up -d
echo "OK — container starting."

# ---------------------------------------------------------------------------
# Step 2 — Wait for postgres to be ready
# ---------------------------------------------------------------------------

log_step "Waiting for postgres to be ready…"
# Probe over TCP: the temporary server the entrypoint runs the init
# scripts against listens on the Unix socket only, so a TCP probe
# cannot succeed until the final server is up.
deadline=$(( $(date +%s) + 30 ))
until "${ENGINE_CMD[@]}" exec "$COMPOSE_SERVICE" pg_isready -h 127.0.0.1 -U postgres \
        -d "$DEV_DB_NAME" >/dev/null 2>&1; do
    if [[ $(date +%s) -gt $deadline ]]; then
        log_error "Postgres did not become ready within 30 seconds."
        log_error "Check logs with:  ${ENGINE_CMD[*]} logs $COMPOSE_SERVICE"
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
if ! "${ENGINE_CMD[@]}" exec "$COMPOSE_SERVICE" psql -h 127.0.0.1 -U postgres -d "$DEV_DB_NAME" \
        -tAc "SELECT 1 FROM pg_roles WHERE rolname = 'portfoliflow_app';" \
        | grep -q '^1$'; then
    log_error "portfoliflow_app role was not created."
    log_error "Either db/init/01-create-app-role.sh is missing, or the volume"
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
"${ENGINE_CMD[@]}" exec "$COMPOSE_SERVICE" psql -h 127.0.0.1 -U postgres -d "$DEV_DB_NAME" -c \
    "SELECT 'tenants' AS tbl, COUNT(*)::TEXT AS n FROM tenants
     UNION ALL SELECT 'users',         COUNT(*)::TEXT FROM users
     UNION ALL SELECT 'asset_classes', COUNT(*)::TEXT FROM asset_classes
     UNION ALL SELECT 'investments',   COUNT(*)::TEXT FROM investments;"

echo
log_step "Done. Start the app with:  portfoliflow-web"
