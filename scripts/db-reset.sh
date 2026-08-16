#!/usr/bin/env bash
# scripts/db-reset.sh
#
# Full reset of the PortfoliFLOW dev database.
#
# Drops the postgres data volume (which discards everything), brings
# the container back up, waits for postgres to be ready, then applies
# alembic migrations and the bootstrap CLI.
#
# This is the canonical "I want a clean slate" workflow. Designed to
# be idempotent: running it twice in a row produces the same end
# state as running it once.
#
# Usage:
#     ./scripts/db-reset.sh                          # interactive bootstrap passwords
#     ./scripts/db-reset.sh --password "test"        # non-interactive owner password
#     ./scripts/db-reset.sh --no-bootstrap           # migrations only, skip user/SAA seeding
#
# Environment variables (consulted when interactive prompts would
# otherwise apply):
#     OWNER_PASSWORD          Primary-tenant owner password
#                             (replaces the deprecated SENTINEL_PASSWORD)
#     SUPER_ADMIN_EMAIL       First super-admin's email
#     SUPER_ADMIN_PASSWORD    First super-admin's password
#
# If SUPER_ADMIN_EMAIL is unset the super-admin step is skipped;
# create one later with 'portfoliflow create-super-admin'.
#
# Requirements:
#     - podman + podman-compose installed
#     - .venv activated (or this script will activate it)
#     - .env present with POSTGRES_* and DATABASE_URL_SUPERUSER
#
# Safety:
#     - Refuses to run if .env's POSTGRES_DB does not match the
#       hardcoded DEV_DB_NAME below. Override via --force if you
#       know what you're doing (production setups should not use
#       this script at all).

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly DEV_DB_NAME="portfoliflow_dev"
readonly COMPOSE_SERVICE="portfoliflow-postgres"

# Colours for output (ignored if not a tty)
if [[ -t 1 ]]; then
    readonly C_BOLD=$'\033[1m'
    readonly C_GREEN=$'\033[32m'
    readonly C_YELLOW=$'\033[33m'
    readonly C_RED=$'\033[31m'
    readonly C_RESET=$'\033[0m'
else
    readonly C_BOLD="" C_GREEN="" C_YELLOW="" C_RED="" C_RESET=""
fi

log_step()    { echo "${C_BOLD}${C_GREEN}==>${C_RESET} ${C_BOLD}$1${C_RESET}"; }
log_warn()    { echo "${C_YELLOW}!! $1${C_RESET}" >&2; }
log_error()   { echo "${C_RED}XX $1${C_RESET}" >&2; }

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

PASSWORD=""
RUN_BOOTSTRAP=1
FORCE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --password)
            PASSWORD="$2"
            shift 2
            ;;
        --no-bootstrap)
            RUN_BOOTSTRAP=0
            shift
            ;;
        --force)
            FORCE=1
            shift
            ;;
        --help|-h)
            sed -n '/^# Usage:/,/^# Safety:/p' "$0" | head -n -1 | sed 's/^# \{0,1\}//'
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
    exit 1
fi

# Make sure we're not about to nuke a non-dev database.
env_db_name="$(grep -E '^POSTGRES_DB=' .env | head -1 | cut -d= -f2- || true)"
if [[ "$env_db_name" != "$DEV_DB_NAME" && $FORCE -eq 0 ]]; then
    log_error "POSTGRES_DB in .env is '$env_db_name', expected '$DEV_DB_NAME'."
    log_error "Refusing to proceed. Override with --force if you really mean it."
    exit 1
fi

# Load .env into the environment so OWNER_* / SUPER_ADMIN_* (and any
# other configured vars) are visible to this script and inherited by
# the bootstrap subprocess. `set -a` exports everything sourced.
# Runs only after the POSTGRES_DB guard above (which reads the file
# directly) has confirmed we're pointing at the dev database.
# Handles standard `KEY=value` lines; values with characters that
# would break `source` (rare in this project's .env) are a
# pre-existing constraint, not parsed specially.
set -a
# shellcheck disable=SC1091
source .env
set +a

# Activate venv if not already active (idempotent).
if [[ -z "${VIRTUAL_ENV:-}" ]]; then
    if [[ -f .venv/bin/activate ]]; then
        # shellcheck source=/dev/null
        source .venv/bin/activate
    else
        log_warn "No .venv found and VIRTUAL_ENV is unset; alembic / bootstrap may fail."
    fi
fi

# Check required CLI commands.
for cmd in podman alembic; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        log_error "Required command not found: $cmd"
        exit 1
    fi
done

if [[ $RUN_BOOTSTRAP -eq 1 ]] && ! command -v portfoliflow >/dev/null 2>&1; then
    log_error "portfoliflow CLI not found (needed for bootstrap)."
    log_error "Either activate the venv or pass --no-bootstrap."
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 1 — Stop the app if it's running
# ---------------------------------------------------------------------------

