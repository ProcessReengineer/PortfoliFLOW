# Provider Directory Format v1 (ADR-0129 Stage A)

Status: **Stage A — contract only.** The formats below are implemented and
tested in `services/provider_channel/`. No service consumes them yet: the
ADR-0128 ticket statuses `sent` / `acknowledged` / `executed` remain defined
in the schema and unreachable in code, and nothing in the tree fetches,
sends, encrypts or decrypts. Stage B builds the directory service, the relay
and the provider portal.

---

## 1. Purpose and trust model

The provider directory is the list of counterparties an instance may talk to
— brokers, secondary desks, advisory and legal firms — published by
portfoliflow.com as a single signed document.

A directory is trusted because it is **signed**, never because of where it
was fetched from. That inverts the usual dependency: the transport can be a
plain HTTPS GET, a file on a USB stick, or a copy pasted by an operator, and
the security properties do not change. Verification happens locally, against
one key.

That key ships in this AGPL repository
(`services/provider_channel/publishing_key.py`). Publishing it in the source
tree is the point: the realistic attack on this design is **key
substitution** — persuading an instance to trust a directory signed by
someone else — not breaking Ed25519. A key that lives in a readable,
diffable, distributed file cannot be swapped quietly on a server.

In Stage A the shipped key is a **placeholder**, and verification fails
closed against it: `verify_directory` raises `PublishingKeyNotConfigured`
before it reads a single byte of the document. Nothing can be trusted by
accident in the window between this contract landing and the real key being
minted.

---

## 2. Document shape

A directory document is a JSON object. Every field is required unless the
table says otherwise; unknown keys are refused and named.

### `Directory`

| Field | Type | Constraint | Reference |
|---|---|---|---|
| `format_version` | integer | must equal `1` | ADR-0129 §2 |
| `directory_version` | integer | monotonic publication counter — the version pin. A client refuses to move backwards (Stage B rule) | ADR-0129 §2 |
| `issued_at` | string | `YYYY-MM-DD`; first valid day, inclusive | ADR-0129 §2 |
| `valid_until` | string | `YYYY-MM-DD`; last valid day, inclusive | ADR-0129 §2 |
| `signature_scheme` | string | `"ed25519"` — the only value v1 accepts | D-sig |
| `publishing_key_id` | string | non-empty; identifies the signing key | ADR-0129 §2 |
| `successor_key` | object \| null | optional; see below | ADR-0129 §2 |
| `providers` | array | entries with unique `provider_id`; returned sorted by `provider_id` | ADR-0129 §2 |

### `ProviderEntry`

| Field | Type | Constraint | Reference |
|---|---|---|---|
| `provider_id` | string | non-empty; unique within the document | ADR-0129 §2 |
| `display_name` | string | non-empty | ADR-0129 §2 |
| `provider_type` | string | one of `broker`, `secondary_desk`, `advisory`, `legal`, `fund_selection`, `other` | ADR-0129 §2 |
| `ticket_kinds` | array of string | subset of `order`, `commitment`, `secondary` — mirrors `services.transactions.constants.KINDS` | ADR-0128 §1 |
| `engagement_categories` | array of string | subset of `advisory`, `legal`, `fund_selection`, `second_opinion`, `other` | ADR-0129 §5 |
| `asset_classes` | array of string | free-form hints; no closed vocabulary, because the directory has no opinion about a tenant's taxonomy | ADR-0129 §2 |
| `jurisdictions` | array of string | ISO 3166-1 alpha-2, upper case | ADR-0129 §2 |
| `encryption_key_type` | string | `"x25519-sealed-box"` | ADR-0129 D-1 |
| `encryption_public_key` | string | 64 hex characters (32 bytes). **Length-checked only** — nothing in Stage A encrypts | ADR-0129 D-1 |

### `SuccessorKey`

| Field | Type | Constraint | Reference |
|---|---|---|---|
| `publishing_key_id` | string | non-empty; identifies the announced key | ADR-0129 §2 |
| `public_key` | string | 64 hex characters (32 bytes), Ed25519 | ADR-0129 §2 |
| `valid_from` | string | `YYYY-MM-DD`; the day the announced key takes over | ADR-0129 §2 |

---

## 3. Canonical serialisation

Exactly one byte string is signed, produced by exactly one call:

```python
json.dumps(
    document,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
    allow_nan=False,
).encode("utf-8")
```

That is `services.provider_channel.canonical_bytes`. Keys are sorted at every
level, there is no insignificant whitespace, and non-finite numbers are
rejected.

**A document is refused unless it is already in this form.** `verify_directory`
re-serialises what it parsed and compares byte for byte; a pretty-printed
document with a genuine signature over its pretty bytes is still refused,
with the message *"document is not in canonical form"*. This closes the
"same content, other bytes" ambiguity: the question *which bytes were
signed?* has one answer, so a verifier can never be talked into checking one
spelling while a reader consumes another.

