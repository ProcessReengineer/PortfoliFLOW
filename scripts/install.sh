#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle
#
# Usage:
#   scripts/install.sh — the guided PortfoliFLOW installer (ADR-0124 §2).
#
#   Takes a machine that has a container engine and a Python 3.11+
#   interpreter to a running local PortfoliFLOW deployment: clone,
#   virtualenv, .env with per-installation secrets, Postgres container,
#   schema, primary tenant, and a verified status report. Every failure
#   names its cause and a copy-paste remedy; there are no silent
#   fallbacks.
#
#   Remote (nothing checked out yet):
#     bash -c "$(curl -fsSL https://portfoliflow.com/install.sh)"
#     bash -c "$(curl -fsSL https://portfoliflow.com/install.sh)" -- --dir ~/portfoliflow --no-ai
#
#   Local (from inside a checkout):
#     ./scripts/install.sh
#     ./scripts/install.sh --doctor
#
# Flags:
#   --dir <path>       Install into <path>. Remote default: ./PortfoliFLOW.
#                      In local mode it must name the checkout itself.
#   --ref <ref>        Branch or tag to clone (default: stable). Remote mode
#                      only; with a checkout present it is reported as not
#                      applied rather than treated as an error.
#   --engine podman|docker
#                      Force the container engine. Same precedence as
#                      scripts/db-init.sh: this flag, then PORTFOLIFLOW_ENGINE,
#                      then podman on PATH, then docker on PATH.
#   --db-port <n>      Host-side Postgres port (default: 5432). The container
#                      always listens on 5432 internally.
#   --non-interactive  Never prompt. Requires PORTFOLIFLOW_OWNER_EMAIL and
#                      PORTFOLIFLOW_OWNER_PASSWORD (absent or empty is an
#                      error, never a generated default); optionally reads
#                      PORTFOLIFLOW_OPENROUTER_API_KEY and
#                      PORTFOLIFLOW_SHIRLEY_MODEL.
#   --no-ai            Skip the AI questions. Shirley, Irene and the Report
#                      Scraper stay idle until a key is added later under
#                      Admin -> Providers & Credentials.
#   --force            Re-configure over an existing .env (backed up first).
#                      Refused while the database volume still exists.
#   --doctor           Run the preflight and the verification only, change
#                      nothing, write no log. This is what a support issue
#                      should carry.
#   --version          Print the installer identity, plus the project version
#                      when run from a checkout.
#   --help             Print this block.
#
# Environment:
#   PORTFOLIFLOW_ENGINE              podman | docker (see --engine)
#   PORTFOLIFLOW_OWNER_EMAIL         primary-tenant owner e-mail
#   PORTFOLIFLOW_OWNER_PASSWORD      primary-tenant owner password
#   PORTFOLIFLOW_OPENROUTER_API_KEY  OpenRouter key   (--non-interactive only)
#   PORTFOLIFLOW_SHIRLEY_MODEL       default AI model (--non-interactive only)
#
# Exit codes:
#   0   success
#   1   generic failure
#   2   bad usage (unknown flag, bad value, no terminal without
#       --non-interactive, --dir disagreeing with the checkout)
#   10  unsupported platform (not Linux or macOS, or bash older than 3.2)
#   11  missing prerequisite (git, curl, Python, engine, Compose, disk)
#   12  the chosen database port is in use by something other than the
#       portfoliflow-postgres container
#   13  refused (existing installation without --force; --force while the
#       database volume still exists; unusable existing .venv)
#
# Modes:
#   Remote — not inside a checkout. Runs the preflight, clones, then execs
#   the cloned copy with --local-mode. The code that installs is always the
#   code being installed (ADR-0124 §2.2).
#   Local — a sibling pyproject.toml declares name = "portfoliflow". The
#   clone phase is skipped.
#
# Log:
#   <target>/install.log, appended (never truncated), one dated separator per
#   run, ANSI colour stripped. No secret is ever written to it: passwords are
#   not echoed, generated values are not printed, .env is never dumped.
#   In remote mode the pre-clone half contributes a one-line preflight
#   summary — the exec'd local copy re-runs and logs the full preflight.
#   --doctor writes no log, so that it stays free of side effects.
#
# Privilege policy (ADR-0124 §2.4):
#   Never sudo. Never installs system packages. Never writes outside the
#   target directory. A missing prerequisite prints the exact command for
#   the detected package manager and exits 11.
#
# Recorded decisions (operator, 2026-08-19):
#   D1  ADR §2.3 Phase 5 says "portfoliflow status". Implemented as
#       `status --json` with database/tenants/users/web_application required
#       and ai_service informational, because `portfoliflow status` exits 1
#       whenever no AI key is configured.
#   D2  ADR §2.5 names only the owner credentials for --non-interactive.
#       PORTFOLIFLOW_OPENROUTER_API_KEY (optional) and
#       PORTFOLIFLOW_SHIRLEY_MODEL are read as well, so an unattended run
#       can configure AI.
#   D3  --force refuses while the volume portfoliflow_postgres_data exists:
#       the regenerated role password would not match a volume whose init
#       script has already run. The installer never removes a volume itself.

set -Eeuo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_URL="https://github.com/ProcessReengineer/PortfoliFLOW.git"
DEFAULT_REF="stable"
DEFAULT_DIR_NAME="PortfoliFLOW"
CONTAINER_NAME="portfoliflow-postgres"
VOLUME_NAME="portfoliflow_postgres_data"
DEV_DB_NAME="portfoliflow_dev"
DEFAULT_DB_PORT="5432"
DEFAULT_AI_MODEL="anthropic/claude-sonnet-4.5"
PRIMARY_TENANT_URL="http://minathena-capital.localhost:8000"
MIN_DISK_KIB=2097152
SECRET_LENGTH=32
MIN_PASSWORD_LENGTH=12
MIN_PASSWORD_CLASSES=2
MAX_PROMPT_ATTEMPTS=3

# ---------------------------------------------------------------------------
# Mutable state (every global is initialised — the script runs under `set -u`)
# ---------------------------------------------------------------------------

MODE=""
TARGET_DIR=""
DIR_FLAG=""
REF="$DEFAULT_REF"
REF_GIVEN=0
ENGINE_FLAG=""
DB_PORT="$DEFAULT_DB_PORT"
NON_INTERACTIVE=0
NO_AI=0
FORCE=0
DOCTOR=0
LOCAL_MODE_FLAG=0
WANT_HELP=0
WANT_VERSION=0

ENGINE_CMD=(podman)
COMPOSE_CMD=(podman compose)
PYTHON_BIN=""
PKG_MGR="none"
OS_NAME=""
ARCH_NAME=""

HAS_ENV=0
HAS_VENV=0
CONTAINER_STATE="absent"
HAS_VOLUME=0
SKIP_DB=0
AI_OK=0
AI_CONFIGURED=0
AI_MODEL=""

OWNER_EMAIL_VALUE=""
OWNER_PASSWORD_VALUE=""
OPENROUTER_KEY_VALUE=""
REPLY_VALUE=""

PREFLIGHT_SUMMARY=""
CURRENT_PHASE="startup"
LOG_FILE=""
LOG_STARTED=0
DYING=0

C_BOLD=""
C_GREEN=""
C_YELLOW=""
C_RED=""
C_RESET=""
C_ESC=""

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

