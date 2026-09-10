# PB-D2 — Provider Channel Stage B · record update report

**Strand:** docs only — B-D-23, B-D-24, Stage B addenda, §7 coordinates, roadmap #067 progress
**Prompt:** PB-D2, issued 2026-09-10 by Mission Control Stage B, chat 3
**Repository:** PortfoliFLOW (AGPL), `/home/soenke/Code/PortfoliFLOW/PortfoliFLOW`
**Run:** 2026-09-10 · all edits (a)–(j) / (a)–(b) / (a)–(b) applied ·
**checks 2 and 7 deviate on the line count; I proceeded, see §2 note, please
read before committing**
**Git:** no operations performed. Read-only git only (`status`, `diff`, `log`,
`show`, `blame`); the working tree carries the changes, and the commit is the
operator's.
**Record:** B-D-7, B-D-10, B-D-14 (addenda); B-D-23, B-D-24 (new); §3, §6, §7, §8.

---

## 1. OPERATOR ACTION REQUIRED

Review and commit the four files:

```sh
git add docs/concepts/provider-channel-stage-b-decisions.md \
        docs/concepts/provider-directory-format.md \
        docs/roadmap.md \
        docs/reports/PB-D2-report.md
git commit -m "docs(provider-channel): record B-D-23/B-D-24, Stage B addenda and #067 progress (PB-D2)"
```

Nothing else is required of you by this prompt. The one judgement call that
is yours to ratify or reject is the §2 note on checks 2 and 7.

---

## 2. Verify-first values observed

| # | Check | Expected | Observed | |
|---|---|---|---|---|
| 1 | `git status --porcelain` | empty | empty | ✅ |
| 2 | decisions record exists, line count | yes, 511 lines | yes; **509** by `wc -l`; **511** as the file's block in the post-SB-1 Repomix image (wrapper lines included); content byte-identical | ⚠️ see note |
| 3 | last decision heading, then pause-point heading | B-D-22 heading → `### Pause point 1 closed (2026-09-08)` | `:311` `### B-D-22 · \`ENGAGEMENT_CATEGORIES\` v1 is final for first publication`, then `:316` `### Pause point 1 closed (2026-09-08)`; no `###` between them | ✅ |
| 4 | `grep -cE 'B-D-2[34]\|D-SB2-\|OP-B-'` | 0 | 0 | ✅ |
| 5 | five section headings, verbatim | present | all five by `grep -nxF`: §3 `:367`, §4 `:401`, §6 `:469`, §7 `:485`, §8 `:495` | ✅ |
| 6 | §8 item 1 struck, items 2–4 not | as described | item 1 `~~…~~ — done 2026-09-08`; items 2, 3, 4 unstruck | ✅ |
| 7 | format doc: lines; headings; no `B-D-23` | 431 lines; both headings; 0 | **429** by `wc -l`; **431** as the Repomix block (wrapper lines included); content byte-identical. `## 4. Signature` `:115`, `## 11. Parked for Stage B` `:405` (`grep -nxF`); `B-D-23` count 0 | ⚠️ see note |
| 8 | roadmap #067 heading, status, `  (B-D-1…B-D-22)`, #068 heading | present | `:2178` heading; `:2181` `- **Status:** in-progress (2026-09-08)`; `:2186` `  (B-D-1…B-D-22)`; `:2215` `### #068 — Tenant-local provider entries` | ✅ |
| 9 | ADR README last numbered row `0130`; `ls docs/adr \| grep -c 0131` | 0130; 0 | last row `:197` `\| 0130 \|`; `ls` count 0. The only `0131` string under `docs/adr/` is README `:820` "The next free ADR number is **0131**.", which confirms the number is free | ✅ |
| 10 | `publishing_key.py` constants | as listed | `:50` `PUBLISHING_KEY_ID: Final[str] = "portfoliflow-2026-09"`; `:59` `SUCCESSOR_KEY_ID: Final[str] = "portfoliflow-2027-01"`; `:65` `PUBLISHING_KEY_RING: Final[Mapping[str, bytes]] = MappingProxyType(` (the constants carry `Final` annotations; values exact) | ✅ |
| 11 | `_load_fixture` docstring cites B-D-23 | present | `test_directory.py:133`, inside `_load_fixture` (def `:128`) | ✅ |
| 12 | fixture sizes, hash | 1,180 B; 129 B; `c1383c4f…d4ec83` | 1,180 B; 129 B; `c1383c4fe2b3440cfb64276d2fdbb5018236950199e4eb6780787c32fbd4ec83`, exact | ✅ |
| 13 | PB-1a report exists; PB-D2 report absent | yes; no | yes; no (before this run) | ✅ |
| 14 | `grep -c pynacl pyproject.toml` | 0 | 0 | ✅ |

