# PortfoliFLOW Operator Handbook

**Tenant lifecycle and platform operations via the `portfoliflow` CLI**

| | |
|---|---|
| Status | v0.2 |
| Scope | Operator / platform-administration tasks (tenant provisioning, super-admin management, diagnostics) |
| Audience | Platform operators with superuser database access |
| Language | English, per ADR-0008 |
| Authoritative sources | ADR-0040 (bootstrap CLI), ADR-0063 (multi-tenant activation, subdomain routing, role model), ADR-0064 (super-admin surface, CLI operations), ADR-0086 (Irene heartbeat tick), ADR-0093 (market-data tick), ADR-0112 (scoped settings, credential vault) |

> This handbook documents the operator surface as implemented. Where it
> and an ADR disagree, the ADR wins — report the drift rather than
> following this document.

---

## 1. Concepts

PortfoliFLOW is multi-tenant from the schema upward, with per-tenant
isolation enforced by PostgreSQL row-level security (RLS). Two tenants
are anchored by hardcoded UUIDs:

- **System tenant** — UUID `00000000-…-0000`, subdomain `admin`, name
  "Platform Administration". Hosts super-admin accounts and nothing
  else. A schema-level `CHECK` binds `is_super_admin = TRUE` to this
  tenant.
- **Primary tenant** — UUID `00000000-…-0001`, subdomain
  `minathena-capital`, name "Minathena Capital". The primary tenant
  installed by bootstrap; carries the demo identity Minathena Capital.

Every other tenant (for example a demo or screenshot tenant) is created
at runtime through the CLI and gets its own subdomain.

**Tenant resolution** is subdomain-based. The resolver maps a request's
`Host` header to a tenant id by reading
`tenants WHERE subdomain = … AND is_active = TRUE`. There is no
"default" tenant at the web layer — an unknown subdomain returns 404.

**Roles** (per-user, within a tenant): `owner`, `member`, `auditor`.
Separately, `is_super_admin` is a platform-level flag that lives only in
the system tenant.

---

## 2. Prerequisites

1. **Install the project (provides the `portfoliflow` console script).**
   From the repository root, inside the project virtual environment:

   ```bash
   pip install -e ".[dev]"
   ```

   The `portfoliflow` command is the console script registered in
   `pyproject.toml` (`portfoliflow = "cli:app"`). There is no
   `python -m cli` entry point.

2. **Configure `.env`.** The CLI connects to PostgreSQL as the
   **superuser** — the only code path permitted to do so. It therefore
   requires `DATABASE_URL_SUPERUSER` (see `.env.example`):

   ```
   DATABASE_URL_SUPERUSER=postgresql+asyncpg://postgres:…@localhost:5432/portfoliflow_dev
   ```

   Application code (FastAPI web app, bot) never uses this — it connects as
   the unprivileged `portfoliflow_app` role via `DATABASE_URL`.

3. **A running database with the schema at head.** Apply migrations with
   `alembic upgrade head` (the Alembic config lives at `db/alembic.ini`),
   and run `portfoliflow bootstrap` once so the system and primary
   tenants exist (see §6).

4. **An existing super-admin** is required for `create-tenant` and
   `create-user`. Those commands attribute the action to the super-admin
   named by `SUPER_ADMIN_EMAIL` in `.env`, and refuse to run if it is
   unset or does not match an active super-admin. See §4 to create the
   first one.

> **Password handling.** Every command that takes a password reads it
> from stdin (`--password-stdin` / `--owner-password-stdin`) or from a
> named environment variable. Passwords are never passed as flags.

---

## 3. Quick start — create a screenshot / demo tenant

Goal: a self-contained tenant, isolated by RLS from Minathena Capital,
suitable for neutral screenshots.

**Step 1 — Ensure a super-admin exists.** Check first:

```bash
portfoliflow status
```

If the report shows no super-admin, create one (then put the same email
into `SUPER_ADMIN_EMAIL` in `.env`):

```bash
echo -n 'choose-a-strong-password' | portfoliflow create-super-admin \
    --email ops@example.com --password-stdin
```

**Step 2 — Create the tenant.** Subdomain rules: `^[a-z][a-z0-9-]*$`,
3–63 characters, and not one of the reserved values `admin`, `www`,
`api`. The command creates the tenant, its initial owner, and the audit
row atomically, then installs default seeds (SAA seeds, an unclassified
sector, default regions):

