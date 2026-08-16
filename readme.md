# PortfoliFLOW

AI-native platform for institutional portfolio management — built for allocators such as Versorgungswerke, family offices, endowments, asset managers and fund-of-funds boutiques. Private-markets and fund-of-funds structures are first-class; the platform is not limited to them.

**What it does, how it looks and who it is for is explained on the homepage: [portfoliflow.com](https://portfoliflow.com).** This README covers only what you need to get a local instance running.

---

## Requirements

- **Python 3.11 or newer**
- **Podman or Docker** with the Compose plugin — runs the PostgreSQL 16 container; no host-side Postgres install needed
- **Linux or macOS** as the reference platforms (Windows works via WSL2; the helper scripts are Bash)
- A **virtual environment** (strongly recommended)

---

## Installation

The release ships in the [`Releases/`](Releases/) folder of this repository. Every block below is paste-able as a whole; run them from a shell in the order shown.

### 1. Get the code

Either unpack the release archive:

```bash
tar -xzf Releases/portfoliflow-<version>.tar.gz
cd portfoliflow-<version>
```

or clone the repository:

```bash
git clone https://github.com/ProcessReengineer/PortfoliFLOW.git
cd PortfoliFLOW
```

### 2. Install PortfoliFLOW

```bash
python -m venv .venv
source .venv/bin/activate            # Windows (WSL2 recommended): .venv\Scripts\activate
pip install --upgrade pip
pip install -e .
```

This installs everything the platform needs at runtime — FastAPI/Uvicorn, SQLAlchemy + asyncpg, Alembic, pandas/SciPy, Plotly, the `portfoliflow` operator CLI and the `portfoliflow-web` server. Add `".[dev]"` for the test and lint tooling, `".[bot]"` for the optional Telegram bot.

### 3. Configure

```bash
cp .env.example .env
```

Open `.env` and set at least:

| Key | What to put there |
|---|---|
| `POSTGRES_PASSWORD` | A password for the Postgres superuser (consumed by the container) |
| `DATABASE_URL_SUPERUSER` | The same password inside the URL (used only by migrations, bootstrap and the CLI) |
| `DATABASE_URL` | Leave as is for a local install — the app role is created by the container init script |
| `OWNER_EMAIL` | Login e-mail of the first tenant owner |
| `CREDENTIAL_VAULT_MASTER_KEY` | Recommended — generate one with `portfoliflow vault-generate-key`; without it, provider keys cannot be stored in the Admin UI (only via `.env`) |

Everything else has working defaults. AI-related keys are covered under [Enabling the AI functions](#enabling-the-ai-functions) below.

### 4. Set up PostgreSQL and initialise the database

**One command** (starts the container, waits for it, applies all migrations, creates the first tenant and owner user):

```bash
./scripts/db-init.sh
```

You are prompted once for the owner password. Non-interactive: `./scripts/db-init.sh --password "…"`.

**Or step by step** — the same sequence, engine-neutral:

```bash
podman compose up -d                                   # or: docker compose up -d
until podman exec portfoliflow-postgres pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done
alembic -c db/alembic.ini upgrade head
echo -n "$OWNER_PASSWORD" | portfoliflow bootstrap --password-stdin
```

The container binds to `127.0.0.1:5432` only, keeps its data in the named volume `portfoliflow_postgres_data`, and on **first start** creates the unprivileged `portfoliflow_app` role the application connects with. Migrations are forward-only: `upgrade head` brings any database — fresh or existing — to the current schema.

### 5. Run

```bash
portfoliflow-web
```

Tenants are resolved by subdomain, so open the primary tenant at

```
http://minathena-capital.localhost:8000
```

Most browsers resolve `*.localhost` to loopback on their own; if yours does not, add `127.0.0.1 minathena-capital.localhost admin.localhost` to `/etc/hosts`, or set `LOCAL_DEV_TENANT_SUBDOMAIN=minathena-capital` in `.env` and use `http://localhost:8000`. Sign in with `OWNER_EMAIL` and the password you gave to `bootstrap`.

Verify the install without a browser:

```bash
portfoliflow status              # schema head, tenant/user state, AI configuration
```

### Resetting

```bash
portfoliflow reset-dev --confirm         # truncate all domain tables, re-bootstrap; schema and volume kept
podman compose down -v && ./scripts/db-init.sh   # full from-scratch reset (drops the volume)
```

The role-creation init SQL runs only on the container's first start, so a truly clean slate needs the volume dropped, not just the container stopped.

---

## Enabling the AI functions

Shirley (the chat assistant), Irene (the Watch Desk monitoring engine) and the Report Scraper all speak the OpenAI-compatible chat-completions API. To use them you need **one** of the following:

- **An API key from [OpenRouter](https://openrouter.ai)** — the default. Gives you access to models from Anthropic, OpenAI, Google, Mistral and others behind one key.
- **An API key from any other OpenAI-compatible provider** — point `OPENROUTER_BASE_URL` at that provider's endpoint.
- **A local model** — run Ollama, LM Studio, vLLM or a similar OpenAI-compatible server and point `OPENROUTER_BASE_URL` at it (e.g. `http://localhost:11434/v1`); the key can then be any non-empty string. No data leaves your machine.

Set the application-wide default in `.env`:

```env
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_API_KEY=sk-or-...
SHIRLEY_MODEL=anthropic/claude-sonnet-4.5     # any model id your endpoint serves
```

and restart. Alternatively — and preferably in a multi-user setup — store keys per tenant or per user under **Admin → Providers & Credentials** in the running application; those apply on the next chat turn without a restart and are encrypted at rest (this is what `CREDENTIAL_VAULT_MASTER_KEY` is for). Without any key the server starts normally; only the AI surfaces answer with a pointer back to configuration.

Two notes: the Report Scraper needs a PDF-capable model (Anthropic models are the shipped default); voice input/output for Shirley needs a separate OpenAI (or compatible STT/TTS) key, because OpenRouter does not proxy audio.

---

## Optional components

- **Telegram bot** — `pip install -e ".[bot]"`, set `TELEGRAM_BOT_ENABLED=true`, then store each tenant's BotFather token under Admin → Providers & Credentials. See `docs/deploy/telegram-multi-bot.md`.
- **Live market data** — Yahoo works out of the box; an optional OpenFIGI key raises the identifier-resolution rate limit. Bloomberg Desktop API is fixture-validated and gated behind a local Terminal.
- **Scheduled ticks** — Irene's heartbeat and market-data refresh run inside the web process by default; systemd units for driving them externally are in `docs/deploy/`.

---

## Documentation

- **`docs/operator-handbook.md`** — tenant and user provisioning, credential vault custody, reaching tenants locally, troubleshooting.
- **`docs/architecture.md`** — layering, dependency rules, module lifecycle.
- **`docs/adr/`** — Architecture Decision Records, with a thematic index in `docs/adr/README.md`.
- **`docs/roadmap.md`** — the steering document.
- **`CONTRIBUTING.md`** — how to contribute; a signed CLA is required before the first merge.
- **`SECURITY.md`** — how to report a vulnerability.

Running the tests: `pip install -e ".[dev]"` then `pytest` (some tests need the Postgres container up).

---

## License & Trademarks

PortfoliFLOW is free software, licensed under the **GNU Affero General Public License, version 3** (AGPL-3.0-only) — see [`LICENSE`](LICENSE). If you run a modified version as a network service, the AGPLv3 requires you to offer its users the corresponding source (§13); a hosted instance does this via the "Source code" link in the application footer.

A **commercial license** — relieving the AGPLv3 obligations for partner integrations and proprietary deployments — is available; contact ProcessReengineer@happycomputercollective.org or see [portfoliflow.com](https://portfoliflow.com).

**PortfoliFLOW™** and **Happy Computer Collective™** are trademarks of Sönke Pinkernelle. Trademark rights are not granted by the code license — see [`TRADEMARKS.md`](TRADEMARKS.md).
