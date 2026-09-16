# PB-1g — `directory-refresh` and `directory-status` operator commands (SB-3b)

**Date:** 2026-09-16 · **Decisions:** B-D-13, B-D-15, B-D-24, D-clock
· **Base commit:** `d0393bb` (`feat(provider-directory): fetch client with
conditional GET, monotonic acceptance, file cache under DATA_DIR and
provenance (SB-3b, PB-1f)`)

---

## OPERATOR ACTION REQUIRED

**Nothing is staged.** No `git add`, no `git commit`, no branch operation was
performed. Stage and commit the four paths yourself:

```bash
git add cli/directory.py cli/__init__.py tests/cli/test_directory_commands.py docs/reports/PB-1g-report.md
```

```bash
git commit -m "feat(cli): directory-refresh and directory-status operator commands (SB-3b, PB-1g)"
```

No dependency changed and `pyproject.toml` is untouched, so no re-install is
required: `httpx` and `typer` were already runtime dependencies and
`pytest-httpx` was already a dev dependency. The two commands are available
from the existing `portfoliflow` console script as soon as the working tree is
on the path — they were exercised live below through the installed entry point.

---

## Verify-first values observed

All ten rows ran before anything was written. **No STOP condition.** Two
line-number mismatches, both in `cli/__init__.py` and both with every text
anchor matching, so §1 says proceed — they are recorded here and explained
under *Open questions*.

| # | Check | Expected | Observed |
|---|---|---|---|
| 1 | `git log --oneline -2` | line 1 ends `(SB-3b, PB-1f)`; line 2 starts `654b574` | `d0393bb feat(provider-directory): … (SB-3b, PB-1f)` / `654b574 docs(provider-channel): …` — match |
| 2 | `git status --porcelain` | empty | empty |
| 3 | `ls cli/directory.py tests/cli/test_directory*` | both absent | both "No such file or directory" |
| 4 | `grep -n 'from cli.create_user import\|from cli.inspect_tenant import\|name="vault-rotate-key"\|^The CLI connects to Postgres' cli/__init__.py` | 61, 62, 89, 50 | 61, 62, 89, **48** — three exact, the docstring paragraph two lines earlier. Text anchors all present, in the stated order. **Line-number mismatch, proceeded.** |
| 5 | `sed -n 45,48p cli/__init__.py` | the two `vault-…` bullets (`vault-generate-key` on 45, `vault-rotate-key` on 47) | the bullets are at **42** and **44**; lines 45–48 are the tail of the `vault-rotate-key` bullet (`under a new master key, cross-tenant, in one transaction` / `(ADR-0112 §2).`), the blank line, and `The CLI connects to Postgres …`. Both bullets exist, in the stated order and style. **Line-number mismatch (−3), proceeded.** |
| 6 | `grep -n` over the SB-3b library | 136 · 229, 91 · 123, 71 · 102 | `refresh_directory` 136; `read_cache` 229, `cache_root_for` 91; `provenance_to_dict` 123, `provenance_from_cache` 71; `REFRESH_STATUSES` 102 — all match |
| 7 | `grep -n 'def get_config' core/config.py`; `grep -n 'json_output: bool = typer.Option' cli/status.py` | 126 · 492 | 126 · 492 — match |
| 8 | `pytest --collect-only -q tests/services/provider_directory` | `89 tests collected` | `89 tests collected in 0.05s` — match |
| 9 | `pytest --collect-only -q tests/cli` | record N | **78 tests collected** before this prompt |
| 10 | `grep -rn 'httpx_mock' tests/cli \| wc -l` | record (expected 0) | **0** — this prompt introduces the first `httpx_mock` use under `tests/cli` |

---

## Diff and gates

### Diff

```
$ git diff --stat cli/__init__.py
 cli/__init__.py | 11 +++++++++++
 1 file changed, 11 insertions(+)
```

```
$ git status --porcelain
 M cli/__init__.py
?? cli/directory.py
?? tests/cli/test_directory_commands.py
?? docs/reports/PB-1g-report.md
```

`cli/__init__.py` took exactly the three insertions of §2.2 and no other line
changed: eight docstring lines after the end of the `vault-rotate-key` bullet,
one import after `from cli.create_user import …` (where alphabetical order
puts it anyway, between `create_user` and `inspect_tenant`, so `ruff check`'s
import rules are satisfied without reordering), and two `app.command(...)`
registrations after `vault-rotate-key`.

### Gate 1 — the module is database-free

```
$ pytest tests/cli/test_directory_commands.py -q
........................                                                 [100%]
24 passed in 2.13s
```

```
$ DATABASE_URL_SUPERUSER=postgresql://dead@127.0.0.1:1/x DATABASE_URL=postgresql://dead@127.0.0.1:1/x pytest tests/cli/test_directory_commands.py -q
........................                                                 [100%]
24 passed in 2.08s
```

