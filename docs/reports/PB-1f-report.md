# PB-1f — Directory fetch client, file cache and provenance (SB-3b)

**Date:** 2026-09-16 · **Decisions:** B-D-13, B-D-14, B-D-15, B-D-23, D-clock
· **Base commit:** `654b574` (`docs(provider-channel): record B-D-10/12/24
addenda, §6 process additions, §7 coordinates post-SB-5, #067 progress and
change-log row, format doc after the sealed box (PB-D4)`)

---

## OPERATOR ACTION REQUIRED

**Nothing is staged.** No `git add`, no `git commit`, no branch operation was
performed. Stage and commit the four paths yourself:

```bash
git add services/provider_channel/directory.py services/provider_directory tests/services/provider_directory docs/reports/PB-1f-report.md
```

```bash
git commit -m "feat(provider-directory): fetch client with conditional GET, monotonic acceptance, file cache under DATA_DIR and provenance (SB-3b, PB-1f)"
```

No dependency changed, so no re-install is required: `httpx` and
`cryptography` were already runtime dependencies and `pytest-httpx` was
already a dev dependency. `pyproject.toml` is untouched.

---

## Verify-first values observed

All eleven rows ran before anything was written. **No STOP condition and no
count mismatch** — every text anchor and every count matched exactly.

| # | Check | Expected | Observed |
|---|---|---|---|
| 1 | `git log --oneline -2` | line 1 ends `(PB-D4)`; line 2 starts `395b157` | `654b574 docs(provider-channel): … (PB-D4)` / `395b157 feat(provider-channel): …` — match |
| 2 | `git status --porcelain` | empty | empty |
| 3 | `ls services/provider_directory tests/services/provider_directory` | both absent | both "No such file or directory" |
| 4 | `grep -n 'data_dir: str = field' core/config.py` | one hit, line 96 | line 96, `os.getenv("DATA_DIR", "data")` — match |
| 5 | `grep -n 'def verify_directory_with_ring' …/ring.py`; `grep -c 'current_key_id: str = PUBLISHING_KEY_ID' …/ring.py` | line 122; count 1 | line 122; count 1 |
| 6 | `wc -c` on the two fixtures | 1180 and 129 | 1180 and 129 |
| 7 | `grep -n 'nothing in Stage A encrypts' …/directory.py` | exactly one hit, line 146 | one hit, line 146 |
| 8 | `grep -n 'pytest-httpx' pyproject.toml`; `grep -n '"httpx>=' pyproject.toml` | line 61; line 25 | line 61 `"pytest-httpx>=0.35",`; line 25 `"httpx>=0.27",` |
| 9 | `grep -n '    "httpx",' …/provider_channel/test_contract.py` | line 172 | line 172 |
| 10 | `pytest --collect-only -q tests/services/provider_channel` | `155 tests collected`, per file 24 · 36 · 9 · 6 · 16 · 43 · 21 | `155 tests collected`; per file **24** (`test_contract`) · **36** (`test_directory`) · **9** (`test_export`) · **6** (`test_prefill`) · **16** (`test_ring`) · **43** (`test_schemas`) · **21** (`test_sealed_box`) — exact match |
| 11 | `grep -n 'pytest-httpx\|httpx_mock' -r tests` (informational) | may be empty | **not empty** — `httpx_mock` is already used in the tree, e.g. `tests/assistants/test_one_shot_extraction_loop_safe.py:70`. The fixture and its marker are an established idiom here, not a new one. |

Installed `pytest-httpx` is **0.36.2**, so the option named in the prompt is
spelled as the marker `@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)`
(0.36 also offers the per-response `is_optional=`; the marker was used, being
the literal option name).

---

## Diff and gates

### What changed

```
 M services/provider_channel/directory.py   (+2 −1, docstring only)
?? services/provider_directory/             (5 files, 1347 lines)
?? tests/services/provider_directory/       (7 files, 2124 lines)
?? docs/reports/PB-1f-report.md
```

| New file | Lines |
|---|---|
| `services/provider_directory/__init__.py` | 106 |
| `services/provider_directory/fetch.py` | 246 |
| `services/provider_directory/cache.py` | 502 |
| `services/provider_directory/provenance.py` | 171 |
| `services/provider_directory/refresh.py` | 322 |
| `tests/services/provider_directory/__init__.py` | 2 |
| `tests/services/provider_directory/conftest.py` | 163 |
| `tests/services/provider_directory/test_contract.py` | 184 |
| `tests/services/provider_directory/test_fetch.py` | 236 |
| `tests/services/provider_directory/test_cache.py` | 384 |
| `tests/services/provider_directory/test_provenance.py` | 248 |
| `tests/services/provider_directory/test_refresh.py` | 907 |

