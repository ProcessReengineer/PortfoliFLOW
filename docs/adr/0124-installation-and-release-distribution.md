# ADR-0124: Installation and Release Distribution — Guided Installer, Engine-Neutral Bootstrap, and the `stable` Branch

- **Status:** Accepted (2026-08-19)
- **Date:** 2026-08-19
- **Deciders:** PortfoliFLOW project owner
- **Closes:** the installation gap left open by the 2026.08.0 public release —
  the README's manual sequence assumes a container engine, a Compose provider
  and (on macOS) a running Podman machine, none of which are checked, and it
  pins a literal version tag that has to be edited by hand on every release.
- **Supersedes / amends:** amends **ADR-0040** only in that the operator-facing
  entry point to `portfoliflow bootstrap` may now be the installer rather than a
  hand-typed command; the bootstrap contract itself is unchanged. Amends the
  **dev-only password note in `db/init/01-create-app-role.sql`** (§1.2). Does
  not touch ADR-0013/0045 (analytics purity), ADR-0063/0064 (tenant substrate)
  or ADR-0112 (scoped settings).
- **Tags:** installation, release, packaging, operations, ci, developer-experience

---

## Context

PortfoliFLOW went public with 2026.08.0 as an AGPL-3.0 source release. The
intended audience is not exclusively the developer community: allocators
evaluating the platform will include people who are comfortable in a terminal
only to the extent of pasting a line someone gave them. The current README asks
them to run five blocks by hand and silently assumes a working container
substrate.

Verified against the 2026-08-19 Repomix snapshot:

1. **The Postgres image is not the problem.** `compose.yml` references
   `docker.io/library/postgres:16`; the first `compose up -d` pulls it. No
   pre-existing container or Containerfile is required. What *is* required and
   nowhere checked:

   - a container engine at all — nothing in `readme.md` or `scripts/db-init.sh`
     verifies one is installed before the first `podman compose` call;
   - a **Compose provider**. `podman compose` is a thin dispatcher; Podman ships
     no Compose implementation. Without `podman-compose` or a `docker-compose`
     binary on `PATH` it fails with a provider error that means nothing to a
     non-developer;
   - on **macOS**, an initialised and running Podman machine
     (`podman machine init && podman machine start`). This appears in neither
     the README nor the scripts. On a fresh Mac, README step 4 cannot succeed.

2. **`scripts/db-init.sh` is Podman-hard-wired** — the prerequisite loop checks
   `podman`, and every subsequent call is `podman compose` / `podman exec` —
   while `readme.md` §Requirements promises "Podman **or** Docker". A Docker
   user has no one-command path. `scripts/db-reset.sh` has the same shape.

3. **`db-init.sh --help` is GNU-only.** It renders usage with `head -n -2`;
   BSD `head` (macOS) rejects a negative count. The help text is broken on one
   of the two reference platforms.

4. **Secrets cannot be generated.** `db/init/01-create-app-role.sql` carries the
   application role password as a literal (`'app_dev_password_change_me'`) and
   hard-codes the database name in its `GRANT CONNECT`. Files under
   `docker-entrypoint-initdb.d` with a `.sql` extension are fed to `psql`
   directly and cannot read the container environment. As long as that stays
   true, any installer that generates a random application password would have
   to rewrite a version-controlled file — so the shipped default password is
   effectively mandatory, and `DATABASE_URL` in `.env.example` documents it.

5. **Bind mounts are unqualified.** `./db/postgresql.conf` and `./db/init` are
   mounted `:ro` with no SELinux label option. On Fedora/RHEL with SELinux
   enforcing and rootless Podman, the container cannot read them.

6. **Port 5432 is assumed free.** A host-side Postgres — common on developer
   machines — produces a bind failure the script does not interpret.

7. **The clone command is version-literal.** `git clone --branch 2026.08.0`
   requires a README edit per release, and a tag clone leaves the user in
   detached HEAD with no update path. A moving *tag* is not an option: `git
   fetch` does not update an existing tag without `--force`, so different users
   would silently hold different "stable" trees.

## Decision

### 1. Engine-neutral, parameterisable container bootstrap

#### 1.1 One engine abstraction, resolved once

`scripts/db-init.sh` and `scripts/db-reset.sh` resolve the engine and the
Compose provider **once** into two indexed arrays and use only those
afterwards:

```
ENGINE_CMD=(podman)            or (docker)
COMPOSE_CMD=(podman compose)   or (podman-compose) or (docker compose) or (docker-compose)
```

Detection order: an explicit `--engine podman|docker` flag, then the
`PORTFOLIFLOW_ENGINE` environment variable, then Podman, then Docker. Podman
first preserves the repo's stated preference (rootless by default) without
excluding Docker. A resolved engine with no working Compose provider is a hard
error naming the two packages that would fix it — never a silent fallback
(project invariant).