**The signature travels beside the document, never inside it.** The signed
object carries no `signature` member — a document containing one is refused.
Publishers distribute the pair `(document_bytes, signature)`. Consequently a
document never has to be edited (to strip a field) in order to be checked,
which is where signature-inside-the-payload formats habitually go wrong.

---

## 4. Signature

* **Scheme.** `signature_scheme` is `"ed25519"`, and Ed25519 is the only
  value v1 accepts. The field is versioned *inside* the format so Stage B can
  rotate to another scheme without a new `format_version` (D-sig).
* **Implementation.** The existing `cryptography` dependency
  (`cryptography.hazmat.primitives.asymmetric.ed25519`). No new dependency
  was added, and there is no libsodium/PyNaCl anywhere in the tree.
* **Key encoding.** Raw 32-byte keys. `publishing_key` is passed to
  `verify_directory` as raw bytes; keys *inside* a document
  (`successor_key.public_key`, `encryption_public_key`) are 64 hex
  characters.
* **Signature file encoding (Stage B, B-D-23).** On the wire the detached
  signature is a separate file: 128 lowercase hex characters + one LF
  (129 bytes), `Content-Type: text/plain`; the document is served as
  `application/json`. Decode strictly — no whitespace tolerance, no case
  folding. See `docs/concepts/provider-channel-stage-b-decisions.md` B-D-23.
* **`publishing_key_id`.** Names the key that signed this document, so a
  client holding several keys during a rotation knows which to try.
* **Successor-key announcement.** Key rotation without an already-trusted
  channel is a chicken-and-egg problem; announcing the next key inside a
  document signed by the current one is the way out. A client that verified
  this document with key K may trust the successor announced inside it from
  `valid_from`. Stage A only *parses* the announcement — **the client rule is
  Stage B.**

### Order of checks

`verify_directory` runs seven checks, each fail-closed, each with its own
exception, so a log line can say exactly why a document was refused:

| # | Check | Exception on failure |
|---|---|---|
| 1 | publishing key is not the un-minted placeholder | `PublishingKeyNotConfigured` |
| 2 | bytes are a canonical JSON object with no `signature` member | `InvalidDirectorySignature` |
| 3 | `format_version` is readable by this build | `UnknownDirectoryFormatVersion` |
| 4 | `signature_scheme` is supported | `UnsupportedSignatureScheme` |
| 5 | signature verifies over the document bytes | `InvalidDirectorySignature` |
| 6 | `now` lies inside the validity window | `DirectoryExpired` |
| 7 | the document matches the v1 shape | `DirectoryShapeError` |

Steps 3 and 4 deliberately precede the signature check: refusing a document
this build cannot read is cheaper and clearer than reporting a signature
failure for it.

Only step 7 returns a value. The shape parser is module-private, so **a
parsed-but-unverified `Directory` is not constructible through the public
API** — a caller cannot mean to verify, forget, and still end up holding
something that looks authoritative.

---

## 5. Validity

`issued_at` and `valid_until` bound an inclusive window; both boundary days
verify. A document is refused when `now` falls outside it — including when
`now` is *before* `issued_at`: a document from the future means the client is
not seeing the current publication, which is as much a reason to distrust it
as an expired one.

`now` is passed in as a `date` (D-clock). The package reads no clock, which
keeps it pure and makes every validity test deterministic.

`directory_version` is the monotonic pin: it increments with each
publication, and a client that has seen version *n* refuses version *n−1*.
Enforcing that requires remembering the last version seen, which is state,
which is Stage B.

---

## 6. Message and confirmation schemas

Two wire shapes, each versioned independently of the directory.

### Envelope — `ENVELOPE_SCHEMA_VERSION = 1`

The ADR-0129 §3 routing envelope: who sends to whom, about which correlated
object, and what the relay currently knows.

| Field | Type | Constraint |
|---|---|---|
| `schema_version` | integer | must equal `1` |
| `sender_tenant_handle` | string | non-empty; the relay resolves it |
| `provider_id` | string | non-empty; matches a directory entry |
| `message_type` | string | `order` or `engagement` |
| `correlation_id` | string | the ticket or engagement id **as text**; opaque here — the package does not know about UUIDs, so an envelope stays readable without a database |
| `status` | string | `sent`, `acknowledged`, `executed` or `declined` |
| `created_at` | string | ISO-8601 **with offset**; naive timestamps refused |
| `updated_at` | string | ISO-8601 **with offset** |

The envelope carries **no payload field**. The ciphertext is Stage B's
concern, and a Stage-A reader that cannot decrypt should also be unable to
hold a decrypted body by accident (ADR-0129 §6).

