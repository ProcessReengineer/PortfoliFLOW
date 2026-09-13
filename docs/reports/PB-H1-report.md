# PB-H1 — Housekeeping (AGPL) · provider-credentials docstring drift and the two pyright errors

**Strand:** housekeeping. Two files, no behaviour change, no new tests.
**Prompt:** PB-H1, issued 2026-09-11 by Mission Control Stage B, chat 3
**Repository:** PortfoliFLOW (AGPL), `/home/soenke/Code/PortfoliFLOW/PortfoliFLOW`
**Run:** 2026-09-11. Both edits (§2.1, §2.2) applied and every §3 gate is green.
All nine verify-first checks matched their text. Two notes in §2: check 7's line
number is a Repomix block figure, proven by `cmp`, and check 8 needs the venv
activated.
**Git:** no operations performed. Read-only git only (`status`, `diff`, `log`,
`show`); the working tree carries the changes, and the commit is the operator's.
**Closes:** the pyright errors and the SB-4 docstring drift (the seventh
provider card, whose pill is *dormant*).

---

## 1. OPERATOR ACTION REQUIRED

Review and commit the three paths:

```sh
git add web/routes/provider_credentials.py \
        tests/web/test_provider_credentials.py \
        docs/reports/PB-H1-report.md
git commit -m "chore(provider-credentials): note the dormant pill in the module docstring; narrow the test engine URLs for pyright (PB-H1)"
```

Nothing else is required by this prompt. None of the open questions in §4
blocks the commit.

---

## 2. Verify-first values observed

| # | Check | Expected | Observed | |
|---|---|---|---|---|
| 1 | `git status --porcelain` | empty | empty | ✅ |
| 2 | `git log -1 --format=%s` | ends with `(PB-D3)` | `docs(provider-channel): record B-D-25/B-D-26, SB-3a/SB-4 addenda, D-SB2-16 correction and #067 progress (PB-D3)` | ✅ |
| 3 | `wc -l` route, test file | 1145, 1542 | 1145, 1542 | ✅ |
| 4 | voice-cards line; bullet end | `:55`; `:59` `  live when it is only stored.` | `:55`; `:59` `  live when it is only stored.` | ✅ |
| 5 | dormant pill string | `:178` | `:178` | ✅ |
| 6 | the two `create_async_engine(…)` sites | `:136`, `:147` | `:136`, `:147` | ✅ |
| 7 | `^def _url(` in test file; sibling helper | 0; `test_transactions_history.py:93` | 0; `test_transactions_history.py:92` | ✅ text; see note |
| 8 | `pyright tests/web/test_provider_credentials.py` | exactly 2 errors, `:136` and `:147` | exactly 2 errors, `:136:34` and `:147:34` (venv active) | ✅ see note |
| 9 | `ls docs/reports/ \| grep -c PB-H1` | 0 | 0 | ✅ |

The `wc -l` counts and the line numbers in checks 4–6 are working-tree figures
and matched exactly.

**Note on check 7.** The grep text matched: the helper exists once in the
sibling, and its four lines are the §2.2 block byte for byte. It sits at line
**92**, not 93. The 93 is a Repomix block line, with the `<file>` wrapper
counted as line 1. The proof:

- `repomix-output.xml` has 1,383 `<file>` entries, which matches the prompt
  header.
- The `tests/web/test_transactions_history.py` block, extracted between its
  wrapper lines, is `cmp`-identical to the tree file (1,167 lines).
- Inside that block, counting the opening `<file path=…>` as line 1,
  `def _url` is on line 93.

This is a counting artefact in a file PB-H1 does not edit, and the text is
exact, so I proceeded under the "text mismatch" rule, as PB-D2 did.

**Note on check 8.** `pyright` is not on `PATH`. Running `.venv/bin/pyright`
without activating the venv reports **4** different errors, all
`reportMissingImports` (`pytest`, `pytest_asyncio`, `sqlalchemy`,
`sqlalchemy.ext.asyncio`), because `[tool.pyright]` declares no
`venv`/`venvPath` and the wrapper does not find `.venv` on its own. With
`source .venv/bin/activate` the expected baseline reproduces exactly. The
before and after figures below are both from the activated venv (pyright
1.1.411).

**Check 8 output, before the edit:**

