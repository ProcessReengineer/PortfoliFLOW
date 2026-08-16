# Module specification — `Statistics` (retired scaffold)

Source: `modules/front_office/statistics.py`  
Retired by: ADR-0094 (GUI Sunset Stage 1), inventory item R9

This module was a registered `BaseModule` scaffold whose public methods were unimplemented `...` bodies (the capability shipped as services + web sections instead). Its docstring specification is preserved verbatim below; the scaffold class itself was deleted.

## Module docstring

```text
Front Office — Statistics module.

Purpose:
    Compute risk/return statistics for funds and portfolios: annualised return,
    volatility, Sharpe, Sortino, Calmar, VaR, CVaR, correlation matrices.

Inputs:
    - DataFrame of period returns (output of ``TimeSeries.compute_returns()``)
    - Risk-free rate (scalar or series)
    - Confidence level for VaR/CVaR

Outputs:
    - Dict of scalar risk/return metrics per fund
    - Correlation/covariance matrices

Dependencies (internal):
    - ``core.base_module.BaseModule``
    - ``core.exceptions.ValidationError``

Dependencies (external):
    - ``pandas``
    - ``numpy``
    - ``scipy``

```

## `class Statistics(BaseModule)`

```text
Risk/return statistics for fund return series.

    Attributes:
        module_name: ``"statistics"``
        module_area: ``"front_office"``
    
```

## `def run(self, *args: Any, **kwargs: Any) -> dict`

```text
Compute a full statistics report for the given return series.

        Keyword Args:
            returns (pd.DataFrame): Period return DataFrame.
            risk_free_rate (float): Annualised risk-free rate. Defaults to 0.0.
            confidence (float): Confidence level for VaR/CVaR (e.g. 0.95).
            periods_per_year (int): Trading periods per year (252 for daily).

        Returns:
            dict: Keys ``status``, ``metrics`` (per-fund dict),
                ``correlation`` (DataFrame), ``covariance`` (DataFrame).
        
```

## `def annualised_return(self, returns: pd.Series, periods_per_year: int=252) -> float`

```text
Compute the annualised geometric return.

        Args:
            returns: Series of period returns.
            periods_per_year: Number of return periods in a year.

        Returns:
            Annualised return as a float.
        
```

## `def annualised_volatility(self, returns: pd.Series, periods_per_year: int=252) -> float`

```text
Compute annualised return volatility (standard deviation).

        Args:
            returns: Series of period returns.
            periods_per_year: Number of return periods in a year.

        Returns:
            Annualised volatility as a float.
        
```

## `def sharpe_ratio(self, returns: pd.Series, risk_free_rate: float=0.0, periods_per_year: int=252) -> float`

```text
Compute the Sharpe ratio.

        Args:
            returns: Series of period returns.
            risk_free_rate: Annualised risk-free rate.
            periods_per_year: Number of return periods in a year.

        Returns:
            Sharpe ratio as a float.
        
```

## `def sortino_ratio(self, returns: pd.Series, risk_free_rate: float=0.0, periods_per_year: int=252) -> float`

```text
Compute the Sortino ratio (uses downside deviation).

        Args:
            returns: Series of period returns.
            risk_free_rate: Annualised risk-free rate.
            periods_per_year: Number of return periods in a year.

        Returns:
            Sortino ratio as a float.
        
```

## `def value_at_risk(self, returns: pd.Series, confidence: float=0.95, method: str='historical') -> float`

```text
Compute Value-at-Risk.

        Args:
            returns: Series of period returns.
            confidence: Confidence level (e.g. 0.95 for 95% VaR).
            method: ``"historical"`` or ``"parametric"``.

        Returns:
            VaR as a positive float representing the potential loss.
        
```

## `def correlation_matrix(self, returns: pd.DataFrame) -> pd.DataFrame`

```text
Compute the pairwise Pearson correlation matrix.

        Args:
            returns: DataFrame of period returns (funds as columns).

        Returns:
            Symmetric correlation matrix DataFrame.
        
```