```bash
echo -n 'demo-owner-password' | portfoliflow create-tenant \
    --name "Demo Capital Partners" \
    --subdomain demo \
    --owner-email owner@demo.example \
    --owner-password-stdin \
    --owner-display-name "Demo Owner"
```

The command is idempotent on the subdomain: re-running with the same
`--subdomain` returns the existing tenant rather than failing.

**Step 3 — Make the tenant reachable in the browser.** See §5. The
quickest route for local dev: add the subdomain to `/etc/hosts`

```
127.0.0.1 demo.localhost minathena-capital.localhost admin.localhost
```

then open `http://demo.localhost:8000` and sign in as
`owner@demo.example`.

**Step 4 — Load synthetic data.** Sign in as the demo owner and use the
web **Upload and Import** action with the synthetic example workbook
shipped in the repository, `sample_data/PortfoliFLOW_example_portfolio.xlsx`.
RLS keeps this data isolated from every other tenant. Use synthetic data
only — never real LP data.

---

## 4. Command reference

All commands run as `portfoliflow <command> [options]`. Passwords are
read from stdin or from the listed environment variable.

### `bootstrap`

Idempotent initialisation of the system tenant (`admin`), the primary
tenant (`minathena-capital`) and its owner, and — when
`SUPER_ADMIN_EMAIL` **and** `SUPER_ADMIN_PASSWORD` are both set — the
first super-admin. Also installs the standard seed data. Safe to re-run.

| Option | Notes |
|---|---|
| `--password-stdin` | Owner password on stdin. Falls back to `OWNER_PASSWORD` env var. |
| `--email` | Owner email. Falls back to `OWNER_EMAIL` env var. |

```bash
echo -n "$OWNER_PASSWORD" | portfoliflow bootstrap --password-stdin
```

### `create-super-admin`

Create a super-admin in the system tenant (idempotent on email).

| Option | Notes |
|---|---|
| `--email` | Required. |
| `--password-stdin` | Password on stdin. Falls back to `USER_PASSWORD` env var. |
| `--display-name` | Optional human display name. |

```bash
echo -n 'pw' | portfoliflow create-super-admin --email ops@example.com --password-stdin
```

### `create-tenant`

Provision a new tenant with an initial owner; installs default seeds.
Requires `SUPER_ADMIN_EMAIL` to match an existing active super-admin.
Idempotent on subdomain.

| Option | Notes |
|---|---|
| `--name` | Required. Display name. |
| `--subdomain` | Required. `^[a-z][a-z0-9-]*$`, 3–63 chars, not `admin`/`www`/`api`. |
| `--owner-email` | Required. |
| `--owner-password-stdin` | Owner password on stdin. Falls back to `OWNER_PASSWORD` env var. |
| `--owner-display-name` | Optional. |

Exit codes: `2` validation error (bad/reserved/taken subdomain, bad
email, missing password), `3` other operational failure.

### `create-user`

Create a user in a target tenant. The `--tenant` argument accepts either
a subdomain or a tenant UUID. Requires `SUPER_ADMIN_EMAIL` to match an
active super-admin.

| Option | Notes |
|---|---|
| `--tenant` | Required. Subdomain or UUID. |
| `--email` | Required. |
| `--roles` | Comma-separated. Default `member`. Valid: `owner`, `member`, `auditor`. |
| `--password-stdin` | Password on stdin. Falls back to `USER_PASSWORD` env var. |
| `--display-name` | Optional. |

```bash
echo -n 'pw' | portfoliflow create-user \
    --tenant demo --email analyst@demo.example --roles member --password-stdin
```

### `inspect-tenant`

Read-only diagnostic snapshot of a single tenant. **Mandatory
`--reason`**; every invocation writes two audit rows (per ADR-0064 §3).

| Option | Notes |
|---|---|
| `--tenant` | Required. Subdomain or UUID. |
| `--reason` | Required, non-empty. Audited justification. |

```bash
portfoliflow inspect-tenant --tenant demo --reason "screenshot tenant verification"
```

### `status`

Non-destructive snapshot: schema head, tenant/user state (total counts,
primary-tenant presence), and AI configuration. No options. Use it as
the first diagnostic step.

### `set-password`

Rotate the password of an existing user. Invalidates that user's active
sessions on rotation.

### `reset-dev` — destructive, dev only

Truncates every domain table, reinstalls the sentinel/primary owner and
re-runs the full bootstrap seed pipeline, so the reset database matches a
fresh `bootstrap`. The schema state (`alembic_version`) is preserved. Gated
by `--confirm`.

