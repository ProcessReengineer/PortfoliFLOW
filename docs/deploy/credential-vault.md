# Credential vault — master-key custody

Operator runbook for the credential vault introduced by ADR-0112. This
note is the **custody procedure**; the CLI invocations it names landed
with strand F1 (migration `b032`, `services/credential_vault`,
`cli/vault.py`).

## Purpose & scope

The vault is the encryption layer over the `scoped_settings` table
(ADR-0112 §2). Rows flagged `is_secret` — provider API keys, the
Telegram bot token, and any future credential field — are stored as
Fernet ciphertext in `value_ciphertext`; non-secret configuration rows
(model names, base URLs) stay in plain `value_plain` so they remain
greppable for support. Encryption is **application-level**: the
database never sees plaintext secrets and never sees the key. There is
no KMS and no external secret manager at this scale (ADR-0112 §7) —
the master key lives only in the deployment's environment.

## The master key

`CREDENTIAL_VAULT_MASTER_KEY` is a single **Fernet key** (a
url-safe base64-encoded 32-byte key). It is the only thing standing
between the ciphertext column and plaintext credentials.

- It is **never stored in the database** — not in `scoped_settings`,
  not in any settings table, not in an audit row.
- It is **never committed to the repository**.
- It enters the process through `.env` or the process environment on
  the server (for a systemd unit: `EnvironmentFile=`, not a literal in
  the unit file).
- The variable is documented in `.env.example` **as of strand F1**,
  the strand that gave it its first reader.

## Generation

Generate a fresh key with:

```
portfoliflow vault-generate-key
```

The command prints exactly one line — the key — on stdout and touches
no database, so it pipes straight into a secret store:

```
portfoliflow vault-generate-key > /run/secrets/portfoliflow_vault_key
```

Do not hand-roll a key from an arbitrary string; Fernet requires the
exact key format (url-safe base64-encoded 32 bytes), and a malformed
value is rejected the first time a cipher is constructed.

## Storage & custody

Operator duties for a hosted deployment:

- Keep the key **outside the repository** — in the deployment's secret
  store, or in a protected `.env` on the server (owned by the service
  account, mode `0600`).
- Limit read access to the **service account** that runs the web
  process and the CLI. No developer laptop copy unless that laptop is
  itself a deployment.
- Keep exactly one authoritative copy plus whatever your backup policy
  requires — and note that a key backup is a *credential* backup by
  proxy, so it inherits the same handling rules.

**Loss of the key means loss of every encrypted credential value.**
There is no recovery path, by design: without the key the ciphertext
is unreadable, and no escrow copy exists anywhere in the system.
Recovery is operational, not cryptographic — tenants re-enter their
credentials through the Providers & Credentials surface (ADR-0112 §6),
and the affected rows are overwritten with values encrypted under the
new key.

## Rotation

Rotation is a **documented operator procedure, not an automatic
mechanism** (ADR-0112 §2). To rotate:

```
printf '%s\n' "$NEW_KEY" | portfoliflow vault-rotate-key --new-key-stdin
```

The **old** key is read from `CREDENTIAL_VAULT_MASTER_KEY` in the
environment; the **new** key is read from stdin and only from stdin —
`--new-key-stdin` is mandatory, so a key never lands in the shell
history or the process table. To supply both on stdin, pass
`--old-key-stdin` as well and write **old first, then new**, one per
line:

```
printf '%s\n%s\n' "$OLD_KEY" "$NEW_KEY" \
  | portfoliflow vault-rotate-key --old-key-stdin --new-key-stdin
```

The procedure it performs:

1. Read **all** `is_secret` rows across all tenants — a cross-tenant
   read that runs on the **superuser engine**, the same sanctioned
   RLS-bypassing pattern as `portfoliflow inspect-tenant` and the
   bootstrap/Alembic CLIs.
2. Decrypt each with the **old** key, re-encrypt with the **new** key.
3. Commit in a **single transaction** — the vault is never left half
   rotated.

On success the command prints the number of re-encrypted rows and
exits 0 (including the "no secret rows yet" case). If any row fails to
decrypt with the old key the transaction **rolls back in full** — no
row is changed — and the command exits 3 with a message naming the
offending row id and the counts. Log lines state counts, providers and
key names only; a value is never written to the log, the message, or
the audit trail. Exit 2 means a configuration problem (missing or
malformed key, `DATABASE_URL_SUPERUSER` unset).

Afterwards, replace `CREDENTIAL_VAULT_MASTER_KEY` in the deployment
environment with the new key and restart the process. Retain the old
key only until the rotation is confirmed, then destroy it.

## Missing-key behaviour

When `CREDENTIAL_VAULT_MASTER_KEY` is unset, the application does not
degrade quietly and does not improvise (ADR-0112 §2):

- The resolver's **vault source is disabled**, with **one WARNING at
  first use** — not one per resolution.
- Reads of secret rows are **not attempted**.
- Writes through the Providers & Credentials admin surface **fail with
  a typed, operator-readable error**.
- There is **never a silent plaintext mode**, and the vault is never
  half-served.

A deployment with no master key therefore behaves like a deployment
with no vault rows: every credential resolution falls through to the
application scope (the environment), which is exactly the
single-operator posture that predates ADR-0112.

## References

- **ADR-0112** — Scoped Settings & Credential Architecture
  (`docs/adr/0112-scoped-settings-and-credential-architecture.md`);
  §2 is the source for storage, rotation and missing-key behaviour,
  §6 for the management surface, §7 for the explicit no-KMS decision.
- **ADR-0095** — Provider Credential Vault
  (`docs/adr/0095-provider-credential-vault.md`); §1–§3 remain the
  authoritative resolution contract (ordered sources, per-provider
  `env_fallback` policy, environment source declaration). Its §4
  storage design is superseded by ADR-0112 §2.
