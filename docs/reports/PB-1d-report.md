# PB-1d — Provider Channel Stage B · SB-4 report

**Strand:** annex ADR-0131 and the `provider_channel.enabled` taxonomy
declaration
**Prompt:** PB-1d, issued 2026-09-11 (Mission Control Stage B, chat 3)
**Repository:** PortfoliFLOW (AGPL), `/home/soenke/Code/PortfoliFLOW/PortfoliFLOW`
**Run:** 2026-09-11 · no STOP condition triggered · all deliverables written
**Git:** no operations performed. Read-only git only (`status`, `log`, `diff`,
`show`). The working tree carries the changes; the commit is the operator's.
**Record:** B-D-21, B-D-25 Q6, ADR-0129 §2/§6, ADR-0112 §1/§3.

---

## 1. OPERATOR ACTION REQUIRED

Commit the seven paths:

| File | State |
|---|---|
| `services/credential_vault/taxonomy.py` | modified — one declaration, one docstring sentence |
| `web/routes/provider_credentials.py` | modified — one entry in each of four copy dictionaries |
| `tests/services/credential_vault/test_taxonomy.py` | modified — two tests (TX-04, TX-06) |
| `tests/web/test_provider_credentials.py` | modified — one card test |
| `docs/adr/README.md` | modified — index row, update block, next free number |
| `docs/adr/0131-provider-channel-enabled-in-the-scoped-settings-taxonomy.md` | new (untracked) |
| `docs/reports/PB-1d-report.md` | new (untracked) — this report |

Suggested message:

```
feat(provider-channel): declare provider_channel.enabled in the scoped-settings taxonomy, annex ADR-0131 (SB-4)
```

**Housekeeping: the dev database was emptied.** The `portfoliflow-postgres`
container was `Exited` when this session started, so verify-first check 11
could not pass (§2). I started it with `podman start portfoliflow-postgres`
and left it running. The web suite's `reset_schema` fixture truncates
`tenants`, `users`, `scoped_settings` and related tables, so any local
deployment data in `portfoliflow_dev` is gone. Re-run `portfoliflow bootstrap`
(and re-import) before using the app locally. Stop the container with
`podman stop portfoliflow-postgres` if you don't want it running.

**Four judgment calls to confirm.** None of them changes a name, identifier or
copy string from the prompt. Details in §6.

1. The web test asserts a little more than §2.4 listed.
2. A two-line comment sits above the new `_CONSUMER_STATUS` entry.
3. ADR-0131 records two B-2 notes and one resolver nuance that the prompt's
   section outline did not spell out.
4. ADR-0131 is 165 lines, slightly over the ~120–160 guide.

---

## 2. Verify-first values observed

