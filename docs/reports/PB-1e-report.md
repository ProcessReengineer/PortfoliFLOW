# PB-1e — x25519 sealed box and versioned export envelope (SB-5)

**Date:** 2026-09-16 · **Decisions:** B-D-12, B-D-20, ADR-0129 §3 D-1 / §6
· **Base commit:** `5929409` (`fix(web): remove internal references from four
user-facing strings (P-UX-H1)`)

---

## OPERATOR ACTION REQUIRED

**Nothing is staged.** No `git add`, no `git commit`, no branch operation was
performed. Stage and commit the twelve paths yourself:

```bash
git add \
  pyproject.toml \
  services/provider_channel/__init__.py \
  services/provider_channel/directory.py \
  services/provider_channel/schemas.py \
  services/provider_channel/export.py \
  services/provider_channel/sealed_box.py \
  tests/services/provider_channel/test_contract.py \
  tests/services/provider_channel/test_schemas.py \
  tests/services/provider_channel/test_export.py \
  tests/services/provider_channel/test_sealed_box.py \
  tests/services/provider_channel/fixtures/throwaway-recipient.json \
  docs/reports/PB-1e-report.md
```

```
feat(provider-channel): x25519 sealed box and versioned export envelope; pynacl runtime dependency (SB-5, PB-1e)
```

Note for the operator: `pynacl` is now a **runtime** dependency, so any
environment that installs from `pyproject.toml` needs a re-install
(`pip install -e ".[dev]"`). CI already installs `.[dev]`, so no workflow
change is required.

---

## Verify-first values observed

All sixteen rows matched. No STOP condition, no count mismatch.

| # | Check | Expected | Observed |
|---|---|---|---|
| 1 | `git status --porcelain` | empty | empty |
| 1b | `git log --oneline -2` | tip subject ends `(P-UX-H1)`; second `775731e … (P-UX-0b)` | `5929409 fix(web): remove internal references from four user-facing strings (P-UX-H1)` / `775731e chore(ux): inventory JavaScript callers and link them to their trigger elements (P-UX-0b)` — **tip hash `5929409`** |
| 2 | `grep -n '^version = ' pyproject.toml` | `7:version = "2026.09.0"` | `7:version = "2026.09.0"` |
| 3 | `grep -n '"cryptography>=42",' pyproject.toml` | `37:` | `37:    "cryptography>=42",` |
| 4 | `grep -c pynacl pyproject.toml` | `0` | `0` |
| 5 | `command grep -rlw nacl services tests --include='*.py'` | no output | no output |
| 6 | `KEY_TYPE_X25519_SEALED_BOX: Final` in `directory.py` | `65:` | `65:KEY_TYPE_X25519_SEALED_BOX: Final[str] = "x25519-sealed-box"` |
| 7 | `def canonical_bytes` / `allow_nan=False` | `254:` and `273:` | `254:` and `273:` |
| 8 | `def test_provider_channel_` in `test_contract.py` | `187:` …`imports_nothing_from_the_ticket_world`, `217:` …`source_has_no_forbidden_imports` | exactly those two, at 187 and 217 |
| 9 | `'and nothing else' / 'nothing encrypts'` in `__init__.py` | `21:` and `29:` | `21:` and `29:` |
| 10 | `_SCHEMA_VERSION: Final[int]` in `schemas.py` | `48:ENVELOPE_`, `51:FILL_` — exactly two | `48:ENVELOPE_SCHEMA_VERSION: Final[int] = 1`, `51:FILL_SCHEMA_VERSION: Final[int] = 1` |
| 11 | `sed -n '118,174p' __init__.py \| grep -c '^    "'` | `55` | `55` |
| 12 | `_FORBIDDEN_MODULE_PREFIXES` / `_FORBIDDEN_CONTAINS` | `149:` and `175:` | `149:` and `175:` |
| 13 | `ls tests/services/provider_channel/fixtures/` | `directory-1.json  directory-1.sig` only | exactly those two |
| 14 | `pytest … --collect-only \| tail -1` | `101 tests collected` | `101 tests collected in 1.48s` |
| 15 | `pytest tests/services/credential_vault/test_taxonomy.py -q` | `26 passed` (DB up) or `26 skipped` | **`26 passed in 39.42s`** — dev Postgres was up |
| 16 | `pip index versions pynacl \| head -1` | lists `1.6.2` | `pynacl (1.6.2)`; available: `1.6.2, 1.6.1, 1.6.0, 1.5.0, …` — `1.6.2` is the newest, so the pin stays `>=1.6.2` |