init_colors() {
    C_ESC=$(printf '\033')
    if [ -t 1 ]; then
        C_BOLD="${C_ESC}[1m"
        C_GREEN="${C_ESC}[32m"
        C_YELLOW="${C_ESC}[33m"
        C_RED="${C_ESC}[31m"
        C_RESET="${C_ESC}[0m"
    fi
}

log_step()  { printf '%s%s==>%s %s%s%s\n' "$C_BOLD" "$C_GREEN" "$C_RESET" "$C_BOLD" "$1" "$C_RESET"; }
log_ok()    { printf '   %sOK%s   %s\n' "$C_GREEN" "$C_RESET" "$1"; }
log_info()  { printf '        %s\n' "$1"; }
log_warn()  { printf '%s!! %s%s\n' "$C_YELLOW" "$1" "$C_RESET" >&2; }
log_error() { printf '%sXX %s%s\n' "$C_RED" "$1" "$C_RESET" >&2; }
log_fail()  { printf '   %sFAIL%s %s\n' "$C_RED" "$C_RESET" "$1" >&2; }

# die <exit-code> <message…> — one message per line, then exit.
die() {
    local code="$1"
    shift
    DYING=1
    local line
    for line in "$@"; do
        log_error "$line"
    done
    printf 'install.sh: exiting %s (see --help for the exit-code table).\n' "$code" >&2
    exit "$code"
}

# ---------------------------------------------------------------------------
# Traps
# ---------------------------------------------------------------------------

phase_hint() {
    case "$CURRENT_PHASE" in
        preflight)
            log_info "The preflight only reads. A failure here is a missing or unreachable"
            log_info "prerequisite — re-run with --doctor for the full check list."
            ;;
        fetch)
            log_info "The most likely cause is a ref that does not exist: --ref '$REF' must"
            log_info "name a branch or tag in $REPO_URL (default: $DEFAULT_REF)."
            log_info "Otherwise check network or proxy access to github.com."
            ;;
        runtime)
            log_info "Virtualenv or dependency installation failed. Usual causes: no network"
            log_info "or an unset proxy; or a wheel that had to compile from source and found"
            log_info "no build toolchain — asyncpg and cryptography are the usual suspects."
            log_info "Install your platform's compiler and Python headers, then re-run."
            ;;
        configure)
            log_info "Nothing was started yet. Check that $TARGET_DIR is writable and that"
            log_info ".env.example is intact, then re-run."
            ;;
        database)
            log_info "Container or schema step failed. Inspect the container:"
            log_info "  ${ENGINE_CMD[*]} logs $CONTAINER_NAME"
            log_info "scripts/db-init.sh waits 30 seconds for Postgres to accept connections;"
            log_info "a slow first image pull can outlast that — re-run and it will be cached."
            log_info "If the message above says 'already has tables', or reports a password"
            log_info "authentication failure, the data volume predates this .env and does not"
            log_info "know its passwords. Drop it and re-run:"
            log_info "  cd $TARGET_DIR && ${COMPOSE_CMD[*]} down -v   # deletes all data"
            ;;
        verify)
            log_info "The deployment exists but does not report healthy. Run the text form for"
            log_info "the detail:  $TARGET_DIR/.venv/bin/portfoliflow status"
            ;;
        summary)
            log_info "The installation completed; only the closing summary failed."
            ;;
        *)
            log_info "Re-run with --doctor to see which prerequisite is unmet."
            ;;
    esac
}

on_err() {
    local status="$1"
    local failed="$2"
    if [ "$DYING" -eq 1 ]; then
        return 0
    fi
    DYING=1
    log_error "Failed during phase '$CURRENT_PHASE'."
    log_error "Command: $failed"
    log_error "Exit status: $status"
    phase_hint
    if [ "$LOG_STARTED" -eq 1 ]; then
        log_info "Transcript: $LOG_FILE"
    fi
    if [ "$status" -eq 0 ]; then
        status=1
    fi
    exit "$status"
}

on_exit() {
    # Let the transcript tee drain before the shell releases the terminal.
    if [ "$LOG_STARTED" -eq 1 ]; then
        sleep 0.2
    fi
}

trap 'on_err "$?" "$BASH_COMMAND"' ERR
trap on_exit EXIT

# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------

strip_ansi() {
    sed "s/${C_ESC}\[[0-9;]*m//g"
}

start_transcript() {
    if [ ! -w "$TARGET_DIR" ]; then
        log_warn "$TARGET_DIR is not writable — continuing without a transcript."
        return 0
    fi
    LOG_FILE="$TARGET_DIR/install.log"
    printf '\n===== %s — install.sh (%s mode) =====\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" "$MODE" >> "$LOG_FILE"
    exec > >(tee >(strip_ansi >> "$LOG_FILE")) 2>&1
    LOG_STARTED=1
}

# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

have() {
    command -v "$1" >/dev/null 2>&1
}

yes_no() {
    if [ "$1" -eq 1 ]; then printf 'yes'; else printf 'no'; fi
}

present_absent() {
    if [ "$1" -eq 1 ]; then printf 'present'; else printf 'absent'; fi
}

usage() {
    local src="${BASH_SOURCE[0]:-}"
    if [ -n "$src" ] && [ -f "$src" ]; then
        # Renders the header block from '# Usage:' to the first non-comment
        # line. awk rather than `head -n -N`, which BSD head rejects.
        awk '/^# Usage:/ { f = 1 }
             f && !/^#/  { exit }
             f           { sub(/^# ?/, ""); print }' "$src"
        return 0
    fi
    # Remote mode via `bash -c "$(curl …)"`: there is no file to read back.
    cat <<'FALLBACK'
scripts/install.sh — the guided PortfoliFLOW installer (ADR-0124 §2).

  bash -c "$(curl -fsSL https://portfoliflow.com/install.sh)"
  bash -c "$(curl -fsSL https://portfoliflow.com/install.sh)" -- --dir ~/portfoliflow --no-ai

Flags: --dir <path> --ref <ref> --engine podman|docker --db-port <n>
       --non-interactive --no-ai --force --doctor --version --help
Exit:  0 ok · 1 generic · 2 usage · 10 platform · 11 prerequisite
       12 port in use · 13 refused

The full help lives in the script header. Download a copy to read it:
  curl -fsSLO https://portfoliflow.com/install.sh && bash install.sh --help
FALLBACK
}

version_info() {
    printf 'portfoliflow install.sh (ADR-0124)\n'
    if [ "$MODE" = "local" ] && [ -f "$TARGET_DIR/pyproject.toml" ]; then
        awk -F'"' '/^version = "/ { print "portfoliflow " $2; exit }' \
            "$TARGET_DIR/pyproject.toml"
    fi
}

detect_pkg_manager() {
    if [ "$OS_NAME" = "Darwin" ] && have brew; then PKG_MGR="brew"; return 0; fi
    if have apt-get; then PKG_MGR="apt-get"; return 0; fi
    if have dnf;     then PKG_MGR="dnf";     return 0; fi
    if have pacman;  then PKG_MGR="pacman";  return 0; fi
    if have brew;    then PKG_MGR="brew";    return 0; fi
    PKG_MGR="none"
}

# install_hint <git|curl|python|podman|docker> — prints one copy-paste line.
install_hint() {
    case "$1" in
        git)
            case "$PKG_MGR" in
                apt-get) printf 'sudo apt-get install -y git' ;;
                dnf)     printf 'sudo dnf install -y git' ;;
                pacman)  printf 'sudo pacman -S git' ;;
                brew)    printf 'brew install git' ;;
                *)       printf 'install git — https://git-scm.com/downloads' ;;
            esac ;;
        curl)
            case "$PKG_MGR" in
                apt-get) printf 'sudo apt-get install -y curl' ;;
                dnf)     printf 'sudo dnf install -y curl' ;;
                pacman)  printf 'sudo pacman -S curl' ;;
                brew)    printf 'brew install curl' ;;
                *)       printf 'install curl — https://curl.se/download.html' ;;
            esac ;;
        python)
            case "$PKG_MGR" in
                apt-get) printf 'sudo apt-get install -y python3 python3-venv' ;;
                dnf)     printf 'sudo dnf install -y python3' ;;
                pacman)  printf 'sudo pacman -S python' ;;
                brew)    printf 'brew install python@3.12' ;;
                *)       printf 'install Python 3.11+ — https://www.python.org/downloads/' ;;
            esac ;;
        podman)
            case "$PKG_MGR" in
                apt-get) printf 'sudo apt-get install -y podman podman-compose' ;;
                dnf)     printf 'sudo dnf install -y podman podman-compose' ;;
                pacman)  printf 'sudo pacman -S podman podman-compose' ;;
                brew)    printf 'brew install podman podman-compose' ;;
                *)       printf 'install Podman — https://podman.io/docs/installation' ;;
            esac ;;
        docker)
            case "$PKG_MGR" in
                brew) printf 'brew install --cask docker' ;;
                *)    printf 'install Docker — https://docs.docker.com/engine/install/' ;;
            esac ;;
        *)
            printf 'install %s' "$1" ;;
    esac
}

