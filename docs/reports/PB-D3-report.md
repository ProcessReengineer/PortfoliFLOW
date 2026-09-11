# PB-D3 — Provider Channel Stage B · record update report

**Strand:** docs only — B-D-25, B-D-26, SB-3a/SB-4 addenda, D-SB2-16 correction, §6 lesson, §7 coordinates, roadmap #067 progress and change log
**Prompt:** PB-D3, issued 2026-09-11 by Mission Control Stage B, chat 3
**Repository:** PortfoliFLOW (AGPL), `/home/soenke/Code/PortfoliFLOW/PortfoliFLOW`
**Run:** 2026-09-11 · all edits (a)–(g) / (a)–(c) applied · every verify-first
check matched, text and line number, with no ±3 tolerance used · one incomplete
parenthetical in check 4 (§2 note)
**Git:** no operations performed. Read-only git only (`status`, `diff`, `log`);
the working tree carries the changes, and the commit is the operator's.
**Record:** B-D-14, B-D-21 (addenda); B-D-25, B-D-26 (new); D-SB2-16
(correction); §6, §7. Also closes the PB-D2 change-log row that was never
applied.

---

## 1. OPERATOR ACTION REQUIRED

Review and commit the three paths:

```sh
git add docs/concepts/provider-channel-stage-b-decisions.md \
        docs/roadmap.md \
        docs/reports/PB-D3-report.md
git commit -m "docs(provider-channel): record B-D-25/B-D-26, SB-3a/SB-4 addenda, D-SB2-16 correction and #067 progress (PB-D3)"
```

Nothing else is required by this prompt. The open questions in §5 are for
Mission Control; none of them blocks the commit.

---

## 2. Verify-first values observed

| # | Check | Expected | Observed | |
|---|---|---|---|---|
| 1 | `git status --porcelain` | empty | empty | ✅ |
| 2 | `git log -1 --format=%s` | ends with `(SB-4)` | `feat(provider-channel): declare provider_channel.enabled in the scoped-settings taxonomy, annex ADR-0131 (SB-4)` | ✅ |
| 3 | `wc -l` record, roadmap | 619, 2384 | 619, 2384 | ✅ |
| 4 | `grep -c 'B-D-25\|B-D-26'` record, roadmap | 0, 0 | 0, 0 | ✅ see note |
| 5 | B-D-14 heading; SB-1 addendum | `:243`; `:257` | `:243`; `:257` | ✅ |
| 6 | B-D-21 heading; last line | `:323`; `:334` | `:323`; `:334` `disabling stops the tenant's timer and surfaces, not the file.` | ✅ |
| 7 | B-D-24 heading; pause-point heading | `:355`; `:366` | `:355`; `:366` | ✅ |
| 8 | D-SB2-16 bullet; its last line | `:392`; `:395` | `:392`; `:395` `  deadline (PB-2b OQ-3).` | ✅ |
| 9 | §7 heading; paragraph end | `:575`; `:593` | `:575`; `:593` `` `in-progress`, tenant-local provider entries #068 `open`. `` | ✅ |
| 10 | `Stage B additions (2026-09-10):` | `:564` | `:564` | ✅ |
| 11 | roadmap `  (B-D-1…B-D-24)`; `**Progress (2026-09-10).**` | `:2186`; `:2213` | `:2186`; `:2213` | ✅ |
| 12 | `## Change log`; last row; `^| 2026-09-1` count | `:2329`; `\| 2026-09-08 \|`; 0 | `:2329`; `\| 2026-09-08 \|`; 0 | ✅ |
| 13 | `ls docs/adr \| grep -c '^0131'`; README "next free … **0132**" | 1; 1 | 1; 1 | ✅ |
| 14 | `ls docs/reports/` | PB-1a, PB-1b, PB-1d, PB-D2; no PB-D3 | `PB-1a-report.md`, `PB-1b-report.md`, `PB-1d-report.md`, `PB-D2-report.md` | ✅ |

The prompt's expectations were `wc -l` counts of the working tree (not
Repomix block counts), and every line number matched exactly.

**Note on check 4.** Both counts are 0 as expected. The parenthetical, though,
says the ids exist "only in ADR-0131 and `docs/reports/PB-1d-report.md`".
`grep -rl 'B-D-25\|B-D-26' docs/` also finds `docs/adr/README.md`: the ADR-0131
index row at `:198` and a body line at `:836`, both citing B-D-25 as ADR-0131
does. The parenthetical is descriptive, not a text anchor any edit depends on,
so I proceeded.

---

## 3. Diff summary

```
 .../concepts/provider-channel-stage-b-decisions.md | 129 ++++++++++++++++++---
 docs/roadmap.md                                    |  17 ++-
 2 files changed, 131 insertions(+), 15 deletions(-)
```

(`docs/reports/PB-D3-report.md` is untracked and therefore not in the stat.
`git diff --patience --stat` gives the same figures.)