| # | Check | Expected | Observed |
|---|---|---|---|
| 1 | `git status --porcelain` | empty | empty ✔ |
| 2 | `git log -1 --format=%s` | ends with `(SB-3a)` | `feat(provider-channel): ring-selection wrapper — verify by publishing_key_id, refuse unknown ids, report successor notices (SB-3a)` ✔ |
| 3 | `grep -rc 'provider_channel'` over the five files | all 0 | all five `:0` ✔ |
| 4 | `provider="voice",` in `taxonomy.py` | line 233 | `233` ✔ |
| 5 | the five dictionaries in `provider_credentials.py` | 172, 183, 195, 208, 222 | `_CONSUMER_STATUS` 172 · `_PROVIDER_DESCRIPTIONS` 183 · `_PROVIDER_LABELS` 195 · `_FIELD_LABELS` 208 · `_FIELD_HINTS` 222 ✔ |
| 6 | `^_ENV_CONFIG_FIELDS` in `credential_resolver.py` | line 143 | `143` ✔ |
| 7 | the two voice tests in `test_taxonomy.py` | 204 and 319 | `204` · `319` ✔ |
| 8 | the card-copy test in `test_provider_credentials.py` | line 447 | `447` ✔ |
| 9 | `ls docs/adr \| grep -c '^0131'` | 0 | `0` ✔ |
| 10 | `^\| 0130 \|` and `next free … **0131**` in `docs/adr/README.md` | 197 and 820 | `197` · `820` ✔ |
| 11 | baseline test run | all passed | **First run: `24 skipped, 55 errors`**, because Postgres was down (connection refused on 5432). Not a code mismatch. After `podman start portfoliflow-postgres` (`pg_isready`: accepting connections; `alembic current`: `b034_add_trade_tickets (head)`): **`79 passed in 145.87s`** ✔. Collected: **24** taxonomy + **55** web (the prompt's 24 and 40 defined functions; parametrisation raises the web count to 55) |
| 12 | `ls docs/reports/` | three reports, no `PB-1d-report.md` | `PB-1a-report.md  PB-1b-report.md  PB-D2-report.md` ✔ |

---

## 3. What landed

### `services/credential_vault/taxonomy.py` (+21 / −2)

The declaration is the last entry of `_V1_DECLARATIONS`, right after
`voice_tts`, with the prompt's comment verbatim:

```python
    # Provider channel (ADR-0131, annex amendment to ADR-0112 §3; ADR-0129
    # §6 and the Stage B record B-D-21). One config-only declaration, one
    # field: the tenant's opt-in switch for the provider channel — directory
    # fetches and encrypted ticket exports. No secret: the directory is a
    # public signed document and exports are sealed to the provider's key,
    # so there is nothing to keep. ``env_fallback=False`` on purpose: an
    # environment variable would switch on phone-home behaviour for every
    # tenant of a deployment at once, which ADR-0129 §6 forbids — enabling
    # is a per-tenant owner action in the vault, nowhere else.
    # ``optional=True`` because there is no credential to be missing.
    ProviderDeclaration(
        provider="provider_channel",
        fields=(ProviderField(name="enabled", is_secret=False, scopes=_TENANT),),
        managed_by_matrix=False,
        env_fallback=False,
        optional=True,
    ),
```

The module docstring gains the prompt's sentence at the end of the
ADR-0118/0123 paragraph: "ADR-0131 is the third: a new config-only provider,
``provider_channel``, whose one field is the tenant's opt-in switch for the
provider channel." The −2 is the paragraph's last two lines re-wrapped to fit
it. `PROVIDER_TAXONOMY` now has seven keys, with `provider_channel` last.

### `web/routes/provider_credentials.py` (+16)

Each entry sits between `openrouter` and `telegram`. In `_FIELD_HINTS` that
means directly before `("telegram", "bot_token")`, after the `openfigi` entry
that already sat between the two. The long strings are split with implicit
concatenation to stay under 100 columns. I checked each **joined** value for
equality against the prompt's text in a Python session: all four `True`.

| Dictionary | Key | Value (verbatim) |
|---|---|---|
| `_CONSUMER_STATUS` | `"provider_channel"` | `dormant — nothing reads this switch yet; the provider channel lands in Stage B` |
| `_PROVIDER_DESCRIPTIONS` | `"provider_channel"` | `The provider channel — signed provider directory and encrypted ticket exports. Off by default; this is the on/off switch for this tenant.` |
| `_PROVIDER_LABELS` | `"provider_channel"` | `Provider channel` |
| `_FIELD_HINTS` | `("provider_channel", "enabled")` | `When on, this instance fetches the signed provider directory from portfoliflow.com — the list version only, never a query — and offers encrypted exports of individual tickets. Nothing about holdings, portfolios, users or analytics leaves the instance. When off, nothing is fetched and the Transactions area shows no provider gesture at all.` |

`_FIELD_LABELS["enabled"]` is unchanged (`"Enabled"`), and so is
`_USER_PANEL_EXCLUDED`. No template, CSS or resolver change: the card renders
from the taxonomy on its own.

### Tests (+18 taxonomy, +45 web)

- `test_tx04_provider_channel_declaration_matches_adr_0131`, directly after
  the TX-04 voice test, verbatim from §2.4.
- `test_tx06_provider_channel_has_no_config_env_link`, directly after the
  TX-06 voice test, verbatim from §2.4.
- `test_provider_channel_card_is_dormant_and_says_what_leaves_the_instance`,
  directly after the card-copy test. It uses that test's setup
  (`client_factory("owner")`, `vault_key`, `GET _SECTION_URL`) and pins the
  copy with **literal** strings rather than reading the dictionaries back, so
  a copy drift fails the test. Assertions:
  - `"Provider channel"` is present, and the dormant string appears verbatim.
  - The label and pill are adjacent: `>Provider channel</h4>\s*<span…>{dormant}</span>`.
  - The description appears verbatim.
  - `id="tenant-provider_channel-enabled-hint">` is directly followed by the
    hint's first sentence.
  - That first sentence appears **exactly once** on the page.
  - `<input type="hidden" name="provider" value="provider_channel">` count ≥ 1.
  - The same control as `voice.enabled`: `<input type="text"\s+id="…">`
    matches, and `<input type="password"\s+id="…">` does not. Checked for
    **both** `tenant-voice-enabled` and `tenant-provider_channel-enabled`.

### `docs/adr/0131-provider-channel-enabled-in-the-scoped-settings-taxonomy.md` (new, 165 lines)

The header follows ADR-0123's shape (Status, Date, Deciders, Closes,
Supersedes / amends, Tags). The Decision sectioning follows ADR-0118's.