### Per-file collected counts, before

| File | Tests |
|---|---|
| `test_contract.py` | 22 |
| `test_directory.py` | 36 |
| `test_prefill.py` | 6 |
| `test_ring.py` | 16 |
| `test_schemas.py` | 21 |
| **Total** | **101** |

---

## Diff and gates

### `git diff --stat` (tracked files)

```
 pyproject.toml                                   |   3 +
 services/provider_channel/__init__.py            |  62 ++-
 services/provider_channel/directory.py           |   5 +-
 services/provider_channel/schemas.py             | 487 ++++++++++++++++++++++-
 tests/services/provider_channel/test_contract.py |  90 +++++
 tests/services/provider_channel/test_schemas.py  | 317 +++++++++++++++
 6 files changed, 954 insertions(+), 10 deletions(-)
```

New (untracked) files:

| Path | Lines |
|---|---|
| `services/provider_channel/sealed_box.py` | 165 |
| `services/provider_channel/export.py` | 140 |
| `tests/services/provider_channel/test_sealed_box.py` | 194 |
| `tests/services/provider_channel/test_export.py` | 293 |
| `tests/services/provider_channel/fixtures/throwaway-recipient.json` | 7 |

`directory.py` is docstring-only, as required — the diff is four added lines
and one changed line inside `canonical_bytes`' docstring; the signature and
every statement are untouched.

### Gate 1 — dependency resolves

`pip install -e ".[dev]"` → `Successfully installed portfoliflow-2026.9.0
pynacl-1.6.2`. `pip show pynacl`:

```
Name: PyNaCl
Version: 1.6.2
```

### Gate 2 — package suite

```
155 passed in 1.73s
```

`0 skipped`. N = 155 ≥ 130 (101 + 54 new). Per-file split after:

| File | Before | After | New |
|---|---|---|---|
| `test_contract.py` | 22 | **24** | +2 |
| `test_directory.py` | 36 | 36 | — |
| `test_export.py` | — | **9** | +9 |
| `test_prefill.py` | 6 | 6 | — |
| `test_ring.py` | 16 | 16 | — |
| `test_schemas.py` | 21 | **43** | +22 |
| `test_sealed_box.py` | — | **21** | +21 |
| **Total** | **101** | **155** | **+54** |

### Gate 3 — SB-4 taxonomy tests unchanged

```
26 passed in 39.59s
```

The dev Postgres was up for both the verify-first run and the gate run, so
this is the real run, not the DB-free substitute. (For the record, the
substitute proof also holds: gate 8's path list contains nothing under
`services/credential_vault/` or `tests/services/credential_vault/`.)

Additionally, and beyond the stated gates, the two suites outside the package
that import `services.provider_channel` plus the whole regression suite were
run, since `nacl` entering the import graph is a repo-wide fact:

```
pytest tests/services/credential_vault tests/web/test_provider_credentials.py tests/regression -q
257 passed in 317.76s (0:05:17)
```

### Gate 4 — the two existing C-2 tests untouched

```
git diff -U0 tests/services/provider_channel/test_contract.py | grep '^@@'
@@ -60,0 +61,3 @@ from services.provider_channel.schemas import (
@@ -64,0 +68,3 @@ from services.transactions.constants import (
@@ -139,0 +146,12 @@ def test_every_prefill_field_is_a_ticket_column(field: str) -> None:
@@ -238,0 +257,72 @@ def test_provider_channel_source_has_no_forbidden_imports() -> None:
```

Both C-2 tests pass unchanged (they are part of the 24 above). Three of the
four hunks are outside 141–239 outright. The fourth anchors at **old line
238** — one line inside the stated bound — because it is a pure insertion
placed where §2.7 asks for it, "after the two existing tests": old line 238 is
the second blank line of the C-2 section and old line 239 is the `# ---` that
opens C-3, so there is no earlier anchor available for content that sits
between them. The C-2 block itself is byte-identical:

```
git show HEAD:…/test_contract.py | sed -n '141,238p'  >  old.txt
sed -n '159,256p' …/test_contract.py                  >  new.txt
cmp old.txt new.txt   →  IDENTICAL (98 lines, byte for byte)
```

The only alternative that would have moved the anchor past 239 was appending
the new test at the end of the file, below C-4 — which would have split C-2
across the module. The placement §2.7 specifies was kept and the boundary is
reported here instead.

