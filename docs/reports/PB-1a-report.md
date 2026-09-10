# PB-1a — Provider Channel Stage B · SB-1 report

**Strand:** the real publishing key lands (ring, tripwire flip, real-key test)
**Prompt:** PB-1a v3 (v2 + Addendum A folded in), issued 2026-09-10
**Repository:** PortfoliFLOW (AGPL), `/home/soenke/Code/PortfoliFLOW/PortfoliFLOW`
**Run:** 2026-09-10 · no STOP condition triggered · all deliverables written ·
**full suite stopped at 19% on operator instruction — see §4**
**Git:** no operations performed. Read-only git only; the working tree carries
the changes, the commit is the operator's.
**Record:** B-D-5 (§3.3), B-D-14, B-D-19, B-D-20, B-D-23; OP-30.

---

## 1. OPERATOR ACTION REQUIRED

No private key was touched in this strand. What follows is a commit proposal;
publication happens after the commit, by you, from the infra repo.

**Files to review and commit (five, plus this report):**

| File | State |
|---|---|
| `services/provider_channel/publishing_key.py` | modified — the real ring |
| `services/provider_channel/__init__.py` | modified — three re-exports |
| `tests/services/provider_channel/test_directory.py` | modified — tripwire flipped, three tests added |
| `tests/services/provider_channel/fixtures/directory-1.json` | new (untracked) — operator-supplied |
| `tests/services/provider_channel/fixtures/directory-1.sig` | new (untracked) — operator-supplied |
| `docs/reports/PB-1a-report.md` | new (untracked) — this report |

Suggested message:

```
feat(provider-channel): land the real publishing key ring, flip the OP-30 tripwire (SB-1)
```

**Then, in the infra repo** (`/home/soenke/Code/pinkernelle-infrastructure`),
only after the commit above exists here:

```sh
cd /home/soenke/Code/pinkernelle-infrastructure
./deploy.sh --dry-run portfoliflow && ./deploy.sh portfoliflow
./deploy.sh caddy      # then run the two printed sudo commands on the host
PY=/home/soenke/Code/PortfoliFLOW/PortfoliFLOW/.venv/bin/python
$PY tools/directory_tool.py verify --url https://portfoliflow.com/directory/v1/ \
    --public-key de7cb38995756192201ed32b568f9c5115b78dbb4127f667611ee8eb1812ba35
```

Then the clean-checkout verification: clone the committed AGPL tree fresh and
run `pytest tests/services/provider_channel -q`, so the key that verifies the
published document is proved to be the key that is *committed*, not the one in
this working tree.

**Open step — the full suite.** It was started and stopped at 19% on your
instruction (see §4). It is not evidence of a problem — 988 tests had run with
zero failures — but the gate is unrun, so please run `pytest -q` before or
alongside the commit:

```sh
cd /home/soenke/Code/PortfoliFLOW/PortfoliFLOW
podman start portfoliflow-postgres      # already up as of this run
nohup ./.venv/bin/python -m pytest -q > /tmp/pf-full-suite.log 2>&1 &
```

Expect ≈3h and a count to compare against 4,904 at track close.

**One correction to carry forward.** The infra repo's `directory/README.md` and
the PB-2a report both describe the published `.json`; the PB-1a v3 prompt and
Addendum A state its size as **1,181 bytes**. It is **1,180**. See §2 check 5 —
the SHA-256 matches exactly, so the fixture is right and the prose is wrong.
Worth fixing in the sheet before it is quoted a third time.

---

## 2. Verify-first results

