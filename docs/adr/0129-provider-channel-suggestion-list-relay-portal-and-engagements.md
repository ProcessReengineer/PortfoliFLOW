# ADR-0129: Provider Channel — Suggestion List, Zero-Knowledge Relay, Provider Portal, and Engagements

- **Status:** Proposed
- **Date:** 2026-08-26
- **Deciders:** PortfoliFLOW project owner
- **Implements roadmap item:** #061 (part b — the order/hand-off flow); revives
  the provider-directory half of the Execution-Network concept that ADR-0107
  cut, under the conditions ADR-0107 named (post-AGPL-release, adoption-led)
- **Supersedes / amends:** none; honours the ADR-0107 red line and the
  ADR-0108 licensing apparatus (open client / proprietary service split)
- **Companion:** ADR-0128 (trade-ticket object model — defines the lifecycle
  states this channel arms: `sent`, `acknowledged`, `executed`)
- **Working document:** `docs/concepts/transactions-record-flow-plan.md` §6
- **Tags:** provider-channel, suggestion-list, relay, encryption, engagement,
  monetisation, agpl-boundary, regulatory

---

## Context

ADR-0128 builds the record layer: every portfolio change ends as a booked
trade ticket. Part (b) of the Transactions concept adds the step *before* the
booking: the user selects a provider from a centrally curated **suggestion
list**, sends an encrypted order or inquiry, and receives a structured
confirmation whose fill data pre-fills the booking. The same channel carries
**engagements** — advisory, legal, fund-selection or second-opinion requests
that have no book effect (ADR-0128 D-5 split).

Three constraints frame the design:

1. **The regulatory red line (ADR-0107):** PortfoliFLOW remains a software
   provider — a venue for the PM's own decisions, never a broker or advisor.
   A centrally curated list from which orders and contacts are generated, and
   which is the monetisation lever, sits close to regulated intermediation
   (Anlage-/Abschlussvermittlung; proximity to Anlageberatung if suggestions
   appear portfolio-personalised). The *architecture* below is designed so the
   platform cannot read what it relays; the *commercial structure*
   (listing fees vs. transaction-linked remuneration, curation depth,
   personalisation) is a legal question that this ADR explicitly does **not**
   answer.
2. **The AGPL boundary (ADR-0108):** the self-hosted instance is AGPL; the
   directory and relay are a separate, centrally operated network service.
   The client code, message formats, and verification keys live in the open
   repository; the service does not. The defensible moat is the provider
   network, not the code — a fork without the network relays nothing.
3. **The audience:** self-hosting, data-sovereignty-motivated institutions.
   Any phone-home behaviour must be radically opt-in, transparent, and
   data-minimal.

Operator decisions fixed in the concept chat: **D-1** — central relay plus a
provider **web portal** (no native app), payloads end-to-end encrypted per
provider public key, e-mail as **notification only**, modern encryption
primitives preferred over OpenPGP, the provider list itself **signed**;
**D-5** — engagements are a separate object on the same channel; user-own
provider lists follow in a second step.

## Decision

### 1. Staging (binding)

- **Stage A — contract only (with ADR-0128 v1):** the lifecycle states,
  message and confirmation schemas, and the directory format are specified
  and versioned in the repository. No service is built; the states stay
  unreachable.
- **Stage B — channel MVP:** directory service + relay + provider portal,
  invited providers, engagements and order hand-offs, structured
  confirmations pre-filling the ADR-0128 booking step.
- **Stage C — monetisation:** commercial terms on the directory.
  **Hard gate: external legal counsel on the intermediation question before
  any Stage-C design work.** Stage B carries no remuneration mechanics of any
  kind, keeping the software-venue posture clean while the structure is
  resolved.
- User-own (tenant-local) provider entries: a named successor inside Stage B
  ("step 2" per operator decision), using the same message path with
  tenant-managed keys and no directory involvement.

### 2. The suggestion list — a signed, versioned directory

- A static, versioned document served from portfoliflow.com: providers with
  id, display data, provider types (`broker`, `secondary_desk`, `advisory`,
  `legal`, …), supported ticket kinds / engagement categories, coverage
  hints (asset classes, jurisdictions), and **one public encryption key per
  provider**.
- The document is **signed** with a portfoliflow.com publishing key whose
  public half ships in the AGPL repository. The instance verifies the
  signature before trusting any provider key — key substitution via a
  compromised fetch is the actual attack surface of such systems, not the
  cipher. Key rotation for the publishing key is part of the format
  (successor-key announcement inside a still-valid document).
- The instance fetches the directory **only** when the tenant has enabled the
  channel (opt-in, off by default), caches it, and renders provenance ("list
  version, fetched when") in the UI. Suggestion filtering happens
  **client-side in the instance** against the coverage hints — the directory
  service never learns the portfolio, holdings, or the query.

### 3. The relay — zero-knowledge message passing

- The instance encrypts the message payload end-to-end to the selected
  provider's key and posts only the ciphertext plus routing envelope (sender
  tenant handle, provider id, message type, ticket/engagement correlation id,
  timestamps) to the relay. **The relay stores and sees ciphertext and
  routing metadata only** — it cannot read orders, and this property is a
  design invariant, not an implementation detail.
- **Encryption primitive (D-1):** libsodium-family asymmetric encryption
  (sealed boxes / age-style recipients), not OpenPGP. Rationale: fewer
  footguns, small dependency surface, browser-capable for the portal
  (WebCrypto/libsodium.js), and the "public key per provider" concept is
  preserved unchanged.
