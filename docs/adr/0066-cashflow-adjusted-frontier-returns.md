# ADR-0066: Cash-Flow-Adjusted Returns for the Portfolio Analysis Frontier

- **Status:** Accepted — 2026-05-28

## Context

The Portfolio Analysis surface (efficient frontier, tangency portfolio, capital market line, per-investment markers) is orchestrated by
`services/portfolio_analysis/portfolio_analysis_service.py::PortfolioAnalysisService.compute_frontier`.

Step 3 of that method derives each investment's periodic-return series directly from its actual NAV history:

```python
return_series_by_name[inv.name] = compute_total_return_series(nav_series)
```

`compute_total_return_series` (in `services/analytics/investment_returns.py`) is the QT-mirrored primitive

```python
cleaned = nav_series.dropna().sort_index()
return cleaned.pct_change().dropna()
```

i.e. `r_t = NAV_t / NAV_{t-1} − 1`. This is correct for investments whose NAV moves only with the market. It is **economically wrong** for any investment with mid-life capital activity (capital calls, distributions) — every drawdown vehicle (Private Equity, Private Debt, Infrastructure, closed-end Real Estate). On a call day the NAV rises purely because capital was injected; on a distribution day it falls purely because capital was returned. `pct_change()` reads both as market return.

### Symptom

On test data v25 the frontier scatter shows (NAV-`pct_change` vs the correct cash-flow-adjusted figure):

| Inv | Class | pct_change ret% | pct_change vol% | correct ret% | correct vol% |
|---|---|---:|---:|---:|---:|
| N | PE Secondaries (vintage 2024) | 66.5 | 52.4 | 7.6 | 9.2 |
| G | PE Large Cap (vintage 2023) | 40.4 | 48.0 | 8.2 | 11.8 |
| F | PE Small Cap (vintage 2020) | 22.8 | 29.5 | 9.5 | 12.8 |
| E | PE EU Mid Cap (harvest) | −6.4 | 22.4 | 8.8 | 11.2 |
| Q | Infra Equity FoF | 27.6 | 36.5 | 6.2 | 7.2 |
| O | Senior Direct Lending | 15.1 | 31.7 | 5.3 | 3.4 |

Liquid sleeves (A, B, C, H, I, J, K, L, M, P, R, T) are unaffected because they have no mid-life flows; their `pct_change` and adjusted figures are bitwise identical. That is the diagnostic fingerprint: only investments with flows are distorted.

### What is NOT the cause

The test data is internally consistent. The `total return actual` sheet (and the `total_return` NAV-kind path) carries the correct return series. The defect is in how the Portfolio Analysis service derives returns — it uses the NAV-only primitive instead of a cash-flow-aware one.

### Constraints discovered in the code

- `compute_total_return_series(nav_series)` is shared by **statistics**, **benchmark comparison**, **portfolio review**, and **investment service**. Its NAV-`pct_change` behaviour is pinned by their parity tests. **It must not change.** The fix is additive.
- There is **no DataStore singleton in the web path.** Data is loaded through tenant-scoped repositories (`InvestmentNavRepository`, `InvestmentCashflowRepository`) and orchestrated in the service. The analytics layer stays pure (DataFrame in / DataFrame out) per ADR-0013 and ADR-0045 §3.
- `services/analytics/` may import repository DTOs but must not contain `AsyncSession` / `async_session` / `get_db_session` / `from sqlalchemy` (the purity regression test `tests/regression/test_analytics_layer_pure.py` scans for these).
- The cashflow DataFrame contract is already established (`compute_net_capital_gain`, `investment_service`): a flat frame with columns `flow_timestamp`, `flow_type`, `amount`, where `amount` is **signed** — capital calls negative, distributions positive.
- `InvestmentCashflowRepository.list_by_investments_and_kind(ids, "actual")` already exists and is batched (no N+1), mirroring the NAV fetch the service already uses.
- The covariance / annualisation convention lives in `derive_expected_returns_and_cov` (geometric mean `(1+μ)**252−1`, linear cov `Σ*252`) and must remain untouched — only the *input return series* changes.

## Decision

Add a new pure analytics function and route the Portfolio Analysis frontier through it. Two coordinated, additive changes:

### 1. New pure function in `services/analytics/investment_returns.py`

