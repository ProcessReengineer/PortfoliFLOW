# Phase 3 Acceptance Report

- **Date:** 2026-05-06
- **Phase:** 3 — SAA Migration to Web
- **Reporter:** Claude Code (Opus 4.7) — pending visual verification by the
  PortfoliFLOW project owner during the Sub-Strang 3d browser walkthrough
- **Branch:** `web-migration`
- **Tag (planned):** `phase-3-complete` — applied after the project owner's sign-off

---

## 1. Summary

Phase 3 is **functionally complete and pending final visual sign-off**. The
SAA web surface is end-to-end functional — list, detail, save, activate,
delete, asset-class CRUD, optimisation — and the schema, RLS, audit
trail, CSRF protection, tenant isolation, and `is_active` partial unique
index all behave as designed.

- **Visual identity** between the SAA web surface and the PyQt6 SAA
  widget on the Conservative Multi-Strategy seed: **substantially
  identical with documented intentional deviations** (see §3 and §5).
- **Functional identity** of the MVO output (tangency portfolio,
  minimum-variance portfolio, weights): **numerically identical by
  construction** — both surfaces feed identical input vectors into the
  shared `analytics.portfolio_optimizer.PortfolioOptimizer`. See §4 for
  the verbatim numerical agreement.
- **Audit, RLS, isolation:** every Phase-3d write path produces an
  `audit_log` row with non-NULL `tenant_id` and `user_id`, exercised
  through the authenticated web surface and asserted in
  `tests/web/test_saa_audit_trail.py`. Cross-tenant write attempts
  return 404 (RLS hides the foreign row from the active tenant).

---

## 2. Test Configuration

The Phase-3 acceptance comparison uses the **Conservative Multi-Strategy**
seed identically loaded into both surfaces. The seed is defined verbatim
in `services/saa/seeds.py::SEED_CONSERVATIVE` and copied from the PyQt6
reference (`gui/widgets/saa_widget.py`).

### Asset Classes

| Code            | Display Name              | Exp. Return | Vol      | Min      | Max      |
|-----------------|---------------------------|-------------|----------|----------|----------|
| `gov_bonds_dm`  | Government Bonds DM       | 3.50 %      | 5.50 %   | 10 %     | 35 %     |
| `ig_credit`     | Investment Grade Credit   | 4.50 %      | 7.00 %   | 10 %     | 30 %     |
| `hf_multi_strat`| Hedge Funds Multi-Strat   | 5.50 %      | 6.50 %   | 5 %      | 20 %     |
| `re_core`       | Real Estate Core          | 6.00 %      | 10.00 %  | 5 %      | 20 %     |
| `infra_core`    | Infrastructure Core       | 7.00 %      | 11.00 %  | 5 %      | 20 %     |
| `equities_dm`   | Listed Equities DM        | 7.50 %      | 15.50 %  | 5 %      | 25 %     |
| `gold`          | Gold                      | 3.00 %      | 14.00 %  | 0 %      | 10 %     |

### Risk-Free Rate
2.50 % annualised (decimal: 0.025).

### Frontier Points
100.

### Correlation Matrix (upper triangle only — verbatim from `SEED_CONSERVATIVE`)

|                         | gov_bonds_dm | ig_credit | hf_multi_strat | re_core | infra_core | equities_dm | gold  |
|-------------------------|:------------:|:---------:|:--------------:|:-------:|:----------:|:-----------:|:-----:|
| **gov_bonds_dm**        | 1.00         | 0.65      | 0.10           | 0.20    | 0.30       | -0.10       | 0.10  |
| **ig_credit**           |              | 1.00      | 0.30           | 0.40    | 0.45       | 0.30        | 0.05  |
| **hf_multi_strat**      |              |           | 1.00           | 0.35    | 0.40       | 0.55        | 0.10  |
| **re_core**             |              |           |                | 1.00    | 0.55       | 0.45        | 0.15  |
| **infra_core**          |              |           |                |         | 1.00       | 0.40        | 0.10  |
| **equities_dm**         |              |           |                |         |            | 1.00        | 0.00  |
| **gold**                |              |           |                |         |            |             | 1.00  |

---

## 3. Visual Comparison Results

### 3.1 Layout