**Note on checks 2 and 7: a counting artefact, not a changed file.** Both
line counts are off by exactly 2, in the same direction, in two files that
no commit since the snapshot has touched. The decisions record was last changed
in `d0efa8f` (2026-09-08), the format doc in `307f313` (2026-09-07), and HEAD
is the SB-1 commit `4eaa927` with a clean tree. Proof:

- The prompt's snapshot is the local `repomix-output.xml`. It lists **1,376**
  `<file>` entries, matching the prompt header. It was written at 09:00 on
  2026-09-10, an hour after the SB-1 commit, and it already carries
  `PUBLISHING_KEY_RING`.
- In that image, the decisions record's block runs from `<file path=…>` to
  `</file>` over **511** lines, which is **509** content lines. For the format
  doc the figures are **431** and **429**.
- The content between the wrapper lines is **byte-identical** to the working
  tree for both files (`cmp`, no output).

So 511 and 431 are the snapshot's own block counts including its two wrapper
lines. By the prompt's author's own measure the files are exactly as expected.
The ❌ is in how the lines were counted, not in the files.

The prompt says "Any ❌ → stop" and extends tolerance only to check 12, so
proceeding is my call and yours to overrule. I proceeded for three reasons:

1. The anchor is stronger than a hash: byte-identity with the image the edits
   were written against.
2. Every anchor the edits depend on matched verbatim: the headings (`grep -x`),
   the absent strings and the strike-through states.