Both scripts gain `--engine`. Neither acquires any other new behaviour in this
ADR.

#### 1.2 Role creation moves from `.sql` to `.sh`

`db/init/01-create-app-role.sql` is replaced by `db/init/01-create-app-role.sh`,
which reads its inputs from the container environment:

```sh
: "${APP_DB_PASSWORD:=app_dev_password_change_me}"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
  CREATE ROLE portfoliflow_app WITH LOGIN PASSWORD '${APP_DB_PASSWORD}';
  GRANT CONNECT ON DATABASE "${POSTGRES_DB}" TO portfoliflow_app;
  ...
SQL
```

`compose.yml` passes `APP_DB_PASSWORD: ${APP_DB_PASSWORD:-app_dev_password_change_me}`.
The literal default is retained deliberately: an existing checkout with an
existing `.env` and an existing volume behaves exactly as before, and the
"DEV ONLY — production must override this before first container start"
comment in the original file remains true and is carried over verbatim. What
changes is that overriding is now possible without editing a tracked file.

The role's non-negotiable properties are unchanged: no `SUPERUSER`, no
`BYPASSRLS`. RLS enforcement through `portfoliflow_app` (ADR-0063 and the
repository-test contract) is untouched.

#### 1.3 Port and SELinux

`compose.yml` binds `"127.0.0.1:${POSTGRES_PORT:-5432}:5432"`, and
`.env.example` documents `POSTGRES_PORT` beside `POSTGRES_DB`. Loopback-only
binding is unchanged and non-negotiable. Both bind mounts gain `,Z`
(`./db/init:/docker-entrypoint-initdb.d:ro,Z`); Docker on non-SELinux hosts
accepts and ignores the suffix, so this is portable.

#### 1.4 GNU-ism removal

`--help` in both scripts is rendered without `head -n -N`. More generally,
every shell file in `scripts/` must run on BSD userland — see §2.1.

### 2. `scripts/install.sh` — the guided installer

#### 2.1 Portability contract

The installer targets **bash 3.2** and **BSD userland**, because macOS ships
`/bin/bash` 3.2 and `bash -c "$(curl …)"` will use it. Consequences, stated so
they are not rediscovered: no associative arrays, no `${var^^}`/`${var,,}`, no
`mapfile`/`readarray`, no `${var@Q}`; no `sed -i` without a backup argument, no
`head -n -N`, no `date -d`, no `grep -P`, no `realpath`. Indexed arrays are
available and are used (§1.1). The script asserts `BASH_VERSINFO` ≥ 3.2 and
refuses otherwise.

The installer is a **single self-contained file** with no `source` of repo
libraries, because in remote mode it runs before any repository exists.

#### 2.2 Two modes, one file

`scripts/install.sh` is both the file served at the one-liner URL and the file
in the repository. It decides at startup:

- **Remote mode** — it is not inside a PortfoliFLOW checkout. It runs Phase 0,
  clones into the target directory, then `exec`s the *cloned* copy with
  `--local-mode`. The code that performs the installation is therefore always
  the code being installed; a stale cached copy at the URL can only ever
  mis-clone, never mis-install.
- **Local mode** — it sits in a checkout (marker: a sibling `pyproject.toml`
  declaring `name = "portfoliflow"`). Phase 1 is skipped.

#### 2.3 Phases

| # | Phase | Content |
|---|---|---|
| 0 | Preflight | OS/arch, bash version, `git`, `curl`, Python ≥ 3.11 (probe `python3.13`/`3.12`/`3.11`/`python3`), engine + Compose provider, Podman machine state on macOS, target-directory writability, chosen DB port free, ≥ 2 GB free |
| 1 | Fetch | `git clone --branch "$REF" --depth 1` into the target directory (remote mode only) |
| 2 | Runtime | `python -m venv .venv`, `pip install --upgrade pip`, `pip install -e .` |
| 3 | Configure | `.env` from `.env.example`; generate `POSTGRES_PASSWORD`, `APP_DB_PASSWORD`, `CREDENTIAL_VAULT_MASTER_KEY`; prompt for `OWNER_EMAIL`, owner password (twice, hidden), optional `OPENROUTER_API_KEY` + model; `chmod 600 .env` |
| 4 | Database | delegate to `./scripts/db-init.sh --engine "$ENGINE" --password-stdin` |
| 5 | Verify | `portfoliflow status` |
| 6 | Summary | tenant URL, login e-mail, start command, where the secrets live |

Phase 4 **delegates** rather than reimplements: by then the clone exists, so
`db-init.sh` is the single implementation of container-start / wait / migrate /
bootstrap. `db-init.sh` gains `--password-stdin` alongside its existing
`--password` so the installer never passes a secret on a command line.