| Section | Lines |
|---|---|
| Header (title + metadata + rule) | 19 |
| Context | 16 |
| Decision (heading) | 2 |
| §1 One declaration, one field | 24 |
| §2 Policy: `env_fallback=False`, `optional=True` | 15 |
| §3 Resolution and truth rule | 19 |
| §4 Admin card: taxonomy-driven appearance, deliberate copy | 17 |
| §5 When off: absent, not disabled (B-D-25 Q6) | 8 |
| §6 Scope note | 9 |
| Alternatives considered | 8 |
| Non-goals | 6 |
| Consequences | 8 |
| Related ADRs | 8 |
| Revision History (one row) | 5 |

### `docs/adr/README.md` (+23 / −1)

- New row after `| 0130 |` (now line 198): number, linked title, the
  prompt's status text verbatim, `2026-09-11`, and the ADR's tags.
- `**Update (2026-09-11):**` block (five sentences) directly after the
  2026-08-31 block, in its register. It covers the declaration, the one policy
  departure and why, the dormant card, and that nothing reads the switch yet.
- `The next free ADR number is **0131**.` → `**0132**` (now line 841).

---

## 4. Gates

| Run | Result |
|---|---|
| Baseline `pytest test_taxonomy.py test_provider_credentials.py` (before edits, DB up) | **79 passed** in 145.87s (24 + 55) |
| New tests + the two neighbouring card tests (spot run) | **29 passed** in 45.04s |
| **Gate** `pytest tests/services/credential_vault tests/web/test_provider_credentials.py tests/services/provider_channel -q` | **206 passed** in 192.38s (0:03:12), exit 0 |
| — breakdown (collected) | `test_taxonomy.py` **26** (24 + 2) · `test_provider_credentials.py` **56** (55 + 1) · `test_fernet.py` 23 (untouched) · provider-channel **101**, unchanged (`test_contract.py` **22**, unchanged; `test_directory.py` 36, `test_prefill.py` 6, `test_ring.py` 16, `test_schemas.py` 21) · total **206** |
| `ruff check` on the four touched Python files | All checks passed! |
| `ruff format --check` on the same four files | 4 files already formatted (`ruff format` was never run; clean on first write) |
| `pyright --pythonpath ./.venv/bin/python` on the four files | `taxonomy.py`, `provider_credentials.py`, `test_taxonomy.py`: **0 errors**. `test_provider_credentials.py`: **2 errors, both pre-existing**, see below |
| bare `pyright` (CI island config) | 2 errors, pre-existing and untouched: the `pandas` import-resolution artifact in `services/overlay/` that PB-1b §5(c) already recorded |
| `grep -rl provider_channel web/templates` | empty (exit 1) ✔ |
| Hint once per card | asserted in the card test: `body.count(first_sentence) == 1` on the owner's page ✔ |
| `grep -c provider_channel services/investments/credential_resolver.py` | `0`, file untouched ✔ |
| `git status --porcelain` | see below |

**The two pyright errors in the web test are older than this change.** Both
are in the untouched fixtures `fresh_superuser_engine` (line 136) and
`app_engine` (line 147): `DATABASE_URL` / `DATABASE_URL_SUPERUSER` are typed
`str | None`, and `create_async_engine` wants `str | URL`. I extracted the
`HEAD` version with `git show HEAD:tests/web/test_provider_credentials.py`
into the scratchpad and ran pyright on it: the same two errors at the same
two lines. My diff to the file is one hunk, `@@ -502,0 +503,45 @@`. The file
is not in CI's pyright island set.

### Working tree

`git status --porcelain`:

```
 M docs/adr/README.md
 M services/credential_vault/taxonomy.py
 M tests/services/credential_vault/test_taxonomy.py
 M tests/web/test_provider_credentials.py
 M web/routes/provider_credentials.py
?? docs/adr/0131-provider-channel-enabled-in-the-scoped-settings-taxonomy.md
?? docs/reports/PB-1d-report.md
```

These are exactly the seven paths the prompt allows (`-uall` shows the same
two untracked files, nothing collapsed). `git diff --stat` for the tracked
five: 121 insertions, 2 deletions. No `git add`, `commit`, `stash`, `branch`,
`checkout`, `switch` or `push` was run.

---

## 5. Docs debt for PB-D3

Added to the list already open from PB-1b (items (a) and (b) there). Listed
here, not fixed.

**(c) Record §7 coordinates.** The next free ADR is **0132**.
`PROVIDER_TAXONOMY` has `provider_channel` (seven declarations, last entry).
The credential-vault taxonomy suite is 26 tests, the provider-credentials web
suite 56. The provider-channel suite stays at **101**, and `test_contract.py`
at 22.