**N = 24**, identical under both. One deviation to record honestly: the gate
asks for the first run "with Postgres down", and Postgres was **up** on this
host throughout (`podman ps` → `portfoliflow-postgres  Up 5 hours (healthy)`;
port 5432 open). Stopping a healthy container the operator has been running
for five hours is a change to their environment that this prompt does not
authorise, and gate 2 needs the server up again immediately afterwards. The
down-condition was therefore produced by the second command — dead DSNs on a
closed port, which `.env`'s `override=False` loading lets win — and that is the
stronger proof of the two: a connection attempt against `127.0.0.1:1` fails
whether or not a server happens to be listening on 5432. The module also
imports nothing that could connect:

```
$ grep -c 'superuser_engine\|cli._db\|sqlalchemy\|core.repositories' cli/directory.py
0
```

### Gate 2 — the CLI suite, Postgres up

```
$ pytest tests/cli -q
........................................................................ [ 70%]
..............................                                           [100%]
102 passed in 43.25s
```

**M = 102** — the 78 collected in verify row 9 plus this prompt's 24.

### Gate 3 — the libraries beneath are untouched

```
$ pytest tests/services/provider_directory -q
89 passed in 0.64s

$ pytest tests/services/provider_channel -q
155 passed in 1.72s
```

### Gate 4 — the live walk, offline

```
$ env -u DATABASE_URL -u DATABASE_URL_SUPERUSER portfoliflow directory-status --data-dir /tmp/pf-sb3b-empty
error: no cache — /tmp/pf-sb3b-empty/provider_directory/directory.json does not exist. Nothing has been fetched yet. Run 'portfoliflow directory-refresh'.
exit: 5
```

```
$ env -u DATABASE_URL -u DATABASE_URL_SUPERUSER portfoliflow directory-refresh --url http://127.0.0.1:9/directory.json --data-dir /tmp/pf-sb3b-empty
status: unavailable
notice: could not fetch http://127.0.0.1:9/directory.json: ConnectError: All connection attempts failed
provenance: none
exit: 4
```

The real `DIRECTORY_URL` was **not** called: the document is unpublished until
release `2026.09.1`. Both invocations ran with no `DATABASE_URL` and no
`DATABASE_URL_SUPERUSER` in the environment and neither attempted a connection.

Two further renders, beyond what the gate asks, because they are what an
operator will actually be looking at on publication day. Both are produced by
the installed command against a cache built offline from the **shipped public
fixture** (`tests/services/provider_channel/fixtures/directory-1.json` and its
public signature) — no private key exists in this repository and none was used.
A good copy:

```
$ env -u DATABASE_URL -u DATABASE_URL_SUPERUSER portfoliflow directory-status --data-dir /tmp/pf-sb3b-demo
provenance:
  directory_version: 1
  publishing_key_id: portfoliflow-2026-09
  fetched_at: 2026-09-16T13:49:14.668491+00:00
  issued_at: 2026-09-10
  valid_until: 2026-12-09
  etag: "directory-1"
  valid: true
  successor_in_use: false
  announced_successor: in_ring
  provider_count: 2
  source_url: https://portfoliflow.com/directory/v1/directory.json
  days_left: 84
exit: 0
```

And the same copy after flipping one byte-run inside a `display_name` — still
well-formed JSON, no longer the bytes that were signed (B-D-13):

```
$ env -u DATABASE_URL -u DATABASE_URL_SUPERUSER portfoliflow directory-status --data-dir /tmp/pf-sb3b-demo
error: a cached directory is present at /tmp/pf-sb3b-demo/provider_directory but unusable — it did not verify against the shipped key ring. Tampered, corrupt, or signed by a key this build does not carry; the disk is data, not trust (B-D-13), so the instance stands on nothing rather than on something it cannot check. Run 'portfoliflow directory-refresh'.
exit: 5
```

Both scratch directories live under `/tmp`; nothing was written into the tree.

### Gate 5 — help

Both `portfoliflow directory-refresh --help` and `portfoliflow
directory-status --help` exit 0 and render the exit-code list (0/2/3/4 and
0/2/5 respectively) in the Typer help body, above the options table. The
options tables show `--url`, `--data-dir`, `--timeout`, `--json` and
`--data-dir`, `--json`.

```
$ { portfoliflow directory-refresh --help; portfoliflow directory-status --help; } | grep -c 'Q-SB\|Board v'
0
```

### Gate 6 — lint and format

```
$ ruff check cli/directory.py cli/__init__.py tests/cli/test_directory_commands.py
All checks passed!

$ ruff format --check cli/directory.py cli/__init__.py tests/cli/test_directory_commands.py
3 files already formatted
```

No line in either new file exceeds the 100-column limit (`E501` is globally
ignored under the ADR-0109 §1 staged adoption, so this was checked directly
with `awk 'length > 100'` rather than left to the linter).

### Gate 7 — types

```
$ pyright cli/directory.py tests/cli/test_directory_commands.py
0 errors, 0 warnings, 0 informations
```