**The 15 deletions are all listed replacements. B-D-1…B-D-24 lose no line.**

- Status header line: 1
- §7 heading: 1
- §7 paragraph: 12. The other five lines of the old paragraph survive
  unchanged inside the new one.
- roadmap `  (B-D-1…B-D-24)`: 1

`git diff -U0 | grep '^-'` shows exactly these lines.

**`docs/concepts/provider-channel-stage-b-decisions.md`** (line numbers after
the edit; the file grows from 619 to 720 lines):

- (a) Status header: `; B-D-25/B-D-26 and addenda 2026-09-11` inserted at
  `:3`. The line was not re-wrapped (now 152 characters), so the edit stays a
  pure insertion.
- (b) B-D-14 second addendum (*2026-09-11*, SB-3a): bullet at `:263`–`:273`.
  It sits directly after the SB-1 addendum, as the last item of the decision,
  before `### B-D-15` (now `:275`).
- (c) B-D-21 addendum (*2026-09-11*, SB-4): bullet at `:347`–`:357`, after a
  blank line that follows `disabling stops … not the file.`, before
  `### B-D-22` (now `:359`).
- (d) D-SB2-16 correction (*2026-09-10*, PB-2c): nested bullet at
  `:474`–`:478`, directly after `  deadline (PB-2b OQ-3).`
- (e) `### B-D-25` at `:389` and `### B-D-26` at `:432`, immediately before
  `### Pause point 1 closed (2026-09-08)` (now `:444`).
- (f) §6 "Stage B additions (2026-09-11)" paragraph at `:656`, after the
  2026-09-10 paragraph.
- (g) §7 heading changed at `:668`; replacement paragraph at `:670`–`:694`;
  `## 8.` now at `:696`.

**`docs/roadmap.md`** (2384 → 2399 lines):

- (a) `  (B-D-1…B-D-26)` at `:2186`.
- (b) **Progress (2026-09-11).** paragraph at `:2225`–`:2236`, directly after
  the 2026-09-10 paragraph, before the `---`.
- (c) Change-log rows `| 2026-09-10 |` at `:2398` and `| 2026-09-11 |` at
  `:2399`, one line each, appended after the 2026-09-08 row (now `:2397`).

---

## 4. Consistency checks

```
$ grep -c 'B-D-25' docs/concepts/provider-channel-stage-b-decisions.md docs/roadmap.md docs/adr/0131-provider-channel-enabled-in-the-scoped-settings-taxonomy.md
docs/concepts/provider-channel-stage-b-decisions.md:4
docs/roadmap.md:2
docs/adr/0131-provider-channel-enabled-in-the-scoped-settings-taxonomy.md:4
```

Expected ≥ 3, ≥ 1, ≥ 3. Result 4, 2, 4 ✅

```
$ grep -c '^| 2026-09-1' docs/roadmap.md
2
```

✅

```
$ grep -c 'day 61' docs/concepts/provider-channel-stage-b-decisions.md
1
```

✅

Secret scan on added lines:

```
$ git diff | grep '^+' | grep -ciE 'seed|keygen|\.ssh|178\.'
0
$ git diff | grep '^+' | grep -cE '[0-9a-f]{64}'
0
```

Both ✅. The same greps with the `+++ b/…` header lines excluded also return
0, 0. The new text adds no path, host or key material. The only hash it
carries is the already-recorded abbreviation `c1383c4f…ec83`.

**Final `git status --porcelain`:**

```
 M docs/concepts/provider-channel-stage-b-decisions.md
 M docs/roadmap.md
?? docs/reports/PB-D3-report.md
```

---

## 5. Open questions

