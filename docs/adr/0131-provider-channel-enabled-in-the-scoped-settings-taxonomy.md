# ADR-0131: Provider Channel Opt-In Switch in the Scoped-Settings Taxonomy — `provider_channel.enabled`, Tenant-Scoped, Config-Only

- **Status:** Accepted (2026-09-11)
- **Date:** 2026-09-11
- **Deciders:** PortfoliFLOW project owner
- **Closes:** B-D-21 of the Stage B decision record
  (`docs/concepts/provider-channel-stage-b-decisions.md` — the switch as a B-1
  deliverable), and OP-B-5's UI consequence via B-D-25 Q6.
- **Supersedes / amends:** **annex amendment to ADR-0112 §3** (one new
  config-only provider), the third after ADR-0118 and ADR-0123. ADR-0112
  itself remains immutable and otherwise unchanged. Amends nothing in
  ADR-0129: its §2 (the instance fetches "**only** when the tenant has enabled
  the channel") and §6 (opt-in per tenant, an owner action, an explicit
  description of what leaves the instance) are the requirements this ADR
  satisfies.
- **Tags:** provider-channel, configuration, multi-tenancy, credentials, admin,
  privacy

---

## Context

Every Stage B behaviour on the instance keys off one question: *has this
tenant enabled the provider channel?* The directory fetch runs on enable, on a
manual refresh and on a background timer, and never while the channel is
disabled (B-D-17). The export gesture on the blotter row exists only for an
enabled tenant (B-D-12). B-D-21 therefore makes the switch a B-1 deliverable
that ships **before** its consumers. SB-3b (fetch client) and SB-6 (timer,
panel, export) then land against a declared setting instead of each inventing
one, and all of them get the same answer.

The taxonomy already has the shape. ADR-0118's `voice` is a config-only
provider whose single field is a switch, which established that a declaration
with an empty secret set is legal. (Preconditions verified 2026-09-11 against
head `b034`, post-SB-3a.)

## Decision

### 1. One declaration, one field

```python
ProviderDeclaration(
    provider="provider_channel",
    fields=(ProviderField(name="enabled", is_secret=False, scopes=_TENANT),),
    managed_by_matrix=False,
    env_fallback=False,
    optional=True,
)
```

B-D-21 writes the switch as `ProviderField(name="provider_channel", …)`. It is
read as the *provider* `provider_channel` carrying one tenant-scoped config
field `enabled`, so the row's identity is `(tenant, provider_channel, enabled)`.
`is_secret=False` because there is nothing to keep: the directory is a public
signed document, and an export is sealed to the provider's public key.

**Why a provider of its own.** No existing declaration owns the channel. A
field on `openrouter` or `telegram` would make that card misstate what it
gates, which is ADR-0118's argument against hosting `voice.enabled` on one
voice half. The channel will also need a credential of its own: the B-2 relay
credential hangs off this same declaration.

### 2. Policy: `env_fallback=False`, `optional=True`

This is the one place this ADR deliberately departs from ADR-0118 §2.

- **`env_fallback=False`.** `voice.enabled` falls back to `VOICE_ENABLED` so a
  single-tenant `.env` deployment keeps working. The provider channel must
  not: an environment variable is deployment-wide, so one line in `.env` would
  switch on phone-home behaviour for every tenant at once. ADR-0129 §6 makes
  enabling a **per-tenant owner action**, and the vault row written through the
  Admin card is the only place it happens. The flags are per declaration, so
  the B-2 relay credential inherits this unless its own ADR revisits it.
- **`optional=True`.** ADR-0118 chose `optional=False` so an enabled-but-keyless
  tenant fails loudly. Here the secret set is empty, so no credential can be
  missing and there is no loud case to keep. B-2 revisits the flag.

### 3. Resolution and truth rule

