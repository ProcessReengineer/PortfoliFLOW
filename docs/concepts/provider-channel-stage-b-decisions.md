# Provider Channel — Stage B Decision Record

- **Status:** Decisions of record (operator-decided 2026-09-08; B-D-23/B-D-24 and addenda 2026-09-10; B-D-25/B-D-26 and addenda 2026-09-11); *Proposed*
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
- *Addendum 2026-09-14:* extends the 2026-09-10 addendum to the rest of
  B-1. Every remaining strand — the B-1 build strands SB-5, SB-3b and
  SB-6 included — runs as its own daughter chat, opened by a
  self-contained kickoff (starting state verified by Mission Control,
  binding decisions quoted, concept questions with proposed answers, the
  slice, a fixed rules block, a closing-report template). Mission Control
  keeps board, register, record and sequence, issues one kickoff at a
  time, ratifies each kickoff before the operator opens the chat, and
  verifies against a full image only at strand close; a daughter chat
  takes no decision of record. "For the remainder of B-1 the Mission
  Control chat writes the build and docs prompts itself" above is
  superseded for build prompts; docs prompts stay with Mission Control.

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

- *Addendum 2026-09-16 (delivered by SB-5, PB-1e, commit `395b157`):*
  `pynacl>=1.6.2` is a runtime dependency (1.6.2 is the first release
  carrying the libsodium build that fixes CVE-2025-69277).
  `services/provider_channel/sealed_box.py` holds the primitive only:
  `seal(plaintext, recipient_public_key)`, `open_sealed(ciphertext,
  private_key)`, `public_key_from_private`, `SEALED_BOX_KEY_BYTES = 32`,
  `SEALED_BOX_OVERHEAD = 48`, `SealedBoxError` ← `InvalidRecipientKey` /
  `SealedBoxOpenFailed`; `open_sealed` exists for tests and the portal
  parity check — nothing on the instance decrypts in production.
  `schemas.py` gains the export contract: `EXPORT_SCHEMA_VERSION = 1`
  (one constant for payload and envelope); `ExportPayload` (the ticket's
  stated fields, no computed total per D-amounts; `direction` ∈
  `EXPORT_DIRECTIONS = {buy, sell}`, pinned to
  `services.transactions.constants` by a C-1 test; free-text field
  `message`, filled deliberately by the export gesture and never from the
  ticket's `note` column); `ExportEnvelope` (`schema_version`,
  `provider_id`, `encryption_key_type`, `recipient_public_key`,
  `sender_tenant_handle: str | None` — no handle exists before B-2 —,
  `correlation_id`, `message_type`, `exported_at` as an argument per
  D-clock, `ciphertext` base64; no plaintext member); and the
  `parse_export` / `export_to_dict` / `parse_export_envelope` /
  `export_envelope_to_dict` pairs. `export.py` holds
  `seal_export(payload, *, provider: ProviderEntry, sender_tenant_handle,
  exported_at) -> ExportEnvelope` and `export_plaintext_bytes` (=
  `directory.canonical_bytes(export_to_dict(payload))` — one
  canonicalisation for signing and sealing) and refuses any key type but
  `x25519-sealed-box` (`UnsupportedEncryptionKeyType`). The C-2
  forbidden lists are unchanged; a positive allow-list test asserts that
  importing the package adds exactly `nacl`, `_sodium`, `_cffi_backend`
  and `cryptography` (plus `_openssl`) to `sys.modules`. Known-answer
  fixture `tests/services/provider_channel/fixtures/throwaway-recipient.json`
  (throwaway pair generated by the run, B-D-20). Cross-implementation
  parity with libsodium.js is a B-3 item: the 48-byte overhead and the
  canonical plaintext are the two facts the portal must reproduce.
  **Left to B-2:** whether the relay message is the routing `Envelope`
  plus a `ciphertext` member (recommended) or wraps `ExportEnvelope`
  whole; and whether requiring `sender_tenant_handle` on the file
  artefact bumps `EXPORT_SCHEMA_VERSION` to 2 (only if the file, not just
  the relay message, requires it). `Envelope`, `FillPayload` and their
  version constants are unchanged.

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