1. **"B-D-25 Q6" and "clause 6" are two names for one thing.**
   - B-D-25 numbers its items as *clauses* 1–7, and B-D-26 cites "B-D-25
     clause 7".
   - The B-D-21 addendum, verbatim from the prompt, cites "B-D-25 Q6". So does
     ADR-0131, at `:8`, `:114` (§5 heading) and `:137`, where "Q" is the
     numbering of the MB-1 brief's questions.
   - The two schemes use the same order, so Q6 is clause 6 ("Disabled") and
     every citation resolves.

   Mission Control should decide between two options:
   - accept the two names as they stand, or
   - add a one-line addendum under B-D-25 later ("clause *n* answers MB-1 brief
     question Q*n*").

   The verbatim rule kept me from harmonising it here, and the ADRs are out
   of scope.
2. **OP-B-5 still does not resolve (PB-1d §5 (g)).** ADR-0131 `:8` cites
   "OP-B-5's UI consequence via B-D-25 Q6". `grep -rn 'OP-B-5' docs/` finds
   only ADR-0131 and `docs/reports/PB-1d-report.md`. PB-D3 as issued does not
   record OP-B-5, so the citation still points outside the repository.
3. **Docs debt carried from PB-1b and PB-1d, and what this prompt closed.**

   Closed by PB-D3:
   - PB-1b (a): the D-SB2-16 day-61 correction. The PB-1b table's calendar
     (day 60 = 2026-11-09, `remaining` 30, no reminder) agrees with the new
     text.
   - PB-1b (b): the §7 entries for SB-3a, the suite count 85 → 101, the B-D-14
     addendum and the #067 progress.
   - PB-1d (c): next free ADR 0132, `PROVIDER_TAXONOMY`, suite 101/22.
   - PB-1d (d): B-D-25 with both clarifications.
   - PB-1d (e): #067 progress.
   - PB-1d (f): the B-D-21 delivery addendum.

   Still open:
   - PB-1b (b), last bullet: the **full-suite baseline** after SB-3a (OP-B-10,
     "expected 4,924"). PB-D3 does not record it, and no evening-run figure
     has been supplied. Per PB-1b, 4,924 is not carried forward as observed.
   - PB-1b suggested naming the `SuccessorStatus` alias in §7. The new §7
     does not name it; the B-D-14 addendum spells out its three values.
   - PB-1d (c) also gave the credential-vault taxonomy suite (26) and the
     provider-credentials web suite (56). The prompt's §7 text does not carry
     them. That looks intentional, since §7 tracks the provider-channel suite
     only.
4. **Placement and formatting choices.** Every "append after" instruction was
   met at the named anchor. Where the prompt left a blank line open, I
   followed the existing pattern:
   - (b) follows the SB-1 addendum with no blank line, as consecutive items
     of one list, like B-D-10's two addenda.
   - (c) opens with a blank line after the prose, as B-D-7 and B-D-14 do and
     as PB-D2 OQ-5 settled, so it renders as a list item rather than a lazy
     continuation.
   - (d) is indented two spaces, so it renders as a sub-bullet of D-SB2-16.
   - (e) and (f) are separated from their neighbours by blank lines.
   - All new text keeps the prompt's own wrapping.
5. **Verification beyond the prompt's table.** Every §7 fact and every
   identifier in the new text was checked against the tree before writing,
   and all match:
   - `services/provider_channel/` holds exactly `__init__`, `directory`,
     `prefill`, `publishing_key`, `ring`, `schemas`.
   - `ring.py` defines `verify_directory_with_ring` (keyword defaults
     `ring=PUBLISHING_KEY_RING`, `current_key_id=PUBLISHING_KEY_ID`),
     `RingVerification` (`key_id`, `successor_in_use`, `announced_successor`),
     `UnknownPublishingKeyId(DirectoryVerificationError)` and
     `SuccessorStatus = Literal["in_ring", "not_in_ring", "contradicts_ring"]`.
   - `PROVIDER_TAXONOMY` is at `services/credential_vault/taxonomy.py:284`.
     The `provider_channel` declaration carries `env_fallback=False` and
     `optional=True` (`:277`–`:278`).
   - `_ENV_CONFIG_FIELDS` lives in `services/investments/credential_resolver.py:143`
     and has no `provider_channel` key. `tests/services/credential_vault/test_taxonomy.py:344`
     asserts the absence.
   - The card pill copy in `web/routes/provider_credentials.py:178` reads
     "dormant — nothing reads this switch yet …".
   - `services/data_normalization/fixtures/default_asset_classes.json`
     exists.
   - The version constants `DIRECTORY_FORMAT_VERSION`,
     `ENVELOPE_SCHEMA_VERSION` and `FILL_SCHEMA_VERSION` are all `1`.
   - `pyproject.toml` has `httpx>=0.27`, `cryptography>=42` and
     `pytest-httpx>=0.35`, and no `pynacl`.
   - Fixtures: 1,180 B and 129 B; SHA-256 prefix and suffix `c1383c4f…ec83`.
   - Suite figures: `pytest --collect-only -q tests/services/provider_channel`
     gives **101 tests collected**, and for `test_contract.py` **22**. This
     was collection only. No test was executed, no DB was touched, and no
     test was written.
   - The backfilled **2026-09-10 row** matches what PB-D2 actually committed
     (`4e41d5d`):
     - the format doc's §4 signature-file-encoding bullet
       (`docs/concepts/provider-directory-format.md:127`–`:131`);
     - its §11 pointer to the record (`:413`);
     - the D-SB2 bullets in the record.
6. **Not touched, because not listed.** Flagged so none of it is a surprise:
   - The record's `- **Date:** 2026-09-08` header line.
   - The record's `- **Governs:**` line.
   - The ADR-0131 file and the ADR index. ADR-0131 §5 says PB-D3 records
     B-D-25 after the ADR; that citation now resolves.
   - The roadmap change-log table is not uniformly chronological. The top rows
     run 2026-08-25 → 2026-08-16 descending, and the tail ascends to
     2026-09-08. Both new rows were appended at the end ("newest last"), as
     instructed.