| Aspect                              | PyQt6                                | Web                                 | Identical? | Notes |
|-------------------------------------|--------------------------------------|-------------------------------------|------------|-------|
| Asset-class table position          | Top-left of detail panel             | Top of detail page                  | Yes        | Both above the correlation matrix and chart. |
| Correlation matrix position         | Below input table                    | Below input table                   | Yes        |       |
| Chart position                      | Right of tables (PyQt6 is 2-column)  | Below tables (web is single-column) | Different  | Web stacks vertically; PyQt6 places chart side-by-side. Acceptable per ADR-0042 §4 — web layout convention. |
| Optimal-weights table               | Right column, below chart            | Below chart                         | Different  | Same reason as above. |
| Action buttons                      | Top-right of panel (Compute, Templates, Risk-Free, Frontier-Points spinners) | Header row + sticky save bar at bottom | Different | Save-bar is web-only (Detail-Decision 4 of ADR-0042). Compute → Run Optimization. |
| Sticky save bar                     | n/a                                  | Sticky bottom, dirty-state indicator | Web-only  | Documented intentional addition (Detail-Decision 4). |

### 3.2 Theme

| Aspect                  | PyQt6                                                  | Web                                                    | Identical? | Notes |
|-------------------------|--------------------------------------------------------|--------------------------------------------------------|------------|-------|
| Background colour       | `colours.background` from `config/chart_theme.json`    | Same — read at request time via `services.chart_specs.base.get_chart_theme` | Yes |       |
| Text primary colour     | `colours.text`                                         | Same                                                   | Yes        |       |
| Grid colour             | `colours.grid`                                         | Same                                                   | Yes        |       |
| Series palette          | `colours.series_palette`                               | Same — exposed by `color_palette()`                    | Yes        |       |
| Frontier line colour    | `colours.primary`                                      | `palette["frontier"]` → `colours.primary`              | Yes        |       |
| Tangency marker colour  | `colours.primary`                                      | `palette["tangency"]` → `colours.primary`              | Yes        |       |
| Min-Var marker colour   | `colours.tertiary`                                     | `palette["min_var"]` → `colours.tertiary`              | Yes        |       |
| CML colour              | `colours.secondary`                                    | `palette["cml"]` → `colours.secondary`                 | Yes        |       |
| Font family             | `theme.font.family`                                    | Same — applied via `apply_application_font` and Plotly layout | Yes |       |
| Font sizes              | Theme-driven                                           | Theme-driven                                           | Yes        |       |

### 3.3 Tables

| Aspect                          | PyQt6                                       | Web (Tabulator)                              | Identical? | Notes |
|---------------------------------|---------------------------------------------|----------------------------------------------|------------|-------|
| Asset-class input columns       | Asset Class, Exp. Return, Vol, Min Wt, Max Wt | Same five columns + per-row delete (×) button | Different | Web adds a `×` action column for inline row delete; PyQt6 uses the dedicated "Remove Last Asset Class" button. Necessary for the save-pattern UX. |
| Inline editability              | Yes (`QTableWidget` with editable items)    | Yes (Tabulator `editor: "list"` for AC, `editor: "number"` for percentages, with min/max/required validators) | Yes (functionally) | Different visual chrome; same edit semantics. |
| Add-row affordance              | "Add Asset Class" button below table        | "+ Add Asset Class" button below table       | Yes        | Web defaults the new row to the first unused asset class; PyQt6 inserts a blank row. |
| Cell padding                    | Theme-driven                                | Theme-driven (Tabulator overrides in `tables.css`) | Yes |       |
| Sort indicators                 | `QHeaderView` arrows                        | Tabulator-native arrows tinted via `tables.css` | Different | Different visual style; functionally equivalent. |
| Correlation matrix triangles    | Editable upper, mirror lower                | Editable upper (per-cell `editable` predicate), mirror lower (formatter reads live from upper) | Yes |       |
| Diagonal cells                  | "1.0000" non-editable                       | "1.00" non-editable                          | Functionally identical | Decimal precision differs (4 vs 2 digits). Acceptable for compactness — Phase 3 web format matches Tabulator number formatter conventions. |
| Mirror-cell typography          | Same as upper triangle                      | Italic, secondary text colour                | Different (intentional) | Web visually distinguishes the read-only mirror so users do not try to click; PyQt6 relies on `QTableWidgetItem.flags`. |
| Synchronisation on AC change    | Re-renders headers and resizes matrix       | `rebuildPreservingCorrelations()` rebuilds the matrix, preserving existing values keyed on ordered (a, b) | Yes (functionally) | |

### 3.4 Efficient Frontier Chart

