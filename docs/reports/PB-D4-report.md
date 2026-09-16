# PB-D4 — Report

**Date:** 2026-09-16 · **Prompt:** PB-D4 (docs only) · **Tip at start:** `395b157`
(`… (SB-5, PB-1e)`), clean tree. Documentation only — no `.py` touched, no git
writes performed.

## OPERATOR ACTION REQUIRED

```sh
git add docs/concepts/provider-channel-stage-b-decisions.md docs/roadmap.md docs/concepts/provider-directory-format.md docs/reports/PB-D4-report.md
git commit -m "docs(provider-channel): record B-D-10/12/24 addenda, §6 process additions, §7 coordinates post-SB-5, #067 progress and change-log row, format doc after the sealed box (PB-D4)"
```

## Verify-first values observed (all 18 rows matched)

| # | Check | Expected | Observed |
|---|---|---|---|
| 1 | `git status --porcelain` | empty | empty |
| 2 | `git log --oneline -1` | `395b157`, `(SB-5, PB-1e)` | match |
| 3 | `wc -l` record · roadmap · format | 720 · 2399 · 438 | 720 · 2399 · 438 |
| 4 | `grep -c '^### B-D-'` | 26 | 26 |
| 5 | record 195/196/197 | `several concept rounds.` · blank · `### B-D-11 · …` | match |
| 6 | record 222/223/224 | `a menu").` · blank · `*Addendum to B-D-1 (2026-09-08):*` | match |
| 7 | record 387/388/389 | `**2026-11-09**; rotate …` · blank · `### B-D-25` | match |
| 8 | record 664/665/666 | `(PB-D2's change-log row).` · blank · `---` | match |
| 9 | record 668 | `## 7. Coordinates (verified 2026-09-11, post-SB-4 Repomix, head \`b034\`)` | match |
| 10 | record 694/696 | `(B-D-24).` · `## 8. Operator actions to open Stage B` | match |
| 11 | roadmap 42 | `**Next free ID:** \`#069\`.` | match |
| 12 | roadmap 2236/2238 | `milestone review.` · `---` | match |
| 13 | roadmap `tail -1` | starts `\| 2026-09-11 \| **Provider channel Stage B — SB-3a and SB-4 shipped` | match |
| 14 | `grep -c 'Progress (2026-09-1'` roadmap | 2 | 2 |
| 15 | format `nothing in Stage A encrypts` | one hit, line 69 | one hit, line 69 |
| 16 | format `currently named and not implemented` | one hit, line 425; 424 = `* The sealed-box implementation …` | match |
| 17 | `ls docs/reports \| grep -c PB-D4` | 0 | 0 |
| 18 | `grep -c 'pynacl>=1.6.2' pyproject.toml` | 1 | 1 |

No text mismatch; the run proceeded.

## Diff and gates

`git diff --numstat`:

```
128	24	docs/concepts/provider-channel-stage-b-decisions.md
4	3	docs/concepts/provider-directory-format.md
19	0	docs/roadmap.md
```

| Gate | Expected | Observed |
|---|---|---|
| `git diff --stat` | exactly the three docs files | three docs files, 151 insertions / 27 deletions |
| `git status --porcelain` | `3× " M"` + `?? docs/reports/PB-D4-report.md` | as expected (report untracked once written) |
| `grep -c '^### B-D-'` record | 26 | **26** (addenda only, no new entry) |
| `grep -c 'Addendum 2026-09-1[346]'` record | 3 | **3** — 09-14 (L196, B-D-10), 09-16 (L236, B-D-12), 09-13 (L442, B-D-24) |
| `grep -c '^## 7. Coordinates (verified 2026-09-16'` | 1 | **1** |
| `grep -c 'verified 2026-09-11, post-SB-4'` | 0 | **0** |
| §8 still follows §7 + one blank line | yes | `## 8.` at L800; L798 = `\`portfoliflow-2027-01\` on 2027-01-01.`, L799 blank |
| `grep -c 'Progress (2026-09-1'` roadmap | 3 | **3** |
| `tail -1 roadmap \| cut -c1-14` | `\| 2026-09-16 \|` | **`\| 2026-09-16 \|`** (file keeps its trailing newline) |
| `sed -n '42p'` roadmap | unchanged, `#069` | **`**Next free ID:** \`#069\`.`** |
| `grep -c 'nothing in Stage A encrypts'` format | 0 | **0** |
| `grep -c 'currently named and not implemented'` format | 0 | **0** |
| `grep -c 'Stage B SB-5'` format | 2 | **2** (L69 table row, L424 §11 bullet) |
| `wc -l` of the three files | see below | **824** · **2418** · **439** |
| working-id scan (the prompt's §3 regex for register items, kickoff question numbers, board versions and console ids) over the four files | no hits | **no hits** |
| `git diff --name-only \| grep -c '\.py$'` | 0 | **0** (no ruff/pyright needed) |

### Line-count deltas — one reportable difference

* **record 720 → 824 (+104)**; the prompt's estimate was `720 + 12 + 46 + 5 + 33 + Δ§7` ≈ 830.
  Actual block sizes as typed: B-D-10 addendum **12** (as estimated), B-D-12
  addendum **41** incl. its leading blank (estimated 46, **−5**), B-D-24 addendum
  **5** (as estimated), §6 additions **32** incl. leading blank (estimated 33,
  **−1**), Δ§7 = 41 − 27 = **+14**. Every text anchor matched, so per §1 and the
  gate table the run proceeded and reports the difference. No content was
  dropped: the difference is line-wrap bookkeeping in the prompt's estimate, not
  missing text.
* **roadmap 2399 → 2418 (+19)** — exactly the estimate (18-line progress block
  incl. leading blank + 1 change-log row).
* **format 438 → 439 (+1)** — exactly the estimate (one-line table row replaced
  in place; the two-line §11 bullet became three).

## Deliberately not touched

1. **`services/provider_channel/directory.py`** — the `encryption_public_key`
   docstring goes stale with SB-5 but is a code file; it belongs to the next
   build prompt on that file.
2. **`docs/adr/README.md`** — its CHANGELOG sentence is a separate open decision.
3. **`docs/roadmap.md` line 42** (`**Next free ID:** \`#069\`.`) — this prompt
   issues no roadmap id. The #067 summary row (line 915) is likewise unchanged.

Nothing else was modified: no ADR, no `CLAUDE.md`, no template, no `.py`.

## Open questions

* None blocking. Two items the record now carries forward for B-2 (B-D-12
  addendum): the relay-message shape (routing `Envelope` + `ciphertext` vs
  wrapping `ExportEnvelope` whole), and whether requiring `sender_tenant_handle`
  on the file artefact bumps `EXPORT_SCHEMA_VERSION` to 2.
* §7 flags two figures as "verify at writing time" (next free ADR **0132**, next
  free roadmap number **#069** — the UX-overhaul track may take it first), and
  records that a post-SB-5 full-suite home run is still due before SB-3b
  (expected ≥ 4,981 against the 4,927 baseline at `2a2be75`).