**(d) B-D-25** is to be recorded with the operator's answer line and the two
clarifications (Mission Control has them). ADR-0131 §5 and the README row
already cite B-D-25 by id, and §5 says in one clause that PB-D3 records it
after the ADR.

**(e) #067 progress:** SB-4 shipped.

**(f) B-D-21's literal wording.** The record writes
`ProviderField(name="provider_channel", is_secret=False, scopes=_TENANT)`.
What landed, as the prompt directed, is the *provider* `provider_channel` with
one field `enabled`. ADR-0131 §1 records that reading. A one-line delivery
addendum on B-D-21 ("delivered by SB-4 as provider `provider_channel`, field
`enabled`; `env_fallback=False`, `optional=True`, ADR-0131") would keep the
record from reading as if a field were named `provider_channel`.

**(g) OP-B-5 is not in this repository.** `grep -rn 'OP-B-5' docs/` finds
nothing, so the ADR header cites it by id only, as the prompt worded it. If
PB-D3 records OP-B-5 in the Stage B record, that citation resolves.

**Build-prompt items (not PB-D3):**

- The comment directly above `_CONSUMER_STATUS` in `provider_credentials.py`
  was already stale before this strand. It says "All three consumers have
  landed", but the dictionary has held six live entries since the voice
  cards. The module docstring's "Consumer honesty" bullet is current: it
  names the three F5 consumers and the voice cards. Neither text mentions a
  *dormant* entry, although the docstring's rule ("never *look* consumed when
  it is not") is exactly what the dormant pill applies. The SB-6 build prompt
  that flips the pill to live is the natural place to refresh the comment.
- `CLAUDE.md`'s "Implemented services" table lists only ADR-0112 under
  "Scoped settings and credentials". ADR-0118, ADR-0123 and now ADR-0131 are
  its annexes. This is pre-existing and optional.

---

## 6. Open questions

**OQ-1 — Extra assertions in the web test.** Beyond §2.4's list I added three
checks:

- The label-to-pill adjacency regex, so the dormant pill is shown to belong
  to *this* card.
- `body.count(first_sentence) == 1`, which also serves as the §3 gate "the
  hint reaches the rendered page only once per card".
- The text-input check run against `voice.enabled` as well, so "the same
  control" is a comparison, not a check on one row alone.

Each can be removed on its own without affecting the rest.

**OQ-2 — The comment above `_CONSUMER_STATUS["provider_channel"]`.** It reads
"ADR-0131 §4: dormant until a reader lands (SB-6) — the pill is a truth, not
a promise." The prompt specified entries, not comments. I added it so the one
non-live pill in the dictionary carries its reason next to it. It is two
lines and can be dropped.

**OQ-3 — Three statements in ADR-0131 the outline didn't spell out.**

- §2 notes that the policy flags are per declaration, so the B-2 relay
  credential **inherits `env_fallback=False` unless its own ADR revisits it**,
  and that B-2 revisits `optional=True`. These are notes, not decisions, but
  they do tell B-2 where its defaults come from.
- §3 records that the config chain's environment link is driven by
  `_ENV_CONFIG_FIELDS` **alone**. The declaration's `env_fallback` flag only
  governs the credential chain (`resolve()`), as
  `CredentialResolver._config_from_source` shows. For this switch, then, the
  enforcing fact is the absence pinned by TX-06, plus the consumer's
  `scopes=("tenant",)`. I stated this so that no one reads `env_fallback=False`
  as the thing that closes the env link.
- §3 also states that a row the owner has **disabled** on the card reads as
  off (the resolver treats a disabled row as absent), even if its value is
  `true`.

Please confirm all three are wanted in the ADR.

**OQ-4 — The ADR is 165 lines against a ~120–160 guide.** Nineteen lines are
the header, whose Closes and Supersedes clauses the prompt specified in full.
I cut the body twice, from 202 lines: I dropped a field table that repeated
the code block, and quoted only the hint verbatim in §4 (the pill and
description are cited, not repeated). Further cuts would start removing
content the outline asks for.

**OQ-5 — For SB-6: how the owner turns the switch on.** The row is the generic
config control, a free-text input exactly like `voice.enabled`. The owner
types `true` and saves; Remove or Disable turns it off. The hint says what
"on" means, but not how to set it. That matches the `voice.enabled` precedent
and is fine while the card is dormant. When SB-6 makes the switch live, a real
toggle, or a hint clause such as "Enter `true` to turn it on", may be worth
doing. ADR-0131 lists a dedicated toggle as a non-goal of this strand, not as
a rejected option.
