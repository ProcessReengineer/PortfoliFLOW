# PB-D5 — Record addenda (B-D-13/15/17), §6 lessons, §7 coordinates post-SB-3b, roadmap #067 progress and change-log row

**Date:** 2026-09-17 · **Type:** docs prompt, documentation only · **Result:** complete, no STOP.

## OPERATOR ACTION REQUIRED

Claude Code performed **no git writes**. Nothing is staged and nothing is committed.
Run:

```sh
git add docs/concepts/provider-channel-stage-b-decisions.md docs/roadmap.md docs/reports/PB-D5-report.md
git commit -m "docs(provider-channel): record B-D-13/15/17 addenda for the fetch client, §6 lessons, §7 coordinates post-SB-3b, #067 progress and change-log row (PB-D5)"
```

## Verify-first values observed

All eighteen rows matched before any edit; no anchor mismatch, so no STOP.

| # | Check | Expected | Observed |
|---|---|---|---|
| 1 | `git status --porcelain` | empty | empty |
| 2 | `git log --oneline -2` | subject ends `(SB-3b, PB-1g)`; line 2 `d0393bb` | `de1b0cb feat(cli): … (SB-3b, PB-1g)` · `d0393bb feat(provider-directory): … (SB-3b, PB-1f)` |
| 3 | `wc -l` record · roadmap | `824` · `2418` | `824` · `2418` |
| 4 | `grep -c '^### B-D-'` | `26` | `26` |
| 5 | record `294p;296p` | `successful fetch.` · `### B-D-14 · Key ring from code only; successor announcements are a notice (C-3)` | both exact; 295 blank |
| 6 | record `336p;338p` | `§9).` · `### B-D-16 · Directory validity: 90 days, re-signed at least every 60 (C-7)` | both exact; 337 blank |
| 7 | record `353p;355p` | `mechanism.` · `### B-D-18 · No forward-compatible reader (C-5)` | both exact; 354 blank |
| 8 | record `754p;756p` | `editable-install finders, or it pins to one venv.` · `---` | both exact; 755 blank |
| 9 | record `758p` | §7 heading reads `verified 2026-09-16, post-SB-5 full image, head 395b157` | exact |
| 10 | record `798p;800p` | `` `portfoliflow-2027-01` on 2027-01-01. `` · `## 8. Operator actions to open Stage B` | both exact; 799 blank |
| 11 | roadmap `42p` | the next-free-ID line reads `#069` | exact |
| 12 | roadmap `2254p;2256p` | `B-1 milestone review; libsodium.js parity check parked for B-3.` · `---` | both exact; 2255 blank |
| 13 | roadmap `tail -1 \| cut -c1-14` | `\| 2026-09-16 \|` | `\| 2026-09-16 \|` |
| 14 | `grep -c 'Progress (2026-09-1'` roadmap | `3` | `3` |
| 15 | `ls services/provider_directory/` | five modules | `__init__.py cache.py fetch.py provenance.py refresh.py` (plus `__pycache__`, a build artefact) |
| 16 | `grep -c 'nothing in Stage A encrypts'` in `services/provider_channel/directory.py` | `0` | `0` |
| 17 | `grep -c 'directory-refresh\|directory-status' cli/__init__.py` | `≥ 2` | `4` |
| 18 | `ls docs/reports \| grep -c -E 'PB-1f\|PB-1g\|PB-D5'` | `2` | `2` (`PB-1f-report.md`, `PB-1g-report.md`; no PB-D5) |

## Diff and gates

`git diff --numstat`:

```
121	33	docs/concepts/provider-channel-stage-b-decisions.md
15	0	docs/roadmap.md
```