- *Addendum 2026-09-16 (delivered by SB-3b, PB-1f, commit `d0393bb`):*
  the cache lives in `services/provider_directory/cache.py`, **outside**
  `services/provider_channel/` — the C-2 contract forbids `httpx` and every
  `core.*` import in the pure package, so the fetch client is its own
  package that imports the pure one. Layout under
  `<data_dir>/provider_directory/`: `directory.json` (the fetched bytes,
  byte-exact), `directory.sig`, `meta.json` (`directory_version`,
  `publishing_key_id`, `fetched_at`, `etag`, `source_url`;
  `META_SCHEMA_VERSION = 1` names the cache-file layout only, not a wire
  version). Written atomically (temp file + `os.replace`) and only after
  verification. **Read re-verifies:** `read_cache(root, now=…)` checks the
  bytes against the shipped ring on every read — the disk is data, not
  trust. Two rules settled on delivery: a cached document that fails
  signature or shape verification on read is treated as **no cache**; a
  cached document that verifies but is **expired** remains the downgrade
  baseline of B-D-15 (`read_cache` returns it with `verification=None`,
  `Provenance.valid=False`) — a signed, dated document is a better anchor
  against downgrade than nothing.

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
- *Addendum 2026-09-11:* the selection step landed in SB-3a as
  `services/provider_channel/ring.py` — `verify_directory_with_ring(document_bytes,
  signature, *, now, ring=PUBLISHING_KEY_RING, current_key_id=PUBLISHING_KEY_ID)
  -> RingVerification`. The document names its key by `publishing_key_id`;
  an id that is not in the ring raises `UnknownPublishingKeyId` **before any
  signature arithmetic**; bytes whose id cannot be read are handed to
  `verify_directory` with the current key so the reference implementation
  refuses them in its own vocabulary. `RingVerification` carries `key_id`,
  `successor_in_use` (the verifying id is not the shipped current id) and
  `announced_successor` ∈ {`in_ring`, `not_in_ring`, `contradicts_ring`, none} —
  reported, never refused. Pure; C-2 unchanged (`docs/reports/PB-1b-report.md`).

### B-D-15 · Version acceptance is strictly monotonic (C-4)

Relative to the cached `directory_version`: **higher** → verify and replace
the cache; **equal and byte-identical** → "unchanged", update `fetched_at`
only; **equal with different bytes** → refuse, keep cache, distinct notice
("version n was re-published without a version bump"); **lower** → refuse,
keep cache, notice ("server serves version n, cached is n+k"). Every
publication, including a typo fix, bumps `directory_version` (format doc
§9).

- *Addendum 2026-09-16 (delivered by SB-3b, PB-1f):* the rule is
  implemented as `services.provider_directory.refresh.refresh_directory(*,
  cache_root, client, now, …) -> RefreshOutcome`, with the closed status
  vocabulary `REFRESH_STATUSES` of **eight** outcomes: `updated`,
  `unchanged` (also the ETag 304 branch, `fetched_at` only),
  `refused_downgrade`, `refused_republished`, `refused_unknown_version`
  (B-D-18), **`refused_unknown_key`** (a document signed under a key id
  the shipped ring does not hold — same treatment as an unknown format
  version: keep the cache, notice "client update required"; B-D-14 stands,
  the announcement is not a key), `refused_invalid` (signature, shape,
  scheme, expiry), `unavailable` (any network or HTTP failure). The
  function never raises for a network or document problem; it raises only
  for caller errors (naive `now`, a `current_key_id` not in the ring, an
  unwritable cache root) before any I/O. The monotonic comparison uses the
  cached `directory_version` even when the cached copy is expired
  (B-D-13 addendum).

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

- *Addendum 2026-09-16 (delivered by SB-3b, PB-1f/PB-1g):* the **how** is
  delivered — conditional GET (`If-None-Match` from `meta.json`, 304 →
  `unchanged`), a 10-second timeout, degradation to the cached document
  with status `unavailable`, `Provenance` (`directory_version`,
  `publishing_key_id`, `fetched_at`, `issued_at`, `valid_until`, `etag`,
  `valid`, `successor_in_use`, `announced_successor`, `provider_count`,
  `source_url`) for the panel. The **when** — on enable, on the refresh
  gesture, the 24-hour background timer via the ADR-0117 scheduler hook,
  and "never while disabled" — is SB-6's, which reads
  `provider_channel.enabled` (B-D-25) and passes a tz-aware UTC `now`.
  The directory URL is a code constant (`fetch.DIRECTORY_URL`,
  `https://portfoliflow.com/directory/v1/directory.json` and its `.sig`
  sibling), overridable only as a function argument — no environment
  variable, no settings field, no taxonomy entry, per ADR-0131's
  no-phone-home-switch reasoning. Operator instrument (PB-1g): `portfoliflow
  directory-refresh [--url] [--data-dir] [--json]` and `portfoliflow
  directory-status [--data-dir] [--json]`, database-free, exit codes
  0 ok · 2 caller/config · 3 refused · 4 unavailable · 5 no cache; the
  only clock read in the delivery sits behind `cli/directory.py::_now`.
  The deployed-directory verification (B-D-24) is the publication-day walk
  with these two commands; it is deferred to release `2026.09.1`.

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

