# PB-H6 — CI parity: CLI help tests and the import-contract baseline

**Date:** 2026-09-22 · **Executor:** Claude Code · **Prompt:** PB-H6 (v2) · **Status:** completed, all gates green

---

## Operator actions

1. Stage and commit the two test files:

   ```bash
   git add tests/cli/test_directory_commands.py tests/services/provider_channel/test_contract.py \
           docs/reports/PB-H6-report.md
   git commit -m "test: make CLI help and import-contract tests CI-independent (PB-H6)"
   ```

2. **Watch the next CI run for F3.** F1 and F2 were reproduced locally (check 4) and are green after
   the change, under both `GITHUB_ACTIONS=true` and `FORCE_COLOR=1`. **F3 could not be reproduced
   locally** — this venv is Python 3.13.14 and carries no `distutils-precedence.pth`, so
   `_distutils_hack` is never loaded here (check 6). The fix is proven instead by the two-directions
   scratch run below, which shows the baseline suppresses a pre-loaded module and still catches one
   the package itself pulls in. Confirm on the next full-suite run.

3. Nothing else changed. The nine pre-existing dirty paths from the UX A-0 work are untouched.

---

## 1 · Verify-first checks

| # | Check | Result |
|---|---|---|
| 1 | `git log` contains the vendor commit | **PASS** — `e2fd660 build(web): vendor HTMX, Plotly, Tabulator and the Lucide icon set locally; pf_icon Jinja global (UX A-0, P-UX-A0v)`, at HEAD |
| 2 | Neither target file dirty | **PASS** — 10 modified + 1 untracked path, none of them a target. REPORTed below |
| 3 | Typer `FORCE_TERMINAL` lines | **PASS — hypothesis confirmed.** typer 0.25.1, rich 15.0.0, click 8.3.3. Quoted below |
| 4 | `GITHUB_ACTIONS=true pytest tests/cli/test_directory_commands.py -q` | **PASS — reproduced.** `2 failed, 22 passed in 6.27s`; exactly F1 and F2, nothing else in the module |
| 5 | `test_contract.py` snippet and allow-list unchanged | **PASS** — `"import services.provider_channel  # noqa: F401\n"` directly followed by `"names = sorted(\n"`; `_ALLOWED_THIRD_PARTY == {"_cffi_backend", "_openssl", "_sodium", "cffi", "cryptography", "nacl"}` |
| 6 | venv Python; `distutils-precedence.pth` present? | **Python 3.13.14; NO.** Only `__editable__.portfoliflow-2026.9.0.pth` and `__editable__.portfolioflow-0.1.0.pth` are installed. **F3 does not reproduce locally** |
| 7 | pyright before-value | **4 errors** — all `reportMissingImports` (`pytest` ×2, `pytest_httpx`, `typer.testing`); pre-existing, pyright does not resolve the venv here |

**Check 2 — dirty paths REPORTed, not touched** (all from the UX A-0 strand):
`config/chart_theme.json`, `config/ui_theme.json`, `config/ui_theme_corporate_blue.json`,
`config/ui_theme_light.json`, `tools/ux_atlas.py`, `web/static/css/base.css`,
`web/static/css/theme.css`, `web/static/vendor/README.md`, `web/templates/_auth_base.html`,
`web/templates/base.html`, and the untracked `docs/reports/P-UX-A0a-report.md`.

### Check 3 — the `FORCE_TERMINAL` lines, verbatim

From `.venv/lib/python3.13/site-packages/typer/rich_utils.py`, lines 74–81:

```python
_TYPER_FORCE_DISABLE_TERMINAL = getenv("_TYPER_FORCE_DISABLE_TERMINAL")
FORCE_TERMINAL = (
    True
    if getenv("GITHUB_ACTIONS") or getenv("FORCE_COLOR") or getenv("PY_COLORS")
    else None
)
if _TYPER_FORCE_DISABLE_TERMINAL:
    FORCE_TERMINAL = False
```

and its one consumer, line 157 (inside the console factory):

```python
        force_terminal=FORCE_TERMINAL,
```

**This confirms the hypothesis exactly.** Both `FORCE_TERMINAL` and `_TYPER_FORCE_DISABLE_TERMINAL`
are module-level, evaluated **once when `typer.rich_utils` is imported** — long before any test runs.
An `env=` on `runner.invoke` therefore cannot switch colour off, and neither could
`monkeypatch.setenv`. The assertion side has to be the robust one, which is what §2.1 does.

The observed failure mode under `GITHUB_ACTIONS=true`:

```
E       AssertionError: assert '--url' in '\x1b[1m                    …
E        +  where '\x1b[1m …  \x1b[2m╰────────…────╯\x1b[0m\n\n' = <Result okay>.output
```

---

## 2 · Changes

### 2.1 F1/F2 — `tests/cli/test_directory_commands.py`

Added `import re`, and a module-private helper placed with the other helpers, immediately before the
`Help` section that uses it:

```python
#: Typer forces a Rich terminal under ``GITHUB_ACTIONS``/``FORCE_COLOR``, which
#: styles option names in fragments — so ``--data-dir`` is no longer contiguous.
_ANSI: Final[re.Pattern[str]] = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain(text: str) -> str:
    """Strip ANSI escape sequences, so a help assertion holds in any terminal mode.

    ``typer.rich_utils`` decides ``FORCE_TERMINAL`` when it is imported, so no
    ``env=`` on the invocation can switch the styling off again — the assertion
    side has to be the robust one.

    Args:
        text: Captured command output, styled or not.

    Returns:
        The same text with every CSI escape sequence removed.
    """
    return _ANSI.sub("", text)
```

The two help tests now invoke with `env={"COLUMNS": "200"}` and assert on `_plain(result.output)` —
the three (resp. two) positive flag checks **and** the `FORBIDDEN_IN_HELP` loop. The `exit_code`
message keeps the raw `result.output`, as specified:

```python
def test_refresh_help_names_its_flags_and_carries_no_working_id() -> None:
    result = runner.invoke(app, ["directory-refresh", "--help"], env={"COLUMNS": "200"})
    output = _plain(result.output)

    assert result.exit_code == 0, result.output
    assert "--url" in output
    assert "--data-dir" in output
    assert "--json" in output
    for forbidden in FORBIDDEN_IN_HELP:
        assert forbidden not in output
```

`test_status_help_names_its_flags_and_carries_no_working_id` is the same shape with `--data-dir` and
`--json`.

Two notes on what the width buys, since it is not what makes the tests pass:

- Stripping ANSI alone is already enough for the positive checks at the default 80 columns — verified
  directly. `COLUMNS=200` is there for the reason the prompt gives: at 80 columns the Options panel
  wraps (`…/directory/v1/directory.js…` is visibly truncated), and a wrap could split a
  `FORBIDDEN_IN_HELP` needle across two lines and hide it. At 200 columns nothing wraps, so the
  negative check is **stricter** than before, which is intended.
- Rich honours `COLUMNS` at render time even with `force_terminal=True` (`MAX_WIDTH` is `None`
  because `TERMINAL_WIDTH` is unset), so the `env=` does reach it. Confirmed: under
  `GITHUB_ACTIONS=true` with `COLUMNS=200`, `_plain(output)` contains **0** residual `\x1b` bytes,
  all positives are `True`, and all three of `Q-SB`, `Board v`, `register` are `False`, for both
  commands.

Not done, per the prompt: no monkeypatching of Typer internals, no Typer/Rich pin, no `NO_COLOR` in
the workflow, no change to `cli/directory.py`, and the other `--help` tests
(`test_both_commands_are_registered_on_the_operator_cli`, `test_irene_tick.py`,
`test_market_data_tick.py`) are untouched.

### 2.2 F3 — `tests/services/provider_channel/test_contract.py`

**Snippet before:**

```python
    code = (
        "import importlib.util\n"
        "import sys\n"
        "import services.provider_channel  # noqa: F401\n"
        "names = sorted(\n"
        "    {m.split('.')[0] for m in sys.modules}\n"
        "    - set(sys.stdlib_module_names)\n"
        "    - {'services', '__main__'}\n"
        ")\n"
```

**Snippet after:**

```python
    code = (
        "import importlib.util\n"
        "import sys\n"
        "before = {m.split('.')[0] for m in sys.modules}\n"
        "import services.provider_channel  # noqa: F401\n"
        "names = sorted(\n"
        "    {m.split('.')[0] for m in sys.modules}\n"
        "    - before\n"
        "    - set(sys.stdlib_module_names)\n"
        "    - {'services', '__main__'}\n"
        ")\n"
```

Everything downstream is unchanged: the stdlib subtraction, the `'services', '__main__'` exclusion,
the `__editable__` skip, the `find_spec` filter, `_ALLOWED_THIRD_PARTY`, and both assertions. No
named exception for `_distutils_hack`, no wildcard.

Docstring, sentence added to the closing paragraph:

> Modules already loaded when the interpreter starts — site `.pth` shims such as setuptools'
> `_distutils_hack`, which a CI interpreter has and a venv may not — are the interpreter's, not the
> package's, and are taken as the baseline.

#### Proof in both directions (scratch run, nothing written)

The driver reads the `code` literal back out of the shipped test with `ast.literal_eval`, so the
proof runs the snippet as committed rather than a retyped copy, then executes each variant via
`.venv/bin/python -c` with `cwd` at the repo root:

```
(0) unmodified — the shipped snippet
    third_party = ['_cffi_backend', '_sodium', 'cryptography', 'nacl']
    'rich' present = False   undeclared = []
(a) `import rich` BEFORE the baseline line
    third_party = ['_cffi_backend', '_sodium', 'cryptography', 'nacl']
    'rich' present = False   undeclared = []
(b) `import rich` AFTER the package import
    third_party = ['_cffi_backend', '_sodium', 'cryptography', 'nacl', 'rich']
    'rich' present = True   undeclared = ['rich']
```