### Gate 1 — no dependency change

```
$ pip install -e ".[dev]"
Successfully installed portfoliflow-2026.9.0
$ git diff --stat pyproject.toml
(empty)
```

### Gate 2 — the two suites

```
$ pytest tests/services/provider_directory -q
89 passed in 0.63s

$ pytest tests/services/provider_channel -q
155 passed in 1.70s
```

**N = 89** for the new suite. Per-file collected counts:

| File | Collected |
|---|---|
| `test_contract.py` | 3 |
| `test_fetch.py` | 24 |
| `test_cache.py` | 20 |
| `test_provenance.py` | 7 |
| `test_refresh.py` | 35 |
| **total** | **89** |

**Same N with Postgres up or down.** The compose container was running
(`portfoliflow-postgres Up 4 hours (healthy)`) and was **not** stopped — the
operator's dev database was left alone. Instead both suites were re-run with
`DATABASE_URL` / `DATABASE_URL_SUPERUSER` pointed at an unreachable endpoint,
which is what the DB fixtures actually consult (`tests/_db_fixtures.py` calls
`load_dotenv` without `override`, so the environment wins):

```
$ DATABASE_URL=…@127.0.0.1:1/nodb DATABASE_URL_SUPERUSER=…@127.0.0.1:1/nodb \
    pytest tests/services/provider_directory -q
89 passed in 0.63s

$ … pytest tests/services/provider_channel -q
155 passed in 1.79s
```

The control that makes this probative: with the same unreachable endpoint, a
DB-backed repository test *notices*, and the new suite does not.

```
$ … pytest tests/repositories/test_anlv_category_repository.py -q -rs
SKIPPED [1] …:35: Cannot reach Postgres at 'postgresql+asyncpg://nobody:nobody@127.0.0.1:1/nodb':
  [Errno 111] Connect call failed ('127.0.0.1', 1). Is the compose container running? …
3 skipped in 0.48s
```

Had the inherited autouse `reset_schema` still been in force under
`tests/services/provider_directory/`, all 89 would have taken that same skip
path rather than running. The D-70 shadow is doing its job.

### Gate 3 — C-2 byte-identical

```
$ pytest tests/services/provider_channel/test_contract.py -q
24 passed in 1.61s

$ git diff --stat tests/services/provider_channel/
(empty)
```

### Gate 4 — ruff

```
$ ruff check services/provider_directory tests/services/provider_directory services/provider_channel/directory.py
All checks passed!

$ ruff format --check services/provider_directory tests/services/provider_directory services/provider_channel/directory.py
13 files already formatted
```

(`ruff format` was run once on those paths during the work — five files were
reformatted, the suites re-run green afterwards, and `--check` is clean.)

### Gate 5 — pyright

```
$ pyright services/provider_directory tests/services/provider_directory services/provider_channel/directory.py
0 errors, 0 warnings, 0 informations
```

Run over these three paths only, per the prompt: `tests/services/provider_channel/test_directory.py`
carries two pre-existing, deliberate errors outside the ADR-0110 island set
(PB-1e report, Gate 5).

### Gate 6 — the pure package is untouched, and no wire version was invented

```
$ python -c "import services.provider_channel as p; print(len(p.__all__))"
76

$ grep -rn 'SCHEMA_VERSION\s*[:=]' services/provider_directory
services/provider_directory/cache.py:70:META_SCHEMA_VERSION: Final[int] = 1
services/provider_directory/cache.py:167:        if schema_version != META_SCHEMA_VERSION:
```

Both hits are `META_SCHEMA_VERSION` — the sidecar's *file layout* version. No
wire-format version is declared anywhere in the new package; the directory's
own `format_version` remains `services.provider_channel`'s to state.

### Gate 7 — working tree

```
$ git status --porcelain
 M services/provider_channel/directory.py
?? services/provider_directory/
?? tests/services/provider_directory/
?? docs/reports/PB-1f-report.md
```

(The report line appears once this file is written; the other three were the
state at the moment the gates ran.)

### Gate 8 — the one docstring

```
$ git diff --stat services/provider_channel/directory.py
 services/provider_channel/directory.py | 3 ++-
 1 file changed, 2 insertions(+), 1 deletion(-)
```