`CREDENTIAL_VAULT_MASTER_KEY` is generated via `portfoliflow vault-generate-key`
in Phase 3 (the venv exists from Phase 2) and written to `.env`. It ceases to be
a "recommended" step a reader may skip; a default installation can store
provider credentials in the Admin UI out of the box.

#### 2.4 Privilege policy

**The installer never invokes `sudo` and never installs system packages.** A
missing prerequisite is a preflight failure that prints the exact command for
the detected package manager (`apt`, `dnf`, `pacman`, `brew`) and exits
non-zero. *Considered:* an opt-in `--install-deps` that prompts before each
privileged command. Rejected for the first version — the trust posture of a
piped one-liner is the whole reason it is acceptable at all, and package-manager
variance across distributions is unbounded surface for a project with one
maintainer. Printing a copy-paste line costs the user one extra step and costs
the project nothing.

Corollaries: no writes outside the target directory, no shell-rc modification,
no service registration, no autostart.

#### 2.5 Idempotence, flags, diagnostics

Re-running is safe. An existing `.venv` is reused, an existing running container
is detected and reported, and an existing `.env` is **never** overwritten
without `--force`.

Flags: `--dir`, `--ref` (default `stable`), `--engine`, `--db-port`,
`--non-interactive`, `--no-ai`, `--force`, `--doctor`, `--version`, `--help`.
In `--non-interactive` mode, `OWNER_EMAIL` and the owner password come from the
environment (`PORTFOLIFLOW_OWNER_EMAIL`, `PORTFOLIFLOW_OWNER_PASSWORD`); an
absent value is an error, never a generated default.

`--doctor` runs Phase 0 and Phase 5 only and changes nothing. It is the
first thing to ask for in a support issue.

`set -euo pipefail` plus an `ERR` trap that reports the failing phase, the
failing command and a phase-specific remediation hint. The full transcript goes
to `install.log` in the target directory. Exit codes are stable and documented
in the script header: `0` success, `1` generic, `2` bad usage, `10` unsupported
platform, `11` missing prerequisite, `12` port in use, `13` refused (existing
installation, no `--force`).

Every interactive prompt reads from `/dev/tty`, not stdin, so the script also
survives being piped. With no TTY and no `--non-interactive`, it exits `2` and
says which form to use.

### 3. The one-liner

Canonical form, documented as the first installation option:

```bash
bash -c "$(curl -fsSL https://portfoliflow.com/install.sh)"
```

`bash -c "$(…)"` rather than `curl … | bash`: with a pipe, stdin *is* the
script, and the interactive prompts of Phase 3 would consume script text.
Arguments pass through with a leading `--` (which becomes `$0`):

```bash
bash -c "$(curl -fsSL https://portfoliflow.com/install.sh)" -- --dir ~/portfoliflow --no-ai
```

**Serving.** `https://portfoliflow.com/install.sh` is a redirect to
`https://raw.githubusercontent.com/ProcessReengineer/PortfoliFLOW/stable/scripts/install.sh`.
The repository stays the single source of truth, there is no deploy step per
release, and the redirect remains a kill switch under project control.
*Considered:* a dedicated `get.portfoliflow.com`. Rejected — an additional DNS
record and certificate for no functional gain, given the homepage already
terminates TLS on the same host. *Considered:* linking `raw.githubusercontent.com`
directly in the README. Rejected — it surrenders the kill switch and pins the
project's install path to a third-party host's URL scheme.

**Truncation safety.** The entire script body is a function definition; the last
executable line is `main "$@"`. A download cut short cannot execute a partial
program. `curl -f` is mandatory in every documented invocation so an HTML error
page is never handed to bash.

**Verification.** Each release publishes `install.sh.sha256` as a release asset.
The README documents the two-step path — download, inspect, check, run — with
equal prominence, not as a footnote:

```bash
curl -fsSLO https://portfoliflow.com/install.sh
shasum -a 256 install.sh          # compare against the release asset
less install.sh
bash install.sh
```

### 4. `stable` as a branch

A branch named `stable` tracks the most recent non-pre-release tag. `git clone
--branch` accepts a branch or a tag identically, so the README command shape is
unchanged:

```bash
git clone --branch stable --depth 1 https://github.com/ProcessReengineer/PortfoliFLOW.git
```

A branch, not a moving tag: tags are immutable by convention and are not
refreshed by `git fetch` without `--force`, so a moving tag would leave
different users on different trees with no way to tell. A branch clone also
gives a tracking ref, which turns updating into `git pull` in place — a tag
clone leaves detached HEAD and no update path at all.

`stable` is advanced by a workflow rather than by hand, so it can never be
forgotten:

```yaml
# .github/workflows/promote-stable.yml
name: promote-stable
on:
  push:
    tags: ['[0-9][0-9][0-9][0-9].[0-9][0-9].[0-9]*']
jobs:
  promote:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4
      - name: Skip pre-releases
        run: case "$GITHUB_REF_NAME" in *-*) echo "pre-release"; exit 78 ;; esac
      - name: Version must match tag
        run: grep -qx "version = \"$GITHUB_REF_NAME\"" pyproject.toml
      - name: Advance stable
        run: git push --force origin "$GITHUB_SHA:refs/heads/stable"
```

The version check is not incidental: it makes tag/`pyproject.toml` drift a
release-blocking failure rather than something discovered later.

Branch protection covers `stable` with force-push permitted only through the
project owner's existing admin bypass. Users who cloned the `2026.08.0` tag are
not retroactively helped; `stable` benefits everyone from the next release
onward.

### 5. README

The installation section leads with the one-liner, then the verified two-step
form, then the manual sequence for readers who want to see every step. The
manual sequence uses `--branch stable` and drops the literal archive filename in
favour of a pointer to the Releases page. The Requirements table names the
Compose provider and the macOS `podman machine` step explicitly.

The manual sequence and `install.sh` are two descriptions of one procedure and
will drift. §6 is the mitigation.

### 6. CI

A new `installer` workflow runs on pull requests touching `scripts/`, `db/`,
`compose.yml` or `readme.md`, and on a weekly schedule:

- `shellcheck` over `scripts/*.sh`;
- **ubuntu-latest**: `install.sh --non-interactive` end-to-end against Docker
  (the runner's native engine), asserting `portfoliflow status` succeeds;
- **macos-latest**: the same, invoked explicitly as `/bin/bash scripts/install.sh`
  so the bash-3.2 contract is exercised, with Podman from Homebrew and
  `podman machine` started by the job — which also proves the preflight's
  machine detection on the only platform that has one;
- a re-run of the installer in the same workspace, asserting the idempotence
  path (§2.5) and a non-zero exit when `.env` exists without `--force`.

The weekly schedule matters more than the PR trigger: installer rot is caused by
the outside world (image tags, Homebrew formulae, GitHub runner images), not by
commits to this repository.

## Implementation strands

| Strand | Scope | Depends on |
|---|---|---|
| **I1** | §1 — engine abstraction in `db-init.sh`/`db-reset.sh` (`--engine`, `--password-stdin`), `01-create-app-role.sh`, `POSTGRES_PORT`, `,Z`, GNU-ism removal, `.env.example` | — |
| **I2** | §2 — `scripts/install.sh` | I1 |
| **I3** | §4 — `promote-stable.yml`, branch protection, README clone command on `stable` | — |
| **I4** | §3, §5, §6 — redirect, `install.sh.sha256` release asset, README rewrite, `installer` workflow | I2, I3 |

Each strand is one Claude Code prompt with a verify-first phase and an explicit
not-in-scope list. I1 lands before I2 because §2.3 Phase 3 cannot generate an
application-role password until §1.2 exists.

## Consequences

- A non-technical evaluator can install PortfoliFLOW by pasting one line, and
  every failure mode that line can hit produces a named cause and a copy-paste
  remedy rather than a stack trace.
- Docker becomes a genuinely supported engine rather than a README claim.
- A default installation carries per-installation secrets — Postgres superuser,
  application role, vault master key — instead of the shipped development
  literals. Existing checkouts are unaffected.
- The release ritual loses a manual step and gains a guard: tagging is
  sufficient, and a tag whose `pyproject.toml` disagrees fails loudly.
- The project acquires a maintenance obligation on two operating systems. The
  weekly CI run is the mechanism that makes that obligation visible before users
  find it.
- Tests and docs to update: `docs/operator-handbook.md` (installer, `--doctor`,
  `--engine`), `readme.md` (§5), `.env.example` (`POSTGRES_PORT`,
  `APP_DB_PASSWORD`), `CONTRIBUTING.md` (shell portability contract), any
  fixture or doc referencing `db/init/01-create-app-role.sql` by name.

## Not in scope

- The hard-coded primary tenant (`Minathena Capital` / `minathena-capital` in
  `cli/bootstrap.py`) and the `portfoliflow_dev` database name. A real
  first-time installer should be able to name its own tenant; that is a product
  decision with its own ADR, and mixing it into the installer would make the
  installer's contract depend on an unsettled question.
- Windows beyond WSL2.
- OS packages, container images of the application itself, Homebrew formulae,
  PyPI publication.
- Reverse proxy, TLS termination, systemd units for the web process, or any
  other production deployment concern.
- An in-place `--upgrade` path. The `stable` tracking branch (§4) makes one
  cheap to add later; it is deliberately not designed here.
- GPG-signed tags. Worth doing, independent of this decision.