| Option | Notes |
|---|---|
| `--confirm` | Required. Without it the command refuses and exits non-zero. |
| `--email` | Sentinel/owner email. Falls back to `SENTINEL_EMAIL` env var. |
| `--password-stdin` | Password on stdin. |

> **Never run `reset-dev` against a database that holds data you want to
> keep.** It is a development convenience only.

### `irene-tick`

Beat every tenant whose Irene schedule is due (ADR-0086). Tenant-blind:
the due tenants are discovered from `irene_schedule` at run time, so the
command takes no options. Designed to be fired by the systemd timer.
Exits 0 on success — including "nothing due" and "a due tenant has no
resolvable credential"; exit 2 on a configuration error (for example
`DATABASE_URL_SUPERUSER` unset), exit 3 on another PortfoliFLOW error. A
single tenant's beat failure never fails the tick. The LLM model is
resolved per tenant (ADR-0112 §4b).

| Option | Notes |
|---|---|
| *(none)* | The command is tenant-blind by design. |

```bash
portfoliflow irene-tick
```

Deployment: [`docs/deploy/README-irene-tick.md`](deploy/README-irene-tick.md).

### `market-data-tick`

Refresh every tenant whose market-data schedule is due (ADR-0093),
mirroring the Irene tick 1:1. Tenant-blind: the due tenants are
discovered from `market_data_schedule` at run time. Same exit-code
contract as `irene-tick` (0 / 2 / 3), and a single tenant's refresh
failure does not fail the tick. No AI/LLM dependency.

| Option | Notes |
|---|---|
| `--tenant` | Restrict the tick to one tenant (UUID or subdomain), bypassing the due gate. **Test seam** — does not persist schedule state. |
| `--provider` | Force the factory to a named provider from the capability matrix (e.g. `synthetic`). **Test seam** — does not persist schedule state. |

```bash
portfoliflow market-data-tick
portfoliflow market-data-tick --tenant minathena-capital --provider synthetic
```

Deployment: [`docs/deploy/README-market-data-tick.md`](deploy/README-market-data-tick.md).

### `vault-generate-key`

Emit a fresh Fernet master key for the credential vault (ADR-0112 §2).
Prints exactly one line — the key — so it can be piped straight into a
secret store. No database connection and no other output. The command
stores nothing: placing the key in the deployment environment as
`CREDENTIAL_VAULT_MASTER_KEY`, and keeping it out of the repository, is
the operator's duty.

| Option | Notes |
|---|---|
| *(none)* | |

```bash
portfoliflow vault-generate-key
```

### `vault-rotate-key`

Re-encrypt every vault secret under a new master key (ADR-0112 §2).
Reads all `is_secret` rows across all tenants on the superuser engine (a
sanctioned RLS-bypassing path, like `inspect-tenant`), decrypts each with
the old key, re-encrypts with the new one, and commits in a **single
transaction** — the vault is never left half rotated, and a row that will
not decrypt aborts the whole rotation. Afterwards replace
`CREDENTIAL_VAULT_MASTER_KEY` in the deployment environment and restart
the process; retain the old key only until the rotation is confirmed,
then destroy it.

| Option | Notes |
|---|---|
| `--new-key-stdin` | Read the new key from stdin. **Required** — the new key is never a flag value. |
| `--old-key-stdin` | Read the old key from stdin instead of `CREDENTIAL_VAULT_MASTER_KEY`. With `--new-key-stdin` the order is old first, then new — one per line. |

```bash
printf '%s\n' "$NEW_KEY" | portfoliflow vault-rotate-key --new-key-stdin
```

Custody and rotation procedure:
[`docs/deploy/credential-vault.md`](deploy/credential-vault.md).

---

## 5. Reaching a tenant in local development

Because resolution is subdomain-based, the host you point the browser at
determines which tenant you land in. Two options:

**Option A — `/etc/hosts` (recommended).** RFC 6761 reserves the
`.localhost` TLD for loopback. Map each tenant subdomain to `127.0.0.1`:

```
127.0.0.1 admin.localhost minathena-capital.localhost demo.localhost
```

Then each tenant has its own URL —
`http://minathena-capital.localhost:8000`,
`http://demo.localhost:8000`, `http://admin.localhost:8000` — and they
coexist in parallel browser tabs without restarting the server.