```python
def compute_cashflow_adjusted_return_series(
    nav_series: pd.Series,
    cashflows: pd.DataFrame,
) -> pd.Series:
    """Period-over-period return series corrected for capital flows.

    For each consecutive pair of NAV observations the market return is

        r_t = (NAV_t + a_t) / NAV_{t-1} - 1

    where ``a_t`` is the net SIGNED cashflow amount falling in the
    half-open interval (date_{t-1}, date_t]: distributions positive,
    calls negative — the same `amount` convention as
    :func:`compute_net_capital_gain`. With no flows in the interval
    ``a_t = 0`` and the formula reduces exactly to ``pct_change()``,
    so liquid investments are unchanged bit-for-bit.

    Args mirror compute_net_capital_gain: `nav_series` indexed by
    as_of_date; `cashflows` a flat actuals frame with `flow_timestamp`
    and signed `amount`. Returns a Series indexed by the later date of
    each pair (same index shape as compute_total_return_series).
    """
```

Rationale for the interval aggregation: NAV observations may be sparse (quarterly) while flows are daily. A flow between two NAV dates must be attributed to the return of that interval, not dropped. Aggregating signed `amount` over `(date_{t-1}, date_t]` is the discrete-time form of the same `NCG = NAV + cumsum(amount)` identity already used in `compute_net_capital_gain`; the two are mutually consistent by construction.

The function imports only pandas/numpy plus (optionally) the existing cashflow-aggregation helper already in the module. It contains no DB/Qt/FastAPI references — purity preserved.

### 2. Route the frontier through it in `PortfolioAnalysisService.compute_frontier`

- Add `cashflows: InvestmentCashflowRepository` to the service constructor (alongside `investments`, `navs`).
- After the batched NAV fetch, add a batched cashflow fetch:
  `cf_rows_by_inv = await self._cashflows.list_by_investments_and_kind(investment_ids, "actual")`.
- For each investment, build the flat cashflow frame (`flow_timestamp`, `amount`) from the DTO list — the same construction `investment_service` already uses — apply the same `as_of_date` truncation that NAVs receive, and call `compute_cashflow_adjusted_return_series(nav_series, cashflows_df)` **instead of** `compute_total_return_series(nav_series)`.
- Everything downstream (`restrict_to_common_window`, `derive_expected_returns_and_cov`, the optimiser, tangency, min-variance, CML, per-investment markers) is unchanged.

The `compute_current_portfolio_position` path (latest-NAV weights) is unaffected — weights are NAV shares, not returns.

## Consequences

**Positive**

- Frontier scatter is correct and presentable: every investment plots at its true risk/return coordinate; the N / E / G / O / Q distortions disappear.
- Liquid investments are bitwise unchanged (the existing `test_portfolio_analysis_service` shape/finiteness assertions and the QT-consistency frontier test continue to pass, because they seed no cashflows → `a_t = 0` everywhere → identical numerics).
- The new function reuses the proven signed-amount cashflow identity from `compute_net_capital_gain`, so the two stay consistent.
- `compute_total_return_series` and all its other consumers (statistics, benchmark, portfolio review, investment service) are untouched — no risk to their parity tests.

**Negative**

- One constructor-signature change on `PortfolioAnalysisService` (add the cashflow repository). The route handler and tests that construct the service must pass the extra repository. Small, mechanical.

**Neutral**

- Whether the Statistics Surface and Front Office Total-Return charts should *also* switch to cash-flow-adjusted returns is a **separate** question, deliberately out of scope here. Those surfaces show a single investment's own return path where `pct_change` is the established (QT-mirrored) definition, and changing them would alter pinned parity tests. If desired, that becomes its own ADR. This ADR fixes only the optimiser, which is where the visible defect is and where cross-investment comparability genuinely requires flow adjustment.

## Tests

1. **Unit — reduction property**: with an empty cashflow frame, `compute_cashflow_adjusted_return_series(nav, empty)` equals `compute_total_return_series(nav)` exactly (`pd.testing.assert_series_equal`).
2. **Unit — correctness on synthetic flows**: a short NAV series built from a known constant market return with one injected call and one distribution between observations; assert the adjusted series recovers the known return to ≤ 1e-12.
3. **Unit — interval attribution**: a flow dated strictly between two NAV observations is attributed to that interval's return (not the next, not dropped).
4. **Integration — `test_portfolio_analysis_service`**: extend with one investment carrying cashflows; assert its frontier marker matches the cash-flow-adjusted annualised stats (not the `pct_change` stats). Keep the existing no-cashflow tests green unchanged.
5. **Purity**: the new function is covered by the existing `tests/regression/test_analytics_layer_pure.py` source scan (no new exemptions needed).

## Related

- ADR-0013 (analytics-layer purity), ADR-0045 §3 (analytics-service foundation), ADR-0043 §1 (signed-amount cashflow convention).
- `compute_net_capital_gain` — the existing function whose `NCG = NAV + cumsum(amount)` identity this fix mirrors.
- `PortfoliFLOW_Testdaten_v25.xlsx` — golden source for the integration assertions (reference figures in the table above).