### Gate 5 — lint and types

```
ruff check services/provider_channel tests/services/provider_channel pyproject.toml   →  All checks passed!
ruff format --check services/provider_channel tests/services/provider_channel          →  17 files already formatted
```

`pyright services/provider_channel tests/services/provider_channel` reports
**2 errors, 0 warnings**, both in `tests/services/provider_channel/test_directory.py`,
a file this task did not touch (`git status` shows it clean) and which is
outside the ADR-0110 typing-island set (`services/overlay`, `services/market_data`):

```
test_directory.py:380:49 - error: Argument of type "object" cannot be assigned to parameter "provider_id" of type "str" in function "_provider"
test_directory.py:424:9  - error: "__setitem__" method not defined on type "Mapping[str, bytes]" (reportIndexIssue)
```

Both are deliberate test constructions that pre-date this work — a
`**override: object` splat into a typed helper, and a `pytest.raises(TypeError)`
around a `Mapping.__setitem__` that is *supposed* to be absent (the B-D-14
"the ring cannot be extended at runtime" pin). Pyright over exactly the nine
files this task created or edited:

```
0 errors, 0 warnings, 0 informations
```

### Gate 6 — fixture

```
python -c "import json; d=json.load(open('tests/services/provider_channel/fixtures/throwaway-recipient.json')); print(list(d)[0])"
note
```

### Gate 7 — export surface and version constants

```
awk '/^__all__ = \[/{f=1} f&&/^    "/{c++} /^\]/{f=0} END{print c}' services/provider_channel/__init__.py
76
```

(The `__all__` block now begins at line 147 and runs to 224, so the literal
`sed -n '118,200p'` window from the prompt no longer spans it; `sed -n '147,224p' … | grep -c '^    "'`
likewise returns `76`.)

```
grep -n '_SCHEMA_VERSION: Final\[int\]' services/provider_channel/schemas.py
59:ENVELOPE_SCHEMA_VERSION: Final[int] = 1
62:FILL_SCHEMA_VERSION: Final[int] = 1
68:EXPORT_SCHEMA_VERSION: Final[int] = 1
```

Exactly three hits; the `ENVELOPE_` and `FILL_` values are unchanged at `1`.

### Gate 8 — working tree

```
 M pyproject.toml
 M services/provider_channel/__init__.py
 M services/provider_channel/directory.py
 M services/provider_channel/schemas.py
 M tests/services/provider_channel/test_contract.py
 M tests/services/provider_channel/test_schemas.py
?? services/provider_channel/export.py
?? services/provider_channel/sealed_box.py
?? tests/services/provider_channel/fixtures/throwaway-recipient.json
?? tests/services/provider_channel/test_export.py
?? tests/services/provider_channel/test_sealed_box.py
?? docs/reports/PB-1e-report.md
```

Exactly the twelve expected paths, and nothing else.

### The `sys.modules` delta — verbatim from the C-2 test

```
third-party modules after importing services.provider_channel: ['_cffi_backend', '_sodium', 'cryptography', 'nacl']
```

