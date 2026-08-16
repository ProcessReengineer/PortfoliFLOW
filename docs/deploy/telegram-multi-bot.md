# Telegram: one bot per tenant

**Decision:** ADR-0112 §5. **Landed:** strand F5.

PortfoliFLOW runs **one Telegram bot per tenant** out of a single web
process. Each tenant stores its own BotFather token in the credential
vault; at start-up the bot process discovers every stored token and gives
each one its own `Bot`, `Dispatcher` and polling task on one shared event
loop, in one daemon thread.

Two operator-visible consequences follow, and both are covered below: a
token change **applies at the next restart**, and the deployment must run
**exactly one web worker**.

---

## 1. Give a tenant its own bot

1. Create the bot with [@BotFather](https://t.me/BotFather) and copy the
   token.
2. Log in to that tenant as an **owner** and open
   Admin → **Providers & Credentials** → *Tenant credentials* → Telegram.
3. Paste the token into **Bot token** and save. It is encrypted with
   `CREDENTIAL_VAULT_MASTER_KEY` before it reaches the database — see
   `credential-vault.md`. The vault must be configured, or the secret
   field is disabled and the write refused.
4. Restart the web process. The start-up log names each dispatcher:

   ```
   Telegram bot [tenant=… source=vault]: dispatcher registered.
   Telegram bot: polling 2 dispatcher(s).
   ```

To switch a tenant's bot off without deleting the token, set the Telegram
**Enabled** field to `false` (or disable the `bot_token` row). Discovery
skips it with an INFO line. An absent `Enabled` row means enabled — it is
an opt-out, not an opt-in.

### Restart-to-apply

The dispatcher set is discovered **once**, at bot start. There is no
rescan timer in v1: writing, changing or removing a token has no effect
until the web process restarts. This is deliberate — a bot that swaps its
token mid-flight would drop updates — and the admin surface says so on the
Telegram card ("token changes apply after a bot restart").

Everything *else* about a turn is live: the OpenRouter key and model
resolve per message (ADR-0112 §4b), so those changes need no restart.

---

## 2. Pair a user to a chat

Authorisation is a **pairing binding**, not a whitelist. A user proves
they control a Telegram chat by redeeming a code in it; the bot then
stores that chat id as their own user-scope setting, and every turn from
that chat runs **as that user** — including their personal model choice.

Walkthrough:

1. The user opens Admin → Providers & Credentials → *My settings* →
   Telegram and clicks **Generate pairing code**. The code is shown once,
   is valid for five minutes, and works once.
2. From the Telegram chat they want to use, they send:

   ```
   /pair ABCD1234
   ```

3. The bot replies that the chat is linked. From then on it answers that
   chat, and the panel shows **paired** with the bound chat id.
4. **Revoke pairing** deletes the binding (and any code still pending).
   The next message from that chat is dropped silently — the bot never
   tells an unknown chat that it exists.

Notes:

* A code only works on the bot of the **tenant it was minted in**. A
  wrong-tenant, expired or unknown code all get the same reply, so the
  chat is no oracle for which it was.
* `/pair` attempts are throttled per chat (5 per 10 minutes).
* Pending codes live in process memory, so a restart voids them. Generate
  a new one.
* Generating a second code invalidates the first.

---

## 3. Single worker — now load-bearing for N bots

Telegram allows exactly **one `getUpdates` consumer per token**. The bot
polls, so a second uvicorn worker would start a second copy of *every*
tenant's bot and each copy would steal roughly half the other's updates.

Run one worker. This was already the assumption for `pending_turns` and
the process-wide turn lock; with per-tenant bots it now protects every
tenant at once. If you need more web capacity before multi-worker support
lands, scale vertically or put the bot in its own single-worker process.

---

## 4. Failure containment

Per-dispatcher supervision means one tenant's problem stays with that
tenant:

| What happens | Effect |
|---|---|
| A token is revoked at BotFather | One ERROR naming the tenant; **that** dispatcher stops. Others keep polling. |
| The network drops | One WARNING, then capped-backoff retries until it returns. |
| A stored token will not decrypt (wrong master key) | That tenant is skipped at discovery with an ERROR naming the row id; the others start. |
| The token scan itself fails | Logged; the process runs with whatever the environment token can serve. |
| Anything above | The web app is unaffected. A bot failure never blocks web start-up. |

No log line, error message or template ever contains a bot token or a
pairing code.

---

## 5. Deprecated transition configuration

Three `.env` variables survive as a transition path and will be removed:

| Variable | Status |
|---|---|
| `TELEGRAM_BOT_TOKEN` | **Deprecated.** Spawns one *additional* dispatcher, bound to the `SHIRLEY_BOT_TENANT_SUBDOMAIN` tenant — and only while that tenant has stored no token of its own (its own row wins). |
| `SHIRLEY_BOT_TENANT_SUBDOMAIN` | **Deprecated.** Binds the variable above and nothing else. Ignored when it is empty. Logs one WARNING at start-up when it is actually used. |
| `TELEGRAM_ALLOWED_USER_IDS` | **Deprecated.** Admits a Telegram *account* on the environment-token dispatcher only, with **no** user identity — so user-scope settings do not apply to those turns. Logs one WARNING at start-up and one when it first admits a turn. |

`TELEGRAM_BOT_ENABLED` is **not** deprecated: it remains the master switch
for the whole bot thread.

### Migrating a single-bot deployment

1. Store the existing token under Admin → Providers & Credentials for the
   tenant that was using it, and restart. The `.env` token stands down
   automatically for that tenant (its own row wins) — the log says so.
2. Pair each user (§2 above), and confirm messages still flow.
3. Clear `TELEGRAM_ALLOWED_USER_IDS`, then `TELEGRAM_BOT_TOKEN` and
   `SHIRLEY_BOT_TENANT_SUBDOMAIN`, and restart once more. The start-up log
   should now show only `source=vault` dispatchers.