`sent` / `acknowledged` / `executed` mirror the ADR-0128 ticket statuses of
the same names. `declined` is **envelope-only**: it has no ticket status,
because declining is a fact about the *message* — the ticket stays exactly
where the operator left it. No book state appears in this vocabulary at all;
the channel informs, and `booked` on an envelope is refused.

### Fill payload — `FILL_SCHEMA_VERSION = 1`

The ADR-0129 §3 structured confirmation. A fill is data, not prose, so the
numbers arrive as fields rather than as something a human retypes off a PDF.

| Field | Type | Constraint |
|---|---|---|
| `schema_version` | integer | must equal `1` |
| `units` | string | decimal string, unsigned; direction lives on the ticket |
| `price_per_unit` | string | decimal string |
| `fees` | string | decimal string |
| `taxes` | string | decimal string |
| `currency` | string | three upper-case letters |
| `trade_date` | string | `YYYY-MM-DD` |
| `settlement_date` | string \| null | optional; may not precede `trade_date` |
| `isin` | string \| null | optional |
| `message` | string \| null | optional free text for the human (ADR-0129 §4) |

**Decimal-as-string.** Money fields arrive as JSON *strings* (`"123.45"`) and
become `Decimal`. A JSON **number** for a money field is refused outright.
The whole point of the encoding is that a provider's digits reach the ticket
unrounded; a float would already have lost them by the time the parser saw
the value.

**Strictness.** Both parsers refuse an absent or unrecognised
`schema_version` loudly (`UnknownSchemaVersion`, naming the offending
version) and refuse unknown keys, naming them. A forward-compatible reader —
one that ignores fields it was not taught — is a Stage-B decision with its
own compatibility story, not a Stage-A default that drops data silently.

---

## 7. Pre-fill mapping

`fill_to_prefill(fill)` maps a confirmation onto proposed booking fields:

| Confirmation field | Pre-fill key | Note |
|---|---|---|
| `units` | `units` | unchanged |
| `price_per_unit` | `price_per_unit` | unchanged |
| `fees` | `fees` | unchanged |
| `taxes` | `taxes` | unchanged |
| `currency` | `currency` | unchanged |
| `trade_date` | `trade_date` | unchanged |
| `settlement_date` | `settlement_date` | `None` when absent — the one default |
| `isin` | `master_data` | becomes `{"identifier_scheme": "isin", "identifier_value": …}`; the key is absent entirely when no ISIN was stated |
| `message` | *(not mapped)* | addressed to the human reviewing the pre-fill, not to a ticket column |

Every key mirrors a `trade_tickets` column, so no translation table stands
between the confirmation and the composer.

**The instance pre-fills the ADR-0128 booking step for user review — it never
books autonomously** (ADR-0129 §3). An `executed` message is the provider's
*claim* about what happened; turning a claim into a booking is an act the
operator performs with the numbers in front of them.

**D-amounts — the pre-fill maps, it never computes.** `gross_amount` and
`net_amount` are deliberately absent from the result. Net arithmetic is
stated exactly once in the codebase, in the ticket layer's validation
(`net_amount` stated outright, else `gross ± fees/taxes`); a second
implementation here would be a second answer waiting to diverge. The composer
derives the totals from these fields exactly as it does for a hand-typed
ticket.

---

## 8. Runtime posture in Stage A

**The channel is absent, not disabled.** There is no feature flag, no
`scoped_settings` row and no credential-taxonomy declaration, because nothing
reads one: no code path fetches a directory, sends a message or receives a
confirmation (D-flag). Stage B declares `provider_channel.enabled` (tenant
scope, config-only, following the `voice` precedent in
`services/credential_vault/taxonomy.py`) through an ADR-0112 annex ADR, at
the point where there is something for it to switch off.

The package accordingly contains **no network code** — no HTTP client or
server, no polling, no e-mail — and **nothing encrypts or decrypts**;
`encryption_key_type` names the sealed-box family without implementing it.
`verify_directory` takes bytes the caller obtained however it liked.

Its import surface is the standard library plus `cryptography` (Ed25519 and
`InvalidSignature`), and nothing else: no ORM, no repositories, no web
framework, and nothing under `services/transactions/`, whose package
`__init__` would drag the whole ticket-service graph in behind it. Where a
vocabulary is shared with that package — the three channel statuses, the
master-data keys, the ISIN scheme, the ticket kinds — the literal is
re-declared locally beside a comment naming its twin, and
`tests/services/provider_channel/test_contract.py` asserts the two are equal.
That is deliberate duplication of a *string*, not of a singularity, and the
pin turns drift into a test failure.

---

## 9. Versioning rules