# ---------------------------------------------------------------------------
# Interaction — every prompt goes to and comes from /dev/tty (ADR-0124 §2.5)
# ---------------------------------------------------------------------------

tty_available() {
    ( exec 3</dev/tty ) 2>/dev/null
}

ask() {
    printf '%s' "$1" > /dev/tty
    IFS= read -r REPLY_VALUE < /dev/tty
}

ask_secret() {
    printf '%s' "$1" > /dev/tty
    IFS= read -r -s REPLY_VALUE < /dev/tty
    printf '\n' > /dev/tty
}

# A value safe to write unquoted into .env: no whitespace, no quote characters.
valid_env_token() {
    # Backslash is rejected alongside whitespace and quotes: env_set hands the
    # value to awk through -v, which would process escape sequences inside it.
    case "$1" in
        ''|*[[:space:]]*|*\'*|*\"*|*\\*) return 1 ;;
    esac
    return 0
}

valid_email() {
    if ! valid_env_token "$1"; then
        return 1
    fi
    case "$1" in
        @*|*@) return 1 ;;
        *@*@*) return 1 ;;
        *@*)   return 0 ;;
    esac
    return 1
}

# Mirrors services/auth/password_policy.py: >= 12 characters, >= 2 of
# lowercase / uppercase / digit / symbol. The bootstrap path does not
# enforce it, so the installer does — a weak owner password would
# otherwise sail through unremarked.
password_class_count() {
    local p="$1" n=0
    case "$p" in *[[:lower:]]*)  n=$((n + 1)) ;; esac
    case "$p" in *[[:upper:]]*)  n=$((n + 1)) ;; esac
    case "$p" in *[[:digit:]]*)  n=$((n + 1)) ;; esac
    case "$p" in *[![:alnum:]]*) n=$((n + 1)) ;; esac
    printf '%s' "$n"
}

password_problem() {
    local p="$1"
    if [ "${#p}" -lt "$MIN_PASSWORD_LENGTH" ]; then
        printf 'must be at least %s characters (got %s)' "$MIN_PASSWORD_LENGTH" "${#p}"
        return 0
    fi
    if [ "$(password_class_count "$p")" -lt "$MIN_PASSWORD_CLASSES" ]; then
        printf 'must combine at least %s of lowercase, uppercase, digits, symbols' \
            "$MIN_PASSWORD_CLASSES"
        return 0
    fi
    return 1
}

# ---------------------------------------------------------------------------
# Secrets and .env editing
# ---------------------------------------------------------------------------

# 32 alphanumeric characters — URL-safe inside DATABASE_URL without escaping.
gen_secret() {
    local out
    # pipefail off in the subshell: head closes the pipe early and tr then
    # dies of SIGPIPE, which is expected, not a failure.
    out=$(set +o pipefail; LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c "$SECRET_LENGTH")
    if [ "${#out}" -ne "$SECRET_LENGTH" ]; then
        die 1 "Could not generate a $SECRET_LENGTH-character secret from /dev/urandom." \
              "Got ${#out} characters. Check that /dev/urandom is readable."
    fi
    printf '%s' "$out"
}

# env_set KEY VALUE — replace the first line matching '^#? ?KEY=' in .env, or
# append. awk over a temporary file, never `sed -i`, whose in-place form needs
# a backup argument on BSD and none on GNU.
env_set() {
    local key="$1" value="$2"
    local file="$TARGET_DIR/.env"
    local tmp="$file.tmp.$$"
    (
        umask 077
        awk -v key="$key" -v val="$value" '
            BEGIN { done = 0; pat = "^#?[ ]?" key "=" }
            !done && $0 ~ pat { print key "=" val; done = 1; next }
            { print }
            END { if (!done) print key "=" val }
        ' "$file" > "$tmp"
    )
    mv "$tmp" "$file"
    chmod 600 "$file"
}

env_written_once() {
    local key="$1" count
    count=$(grep -c "^${key}=" "$TARGET_DIR/.env" 2>/dev/null || true)
    if [ "$count" != "1" ]; then
        die 1 "Writing $key to .env did not produce exactly one line (found $count)." \
              "Inspect $TARGET_DIR/.env by hand."
    fi
}

# ---------------------------------------------------------------------------
# Container engine resolution (ADR-0124 §1.1)
# ---------------------------------------------------------------------------
#
# Same precedence and same wording as resolve_engine in scripts/db-init.sh,
# re-implemented rather than sourced: in remote mode the repository does not
# exist yet (ADR-0124 §2.1), and one voice across both files matters more to
# the reader than the duplication costs.

resolve_engine() {
    local requested="" origin=""

    if [ -n "$ENGINE_FLAG" ]; then
        requested="$ENGINE_FLAG"
        origin="--engine"
    elif [ -n "${PORTFOLIFLOW_ENGINE:-}" ]; then
        requested="$PORTFOLIFLOW_ENGINE"
        origin="PORTFOLIFLOW_ENGINE"
    fi

    if [ -n "$requested" ]; then
        if [ "$requested" != "podman" ] && [ "$requested" != "docker" ]; then
            die 2 "$origin names an unknown container engine: '$requested'." \
                  "Supported values are 'podman' and 'docker'."
        fi
        if ! have "$requested"; then
            die 11 "$origin requested '$requested', but '$requested' is not on PATH." \
                   "Install $requested, or drop $origin to use whichever engine is present." \
                   "  $(install_hint "$requested")"
        fi
        ENGINE_CMD=("$requested")
    elif have podman; then
        ENGINE_CMD=(podman)
    elif have docker; then
        ENGINE_CMD=(docker)
    else
        die 11 "No container engine found. PortfoliFLOW needs Podman or Docker" \
               "to run its PostgreSQL 16 container." \
               "  Podman (rootless, the repo default): $(install_hint podman)" \
               "  Docker:                              $(install_hint docker)"
    fi

    if [ "${ENGINE_CMD[0]}" = "podman" ]; then
        if podman compose version >/dev/null 2>&1; then
            COMPOSE_CMD=(podman compose)
        elif podman-compose --version >/dev/null 2>&1; then
            COMPOSE_CMD=(podman-compose)
        else
            die 11 "Podman is installed, but no Compose provider is available." \
                   "Podman ships no Compose implementation of its own — 'podman compose'" \
                   "delegates to one that must be installed separately. Install either:" \
                   "  - podman-compose            (pip install podman-compose, or your package manager)" \
                   "  - the Docker Compose plugin (package 'docker-compose-plugin')" \
                   "Or use Docker instead:  install.sh --engine docker"
        fi
    else
        if docker compose version >/dev/null 2>&1; then
            COMPOSE_CMD=(docker compose)
        elif docker-compose --version >/dev/null 2>&1; then
            COMPOSE_CMD=(docker-compose)
        else
            die 11 "Docker is installed, but no Compose provider is available." \
                   "Install the Docker Compose plugin (package 'docker-compose-plugin')," \
                   "or the standalone 'docker-compose' binary." \
                   "  https://docs.docker.com/compose/install/"
        fi
    fi
}

