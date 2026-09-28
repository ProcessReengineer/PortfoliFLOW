<!-- SPDX-License-Identifier: AGPL-3.0-only -->
<!-- Copyright (c) 2025-2026 Sönke Pinkernelle -->

# P-CORR-1 — Correlation matrix: canvas-free cell shading

**Runs after:** *"feat(web-research): resolve the news LLM per tenant through the
scoped-settings taxonomy, annex ADR-0132 (P-WR-1)"* · **Tip:** `675a4c3` ·
working tree clean at start (verify-first check 1 ✔).

**Scope as executed:**
`services/chart_specs/statistics_correlation_heatmap.py`,
`tests/services/chart_specs/test_statistics_correlation_heatmap.py`,
`docs/reports/P-CORR-1-report.md` (this file).

**Not touched:** `services/chart_specs/_theme.py`, `services/chart_specs/base.py`,
`services/chart_specs/__init__.py`, `web/routes/statistics.py`, every template and
stylesheet, `gui/widgets/statistics_widgets.py`, every doc outside `docs/reports/`.
No ADR (none required — the D-register below is the record).

---

## OPERATOR ACTION REQUIRED

1. **The commit is yours.** Suggested subject:

       fix(statistics): shade correlation cells with SVG shapes, not the canvas raster (P-CORR-1)

2. **Gate 6 (full suite) was NOT run — this is the one gate this report cannot
   sign off.** The session's testing budget was cut to ~30 minutes mid-run, and the
   project's full run is two steps of ≈2 h 07 m, needed twice (before and after).
   The baseline run was launched detached, then killed ≈2 minutes into step 1.
   **What ran instead** is the change's whole blast radius — 515 tests, all green:
   `tests/services/chart_specs/` (266), `tests/web/test_statistics_section_routes.py`
   (18), `tests/regression` (164, matching the 2026-09-25 baseline exactly), and the
   two other statistics-facing web files (67). **Please run the full suite at leisure
   before or after committing.** Residual risk is low and bounded: the change is
   confined to one pure chart-spec module whose only non-test consumers are
   `services/chart_specs/__init__.py` (re-export) and `web/routes/statistics.py`
   (one call), and no test outside the two edited files references it
   (`grep -rln 'correlation_heatmap' tests/` → the edited test file alone).

3. **Gate 5 expected 24 passed; the file yields 18.** Not a regression — see *Flags*.
   `tests/web/test_statistics_section_routes.py` is byte-identical to HEAD, contains
   exactly 18 `def test_` functions and no `parametrize`. All 18 pass with the dev
   Postgres up. **The prompt's "24" is a stale expectation.**

4. **Run `portfoliflow bootstrap` before the visual round.** The aborted full-suite
   step and the DB-backed route tests both used the dev database.

5. **Browser round, both browsers:**
   - **LibreWolf** (`privacy.resistFingerprinting` on): open Statistics, reload three
     times — cells identical each time, diagonal red, Cash EUR/Cash USD near-red,
     near-zero cells dark.
   - **Brave:** indistinguishable from before.
   - Hover any cell: tooltip shows `<row> vs <col> / ρ = x.xxxx` (the trace is
     invisible but still hovered — Plotly computes hover from data, not pixels).
   - Colorbar present with −1 … +1 ticks. No `-0.00` anywhere.

---

## Verify-first

Run under `source .venv/bin/activate`, before any edit.

| # | Check | Result |
|---|---|---|
| 1 | `git status --porcelain` | empty ✔ |
| 2 | `wc -l` module | **165** ✔ |
| 3 | `wc -l` test file | **138** ✔ |
| 4 | `grep -c 'def build_correlation_heatmap_spec'` | **1** ✔ |
| 5 | `grep -c 'def _format_correlation'` | **1** ✔ |
| 6 | `grep -n 'return f"{value:.2f}"'` | one hit, **line 156** ✔ |
| 7 | `grep -c 'opacity\|shapes\|_interpolate_colour\|_hex_to_rgb'` (module) | **0** ✔ |
| 8 | `grep -c 'opacity\|shapes\|_interpolate_colour'` (tests) | **0** ✔ |
| 9 | `grep -c '^def test_'` (tests) | **15** ✔ |
| 10 | `grep -n 'len(spec\["data"\]) == 1'` | two hits, **lines 41 and 111** ✔ |
| 11 | `build_correlation_heatmap_spec` call sites outside `tests/` | `__init__.py:101` (import), `__init__.py:117` (`__all__`), `web/routes/statistics.py:43` (import), `:330` (call), module `:28` (def) — **exactly the five expected** ✔ |
| 12 | `grep -n '"primary"\|"primary_alt"\|"cell_bg_even"' config/chart_theme.json` | `:24 #E8304A`, `:25 #4A9BD9`, `:123 #1E1E1E` — all three values ✔ (the pattern also matches two palette-**order** strings at `:76`/`:77`, which are list entries, not value lines — the three expected value hits are all present) |
| 13 | `pytest <heatmap test file> -q` | **15 passed** in 23.70 s ✔ |