```
$ source .venv/bin/activate && pyright tests/web/test_provider_credentials.py
/home/soenke/Code/PortfoliFLOW/PortfoliFLOW/tests/web/test_provider_credentials.py
  /home/soenke/Code/PortfoliFLOW/PortfoliFLOW/tests/web/test_provider_credentials.py:136:34 - error: Argument of type "str | None" cannot be assigned to parameter "url" of type "str | URL" in function "create_async_engine"
    Type "str | None" is not assignable to type "str | URL"
      Type "None" is not assignable to type "str | URL"
        "None" is not assignable to "str"
        "None" is not assignable to "URL" (reportArgumentType)
  /home/soenke/Code/PortfoliFLOW/PortfoliFLOW/tests/web/test_provider_credentials.py:147:34 - error: Argument of type "str | None" cannot be assigned to parameter "url" of type "str | URL" in function "create_async_engine"
    Type "str | None" is not assignable to type "str | URL"
      Type "None" is not assignable to type "str | URL"
        "None" is not assignable to "str"
        "None" is not assignable to "URL" (reportArgumentType)
2 errors, 0 warnings, 0 informations
```

**After the edit** (the §3 gate, both files):

```
$ source .venv/bin/activate && pyright tests/web/test_provider_credentials.py web/routes/provider_credentials.py
0 errors, 0 warnings, 0 informations
```

(The "new pyright version available" banner is omitted from both outputs.)

---

## 3. Diff and gates

```
$ git diff --stat
 tests/web/test_provider_credentials.py | 10 ++++++++--
 web/routes/provider_credentials.py     |  6 +++++-
 2 files changed, 13 insertions(+), 3 deletions(-)
```

```diff
diff --git a/tests/web/test_provider_credentials.py b/tests/web/test_provider_credentials.py
index e1e1179..4ac5cba 100644
--- a/tests/web/test_provider_credentials.py
+++ b/tests/web/test_provider_credentials.py
@@ -116,6 +116,12 @@ def _clean_pairing_store() -> Any:
     telegram_pairing.reset_store()
 
 
+def _url(value: str | None) -> str:
+    """Narrow a configured URL to ``str``; ``_require_db`` already skipped if unset."""
+    assert value is not None
+    return value
+
+
 def _require_db() -> None:
     if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
         pytest.skip(
@@ -133,7 +139,7 @@ def _require_db() -> None:
 @pytest_asyncio.fixture
 async def fresh_superuser_engine() -> AsyncGenerator[AsyncEngine, None]:
     _require_db()
-    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
+    engine = create_async_engine(_url(DATABASE_URL_SUPERUSER), future=True, poolclass=NullPool)
     try:
         yield engine
     finally:
@@ -144,7 +150,7 @@ async def fresh_superuser_engine() -> AsyncGenerator[AsyncEngine, None]:
 async def app_engine() -> AsyncGenerator[AsyncEngine, None]:
     """An RLS-subject engine for reading rows back the way the app writes them."""
     _require_db()
-    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
+    engine = create_async_engine(_url(DATABASE_URL), future=True, poolclass=NullPool)
     try:
         yield engine
     finally:
diff --git a/web/routes/provider_credentials.py b/web/routes/provider_credentials.py
index 5b81985..bee88c2 100644
--- a/web/routes/provider_credentials.py
+++ b/web/routes/provider_credentials.py
@@ -54,7 +54,11 @@ Four rules this module exists to keep:
   bot restart**, because the dispatcher set is discovered once at start.
   The three voice cards are live as of #059 V3/V4 — the configuration is
   resolved per web turn and per Telegram voice message, so a save applies
-  without a restart and carries no caveat at all (ADR-0118 §8). A row an
+  without a restart and carries no caveat at all (ADR-0118 §8). The
+  provider-channel card (ADR-0131) is the first whose pill says
+  **dormant**: its one switch is declared and saved like every other row,
+  and nothing reads it until the channel's consumers land (Stage B, SB-6)
+  — the pill states that rather than implying a live effect. A row an
   operator writes must never *look* consumed when it is not — nor look
   live when it is only stored.
```

(`docs/reports/PB-H1-report.md` is untracked and therefore not in the diff.)

**Gates:**