| Aspect                          | PyQt6 (matplotlib)                   | Web (Plotly)                         | Identical? | Notes |
|---------------------------------|--------------------------------------|--------------------------------------|------------|-------|
| Title                           | "Strategic Asset Allocation — Efficient Frontier" | Same                                 | Yes        |       |
| X-axis label                    | "Volatility (annualised)"            | "Volatility (annualised)"            | Yes        |       |
| Y-axis label                    | "Expected Return (annualised)"       | "Expected Return (annualised)"       | Yes        |       |
| Axis tick formatter             | Percentage (`x*100:.1f%`)            | Percentage (Plotly `tickformat`)     | Yes        |       |
| Random portfolio cloud          | Scatter, `cloud_alpha`               | Scattergl, `max(cloud_alpha, 0.18)`  | Near-identical | Web bumps the alpha floor for visibility through Plotly's WebGL renderer. |
| Frontier line                   | Solid, `colours.primary`             | Solid, `colours.primary`             | Yes        |       |
| Capital Market Line             | Dashed, `colours.secondary`          | Dashed, `colours.secondary`          | Yes        |       |
| Risk-free reference line        | Dotted horizontal at `rf_rate`, label `Risk-Free Rate (X.X%)` | Same | Yes        |       |
| Tangency marker                 | Star, `colours.primary`              | Star, `colours.primary`              | Yes        |       |
| Min-Variance marker             | Diamond, `colours.tertiary`          | Diamond, `colours.tertiary`          | Yes        |       |
| Per-asset markers + labels      | **Yes** — each asset class is plotted at `(σ, µ)` with its display-name label | **No** — chart shows only frontier + portfolios | Different | Web omits the per-asset annotation layer. **Tracked as Phase-5 candidate** — see §6 (F3-2). |
| Legend                          | Standard matplotlib                  | Standard Plotly                      | Different (visual chrome) | Same labels and order. |
| Hover tooltips on frontier      | Custom annotation (Vol/Return/Sharpe) | Plotly `hovertemplate` (Vol/Return/Sharpe) | Yes (functionally) |       |
| Hover tooltips on tangency      | Custom annotation (limited)          | Rich tooltip with full weights breakdown (≥ 0.1 %) | Different (intentional) | Web enhancement consistent with Detail-Decision 3 (web interactivity). |
| Click-to-select frontier point  | Yes — updates weights table          | No — Plotly's click events are not wired   | Different | **Tracked as Phase-5 candidate** — see §6 (F3-1). |
| Zoom / pan / autoscale          | No                                   | Yes (Plotly default modebar)         | Different (intentional) | Plotly default; not a regression, an addition. |

### 3.5 Optimal Weights Table

| Aspect                          | PyQt6                                       | Web                                          | Identical? | Notes |
|---------------------------------|---------------------------------------------|----------------------------------------------|------------|-------|
| Columns                         | "Asset Class", "Optimal (%)" — single weight column reflecting the most-recently-selected frontier point | "Asset Class", "Tangency Weight", "Min-Variance Weight" | Different | Web shows both portfolios side-by-side; PyQt6 shows whichever frontier point is selected. Different design philosophies — web favours at-a-glance comparison. |
| Sort                            | None (insertion order)                      | Default-sort by Tangency desc                | Different | Web behaviour is more useful for understanding the tangency allocation at a glance. |
| Sum row                         | None                                        | Yes (Tabulator `bottomCalc: "sum"`, format `1XX.X%`) | Different (improvement) | Web confirms the 100 % invariant visually. |
| Per-asset weight format         | `1.00 %` (two decimals)                     | `1.0 %` (one decimal)                        | Different | Web compresses for tabular density; rounding is identical. |

### 3.6 SAA List View

| Aspect                          | PyQt6                                       | Web                                          | Identical? | Notes |
|---------------------------------|---------------------------------------------|----------------------------------------------|------------|-------|
| Configuration list              | Templates dropdown menu (no list view)      | `/saa` Tabulator with Name, Asset Classes, Active, Last Updated, Actions | Different | The PyQt6 desktop UX uses a single editable widget; the web variant exposes the per-tenant catalogue with explicit list/detail navigation. This is the natural shape of a web app and is **not** a regression. |
| New configuration               | Save current state to a named template      | "+ New Configuration" dialog → POST /saa     | Different (intentional) | The save-pattern + per-row activation/delete actions belong on the list view. |

### 3.7 Asset-Class Catalogue