- **(a)** a module already in `sys.modules` before the baseline is taken — the stand-in for
  `_distutils_hack` — is subtracted away and never reaches the allow-list check. This is the F3 fix.
- **(b)** the same module, when it arrives *because of* `import services.provider_channel`, still
  appears and still falls outside `_ALLOWED_THIRD_PARTY`. The guard has not been blunted.

Note that (a) reproduces (0) exactly, which is the point: the baseline removes the interpreter's own
start-up noise and changes nothing else.

---

## 3 · Gates

| Gate | Result |
|---|---|
| 1 · `ruff check` / `ruff format --check` on the two files | **clean** — `All checks passed!` / `2 files already formatted` |
| 2 · `pyright` on the two files | **4 errors** — equal to check 7's before-value, no new errors |
| 3 · `pytest` both paths, plain **and** `GITHUB_ACTIONS=true` | **green both ways**, 0 skipped |
| 4 · `git status --porcelain` | only the two test files beyond what check 2 REPORTed |

```
$ .venv/bin/ruff check tests/cli/test_directory_commands.py tests/services/provider_channel/test_contract.py
All checks passed!
$ .venv/bin/ruff format --check tests/cli/test_directory_commands.py tests/services/provider_channel/test_contract.py
2 files already formatted
```

```
$ .venv/bin/pyright tests/cli/test_directory_commands.py tests/services/provider_channel/test_contract.py
tests/cli/test_directory_commands.py:34:8 - error: Import "pytest" could not be resolved
tests/cli/test_directory_commands.py:35:6 - error: Import "pytest_httpx" could not be resolved
tests/cli/test_directory_commands.py:36:6 - error: Import "typer.testing" could not be resolved
tests/services/provider_channel/test_contract.py:37:8 - error: Import "pytest" could not be resolved
4 errors, 0 warnings, 0 informations
```

The four are the same four as before the change, shifted one line by the added `import re`.

**Gate 3, both runs:**

```
$ .venv/bin/pytest tests/cli/test_directory_commands.py tests/services/provider_channel -q
179 passed in 5.51s

$ GITHUB_ACTIONS=true .venv/bin/pytest tests/cli/test_directory_commands.py tests/services/provider_channel -q
179 passed in 4.90s
```

**179 passed, 0 skipped, 0 failed in both.** Both paths are DB-free as documented, and no warnings
summary was emitted for either run — so no new pytest warning to name.

Additionally, the other trigger in the same condition:

```
$ FORCE_COLOR=1 .venv/bin/pytest tests/cli/test_directory_commands.py -q
24 passed in 2.21s
```

**Gate 4 — `git status --porcelain`:**

```
 M config/chart_theme.json
 M config/ui_theme.json
 M config/ui_theme_corporate_blue.json
 M config/ui_theme_light.json
 M tests/cli/test_directory_commands.py          ← PB-H6
 M tests/services/provider_channel/test_contract.py   ← PB-H6
 M tools/ux_atlas.py
 M web/static/css/base.css
 M web/static/css/theme.css
 M web/static/vendor/README.md
 M web/templates/_auth_base.html
 M web/templates/base.html
?? docs/reports/P-UX-A0a-report.md
```

(plus this report, `docs/reports/PB-H6-report.md`, once written.)

**`git diff --stat`:**

```
 tests/cli/test_directory_commands.py             | 42 +++++++++++++++++++-----
 tests/services/provider_channel/test_contract.py |  7 +++-
 2 files changed, 39 insertions(+), 10 deletions(-)
```

No product code, no dependency change, no workflow change, no git operations.

---

## 4 · Out of scope, as instructed

- **`HTTP_422_UNPROCESSABLE_ENTITY`** (six sites in `web/routes/watch_desk.py`, one in
  `web/routes/market_data.py`) — not touched. Stage B record §6 addition (4) binds this to the
  literal `status_code=422` inside the next build prompt that touches those files.
- **numpy/scipy pytest warnings** — left alone. Neither of the two paths under gate 3 emitted a
  warnings summary at all, so there is no new warning to name.
- **Workflow edits** — none. The fix lives entirely on the assertion side, so the tests pass in any
  environment rather than in one configured to be quiet.
- **Git operations** — none. The operator commits.

### Follow-up line

`.github/workflows/full-suite.yml` runs `runs-on: ubuntu-latest` with `actions/checkout@v4` and
`actions/setup-python@v5`; the Node 20 action-runtime deprecation and the `ubuntu-latest` → Ubuntu 26
migration notice both apply and will need a bump (`checkout@v5`, `setup-python@v6`, and a decision on
pinning the runner image) — worth its own housekeeping prompt, not this one.