| Gate | Result | |
|---|---|---|
| `pyright` on both files | `0 errors, 0 warnings, 0 informations` | ✅ |
| `ruff check` on both files | `All checks passed!` | ✅ |
| `ruff format --check` on both files | `2 files already formatted` | ✅ |
| `pytest tests/web/test_provider_credentials.py -q` (Postgres `portfoliflow-postgres` healthy) | **56 collected, 56 passed** in 116.72 s. Same count as before: `--collect-only` gave 56 before the run (41 `def test_` functions, parametrised to 56) | ✅ |
| `git diff --stat` | two files | ✅ |
| non-docstring-indented changed lines in `provider_credentials.py` | `0` | ✅ |
| `git status --porcelain` | ` M tests/web/test_provider_credentials.py`, ` M web/routes/provider_credentials.py`, `?? docs/reports/PB-H1-report.md` | ✅ |

**No-other-text-change proof (§2.1).** The module docstring was parsed from
`HEAD` and from the working tree and split into words. The new word sequence
equals the old one with the §2.1 text spliced in directly after `(ADR-0118 §8).`
→ `True`. The re-wrap moved words, but no word was added, dropped or changed
outside the insertion.

**Verbatim proof (§2.2).** `diff` of `test_transactions_history.py:92–95`
against `test_provider_credentials.py:119–122` is empty.

---

## 4. Open questions

1. **Re-wrap choice (§2.1).** "Re-wrap the bullet" allows either re-flowing the
   whole bullet or only the text after the splice. I did the second, at the
   docstring's fill column of 74 characters. The widest breakable lines are 74;
   the one 80-character line is an unbreakable `:data:` reference.
   - Lines 48–56 are unchanged.
   - The greedy wrap put the last two lines back exactly as they were, so the
     diff is one changed line plus four inserted ones.
   - The inserted prose therefore starts mid-line (`… (ADR-0118 §8). The`)
     rather than on its own line as the prompt's block did. The words are the
     prompt's exactly (proof in §3).
2. **A second drifted text in the same file, not touched.** The comment above
   `_CONSUMER_STATUS` (now `:171`–`:175`, was `:167`–`:171`) still reads "All
   three consumers have landed; the Telegram entry keeps the restart caveat…".
   The dict now has seven entries, one of them dormant, and only the inline
   comment on the `provider_channel` entry (`:179`–`:180`) says so. It is a
   `#:` comment, not the module docstring, so PB-H1's scope excluded it. It is
   a candidate for the next housekeeping pass. The docstring bullet's own
   "All three are live as of F5" is framed as history and still reads
   correctly after the insertion.
3. **Mixed line-number provenance in one table.** Checks 3–6 quote working-tree
   figures and check 7 quotes a Repomix block figure (§2 note). PB-D3 had
   only tree figures, and PB-D2 had Repomix `+2` counts. It would help if
   Mission Control stated per table, or per check, which source a line number
   comes from, or always converted to tree figures. The PB-D2 lesson on the
   Repomix offset is still pending ratification.
4. **How verify-first should invoke pyright.** A bare `pyright …` fails here
   (`command not found`), and `.venv/bin/pyright …` without activation gives a
   misleading 4-error `reportMissingImports` result (§2 note). Future prompts
   could spell out `source .venv/bin/activate && pyright …`. Declaring the venv
   in `[tool.pyright]` would also work locally, but CI installs into the
   runner's Python with no `.venv`, so that is a config decision for Mission
   Control and out of scope here.
5. **The pyright fix is done for this file only. The pattern is wider.**
   - 67 test files (53 under `tests/web/`) still pass an un-narrowed
     `DATABASE_URL` or `DATABASE_URL_SUPERUSER` straight to
     `create_async_engine`.
   - Five files now carry the `_url` helper: this one and four
     `tests/web/test_transactions_*.py`.
   - None of this is a gate failure. CI's pyright covers only the typing
     islands (`services/overlay`, `services/market_data`), and `tests/` is not
     among them.

   If Mission Control wants `tests/web` pyright-clean, a shared helper in a
   conftest or `tests/_db_fixtures` would beat 67 copies. That is a decision
   for the board, not for this prompt.
6. **The helper's docstring holds in this file.** "``_require_db`` already
   skipped if unset" is accurate here as well. Both fixtures call
   `_require_db()` on the line before the engine is built, and `_require_db`
   skips when either URL is unset. The `assert` can therefore only fire if a
   future fixture forgets that call.