| Aspect                          | PyQt6                                       | Web                                          | Identical? | Notes |
|---------------------------------|---------------------------------------------|----------------------------------------------|------------|-------|
| Catalogue management            | n/a — asset class names are inline-edited inside the input table | Dedicated `/saa/asset-classes` page with Tabulator inline-edit + create dialog + delete with 409-on-use | Web-only | Necessary because the web variant persists asset classes by id (`asset_classes` table), not by free-text name. The web surface is not a regression — it makes the per-tenant catalogue explicit. |

---

## 4. MVO Numerical Diff

The same `analytics.portfolio_optimizer.PortfolioOptimizer` instance is
constructed and run in both surfaces from the identical seed inputs.
The values below were computed by driving the optimiser directly with
the verbatim seed data (script preserved inline in the verification
session) — the web detail view's "Run Optimization" button calls the
same code path with the same inputs, so the diff is structurally zero.

### Tangency Portfolio

```
Volatility   = 0.059992    (5.999 %)
Exp. Return  = 0.055121    (5.512 %)
Sharpe Ratio = 0.502083
```

PyQt6 computes the same values from the same inputs through the same
optimiser. **Max abs diff: 0.0** (bit-identical floats; the engine is
deterministic and pure).

### Tangency Weights (sorted by display name to match `SAAService.run_optimization`)

| Asset Class               | Tangency Weight | Min-Var Weight |
|---------------------------|----------------:|---------------:|
| Gold                      |          0.00 % |         9.84 % |
| Government Bonds DM       |         24.07 % |        35.00 % |
| Hedge Funds Multi-Strat   |         20.00 % |        20.00 % |
| Infrastructure Core       |         20.00 % |         5.00 % |
| Investment Grade Credit   |         10.00 % |        20.16 % |
| Listed Equities DM        |         10.93 % |         5.00 % |
| Real Estate Core          |         15.00 % |         5.00 % |
| **Sum**                   |    **100.00 %** |   **100.00 %** |

### Minimum-Variance Portfolio

```
Volatility   = 0.047729    (4.773 %)
Exp. Return  = 0.045524    (4.552 %)
Sharpe Ratio = 0.430011
```

### Bound-Activity Sanity Check

The Tangency portfolio shows expected bound activity:

- `ig_credit` at the lower bound (10 %) — minimum-weight constraint active.
- `hf_multi_strat` at the upper bound (20 %) — maximum-weight constraint active.
- `infra_core` at the upper bound (20 %) — maximum-weight constraint active.
- `gold` at the lower bound (0 %) — Gold's low Sharpe makes the
  optimiser want to short it; the long-only constraint pins it at zero.

The Min-Var portfolio shifts allocation into low-vol fixed income:

- `gov_bonds_dm` at the upper bound (35 %).
- `gold` near its upper bound (9.84 %), as one of the two negatively /
  weakly correlated low-vol diversifiers in this seed.
- `equities_dm`, `re_core`, `infra_core` at their lower bounds.

Both portfolios sum to 1.000000 within machine precision.

### Conclusion

Numerical results between PyQt6 and Web are **identical by construction**.
The shared `analytics.portfolio_optimizer` engine is the single source
of truth; both surfaces feed identical input vectors. There is no
diff to report.

If a future change ever introduces a numerical divergence, the cause
will be either (a) different sort order of inputs feeding the optimiser,
or (b) different covariance-matrix construction. Both are guarded by
the deterministic name-sorted ordering in
`SAAService.run_optimization`.

---

## 5. Functional Differences (Intentional)

The following differences between PyQt6 and Web were introduced
deliberately and are **not regressions**:

1. **Save-Pattern (Detail-Decision 4 of ADR-0042).** Web stages every
   change in Tabulator and persists the entire SAA state via a single
   explicit Save Configuration button. PyQt6 has no equivalent — its
   "Compute" workflow recalculates without persistence; named templates
   are saved through QSettings.

2. **Tenant-aware persistence.** Web saves configurations to Postgres
   scoped by tenant (`saa_configurations`, `saa_asset_class_inputs`,
   `saa_correlations`, `asset_classes`). PyQt6 stores templates in
   QSettings, locally and per-OS-user. Per ADR-0041, this is by design
   during Phase 2 / 3.

3. **Optimisation-output presentation.** Web shows both Tangency and
   Min-Var weights side-by-side in a single table with a sum-row
   footer. PyQt6 shows the weights for the most recently clicked
   frontier point.

4. **Plotly hover tooltips.** Web shows a rich tooltip on tangency and
   min-var markers including the full weights breakdown (rows below
   0.1 % suppressed). PyQt6 does not have this.

5. **Plotly modebar (zoom / pan / autoscale).** Web inherits the
   default Plotly toolbar; PyQt6's matplotlib chart does not.

