# Provider Channel — Stage B Decision Record

- **Status:** Decisions of record (operator-decided 2026-09-08; B-D-23/B-D-24 and addenda 2026-09-10); *Proposed*
  items are marked as such
- **Date:** 2026-09-08 · **Decider:** PortfoliFLOW project owner
- **Seeded by:** Stage B handover from the Transactions (#061) track
  (Mission Control, 2026-09-08)
- **Governs:** the Stage B concept chats B-1/B-2/B-3 and the Stage B build and Mission Control Stage B (chats 1–3)
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

- *Addendum 2026-09-09:* `pinkernelle-infrastructure` is a **local-only git
  repository with no remote**, by decision; its backup is a `git bundle` to
  a second medium after every signing run and every site change. Mission
  Control artefacts (boards, issued prompts) are kept by the operator
  outside the AGPL repository. The signing tool's Python convention: the
  environment variable `PORTFOLIFLOW_SRC` points at this AGPL checkout and
  the tool runs with that checkout's `.venv/bin/python`; there is no second
  `pyproject.toml`.

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
- *Addendum 2026-09-10:* supersedes "B-2 and B-3 are concept pause points
  inside Mission Control … No separate concept chats are opened" above.
  For the remainder of B-1 the Mission Control chat writes the build and
  docs prompts itself (MB-1 stays an in-chat pause point). **From B-2 on,
  the Mission Control chat only sets scope and keeps board and record; each
  strand (B-2, B-3) gets its own daughter chat** that does the concept
  work, produces the Claude Code prompts, and returns a closing report to
  Mission Control. Rationale: B-1's remaining strands are fully decided
  and mechanical; B-2 and B-3 each carry several concept rounds.

### B-D-11 · Out of scope for Stage B (named successors)

- Tenant-local provider entries ("step 2" in ADR-0129 §1) — separate
  roadmap item; needs tenant key management and changes the trust model
  (no directory involved).
- "Stage B.1 — first real providers" (B-D-2).
- Conversation surface on the portal; webhook/push delivery; provider-side
  API (ADR-0129 commissions, unchanged).
- Stage C commercial structure — after counsel (B-D-6).

### B-D-12 · Sealed box and encrypted export ship in B-1 (pause point 1, C-9)

The B-1 milestone includes the `x25519-sealed-box` implementation on the
instance and an **export gesture** on the blotter row: the user chooses a
provider from the verified directory and downloads the ticket as a sealed-box
ciphertext for that provider's `encryption_public_key`, to be sent outside
the system; the return path stays manual (ADR-0128 booking flows).
**Dependency decision (operator-gated, taken here): `pynacl` becomes a
runtime dependency in B-1.** A libsodium-compatible sealed box (X25519 +
XSalsa20-Poly1305) cannot be built on `cryptography` alone (no XSalsa20), and
the portal will decrypt with libsodium.js — compatibility by construction is
the point. The implementation lives in `services/provider_channel/` (pure,
DB-free); the C-2 import-isolation contract gains `nacl` as the only new
permitted import. The MB-1 mockup must place the export gesture so that the
B-2 "Send" gesture fits beside it without re-opening A-13 ("two buttons, not
a menu").

*Addendum to B-D-1 (2026-09-08):* the milestone sentence "the
encrypted-export degradation path works" is understood as defined here;
Stage A had neither encryption nor an export — what worked before Stage B
was manual booking only.

### B-D-13 · Directory cache is a file under `DATA_DIR`; no table; no learned successor (C-1)

The verified directory is cached as files under `DATA_DIR`
(`core/config.py` `data_dir`, default `data`): canonical bytes, detached
signature, and a small metadata file (`directory_version`,
`publishing_key_id`, `fetched_at`, ETag). The cache is **instance-wide**
(one publisher, one document; not tenant data). No database table is
created for it, so **`engagements` keeps migration `b035`** (B-D-3
unchanged). A `successor_key` announcement is **never persisted** and never
used for verification (see B-D-14); publishing-key rotation happens
exclusively through a code release. Consequence accepted: replacing the host
without carrying `DATA_DIR` loses the downgrade baseline until the next
successful fetch.

### B-D-14 · Key ring from code only; successor announcements are a notice (C-3)

`services/provider_channel/publishing_key.py` holds the key ring
`{publishing_key_id → public key}`: the current key and, once minted and
released, its successor. The instance reads `publishing_key_id` from the
still-unverified document, looks it up in the ring, and passes **that one
key** to `verify_directory`; an unknown id refuses the document (cache
kept). `verify_directory` itself is not changed. When a *verified* document
announces a `successor_key` whose id is not in the ring, the provenance line
shows a notice ("successor key announced from <valid_from>; client update
required before <valid_until>"). Ceremony rule that follows: mint the
successor → release it in the ring → announce it in the directory → switch
signing no earlier than one validity window later (B-D-16).

- *Addendum 2026-09-10:* delivered by SB-1 — `PUBLISHING_KEY_RING` in
  `services/provider_channel/publishing_key.py` holds `portfoliflow-2026-09`
  (current) and `portfoliflow-2027-01` (successor, `valid_from`
  2027-01-01). Because the successor is already in the ring, rotation day
  needs no client release. §3 item 5 (learned successor) is superseded by
  this decision.

### B-D-15 · Version acceptance is strictly monotonic (C-4)

Relative to the cached `directory_version`: **higher** → verify and replace
the cache; **equal and byte-identical** → "unchanged", update `fetched_at`
only; **equal with different bytes** → refuse, keep cache, distinct notice
("version n was re-published without a version bump"); **lower** → refuse,
keep cache, notice ("server serves version n, cached is n+k"). Every
publication, including a typo fix, bumps `directory_version` (format doc
§9).

### B-D-16 · Directory validity: 90 days, re-signed at least every 60 (C-7)

`valid_until` = `issued_at` + 90 days; re-sign and re-publish at least
every 60 days (calendar rule in `stage-b-operations.md`). The
publishing-key rotation overlap is at least one validity window. The window
is a document value, not a format parameter; it may be tightened for Stage
B.1 without code changes.

### B-D-17 · Fetch cadence (C-2)

The instance fetches on enable, on a manual "refresh" gesture, and in the
background at most **once per 24 hours**, conditionally (`If-None-Match`
against the ETag); never while the channel is disabled. Every fetch degrades
on timeout or refusal to the cached document. The background timer built in
B-1 is responsible for the directory only; the B-2 relay poll reuses the
mechanism.

### B-D-18 · No forward-compatible reader (C-5)

The v1 reader stays strict. A future format bump ships in a release that
reads the new version, and the operator publishes v1 and v2 documents in
parallel (`/directory/v1/`, `/directory/v2/`) for at least one validity
window (operational rule in `stage-b-operations.md`). Client behaviour on an
unknown version is as in §4: keep the cache, show "client update required".

### B-D-19 · Publishing-key id scheme (C-6)

`portfoliflow-YYYY-MM`: the current key carries its minting month, the
successor its planned `valid_from` month (format doc §10 convention).
`valid_from` remains authoritative; the id is a label.

### B-D-20 · Test-provider keys and entries (C-8)

The ceremony mints one X25519 key pair per test provider **offline**
(`cryptography` suffices for key generation); private halves are held like
the publishing key and never enter any repository. Repository tests use
throwaway keys they generate themselves; the proof "export against the
deployed directory decrypts" is an **operator walk** with the private
test-provider key, not a test in the repository. B-3 replaces these keys
with browser-generated ones by an ordinary `directory_version` bump. First
publication carries **two** entries: `test-broker-01`
(`[TEST] Example Broker Desk`, `broker`, `ticket_kinds: ["order"]`, no
engagement category) and `test-secondary-01`
(`[TEST] Example Secondary Desk`, `secondary_desk`,
`ticket_kinds: ["secondary", "commitment"]`,
`engagement_categories: ["second_opinion"]`). The `asset_classes` strings
the filter matches against are a verify-first item for the filter strand
(source: the instance's own constants, by grep).

### B-D-21 · `provider_channel.enabled` is a B-1 deliverable (C-10)

Because the instance fetches only when enabled (§4) and B-1 already has a
background fetch (B-D-17) and an export gesture (B-D-12), the setting ships
**inside B-1**, before filter and export: annex ADR **0131** (verified at
writing time) in the ADR-0118/0123 pattern —
`ProviderField(name="provider_channel", is_secret=False, scopes=_TENANT)`,
config-only, owner action, off by default. The C-2 import-isolation test
stays green through B-1 (fetch, filter and export import nothing from the
ticket world); its conscious renegotiation remains a B-2 item. Note recorded:
the setting is tenant-scoped while the cache (B-D-13) is instance-wide —
disabling stops the tenant's timer and surfaces, not the file.

### B-D-22 · `ENGAGEMENT_CATEGORIES` v1 is final for first publication

advisory / legal / fund_selection / second_opinion / other — unchanged
until Stage B.1 at the earliest; extension is a format bump (§9).

### B-D-23 · `.sig` file encoding (adopted 2026-09-09, ratified here)

The detached signature is published as a separate file beside the
document: the 64-byte Ed25519 signature as **128 lowercase hex characters
followed by exactly one LF** (129 bytes). Served with
`Content-Type: text/plain`; the document with `application/json`; both
under `Cache-Control: no-cache` with an ETag. Readers decode strictly —
no `.strip()`, no case folding: a signature that only verifies after the
reader tidies it is not the signature the operator published. Web path:
`https://portfoliflow.com/directory/v1/{directory.json, directory.sig,
directory-<n>.json, directory-<n>.sig}`. Reference implementation of the
reader: `_load_fixture` in `tests/services/provider_channel/test_directory.py`;
of the writer: `sign` in the infra signing tool.

### B-D-24 · First publication ships with the next PortfoliFLOW release (2026-09-10)

`directory_version 1` was signed on 2026-09-10 (`issued_at 2026-09-10`,
`valid_until 2026-12-09`, SHA-256 `c1383c4f…ec83`, test providers only per
B-D-2/B-D-20) and is committed in the infra repository, but it is **not
published until the next PortfoliFLOW release**. Publication gates
**SB-3b only** (the fetch client's report verifies against the deployed
directory); PB-D2, SB-3a and the infra hardening strands do not wait for
it. Calendar consequences (B-D-16): re-sign `directory_version 2` by
**2026-11-09**; rotate to the successor on **2027-01-01**.

### Pause point 1 closed (2026-09-08)

B-D-12…B-D-22 close the B-1 concept share (record §5, "B-1"). Naming used by
Mission Control from here on: build strands **SB-n**, implementation
prompts **PB-na**, docs prompts **PB-Dn**, mockups **MB-n**. The `httpx`
verify-first item in §5 B-1 is closed: `httpx>=0.27` is already a runtime
dependency; `pytest-httpx` is available in the dev extras.

### Tool-level decisions D-SB2-1…16 (infra repository, by reference)

Decisions about the signing/verify tool and the deploy path are recorded
in the private infra repository's reports and are binding there:
`docs/reports/PB-2a-report.md` (D-SB2-1…12) and
`docs/reports/PB-2b-report.md` (D-SB2-13). Those that touch this
repository's contract:

- **D-SB2-13** — a header regression on a document whose signature
  verified is a *deployment* finding, not a *trust* finding: warning line,
  exit 0; `--strict` turns warnings into exit 1 for cron. Document-level
  findings stay exit 1.
- **D-SB2-14** *(proposed → PB-2c)* — the tool's `verify` resolves the key
  by the document's `publishing_key_id` from `PUBLISHING_KEY_RING`
  (SB-1) and cross-checks the hex against its own `PUBLISHING_KEYS`
  mirror; `--public-key` becomes an optional override.
- **D-SB2-15** *(proposed → PB-2c)* — `check_source` refuses a source whose
  `issued_at` lies in the future relative to the judging day (PB-2b OQ-1).
- **D-SB2-16** *(proposed → PB-2c)* — under `--strict`, the re-sign
  reminder (fewer than 30 days of validity left) also fails the run; the
  reminder window opens on day 60, which is exactly the B-D-16 re-sign
  deadline (PB-2b OQ-3).

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

*Status 2026-09-10:* items 1, 3, 4 and 6 executed (ceremony 2026-09-09;
public halves in `publishing_key.py`, SB-1; successor announced in
`directory_version 1`). Item 2's "encrypted, two separate media" step is
**still open** — the private halves exist and are held per
`stage-b-operations.md`, but not yet in the form item 2 requires; tracked
there as an operator action. Item 5 is superseded by B-D-14 (no learned
successor; the ring is code).

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

Stage B additions (2026-09-10): a test that judges a *dated* document
derives its dates from the document, never from the prompt or a literal —
two PB-2a tests with hard-coded dates rotted the day the source was
re-dated (PB-2b §1). A report's `N passed` is dated; re-check it against
the tree before planning on it. Secrets move file-to-file, never through
a screen, a chat or a prompt. From first publication on, SB-3b and every
later report verifies against the deployed directory, not only the
snapshot.

---

## 7. Coordinates (verified 2026-09-10, post-SB-1 Repomix, head `b034`)

Alembic head `b034` (next instance-side migration takes `b035` — reserved
for `engagements`, B-D-3/B-D-13; the directory cache is a file, B-D-13).
Next free ADR **0131** (verify at writing time; expected for the
`provider_channel.enabled` annex, B-D-21). Next free roadmap number
**#069**. Version constants all `= 1` (envelope, fill, directory format);
first publication stays on v1 (B-D-2). Runtime dependencies: `httpx>=0.27`
and `cryptography>=42` present; `pytest-httpx` in the dev extras; `pynacl`
decided for B-1 (B-D-12) but **absent until SB-5**. Key ring (SB-1):
`PUBLISHING_KEY`, `PUBLISHING_KEY_ID`, `SUCCESSOR_KEY`, `SUCCESSOR_KEY_ID`,
`PUBLISHING_KEY_RING`, `PUBLISHING_KEY_PLACEHOLDER`, `is_placeholder` in
`services/provider_channel/publishing_key.py`; `verify_directory(document_bytes,
signature, *, publishing_key: bytes, now: date)` unchanged. Fixtures:
`tests/services/provider_channel/fixtures/directory-1.json` (1,180 B,
SHA-256 `c1383c4f…ec83`) and `directory-1.sig` (129 B). Provider-channel
suite 85 tests; `test_contract.py` 22, unchanged by Stage B. Reports live
in `docs/reports/`. Roadmap #061 `shipped (2026-09-08)`; Stage B is #067
`in-progress`, tenant-local provider entries #068 `open`.

## 8. Operator actions to open Stage B

1. ~~Commit this record to `docs/concepts/provider-channel-stage-b-decisions.md`
   (via a docs prompt, per §6).~~ — done 2026-09-08 (record placed by the
   operator; addenda B-D-12…B-D-22 via PB-D1).
2. ~~Roadmap: close #061; raise the Stage B item and the "tenant-local
   provider entries" successor item (B-D-9/11).~~ — done 2026-09-08 (#061
   shipped; #067, #068 via PB-D1).
3. Create the private `portfoliflow-network` repository; add the
   `directory/` source folder and signing tool to
   `pinkernelle-infrastructure` (B-D-7). — `pinkernelle-infrastructure` part
   done 2026-09-09 (PB-2a). `portfoliflow-network` is
   **not needed before B-2**; create it when B-2's daughter chat opens
   (B-D-10 addendum).
4. ~~Open **Mission Control for Stage B** (B-D-10) with: this record, the
   Stage B handover, ADR-0129, ADR-0128,
   `docs/concepts/provider-directory-format.md`, and a fresh Repomix
   image. Its first pause point is the B-1 concept share; its first
   action after that is the key ceremony (§3).~~ — done 2026-09-08; pause
   point 1 closed the same day; ceremony 2026-09-09; SB-1 committed
   2026-09-10.
5. **Publish `directory_version 1` with the next release** (B-D-24): deploy
   the signed pair from the infra repository, then verify against
   `https://portfoliflow.com/directory/v1/` with the tool in `--strict`
   mode; only then cut SB-3b.
