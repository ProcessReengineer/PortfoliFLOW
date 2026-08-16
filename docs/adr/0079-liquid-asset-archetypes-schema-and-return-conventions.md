# ADR-0079: Liquid-Asset Archetypes, Per-Investment Schema, and Mark-to-Market Return Conventions

- **Status:** Accepted — 2026-06-15

## Context

Every per-investment surface today renders one presentation, shaped by private-markets logic (`single-investment review`, ADR-0073; Front Office Overview, ADR-0067/0072): a cash-flows-and-NAV dual axis, TVPI/DPI/RVPI multiples, net capital gain / J-curve, money-weighted IRR, vintage, and commitment/unfunded. This is correct for drawdown vehicles and **economically meaningless for listed instruments** — a `listed_equity` or `listed_bonds` fund has no capital calls, no TVPI, no J-curve.

ADR-0074 brought liquid asset classes into product scope. The data model is partly ready: the `investment_type` discriminator (`core/models/investment.py`) already carries `listed_equity` and `listed_bonds` alongside the private types, and the asset-class catalogue already lists equities, credit, govies, and cash. What is missing is a **presentation and analytics model** for mark-to-market instruments, plus the reference data that model needs.

### What exists and is reusable

- `investment_type`: `private_equity, private_debt, real_estate, infra_equity, listed_equity, listed_bonds, other`.
- `flow_type` on `investment_cashflows`: `capital_call, distribution, fee, carry, dividend, coupon, other`. Income flows for listed instruments (`dividend`, `coupon`) are already modelled, with signed `amount` (ADR-0043 §1).
- `compute_cashflow_adjusted_return_series(nav, cashflows)` (ADR-0066): `r_t = (NAV_t + a_t) / NAV_{t-1} − 1`, reducing to `pct_change()` exactly when no flows fall in the interval. This is the correct primitive for **any** vehicle with mid-period flows — including dividend- and coupon-paying listed funds, not only drawdown vehicles.
- The Statistics surface (VaR/CVaR/Ulcer/Downside Deviation/Sortino/autocorrelation), Benchmarks & Attribution (ADR-0061), and the per-investment composition tables (`investment_sector_weights`, `investment_region_weights`, `investment_country_weights`).
- `investment_navs.source` — a nullable free-text provenance column — establishes precedent for recording the origin of an analytics input.

### What is missing

Fixed-income characteristics cannot be reconstructed from a NAV series the way total return can: yield-to-maturity, effective duration, option-adjusted spread, convexity, and the credit-rating / maturity composition are genuine **inputs**, not derivations of price. ADR-0043 itself parks "Listed-Bond duration" as future type-specific work and reserves `investments.type_specific_data` (JSONB) as the parking lot. There is also no mechanism routing each `investment_type` to an appropriate analytical surface.

### Spike findings (throwaway `liquid_spike.py`, 2026-06-15)

Two synthetic fund-level instruments (one EUR IG credit fund, one global equity fund), monthly over ten years, were generated to pin conventions **before** committing schema:

1. **FI total return is exactly derivable.** Reconstructing the total-return index from `ytm`/`eff_duration`/`convexity` via `r_t = ytm_{t-1}/12 − D·Δy + ½·C·Δy²` matched the generated index to 0.007 index points (rounding only). Persisting a FI `tr_index` would store derivable data.
2. **Equity total return diverges by reinvestment convention.** Recomputing the same path cash-flow-adjusted from `price_index` + dividend flows via `(P_t + d_t) / P_{t-1} − 1` diverged from a naive "flat dividend-rate added to price return" construction by ~1.7 % terminally, growing monotonically. The divergence is purely the distribution-attachment convention — which NAV the income attaches to. Left unpinned, a recomputed TWR drifts against a manager-reported figure at tens of basis points per year.

### Correction to a working assumption

Earlier design discussion assumed the existing composition-weight tables are time-series. They are not: `investment_sector_weights` carries the natural key `(investment_id, sector_id)` with `weight_pct` (0–100) and **no `as_of_date`** — it is point-in-time. The decision below to historise the new FI weight tables therefore *establishes* a time-series weight pattern rather than mirroring the existing one; aligning the legacy point-in-time weights is noted out of scope.

## Decision

Four coordinated, additive changes at the per-investment level. The portfolio-level aggregation of money-weighted (IRR) and time-weighted (TWR) sleeves is explicitly **not** decided here (see Consequences → Neutral).

### 1. Three presentation archetypes, mapped from `investment_type`