Two names beyond the prompt's allow-list of `{'cryptography', 'nacl', 'cffi',
'_cffi_backend'}` appeared, and both were ratified rather than treated as a
STOP, per §2.7:

* **`_sodium`** — libsodium's C extension, i.e. part of `nacl`'s own binding
  tree, exactly the case §2.7 names. Added to the allow-list with a comment.
* **`_openssl`** — `cryptography`'s equivalent C extension. It appears in
  `sys.modules` but is filtered out by the importable-top-level-package step,
  so it does not reach the assertion here. It is nevertheless in the
  allow-list, defensively: it is the same category as `_sodium`, and whether
  the `find_spec` filter drops it is a property of how the wheel was built,
  not something this test should depend on.

A third category had to be **excluded** rather than allow-listed:
`__editable___portfoliflow_2026_9_0_finder` and
`__editable___portfolioflow_0_1_0_finder`. These are the path finders an
editable install injects — they are *how* `services` is importable at all,
not something the package imports — and their module names carry the
installed version (one of them is a leftover from the retired `portfolioflow`
misspelling, ADR-0044). Allow-listing them would have pinned the test to one
developer's venv. The test drops any name beginning with `__editable__`, with
a comment saying why.

### Fixture

```
public_key_hex = a4ed736c37fe8a061f44389a143a7dc02d3de83905d2eb889e1edb17ed619e38
```

Throwaway X25519 pair, generated by the run that produced this report with
the package's own `seal` / `export_plaintext_bytes` (B-D-20). The private key
is published in the fixture on purpose; the generator script is not
committed. `note` is the first content line by construction.

---

## Open questions

Each of these is something the prompt left open or where the prompt's literal
text could not be satisfied as written. All are decided and implemented; none
needs a code change to proceed.

1. **The leak guard's `guarded` set collided with the payload's own
   `ticket_kind`.** The prompt's formula exempts `message_type` by *key*, but
   the prompt's `_payload` helper sets `ticket_kind="order"` and
   `message_type=MESSAGE_TYPE_ORDER` — the same *string*. The envelope
   legitimately restates `message_type`, so the intersection was never empty
   and the test could not pass as specified. Exempting the value `"order"`
   would have silently un-guarded `ticket_kind`, so instead the leak-guard
   test alone overrides `ticket_kind="secondary"` (still a valid
   `TICKET_KINDS` member, still coherent with `message_type="order"`), which
   keeps every payload field guarded. The `_payload` helper's default is
   unchanged at `"order"`, as specified.
2. **The substring half of the leak guard was flaky as specified, at roughly
   0.7% per run.** A sealed export's base64 is ~670 characters drawn from a
   64-symbol alphabet, so short payload values built from that same alphabet
   occur in it by chance. Measured over 20,000 seals: `"EUR"` 0.230%, `"buy"`
   0.230%, `"500"` 0.220%, `"isin"` 0.005%, and nothing at five characters or
   more. The substring assertion is therefore applied to values of **six
   characters or more** (`_SUBSTRING_GUARD_MIN_LENGTH`), where the chance is
   below one in a hundred million, with the measurement recorded in the
   comment. Nothing is lost: the set-membership half still covers every value
   regardless of length; values containing `.` or `-` could never collide
   anyway (neither is in the standard base64 alphabet); and a plaintext copy
   appended to an artefact — the failure mode this half exists to catch —
   would carry the long values (the ISIN, the dates, the message) too. The
   test asserts at least five values were actually checked, so the half
   cannot go vacuous. Re-run 40× consecutively: 40/40 green.
3. **`EXPORT_DIRECTIONS` got its own section rather than joining the
   "Message vocabulary" block.** §2.3 says "in the vocabulary block" and
   points at the statuses block (lines 68–91) for the idiom. The directions
   are placed in a new `# Export directions` section immediately after the
   envelope statuses, carrying the same header comment explaining why the
   literal is re-declared rather than imported. Folding them into "Message
   vocabulary" would have mixed message types with trade directions under one
   heading.
4. **`__all__` ordering.** §2.6 describes it as "upper-case constants, then
   classes, then functions, each group alphabetical", which is *not* plain
   `sorted()` — plain sorting interleaves `Directory` among the `DIRECTORY_*`
   constants. The grouped rule was implemented and then verified against the
   committed files before use: it reproduces both `schemas.py` (18 names) and
   `__init__.py` (55 names) exactly as they stand at `HEAD`.
5. **`parse_export` reads `direction` inline rather than through a new
   reader.** §2.3 lists three new readers (`_as_optional_decimal`,
   `_as_hex_key`, `_as_base64`) and none of them covers an *optional* closed
   vocabulary, which `direction` is. Rather than add a fourth reader the
   prompt did not ask for, the membership check is done inline in
   `parse_export`, immediately after `_as_optional_str`, and its `SchemaError`
   names the field and the offending value like every other refusal.
6. **Two small serialiser helpers were added to `schemas.py`.**
   `_optional_decimal_text` and `_optional_date_text` — three lines each —
   keep `export_to_dict` readable across its five optional decimals and two
   optional dates rather than repeating a conditional expression seven times.
   They are private and add no behaviour.
7. **`_KEY_HEX_LENGTH` is re-declared in `schemas.py`.** `directory.py`
   already has a private constant of that name; importing a private name
   across modules would be worse than restating `64` beside the `_HEX_DIGITS`
   alphabet it is used with. Same reasoning as the `_SEALED_BOX_OVERHEAD = 48`
   re-declaration §2.3 explicitly asks for.
8. **`schemas.py` now imports from `directory.py`.** `TICKET_KINDS` and
   `SUPPORTED_ENCRYPTION_KEY_TYPES` are imported as §2.3 directs. No cycle is
   introduced: `directory.py` imports only `publishing_key`, and `export.py`
   sits above both.