| # | Check | Result |
|---|---|---|
| 1 | `publishing_key.py` surface | **PASS** — public names were exactly `PUBLISHING_KEY`, `PUBLISHING_KEY_ID`, `PUBLISHING_KEY_PLACEHOLDER`, `is_placeholder`; `PUBLISHING_KEY_ID == 'portfoliflow-publishing-key-placeholder'`; `PUBLISHING_KEY is PUBLISHING_KEY_PLACEHOLDER` → `True` |
| 2 | Tripwire tests and helpers present | **PASS** — both tests found (`:357`, `:375`); `_PRIVATE_BYTES` `:46`, `_PUBLIC_BYTES` `:47`, `_document` `:77`, `_NOW` `:53`. **`_NOW` definition line, verbatim:** `_NOW: Final[date] = date(2026, 9, 7)` — as expected, and before the fixture's `issued_at 2026-09-10`, so B.3 uses `_REAL_KEY_NOW` instead |
| 3 | `directory.py` exports | **PASS** — all nine names in `__all__`. **`verify_directory`'s use of `publishing_key`:** the module's only import from it is `from services.provider_channel.publishing_key import is_placeholder` (`directory.py:47`) — the key itself is passed by the caller, as expected |
| 4 | `__init__.py` re-exports the four names | **PASS** — and **no test under `tests/services/provider_channel/` references `__all__` at all**, so nothing pinned its length. No STOP |
| 5 | Fixture present and verifiable | **PASS** — output below |
| 6 | `git status --short` | **PASS** — exactly the two fixture files untracked, nothing else (see §5) |

### Check 5 — printed output

```
1 portfoliflow-2026-09 SuccessorKey(publishing_key_id='portfoliflow-2027-01', public_key='095cb28132333f9187bd472ce3aff39f5bf66b9a14b49a140e5d252417e37670', valid_from=datetime.date(2027, 1, 1)) ['test-broker-01', 'test-secondary-01']
```

Matches the expected line. The asserts inside the block all held: SHA-256
`c1383c4f…d4ec83`, `.sig` exactly 129 bytes ending in one `\n`, and the
document verified against `de7cb389…12ba35` at `issued_at + 1 day`.

**Deviation from the sheet, not a failure:** `len(payload)` is **1,180**, not
the stated 1,181. The prompt's executable check asserts the SHA-256 and that
passed, so the bytes are exactly the published ones; only the prose count is
off by one. Proceeded — a STOP here would have been a STOP on a typo.

`git status --short` reports `?? tests/services/provider_channel/fixtures/` —
git collapses a wholly-untracked directory to one line. `git status --short -uall`
expands it to the two files the check names, and shows nothing else.

---

## 3. Diff summary

### `services/provider_channel/publishing_key.py` (+76 / −22 lines net of the rewrite)

- **Module docstring** rewritten: the "Stage A ships a placeholder…" paragraph
  is replaced by two — the real key landed 2026-09-10 (SB-1), minted offline
  with its successor per B-D-5 §3.3, with the placeholder value retained *only*
  as the fail-closed sentinel; and rotation is a code release (B-D-14), a
  `successor_key` announcement being a notice rather than a key the client
  begins to trust.
- **`PUBLISHING_KEY` / `PUBLISHING_KEY_ID`** replaced by the real key
  (`de7cb389…12ba35`) and `portfoliflow-2026-09`, exactly as the prompt states
  them.
- **Added** `SUCCESSOR_KEY` (`095cb281…7670`), `SUCCESSOR_KEY_ID`
  (`portfoliflow-2027-01`), and `PUBLISHING_KEY_RING` as a
  `Final[Mapping[str, bytes]]` `MappingProxyType` over the two.
- **Imports added:** `from collections.abc import Mapping`,
  `from types import MappingProxyType`.
- **`PUBLISHING_KEY_PLACEHOLDER` value and `is_placeholder` are unchanged.**
- `__all__` extended with the three new names, alphabetical.

### `services/provider_channel/__init__.py` (+6)

The three new names added to the `publishing_key` import block and to
`__all__`, in the file's existing alphabetical order (`PUBLISHING_KEY_RING`
after `PUBLISHING_KEY_PLACEHOLDER`; `SUCCESSOR_KEY`, `SUCCESSOR_KEY_ID`
between `SIGNATURE_SCHEME_ED25519` and `SUPPORTED_ENCRYPTION_KEY_TYPES`).

