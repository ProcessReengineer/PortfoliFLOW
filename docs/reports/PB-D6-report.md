# PB-D6 — Record §7 after the pyright island change and the 2026-09-17 full-suite baseline

**Date:** 2026-09-17 · **Type:** docs prompt — documentation only.

## OPERATOR ACTION REQUIRED

Nothing is staged and nothing is committed by this prompt. To land it:

```sh
git add docs/concepts/provider-channel-stage-b-decisions.md docs/roadmap.md docs/reports/PB-D6-report.md
git commit -m "docs(provider-channel): §7 after the pyright island change and the 2026-09-17 full-suite baseline; change-log row (PB-D6)"
```

## Verify-first values observed

| # | Check | Expected | Observed |
|---|---|---|---|
| 1 | `git status --porcelain` | empty | empty |
| 2 | `wc -l` record · roadmap | `912` · `2433` | `912` · `2433` |
| 3 | record: island-set sentence | one hit, line 859 | one hit, line 859; next line starts `(housekeeping pending). **CLI:**` |
| 4 | record: `4,927 passed at` | one hit, line 872 | one hit, line 872; lines 873–875 as quoted |
| 5 | record `sed -n '876p'` | `PB-1f, PB-1g, …` | matched verbatim |
| 6 | `grep -c 'services/provider_channel' pyproject.toml` | `1` | `1` (PB-H5 applied) |
| 7 | `docs/reports/full-suite-2026-09-17-report.md` | exists | exists |
| 8 | roadmap: `Next: full-suite home run` | one hit, line 2265 | one hit, line 2265 |
| 9 | roadmap `sed -n '42p'` | `**Next free ID:** `#069`.` | matched |
| 10 | roadmap `tail -1 \| cut -c1-14` | `\| 2026-09-17 \|` | `\| 2026-09-17 \|` |
| 11 | `ls docs/reports \| grep -c PB-D6` | `0` | `0` |

Tip at start: `5e0881c docs(reports): full-suite baseline 2026-09-17 after SB-5 and SB-3b, 5,109 passed at 3a34537`, over `3a34537` (PB-D5) and `de1b0cb` (SB-3b/PB-1g) — as expected.

## Diff and gates

Two files changed: record §7 (two replacements, reflowed at ≤ 80 columns) and roadmap (#067 progress clause + one appended change-log row). Report added.

The #067 progress sentence, before and after:

- before: `publication-day walk with \`2026.09.1\`. Next: full-suite home run` / `(≥ 5,094), pyright island housekeeping, SB-6 (\`enabled\` consumer, 24-hour` / `timer, refresh gesture, panel, suggestion filter, export gesture), B-1` / `milestone review.`
- after: `publication-day walk with \`2026.09.1\`. Next: pyright island housekeeping` / `done, home run green (5,109 at \`3a34537\`); SB-6 (\`enabled\` consumer,` / `24-hour timer, refresh gesture, panel, suggestion filter, export` / `gesture), B-1 milestone review.`

| Gate | Expected | Observed |
|---|---|---|
| `git diff --stat` | the two docs files only | record + roadmap only (report untracked) |
| `grep -c 'housekeeping pending'` record | `0` | `0` |
| `grep -c '5,109 passed at'` record | `1` | `1` |
| `grep -c '4,927 passed at'` record | `0` | `0` (4,927 survives only as "baseline of 4,927") |
| `grep -c '^### B-D-'` record | `26` | `26` |
| `grep -c 'full-suite home run'` roadmap | `0` in #067 progress | `0` file-wide |
| `tail -1` roadmap `\| cut -c1-14` | `\| 2026-09-17 \|` | `\| 2026-09-17 \|`; two rows dated 2026-09-17, the new one last |
| Leak scan (internal working ids) | no hits | no hits in the three files |
| No `.py` touched | `0` | `0` |

Line counts after the edits: record `918` (+6), roadmap `2434` (+1). Roadmap line 42 (`**Next free ID:** \`#069\`.`) unchanged.

## Open questions

1. **The six sample-workbook skips are a standing, not a one-off, gap.** They skip on every machine and in CI because `data/sample/` is gitignored, so the v21/v24 limits/AUM roundtrip, Phase-7 import wiring and benchmark import route have no gate anywhere. Recorded in the change-log row as open and outside Stage B; it wants a roadmap item of its own or a committed minimal fixture.
2. **The baseline is `3a34537`, one commit below the tip.** PB-H5's typing-only edits were in the tested tree but the full-suite report commit (`5e0881c`) is not; the record states this explicitly, so no re-run is implied before SB-6.