```diff
@@ -143,7 +143,8 @@ class ProviderEntry:
         encryption_public_key: 64 hex characters (32 bytes). Length-checked
-            only; nothing in Stage A encrypts.
+            at parse; :func:`~services.provider_channel.export.seal_export`
+            seals order exports to this key (B-D-12, SB-5).
```

### The `sys.modules` delta, verbatim

Printed by `test_contract.py::test_the_import_graph_reaches_http_and_the_pure_package_and_nothing_forbidden`
(`pytest -s`). 273 modules:

```
_ast, _bisect, _blake2, _bz2, _cffi_backend, _colorize, _compat_pickle,
_compression, _datetime, _decimal, _hashlib, _json, _locale, _lzma, _opcode,
_opcode_metadata, _openssl, _openssl.lib, _pickle, _random, _socket,
_sodium, _sodium.lib, _ssl, _string, _struct, _tokenize, _typing,
_weakrefset, array, ast, atexit, base64, binascii, bisect, bz2, calendar,
click, click._compat, click._utils, click.core, click.decorators,
click.exceptions, click.formatting, click.globals, click.parser,
click.termui, click.types, click.utils, colorsys, copy, cryptography,
cryptography.__about__, cryptography.exceptions, cryptography.hazmat,
cryptography.hazmat.bindings, cryptography.hazmat.bindings._rust,
cryptography.hazmat.primitives,
cryptography.hazmat.primitives._serialization,
cryptography.hazmat.primitives.asymmetric,
cryptography.hazmat.primitives.asymmetric.ed25519,
cryptography.hazmat.primitives.hashes, cryptography.utils, dataclasses,
datetime, decimal, dis, email, email._encoded_words, email._parseaddr,
email._policybase, email.base64mime, email.charset, email.encoders,
email.errors, email.feedparser, email.header, email.iterators,
email.message, email.parser, email.quoprimime, email.utils, fractions,
gettext, hashlib, http, http.client, http.cookiejar, httpx,
httpx.__version__, httpx._api, httpx._auth, httpx._client, httpx._config,
httpx._content, httpx._decoders, httpx._exceptions, httpx._main,
httpx._models, httpx._multipart, httpx._status_codes, httpx._transports,
httpx._transports.asgi, httpx._transports.base, httpx._transports.default,
httpx._transports.mock, httpx._transports.wsgi, httpx._types,
httpx._urlparse, httpx._urls, httpx._utils, idna, idna.core, idna.idnadata,
idna.intranges, idna.package_data, importlib.abc, importlib.metadata,
importlib.metadata._collections, importlib.metadata._functools,
importlib.metadata._itertools, importlib.metadata._meta,
importlib.resources, importlib.resources._common,
importlib.resources._functional, importlib.resources.abc, inspect,
ipaddress, json, json.decoder, json.encoder, json.scanner, locale, logging,
lzma, math, mimetypes, mmap, nacl, nacl._sodium, nacl.bindings,
nacl.bindings.crypto_aead, nacl.bindings.crypto_box,
nacl.bindings.crypto_core, nacl.bindings.crypto_generichash,
nacl.bindings.crypto_hash, nacl.bindings.crypto_kx,
nacl.bindings.crypto_pwhash, nacl.bindings.crypto_scalarmult,
nacl.bindings.crypto_secretbox, nacl.bindings.crypto_secretstream,
nacl.bindings.crypto_shorthash, nacl.bindings.crypto_sign,
nacl.bindings.randombytes, nacl.bindings.sodium_core, nacl.bindings.utils,
nacl.encoding, nacl.exceptions, nacl.public, nacl.utils, numbers, opcode,
pickle, pygments, pygments.filter, pygments.filters, pygments.lexer,
pygments.lexers, pygments.lexers._mapping, pygments.modeline,
pygments.plugin, pygments.regexopt, pygments.style, pygments.styles,
pygments.styles._mapping, pygments.token, pygments.util, quopri, random,
rich, rich._emoji_replace, rich._export_format, rich._extension,
rich._fileno, rich._log_render, rich._loop, rich._null_file, rich._palettes,
rich._pick, rich._ratio, rich._spinners, rich._unicode_data,
rich._unicode_data._versions, rich._wrap, rich.align, rich.ansi, rich.box,
rich.cells, rich.color, rich.color_triplet, rich.console, rich.constrain,
rich.containers, rich.control, rich.default_styles, rich.emoji, rich.errors,
rich.file_proxy, rich.filesize, rich.highlighter, rich.jupyter, rich.live,
rich.live_render, rich.markup, rich.measure, rich.padding, rich.pager,
rich.palette, rich.progress, rich.progress_bar, rich.protocol, rich.region,
rich.repr, rich.screen, rich.segment, rich.spinner, rich.style, rich.styled,
rich.syntax, rich.table, rich.terminal_theme, rich.text, rich.theme,
rich.themes, select, selectors, services, services.provider_channel,
services.provider_channel.directory, services.provider_channel.export,
services.provider_channel.prefill, services.provider_channel.publishing_key,
services.provider_channel.ring, services.provider_channel.schemas,
services.provider_channel.sealed_box, services.provider_directory,
services.provider_directory.cache, services.provider_directory.fetch,
services.provider_directory.provenance, services.provider_directory.refresh,
shutil, socket, ssl, string, struct, tempfile, textwrap, threading, token,
tokenize, traceback, typing, unicodedata, urllib, urllib.error,
urllib.parse, urllib.request, urllib.response, weakref, zipfile,
zipfile._path, zipfile._path.glob, zlib
```