engine_reachable() {
    local err=""
    if "${ENGINE_CMD[@]}" info >/dev/null 2>&1; then
        return 0
    fi
    err=$("${ENGINE_CMD[@]}" info 2>&1 >/dev/null || true)

    log_fail "${ENGINE_CMD[0]} is installed but not answering."
    if [ "$OS_NAME" = "Darwin" ] && [ "${ENGINE_CMD[0]}" = "podman" ]; then
        local machines=""
        machines=$(podman machine list --format '{{.Name}}' 2>/dev/null || true)
        if [ -z "$machines" ]; then
            die 11 "No Podman machine exists. macOS runs containers inside a VM:" \
                   "  podman machine init && podman machine start"
        fi
        die 11 "A Podman machine exists but is not running:" \
               "  podman machine start"
    fi
    case "$err" in
        *"permission denied"*|*"Permission denied"*)
            die 11 "Permission denied talking to the ${ENGINE_CMD[0]} socket." \
                   "Add your user to the 'docker' group (then log out and back in)," \
                   "or switch to rootless Docker:" \
                   "  sudo usermod -aG docker \"\$USER\"" \
                   "  https://docs.docker.com/engine/security/rootless/"
            ;;
    esac
    die 11 "The ${ENGINE_CMD[0]} daemon is not reachable. Start it (Docker Desktop, or" \
           "'sudo systemctl start docker'), then re-run." \
           "Reported: $err"
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

need_value() {
    if [ "$2" -lt 2 ]; then
        die 2 "$1 needs a value." "See 'install.sh --help'."
    fi
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --dir)              need_value "$1" "$#"; DIR_FLAG="$2"; shift 2 ;;
            --ref)              need_value "$1" "$#"; REF="$2"; REF_GIVEN=1; shift 2 ;;
            --engine)           need_value "$1" "$#"; ENGINE_FLAG="$2"; shift 2 ;;
            --db-port)          need_value "$1" "$#"; DB_PORT="$2"; shift 2 ;;
            --non-interactive)  NON_INTERACTIVE=1; shift ;;
            --no-ai)            NO_AI=1; shift ;;
            --force)            FORCE=1; shift ;;
            --doctor)           DOCTOR=1; shift ;;
            --local-mode)       LOCAL_MODE_FLAG=1; shift ;;
            --version)          WANT_VERSION=1; shift ;;
            --help|-h)          WANT_HELP=1; shift ;;
            *)
                die 2 "Unknown argument: $1" "See 'install.sh --help' for the accepted flags."
                ;;
        esac
    done

    case "$DB_PORT" in
        ''|*[!0-9]*) die 2 "--db-port needs a number, got '$DB_PORT'." ;;
    esac
    if [ "$DB_PORT" -lt 1 ] || [ "$DB_PORT" -gt 65535 ]; then
        die 2 "--db-port must be between 1 and 65535, got '$DB_PORT'."
    fi
    if [ -n "$ENGINE_FLAG" ] && [ "$ENGINE_FLAG" != "podman" ] && [ "$ENGINE_FLAG" != "docker" ]; then
        die 2 "--engine names an unknown container engine: '$ENGINE_FLAG'." \
              "Supported values are 'podman' and 'docker'."
    fi
}

# ---------------------------------------------------------------------------
# Mode detection (ADR-0124 §2.2)
# ---------------------------------------------------------------------------