**All 13 ✔ — no STOP condition.**

---

## What changed

### §1 — `services/chart_specs/statistics_correlation_heatmap.py` (+61 / −4)

- **§1.1** Module docstring: one paragraph added after the "Pure function" paragraph,
  recording that cells are SVG `layout.shapes` and *why* (canvas readback randomised
  by anti-fingerprinting browsers).
- **§1.2** Two module-private helpers added after `_format_correlation`:
  `_hex_to_rgb` (`#RRGGBB` / `#RRGGBBAA`, alpha ignored; `ValueError` otherwise) and
  `_interpolate_colour` (clamped linear-RGB interpolation over the same three theme
  stops, returning upper-case `#RRGGBB`).
- **§1.3** `shapes: list[dict[str, Any]] = []` declared beside `annotations`; one
  `rect` appended per **finite** cell in the same branch as the annotation, so NaN
  cells get neither. Coordinates are category serial numbers (`j ± 0.5`, `i ± 0.5`);
  shape order is row-major and matches annotation order by construction.
- **§1.4** `"opacity": 0.0` added to **both** heatmap traces (empty-input and
  populated); `"shapes": shapes` on the populated layout and `"shapes": []` on
  `empty_layout`. Function docstring: the *Args* NaN sentence now reads "no annotation
  and no shape (the plot background shows through)", and *Returns* gains
  "``layout.shapes`` carries one ``rect`` per finite cell."
- **§1.5** `_format_correlation` normalises `|value| < 0.005` to `0.0`, so a small
  negative renders `0.00` rather than `-0.00`.

Unchanged, as required: the colorscale list, `zmin`/`zmax`, `x`/`y`, the annotation
font rule, margins, `_config()` and the `apply_theme` call. The module still exports
exactly one public function. `apply_theme` was checked and only `setdefault`s layout
keys and never touches `data`, so both the explicit `shapes` list and the trace
`opacity` survive it intact.

### §2 — `tests/services/chart_specs/test_statistics_correlation_heatmap.py` (+72 / −0)

The 15 pre-existing tests are unmodified except `test_nan_cells_are_not_annotated`,
which gained the single prescribed line `assert len(spec["layout"]["shapes"]) == 2`.
`_format_correlation` and `_interpolate_colour` are imported from the module. Seven
tests added: `test_heatmap_trace_is_invisible`, `test_one_rect_shape_per_finite_cell`,
`test_shape_geometry_matches_cell_index`, `test_diagonal_shapes_use_hot_stop`,
`test_interpolate_colour_hits_the_three_stops`,
`test_interpolate_colour_midpoint_is_linear_rgb`, `test_negative_zero_formats_as_zero`.
**22 tests in the file.**

**The midpoint literals were recomputed, not trusted.** Both prompt values hold
against the implementation, including the two half-to-even roundings the prompt warned
about: `0.5 → #832734`, and `-0.5 → #345C7C` (green `92.5 → 92 = 0x5C`, blue
`123.5 → 124 = 0x7C`). The clamping and three-stop values were likewise executed
before being asserted.

```
 services/chart_specs/statistics_correlation_heatmap.py      | 65 +++++++++++++++++--
 tests/services/chart_specs/test_statistics_correlation_heatmap.py | 72 ++++++++++++++++++++++
 2 files changed, 133 insertions(+), 4 deletions(-)
```

---

## Gates

| # | Gate | Result |
|---|---|---|
| 1 | `ruff check` on both files | **clean** — "All checks passed!" ✔ |
| 2 | `ruff format --check` on both files | **clean** — "2 files already formatted" ✔ |
| 3 | `pyright` on the module | **0 errors**, 0 warnings, 0 informations ✔ |
| 4 | `pytest tests/services/chart_specs/ -q` | **266 passed** in 407.35 s; the heatmap file alone reports **22 passed** in 35.01 s ✔ |
| 5 | `pytest tests/web/test_statistics_section_routes.py -q` | **18 passed**, 0 skipped, in 24.47 s — **with the dev Postgres up** (`portfoliflow-postgres` healthy, `pg_isready` accepting). ⚠ Expected 24; the file holds 18 tests and is byte-identical to HEAD — stale expectation, not a regression. See *Flags*. |
| 6 | `pytest -q` (full suite), before and after | **NOT RUN — budget.** Baseline launched detached, killed ≈2 min into step 1; no after-run. **Substitute, all green, 0 failed / 0 skipped:** chart_specs 266 · statistics routes 18 · `tests/regression` 164 in 137.53 s (equals the 2026-09-25 baseline of 164/164) · `test_data_spec_attribute_parses.py` + `test_shell_sidebar_and_areas.py` 67 in 130.94 s. **Total 515 passed.** ⚠ Gate open — see OPERATOR ACTION REQUIRED §2. |
| 7 | `git status --porcelain` | exactly the three expected paths ✔ — `services/chart_specs/statistics_correlation_heatmap.py`, `tests/services/chart_specs/test_statistics_correlation_heatmap.py`, `docs/reports/P-CORR-1-report.md` |