### Gate 8 — the working tree

Both listings are reproduced under *Diff* above and match the prompt exactly:
`1 file changed, 11 insertions(+)`, and the four expected `git status` entries
and no others.

---

## What the tests pin

24 tests, in five groups. Names state invariants rather than mechanics.

* **Help and registration** (3) — both `--help` screens exit 0 and name their
  flags; neither carries a working id; both commands appear on `portfoliflow
  --help`.
* **The exit-code map** (9) — parametrised over all eight `REFRESH_STATUSES`
  with a fake `refresh_directory`, asserting 0/0/3/3/3/3/3/4 and that the text
  render carries `status:` and `notice:` lines; plus one test that the
  expected-code tuple is still the same length as the library's status tuple,
  so a ninth status added upstream fails here rather than silently mapping to 0.
* **Caller errors** (3) — a `ValueError` and an `OSError` from the library both
  become exit 2 with the message on stderr; a `--url` that is not a `.json`
  document is refused with **no HTTP request made at all**, asserted through
  `httpx_mock.get_requests()`.
* **Plumbing** (3) — `--url`, `--data-dir` and `--timeout` arrive at the
  library; the cache root is `<data-dir>/provider_directory`; the default root
  comes from the `_default_data_dir` seam; the instant the library receives is
  the one `_now` returned.
* **End to end** (6) — the published fixture served over `httpx_mock`:
  `directory-refresh --json` reports `updated` with `directory_version` 1,
  `provider_count` 2 and a `days_left` computed from the document's own
  `valid_until`; `directory-status --json` then reports the identical
  provenance; a second refresh answered 304 to the stored `ETag`
  (`match_headers`) reports `unchanged`; a tampered cached document reports
  *present but unusable*; an empty data directory reports *no cache*; and both
  commands' `--json` output parses as exactly one object from `stdout` alone.

No literal date appears in the module: the instant is derived from the
fixture's own `issued_at` (noon UTC, one day later), following
`tests/services/provider_directory/conftest.py`, so an edited validity window
moves the tests with it.

---

## Open questions

Five things the prompt left open, and what was chosen.

**1. The `cli/__init__.py` line numbers were off, the text was not.** Verify
rows 4 and 5 expected the docstring anchors two and three lines further down
than the working tree has them (the import and registration anchors, 61/62/89,
were exact). Every text anchor matched, so §1's rule applied: proceed and
report. The insertion was made against the **text** anchor — immediately after
the `(ADR-0112 §2).` line that ends the `vault-rotate-key` bullet — not against
line 48, and the resulting diff is the specified `+11 −0`.

**2. Exit 2 is reachable from `directory-status` as well.** §2.1 specifies only
0 and 5 for the status command, but `read_cache` raises `OSError` when the data
directory exists and cannot be read — a permissions problem is an ordinary
operator condition, and a bare traceback is a poor answer to it. The command
therefore maps `(ValueError, OSError)` to exit 2 exactly as refresh does, and
its `--help` names all three codes. Nothing in the specified paths can reach
it: `_now()` is always timezone-aware, and a *missing* directory is the exit-5
"no cache" branch, not an `OSError`.

**3. The four exit-2 exception types are caught as two.**
`UnknownPublishingKeyId` and `PublishingKeyNotConfigured` both derive from
`DirectoryVerificationError`, which derives from `ValueError` (verified by
reading the MRO, not assumed), so `except (ValueError, OSError)` catches
everything §2.1 enumerates. They are not imported, which keeps the import list
exactly as §2.1 specifies and keeps `cli/` from naming trust-layer exception
types it does not otherwise touch. The docstring says so at the catch site.

**4. One name beyond the specified import list.** `Provenance` is imported from
`services.provider_directory` — for a type annotation only, on the one helper
that turns a provenance into `days_left`. The alternative was to inline the
same two-line computation in both commands or to re-parse the ISO string that
`provenance_to_dict` had just formatted; both are worse. It is the same
package as the other nine names and appears in its `__all__`. None of the
forbidden imports (`cli._db`, `superuser_engine`, `sqlalchemy`,
`core.repositories`, `configure_logging`) is present — gate 1 greps for that.

**5. `stdout` and `stderr` are genuinely separate, and the tests assert on
that.** Click 8.3 gives `Result.stdout`, `Result.stderr` and a mixed
`Result.output`. Every `--json` assertion parses `result.stdout` and every
error-wording assertion reads `result.stderr`, which pins the property §2.1
actually asks for — that stdout stays a clean paste — rather than merely the
presence of a string somewhere in the combined stream.

Nothing else changed: no `services/`, `web/`, `core/`, `pyproject.toml`,
`CLAUDE.md`, ADR, `docs/concepts/` file, template or taxonomy entry was
touched; no timer or scheduler hook was added; `provider_channel.enabled` is
not read anywhere in the new code (SB-6).