- *Addendum 2026-09-11:* delivered by SB-4 as **provider `provider_channel`,
  field `enabled`** (tenant, config-only) — ADR-0131. Two deliberate
  departures from the ADR-0118 voice pattern: `env_fallback=False` and no
  `_ENV_CONFIG_FIELDS` entry, so no environment variable can switch the
  channel on for every tenant of a deployment at once (ADR-0129 §6);
  `optional=True`, because there is no credential to be missing. Truth
  rule: on iff the tenant value, stripped and lower-cased, is `"true"`.
  The Admin card's field hint is the ADR-0129 §6 statement of what leaves
  the instance and is the only mention of the channel while it is off
  (B-D-25 Q6). Nothing reads the switch until SB-6; the card's pill says
  so (`docs/reports/PB-1d-report.md`).

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

- *Addendum 2026-09-13:* release `2026.09.0` was cut on 2026-09-13
  **without** the publication (other items ranked higher); first
  publication of `directory_version 1` ships with **`2026.09.1`**. The
  calendar consequences above are unchanged.

### B-D-25 · The suggestion list in the Transactions UI (MB-1, operator 2026-09-11)

Seven clauses, decided on the MB-1 brief (Mission Control chat 3); they bind
SB-6 and the B-2 "Send" gesture.

1. **Placement.** A fourth blotter-row gesture **`Providers…`** opens a panel
   in the row's own detail cell `#tx-detail-{id}` — the third occupant of the
   D-6b slot beside Impact and Cancel. The panel lists the providers that
   match the ticket, one row per provider with its own **`Export sealed
   ticket`** button. In B-2, "Send" becomes a **second button per provider
   row inside the panel**, beside Export — two buttons, not a menu (A-13);
   the blotter row itself never gains a fifth gesture.
2. **Statuses.** The gesture appears on `proposed` and `approved` tickets
   only. Draft: parameters incomplete (the Impact rule, D-6f). Booked and
   cancelled: no gesture.
3. **Provenance.** Panel foot, one line: `Directory v<directory_version> ·
   signed by <publishing_key_id> · fetched <when> · valid to <valid_until>`.
   States: *stale* (last refresh failed, cache still valid) keeps rows and
   export; *expired* and *absent* show the line and **no provider rows — and
   therefore no export button anywhere in the panel**. Successor notices
   from SB-3a are log lines, not UI.
4. **Refresh.** A button beside the provenance line, for **any**
   Transactions user — the fetch is a public conditional GET carrying a
   version pin and no query (ADR-0129 §6). The response re-renders the
   panel, so a refresh also re-filters.
5. **Test entries.** `[TEST]` entries are **shown**, the prefix rendered as a
   chip, with one standing sentence in the panel foot: *Entries marked
   [TEST] are placeholder desks operated by PortfoliFLOW for verifying the
   channel; an export to them reaches nobody.* No second flag in the format
   (B-D-2: marked by name alone).
6. **Disabled.** With `provider_channel.enabled` off — the default — the
   Transactions area renders **nothing**: no gesture, no greyed button, no
   tooltip. The channel is mentioned only in the setting's own card
   (ADR-0131 §4–5). "Absent, not disabled."
7. **Filter miss.** Matching providers are listed first; non-matching ones
   sit behind `show the other N` with a count line (`1 of 2 listed providers
   match this ticket`). Coverage hints are hints, not eligibility
   (ADR-0129 §2).

*Clarifications (operator, 2026-09-11):* no "Send" placeholder of any kind
renders in B-1 — the position is reserved, not drawn; and in the expired /
absent states of clause 3 no export gesture exists in the panel at all.

### B-D-26 · `asset_classes` hints match tenant codes literally (2026-09-11)

The SB-6 filter compares each directory entry's `asset_classes` strings with
the **`AssetClass.code` of the ticket's investment in the tenant**, string
for string, with no mapping table. The instance's bootstrap codes
(`services/data_normalization/fixtures/default_asset_classes.json`) are the
published vocabulary; the first directory uses exactly those. A tenant that
has renamed its codes gets non-matches, and non-matches land under
B-D-25 clause 7's `show the other N`, not in the void. A vocabulary
correction on the publisher's side is a `directory_version` bump (B-D-15);
on the tenant's side it is the tenant's own naming.

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
  - *Correction 2026-09-10 (PB-2c):* implemented as `remaining < 30`. The
    window therefore opens on **day 61**, the first day *after* the B-D-16
    deadline: a strict cron turns red the day a re-sign is overdue, not the
    day it is due — chosen so the run stays green while the operator is
    doing the re-sign. "Opens on day 60" above is superseded.

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