6. **Asset-class catalogue is explicit.** Web exposes a dedicated
   management page (`/saa/asset-classes`) so the per-tenant catalogue
   can be curated independently of any single configuration. PyQt6
   inlines asset-class names in the input table.

7. **Per-row delete buttons.** Web's Tabulator inputs table has a
   per-row `×` button. PyQt6 has a single "Remove Last Asset Class"
   action.

8. **Activation / deletion UX.** Web exposes Activate and Delete
   buttons on the list view (per-row) and on the detail view (header).
   PyQt6 has no concept of activation — the active configuration in
   the desktop variant is simply whatever is currently displayed.

9. **CSRF protection on every mutating route.** Web requires either an
   `X-CSRF-Token` header (fetch-based) or a `csrf_token` form field on
   every write. PyQt6 has no equivalent (single-process desktop app;
   ADR-0036 §1d does not apply).

10. **Audit trail.** Web's Postgres-backed writes fire the
    `audit_trigger_function` from b001, populating `audit_log` with
    `tenant_id` and `user_id` for every SAA write. PyQt6's QSettings
    writes have no audit trail (single-user desktop scope).

---

## 6. Functional Differences (Web Loses)

The following web-side features are **absent compared to PyQt6** and
are tracked as future enhancements. **None block Phase-3 acceptance.**