log_step "Checking for running portfoliflow-web processes…"
if pgrep -f portfoliflow-web >/dev/null 2>&1; then
    log_warn "portfoliflow-web is running. Please stop it (Ctrl+C in that terminal) and re-run this script."
    exit 1
fi
echo "OK — no portfoliflow-web running."

# ---------------------------------------------------------------------------
# Step 2 — Bring compose stack down WITH the data volume
# ---------------------------------------------------------------------------

log_step "Stopping postgres container and removing data volume…"
podman compose down -v
echo "OK — container down, volume removed."

# ---------------------------------------------------------------------------
# Step 3 — Bring compose stack back up; init scripts run automatically
# ---------------------------------------------------------------------------

log_step "Starting postgres container (init scripts in db/init/ will run)…"
podman compose up -d
echo "OK — container starting."

# ---------------------------------------------------------------------------
# Step 4 — Wait for postgres to be ready
# ---------------------------------------------------------------------------

log_step "Waiting for postgres to be ready…"
# Probe over TCP: the temporary server the entrypoint runs the init
# scripts against listens on the Unix socket only, so a TCP probe
# cannot succeed until the final server is up.
deadline=$(( $(date +%s) + 30 ))
until podman exec "$COMPOSE_SERVICE" pg_isready -h 127.0.0.1 -U postgres \
        -d "$DEV_DB_NAME" >/dev/null 2>&1; do
    if [[ $(date +%s) -gt $deadline ]]; then
        log_error "Postgres did not become ready within 30 seconds."
        log_error "Check logs with:  podman logs $COMPOSE_SERVICE"
        exit 1
    fi
    sleep 1
done
echo "OK — postgres ready."

# Verify that the app role was created by the init script. The query
# is retried: a failed connection means the server is still settling,
# and only a query that answers something other than 1 means the role
# is genuinely missing.
role_deadline=$(( $(date +%s) + 30 ))
while true; do
    if role_present=$(podman exec "$COMPOSE_SERVICE" psql -h 127.0.0.1 \
            -U postgres -d "$DEV_DB_NAME" -tAc \
            "SELECT 1 FROM pg_roles WHERE rolname = 'portfoliflow_app';" \
            2>/dev/null); then
        if [[ "${role_present//[[:space:]]/}" != "1" ]]; then
            log_error "portfoliflow_app role was not created by init scripts."
            log_error "Check db/init/ contents and podman logs $COMPOSE_SERVICE."
            exit 1
        fi
        break
    fi
    if [[ $(date +%s) -gt $role_deadline ]]; then
        log_error "Postgres came up but psql could not connect within 30 seconds."
        log_error "Check logs with:  podman logs $COMPOSE_SERVICE"
        exit 1
    fi
    sleep 1
done
echo "OK — portfoliflow_app role present."

# ---------------------------------------------------------------------------
# Step 5 — Apply Alembic migrations
# ---------------------------------------------------------------------------

log_step "Applying Alembic migrations…"
alembic -c db/alembic.ini upgrade head
echo "OK — schema at head."

# ---------------------------------------------------------------------------
# Step 6 — Bootstrap sentinel tenant, user, SAA asset classes
# ---------------------------------------------------------------------------

if [[ $RUN_BOOTSTRAP -eq 0 ]]; then
    log_warn "Skipping bootstrap (--no-bootstrap)."
