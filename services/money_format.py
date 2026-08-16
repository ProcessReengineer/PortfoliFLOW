# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Currency-aware money formatting (ADR-0101 §3).

The single source of the *prefix rule* — how a currency code becomes a
display prefix — shared by the two consumers that must agree on it:

* :func:`web.routes.overview._format_money_compact` (the Overview hero,
  invested-book and cash strings), and
* the money-bearing chart specs under ``services/chart_specs/``
  (bar labels, hover templates, value-axis titles).

Keeping the rule here rather than in either consumer is what makes the
ADR-0101 §4 invisibility guarantee checkable in one place: for ``EUR`` the
prefix is ``€`` and every downstream string is byte-identical to the
pre-block output.

Pure stdlib — no pandas, no DB, no FastAPI. Importable from
``services/chart_specs/`` without dragging the repository layer into a
spec's import graph (which importing ``services.fx`` would do, since that
package's ``__init__`` reaches the FX repository).
"""

from __future__ import annotations

#: Currencies rendered with a typographic symbol rather than their ISO code.
#: Everything else falls back to the ISO-code prefix (``CHF 1.2M``), which is
#: unambiguous for the long tail and needs no per-currency curation.
_CURRENCY_SYMBOLS: dict[str, str] = {
    "EUR": "€",
    "USD": "$",
    "GBP": "£",
}

#: Used when a caller passes an empty currency. The tenant's functional
#: currency column is ``NOT NULL DEFAULT 'EUR'``, so this is a formatting
#: guard, not a data assumption.
_DEFAULT_CURRENCY = "EUR"


def currency_prefix(currency: str) -> str:
    """Return the display prefix for a currency code.

    The prefix is designed to be concatenated directly in front of a
    number, so the ISO branch carries its own trailing space while the
    symbol branch does not:

    * ``EUR`` → ``"€"``   → ``€1.2M``
    * ``USD`` → ``"$"``   → ``$1.2M``
    * ``GBP`` → ``"£"``   → ``£1.2M``
    * ``CHF`` → ``"CHF "`` → ``CHF 1.2M``

    Args:
        currency: An ISO 4217 code. Case-insensitive; an empty value is
            treated as ``EUR``.

    Returns:
        The display prefix, ready to concatenate.
    """
    code = (currency or _DEFAULT_CURRENCY).upper()
    return _CURRENCY_SYMBOLS.get(code, f"{code} ")


def format_money_compact(value: float, currency: str) -> str:
    """Render a monetary figure as a compact institutional string.

    Thousands collapse to ``k``, millions to ``M``, billions to ``B``, with
    progressively more precision so each band keeps roughly three to four
    significant figures:

    * ``< 1k``     → ``€500``        (``.0f``)
    * ``1k–1M``    → ``€12k``        (``.0f`` on the k-value)
    * ``1M–1B``    → ``€342.6M``     (``.1f``)
    * ``>= 1B``    → ``€1.24B``      (``.2f``)

    The currency only selects the prefix (:func:`currency_prefix`); the
    thresholds, rounding and sign handling are currency-independent, so a
    EUR figure formats exactly as it did before ADR-0101 (§4).

    The output is ASCII/locale-neutral English (ADR-0008). Negative inputs
    keep a leading minus sign ahead of the prefix (``-€1.2M``).

    Args:
        value: The amount to format, in ``currency``.
        currency: The ISO 4217 code the amount is denominated in.

    Returns:
        A compact display string, e.g. ``"€342.6M"`` or ``"CHF 1.24B"``.
    """
    prefix = currency_prefix(currency)
    abs_v = abs(value)
    sign = "-" if value < 0 else ""
    if abs_v >= 1_000_000_000:
        return f"{sign}{prefix}{abs_v / 1_000_000_000:.2f}B"
    if abs_v >= 1_000_000:
        return f"{sign}{prefix}{abs_v / 1_000_000:.1f}M"
    if abs_v >= 1_000:
        return f"{sign}{prefix}{abs_v / 1_000:.0f}k"
    return f"{sign}{prefix}{abs_v:.0f}"


__all__ = ["currency_prefix", "format_money_compact"]
