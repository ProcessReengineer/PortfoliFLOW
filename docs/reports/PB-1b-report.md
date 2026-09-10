# PB-1b — Provider Channel Stage B · SB-3a report

**Strand:** key-ring selection wrapper — verify by `publishing_key_id`, refuse
unknown ids, report successor notices
**Prompt:** PB-1b, issued 2026-09-10 (Mission Control Stage B, chat 3)
**Repository:** PortfoliFLOW (AGPL), `/home/soenke/Code/PortfoliFLOW/PortfoliFLOW`
**Run:** 2026-09-10 · no STOP condition triggered · all deliverables written ·
**full suite stopped at 17% on operator instruction (time limit) and deferred
to a separate evening run — see §4**
**Git:** no operations performed. Read-only git only; the working tree carries
the changes, the commit is the operator's.
**Record:** B-D-14, B-D-16, D-clock, C-2.

---

## 1. OPERATOR ACTION REQUIRED

Commit the four paths:

| File | State |
|---|---|
| `services/provider_channel/ring.py` | new (untracked) — the selection step |
| `services/provider_channel/__init__.py` | modified — four re-exports, one docstring clause |
| `tests/services/provider_channel/test_ring.py` | new (untracked) — 9 test functions, 16 collected items |
| `docs/reports/PB-1b-report.md` | new (untracked) — this report |

Suggested message:

```
feat(provider-channel): ring-selection wrapper — verify by publishing_key_id, refuse unknown ids, report successor notices (SB-3a)
```

**Open step: the full-suite baseline (OP-B-10), deferred to this evening.**
The run was started and then stopped at 17% on your instruction, because the
session ended around 16:00 CEST and the gate takes about three hours. 887
tests had run by then, with no failures or errors (§4). The baseline is
therefore still outstanding. Please run it separately, for example
overnight:

```sh
cd /home/soenke/Code/PortfoliFLOW/PortfoliFLOW
podman start portfoliflow-postgres      # left running after this session
nohup ./.venv/bin/python -m pytest -q > /tmp/pf-full-suite.log 2>&1 &
tail -1 /tmp/pf-full-suite.log          # next morning: the "N passed, …" line
```

Expected count: 4,904 (track close) + 4 (SB-1) + 16 (this strand) =
**4,924 passed**, plus whatever skip/xfail/deselect tail the line carries.
Committing doesn't have to wait for it: the whole blast radius is
`services/provider_channel/` and its test package, and both are green (§4).
The count is the board's baseline, not a gate for this diff.

**Housekeeping:** the `portfoliflow-postgres` container was `Exited` when
this session started. I started it for the full-suite run and left it
running for tonight. Stop it with `podman stop portfoliflow-postgres` if you
don't want it up until then.

**Three judgment calls to confirm.** All three are pinned by tests and listed
in §6. None of them changes a name or signature from the prompt:

1. The `current_key_id ∉ ring` guard runs on **every** call, not only on the
   unreadable-id path.
2. Successor keys are compared as **bytes**, not by `.hex()` text.
3. Two extra `pytest.param`s and one extra assertion inside the specified
   tests pin (1), (2), and the rule-1 "id is not a `str`" branch.

---

## 2. Verify-first values observed

| # | Check | Expected | Observed |
|---|---|---|---|
| 1 | `git status --porcelain` | empty | empty ✔ |
| 2 | `git log -1 --format=%s` | ends with `(PB-D2)` | `docs(provider-channel): record B-D-23/B-D-24, Stage B addenda and #067 progress (PB-D2)` ✔ |
| 3 | `ls services/provider_channel/` | five modules + `__pycache__`, no `ring.py` | `__init__.py  __pycache__  directory.py  prefill.py  publishing_key.py  schemas.py` ✔ |
| 4 | `grep -rc 'UnknownPublishingKeyId\|verify_directory_with_ring\|RingVerification' services tests` | all 0 | no file with a non-zero count ✔ |
| 5 | `^class PublishingKeyNotConfigured` in `directory.py` | line 117 | `117` ✔ |
| 6 | `^def verify_directory` in `directory.py` | line 479 | `479` ✔ |
| 7 | `PUBLISHING_KEY_RING` in `__init__.py` | 2 hits, lines 86 and 133 | `86:    PUBLISHING_KEY_RING,` · `133:    "PUBLISHING_KEY_RING",` ✔ |
| 8 | `"PublishingKeyNotConfigured",` count | 1 | `1` ✔ |
| 9 | helpers in `test_directory.py` | 58, 59, 73, 128, 97 | `_PRIVATE_BYTES` 58 · `_PUBLIC_BYTES` 59 · `_REAL_KEY_NOW` 73 · `_document` 97 · `_load_fixture` 128 ✔ |
| 10 | `"date.today"` count in `test_contract.py` | 1 | `1` ✔ |
| 11 | `sha256sum …/directory-1.json` | `c1383c4f…d4ec83` | `c1383c4fe2b3440cfb64276d2fdbb5018236950199e4eb6780787c32fbd4ec83` ✔ |
| 12 | provider-channel suite | `85 passed` | `85 passed in 2.50s` ✔ |
| 13 | `ls docs/reports/` | `PB-1a-report.md`, `PB-D2-report.md` | exactly those two ✔ |