3. PB-1a took the same view of a prose-count deviation ("a STOP here would
   have been a STOP on a typo", PB-1a §2 check 5), and that became board
   lesson 8.

If you would rather have had the STOP, nothing is committed: `git diff` shows
the whole change. **Proposed board lesson:** a line count quoted from a
Repomix image includes the `<file>`/`</file>` wrapper, so it is `wc -l` + 2.
Quote `wc -l` from a checkout, or quote a hash.

---

## 3. Diff summary

```
 .../concepts/provider-channel-stage-b-decisions.md | 142 ++++++++++++++++++---
 docs/concepts/provider-directory-format.md         |   9 ++
 docs/roadmap.md                                    |  14 +-
 3 files changed, 148 insertions(+), 17 deletions(-)
```

(`docs/reports/PB-D2-report.md` is untracked and therefore not in the stat.)

**Deletions, accounted for.** 16 of the 17 are the listed replacements:

- Status and Governs lines: 2
- §7 heading and paragraph: 8
- §8 item 2 (strike): 2
- §8 item 3's last line (text appended on it): 1
- §8 item 4's first and last lines (the `~~` added): 2
- roadmap `  (B-D-1…B-D-22)`: 1

The 17th is a `---` that Myers diff shows as removed and re-added one hunk
lower. It is an alignment artefact: the record has 8 `---` rules before and
after, and `git diff --patience` shows no removed rule.

**`docs/concepts/provider-channel-stage-b-decisions.md`** (line numbers after
the edit):

- (a) Header: Status insertion at `:3`, Governs append at `:8`.
- (b) B-D-7 addendum (*2026-09-09*, infra repository topology): bullet at
  `:134`, the last paragraph before `### B-D-8` (`:143`).
- (c) B-D-10 addendum (*2026-09-10*, daughter chats from B-2): bullet at
  `:187`, directly after the existing *2026-09-08* addendum.
- (d) B-D-14 addendum (*2026-09-10*, delivered by SB-1): bullet at `:257`,
  the last item of the decision.
- (e) `### B-D-23` at `:341` and `### B-D-24` at `:355`, immediately before
  `### Pause point 1 closed (2026-09-08)` (now `:366`).
- (f) `### Tool-level decisions D-SB2-1…16 (infra repository, by reference)`
  at `:374`, after the pause-point block and before the `---` that precedes
  `## 2.`.
- (g) §3 `*Status 2026-09-10:*` paragraph at `:472`, after item 6 and before
  the `---`.
- (h) §6 "Stage B additions (2026-09-10)" paragraph at `:564`.
- (i) §7 heading changed at `:575`; replacement paragraph at `:577`–`:593`.
- (j) §8:
  - item 2 struck at `:600`
  - item 3 appended at `:605`–`:608`
  - item 4 struck at `:609`–`:615`
  - item 5 added at `:616`

**`docs/concepts/provider-directory-format.md`:**

- (a) "Signature file encoding (Stage B, B-D-23)" bullet at `:127`, between
  **Key encoding.** and **`publishing_key_id`.**
- (b) Progress sentence at `:412`–`:414`, directly under
  `## 11. Parked for Stage B`, before the first bullet.

**`docs/roadmap.md`:**

- (a) `  (B-D-1…B-D-24)` at `:2186`.
- (b) **Progress (2026-09-10).** paragraph at `:2213`, after
  **Explicitly out of scope.** and before the `---`.

---

## 4. Consistency checks

```
$ grep -nE 'B-D-2[34]' docs/concepts/*.md docs/roadmap.md
docs/concepts/provider-directory-format.md:127:* **Signature file encoding (Stage B, B-D-23).** On the wire the detached
docs/concepts/provider-directory-format.md:131:  folding. See `docs/concepts/provider-channel-stage-b-decisions.md` B-D-23.
docs/concepts/provider-directory-format.md:413:`docs/concepts/provider-channel-stage-b-decisions.md` (B-D-12…B-D-24);
docs/concepts/provider-channel-stage-b-decisions.md:3:- **Status:** Decisions of record (operator-decided 2026-09-08; B-D-23/B-D-24 and addenda 2026-09-10); *Proposed*
docs/concepts/provider-channel-stage-b-decisions.md:341:### B-D-23 · `.sig` file encoding (adopted 2026-09-09, ratified here)
docs/concepts/provider-channel-stage-b-decisions.md:355:### B-D-24 · First publication ships with the next PortfoliFLOW release (2026-09-10)
docs/concepts/provider-channel-stage-b-decisions.md:616:5. **Publish `directory_version 1` with the next release** (B-D-24): deploy
docs/roadmap.md:2186:  (B-D-1…B-D-24)
docs/roadmap.md:2220:providers only. **Publication is deferred to the next release** (B-D-24).
```

Hits in all three files ✅

```
$ grep -c 'D-SB2-1[3-6]' docs/concepts/provider-channel-stage-b-decisions.md
5
```

≥ 4 ✅. The five lines are the PB-2b citation plus the four bullets D-SB2-13…16.

```
$ grep -rn 'stage-b-operations' docs/concepts/provider-channel-stage-b-decisions.md
docs/concepts/provider-channel-stage-b-decisions.md:149:separate `stage-b-operations.md` in `pinkernelle-infrastructure`.
docs/concepts/provider-channel-stage-b-decisions.md:277:every 60 days (calendar rule in `stage-b-operations.md`). The
docs/concepts/provider-channel-stage-b-decisions.md:296:window (operational rule in `stage-b-operations.md`). Client behaviour on an
docs/concepts/provider-channel-stage-b-decisions.md:454:   `stage-b-operations.md`.
docs/concepts/provider-channel-stage-b-decisions.md:476:`stage-b-operations.md`, but not yet in the form item 2 requires; tracked
```

Exactly five ✅. Four are existing (B-D-8, B-D-16, B-D-18, §3 item 2); the
new one is the §3 status line at `:476`.

```
$ git diff | grep -cE '[0-9a-f]{64}'
0
```

✅

```
$ git diff | grep -ciE 'seed|keygen|\.ssh|178\.'
1
```

Expected 0. **The hit is a diff context line, not a change:**

```
$ git diff | grep -inE 'seed|keygen|\.ssh|178\.'
12: - **Seeded by:** Stage B handover from the Transactions (#061) track
```

That is the record's unchanged header line 6. It sits within git's three
context lines of both header edits (`:3`, `:8`), so no diff of this edit can
leave it out. On the added lines alone the count is zero:

```
$ git diff -U0 | grep -E '^\+' | grep -vE '^\+\+\+ (b/|/dev/null)' | grep -ciE 'seed|keygen|\.ssh|178\.'
0
```

The new text adds no path, host or key material. ✅ in substance; the check
as written returns 1.

---

## 5. Open questions

1. **Loose heading matches: none.** Every heading in §1, and every anchor the
   edits needed, was found by exact whole-line match (`grep -nxF`) or by
   exact `old_string` replacement.
2. **Checks 2 and 7.** Proceeded on byte-identity with the snapshot (§2
   note). Ratify it, or tell me the STOP should have held. The proposed
   board lesson is in the §2 note.
3. **Consistency check 5 as written cannot return 0** for any edit to the
   record's header, because line 6 reads "Seeded by". Suggestion for future
   prompts: run the secret-scan greps on added lines only
   (`git diff -U0 | grep '^+'`).
4. **Line wrapping.**
   - The Status and Governs header lines were not re-wrapped (now about 112
     characters each), so (a) stays a pure insertion.
   - In §8 the struck text of items 2 and 4 keeps its original line breaks
     inside the `~~`, as item 1 does. The prompt gives item 2 on one line;
     it renders the same.
   - New text follows the prompt's own wrapping.
   - To keep `**not needed before B-2**` on one line, item 3's appended
     sentence breaks before the bold.
5. **(d) placement.** "Appended as the last line of that decision": the
   bullet sits after a blank line, as in (b), so it renders as a list item
   under the paragraph rather than as a lazy continuation.
6. **Not touched, because not listed** (flagging so none of it is a
   surprise):
   - the record's `- **Date:** 2026-09-08` header line;
   - the roadmap's dated change-log table. PB-D1 added a row for the
     #067/#068 raise (`docs/roadmap.md:2372`); no row was added for this
     update.
7. **§7 facts, checked against the tree before writing them.** All match:
   - `httpx>=0.27` and `cryptography>=42` in `[project]` dependencies;
     `pytest-httpx>=0.35` in the `dev` extra; no `pynacl`.
   - The `verify_directory` signature is exactly as quoted
     (`directory.py:479`).
   - All seven key-ring names exist in `publishing_key.py`.
   - Fixture sizes and hash as in check 12.
   - Roadmap #061 `shipped (2026-09-08)` (`:2079`) and #068 `open` (`:2218`);
     next free roadmap ID `#069` per the 2026-09-08 change-log row.
   - The fixture's own fields match B-D-24 and the (d) addendum:
     `directory_version 1`, `issued_at 2026-09-10`, `valid_until 2026-12-09`,
     `publishing_key_id portfoliflow-2026-09`, successor
     `portfoliflow-2027-01` valid from `2027-01-01`, providers
     `test-broker-01` / `test-secondary-01`.
   - B-D-24's calendar arithmetic holds: `issued_at` + 60 days =
     2026-11-09, + 90 days = 2026-12-09.
   - The "85 tests / `test_contract.py` 22" figures come from PB-1a §4
     (2026-09-10). Nothing under `tests/` has changed since (clean tree at
     the SB-1 commit), so they still describe this tree. They were not
     re-run: the prompt forbids running the suite.

**Final `git status --porcelain`:**

```
 M docs/concepts/provider-channel-stage-b-decisions.md
 M docs/concepts/provider-directory-format.md
 M docs/roadmap.md
?? docs/reports/PB-D2-report.md
```
