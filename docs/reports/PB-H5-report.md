# PB-H5 — Stage B packages into the pyright island set; two pre-existing errors cleared

**Date:** 2026-09-17 · **Type:** housekeeping, no behaviour change · **Files:** 3 — `pyproject.toml`, `tests/services/provider_channel/test_directory.py`, this report.

## OPERATOR ACTION REQUIRED

Claude Code performed no git writes. Nothing is staged; nothing is committed.

```sh
git add pyproject.toml tests/services/provider_channel/test_directory.py docs/reports/PB-H5-report.md
git commit -m "chore(typing): add services/provider_channel and services/provider_directory to the pyright island set; clear two pre-existing errors in test_directory.py (PB-H5)"
```

## Verify-first values observed

| # | Check | Observed | Match |
|---|---|---|---|
| 1 | `git status --porcelain` | empty | ✅ |
| 2 | `git log --oneline -2` | line 1 ends `(PB-D5)` (`3a34537`); line 2 `de1b0cb` | ✅ |
| 3 | `[tool.pyright] include` (line 125) | exactly `"services/overlay",`, `"services/market_data",`, `]` | ✅ |
| 4 | `grep -c 'services/provider' pyproject.toml` | `0` | ✅ |
| 5 | `pyright` (bare, as CI) | `0 errors, 0 warnings, 0 informations` | ✅ |
| 6 | `pyright services/provider_channel services/provider_directory` | `0 errors` | ✅ |
| 7 | `pyright tests/.../test_directory.py` | exactly `2 errors` (below) | ✅ |
| 8 | `override: dict[str, object], needle: str` | one hit, line 377 | ✅ |
| 9 | `PUBLISHING_KEY_RING[SUCCESSOR_KEY_ID] = PUBLISHING_KEY` | one hit, line 424 | ✅ |
| 10 | `^from typing import` | `from typing import Final` (line 25) | ✅ |
| 11 | `pytest` both packages | `155 passed` · `89 passed` (3.1 s, DB-free) | ✅ |
| 12 | `ls docs/reports \| grep -c PB-H5` | `0` | ✅ |

The two pre-existing errors, verbatim:

```
tests/services/provider_channel/test_directory.py:380:49 - error: Argument of type "object" cannot be assigned to parameter "provider_id" of type "str" in function "_provider"
    "object" is not assignable to "str" (reportArgumentType)
tests/services/provider_channel/test_directory.py:424:9 - error: "__setitem__" method not defined on type "Mapping[str, bytes]" (reportIndexIssue)
```

Both sites and both mechanisms are the predicted ones, so no deviation from §2.2 was needed.
After the edits `pyright` on that file prints `0 errors, 0 warnings, 0 informations` — no
diagnostic lines at all.

## Diff and gates

| Gate | Observed |
|---|---|
| `pyright` (bare) | `0 errors, 0 warnings, 0 informations`; **29 files analysed** across the four islands (7 + 9 + 8 + 5 `.py` files) |
| `pyright tests/.../test_directory.py` | `0 errors` |
| `pyright` on all four packages + both test packages | `0 errors` |
| `ruff check` / `ruff format --check` on the test file | `All checks passed!` / `1 file already formatted` |
| `pytest tests/services/provider_channel tests/services/provider_directory -q` | `155 passed` · `89 passed` — unchanged |
| `git diff --numstat` | `pyproject.toml` **3 0**; `test_directory.py` **4 5** (see below) |
| `git status --porcelain` | ` M pyproject.toml`, ` M tests/.../test_directory.py`, `?? docs/reports/PB-H5-report.md` |
| Leak scan | Added lines of both edited files were extracted and every identifier-shaped token enumerated; the only hit is `ADR-0129`, a published repo ADR (the provider-channel one). No Mission Control working id appears. |

**Why `test_directory.py` is `+4 −5`, not the predicted `+3/+4 −2`.** `Any` is three
characters shorter than `object`, so after 2.2.a the signature fits inside the 100-column limit
and `ruff format` — itself a CI gate (ADR-0109 §2) — collapses its three lines into one, making
that change `−3 +1` rather than `−1 +1`. The other three lines are as specified: the typing
import (`−1 +1`), the inline `# pyright: ignore[reportIndexIssue]` (`−1 +1`), and one new comment
line. No assertion changed meaning; no test was added, removed or renamed; `155 passed` is the
same 155.

## Deliberately not touched

The record's §7 sentence "Not yet in the `[tool.pyright]` island set (housekeeping pending)"
(`docs/concepts/provider-channel-stage-b-decisions.md:860`) is now stale but **was left as-is** —
that is a docs-prompt change and follows with the next one. `docs/reports/PB-D5-report.md:74`
states the same thing as a historical record and is likewise untouched.

## Open questions

1. **The numstat prediction.** The prompt's `+3/+4 −2` did not account for the `ruff format`
   reflow. Nothing is wrong with the result, but when numstats are predicted by hand, an
   annotation change that shortens a line is worth treating as potentially reflow-triggering.
2. **`Any` vs. narrowing the helper.** `dict[str, Any]` is the smallest change that clears the
   splat and leaves `_provider` untouched as instructed. A `TypedDict(total=False)` over the
   entry shape would type the override precisely, but it must track the entry vocabulary — a
   build decision, not housekeeping.