else
    log_step "Bootstrapping multi-tenant: tenants, owner, super-admin, seeds…"

    # ---- Owner password ---------------------------------------------------
    if [[ -z "$PASSWORD" ]]; then
        # First check the env var the bootstrap CLI consults.
        if [[ -n "${OWNER_PASSWORD:-}" ]]; then
            PASSWORD="$OWNER_PASSWORD"
            log_step "Using OWNER_PASSWORD from environment for the primary-tenant owner."
        elif [[ -n "${SENTINEL_PASSWORD:-}" ]]; then
            PASSWORD="$SENTINEL_PASSWORD"
            log_warn "Using deprecated SENTINEL_PASSWORD env var. Rename to OWNER_PASSWORD."
        else
            echo
            echo "Enter the password for the primary-tenant owner."
            echo "(This is your local dev password — choose what you'll remember.)"
            read -rs -p "Owner password: " PASSWORD
            echo
        fi
    fi
    if [[ -z "$PASSWORD" ]]; then
        log_error "Empty owner password — bootstrap aborted."
        exit 1
    fi

    # ---- Super-admin password ---------------------------------------------
    SA_PASSWORD="${SUPER_ADMIN_PASSWORD:-}"
    SA_EMAIL="${SUPER_ADMIN_EMAIL:-}"

    if [[ -z "$SA_EMAIL" ]]; then
        log_warn "SUPER_ADMIN_EMAIL not set — skipping super-admin creation."
        log_warn "Run 'portfoliflow create-super-admin' separately, or set"
        log_warn "SUPER_ADMIN_EMAIL and SUPER_ADMIN_PASSWORD in .env."
    elif [[ -z "$SA_PASSWORD" ]]; then
        echo
        echo "SUPER_ADMIN_EMAIL is set to: $SA_EMAIL"
        echo "Enter the password for the super-admin account."
        read -rs -p "Super-admin password: " SA_PASSWORD
        echo
        if [[ -z "$SA_PASSWORD" ]]; then
            log_error "Empty super-admin password — bootstrap aborted."
            exit 1
        fi
    fi

    # Export so the bootstrap CLI subprocess picks them up via
    # os.getenv. OWNER_PASSWORD is forwarded for symmetry; the CLI
    # actually reads the primary-owner password from stdin
    # (--password-stdin) for back-compat with the existing flow.
    export OWNER_PASSWORD="$PASSWORD"
    export SUPER_ADMIN_PASSWORD="$SA_PASSWORD"
    export SUPER_ADMIN_EMAIL="$SA_EMAIL"

    # Display names (ADR-0068). Already exported by the `set -a;
    # source .env` block above; re-exported here for parity with the
    # other bootstrap vars and to document that bootstrap consumes
    # them. No-ops when unset.
    export OWNER_DISPLAY_NAME="${OWNER_DISPLAY_NAME:-}"
    export SUPER_ADMIN_DISPLAY_NAME="${SUPER_ADMIN_DISPLAY_NAME:-}"

    echo -n "$PASSWORD" | portfoliflow bootstrap --password-stdin
    echo "OK — bootstrap complete."
fi

# ---------------------------------------------------------------------------
# Step 7 — Final verification
# ---------------------------------------------------------------------------

log_step "Verifying final state…"

podman exec "$COMPOSE_SERVICE" psql -h 127.0.0.1 -U postgres -d "$DEV_DB_NAME" -c \
    "SELECT 'tenants'        AS tbl, COUNT(*)::TEXT AS n FROM tenants
     UNION ALL SELECT 'users',         COUNT(*)::TEXT FROM users
     UNION ALL SELECT 'asset_classes', COUNT(*)::TEXT FROM asset_classes
     UNION ALL SELECT 'investments',   COUNT(*)::TEXT FROM investments
     UNION ALL SELECT 'navs',          COUNT(*)::TEXT FROM investment_navs;"

if [[ $RUN_BOOTSTRAP -eq 1 ]]; then
    # The UUID literal mirrors core.tenant_constants.PRIMARY_TENANT_ID;
    # the shell can't import the Python constant, so it's inlined here.
    owner_count=$(podman exec "$COMPOSE_SERVICE" psql -h 127.0.0.1 -U postgres \
        -d "$DEV_DB_NAME" -tAc \
        "SELECT COUNT(*) FROM users
         WHERE tenant_id = '00000000-0000-0000-0000-000000000001'
           AND 'owner' = ANY(roles) AND is_active;")
    owner_count="${owner_count//[[:space:]]/}"
    if [[ "${owner_count:-0}" -lt 1 ]]; then
        log_error "No active owner in the primary tenant after bootstrap."
        log_error "Check OWNER_EMAIL / OWNER_PASSWORD in .env, then re-run."
        exit 1
    fi
    echo "OK — primary-tenant owner present ($owner_count)."

    if [[ -n "${SUPER_ADMIN_EMAIL:-}" ]]; then
        sa_count=$(podman exec "$COMPOSE_SERVICE" psql -h 127.0.0.1 -U postgres \
            -d "$DEV_DB_NAME" -tAc \
            "SELECT COUNT(*) FROM users
             WHERE is_super_admin = TRUE AND is_active;")
        sa_count="${sa_count//[[:space:]]/}"
        if [[ "${sa_count:-0}" -lt 1 ]]; then
            log_error "SUPER_ADMIN_EMAIL is set but no super-admin exists."
            log_error "Check SUPER_ADMIN_PASSWORD in .env, then re-run."
            exit 1
        fi
        echo "OK — super-admin present ($sa_count)."
    else
        log_warn "No SUPER_ADMIN_EMAIL set — admin.localhost login will"
        log_warn "not work until you create a super-admin."
    fi
fi

echo
log_step "Done. The dev database is in a fresh post-bootstrap state."
echo "Next steps:"
echo "  - Add /etc/hosts entries for the dev subdomains:"
echo "        127.0.0.1   admin.localhost"
echo "        127.0.0.1   minathena-capital.localhost"
echo "  - Start the app:  portfoliflow-web"
echo "  - Log in as super-admin:   http://admin.localhost:8000/login"
echo "  - Log in as primary owner: http://minathena-capital.localhost:8000/login"
echo "  - Import data via /admin#data-import"