- Status vocabulary on the envelope mirrors the ADR-0128 states:
  `sent → acknowledged → executed`, plus `declined`. Status transitions are
  written by the portal and polled (v1) by the instance; the instance maps
  them onto the ticket/engagement lifecycle. Polling over webhooks in v1:
  self-hosted instances behind NAT must not be required to expose an inbound
  endpoint.
- **The confirmation is structured:** for order tickets, the provider's
  `executed` message carries a schema-versioned fill payload (units, price,
  fees/taxes, currency, trade and settlement dates, optional ISIN for
  new-instrument purchases). The instance decrypts it and **pre-fills the
  ADR-0128 booking step for user review — it never books autonomously.** The
  human confirms every booking; the channel informs, the PM decides (the
  ADR-0107 line, kept structurally).
- Fallback without a portal-willing provider: the instance can export the
  encrypted payload as a file/e-mail attachment. No structured return path
  exists then; the user books manually through the ADR-0128 flows, which is
  the designed degradation — part (b) is strictly a front-end to part (a).

### 4. The provider portal — web, not native

- One central web application: provider login, inbox, **client-side
  decryption** (the provider's private key never reaches the server —
  key material stays in the browser, with an explicit, documented
  key-backup responsibility on the provider), structured
  acknowledge/decline/execute forms producing the confirmation payloads.
- **E-mail is notification only** ("a new request is waiting"), never
  transport; no payload content, no portfolio data, no ticket parameters in
  any mail.
- Deliberately minimal v1: no threading, no free-form chat. A `message`
  free-text field inside the encrypted payload covers the residual need;
  a conversation surface is a named successor.

### 5. Engagements — the non-booking sibling

- New tenant-scoped object **`engagements`** (ADR-0128 D-5): category
  (advisory / legal / fund-selection / second-opinion / other), free-text
  brief, optional `investment_id` and `case_id` references, lifecycle
  `draft → proposed → approved → sent → acknowledged → declined|closed` —
  the ticket lifecycle minus the booking tail. No units, no amounts, no
  effects table.
- Engagements use the identical directory, encryption, relay and portal
  path. The optional case link carries the provenance chain: a Watch-Desk
  finding becomes a Case, the Case spawns an engagement ("have the LPA
  amendment reviewed"), the engagement's outcome is documented back on the
  Case.
- Shirley remains the in-house first opinion; an engagement is explicitly
  the *external* second opinion. No coupling between the two in v1.

### 6. Privacy and data-minimisation invariants (binding)

- Channel entirely **opt-in per tenant**, off by default; enabling it is an
  owner action with an explicit description of what leaves the instance.
- What leaves the instance: directory fetches (version pin, no query), and
  per message the ciphertext + routing envelope. **Never**: portfolio
  composition, holdings, AUM, or any analytics — an order payload contains
  the order's own parameters only.
- The relay's retention is bounded (delivered ciphertext is deletable after
  terminal status; concrete windows are a Stage-B operational decision).
- These invariants are stated in user-facing documentation at Stage B launch;
  for the self-hosting audience the verifiable client code **is** the trust
  argument.

## Alternatives considered

- **PGP e-mail as transport** — rejected: unparseable free-text
  confirmations kill the loop-closing value; key UX and distribution
  unsolved; the target providers do not live in PGP mail.
- **Direct instance→provider API** — rejected for v1: requires providers to
  operate software; a later premium path for large houses.
- **Native provider app** — rejected: onboarding friction; a broker desk
  installs nothing. The web portal is the app.
- **Webhooks to the instance** — rejected for v1: self-hosted instances must
  not be forced to expose inbound endpoints; polling is the NAT-proof
  default.
- **Relay with server-side decryption** ("simpler portal") — rejected: the
  zero-knowledge property is both the trust argument for the audience and
  structural support for the software-venue posture; giving it up buys
  convenience at the cost of the two things the channel most needs.

## Consequences

**Positive.** The channel closes the loop machine-readably: suggestion →
encrypted hand-off → structured confirmation → pre-filled booking → ledger,
with the human confirming at the decisive point. The engagement object turns
the same rails into a services marketplace substrate. The AGPL split
(open client, proprietary network) matches the monetisation thesis: the moat
is the network.

**Negative / accepted.** portfoliflow.com becomes operated infrastructure
with availability expectations and metadata stewardship duties. Provider
onboarding is a chicken-and-egg effort (the reason ADR-0107 deferred the
directory; Stage B starts invited). Client-side key handling shifts a real
key-backup burden onto providers. Stage C is blocked on legal counsel by
design — accepted, because an unstructured start there endangers the entire
product posture.

**Process (binding, as in ADR-0128).** Stage B is implemented in
operator-gated sub-strands with deliberate pause points — the portal UX, the
directory format, and the confirmation schema are each discussed or mocked up
before build. Development pace is traded for precision on the target picture;
the operator retains tight control of this workstream.

**Commissions (recorded, not designed):** tenant-local provider entries
(step 2); conversation surface on the portal; webhook/push delivery as an
opt-in alternative to polling; provider-side API (Stage-D candidate);
`Stage C` commercial-structure ADR **after** legal counsel; relay retention
policy (Stage-B operational decision).

## Compliance / verification

- Stage A ships schemas + signature-verification code + tests in the AGPL
  repository; a regression test verifies the instance refuses an unsigned or
  tampered directory document.
- A contract test pins the ADR-0128 state mapping (`executed` never books —
  it pre-fills; only a user action books).
- No repository component gains a hard runtime dependency on the central
  service: with the channel disabled, ADR-0128 is fully functional
  (asserted by the existing test suites running with the feature flag off).
- Roadmap: part (b) is tracked under #061 until Stage B is scheduled, at
  which point it is raised as its own item (operator action).
