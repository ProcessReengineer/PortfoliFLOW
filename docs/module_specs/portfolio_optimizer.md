# Module specification — `PortfolioOptimizer` (retired scaffold)

Source: `modules/front_office/portfolio_optimizer.py`  
Retired by: ADR-0094 (GUI Sunset Stage 1), inventory item R9

This module was a registered `BaseModule` scaffold whose public methods were unimplemented `...` bodies (the capability shipped as services + web sections instead). Its docstring specification is preserved verbatim below; the scaffold class itself was deleted.

## Module docstring

```text
Front Office — Portfolio Optimiser module.

Purpose:
    Construct optimal portfolio allocations using mean-variance optimisation
    (Markowitz), risk-parity, and Black-Litterman approaches.  Returns the
    efficient frontier, optimal weights, and portfolio-level statistics.

Inputs:
    - Expected returns vector (pandas Series)
    - Covariance matrix (pandas DataFrame)
    - Constraints dict (long-only, weight bounds, sector limits, etc.)
    - Optimisation objective (``"max_sharpe"``, ``"min_vol"``, ``"risk_parity"``)

Outputs:
    - Optimal weight vector (pandas Series)
    - Efficient frontier data points (list of dicts)
    - Portfolio-level statistics (return, vol, Sharpe)

Dependencies (internal):
    - ``core.base_module.BaseModule``
    - ``core.exceptions.ValidationError``
    - ``modules.front_office.statistics.Statistics``

Dependencies (external):
    - ``pandas``
    - ``numpy``
    - ``scipy.optimize``

```

## `class PortfolioOptimizer(BaseModule)`

```text
Mean-variance and risk-parity portfolio optimisation.

    Attributes:
        module_name: ``"portfolio_optimizer"``
        module_area: ``"front_office"``
    
```

## `def run(self, *args: Any, **kwargs: Any) -> dict`

```text
Run portfolio optimisation.

        Keyword Args:
            expected_returns (pd.Series): Expected return per asset.
            covariance (pd.DataFrame): Asset covariance matrix.
            objective (str): ``"max_sharpe"``, ``"min_vol"``, or
                ``"risk_parity"``.
            constraints (dict): Optional constraint overrides.
            risk_free_rate (float): Annualised risk-free rate; defaults to 0.0.

        Returns:
            dict: Keys ``status``, ``weights`` (Series), ``metrics`` (dict),
                ``frontier`` (list[dict]).

        Raises:
            ValidationError: If inputs are inconsistent or under-determined.
        
```

## `def max_sharpe(self, expected_returns: pd.Series, covariance: pd.DataFrame, risk_free_rate: float=0.0, weight_bounds: tuple[float, float]=(0.0, 1.0)) -> pd.Series`

```text
Find weights that maximise the Sharpe ratio.

        Args:
            expected_returns: Expected return per asset (annualised).
            covariance: Annualised covariance matrix.
            risk_free_rate: Annualised risk-free rate.
            weight_bounds: ``(min_weight, max_weight)`` applied to each asset.

        Returns:
            Series of optimal weights indexed by asset name.

        Raises:
            ValidationError: If the optimisation fails to converge.
        
```

## `def min_volatility(self, covariance: pd.DataFrame, weight_bounds: tuple[float, float]=(0.0, 1.0)) -> pd.Series`

```text
Find weights that minimise portfolio volatility.

        Args:
            covariance: Annualised covariance matrix.
            weight_bounds: ``(min_weight, max_weight)`` per asset.

        Returns:
            Series of optimal weights indexed by asset name.
        
```

## `def risk_parity(self, covariance: pd.DataFrame) -> pd.Series`

```text
Find weights such that each asset contributes equally to total risk.

        Args:
            covariance: Annualised covariance matrix.

        Returns:
            Series of risk-parity weights indexed by asset name.
        
```

## `def efficient_frontier(self, expected_returns: pd.Series, covariance: pd.DataFrame, num_points: int=50) -> list[dict]`

```text
Sample the efficient frontier.

        Args:
            expected_returns: Expected return per asset (annualised).
            covariance: Annualised covariance matrix.
            num_points: Number of frontier points to compute.

        Returns:
            List of dicts, each with keys ``"volatility"``, ``"return"``,
            ``"sharpe"``, and ``"weights"``.
        
```