| Gate | Expected | Observed |
|---|---|---|
| `git diff --stat` | exactly the two docs files | exactly the two docs files, 136 insertions / 33 deletions |
| `git status --porcelain` | `2× " M"` + `?? docs/reports/PB-D5-report.md` | as expected |
| `grep -c '^### B-D-'` record | `26` | `26` — addenda only, no new entry |
| `grep -c 'Addendum 2026-09-16 (delivered by SB-3b'` | `3` | `3` |
| `grep -c '^## 7. Coordinates (verified 2026-09-17'` | `1` | `1` |
| `grep -c 'verified 2026-09-16, post-SB-5'` | `0` | `0` |
| `grep -c 'Fetch-client lessons'` | `1` | `1` |
| §8 heading placement | directly after §7's last paragraph plus one blank line | §7 heading at 834, §7 ends at 886 (`2027-01-01.`), 887 blank, `## 8.` at 888 |
| `grep -c 'Progress (2026-09-1'` roadmap | `4` | `4` |
| `tail -1` roadmap, first 14 chars | `\| 2026-09-17 \|` | `\| 2026-09-17 \|` |
| roadmap `sed -n '42p'` | unchanged, `#069` | unchanged (asserted programmatically before and after the splice) |
| `wc -l` both files | report both | record `824 → 912`; roadmap `2418 → 2433` |
| Leak scan, three files | no hits | no hits in any added line |
| `git diff --name-only \| grep -c '\.py$'` | `0` | `0` — no ruff, no pyright run |

**Line-count reconciliation.** Record net +88 = B-D-13 addendum +19 · B-D-15 +17 · B-D-17 +22 · §6 lessons +18 · §7 rewrite +12 (41 lines replaced by 53), each block counted with its preceding blank line. The prompt's estimate (`+20 +17 +25 +18 +Δ§7`) is one line high on the first block and three high on the third; every text anchor matched, so per §1 this is *proceed and report*, not a STOP. Roadmap +15 = 1 blank + 13 progress lines + 1 change-log row, matching the estimate exactly.

**Leak scan, in words.** The three files were scanned — as whole files, and again over the added lines of the diff alone — for the four Mission Control working-id shapes named in §3. **No hits in any line this prompt added.** The whole-file sweep does surface pre-existing text in the record and the roadmap: ordinary English words that also occur inside two of those shapes, the phrase "Mission Control" itself, the handover question numbers already carried in the B-D-1…B-D-6 headings since the record's first commit, and §6's own statement of the rule. None of these are working ids, and none sits in this prompt's diff. Per §2.3 neither the shapes nor the search pattern are reproduced here.

**Blank-line hygiene.** Every splice seam was inspected: exactly one blank line between each anchor and its new block, and between each block and the following heading or rule. No consecutive blank lines were introduced.

## Deliberately not touched

1. **The `[tool.pyright]` include list in `pyproject.toml`.** `services/provider_directory/` is still outside the ADR-0110 typing-island set. §7 now records that fact ("Not yet in the `[tool.pyright]` island set (housekeeping pending)"), but the include line itself belongs to the separate housekeeping prompt and was not edited. No `.py` and no `pyproject.toml` appear in the diff.
2. **`docs/roadmap.md` line 42, `**Next free ID:** `#069`.`** Unchanged, as §2.2 requires — asserted programmatically both before and after the splice. The #067 summary row is likewise unchanged; only the detail block gained a progress paragraph.

Also untouched, per §0: no ADR, no `CLAUDE.md`, no template, no `docs/adr/README.md`, no `docs/concepts/provider-directory-format.md`.

## Open questions

1. **Full-suite home run is still owed** before SB-6, expected ≥ 5,094 (= 4,927 baseline + 54 SB-5 + 113 SB-3b). §7 and the #067 progress paragraph both record it as outstanding; this prompt ran no tests, none being applicable to a documentation-only change.
2. **Pyright island housekeeping** for `services/provider_directory/` — noted in §7 and in the progress paragraph as the next non-SB-6 item, needing its own prompt.
3. **Next free ADR `0132` and next free roadmap `#069`** are carried into §7 with an explicit "verify at writing time" caveat, since the UX-overhaul track may claim `#069` first. Nothing in this prompt consumed either number.