| Archetype | `investment_type` | Return logic |
|---|---|---|
| Capital-Account | `private_equity, private_debt, real_estate, infra_equity` | money-weighted (existing surface) |
| Total-Return Equity | `listed_equity` (and equity-like `other`) | time-weighted |
| Fixed-Income | `listed_bonds` | time-weighted |

Cash is a degenerate Fixed-Income case (balance + running yield). The Capital-Account archetype is the present surface, unchanged. The two mark-to-market archetypes are new and share the time-weighted return path.

**Total-Return Equity shows:** a total-return index rebased to 100 with benchmark overlay; trailing TWR (1M/3M/YTD/1Y/3Y/SI); an underwater / max-drawdown plot; rolling 12-month volatility and Sharpe; the Statistics-surface risk block (Sortino, VaR, CVaR) plus benchmark-relative beta, tracking error, and information ratio; trailing-12-month dividend yield and distribution history; sector and region composition; and a monthly-return heatmap. Most of this recombines existing analytics; new work is the trailing-return table, the rolling windows, the monthly reshape, and the benchmark-relative beta.

**Fixed-Income shows:** the same total-return-index-plus-benchmark hero; a YTM-and-OAS dual-axis time series; effective duration and convexity; a credit-rating distribution with a notch-weighted average rating; a maturity ladder; the underwater/volatility block computed on the TR series (the rate-selloff drawdown makes this meaningful for FI); and a coupon schedule with running yield.

### 2. Per-investment time-series schema (new), all tenant-scoped and RLS-enforced

`investment_bond_analytics` — natural key `(investment_id, as_of_date)`:

- `ytm`, `eff_duration` NOT NULL; `oas`, `convexity` nullable (not every manager reports all four).
- `basis TEXT NOT NULL CHECK (basis IN ('reported','computed'))`.
- standard `tenant_id` and audit columns (`created_by`, `created_at`, `updated_at`).
- **No `tr_index` column** — total return is derived on read (spike finding 1; ADR-0013).

`investment_rating_weight` — natural key `(investment_id, as_of_date, rating_bucket)`:

- `rating_bucket TEXT CHECK (rating_bucket IN ('AAA','AA','A','BBB','BB','B','CCC_and_below','NR'))`.
- `weight_pct NUMERIC CHECK (weight_pct >= 0 AND weight_pct <= 100)`, plus `basis`, `tenant_id`, audit.

`investment_maturity_weight` — natural key `(investment_id, as_of_date, maturity_bucket)`:

- `maturity_bucket TEXT CHECK (maturity_bucket IN ('0-1y','1-3y','3-5y','5-7y','7-10y','10y+'))`.
- `weight_pct`, `basis`, `tenant_id`, audit.

Bucket taxonomies are constrained text (the `flow_type` / `nav_kind` pattern), not reference-table foreign keys: the sets are small, fixed, and canonical, and are decided here. Both weight tables are **time-series** (`as_of_date` in the natural key) — deliberately diverging from the point-in-time `investment_sector_weights`, because FI composition shifts materially over a fund's life and the archetype shows it over time.

Additively, `investment_navs` gains `basis TEXT NULL` (NULL ⇒ treated as `reported`). The existing free-text `source` column is orthogonal provenance and is untouched.

### 3. Return, rating, and bucket conventions

- **Mark-to-market total return is time-weighted and derived, never stored.** Both listed archetypes compute TR via `compute_cashflow_adjusted_return_series(nav, income_flows)` (ADR-0066) on the reported NAV (price) series, with `dividend` / `coupon` flows as the signed `a_t`. Raw `pct_change()` is wrong for income-paying funds for the same reason it is wrong for drawdown vehicles — the income leaves the NAV and must be added back.
- **Distribution-attachment convention (pinned by spike finding 2):** income attaches at the period-end, ex-distribution NAV — `r_t = (NAV_t^ex + a_t) / NAV_{t-1} − 1` — which is exactly the ADR-0066 form. This is the single pinned convention for equity dividends and bond coupons alike. The production path therefore already implements the correct convention; the spike's equity divergence was a generator artefact.
- **FI TR comes from reported NAV; the duration identity is a cross-check only.** The `r = carry − D·Δy + ½·C·Δy²` identity is the fixture generator and an optional reconciliation, not a production return path.
- **Average rating is notch-weighted, never a naive mean.** The distribution is stored; the headline average is derived by mapping buckets to numeric notches, weighting, and mapping back.
- **`tr_index` is not persisted** for either listed archetype. Fixtures store NAV (price) + income flows; TR is computed on read.