`httpx` and `services.provider_channel` are both present, as required. None of
`core*`, `sqlalchemy*`, `fastapi*`, `pydantic*`, `services.transactions*`,
`services.market_data*` appears.

---

## Open questions

Five things the prompt left open, and what was chosen.

**1. `click`, `rich` and `pygments` are in the import delta, and are not
allow-listed.** They arrive behind `httpx._main`, httpx's own CLI module,
which `import httpx` executes. The provider-channel contract test carries a
positive `_ALLOWED_THIRD_PARTY` allow-list; this one deliberately does **not**
mirror that shape, because pinning httpx's transitive tree would turn an httpx
point release into a test failure with no security question behind it. The
negative list (what may never appear) is pinned instead, plus a positive
assertion on the two modules that must. If Mission Control wants the stricter
form, it is a three-line addition to `_REQUIRED_MODULES`' neighbour.

**2. `CacheMeta.fetched_at` is normalised to UTC rather than merely required
to be UTC.** The prompt describes the field as "tz-aware UTC". Requiring a
caller to pre-convert would make `fetched_at=now` wrong for any caller holding
a local-offset instant, and silently so. `__post_init__` therefore refuses a
naive value (`ValueError`) and converts an aware one with `.astimezone`, so
the attribute is literally UTC everywhere and the on-disk ISO string always
carries `+00:00`. Equality is unaffected — aware datetimes compare by instant.

**3. `touch_meta` raises rather than inventing a sidecar.** The prompt does not
say what happens when `meta.json` is absent. It raises `CacheMetaError`:
everything `touch_meta` carries over (`directory_version`, `publishing_key_id`,
`source_url`) would otherwise have to be fabricated, and a fabricated
`source_url` is the one field an operator would later read as evidence. The
only caller, step 4/7 of `refresh_directory`, reaches it only when a sidecar
parsed moments earlier.

**4. Provenance is re-read from disk on every path, including the refusals.**
Step 9 of the prompt specifies the read-back for the write/touch paths and
"provenance from `cached`" for `unavailable`. Both are the same value, so one
helper does the read-back uniformly. The reason to prefer it: after a write,
the provenance that matters is the one the *next* reader would compute, and
taking the same path is the only way to be sure those two agree. Cost is one
extra file read and signature verification per call.

**5. `provider_count` for an expired copy falls back to `0` for a
non-list `providers`.** The prompt says to read it "via `json.loads` … no
shape parsing beyond that". An expired document never reached the shape
parser, so `providers` is not guaranteed to be a list even though its
signature verified. `len()` on a list, `0` otherwise — rather than raising
over a document the caller already knows it cannot act on. `issued_at` and
`valid_until` need no such guard: the trust gate read both through
`_shape_date` *before* it refused the window, so they are well-formed by
construction.

Two smaller judgement calls, recorded for completeness: the test ring uses the
ids `test-2026` / `test-2027`, deliberately unlike the shipped
`portfoliflow-YYYY-MM` spelling so a throwaway key can never be mistaken for a
real one in a failure message; and `make_document` drops the published
`successor_key` by default, because the shipped announcement names a key the
*real* ring holds and the test ring does not, which would otherwise put a
"client update required" notice on every unrelated assertion.