A consumer asks
`await resolver.resolve_config("provider_channel", "enabled", scopes=("tenant",))`.
The channel is **on** iff the value, stripped and lower-cased, is `"true"`.
This is the `VOICE_ENABLED` convention (`bot/telegram_bot.py`,
`_resolve_bot_voice_enabled`), so every switch in the taxonomy reads the same
way. Unset is off, and so is a row the owner has disabled on the card (the
resolver treats it as absent).

`_ENV_CONFIG_FIELDS` gets no entry. That absence, not the `env_fallback` flag,
is what closes the environment link for a config field, because the config
chain reads its environment source from that table alone. TX-06 pins it, and
`scopes=("tenant",)` makes the consumer independent of the table as well.

**No consumer in this ADR.** The reader lands with SB-6 in the web/timer layer.
`services/provider_channel/` never imports the resolver (C-2,
`tests/services/provider_channel/test_contract.py`).

### 4. Admin card: taxonomy-driven appearance, deliberate copy

The card appears because the declaration exists (ADR-0118 §7), in the
owner-only tenant panel. Its row uses the same config control as
`voice.enabled`. The pill says *dormant* ("nothing reads this switch yet") and
changes when SB-6 lands a reader: the pill is a truth, not a promise. The field
hint is the ADR-0129 §6 statement of what leaves the instance, verbatim:

> When on, this instance fetches the signed provider directory from
> portfoliflow.com — the list version only, never a query — and offers
> encrypted exports of individual tickets. Nothing about holdings, portfolios,
> users or analytics leaves the instance. When off, nothing is fetched and the
> Transactions area shows no provider gesture at all.

While the channel is off, this card is the **only** place in the product that
mentions it.

### 5. When off: absent, not disabled (B-D-25 Q6)

With the channel off, the Transactions area (ADR-0128) renders **no** provider
gesture: no greyed button, no tooltip. The only way to discover the channel is
this card's description. The rule is B-D-25 in the Stage B record; PB-D3
records it after this ADR lands, and it is cited by that id here. SB-6
implements it.

### 6. Scope note

The setting is tenant-scoped; the directory cache is instance-wide, a file
under `DATA_DIR` (B-D-13). Disabling stops the tenant's timer and surfaces, not
the file. Whether an instance can forbid every tenant from enabling it is
deferred to B-2 — possibly an application-scope kill switch in the ADR-0112 §5
pattern (the `TELEGRAM_BOT_ENABLED` master switch). A kill switch can only turn
the channel *off*, so it would not conflict with §2. Noted, not decided.

## Alternatives considered

- **An environment fallback, as `voice` has.** Rejected (§2): contradicts ADR-0129 §6.
- **A field on `openrouter` or `telegram`.** Rejected (§1): neither owns the channel.
- **A column on `tenants`.** Rejected: it would be the first switch outside the
  taxonomy, and it would need a migration and a surface of its own.
- **A greyed gesture when off.** Rejected by B-D-25 Q6 (§5).

## Non-goals

The fetch client, cache, timer, panel, filter and export (SB-3b, SB-6); the
relay credential (B-2); an application-scope kill switch (the §6 note); a
dedicated toggle control on the card.

## Consequences

- Every tenant's owner sees one new *dormant* card in Admin → Providers &
  Credentials; `PROVIDER_TAXONOMY` grows by one, to seven declarations.
- No migration, no schema change. `_ENV_CONFIG_FIELDS`,
  `_ENV_CREDENTIAL_FIELDS` and the C-2 contract are unchanged.
- TX-04 and TX-06 gain a pin each, and the web suite gains one card test.

## Related ADRs

- **ADR-0112**: the base (§3 amended by this annex).
- **ADR-0118**: the config-only switch precedent.
- **ADR-0123**: the second annex amendment.
- **ADR-0129**: §2 and §6 are the requirements satisfied here.
- **ADR-0128**: the Transactions Area, where the gesture lives.

## Revision History

| Date | Author | Change |
|---|---|---|
| 2026-09-11 | PortfoliFLOW project owner | Drafted and accepted with its implementation (SB-4). |