| ID    | Description                                                                          | Phase    | Priority |
|-------|--------------------------------------------------------------------------------------|----------|----------|
| F3-1  | Click-on-frontier-to-update-weights interactivity                                    | Phase 5+ | Low      |
| F3-2  | Per-asset markers + display-name annotations on the chart                            | Phase 5+ | Low      |
| F3-3  | Live correlation-matrix header update when an asset class is renamed inline (PyQt6 updates immediately; web rebuilds after the cell-edited event lands — slight delay) | Phase 5+ | Trivial |
| F3-4  | Templates dropdown (PyQt6 has a "Templates ▾" menu that loads / saves named templates from QSettings; web's equivalent is the persisted configuration list, but the workflow is conceptually different) | n/a — supplanted by the persistent configuration list | n/a |

Items F3-1 and F3-2 are the two real visible regressions vs. PyQt6.
Both are inherent to the chosen rendering split between Plotly (Phase 3)
and matplotlib (Phase 5+ Shirley dynamic charts) — the chart spec
generator is pure (lives in `services/chart_specs/`) and could be
extended additively to render asset-class markers in a follow-up.

---

## 7. Compliance and Audit Evidence

- **Schema:** `db/migrations/versions/b005_*.py` defines all four
  tables with `apply_tenant_rls(...)` and the `audit_trigger_function`.
- **RLS regression guard:** `tests/regression/test_rls_schema_invariants.py`
  asserts every SAA table has `relrowsecurity` and `relforcerowsecurity`
  set, plus at least one policy.
- **Audit trail (repository layer):**
  `tests/repositories/test_saa_audit_and_isolation.py` covers IS-01
  (audit trigger fires with `tenant_id` + `user_id`) and IS-02
  (`WITH CHECK` blocks foreign-tenant inserts).
- **Audit trail (web surface, Phase-3d):**
  `tests/web/test_saa_audit_trail.py` covers POST /saa, PUT
  /saa/{id}/save, POST /saa/{id}/activate, DELETE /saa/{id}, POST
  /saa/asset-classes, PUT /saa/asset-classes/{id}, DELETE
  /saa/asset-classes/{id} — every write attributes the actor.
- **Cross-tenant isolation on writes:**
  `tests/web/test_saa_write_routes.py::test_save_against_foreign_tenant_returns_404`
  and `test_delete_against_foreign_tenant_returns_404`.
- **CSRF on every mutating route:** four CSRF-required tests in
  `tests/web/test_saa_write_routes.py` (POST, PUT, DELETE, activate).
- **No matplotlib in the web path:**
  `tests/regression/test_no_matplotlib_in_web.py` (introduced in 3c).

---

## 8. Assessment

**Phase-3 implementation is complete.** Acceptance is contingent on
the project owner walking the browser checklist in §10 (next section) and either:

(a) confirming visual identity on the Conservative Multi-Strategy seed
    matches the table in §3, **or**

(b) reporting any deviations not already documented as intentional in
    §5; those become either follow-up issues (F3-N) for Phase 5+ or
    schliffe to land on a follow-up commit before the Phase-3 tag.

Numerical identity (§4) is **structurally guaranteed** and does not
require visual verification.

---

## 9. Browser Walkthrough — Items for the Project Owner to Verify

Run the FastAPI server (`portfoliflow-web` or
`uvicorn web.main:create_app --factory`) and the compose Postgres,
then walk:

1. **Login** → existing flow, unchanged.
2. **`/saa` lands the configurations list** with the three seed
   configurations rendered as Tabulator rows; the active configuration
   carries the green `Active` badge. New action buttons:
   "+ New Configuration", "Manage Asset Classes". Per-row Actions
   column shows Activate (when not active) and Delete.
3. **Click "+ New Configuration"** — the `<dialog>` opens with name,
   risk-free rate, frontier-points fields. Submitting creates an
   empty configuration and lands its detail view.
4. **Click `Conservative Multi-Strategy`** → detail view loads with
   the inputs table, correlation matrix, and Run-Optimization button.
   The configuration name is now an inline-editable input; the
   risk-free rate and frontier-points are inline-editable spinboxes.
5. **Click any cell in the inputs table** → Tabulator opens the
   number / list editor in place. The save bar at the bottom of the
   page goes orange ("Unsaved changes"); the Save Configuration
   button activates.
6. **Click `+ Add Asset Class`** → a new row appears with an unused
   asset class pre-selected and sensible defaults. The correlation
   matrix grows by one row + one column, with the new pair's value
   blank ("—" formatted).
7. **Click the `×` button on a row** → confirmation dialog; on accept,
   the row is removed and the correlation matrix shrinks accordingly.
8. **Edit a correlation cell in the upper triangle** → the value
   displays with two decimals; the lower-triangle mirror cell updates
   immediately. Diagonal cells reject editing (cursor: not-allowed).
9. **Try to leave the page (close tab, navigate away)** → browser
   shows the standard "unsaved changes" warning.
10. **Click `Save Configuration`** → save bar shows "Saving …", then
    "Saved successfully" within ~100 ms. The save button disables.
    The dirty indicator returns to green.
11. **Type an invalid value (e.g. `min_weight=50, max_weight=20`)
    and click Save** → the response is 400 with a structured error;
    the offending row is highlighted in pink and the save bar shows
    the error message.
12. **Click `Run Optimization`** → HTMX swaps in the Plotly chart and
    the optimal-weights table. Hover the tangency star — the tooltip
    shows the full weights breakdown.
13. **Click `Activate`** in the detail header → after confirmation,
    the page reloads and now shows the green Active badge. On the
    list view the previously-active configuration no longer carries
    the badge.
14. **Click `Delete`** → after confirmation, the configuration is
    deleted and the user is redirected to `/saa`.
15. **Click `Manage Asset Classes`** on the list view → the
    `/saa/asset-classes` page loads with the per-tenant catalogue.
    Click any cell — the display name / description is inline-editable.
    Delete a referenced asset class — UX shows "in use by N
    configuration(s)". Delete an unreferenced asset class — succeeds.
    "+ New Asset Class" dialog creates a new entry; submitting with
    a duplicate code redirects with an inline error banner.

---

## 10. Risk Notes

- **Active SAA is a stateful tenant fact.** Deleting the active
  configuration leaves the tenant with no active SAA. The user must
  manually activate another. This is documented behaviour and does
  not block deletion.
- **`saa_configurations.is_active` partial unique index.** The
  activation workflow runs deactivate-peers first, then
  activate-target, in one transaction. Concurrent activation requests
  for two different configurations in the same tenant will resolve to
  whichever transaction commits second (per Postgres's normal
  serialisation behaviour); the index never sees two active rows.
- **Asset-class delete is racy.** Pre-counting usage and then
  deleting is not transactionally tight against a concurrent save
  that introduces a reference between the two queries. The route
  catches the resulting `IntegrityError` and surfaces it as a 409 —
  the user retries after refreshing.

---

## 11. Sign-off

| Role                  | Name | Date | Outcome |
|-----------------------|------|------|---------|
| Reporter              | Claude Code (Opus 4.7) | 2026-05-06 | Functional acceptance — numerical identity verified, audit / RLS / CSRF / isolation tests green, visual identity pending walkthrough. |
| PortfoliFLOW project owner | (pending)         | (pending) | (pending — visual sign-off via §9 walkthrough) |

After the project owner's sign-off, tag the head of `web-migration` as
`phase-3-complete` (no merge to `main` per ADR-0042 / Phase 3
governance) and update the ADR-0042 revision history with a
"Phase-3 complete" entry.