**Five of seven gates pass outright; gate 5 passes on substance with a corrected
expectation; gate 6 is deferred to the operator.**

---

## D-register

| # | Decision | Alternative rejected |
|---|---|---|
| D-1 | Cells are `layout.shapes` with `layer: "below"`; the heatmap trace stays at `opacity: 0` for colorbar and hover. | Shapes `above` a visible heatmap — hairline seams of the corrupted raster could show at anti-aliased rect edges. Removing the trace — loses the colorbar and hover for free. |
| D-2 | NaN cells get no shape (plot background shows through), matching "no annotation". | A neutral-coloured shape — hides the fact that the pair could not be computed. |
| D-3 | Colours interpolated server-side in linear RGB over the same three theme stops. | Sending a colour table for the client to interpolate — puts theme logic in the browser. |
| D-4 | `-0.00` is normalised to `0.00` in `_format_correlation`. | Leaving it — cosmetic, but visible on every demo. |
| D-5 | *(taken under the budget cut)* Gate 6's two full runs were replaced by a blast-radius selection of 515 tests and handed to the operator as an explicit open gate. | Claiming the gate on the 2026-09-25 full-suite baseline four commits back — that baseline predates `675a4c3` and would have been a false green. Silently dropping the gate — worse. |
| D-6 | The full run used the project's canonical **two-step** selection (`-m "not integration and not timing"`, `--ignore=tests/regression` then `tests/regression`) rather than a bare `pytest -q`. | A bare `pytest -q` — would have pulled in the `integration` and `timing` markers the project's own full-suite reports exclude, making the numbers incomparable to the recorded baselines. |

---

## Flags

**None of these were acted on — all are reported, not edited.**

1. **Gate 5's expected count is stale (24 → 18).**
   `tests/web/test_statistics_section_routes.py` is 771 lines, byte-identical to HEAD
   (`git diff --stat HEAD -- <file>` empty), holds exactly 18 `def test_` functions and
   zero `parametrize` decorators. All 18 pass. Nothing in this strand can change that
   file's collection count. Worth correcting in whatever prompt template carries the 24.

2. **Docs describing *how* the correlation matrix is drawn — one near-miss, nothing to
   change.** `docs/module_specs/charts.md:92` carries
   `def correlation_heatmap(self, correlation_matrix: pd.DataFrame, title: str='Correlation Matrix') -> Any`
   with the sentence *"Create an annotated correlation heatmap."* That file is headed
   **"Module specification — `Charts` (retired scaffold)"**, retired by ADR-0094 Stage 1,
   and preserves the docstring of a **deleted** class verbatim as a historical record —
   it does not describe the live drawing path and should not be edited. Two ADRs mention
   the heatmap only to place it *out of scope* (`0069:162`, `0048:173`) and one only
   lists it as migrated content (`0045:29`); `0062:145` is about autocorrelation, not
   this chart. **No user-manual or module-spec page describes the live rendering
   mechanism**, so nothing documents the canvas behaviour this change removes.

3. **Type grep (out-of-scope confirmation) — exactly the two expected hits, both in this
   module:** `statistics_correlation_heatmap.py:70` and `:137`. No `contour`, no `image`,
   no other `heatmap` anywhere under `services/chart_specs/`. The correlation matrix
   really is the only canvas-rendered figure.

4. **Shape count for large universes.** n = 23 → **529** rects; the count is n², so
   n = 50 would be 2,500. Plotly handles a few thousand simple rects without trouble, and
   the annotation list is already the same order (one text node per cell), so shapes do
   not change the figure's complexity class. **No cap implemented** (out of scope). If one
   is ever wanted, the cheap and safe form is to keep the shapes and drop the *annotations*
   above some n — the text is what stops being legible first, and the numbers remain
   available on hover. Capping the shapes instead would reintroduce the canvas.

5. **`gui/widgets/statistics_widgets.py::CorrelationMatrixWidget`** — matplotlib, not
   affected, left alone as instructed. (Note the `gui/` tree was removed by ADR-0094
   Stage 1; the module docstring's reference to it is pre-existing and was preserved
   verbatim rather than silently corrected — out of scope here, but a candidate for a
   future docstring sweep.)

6. **Dev database.** The aborted full-suite step and the DB-backed route tests both ran
   against the dev Postgres; `portfoliflow bootstrap` before any visual check.