---

## 3. What landed

### `services/provider_channel/ring.py` (new, 202 lines)

Public surface, verbatim:

```python
SuccessorStatus = Literal["in_ring", "not_in_ring", "contradicts_ring"]


class UnknownPublishingKeyId(DirectoryVerificationError):
    """The document names a publishing key id that is not in the ring."""


@dataclass(frozen=True, slots=True)
class RingVerification:
    directory: Directory
    key_id: str
    successor_in_use: bool
    announced_successor: SuccessorStatus | None


def verify_directory_with_ring(
    document_bytes: bytes,
    signature: bytes,
    *,
    now: date,
    ring: Mapping[str, bytes] = PUBLISHING_KEY_RING,
    current_key_id: str = PUBLISHING_KEY_ID,
) -> RingVerification:

__all__ = ["RingVerification", "SuccessorStatus", "UnknownPublishingKeyId", "verify_directory_with_ring"]
```

Two private helpers: `_declared_key_id(document_bytes) -> str | None` (rule 1's
read) and `_announced_successor(directory, ring) -> SuccessorStatus | None`
(rule 4).

**Imports:** `json`, `collections.abc.Mapping`, `dataclasses.dataclass`,
`datetime.date`, `typing.Literal`, plus `Directory`,
`DirectoryVerificationError`, `verify_directory` from `.directory` and
`PUBLISHING_KEY_ID`, `PUBLISHING_KEY_RING` from `.publishing_key`. Nothing
else: no `cryptography` import of its own, no logging, no clock.
`directory.py` and `publishing_key.py` are unchanged.

**How the rules are implemented:**

| Rule | Implementation |
|---|---|
| 1 — read the id without trusting the bytes | `_declared_key_id` decodes UTF-8 JSON and returns the id only if the result is a `dict` whose `publishing_key_id` is a `str`. Otherwise it returns `None`, and the function goes on to call `verify_directory(…, publishing_key=ring[current_key_id], …)` so the gate refuses the document in its own words. It raises nothing of its own. The guard against `current_key_id ∉ ring` runs **first, on every call** (judgment call 1, §6). |
| 2 — unknown id refuses before the signature | `raise UnknownPublishingKeyId(f"publishing_key_id {…!r} is not in the shipped key ring {sorted(ring)}; a document may only be believed on the strength of a key that was already in the ring (B-D-14)")`. The message is verbatim from the prompt. `verify_directory` is not called. |
| 3 — verify | The only `verify_directory` call site: `ring[key_id]`. Every exception passes through unchanged. |
| 4 — notices | `successor_in_use = key_id != current_key_id`. `announced_successor` is `None` / `"in_ring"` / `"not_in_ring"` / `"contradicts_ring"`, with keys compared as bytes (judgment call 2, §6). |

**Rule 1 has one call site, not two.** When the id is unreadable, `key_id` is
set to `current_key_id` and control reaches the same `verify_directory` call
as a readable id. The gate cannot return for such a document: step 2 refuses
bytes that are not a JSON object, and step 7 requires the very
`publishing_key_id` that could not be read. So this needs no unreachable
branch and no second copy of a refusal. Even in that impossible case, the
key used is still a key from the ring, so B-D-14's invariant would hold.