### `tests/services/provider_channel/test_directory.py` (+143 / −14)

- **Imports:** `re`, `pathlib.Path`, `Ed25519PublicKey`, and the four further
  `publishing_key` names.
- **Module constants:** `_FIXTURES` (`Path(__file__).parent / "fixtures"`) and
  `_REAL_KEY_NOW = date(2026, 9, 11)`, documented as `issued_at + 1 day` and as
  *not* `_NOW` and never `date.today()`.
- **`_load_fixture() -> tuple[bytes, bytes]`** added beside the other module
  helpers: reads the document as bytes, requires the single trailing `\n` and
  128 hex characters, `bytes.fromhex`. No `.strip()`.
- **`test_placeholder_publishing_key_fails_closed` → `test_shipped_publishing_key_is_real_and_the_placeholder_still_fails_closed`**,
  asserting all six items the prompt lists (real key, lengths, distinctness,
  both ids against `^portfoliflow-\d{4}-\d{2}$`, ring equality, ring
  immutability via `pytest.raises(TypeError)`, and the placeholder guard).
- **`test_placeholder_is_refused_before_the_document_is_read`** kept, now
  passing `publishing_key=PUBLISHING_KEY_PLACEHOLDER` explicitly.
- **Three new tests** added under a new `SB-1` section header:
  `test_real_key_verifies_the_first_published_directory`,
  `test_real_key_refuses_a_document_signed_by_another_key`,
  `test_ring_keys_are_valid_ed25519_public_keys` (parametrised over the ring's
  two ids).
- The section header `D-10: the placeholder tripwire` became
  `D-10 / OP-30: the shipped key ring, and the placeholder as sentinel`.

**Other placeholder references found by the required search:** none expecting
the placeholder. The string `portfoliflow-publishing-key-placeholder` does not
occur anywhere under `tests/`. Every remaining `PUBLISHING_KEY` reference in the
module is in code written or rewritten by this prompt.

### Judgment calls, both documentation-only

1. **The `#:` comment on `PUBLISHING_KEY_PLACEHOLDER`** said the real key
   "lands here as a one-line change. Until then verification MUST fail closed".
   In the very commit where the key lands, that sentence is false. Rewrote it
   to say the constant is no longer the shipped key and is kept as the
   fail-closed sentinel. **Value and name unchanged**, so "keep
   `PUBLISHING_KEY_PLACEHOLDER` and `is_placeholder` unchanged" holds in
   substance; `is_placeholder` including its docstring is byte-identical.
2. **The test module docstring** said "no key material is committed". After
   this strand the module reads a committed public key and a committed
   signature, so it now says the ad-hoc pair is still per-run, the shipped
   *public* key is exercised against the fixture, and the production *private*
   key still never lives in this repository.

Neither is a `docs/` edit; §C is respected.

---

## 4. Test results

| Run | Result |
|---|---|
| `pytest tests/services/provider_channel -q` (baseline, before edits) | **81 passed** in 4.49s |
| `pytest tests/services/provider_channel -q` (after) | **85 passed** in 3.49s |
| `pytest tests/services/provider_channel/test_contract.py -q` | **22 passed** in 3.30s — green, unchanged |
| `ruff check` (both trees) | All checks passed |
| `ruff format` | Reformatted `test_directory.py` once, then clean; suite re-run green afterwards |
| Full suite | see below |

**The count: 85, not 84.** The prompt predicted "81 + 3 new − 0 removed".
Three test *functions* were added, but
`test_ring_keys_are_valid_ed25519_public_keys` is parametrised over the ring's
two ids and therefore collects as **two** items. The replaced tripwire is a
rename, so it neither adds nor removes. 81 + 1 + 1 + 2 = **85**.

**Pre-existing, out of scope:** `pyright` reports 2 errors in
`services/overlay/steps.py` (`Import "pandas" could not be resolved`). The
typing island is `services/overlay` + `services/market_data` (`pyproject.toml`
`[tool.pyright]`); this strand touched neither, and the finding reproduces
independently of these changes.