absolutise() {
    case "$1" in
        /*) printf '%s' "${1%/}" ;;
        *)  printf '%s' "$(pwd)/${1%/}" ;;
    esac
}

is_checkout() {
    [ -f "$1/pyproject.toml" ] && grep -q '^name = "portfoliflow"$' "$1/pyproject.toml"
}

detect_mode() {
    local src="${BASH_SOURCE[0]:-}" here="" parent=""

    if [ -n "$src" ] && [ -f "$src" ]; then
        here=$(cd "$(dirname "$src")" && pwd)
        parent=$(cd "$here/.." && pwd)
        if is_checkout "$parent"; then
            MODE="local"
            TARGET_DIR="$parent"
        fi
    fi

    if [ "$MODE" != "local" ] && [ "$LOCAL_MODE_FLAG" -eq 1 ]; then
        die 2 "--local-mode was given, but this file does not sit in a PortfoliFLOW checkout." \
              "--local-mode is set by the installer itself after cloning; do not pass it by hand."
    fi

    if [ "$MODE" = "local" ]; then
        if [ -n "$DIR_FLAG" ]; then
            local wanted
            wanted=$(absolutise "$DIR_FLAG")
            if [ -d "$wanted" ]; then
                wanted=$(cd "$wanted" && pwd)
            fi
            if [ "$wanted" != "$TARGET_DIR" ]; then
                die 2 "--dir '$DIR_FLAG' does not name this checkout ($TARGET_DIR)." \
                      "Running from inside a checkout always installs into that checkout." \
                      "Drop --dir, or run the installer from outside a checkout to install elsewhere."
            fi
        fi
        return 0
    fi

    MODE="remote"
    if [ -n "$DIR_FLAG" ]; then
        TARGET_DIR=$(absolutise "$DIR_FLAG")
    else
        TARGET_DIR="$(pwd)/$DEFAULT_DIR_NAME"
    fi
}

# ---------------------------------------------------------------------------
# Credential source — validated early so an unattended run fails in seconds
# ---------------------------------------------------------------------------

check_credential_source() {
    if [ "$NON_INTERACTIVE" -eq 1 ]; then
        if [ -z "${PORTFOLIFLOW_OWNER_EMAIL:-}" ]; then
            die 2 "--non-interactive needs PORTFOLIFLOW_OWNER_EMAIL." \
                  "There is no default owner e-mail — set it and re-run."
        fi
        if [ -z "${PORTFOLIFLOW_OWNER_PASSWORD:-}" ]; then
            die 2 "--non-interactive needs PORTFOLIFLOW_OWNER_PASSWORD." \
                  "There is no generated default password — set it and re-run."
        fi
        return 0
    fi
    if ! tty_available; then
        die 2 "No terminal available for the interactive prompts." \
              "Use --non-interactive with PORTFOLIFLOW_OWNER_EMAIL and PORTFOLIFLOW_OWNER_PASSWORD."
    fi
}

# ---------------------------------------------------------------------------
# Phase 0 — preflight
# ---------------------------------------------------------------------------

pf_check_os() {
    OS_NAME=$(uname -s)
    ARCH_NAME=$(uname -m)
    case "$OS_NAME" in
        Linux|Darwin) log_ok "operating system: $OS_NAME ($ARCH_NAME)" ;;
        *)
            log_fail "operating system: $OS_NAME"
            die 10 "PortfoliFLOW's installer supports Linux and macOS only." \
                   "On Windows, run it inside WSL2."
            ;;
    esac
    detect_pkg_manager
    if [ "$(id -u)" = "0" ]; then
        log_warn "Running as root: the installation and its files will be root-owned."
        log_warn "That works, but a normal user account is the intended setup."
    fi
}

pf_check_bash() {
    log_ok "bash: ${BASH_VERSION:-unknown}"
}

pf_check_tools() {
    local tool
    for tool in git curl; do
        if have "$tool"; then
            log_ok "$tool: $(command -v "$tool")"
        else
            log_fail "$tool: not found"
            die 11 "'$tool' is required and was not found on PATH." \
                   "  $(install_hint "$tool")"
        fi
    done
}

pf_check_python() {
    local cand="" venv_blocked=""
    for cand in python3.13 python3.12 python3.11 python3; do
        if ! have "$cand"; then
            continue
        fi
        if ! "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
            continue
        fi
        if "$cand" -c 'import venv, ensurepip' >/dev/null 2>&1; then
            PYTHON_BIN="$cand"
            break
        fi
        venv_blocked="$cand"
    done

    if [ -n "$PYTHON_BIN" ]; then
        log_ok "python: $PYTHON_BIN ($("$PYTHON_BIN" -c 'import platform; print(platform.python_version())'))"
        return 0
    fi

    if [ -n "$venv_blocked" ]; then
        log_fail "python: $venv_blocked is new enough but cannot create virtual environments"
        die 11 "'$venv_blocked' satisfies Python >= 3.11 but 'import venv, ensurepip' fails." \
               "Debian and Ubuntu ship the interpreter without the venv module — that is this" \
               "exact failure, and it is the most common one on those distributions." \
               "  $(install_hint python)"
    fi
    log_fail "python: no interpreter >= 3.11 found"
    die 11 "PortfoliFLOW needs Python 3.11 or newer (probed python3.13, python3.12," \
           "python3.11, python3)." \
           "  $(install_hint python)"
}

pf_check_engine() {
    resolve_engine
    log_ok "container engine: ${ENGINE_CMD[*]} (compose: ${COMPOSE_CMD[*]})"
    engine_reachable
    log_ok "engine reachable: ${ENGINE_CMD[0]} info answered"
}

pf_check_markers() {
    if [ -f "$TARGET_DIR/.env" ]; then HAS_ENV=1; fi
    if [ -d "$TARGET_DIR/.venv" ]; then HAS_VENV=1; fi

    if "${ENGINE_CMD[@]}" ps --format '{{.Names}}' 2>/dev/null | grep -q "^${CONTAINER_NAME}\$"; then
        CONTAINER_STATE="running"
    elif "${ENGINE_CMD[@]}" ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^${CONTAINER_NAME}\$"; then
        CONTAINER_STATE="stopped"
    else
        CONTAINER_STATE="absent"
    fi

    if "${ENGINE_CMD[@]}" volume ls --format '{{.Name}}' 2>/dev/null | grep -q "^${VOLUME_NAME}\$"; then
        HAS_VOLUME=1
    fi

    log_info "existing .env:      $(yes_no "$HAS_ENV")"
    log_info "existing .venv:     $(yes_no "$HAS_VENV")"
    log_info "container:          $CONTAINER_STATE ($CONTAINER_NAME)"
    log_info "data volume:        $(present_absent "$HAS_VOLUME") ($VOLUME_NAME)"
}

pf_check_target() {
    if [ "$MODE" = "local" ]; then
        if [ -w "$TARGET_DIR" ]; then
            log_ok "target directory: $TARGET_DIR (checkout, writable)"
        else
            log_fail "target directory: $TARGET_DIR is not writable"
            die 11 "The checkout at $TARGET_DIR is not writable by $(id -un)."
        fi
        return 0
    fi

    local parent
    parent=$(dirname "$TARGET_DIR")
    if [ ! -d "$parent" ]; then
        log_fail "target directory: $parent does not exist"
        die 11 "The parent directory $parent does not exist. Create it, or choose another --dir."
    fi
    if [ ! -w "$parent" ]; then
        log_fail "target directory: $parent is not writable"
        die 11 "The parent directory $parent is not writable by $(id -un)."
    fi
    if [ ! -e "$TARGET_DIR" ]; then
        log_ok "target directory: $TARGET_DIR (will be created)"
        return 0
    fi
    if [ ! -d "$TARGET_DIR" ]; then
        log_fail "target directory: $TARGET_DIR exists and is not a directory"
        die 13 "$TARGET_DIR exists and is not a directory. Choose another --dir."
    fi
    if is_checkout "$TARGET_DIR"; then
        log_ok "target directory: $TARGET_DIR (existing PortfoliFLOW checkout)"
        return 0
    fi
    # shellcheck disable=SC2012  # stat(1) has no portable form across GNU/BSD
    if [ -z "$(ls -A "$TARGET_DIR" 2>/dev/null)" ]; then
        log_ok "target directory: $TARGET_DIR (empty)"
        return 0
    fi
    log_fail "target directory: $TARGET_DIR is not empty and not a checkout"
    die 13 "$TARGET_DIR exists, is not empty, and is not a PortfoliFLOW checkout." \
           "Choose an empty directory with --dir, or remove it yourself first."
}

pf_check_port() {
    if [ "$CONTAINER_STATE" = "running" ]; then
        log_ok "database port $DB_PORT: held by the running $CONTAINER_NAME container (probe skipped)"
        return 0
    fi
    if ( exec 3<>"/dev/tcp/127.0.0.1/$DB_PORT" ) 2>/dev/null; then
        log_fail "database port $DB_PORT: already in use"
        local inspect="ss -ltnp | grep :$DB_PORT"
        if [ "$OS_NAME" = "Darwin" ]; then
            inspect="lsof -nP -iTCP:$DB_PORT"
        fi
        die 12 "Something is already listening on 127.0.0.1:$DB_PORT, and it is not the" \
               "$CONTAINER_NAME container. A host-side Postgres is the usual cause." \
               "Find it with:   $inspect" \
               "Or move PortfoliFLOW's Postgres aside:  install.sh --db-port 5433"
    fi
    log_ok "database port $DB_PORT: free"
}

pf_check_disk() {
    local check_dir avail
    if [ "$MODE" = "local" ]; then
        check_dir="$TARGET_DIR"
    else
        check_dir=$(dirname "$TARGET_DIR")
    fi
    avail=$(df -Pk "$check_dir" | awk 'NR==2 {print $4}')
    case "$avail" in
        ''|*[!0-9]*)
            log_warn "could not read free space on $check_dir — continuing."
            return 0
            ;;
    esac
    if [ "$avail" -lt "$MIN_DISK_KIB" ]; then
        log_fail "free space on $check_dir: $((avail / 1024)) MB"
        die 11 "At least 2 GB free is needed on $check_dir (the postgres:16 image, the" \
               "virtualenv and the database). Found $((avail / 1024)) MB."
    fi
    log_ok "free space on $check_dir: $((avail / 1024)) MB"
}

phase_preflight() {
    CURRENT_PHASE="preflight"
    log_step "Phase 0 — preflight"
    pf_check_os
    pf_check_bash
    pf_check_tools
    pf_check_python
    pf_check_engine
    pf_check_markers
    pf_check_target
    pf_check_port
    pf_check_disk

    if [ "$REF_GIVEN" -eq 1 ] && [ "$MODE" = "local" ]; then
        log_warn "--ref '$REF' ignored: a checkout is already present at $TARGET_DIR."
    fi

    PREFLIGHT_SUMMARY="$OS_NAME/$ARCH_NAME · $PYTHON_BIN · ${ENGINE_CMD[*]} (${COMPOSE_CMD[*]}) · $TARGET_DIR · port $DB_PORT · $MODE mode"
    log_info "preflight passed: $PREFLIGHT_SUMMARY"
}

# ---------------------------------------------------------------------------
# Phase 1 — fetch (remote mode only; execs and never returns)
# ---------------------------------------------------------------------------

phase_fetch() {
    CURRENT_PHASE="fetch"
    log_step "Phase 1 — fetch"

    if is_checkout "$TARGET_DIR"; then
        log_info "existing checkout found at $TARGET_DIR — not cloning."
        if [ "$REF_GIVEN" -eq 1 ]; then
            log_warn "--ref '$REF' not applied: the checkout is used as it stands."
        fi
    else
        log_info "cloning $REPO_URL ($REF) into $TARGET_DIR"
        git clone --branch "$REF" --depth 1 "$REPO_URL" "$TARGET_DIR"
        log_ok "clone complete."
    fi

    if [ ! -f "$TARGET_DIR/scripts/install.sh" ]; then
        die 1 "The clone at $TARGET_DIR carries no scripts/install.sh." \
              "Ref '$REF' does not look like a PortfoliFLOW tree."
    fi

    # The pre-clone half contributes a summary line rather than replaying its
    # output: the exec'd copy re-runs the preflight and logs it in full, and a
    # tee started here would be inherited and double every subsequent line.
    if [ -w "$TARGET_DIR" ]; then
        {
            printf '\n===== %s — install.sh (remote mode) =====\n' "$(date '+%Y-%m-%d %H:%M:%S')"
            printf 'preflight passed: %s\n' "$PREFLIGHT_SUMMARY"
            printf 'fetch: %s @ %s -> %s\n' "$REPO_URL" "$REF" "$TARGET_DIR"
            printf 'handing off to the cloned copy (--local-mode)\n'
        } >> "$TARGET_DIR/install.log"
    fi

    local fwd
    fwd=(--local-mode --dir "$TARGET_DIR" --engine "${ENGINE_CMD[0]}" --db-port "$DB_PORT")
    if [ "$NON_INTERACTIVE" -eq 1 ]; then fwd=("${fwd[@]}" --non-interactive); fi
    if [ "$NO_AI" -eq 1 ];           then fwd=("${fwd[@]}" --no-ai); fi
    if [ "$FORCE" -eq 1 ];           then fwd=("${fwd[@]}" --force); fi

    log_step "Handing off to the installed copy"
    exec bash "$TARGET_DIR/scripts/install.sh" "${fwd[@]}"
}

# ---------------------------------------------------------------------------
# Phase 2 — runtime
# ---------------------------------------------------------------------------

phase_runtime() {
    CURRENT_PHASE="runtime"
    log_step "Phase 2 — Python runtime"
    cd "$TARGET_DIR"

    local recreate=0
    if [ -x ".venv/bin/python" ]; then
        if .venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
            log_ok "reusing .venv ($(.venv/bin/python -c 'import platform; print(platform.python_version())'))"
        elif [ "$FORCE" -eq 1 ]; then
            log_warn "existing .venv is older than Python 3.11 — recreating (--force)."
            recreate=1
        else
            die 13 "The existing .venv runs a Python older than 3.11." \
                   "Remove it ('rm -rf $TARGET_DIR/.venv') or re-run with --force."
        fi
    else
        recreate=1
    fi

    if [ "$recreate" -eq 1 ]; then
        if [ -d ".venv" ]; then
            rm -rf .venv
        fi
        log_info "creating .venv with $PYTHON_BIN"
        "$PYTHON_BIN" -m venv .venv
    fi

    log_info "upgrading pip"
    .venv/bin/python -m pip install -q --upgrade pip
    log_info "installing portfoliflow and its dependencies (this takes a few minutes)"
    .venv/bin/python -m pip install -q -e .
    log_ok "runtime ready: $TARGET_DIR/.venv"
}

# ---------------------------------------------------------------------------
# Phase 3 — configure
# ---------------------------------------------------------------------------

configure_owner_interactive() {
    local attempt=0 problem=""

    attempt=0
    while [ "$attempt" -lt "$MAX_PROMPT_ATTEMPTS" ]; do
        attempt=$((attempt + 1))
        ask "Owner e-mail address (the login for the primary tenant): "
        if valid_email "$REPLY_VALUE"; then
            OWNER_EMAIL_VALUE="$REPLY_VALUE"
            break
        fi
        log_warn "That does not look like an e-mail address (text, '@', text, no spaces or quotes)."
    done
    if [ -z "$OWNER_EMAIL_VALUE" ]; then
        die 2 "No valid owner e-mail after $MAX_PROMPT_ATTEMPTS attempts."
    fi

    attempt=0
    while [ "$attempt" -lt "$MAX_PROMPT_ATTEMPTS" ]; do
        attempt=$((attempt + 1))
        ask_secret "Owner password (at least $MIN_PASSWORD_LENGTH characters, at least $MIN_PASSWORD_CLASSES character classes): "
        local first="$REPLY_VALUE"
        if problem=$(password_problem "$first"); then
            log_warn "Password rejected: $problem."
            continue
        fi
        ask_secret "Repeat the password: "
        if [ "$first" != "$REPLY_VALUE" ]; then
            log_warn "The two entries differ."
            continue
        fi
        OWNER_PASSWORD_VALUE="$first"
        break
    done
    if [ -z "$OWNER_PASSWORD_VALUE" ]; then
        die 2 "No acceptable owner password after $MAX_PROMPT_ATTEMPTS attempts."
    fi
}

configure_owner_env() {
    OWNER_EMAIL_VALUE="${PORTFOLIFLOW_OWNER_EMAIL:-}"
    OWNER_PASSWORD_VALUE="${PORTFOLIFLOW_OWNER_PASSWORD:-}"
    if [ -z "$OWNER_EMAIL_VALUE" ]; then
        die 2 "--non-interactive needs PORTFOLIFLOW_OWNER_EMAIL; it is unset or empty."
    fi
    if [ -z "$OWNER_PASSWORD_VALUE" ]; then
        die 2 "--non-interactive needs PORTFOLIFLOW_OWNER_PASSWORD; it is unset or empty."
    fi
    if ! valid_email "$OWNER_EMAIL_VALUE"; then
        die 2 "PORTFOLIFLOW_OWNER_EMAIL is not a usable e-mail address" \
              "(text, '@', text; no whitespace and no quote characters)."
    fi
    local problem=""
    if problem=$(password_problem "$OWNER_PASSWORD_VALUE"); then
        die 2 "PORTFOLIFLOW_OWNER_PASSWORD is too weak: $problem."
    fi
}

configure_ai() {
    if [ "$NO_AI" -eq 1 ]; then
        log_info "AI configuration skipped (--no-ai)."
        return 0
    fi

    if [ "$NON_INTERACTIVE" -eq 1 ]; then
        OPENROUTER_KEY_VALUE="${PORTFOLIFLOW_OPENROUTER_API_KEY:-}"
        if [ -z "$OPENROUTER_KEY_VALUE" ]; then
            log_info "no PORTFOLIFLOW_OPENROUTER_API_KEY — AI stays unconfigured."
            return 0
        fi
        AI_MODEL="${PORTFOLIFLOW_SHIRLEY_MODEL:-$DEFAULT_AI_MODEL}"
    else
        ask_secret "OpenRouter API key (leave empty to configure later in Admin -> Providers & Credentials): "
        OPENROUTER_KEY_VALUE="$REPLY_VALUE"
        if [ -z "$OPENROUTER_KEY_VALUE" ]; then
            log_info "no key given — AI stays unconfigured."
            return 0
        fi
        ask "Default model [$DEFAULT_AI_MODEL]: "
        AI_MODEL="$REPLY_VALUE"
        if [ -z "$AI_MODEL" ]; then
            AI_MODEL="$DEFAULT_AI_MODEL"
        fi
    fi

    if ! valid_env_token "$OPENROUTER_KEY_VALUE"; then
        die 2 "The OpenRouter API key contains whitespace or a quote character and cannot be" \
              "written to .env unquoted. Check what was pasted."
    fi
    if ! valid_env_token "$AI_MODEL"; then
        die 2 "The model name contains whitespace or a quote character: '$AI_MODEL'."
    fi
    AI_CONFIGURED=1
}

phase_configure() {
    CURRENT_PHASE="configure"
    log_step "Phase 3 — configuration and secrets"
    cd "$TARGET_DIR"

    if [ "$FORCE" -eq 1 ] && [ "$HAS_VOLUME" -eq 1 ]; then
        die 13 "--force regenerates every secret, but the database volume '$VOLUME_NAME'" \
               "already exists. The init script that sets the portfoliflow_app role password" \
               "runs only on a fresh volume, so the new password would never reach Postgres." \
               "Drop the volume first — this deletes all data — then re-run:" \
               "  cd $TARGET_DIR && ${COMPOSE_CMD[*]} down -v"
    fi

    if [ "$HAS_ENV" -eq 1 ]; then
        if [ "$FORCE" -eq 0 ]; then
            die 13 ".env exists — this looks like an existing installation." \
                   "Verify it with:      $TARGET_DIR/scripts/install.sh --doctor" \
                   "Re-configure with:   $TARGET_DIR/scripts/install.sh --force" \
                   "(--force regenerates all secrets, which requires a fresh database volume.)"
        fi
        local backup
        backup=".env.bak.$(date +%Y%m%d-%H%M%S)"
        cp .env "$backup"
        chmod 600 "$backup"
        log_warn "existing .env backed up to $backup (it holds the previous secrets)."
    fi

    if [ "$HAS_VOLUME" -eq 1 ] && [ "$HAS_ENV" -eq 0 ]; then
        log_warn "the data volume '$VOLUME_NAME' exists but there is no .env."
        log_warn "The secrets generated below cannot match it: both the portfoliflow_app"
        log_warn "password (db/init/01-create-app-role.sh) and the Postgres superuser password"
        log_warn "are fixed when the volume is first created and never re-read. Phase 4 will"
        log_warn "most likely fail to authenticate."
        log_warn "Drop the volume first — this deletes all data — then re-run:"
        log_warn "  cd $TARGET_DIR && ${COMPOSE_CMD[*]} down -v"
    fi

    cp .env.example .env
    chmod 600 .env
    log_ok "wrote .env from .env.example (mode 600)"

    local pg_password app_password vault_key vault_out
    pg_password=$(gen_secret)
    app_password=$(gen_secret)
    vault_out=$(.venv/bin/portfoliflow vault-generate-key)
    if [ "$(printf '%s\n' "$vault_out" | wc -l | tr -d ' ')" != "1" ] || [ -z "$vault_out" ]; then
        die 1 "'portfoliflow vault-generate-key' did not print exactly one line." \
              "The credential vault master key could not be generated; nothing else was changed."
    fi
    vault_key="$vault_out"
    if ! valid_env_token "$vault_key"; then
        die 1 "The generated vault master key contains whitespace or a quote character."
    fi
    log_ok "generated Postgres superuser, application-role and vault master secrets"

    if [ "$NON_INTERACTIVE" -eq 1 ]; then
        configure_owner_env
    else
        configure_owner_interactive
    fi
    log_ok "owner: $OWNER_EMAIL_VALUE"

    configure_ai

    env_set POSTGRES_PASSWORD "$pg_password"
    env_set POSTGRES_PORT "$DB_PORT"
    env_set APP_DB_PASSWORD "$app_password"
    env_set DATABASE_URL "postgresql+asyncpg://portfoliflow_app:${app_password}@localhost:${DB_PORT}/${DEV_DB_NAME}"
    env_set DATABASE_URL_SUPERUSER "postgresql+asyncpg://postgres:${pg_password}@localhost:${DB_PORT}/${DEV_DB_NAME}"
    env_set CREDENTIAL_VAULT_MASTER_KEY "$vault_key"
    env_set OWNER_EMAIL "$OWNER_EMAIL_VALUE"

    env_written_once POSTGRES_PASSWORD
    env_written_once POSTGRES_PORT
    env_written_once APP_DB_PASSWORD
    env_written_once DATABASE_URL
    env_written_once DATABASE_URL_SUPERUSER
    env_written_once CREDENTIAL_VAULT_MASTER_KEY
    env_written_once OWNER_EMAIL

    if [ "$AI_CONFIGURED" -eq 1 ]; then
        env_set OPENROUTER_API_KEY "$OPENROUTER_KEY_VALUE"
        env_set SHIRLEY_MODEL "$AI_MODEL"
        env_written_once OPENROUTER_API_KEY
        env_written_once SHIRLEY_MODEL
        log_ok "AI configured: model $AI_MODEL"
    fi

    local perms
    # shellcheck disable=SC2012  # stat(1) has no portable form across GNU/BSD
    perms=$(ls -l .env | awk '{print $1}')
    case "$perms" in
        -rw-------*) log_ok ".env permissions: $perms" ;;
        *)
            die 1 ".env should be mode 600 but reads '$perms'." \
                  "Fix it with: chmod 600 $TARGET_DIR/.env"
            ;;
    esac
}

# ---------------------------------------------------------------------------
# Phase 4 — database
# ---------------------------------------------------------------------------

phase_database() {
    CURRENT_PHASE="database"
    log_step "Phase 4 — database"
    cd "$TARGET_DIR"

    if [ "$CONTAINER_STATE" = "running" ]; then
        local tables
        tables=$("${ENGINE_CMD[@]}" exec "$CONTAINER_NAME" psql -h 127.0.0.1 -U postgres \
            -d "$DEV_DB_NAME" -tAc \
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'" \
            2>/dev/null | tr -d ' \r' || true)
        case "$tables" in
            ''|0|*[!0-9]*) : ;;
            *)
                SKIP_DB=1
                log_warn "database already initialised ($tables tables) — skipping."
                log_info "Use scripts/db-reset.sh for a clean slate."
                log_info "You reached this by re-running with the container and its volume in"
                log_info "place but no .env, so the .env just written carries freshly generated"
                log_info "passwords the existing volume has never seen. If the application"
                log_info "cannot connect, drop the volume and re-run — this deletes all data:"
                log_info "  cd $TARGET_DIR && ${COMPOSE_CMD[*]} down -v"
                return 0
                ;;
        esac
    fi

    log_info "first start pulls postgres:16 (~150 MB) — this can take a few minutes."
    # VIRTUAL_ENV is cleared so db-init.sh sources this checkout's .venv rather
    # than an unrelated environment the operator's shell may already carry.
    printf '%s\n' "$OWNER_PASSWORD_VALUE" | \
        env VIRTUAL_ENV= ./scripts/db-init.sh --engine "${ENGINE_CMD[0]}" --password-stdin
    log_ok "database initialised, schema at head, primary tenant bootstrapped."
}

# ---------------------------------------------------------------------------
# Phase 5 — verify
# ---------------------------------------------------------------------------

status_eval() {
    "$TARGET_DIR/.venv/bin/python" - "$1" <<'PY'
import json
import sys

raw = sys.argv[1]
try:
    report = json.loads(raw)
except ValueError:
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < start:
        print("PARSE_ERROR=1")
        sys.exit(2)
    try:
        report = json.loads(raw[start:end + 1])
    except ValueError:
        print("PARSE_ERROR=1")
        sys.exit(2)

required = ["database", "tenants", "users", "web_application"]
bad = [k for k in required if not report.get(k, {}).get("ok", False)]
print("AI_OK=%d" % (1 if report.get("ai_service", {}).get("ok") else 0))
for key in bad:
    print("FAILED_SECTION=%s" % key)
    for note in report.get(key, {}).get("notes", []):
        print("NOTE=%s" % note)
sys.exit(1 if bad else 0)
PY
}

phase_verify() {
    CURRENT_PHASE="verify"
    local doctor="${1:-0}"
    log_step "Phase 5 — verification"

    if [ ! -x "$TARGET_DIR/.venv/bin/portfoliflow" ] || [ ! -f "$TARGET_DIR/.env" ]; then
        if [ "$doctor" -eq 1 ]; then
            log_warn "not installed: no .venv and/or no .env at $TARGET_DIR."
            log_info "Run '$TARGET_DIR/scripts/install.sh' to install."
            return 0
        fi
        die 1 "Expected a virtualenv and a .env at $TARGET_DIR, and found none."
    fi

    cd "$TARGET_DIR"

    local status_json status_rc=0
    status_json=$(.venv/bin/portfoliflow status --json 2>&1) || status_rc=$?

    if [ "$status_rc" -eq 2 ]; then
        printf '%s\n' "$status_json"
        die 1 "'portfoliflow status' could not run its checks (exit 2)." \
              "Run the text form for the detail:  $TARGET_DIR/.venv/bin/portfoliflow status"
    fi

    local eval_out eval_rc=0
    eval_out=$(status_eval "$status_json" 2>&1) || eval_rc=$?

    if [ "$eval_rc" -eq 2 ]; then
        printf '%s\n' "$status_json"
        die 1 "Could not read the JSON report from 'portfoliflow status --json'." \
              "Run the text form for the detail:  $TARGET_DIR/.venv/bin/portfoliflow status"
    fi

    case "$eval_out" in
        *AI_OK=1*) AI_OK=1 ;;
        *)         AI_OK=0 ;;
    esac

    if [ "$eval_rc" -ne 0 ]; then
        printf '%s\n' "$eval_out" | grep -v '^AI_OK=' >&2 || true
        die 1 "The deployment is not healthy: one or more required sections failed." \
              "Run the text form for the detail:  $TARGET_DIR/.venv/bin/portfoliflow status"
    fi

    log_ok "database, tenants, users and web application all report OK."
    if [ "$AI_OK" -eq 1 ]; then
        log_ok "AI service configured."
    else
        log_warn "AI not configured — Shirley, Irene and the Report Scraper are idle until a key is set."
        log_info "Add one under Admin -> Providers & Credentials, or set OPENROUTER_API_KEY"
        log_info "and SHIRLEY_MODEL in $TARGET_DIR/.env."
    fi
}

# ---------------------------------------------------------------------------
# Phase 6 — summary
# ---------------------------------------------------------------------------

phase_summary() {
    CURRENT_PHASE="summary"
    log_step "Phase 6 — done"
    printf '\n'
    printf 'PortfoliFLOW is installed in %s.\n' "$TARGET_DIR"
    printf 'Start: cd %s && .venv/bin/portfoliflow-web\n' "$TARGET_DIR"
    printf 'Open: %s\n' "$PRIMARY_TENANT_URL"
    printf '  That host names the primary tenant. Most systems resolve *.localhost to\n'
    printf '  127.0.0.1 on their own; if yours does not, add this line to /etc/hosts:\n'
    printf '    127.0.0.1   minathena-capital.localhost\n'
    printf 'Sign in as %s with the password you chose.\n' "$OWNER_EMAIL_VALUE"
    printf 'Secrets: %s/.env (mode 600) — the Postgres superuser password, the\n' "$TARGET_DIR"
    printf '  application role password and the vault master key were generated for this\n'
    printf '  installation; back it up, it is not recoverable.\n'
    if [ "$AI_OK" -eq 1 ]; then
        printf 'AI: configured (model %s). Shirley, Irene and the Report Scraper are live.\n' "$AI_MODEL"
    else
        printf 'AI: not configured. Shirley, Irene and the Report Scraper stay idle until a\n'
        printf '  key is added under Admin -> Providers & Credentials.\n'
    fi
    if [ "$SKIP_DB" -eq 1 ]; then
        printf 'Note: the database was already initialised and was left untouched.\n'
    fi
    if [ "$LOG_STARTED" -eq 1 ]; then
        printf 'Log: %s\n' "$LOG_FILE"
    fi
    printf 'Re-check any time: %s/scripts/install.sh --doctor\n' "$TARGET_DIR"
}

# ---------------------------------------------------------------------------
# --doctor
# ---------------------------------------------------------------------------

run_doctor() {
    log_step "PortfoliFLOW installer — doctor"
    log_info "Read-only: nothing is started, written or changed."
    phase_preflight
    if [ "$MODE" = "remote" ]; then
        CURRENT_PHASE="verify"
        log_step "Phase 5 — verification"
        log_info "no installation at $TARGET_DIR to verify."
        return 0
    fi
    phase_verify 1
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

main() {
    if [ -z "${BASH_VERSINFO:-}" ] || [ "${BASH_VERSINFO[0]}" -lt 3 ] \
       || { [ "${BASH_VERSINFO[0]}" -eq 3 ] && [ "${BASH_VERSINFO[1]}" -lt 2 ]; }; then
        printf 'install.sh needs bash 3.2 or newer (found %s).\n' "${BASH_VERSION:-unknown}" >&2
        exit 10
    fi

    init_colors
    parse_args "$@"

    if [ "$WANT_HELP" -eq 1 ]; then
        usage
        exit 0
    fi

    detect_mode

    if [ "$WANT_VERSION" -eq 1 ]; then
        version_info
        exit 0
    fi

    if [ "$DOCTOR" -eq 1 ]; then
        run_doctor
        exit 0
    fi

    check_credential_source

    if [ "$MODE" = "local" ]; then
        start_transcript
    fi

    phase_preflight

    if [ "$MODE" = "remote" ]; then
        phase_fetch   # execs the cloned copy; nothing below runs in remote mode
    fi

    phase_runtime
    phase_configure
    phase_database
    phase_verify
    phase_summary
}

main "$@"
