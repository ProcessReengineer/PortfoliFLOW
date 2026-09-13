# PB-H3 — Housekeeping: retire the closed rerun-once exception, fix the consumer-status comment, drop the redundant asyncio marker

**Date:** 2026-09-13 · **Repository:** PortfoliFLOW (AGPL) · **Base:** `492c7fa`
**Scope:** comments in two files, one test marker deleted. No behaviour change, no ADR, no new tests.

## OPERATOR ACTION REQUIRED

```sh
git add .github/workflows/full-suite.yml web/routes/provider_credentials.py tests/services/test_tenant_resolution.py docs/reports/PB-H3-report.md
git commit -m "chore: retire the closed rerun-once exception in full-suite.yml, fix the consumer-status comment, drop the redundant asyncio marker (PB-H3)"
```

## Verify-first values observed

| # | Check | Expected | Observed |
|---|---|---|---|
| 1 | `git status --porcelain` | empty | empty ✅ |
| 2 | `git log -1 --format=%s` | `docs(reports): full-suite baseline 2026-09-11, …` | exact match ✅ |
| 3 | `git log -2 \| tail -1` | ends `(PB-H1)` | ends `(PB-H1)` ✅ |
| 4 | `wc -l` of the three files | 77 / 1149 / 161 | 77 / 1149 / 161 ✅ |
| 5 | `TWO known flake signatures are rerun-once exceptions` | one hit, line 11 | one hit, line 11 ✅ |
| 6 | `sed -n '6p;15p'` full-suite.yml | contract line / `red. Any other failure…` | both exact ✅ |
| 7 | `All three consumers have landed` | one hit, line 172 | one hit, line 172 ✅ |
| 8 | `sed -n '171p;175p'` provider_credentials.py | pill line / OpenRouter line | both exact ✅ |
| 9 | `grep -n pytestmark` | one hit `80:` | one hit `80:` ✅ |
| 10 | lines 79/81/82 blank | 3 | 3 ✅ |
| 11 | `sed -n '83p'` | `async def test_subdomain_resolver_finds_active_tenant(` | exact ✅ |
| 12 | `asyncio_mode` in pyproject | `129:asyncio_mode = "auto"` | `129:asyncio_mode = "auto"` ✅ |
| 13 | collect-only | `20 tests collected` | `20 tests collected in 0.53s` ✅ |
| 14 | run summary contains `13 warnings` | — | **`20 skipped in 0.46s`, 0 warnings — mismatch, see below** |
| 15 | `ls docs/reports \| grep -c PB-H3` | 0 | 0 ✅ |

Every text anchor matched, so per §1 this proceeded and the count mismatch is reported.

**Check 14 — a third outcome the prompt did not list.** Postgres is not running
(no `portfoliflow-postgres` container up), and the `superuser_engine` fixture
skips *every* test in the file, the 13 synchronous ones included, because
`tests/services/conftest.py` re-exports the DB fixtures into the module. So the
observed down-state is `20 skipped`, not the anticipated `13 passed, 7 skipped`.
With nothing executing, pytest-asyncio never emits the per-sync-test warning, so
the 13-warning baseline could not be reproduced here either.

I did **not** start the container: `reset_schema` truncates ~25 domain tables
(`tenants`, `users`, `investments`, …), so running this file with Postgres up
would wipe the operator's local deployment data and require a re-`bootstrap`.
That is not a side effect to take unasked for a comment-only change, and §3
explicitly allows reporting the DB-down result.

## Diff and gates

```diff
diff --git a/.github/workflows/full-suite.yml b/.github/workflows/full-suite.yml
index 5b5aaa2..0fbb048 100644
--- a/.github/workflows/full-suite.yml
+++ b/.github/workflows/full-suite.yml
@@ -3,16 +3,15 @@
 # Postgres 16 service container; the app role is applied via psql after
 # checkout (service containers cannot mount repo files), then Alembic
 # migrates to head. The regression-guard and migration-roundtrip family
-# runs as the FINAL named step: it is the public architectural contract,
-# and the roundtrip suites leave the schema downgraded (Strand-1 closure
-# §5) — nothing may depend on the database after it.
+# runs as the FINAL named step: it is the public architectural contract
+# (ADR-0109 §4). The roundtrip guards run on per-test scratch databases
+# (tests/regression/conftest.py) and leave the service database at head.
 #
-# continue-on-error is false. Until the flake-fix run (Chat D, #052
-# gate 6) lands, TWO known flake signatures are rerun-once exceptions
-# (ADR-0109 §4): the migration-roundtrip downgrade and AI-service
-# singleton pollution in combined runs. A failure matching one of them
-# is re-dispatched ONCE via workflow_dispatch before being treated as
-# red. Any other failure is red immediately.
+# continue-on-error is false and every failure is red immediately. The
+# rerun-once exception ADR-0109 §4 granted to two flake signatures is
+# closed (flake-fix closure, roadmap #052 gate 6): the roundtrip
+# downgrade flake ended with the scratch databases, and the AI-service
+# singleton flake ended with services/ai_service.py (ADR-0094).
 name: Full suite
 
 on:
diff --git a/tests/services/test_tenant_resolution.py b/tests/services/test_tenant_resolution.py
index 3f892bc..831bce1 100644
--- a/tests/services/test_tenant_resolution.py
+++ b/tests/services/test_tenant_resolution.py
@@ -77,9 +77,6 @@ def test_extract_subdomain_localhost_with_env(monkeypatch) -> None:
 # ---------------------------------------------------------------------------
 
 
-pytestmark = pytest.mark.asyncio
-
-
 async def test_subdomain_resolver_finds_active_tenant(
     superuser_engine: AsyncEngine,
     seed_tenant,
diff --git a/web/routes/provider_credentials.py b/web/routes/provider_credentials.py
index bee88c2..67a5819 100644
--- a/web/routes/provider_credentials.py
+++ b/web/routes/provider_credentials.py
@@ -169,8 +169,9 @@ _USER_ACTIONS: frozenset[str] = frozenset({"save", "delete"})
 _USER_PANEL_EXCLUDED: frozenset[tuple[str, str]] = frozenset({("telegram", "chat_id")})
 
 #: Whether anything reads a provider's rows *yet*. Rendered as a pill on
-#: every provider card. All three consumers have landed; the Telegram
-#: entry keeps the restart caveat, because the dispatcher set is
+#: every provider card. Every entry is live except ``provider_channel``,
+#: which stays dormant until its reader lands (ADR-0131 §4, SB-6). The
+#: Telegram entry keeps the restart caveat, because the dispatcher set is
 #: discovered once at bot start (ADR-0112 §5, D2) and a token written
 #: here is therefore not live the way an OpenRouter key is.
 _CONSUMER_STATUS: dict[str, str] = {
```