**Option B — `LOCAL_DEV_TENANT_SUBDOMAIN` env var.** When the request
host is a plain single label (`localhost`, `127.0.0.1`, the ASGI test
client), the resolver falls back to the subdomain named by this variable:

```
LOCAL_DEV_TENANT_SUBDOMAIN=demo
```

Then `http://localhost:8000` maps to the demo tenant. Limitation: only
one tenant at a time, and switching requires editing `.env` and
restarting. Prefer Option A for screenshot work across multiple tenants.

If neither applies, a plain `localhost` request raises an unknown-
subdomain error by design (no silent default).

---

## 6. First-time platform setup (reference)

```bash
# 1. Install
pip install -e ".[dev]"

# 2. Apply schema
alembic -c db/alembic.ini upgrade head      # adjust invocation to your setup

# 3. Bootstrap system + primary tenants (+ first super-admin if env set)
echo -n "$OWNER_PASSWORD" | portfoliflow bootstrap --password-stdin

# 4. Verify
portfoliflow status
```

After this, `create-tenant` / `create-user` are available for any
additional tenants.

---

## 6a. Guided installer (`scripts/install.sh`)

`scripts/install.sh` performs the whole of §6 unattended (ADR-0124 §2):
it checks the prerequisites, creates `.venv`, writes a `.env` whose
Postgres, application-role and vault secrets are generated per
installation, delegates the database to `scripts/db-init.sh`, and
verifies the result with `portfoliflow status`.

```bash
./scripts/install.sh                       # from inside a checkout
./scripts/install.sh --engine docker       # force the container engine
./scripts/install.sh --db-port 5433        # a host Postgres already holds 5432
./scripts/install.sh --no-ai               # skip the OpenRouter questions
./scripts/install.sh --force               # re-configure over an existing .env
./scripts/install.sh --doctor              # check only; changes nothing
```

`--doctor` re-runs the preflight and the verification and writes
nothing. **Its output is what a support issue should carry** — it names
the engine, the Compose provider, the interpreter, the container and
volume state, and which check failed.

Exit codes: `0` success · `1` generic failure · `2` bad usage · `10`
unsupported platform · `11` missing prerequisite · `12` the database
port is in use · `13` refused (existing installation without `--force`,
or `--force` while the data volume still exists).

The installer never uses `sudo`, never installs system packages, and
never writes outside the target directory. A missing prerequisite prints
the exact command for the detected package manager and stops.