Stage B additions (2026-09-11): expectations in a prompt's verify-first
table — counts, line numbers, grep hits — are produced by *running* them
against the snapshot the prompt is written from, never estimated from
having read the code; a count taken from a Repomix image is `wc -l` + 2 for
the `<file>` wrappers. When such a count disagrees but every text anchor
(heading, identifier, hash) matches, the agent proceeds, reports the
disagreement, and Mission Control ratifies; any text mismatch remains a
STOP. A one-line addendum prompt is verified against the tree like any
other prompt — "committed" is not "applied" (PB-D2's change-log row).

Stage B additions (2026-09-16): (1) Mission Control's working ids —
register items, kickoff question numbers, board versions — never enter a
repository document; decision ids, ADR numbers and prompt ids do.
Forward-only: committed history is not rewritten for it. (2) Release
process: `pyproject.toml` version commit → CI green → tag; the version
guard in `promote-stable` / `release-assets` fails a tag whose
`pyproject.toml` disagrees. (3) A daughter chat's closing report has fixed
headings — Commits · Files touched · Counts before/after · Items ·
Decision requests · Lessons · Register delta — and is verified by Mission
Control against a fresh full image before the strand closes. (4)
Starlette's deprecated `HTTP_422_UNPROCESSABLE_ENTITY` (seven sites in
`web/routes/watch_desk.py` and `web/routes/market_data.py`) is replaced by
the literal `status_code=422` inside the next build prompt that touches
those files, not by a housekeeping prompt. (5) Housekeeping lessons
(PB-H1/PB-H3): a verify-first row on a `tests/services/` suite states the
DB-down outcome too — those directories skip whole modules without
Postgres, so a warning gate can pass vacuously; a comment-only change never
warrants starting the container, because the web/taxonomy suites wipe the
dev tenant. (6) Sealed-box lessons (PB-1e): a tip check is a text anchor
("subject ends in `(<prompt-id>)`" plus the predecessor hash on line 2 of
`git log --oneline -2`), never a hash the operator must paste before the
run; "untouched region" line bounds include the section's trailing blank
line; every expectation in a verify-first table is executed against the
slice, including values "already known" (a stale `__all__` size travelled
from Stage A's 48 to a 55-name list); a leak guard comparing envelope
values against payload values exempts the shared routing keys by key,
checks that no payload literal equals a routing literal by vocabulary
(`"order"` is a message type and a ticket kind) and puts a length floor
under substring matches; a positive import allow-list anticipates a
binding's top-level modules (`_sodium`, `_cffi_backend`, `_openssl`) and
editable-install finders, or it pins to one venv.

Fetch-client lessons (PB-1f/PB-1g, 2026-09-16): (7) every line number in
a verify-first table comes from `grep -n` on the slice, never from reading
a printed excerpt — text anchors caught a three-line slip, but the slip was
avoidable. (8) "With Postgres down" is written as its substitute proof:
export dead DSNs (`postgresql://dead@127.0.0.1:1/x`; `.env` loads with
`override=False`) plus a control test that skips — stronger, non-invasive,
and it never asks a prompt to stop a container it does not own. (9) Pyright
gates are scoped to the touched files; the ADR-0110 island set is not
widened as a side effect of a build prompt. (10) A positive third-party
import allow-list is not mirrored for a package whose import graph includes
a CLI module (`httpx._main` drags in `click`, `rich`, `pygments`); pin what
must be present and what must be absent. (11) Anything that reads the
clock sits behind a patchable seam, and end-to-end tests derive `now` from
the fixture document so the walk does not rot on the fixture's
`valid_until`. (12) The prompt budget is **three pages**, not two: the
enumerated test cases are what produced STOP-free runs; the enumeration
stays, the budget moves.

---

## 7. Coordinates (verified 2026-09-17, post-SB-3b full image, head `de1b0cb`)

Git: `de1b0cb` = SB-3b CLI (PB-1g) on `d0393bb` = SB-3b library (PB-1f) on
`654b574` = PB-D4 on `395b157` = SB-5 (PB-1e); `origin/main` at `de1b0cb`.
Release **`2026.09.0`** cut 2026-09-13 (tag `a4de56b`, `pyproject.toml`
version `2026.09.0`) without the publication (B-D-24 addendum). Full-image
file count 1,420. Alembic head `b034` (next instance-side migration takes
`b035` — reserved for `engagements`, B-D-3/B-D-13). Last ADR **0131**; next
free **0132** (verify at writing time). Next free roadmap number **#069**
(verify at writing time — the UX-overhaul track may take it first). Wire
version constants all `= 1`: `ENVELOPE_SCHEMA_VERSION`,
`FILL_SCHEMA_VERSION`, `EXPORT_SCHEMA_VERSION`, directory format;
`META_SCHEMA_VERSION = 1` is the cache-file layout (B-D-13 addendum), not a
wire version. Runtime dependencies: `httpx>=0.27`, `cryptography>=42`,
`pynacl>=1.6.2`; `pytest-httpx` in the dev extras; SB-3b added none.
**Pure package** `services/provider_channel/`: `__init__` (`__all__` 76),
`directory`, `export`, `prefill`, `publishing_key`, `ring`, `schemas`,
`sealed_box`; purity contract: standard library, `cryptography`, `nacl`;
no network, no clock, nothing decrypts in production; suite **155**
(`test_contract.py` 24 — the two C-2 tests unchanged since Stage A).
**Client package** `services/provider_directory/` (SB-3b): `__init__`
(`__all__` 29), `fetch`, `cache`, `provenance`, `refresh`; imports the
pure package and `httpx`; its own import-isolation test forbids `core.*`,
`sqlalchemy`, `fastapi`, `pydantic`, `services.transactions`; suite **89**
(contract 3 · fetch 24 · cache 20 · provenance 7 · refresh 35), DB-free,
same 89 with dead DSNs. In the `[tool.pyright]` island set since PB-H5
(together with `services/provider_channel`; ADR-0109 §3 "island by
island"). **CLI:** `cli/directory.py` — `directory-refresh`,
`directory-status` (B-D-17 addendum); `tests/cli/` suite **102** (+24).
Key ring (SB-1): `PUBLISHING_KEY`, `PUBLISHING_KEY_ID`, `SUCCESSOR_KEY`,
`SUCCESSOR_KEY_ID`, `PUBLISHING_KEY_RING`, `PUBLISHING_KEY_PLACEHOLDER`,
`is_placeholder`; `verify_directory(document_bytes, signature, *,
publishing_key: bytes, now: date)`; `verify_directory_with_ring(...) ->
RingVerification` and `UnknownPublishingKeyId` (SB-3a). Export (SB-5):
`seal_export`, `export_plaintext_bytes`, `seal`, `open_sealed` (B-D-12
addendum). Taxonomy: `PROVIDER_TAXONOMY` has `provider_channel` with the one
config field `enabled` (SB-4); nothing reads it until SB-6. Fixtures:
`tests/services/provider_channel/fixtures/directory-1.json` (1,180 B,
SHA-256 `c1383c4f…ec83`), `directory-1.sig` (129 B),
`throwaway-recipient.json`. Full-suite baseline **5,109 passed at
`3a34537`** (2026-09-17, `docs/reports/full-suite-2026-09-17-report.md`;
5,118 selected, 0 failed, 8 skipped, 1 xfailed, 8 deselected; +188 over
the 2026-09-11 baseline of 4,927 at `2a2be75` — SB-5 +54, SB-3b +113,
UX track +21; the PB-H5 typing-only edits were in the tested tree). Six
of the eight skips are sample-workbook tests that skip wherever
`data/sample/` is absent, including CI; the 13 `@pytest.mark.asyncio`
warnings PB-H3 removed are confirmed at 0 with Postgres up. Reports in
`docs/reports/` (PB-1a, PB-1b, PB-1d, PB-1e, PB-1f, PB-1g, PB-D2, PB-D3,
PB-D4, PB-H1, PB-H3, PB-H5, full-suite ×2). Roadmap
#061 `shipped (2026-09-08)`; Stage B is #067 `in-progress`, tenant-local
provider entries #068 `open`. Infra side (by reference): signing tool
`verify` resolves the key from this repository's ring (D-SB2-14, PB-2c);
`directory_version 1` signed 2026-09-10, unpublished until `2026.09.1`
(B-D-24 addendum); publication-day walk: `env -u DATABASE_URL -u
DATABASE_URL_SUPERUSER portfoliflow directory-refresh --data-dir
/tmp/pf-walk --json` twice (`updated`, then `unchanged` via the Caddy
ETag), then `directory-status --data-dir /tmp/pf-walk`; re-sign
`directory_version 2` by 2026-11-09; rotate to `portfoliflow-2027-01` on
2027-01-01.

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