| Change | Bump |
|---|---|
| Add, remove or re-mean a `Directory` / `ProviderEntry` / `SuccessorKey` field | `DIRECTORY_FORMAT_VERSION` |
| Change the canonical serialisation, or move the signature inside the document | `DIRECTORY_FORMAT_VERSION` |
| Add a value to a closed vocabulary (`provider_type`, `ticket_kinds`, `engagement_categories`, `encryption_key_type`) | `DIRECTORY_FORMAT_VERSION` — v1 readers refuse unknown members, so an addition is breaking |
| Add a signature scheme | **no bump** — `signature_scheme` is versioned inside the format (D-sig) |
| Publish a new directory (entries added, removed or edited) | `directory_version` only |
| Rotate the publishing key | `directory_version` only; announce via `successor_key` |
| Add, remove or re-mean an envelope field | `ENVELOPE_SCHEMA_VERSION` |
| Add, remove or re-mean a fill field | `FILL_SCHEMA_VERSION` |
| Add an envelope `status` or `message_type` value | `ENVELOPE_SCHEMA_VERSION` — same reasoning as closed vocabularies above |

The three versions are independent: a new envelope field does not invalidate
a published directory.

---

## 10. Worked example

A minimal valid document with two providers and a successor-key announcement.
Shown indented for reading — **the signed bytes are the canonical
single-line form**, which is what `canonical_bytes` produces and the only
form that verifies.

```json
{
  "directory_version": 12,
  "format_version": 1,
  "issued_at": "2026-09-01",
  "providers": [
    {
      "asset_classes": ["listed_equity", "listed_bonds"],
      "display_name": "Alpha Broker",
      "encryption_key_type": "x25519-sealed-box",
      "encryption_public_key": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "engagement_categories": [],
      "jurisdictions": ["DE", "LU"],
      "provider_id": "alpha-broker",
      "provider_type": "broker",
      "ticket_kinds": ["order"]
    },
    {
      "asset_classes": ["private_equity"],
      "display_name": "Zeta Secondary Desk",
      "encryption_key_type": "x25519-sealed-box",
      "encryption_public_key": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "engagement_categories": ["second_opinion"],
      "jurisdictions": ["CH"],
      "provider_id": "zeta-secondary",
      "provider_type": "secondary_desk",
      "ticket_kinds": ["secondary", "commitment"]
    }
  ],
  "publishing_key_id": "portfoliflow-2026-09",
  "signature_scheme": "ed25519",
  "successor_key": {
    "public_key": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    "publishing_key_id": "portfoliflow-2027-01",
    "valid_from": "2027-01-01"
  },
  "valid_until": "2026-10-01"
}
```

Signing and verifying it with a throwaway key — the production private key is
minted and held by the operator and never lives in this repository:

```python
from datetime import date

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from services.provider_channel import sign_directory, verify_directory

document = {...}  # the object above, as Python primitives

key = Ed25519PrivateKey.generate()
payload, signature = sign_directory(document, private_key=key.private_bytes_raw())

directory = verify_directory(
    payload,
    signature,
    publishing_key=key.public_key().public_bytes_raw(),
    now=date(2026, 9, 7),
)

[entry.provider_id for entry in directory.providers]
# ['alpha-broker', 'zeta-secondary']   — always sorted
directory.successor_key.publishing_key_id
# 'portfoliflow-2027-01'
```

Passing the shipped `PUBLISHING_KEY` instead of a throwaway key raises
`PublishingKeyNotConfigured` for as long as it is the placeholder — which is
the intended Stage-A behaviour, and the tripwire that turns red the day the
real key lands.

---

## 11. Parked for Stage B

Progress against this list is recorded in
`docs/concepts/provider-channel-stage-b-decisions.md` (B-D-12…B-D-24);
this section is left as the Stage A statement of what was parked.

* Publishing-key lifecycle: minting, custody, rotation cadence, and the
  client rule for acting on a `successor_key` announcement.
* The directory service itself — publication, hosting, fetch cadence,
  caching, and the `directory_version` monotonicity check.
* The relay API and its retention policy (the zero-knowledge property).
* The provider portal — web, not native; e-mail as notification only.
* The `provider_channel.enabled` setting, via an ADR-0112 annex ADR.
* A forward-compatible reader, if the compatibility story warrants one.
* The sealed-box implementation (`x25519-sealed-box`): encryption and
  decryption, currently named and not implemented.
* Tenant-local provider entries — user-own provider lists, explicitly step 2
  within ADR-0129's own staging.
* The `engagement` object, of which only `MESSAGE_TYPE_ENGAGEMENT` exists as
  a literal today.
* Arming the ticket statuses `sent` / `acknowledged` / `executed`, and the
  blotter "Send" gesture that would write the first of them.

---

*Implemented by `services/provider_channel/`; tested by
`tests/services/provider_channel/`. See ADR-0129 (§1 staging, §2 directory,
§3 relay/fill payload, §6 privacy invariants), ADR-0128 (status vocabulary),
and `docs/concepts/transactions-record-flow-plan.md` §1.3 and §6.*