| Gate | Expected | Result |
|---|---|---|
| `ruff check` (2 files) | `All checks passed!` | `All checks passed!` ✅ |
| `ruff format --check` (2 files) | `2 files already formatted` | `2 files already formatted` ✅ |
| `pyright web/routes/provider_credentials.py` | 0 errors | `0 errors, 0 warnings, 0 informations` ✅ |
| collect-only | `20 tests collected` | `20 tests collected in 0.27s` ✅ |
| `-q -W error::pytest.PytestWarning` | `20 passed` / `13 passed, 7 skipped` | **`20 skipped in 0.40s`** — Postgres down; gate passes but is vacuous ⚠️ |
| `wc -l` of the three files | 76 / 1150 / 158 | 76 / 1150 / 158 ✅ |
| `git diff -U0` yml hunks | 1 | **2** — explained below ⚠️ |
| `git diff -U0` provider hunks | 1 | 1 (`@@ -172,2 +172,3 @@`) ✅ |
| `git diff --numstat` test file | `0	3` | `0	3` ✅ |
| `git diff --stat` | the three files | exactly the three files ✅ |
| `git status --porcelain` | 3× ` M` + `?? docs/reports/PB-H3-report.md` | as expected ✅ |

**The yml hunk count is an artefact of the replacement text, not a deviation.**
§2.1's nine replacement lines keep the bare `#` at line 9 byte-identical to the
old line 9, so at `-U0` git splits the change at that unchanged line:
`@@ -6,3 +6,3 @@` and `@@ -10,6 +10,5 @@`. Both old ranges lie inside lines
6–15 as the gate intends, and at default context it is a single hunk
(`@@ -3,16 +3,15 @@`). Expecting `1` at `-U0` was not achievable given the
prescribed replacement block. The file content is exactly as specified.

**Substitute evidence for the marker deletion**, since the warning gate could
not execute. Marker filters need no database:

- after the edit, `--collect-only -m asyncio` → **7/20 collected (13 deselected)**
- after the edit, `--collect-only -m 'not asyncio'` → **13/20 collected (7 deselected)**

That is the intended end state: `asyncio_mode = "auto"` auto-marks exactly the
7 async tests, and the 13 synchronous tests no longer carry the marker — which
is precisely the source of the 13 warnings. No decorator was added anywhere,
and `pytest` stays imported and used (`parametrize`, `raises`), so ruff is clean.

## Open questions

1. **The warning gate is unproven in this environment.** `20 passed` with
   Postgres up is the only run that actually demonstrates the 13 warnings are
   gone. If you want that before committing: `podman start portfoliflow-postgres`,
   then `pytest tests/services/test_tenant_resolution.py -q -W error::pytest.PytestWarning`
   — and note it truncates the dev DB, so re-`portfoliflow bootstrap` afterwards.
   The marker-filter evidence above is the strongest DB-free substitute.
2. **The `13 passed, 7 skipped` expectation in §3 is wrong for this repo.** The
   DB fixtures skip the whole module, sync tests included (a known property,
   documented in the fixture docstrings). Worth correcting in future prompts
   that quote a DB-down expectation for `tests/services/`, `tests/repositories/`,
   `tests/web/` or `tests/assistants/`.
3. **`full-suite.yml` line 5 now reads slightly loose.** The retained line ends
   "The regression-guard and migration-roundtrip family / runs as the FINAL named
   step" and the new text explains the scratch databases, so the *reason* it runs
   last is no longer stated (it used to be "nothing may depend on the database
   after it"). It still runs last as the architectural contract, which the new
   text does say. Left exactly as §2.1 prescribed; flagging in case you want the
   rationale restored in a follow-up.