### 4. Forward-compatibility for a later single-security layer

Single securities (bottom-up holdings) are a later evolution stage, out of scope here, but three guarantees keep that stage purely additive:

- **No security-level fields on `investments`.** ISIN, coupon rate, maturity date, and issuer rating do not go on the fund row; the fund stays a black-box anchor. `type_specific_data` remains the parking lot, not a modelling surface.
- **The `basis` discriminator on every analytics input.** Today every row is `reported`. A future holdings-aggregation job writes `computed` rows for the same `(investment_id, as_of_date, …)`; chart-specs read "prefer `computed`, else `reported`". No schema change when securities arrive.
- **`reported` vs `computed` is an explicit either/or with a visible basis** (the no-silent-fallback discipline): a partially-populated holdings set must never silently understate duration. A future `holdings` / `securities` layer hangs off the fund and is shared by both listed archetypes.

## Consequences

**Positive**

- `listed_equity` and `listed_bonds` get correct, factsheet-grade surfaces instead of meaningless private-markets multiples.
- The mark-to-market return path reuses the proven ADR-0066 primitive; equity and FI share one pinned distribution convention, already formally correct in production.
- New FI reference data follows the established time-series and constrained-text patterns; the `basis` seam makes the later single-security layer additive rather than a rewrite.
- Analytics-layer purity (ADR-0013/0045) is preserved: the new computations are DataFrame-in/DataFrame-out, with no DB/Qt/FastAPI in `services/analytics/`.

**Negative**

- Three new tables plus one additive column on `investment_navs`, with the Alembic migration pair, repositories, and an import-format extension. The sample dataset must gain monthly NAV and income flows for listed instruments, plus the FI characteristic and weight series.
- The new time-series weight tables diverge from the point-in-time `investment_sector_weights`; the codebase carries two weight conventions until/unless the legacy ones are historised.

**Neutral**

- **Portfolio-level TWR/IRR aggregation is deliberately out of scope.** A portfolio mixing money-weighted and time-weighted sleeves needs a coherent blended-return rule; that is a portfolio/overview concern, not a per-investment one, and the archetypes are fully buildable without it. It becomes its own successor ADR — the same separation rationale ADR-0066 used when it deferred the Statistics/Front-Office return question.
- Whether the legacy sector/region/country weights should be historised to match the new pattern is left open.

## Tests

1. **FI TR derivation / non-persistence:** given a `bond_analytics` series, the derived TR index reconstructs to ≤ 1e-6 of the duration-identity reference (spike finding 1); assert no `tr_index` column exists on the table.
2. **Distribution-attachment convention:** a synthetic NAV plus a single income flow asserts `compute_cashflow_adjusted_return_series` equals `(NAV_t^ex + a_t) / NAV_{t-1} − 1` exactly, and that the naive flat-rate construction is *rejected* (regression guarding spike finding 2).
3. **Reduction property:** a listed instrument with zero income flows yields TR identical bit-for-bit to `pct_change()` (continuity with ADR-0066).
4. **Notch-weighted rating:** a known rating distribution maps to the expected notch-weighted average; a naive mean would differ — assert the difference is detected.
5. **Archetype routing:** each `investment_type` resolves to the intended archetype; `listed_bonds` never renders TVPI/J-curve, `private_equity` never renders YTM/duration.
6. **Schema / RLS / purity:** the three new tables carry `tenant_id` and pass the RLS-context test (ADR-0078); the bucket CHECK constraints reject out-of-taxonomy values; the new analytics functions pass `tests/regression/test_analytics_layer_pure.py`.

## Related

- ADR-0066 (cash-flow-adjusted returns — the reused TR primitive and the deferral-as-separate-ADR pattern); ADR-0013, ADR-0045 (analytics-layer purity); ADR-0043 (investment domain schema, signed-amount convention, `type_specific_data` parking lot, listed-bond-duration future note); ADR-0074 (institutional product scope — liquid in scope); ADR-0061 (benchmarks & attribution — benchmark overlay, tracking error, information ratio); ADR-0067, ADR-0072, ADR-0073 (the per-investment and overview surfaces the archetypes extend); ADR-0078 (RLS in tenant context).
- Successor, to be written: portfolio-level TWR/IRR aggregation for mixed-sleeve portfolios.
- `liquid_spike.py` — the throwaway data spike that pinned the two return conventions; its column choices seeded this schema.