Every release attaches `install.sh.sha256` — the SHA-256 of that
release's `scripts/install.sh` — as an asset on the
[Releases page](https://github.com/ProcessReengineer/PortfoliFLOW/releases),
so a downloaded copy can be checked with `shasum -a 256 install.sh`
before it is run (ADR-0124 §3). The installer itself is exercised by
`.github/workflows/installer.yml`: on every pull request that touches
`scripts/`, on `main`, and weekly on Monday at 05:17 UTC, which is what
catches an installer broken by a moving runner image or Compose provider
rather than by a commit.

---

## 7. Verification and troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| `create-tenant`/`create-user` refuses with a super-admin message | `SUPER_ADMIN_EMAIL` unset or not matching an active super-admin | Create one with `create-super-admin`, set `SUPER_ADMIN_EMAIL` in `.env`, retry |
| `DATABASE_URL_SUPERUSER is not set` | Missing superuser URL | Add it to `.env` (see `.env.example`) |
| Subdomain rejected | Fails `^[a-z][a-z0-9-]*$`, too short/long, or reserved (`admin`/`www`/`api`) | Choose a compliant, non-reserved subdomain (≥3 chars) |
| Browser shows 404 at `…​.localhost:8000` | Subdomain not in `/etc/hosts`, tenant inactive, or wrong subdomain | Add the `/etc/hosts` entry; confirm the tenant with `inspect-tenant`; check `is_active` |
| `localhost:8000` shows the wrong/no tenant | No `LOCAL_DEV_TENANT_SUBDOMAIN`, or stale value | Set it and restart, or switch to the `.localhost` host (Option A) |
| Seeds missing after `create-tenant` | The post-creation seed step failed (the tenant itself still committed) | Re-run the seed installer or fill defaults via the SAA UI; check the warning in the CLI log |
| `install.sh` exits 13 with "`.env` exists" | An installation is already configured here | Check it with `./scripts/install.sh --doctor`; to re-configure, `./scripts/install.sh --force` — and since that regenerates every secret, drop the data volume first with `<compose> down -v` (this deletes all data) |

---

## 8. Optional: Voice I/O for Shirley (ADR-0076, ADR-0118)

Shirley can take a spoken question and answer in voice, on both the web chat and via
Telegram **voice messages**. Voice is **additive**: when it is off (the default), every
text and image path behaves exactly as before, and the voice affordances are hidden.

**Enable it per tenant** under Admin → Providers & Credentials: the **Voice** card's
`enabled` toggle, plus the **Voice — speech-to-text** and **Voice — text-to-speech** cards
for the credentials and models. Or **application-wide** by setting the `VOICE_*` keys in
`.env` (authoritative reference and full comments live in `.env.example`):

```
VOICE_ENABLED=true
VOICE_STT_API_KEY=…          # OpenAI key for transcription
VOICE_TTS_API_KEY=…          # OpenAI key for synthesis (may equal the STT key)
```

A tenant row outranks the environment value for the same field; `.env` is the
application-scope fallback beneath it.

The model/voice/base-URL defaults (`gpt-4o-mini-transcribe`, `gpt-4o-mini-tts`, `nova`,
`https://api.openai.com/v1`) are sensible out of the box; override them per tenant or in
`.env` if needed. Both surfaces resolve the configuration **per voice interaction** — per
web turn and per Telegram voice message — so a change at either scope applies on the next
message, with no restart.

Operational notes:

- **Separate credentials.** Audio does **not** route through OpenRouter (it does not proxy
  audio endpoints). The voice credentials — tenant rows or the `VOICE_*` keys — are
  independent of the OpenRouter key.
- **No silent fallback.** A missing/invalid key, an empty transcript, an unsupported audio
  format, or a synthesis failure surfaces a clear message; it never fails silently. Voice
  left off for a tenant simply hides the affordances; voice switched on with no resolvable
  credential answers with an error naming both scopes you can fix it in. Neither ever
  blocks web or bot startup.
- **Audio is not persisted.** Recordings and synthesised replies are processed in memory for
  one turn only; the transcript becomes the chat history, mirroring the image-input contract.
- **Web** needs HTTPS for the microphone (`getUserMedia`); Caddy provides TLS in deployment,
  and `localhost` is exempt for local development.
- **Telegram** uses voice **messages**, not calls. A spoken reply is sent as an OGG/Opus
  voice note; if a client rejects the container it falls back to an audio file.

**Smoke test** (with voice enabled for the tenant, at either scope):
1. Web: open `/assistants#shirley`, toggle voice, record a question — confirm the transcript
   appears as your line, the answer streams, and the spoken reply plays.
2. Telegram (with `TELEGRAM_BOT_ENABLED=true`): send a voice message — confirm the reply
   arrives both as text and as a **playable voice note**.
3. Confirm a normal text message still works unchanged on both surfaces.

---

## 9. Notes

- **Audit.** Super-admin actions are recorded in a dedicated audit
  table separate from per-tenant audit logs. `inspect-tenant` requires
  and audits a `--reason`.
- **Web admin surface.** A super-admin web surface exists (under the
  `admin` tenant) for tenant management. The CLI documented here is the
  canonical, scriptable path and the emergency fallback; treat it as
  authoritative for operator runbooks.
- **Idempotency.** `bootstrap`, `create-super-admin`, `create-tenant`
  and `create-user` are idempotent on their natural key, so they are
  safe to re-run in provisioning scripts.

---

## 10. Related operator docs

Scheduled background ticks are deployed as systemd units, each with its
own operator README:

- [`docs/deploy/README-irene-tick.md`](deploy/README-irene-tick.md) — the
  Irene heartbeat tick (ADR-0086): install, cadence, environment, exit
  codes.
- [`docs/deploy/README-market-data-tick.md`](deploy/README-market-data-tick.md)
  — the market-data import tick (ADR-0091).

Two further operator docs cover surfaces that are not ticks:

- [`docs/deploy/telegram-multi-bot.md`](deploy/telegram-multi-bot.md) —
  the multi-tenant Telegram bot (ADR-0112 §5): token discovery, user
  pairing, and the single-worker constraint that N bots make
  load-bearing.
- [`docs/deploy/credential-vault.md`](deploy/credential-vault.md) —
  credential-vault operations (ADR-0112 §2): master-key custody,
  rotation, and what happens when the key is missing.

---

*Successor work: render this handbook as a styled PDF once the content
is settled.*
