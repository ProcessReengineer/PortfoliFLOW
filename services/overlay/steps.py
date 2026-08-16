# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The two path primitives the executors transform through (ADR-0104 §2).

A plan path — a value path or a cash path — is a **balance** series, not a
flow series: the value at a date is the level that holds *from* that date
until the next observation. The executors do exactly two things to such a
series, and each is written once here:

* :func:`add_step` — **change the level by an amount** from a date onward. The
  **settle-against-cash primitive** (ADR-0104 §2): the cash side of an inserted
  transaction and the two half-steps of a re-paced flow, and equally the
  value-path step of the inserted transaction itself.
* :func:`scale_after` — **rescale the level by a factor** after a date. The
  primitive of the ``market_shock`` (ADR-0104 §2): a per-cent level shift is
  multiplicative, so it scales each point rather than displacing all of them
  by one constant.

The distinction is the economics, not a convenience. A step is an *amount*: a
trade moves the same €C into the position at every later point. A scale is a
*ratio*: a −20 % mark-down takes a fifth off each later point, and how much
that is in euros depends on the point. Expressing the shock as a step would
mean picking one date's fifth and freezing it, which is not what a level shift
is.

Two properties matter to the callers of both:

* **They never mutate.** The caller gets a new series; the frames it was
  handed stay bit-identical (ADR-0104 §2, the purity contract, and the
  no-mutation regression anchor).
* **They are Decimal-safe.** A plan path assembled from ``Numeric`` columns
  holds :class:`~decimal.Decimal` values, and money must not round-trip through
  a float to gain a step or a shock. A path whose dtype is ``object`` is taken
  to hold Decimals and keeps them; a numeric path takes the change as a float.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import cast

import pandas as pd


def _index_key(index: pd.Index, effective: date) -> object:
    """Return ``effective`` in the key type the index is addressed by.

    Plan paths are assembled with a :class:`pandas.DatetimeIndex`, whose keys
    are :class:`pandas.Timestamp`. A path indexed by plain :class:`date`
    objects (an ``object`` index) is addressed by the date itself. Both are
    supported so the primitive imposes no index convention on the S2.2
    assembly seam.
    """
    if isinstance(index, pd.DatetimeIndex):
        return pd.Timestamp(effective)
    return effective


def _holds_decimal(path: pd.Series) -> bool:
    """Whether the path carries Decimal values rather than machine floats."""
    return path.dtype == object


def zero_path() -> pd.Series:
    """An empty, Decimal-valued balance path — a level of zero throughout.

    The pre-history level of any balance is zero, so an *absent* path and an
    empty one mean the same thing. :func:`add_step` therefore turns this into
    a single-point path at the step's effective date, which is exactly what
    an ``insert_transaction`` on an investment carrying no plan NAV needs
    (ADR-0104 §2; the contributing-nothing semantics of
    :func:`services.investments.aum.build_nav_series`).

    Returns:
        An empty series with a :class:`pandas.DatetimeIndex` and ``object``
        dtype — so the first step lands as a :class:`~decimal.Decimal`.
    """
    return pd.Series(dtype="object", index=pd.DatetimeIndex([]))


def add_step(path: pd.Series, effective: date, amount: Decimal) -> pd.Series:
    """Add ``amount`` to a balance path from ``effective`` onward.

    Every point of ``path`` dated at or after ``effective`` gains ``amount``.
    If ``effective`` is not already in the index, a point is inserted there
    first, carrying forward the balance in force at that moment — the value of
    the latest earlier point, or zero where the path has no earlier point —
    and the index is returned sorted. The result is the same series with a
    step in it, never a re-based one: history is arithmetically untouched,
    which is what makes the ADR-0104 §5 identical-history invariant hold by
    construction rather than by assertion.

    Args:
        path: The balance path. Assumed to carry a unique index. Not mutated.
        effective: The date the step takes effect — inclusive.
        amount: The signed step. Positive raises the balance from
            ``effective`` onward, negative lowers it. A cash settlement is
            the negated consideration; a re-paced flow is a negative step at
            its old date and a positive one at its new date.

    Returns:
        A new series: ``path`` with the step applied, index sorted, and the
        value type of the input preserved (Decimal stays Decimal).
    """
    key = _index_key(path.index, effective)
    stepped = path.copy(deep=True)

    if _holds_decimal(stepped):
        delta: Decimal | float = amount
        zero: Decimal | float = Decimal(0)
    else:
        delta = float(amount)
        zero = 0.0

    if key not in stepped.index:
        # Boolean-mask indexing narrows to an Index; pandas carries no stubs,
        # so pyright infers a scalar branch that cannot occur here.
        earlier = cast(pd.Index, stepped.index[stepped.index < key])
        carried = stepped.loc[earlier.max()] if len(earlier) else zero
        stepped.loc[key] = carried
        stepped = stepped.sort_index()

    at_or_after = stepped.index >= key
    stepped.loc[at_or_after] = stepped.loc[at_or_after].map(lambda level: level + delta)
    return stepped


def scale_after(path: pd.Series, after: date, factor: Decimal) -> pd.Series:
    """Multiply a balance path's levels by ``factor`` strictly after ``after``.

    The ``market_shock`` primitive (ADR-0104 §2). Every point dated **strictly
    after** ``after`` is multiplied by ``factor``; every point at or before it
    is returned bit-identical.

    **Strictly after, and inclusive-at is not an option.** ``after`` is the
    plan/actual seam t₀, and the observation *at* the seam is the last
    **actual** — a realised valuation. ADR-0104 §5 is binding that an overlay
    never touches actuals ("left of t₀ both worlds are the same path by
    definition"), and :func:`services.overlay.executors.execute_insert_transaction`
    already refuses a trade dated at or before the seam for the same reason. A
    shock that rewrote the seam value would restate a NAV the book has already
    reported. ADR-0104 §2's "timing v1 = immediate at t₀" is a statement about
    the *regime* — full magnitude at once, no ramp and no lag — not a licence to
    re-mark the last actual: the first **plan** point already carries the whole
    shock, which is what "immediate" buys.

    Unlike :func:`add_step`, this inserts **no** index point. A step has to
    establish the level in force at its effective date; a rescale has nothing to
    establish — it acts on the observations that exist, and an empty path scales
    to an empty path. An investment carrying no plan value path is therefore
    shocked to nothing, which is the correct answer rather than a special case.

    Args:
        path: The balance path. Assumed to carry a unique index. Not mutated.
        after: The date the rescale takes effect **after** — exclusive.
        factor: The multiplier. ``1`` is the identity; ``Decimal("0.8")`` is a
            −20 % level shift.

    Returns:
        A new series: ``path`` with the post-``after`` levels rescaled, the
        index unchanged, and the value type of the input preserved (Decimal
        stays Decimal).
    """
    key = _index_key(path.index, after)
    scaled = path.copy(deep=True)

    multiplier: Decimal | float = factor if _holds_decimal(scaled) else float(factor)

    strictly_after = scaled.index > key
    scaled.loc[strictly_after] = scaled.loc[strictly_after].map(lambda level: level * multiplier)
    return scaled


__all__ = ["add_step", "scale_after", "zero_path"]