**Docstrings:** the module docstring follows `publishing_key.py` (selection
step, "selecting is not trusting", "an announced successor is a notice,
never a key", purity line). Both classes have docstrings, and the function
has Google-style Args/Returns/Raises. The Raises section lists
`UnknownPublishingKeyId` first, then "everything `verify_directory` raises"
once, naming `PublishingKeyNotConfigured`.

### `services/provider_channel/__init__.py` (+14 / −2)

```diff
@@ -11,8 +11,10 @@
 (:mod:`~services.provider_channel.directory`), the publishing key the gate
-verifies against (:mod:`~services.provider_channel.publishing_key`), and the
-seam that turns a confirmation into proposed booking fields
+verifies against (:mod:`~services.provider_channel.publishing_key`), the
+ring-selection step that picks that key by the document's id
+(:mod:`~services.provider_channel.ring`, Stage B), and the seam that turns a
+confirmation into proposed booking fields
 (:mod:`~services.provider_channel.prefill`).
@@ -88,6 +90,12 @@ from services.provider_channel.publishing_key import (
+from services.provider_channel.ring import (
+    RingVerification,
+    SuccessorStatus,
+    UnknownPublishingKeyId,
+    verify_directory_with_ring,
+)
 from services.provider_channel.schemas import (
@@ -145,9 +153,12 @@ __all__ = [
     "PublishingKeyNotConfigured",
+    "RingVerification",
     "SchemaError",
     "SuccessorKey",
+    "SuccessorStatus",
     "UnknownDirectoryFormatVersion",
+    "UnknownPublishingKeyId",
     "UnknownSchemaVersion",
@@ -159,4 +170,5 @@ __all__ = [
     "verify_directory",
+    "verify_directory_with_ring",
 ]
```

The docstring clause is the one the prompt specified, inserted after the
`publishing_key` clause. The surrounding lines were re-wrapped at the file's
existing width. No other words in the docstring changed.

### `tests/services/provider_channel/test_ring.py` (new, 338 lines)

**Reuse by import, not copy.** `tests/__init__.py`,
`tests/services/__init__.py` and `tests/services/provider_channel/__init__.py`
all exist, so the tests import
`from tests.services.provider_channel.test_directory import _PRIVATE_BYTES, _PUBLIC_BYTES, _REAL_KEY_NOW, _document, _load_fixture`
directly. None of the imported names start with `test`, so nothing is
collected twice. A second throwaway key pair (`_OTHER_KEY`) is generated in
the module in the `_SIGNING_KEY` style. The package `conftest.py` already
shadows the autouse DB fixture, so the module is DB-free.

**Dates come from the document** (record §6, Stage B additions):
`_judging_day(document)` is the document's own `issued_at + 1 day`, following
the `_REAL_KEY_NOW` convention. `_CURRENT_ID` is read from `_document()`
rather than restated. The fixture test uses `_REAL_KEY_NOW` as the prompt
specified.

| # | Test | Items |
|---|---|---|
| 1 | `test_real_fixture_verifies_through_the_ring` | 1 |
| 2 | `test_unknown_publishing_key_id_is_refused_before_the_signature` | 1 (genuine and garbage signature, in a loop) |
| 3 | `test_ring_selects_the_key_by_id_and_refuses_the_wrong_key` | 1 |
| 4 | `test_successor_in_use_is_reported_not_refused` | 1 |
| 5 | `test_announced_successor_statuses` | 5: `in-ring`, `not-in-ring`, `contradicts-ring`, `no-announcement`, **+ `in-ring-upper-case-hex`** |
| 6 | `test_unreadable_document_is_refused_by_the_reference_implementation` | 4: `not-json`, `json-list`, `object-without-id`, **+ `object-with-non-string-id`** |
| 7 | `test_placeholder_in_ring_still_fails_closed` | 1 |
| 8 | `test_misconfigured_current_key_id_is_an_unknown_id_not_a_key_error` | 1 (**+ a readable-document assertion**) |
| 9 | `test_ring_module_reads_no_clock` | 1 |

**9 functions, 16 collected items.** The prompt's specification alone would
give 14: seven unparametrised functions, plus 4 items for test 5 and 3 for
test 6. The two extra params, marked in bold, account for the other two:

- **`in-ring-upper-case-hex`** pins judgment call 2.
- **`object-with-non-string-id`** covers the "or not a `str`" half of rule 1,
  which the three specified inputs don't reach.

**Test 6 asserts more than the prompt asked.** Each input is signed
**genuinely** with the ring's current key, so the two object cases reach the
gate's step 7 instead of stopping at the signature. The test asserts the
exact refusal type per input: `InvalidDirectorySignature` for `not-json` and
`json-list`, `DirectoryShapeError` for both objects. This comes on top of the
required "subclass of `DirectoryVerificationError`, not
`UnknownPublishingKeyId`". The effect is to show which gate step refuses each
input.

**Other details:**

- **Test 2:** the genuine signature is made by a key that *is* in the ring,
  under a different id. The refusal therefore can't be a signature failure
  in disguise.
- **Test 5:** the `not-in-ring` case names the successor's real key under an
  unknown id. The ring is keyed by id, so this is still `not_in_ring`.
- **Test 9:** it uses the concatenated literals as instructed. The contract
  scan covers `services/provider_channel/` only (`_PACKAGE_ROOT`), so the
  concatenation is belt-and-braces.

---

## 4. Gates

| Run | Result |
|---|---|
| `pytest tests/services/provider_channel -q` (baseline, before edits) | **85 passed** in 2.50s |
| `pytest tests/services/provider_channel/test_ring.py -v` | **16 passed** in 0.08s |
| `pytest tests/services/provider_channel -q` (after) | **101 passed** in 1.62s (85 + 16) |
| `pytest tests/services/provider_channel/test_contract.py -q` | **22 passed** in 1.53s, unchanged |
| `ruff check services/provider_channel tests/services/provider_channel` | All checks passed! |
| `ruff format --check` (same paths) | 13 files already formatted. The new files were clean on first write; `ruff format` was never run |
| `pyright services/provider_channel` | 0 errors, 0 warnings, 0 informations |
| `pyright --pythonpath ./.venv/bin/python services/provider_channel tests/services/provider_channel/test_ring.py` | 0 errors, 0 warnings, 0 informations |
| `pyright`, CI's island config (`services/overlay`, `services/market_data`) | 2 errors, pre-existing and not touched here. With `--pythonpath ./.venv/bin/python`: **0 errors**. See §5 (c) |
| Mutation check (scratch plugins, `ring.py` untouched) | 4 of 4 mutants killed, see below |
| **Full suite** `pytest -q` | **Not run to completion.** Stopped at 17% (887 executed, 0 F/E) on operator instruction; deferred to an evening run, see below |
| `git status --porcelain` | see below |

### Mutation check

To show that the tests pin what they claim, I ran `test_ring.py` against four
deliberately wrong implementations. Each was patched in by a scratch pytest
plugin (`-p`) outside the repository. `ring.py` was never edited, because the
full suite running alongside reads it.

| Mutant | Tests that failed |
|---|---|
| Successor compared by `.hex()` text | `announced_successor_statuses[in-ring-upper-case-hex]` |
| "Try every ring key", ignoring the id | #2, #3, #6 `[object-without-id]`, #6 `[object-with-non-string-id]`, #8 |
| Check the signature against the current key before the id | #2, #4 |
| Current-key guard scoped to the unreadable path only (the prompt's literal reading) | #8, via the extra readable-document assertion |

### Full suite: NOT RUN TO COMPLETION (deferred)

The prompt requires the full serial run and its final line. **It was started
and then deliberately stopped** on the operator's instruction: the session
ended around 16:00 CEST, and the run needed another two and a half hours or
so. The operator will run it separately this evening. This is the same
situation as PB-1a, recorded here rather than quietly left out. Under the
prompt's own terms it is a report finding, not a STOP.

| | |
|---|---|
| Command | `./.venv/bin/python -m pytest -q > /tmp/pf-full-suite.log 2>&1`, serial, dev Postgres up and healthy (`pg_isready`) |
| Started | 2026-09-10 15:07:57 CEST |
| Stopped | ≈ 15:15:25 CEST (about 7m30s), via task stop. The pytest process was confirmed gone afterwards |
| Reached | **17%, 887 tests executed**: 885 passed, 1 skipped (`s`), 1 xfail (`x`) |
| Failures/errors in that window | **0** (no `F` or `E` in the progress stream) |
| Final line | none, because the run never reached a summary line |
| Board expectation for the evening run | 4,904 (track close) + 4 (SB-1) + 16 (this strand) = **4,924 passed** |
| Leftovers | none. After the stop the cluster held only `portfoliflow_dev`, `postgres`, `template0`, `template1`, so no migration-guard scratch database was stranded |

Last 20 lines of `/tmp/pf-full-suite.log`, as the prompt requires for a run
that didn't complete. The whole log is 13 lines of progress, so this is all
of it:

```
........................................................................ [  1%]
........................................................................ [  2%]
........................................................................ [  4%]
........................................................................ [  5%]
...............s........................................................ [  7%]
........................................................................ [  8%]
........................................................................ [ 10%]
...................x.................................................... [ 11%]
........................................................................ [ 13%]
........................................................................ [ 14%]
........................................................................ [ 16%]
........................................................................ [ 17%]
.......................
```

**What the partial run does and doesn't show.** pytest collects
alphabetically, so the 887 tests that ran are the DB-free front of the suite.
As in PB-1a, the slow DB-backed suites weren't reached. That includes
`tests/services/provider_channel/` itself, but that package was run in full
and on its own (101 passed, contract 22) before and after the one
comment-only edit to `ring.py`, made while the suite was still in its first
13%. **Collection succeeded for the entire suite**: pytest printed progress
percentages, so every test module imported cleanly with `ring.py` and the new
`__init__` re-exports in place. That rules out the one repository-wide
failure this strand could plausibly cause, an import error in the package.

**Blast radius.** `ring.py` is new, and nothing outside this package and its
tests imports it. The only change to an existing module is additive
re-exports in `services/provider_channel/__init__.py`. None of
`directory.py`, `publishing_key.py`, the fixtures, or any consumer elsewhere
was touched.

### Working tree

`git status --porcelain`:

```
 M services/provider_channel/__init__.py
?? docs/reports/PB-1b-report.md
?? services/provider_channel/ring.py
?? tests/services/provider_channel/test_ring.py
```

Exactly the four paths the prompt allows (`-uall` shows the same four, with
nothing collapsed). No `git add`, `commit`, `branch`, `checkout`, `switch` or
`push` was run.

---

## 5. Docs debt for the next docs prompt (PB-D3)

Listed here, not fixed.

**(a) D-SB2-16's reminder window is off by one, and not only in wording.**
The record's D-SB2-16 bullet says the reminder window "opens on day 60,
which is exactly the B-D-16 re-sign deadline". With `remaining < 30`, which
PB-2c kept, it opens on **day 61**. On the live calendar (B-D-24:
`issued_at 2026-09-10`, `valid_until 2026-12-09`):

| Day | Date | `remaining` | reminder (`< 30`) |
|---|---|---|---|
| 59 | 2026-11-08 | 31 | no |
| 60 | 2026-11-09 (**re-sign deadline**, B-D-24) | 30 | **no** |
| 61 | 2026-11-10 | 29 | yes |

So under `--strict`, cron first fails the run the day **after** the re-sign
deadline, not on it. Either the record changes to "day 61, the day after the
deadline", or the tool changes to `<= 30` so it matches the record. That
choice is Mission Control's. I computed the day numbers above from the
B-D-24 dates; I didn't read the infra tool's code, which lives in the other
repository.

**(b) SB-3a landed. Coordinates and progress to update:**

- §7: "Provider-channel suite 85 tests" → **101**. `test_contract.py` stays
  **22**, and "unchanged by Stage B" still holds.
- §7: add the new module `services/provider_channel/ring.py` with
  `verify_directory_with_ring(document_bytes, signature, *, now, ring=PUBLISHING_KEY_RING, current_key_id=PUBLISHING_KEY_ID) -> RingVerification`,
  the new refusal `UnknownPublishingKeyId(DirectoryVerificationError)`, and
  `SuccessorStatus`. `verify_directory` remains unchanged, as §7 already
  says.
- B-D-14: a delivery addendum like SB-1's ("delivered by SB-3a: …"),
  recording that an unknown id refuses before the signature and that
  successor announcements come back as `announced_successor`
  (`in_ring`/`not_in_ring`/`contradicts_ring`). The provenance-line notice
  itself is SB-3b's.
- Roadmap #067: SB-3a done, SB-3b next (gated on publication, B-D-24).
- Full-suite count: **still open**. Two strands in a row (SB-1, SB-3a) have
  stopped the full run for time. PB-D3 should record the evening run's line
  as the post-SB-3a baseline (OP-B-10; expected 4,924) only once it exists,
  and should not carry 4,924 forward as observed.

**(c) Other things I noticed:**

- **The "pre-existing pyright errors" are an interpreter-resolution
  artifact.** Bare `pyright` (the CI command) reports
  `Import "pandas" could not be resolved` at `services/overlay/pipeline.py:48`
  and `services/overlay/steps.py:44` on this machine. With
  `--pythonpath ./.venv/bin/python` the island run is 0 errors. The same
  thing made a bare `pyright tests/…/test_ring.py` fail to resolve `pytest`.
  PB-1a's report attributed both errors to `steps.py`; one is in
  `pipeline.py`. Nothing needs fixing in code. The finding is that local
  gate tables should run pyright against the venv interpreter.
- **The `test_directory.py` docstring still defers a question.** The
  docstring of `test_ring_keys_are_valid_ed25519_public_keys` says the
  overlap rule around a successor's `valid_from` "is SB-3's question".
  SB-3a's answer, by the prompt's design, is that there is **no client-side
  time gate**: any key in the ring verifies, whatever `valid_from` says. See
  OQ-3. If that is confirmed, the docstring needs a one-line update. It's a
  test file, so the edit belongs to a build prompt, not PB-D3.
- **The package docstring is still Stage A's.** It opens "The
  provider-channel contract — ADR-0129 Stage A" and "**Contract only: no
  service is built here**". The prompt restricted this strand to one clause.
  Once SB-3b lands, that framing will need a Stage-B revision (build
  prompt).

---

## 6. Open questions

**OQ-1 — `current_key_id ∉ ring` is checked on every call, not only on the
unreadable path.** The prompt puts the guard inside rule 1, where
`ring[current_key_id]` would otherwise raise `KeyError`. Read literally,
though, a misconfigured caller with a *readable* document would get
`successor_in_use=True` on **every** document: a false rotation notice
instead of an error. I made the guard unconditional and run it first. Test 8
pins this with the published fixture and `current_key_id="nope"`. The
literal scoping survives every other test (see the mutation table), so
reverting takes one line in `ring.py` and the removal of one assertion in
test 8. A related point: the misconfiguration raises `UnknownPublishingKeyId`
as specified, but that class's specified docstring ("The document names a
publishing key id…") describes only the document case. The two messages
differ, but a separate exception class may read better. That's your call.

**OQ-2 — keys are compared as bytes, not by `.hex()` text.** The prompt says
`"in_ring"` when the ring key's `.hex()` equals `successor.public_key`. But
the v1 shape reader (`_shape_hex_key`) accepts hex in either case, and
`.hex()` always returns lowercase. An announcement written in upper-case hex
for the *same* key would therefore come back as `"contradicts_ring"`, a false
alarm. `_announced_successor` compares `ring_key == bytes.fromhex(…)`
instead. `bytes.fromhex` can't fail there, because the shape reader has
already checked the value. The `in-ring-upper-case-hex` param pins this. The
first publication uses lowercase, so nothing differs today.

**OQ-3 — no `valid_from` gate on the client.** A document signed by the
successor verifies as soon as it arrives, even before the successor's
`valid_from`. It is reported as `successor_in_use=True` and not refused.
That follows B-D-14 as written: only ring membership matters, and "switch
signing no earlier than one validity window later" is a rule for the
publisher's ceremony, not a client check. Please confirm this is intended.
If it is, see §5 (c) for the `test_directory.py` docstring that still
defers the question.

**OQ-4 — where the SB-3b fetch client can live (for planning).**
`test_contract.py` C-2 forbids `httpx` in the import graph of
`services.provider_channel` (`_FORBIDDEN_MODULE_PREFIXES`), and B-D-21 says
C-2 stays green through B-1. So the fetch client can't sit inside this
package. It has to live beside it and call `verify_directory_with_ring`
from there. This is probably already assumed; I'm writing it down so SB-3b's
verify-first can check it.
