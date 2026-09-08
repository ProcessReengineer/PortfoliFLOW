# Provider Channel — Stage B Decision Record

- **Status:** Decisions of record (operator-decided 2026-09-08); *Proposed*
  items are marked as such
- **Date:** 2026-09-08 · **Decider:** PortfoliFLOW project owner
- **Seeded by:** Stage B handover from the Transactions (#061) track
  (Mission Control, 2026-09-08)
- **Governs:** the Stage B concept chats B-1/B-2/B-3 and the Stage B build
- **Governed by:** ADR-0129 (frame; §2 inheritance is binding, versionable
  only), ADR-0128 (status seam), ADR-0107 (red line), ADR-0108 (AGPL
  split), `docs/concepts/provider-directory-format.md` (format v1)
- **Intended location:** `docs/concepts/provider-channel-stage-b-decisions.md`
  (AGPL repository). Operational internals (key custody specifics, server
  layout) live in the private infrastructure repository, not here.
- **Convention:** decisions are numbered **B-D-n** and are appended, never
  rewritten; a superseded decision gets a one-line addendum pointing to its
  successor. Downstream prompts cite this file, not chat memory.

---

## 0. What Stage B is (restated, no drift)

Stage B builds the **operated** half of the provider channel: a signed,
versioned provider directory published on portfoliflow.com; a
zero-knowledge relay (ciphertext + routing envelope only); a provider portal
(web, client-side decryption); and the instance-side arming
(`provider_channel.enabled`, blotter "Send", the three dormant ticket
statuses, the `engagements` object). It carries **no remuneration mechanics
of any kind** (Stage C is legal-gated). The channel is strictly a front-end
to the ADR-0128 record flow: **`executed` never books — it pre-fills; only a
user action books.**

---

## 1. Decisions of record

### B-D-1 · Scope: the full loop, built directory-first (handover Q-1)

Stage B delivers directory **and** relay **and** portal. Build order is
B-1 → B-2 → B-3. **B-1 is closed as a self-contained, runnable milestone:**
signed directory online, instance fetches and verifies it, client-side
suggestion filtering works, the encrypted-export degradation path works.
This milestone is a deliberate operator hold point at which the start of
B-2 is decided; it is *sequencing inside Stage B*, not a reduced release.

The directory is a **hand-maintained, locally signed document** (no
directory service with an admin surface). This is the ADR-0129 §2 design
unchanged; it is recorded here because the operator's Q-1 reasoning named
it explicitly.

### B-D-2 · First providers are test providers with full functionality (Q-3)

The first published directory lists **test providers only** — entries
operated by the project owner that exercise the *complete* provider
functionality (portal login, client-side decryption, acknowledge / decline
/ execute, fill payload). No real commercial offering stands behind them;
they are removed from the directory once real providers are onboarded.

**Marking is by name, not by flag.** No format change is made for test
entries. Conventions:

- `provider_id` carries the prefix `test-` (e.g. `test-broker-01`);
- `display_name` carries a visible prefix `[TEST] ` (e.g.
  `[TEST] Example Broker Desk`);
- the instance UI shows a short standing note while any `test-` entry is
  present in the fetched directory ("Test entries — no offering stands
  behind these providers") — *Proposed*, decided in B-1 with the fetch UI;
- removal is an ordinary directory update with a `directory_version` bump.

Consequence: the first publication stays on **format v1**. A v2 bump before
first publication is off the table unless B-1 finds another missing field.

Implications recorded (so they do not return as surprises):

1. The portal is first designed for a user who knows the architecture. The
   B-3 mockup is done **from the perspective of a desk employee who has
   never heard of sealed boxes**; a second browser profile or second device
   acts as a second provider.
2. The transition to real providers is a **named successor milestone,
   "Stage B.1 — first real providers"**, between Stage B and Stage C. It
   needs names, a conversation about their custodial reality, and an
   onboarding procedure — none of which exists today.

### B-D-3 · Engagements ship with Stage B, as the last strand (Q-5)

Relay and portal are built for **both** message types from day one (the
discriminator is `message_type` on the envelope; cost is small). The
instance-side `engagements` object — table (migration `b035`), lifecycle
`draft → proposed → approved → sent → acknowledged → declined|closed`, no
effects table, optional `investment_id`/`case_id`, Area placement — is the
**last build strand** of Stage B with its own pause point. If capacity runs
out after B-3, this strand moves to a named successor without touching what
was built; the rails are already in place.

### B-D-4 · Operational posture (Q-2) — see §2

Adopted in full as written in §2: best effort / no SLA; zero-knowledge
incident story; pseudonymous tenant handles and minimal logging;
mailbox-not-archive retention; on-demand polling plus light background poll.
§2 must be written down before B-2 designs the relay, because retention
windows and polling cadence are downstream of it.

### B-D-5 · Publishing-key ceremony (Q-4) — see §3

Adopted in full: offline generation, custody separate from the server and
from any git repository, **successor key generated at the same time** and
stored separately, an overlap rule for the client, and the tripwire flip in
the same commit that lands the real public key.

### B-D-6 · Legal counsel after the Stage B build (Q-6)

External legal counsel on the intermediation question is engaged **after**
the Stage B build, not in parallel. The Stage C gate (ADR-0107 / ADR-0129
§1) is unchanged: no Stage C design work before counsel. Consequence
accepted: how test providers may be presented publicly is answered by the
operator's own judgement for now (B-D-2 marking plus the UI note).

### B-D-7 · Repository topology

- **AGPL repository:** instance side only — fetch/verify/cache, encryption
  client, poller, blotter "Send", prefill path, `engagements`, the
  `provider_channel.enabled` annex, tests, this record.
- **New private repository `portfoliflow-network`:** relay service and
  provider portal, one deploy path.
- **`pinkernelle-infrastructure` (private):** the directory *source* file,
  the signing tool (which imports `sign_directory` from the AGPL package so
  signer and verifier match by construction), the deploy step that ships
  the signed document to the web root, and the operational notes excluded
  from this record.

The production private key never lives in any repository (ADR-0129 Stage A
`sign_directory` docstring).

### B-D-8 · This record's location and split

This file lives in the AGPL repository under `docs/concepts/`. It holds
architecture decisions and the operational posture in the form that later
becomes user-facing documentation (ADR-0129 §6 "stated at Stage B launch").
Key-custody specifics, server layout, and credentials handling live in a
separate `stage-b-operations.md` in `pinkernelle-infrastructure`.

### B-D-9 · ADR and roadmap numbering

- **No new framing ADR.** ADR-0129 remains the frame; Stage B uses small
  annex ADRs (ADR-0112 pattern). Next free ADR number **0131** is expected
  to go to the `provider_channel.enabled` annex when the instance side is
  built; further annexes only if a concept chat finds ADR-worthy material
  (e.g. retention). Numbers are verified against `docs/adr/README.md` at
  writing time, never reserved.
- **Stage B is its own roadmap item** (title: "Provider channel Stage B —
  directory, relay, portal"), raised by the operator when scheduling;
  ADR-0129 references it. #061 closes as `shipped (2026-09-08)`.
- **Tenant-local provider entries ("step 2")** are recorded on the roadmap
  as a **separate successor item**, explicitly outside Stage B (see B-D-11).

### B-D-10 · Concept-chat sequence and Mission Control timing

- Concept work runs in the order **B-1 → B-2 → B-3** (§5). B-2 contains a
  **portal sketch mockup** so the relay API is not designed blind; B-3
  refines it into the build mockup.
- **Mission Control for Stage B opens immediately after this record, with
  B-1 as its first pause point.** Rationale: B-1's concept share is small
  (cadence, overlap rule, unknown-version behaviour, dependency check)
  while its build share is substantial (ceremony, signing tool,
  publication, instance fetch/verify/cache, filter, export path, tripwire
  commit) — that is already Mission Control work. **B-2 and B-3 are
  concept pause points inside Mission Control**, each closed by an
  operator decision before the corresponding build strands are cut — the
  same pattern as the Transactions track's mockup sessions inside the
  board. No separate concept chats are opened.
- Mission Control runs its own board, inherits the Transactions track's
  eight process rules (§6) and this record as its decisions-of-record
  document; kickoffs and closing reports are gated on fresh Repomix images.
  Verify-first extends to "verify against the deployed directory/relay",
  not only the snapshot.
- *Addendum 2026-09-08:* supersedes the earlier "MC opens at the build
  kickoff, after the concept chats" wording of this decision.

### B-D-11 · Out of scope for Stage B (named successors)

- Tenant-local provider entries ("step 2" in ADR-0129 §1) — separate
  roadmap item; needs tenant key management and changes the trust model
  (no directory involved).
- "Stage B.1 — first real providers" (B-D-2).
- Conversation surface on the portal; webhook/push delivery; provider-side
  API (ADR-0129 commissions, unchanged).
- Stage C commercial structure — after counsel (B-D-6).

---

## 2. Operational posture (B-D-4)

**Availability.** portfoliflow.com's directory, relay and portal are
operated **best effort, without an SLA**. This is honest for a solo
operation and acceptable because of a structural property that Stage B
must keep true: **an outage of the relay never blocks a booking.** The
encrypted-export path and manual booking through the ADR-0128 flows remain
available at all times; every instance-side call to the relay degrades on
timeout instead of blocking a user gesture. This sentence appears in the
user-facing documentation at launch.

**Incident story.** The zero-knowledge design keeps it short:
*server compromised* → the attacker sees ciphertext and routing metadata,
never order contents; *publishing key compromised* → successor-key rotation
through the format, notice in the repository; *provider key compromised* →
the provider generates a new key pair, a directory update publishes it.
Each case is one paragraph in the launch documentation.

**Metadata stewardship.** The operator can observe *who sent what type of
message to whom, when* — not content. Two cheap-if-early measures are
binding: (1) **tenant handles are pseudonymous** (a random handle issued
when the channel is enabled; no organisation names on the relay);
(2) **logging is minimal** — no access logs beyond what operating the
service requires, no retention of client IPs beyond the operational
window.

**Retention (the relay is a mailbox, not an archive).** Ciphertext is
deleted once the instance has collected a terminal status
(`executed`/`declined`) plus a **7-day** safety window. Non-terminal
messages **expire after 30 days**; an expired message surfaces to the
instance as a distinct status so the ticket can be re-sent or handled
manually. Concrete windows are constants in the relay, revisited in B-2 if
the design shows a reason.

**Polling.** The instance polls **on demand when the blotter is opened**,
plus a **light background poll (≈ every 15 minutes) only while non-terminal
messages exist**. NAT-safe, low load, and sufficient — a broker fill is not
needed to the second.

---

## 3. Publishing-key ceremony (B-D-5) — checklist

The publishing key is the root of the trust model (ADR-0129 §2: key
substitution via a compromised fetch is the actual attack surface). The
ceremony is cheap before first publication and expensive after it.

1. **Generate offline.** On a machine without network access or in a fresh
   VM, with the signing tool (`pinkernelle-infrastructure`) that uses
   `sign_directory`. Ed25519, raw 32-byte keys. Generate **two** key pairs:
   the publishing key and its successor.
2. **Custody.** Both private keys are stored encrypted (age or GPG) on two
   physically separate media, **separately from each other**; never on the
   Hetzner server, never in any git repository. The specifics (media,
   passphrase handling, optional hardware token) are recorded only in
   `stage-b-operations.md`.
3. **Publish the public key.** The publishing key's public half lands in
   `services/provider_channel/publishing_key.py` as the documented one-line
   change, with a real `PUBLISHING_KEY_ID`. **In the same commit**, OP-30
   is honoured: `test_placeholder_publishing_key_fails_closed` flips its
   assertions, and a test verifying a real-key-signed document is added.
4. **Announce the successor.** The first published directory may already
   carry the successor in `successor_key` with a `valid_from` in the
   future, so rotation is a routine update rather than an emergency.
5. **Client overlap rule (B-1 decides the details).** When a *verified*
   document announces a successor, the instance records it and accepts
   documents signed by it from `valid_from`; the current key remains valid
   until the announcing document's `valid_until`. Both keys verify inside
   the overlap window.
6. **Sign locally, ship the signature beside the document** (format v1:
   the signature never lives inside the signed JSON; canonical bytes are
   enforced, not documented).

---

## 4. Directory publication (recorded here; details in B-1)

- Static files served by Caddy from the existing web root via the existing
  rsync deploy path, e.g. `/directory/v1/directory.json` and
  `/directory/v1/directory.sig`, plus versioned copies
  (`directory-<n>.json`) for traceability. `Content-Type: application/json`,
  short cache TTL, ETag.
- The instance fetches **only when the channel is enabled**, caches the
  verified document, refuses a lower `directory_version` than the cached
  one (downgrade refusal), and shows provenance ("list version n, fetched
  at …") in the UI.
- v1 readers refuse unknown format versions and unknown members by design.
  The fetch path treats "unknown version" as *keep the cached document,
  surface a "client update required" notice* — never as a crash and never
  as silent acceptance.

---

## 5. Concept chats — sequence and per-chat parking lists

**B-1 · Directory & publishing-key lifecycle** (first; closes as the
B-D-1 milestone)
- Execute §3 (ceremony) and §4 (publication); first signed directory with
  test providers online.
- Fetch cadence and caching; downgrade refusal; successor overlap rule
  details; the "unknown version" client behaviour.
- Confirm `ENGAGEMENT_CATEGORIES` v1 (advisory / legal / fund_selection /
  second_opinion / other) is final for first publication — extension is a
  format bump.
- Instance-side fetch/verify/cache wiring, suggestion filter against
  coverage hints, encrypted-export path; the standing test-entry UI note.
- **Verify first:** whether `httpx` is already a runtime dependency (else
  the fetch client is a second new dependency beside pynacl and is
  operator-gated like it).

**B-2 · Relay API, retention & polling**
- pynacl decision (operator-gated; Stage A's "not until used" line ends
  here) and the sealed-box implementation on the instance.
- Relay API contract (instance: post message, poll statuses; portal: inbox,
  fetch ciphertext, write status + reply ciphertext), envelope handling,
  the `expired` status.
- Relay datastore (SQLite vs. Postgres in Podman), retention job, tenant
  handle issuance (self-service on enable vs. invitation), instance→relay
  auth, minimal logging (§2).
- **Portal sketch mockup** so the API is designed against a concrete
  consumer.

**B-3 · Provider identity, onboarding, portal auth & UX**
- Invitation procedure, login, browser-side key generation and the
  documented key-backup burden, the three forms (acknowledge / decline /
  execute → `FillPayload`; engagements: acknowledge / decline / closed),
  e-mail notification (which sending service — an operations question),
  the build mockup **from the desk-employee perspective** (B-D-2).

**Instance side (rides with the build, not a concept chat)**
- Annex ADR for `provider_channel.enabled` (tenant scope, owner action,
  config-only; ADR-0112 pattern; expected 0131).
- Arming `sent/acknowledged/executed` in the ticket service — and the
  **conscious renegotiation of C-2** (import isolation proves "absent";
  after wiring it must prove "off by flag") and preservation of C-4.
- Blotter "Send" as the fourth gesture (F-8) with the A-13 "two buttons,
  not a menu" revision.
- Poller placement and timeout degradation (§2).
- Prefill path: `executed` → `fill_to_prefill` → booking step for review.
- Migration `b035` and the `engagements` surface — last strand (B-D-3).

---

## 6. Process (inherited, binding)

Operator-gated sub-strands with deliberate pause points; mockups before
build (ADR-0128/0129 Consequences). The Transactions track's eight rules
apply to every Stage B build prompt: one concern per prompt · verify-first
with hard STOP-AND-REPORT · never `git add`/commit/branch/push · no
follow-on prompt before the predecessor's commit is confirmed · suites
serial against the dev DB · identifiers verbatim from the snapshot · test
helpers by grep-confirmed path · when a test directory must be DB-free,
grep every ancestor `conftest.py` for `autouse`. Operator-scope docs edits
are typed by a docs *prompt*, never left to manual editing (track lesson
OP-08/14/16). Dependency additions are operator-gated decisions recorded
here as B-D entries.

---

## 7. Coordinates (verified 2026-09-08, post-S7 Repomix, head `b034`)

Alembic head `b034` (next instance-side migration takes `b035`). Next free
ADR **0131** (verify at writing time). Version constants all `= 1`
(envelope, fill, directory format); first publication stays on v1
(B-D-2). Roadmap #061 `shipped (2026-09-08)` pending operator close-out;
Stage B raised as its own item, "step 2" as a separate successor item.

## 8. Operator actions to open Stage B

1. Commit this record to `docs/concepts/provider-channel-stage-b-decisions.md`
   (via a docs prompt, per §6).
2. Roadmap: close #061; raise the Stage B item and the "tenant-local
   provider entries" successor item (B-D-9/11).
3. Create the private `portfoliflow-network` repository; add the
   `directory/` source folder and signing tool to
   `pinkernelle-infrastructure` (B-D-7).
4. Open **Mission Control for Stage B** (B-D-10) with: this record, the
   Stage B handover, ADR-0129, ADR-0128,
   `docs/concepts/provider-directory-format.md`, and a fresh Repomix
   image. Its first pause point is the B-1 concept share; its first
   action after that is the key ceremony (§3).