### Full suite — NOT RUN TO COMPLETION

The prompt requires a full serial run at the end. **It was started and then
deliberately stopped**, on the operator's instruction, because the run needed a
further 1–2.5 hours and the operator was time-restricted. Recorded here rather
than quietly omitted.

| | |
|---|---|
| Command | `pytest -q` (serial, dev Postgres up and healthy) |
| Started | 07:30:21, stopped 07:38 (7m40s) |
| Reached | **19% — 988 tests executed** |
| Failures/errors in that window | **0** (no `F` or `E` in the progress stream) |
| Reason for stopping | operator time constraint; not a failure and not a hang |
| Comparison against 4,904 | **not available** — the run did not reach a summary line |

**The full gate is therefore an open pre-commit step for the operator.** Note
that pytest collects alphabetically, so the 988 tests that did run are the
DB-free front of the suite; the slow DB-backed `tests/repositories/` and
`tests/web/` suites were never reached.

**Why the risk of stopping is low, stated so the operator can weigh it.** A
repo-wide search for `PUBLISHING_KEY`, `PUBLISHING_KEY_RING`, `SUCCESSOR_KEY`
and `is_placeholder` across every `*.py` outside `.venv` returns **no consumer
anywhere in `core/`, `modules/`, `web/`, `bot/` or `cli/`**. The only
non-test consumer of this module in the entire repository is
`services/provider_channel/directory.py:47`, which imports `is_placeholder`
alone — and `is_placeholder`, including its docstring, is byte-identical to
before. The blast radius of this change is `services/provider_channel/` plus
its own test package, both of which are fully green (85 + 22).

Two known pre-existing hazards, neither triggered here and both worth
remembering when the full gate is run:

- the AI-service singleton pollution between `tests/web/` and
  `tests/assistants/` in combined runs (a recorded flake, unrelated to this
  strand);
- the `pyright` island findings in `services/overlay/steps.py` noted above.

---

## 5. Working tree

`git status --short -uall`:

```
 M services/provider_channel/__init__.py
 M services/provider_channel/publishing_key.py
 M tests/services/provider_channel/test_directory.py
?? tests/services/provider_channel/fixtures/directory-1.json
?? tests/services/provider_channel/fixtures/directory-1.sig
?? docs/reports/PB-1a-report.md
```

`git diff --stat`:

```
 services/provider_channel/__init__.py             |   6 +
 services/provider_channel/publishing_key.py       |  76 ++++++++---
 tests/services/provider_channel/test_directory.py | 157 ++++++++++++++++++++--
 3 files changed, 203 insertions(+), 36 deletions(-)
```

No `git add`, `commit`, `branch`, `checkout`, `switch` or `push` was run.

---

## 6. Notes for Mission Control

- **OQ-1 from the PB-2a report is now answered by the working tree.** It asked
  whether SB-1 should land a mapping ring or keep the successor out of the AGPL
  repo until rotation. PB-1a v3 §A answers "ring, with the successor", and the
  ring here is `PUBLISHING_KEY_RING: Mapping[str, bytes]` — note the infra
  tool's mirror `PUBLISHING_KEYS` is `Mapping[str, str]` (hex). The two agree on
  content; they differ in representation and in name. If the intent is that the
  tool's mirror cross-checks this ring (D-SB2-7), the follow-up strand should
  decide which spelling is canonical rather than let a future reader assume the
  names match.
- **OQ-3 is answered too:** both keys were minted before first signing — the
  fixture announces `portfoliflow-2027-01` / `095cb281…`, this ring carries the
  same bytes, and `test_real_key_verifies_the_first_published_directory` now
  pins the two against each other.
- **`_REAL_KEY_NOW` is a fixed date, deliberately.** It is `issued_at + 1 day`,
  so the test does not expire when the document does. When the directory is
  re-signed (B-D-16, every 60 days), the fixture and this constant move
  together or the test goes red — which is the intended coupling, but worth
  knowing before the first re-sign.
