# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for the pure holdings derivation (ADR-0097 §4).

These tests exercise :mod:`services.investments.holdings` with no DB — the
module operates on the :class:`services.investments.holdings.LedgerTransaction`
protocol, so a lightweight stand-in dataclass drives every case:

* ``derive_holdings`` — cumulative signed sum as a step function, one point
  per distinct ``trade_date``.
* Total order ``(trade_date, created_at, id)`` — input iteration order and
  same-day ties do not change the result.
* Transfer signs — positive and negative transfers both move holdings.
* ``holdings_as_of`` — carry-forward step lookup.
* ``first_negative_holding_date`` — ``None`` when holdings stay
  non-negative; the offending ``trade_date`` otherwise, including intra-day
  overdraw.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

from services.investments.holdings import (
    HoldingPoint,
    derive_holdings,
    first_negative_holding_date,
    holdings_as_of,
)


@dataclass(frozen=True)
class _Txn:
    """Minimal ledger row satisfying the LedgerTransaction protocol."""

    txn_type: str
    trade_date: date
    units: Decimal
    created_at: datetime
    id: UUID


def _txn(
    txn_type: str,
    trade_date: date,
    units: str,
    *,
    created_at: datetime | None = None,
    id: UUID | None = None,
) -> _Txn:
    return _Txn(
        txn_type=txn_type,
        trade_date=trade_date,
        units=Decimal(units),
        created_at=created_at or datetime(2026, 1, 1, tzinfo=timezone.utc),
        id=id or UUID(int=0),
    )


# ---------------------------------------------------------------------------
# derive_holdings — cumulative step function
# ---------------------------------------------------------------------------


def test_derive_holdings_empty_ledger() -> None:
    assert derive_holdings([]) == []


def test_derive_holdings_cumulative_signed_sum() -> None:
    txns = [
        _txn("opening", date(2025, 1, 1), "100"),
        _txn("buy", date(2025, 3, 1), "50"),
        _txn("sell", date(2025, 6, 1), "-30"),
    ]
    assert derive_holdings(txns) == [
        HoldingPoint(date(2025, 1, 1), Decimal("100")),
        HoldingPoint(date(2025, 3, 1), Decimal("150")),
        HoldingPoint(date(2025, 6, 1), Decimal("120")),
    ]


def test_derive_holdings_collapses_same_day_to_end_of_day_point() -> None:
    """Two transactions on one day collapse to a single end-of-day point."""
    txns = [
        _txn("opening", date(2025, 1, 1), "100"),
        _txn(
            "buy",
            date(2025, 3, 1),
            "50",
            created_at=datetime(2026, 1, 1, 9, tzinfo=timezone.utc),
        ),
        _txn(
            "buy",
            date(2025, 3, 1),
            "25",
            created_at=datetime(2026, 1, 1, 10, tzinfo=timezone.utc),
        ),
    ]
    assert derive_holdings(txns) == [
        HoldingPoint(date(2025, 1, 1), Decimal("100")),
        HoldingPoint(date(2025, 3, 1), Decimal("175")),
    ]


def test_derive_holdings_is_input_order_independent() -> None:
    """Shuffled input yields the identical step function (total order)."""
    a = _txn(
        "opening",
        date(2025, 1, 1),
        "100",
        created_at=datetime(2026, 1, 1, 8, tzinfo=timezone.utc),
    )
    b = _txn(
        "buy",
        date(2025, 3, 1),
        "50",
        created_at=datetime(2026, 1, 1, 9, tzinfo=timezone.utc),
    )
    c = _txn(
        "sell",
        date(2025, 6, 1),
        "-30",
        created_at=datetime(2026, 1, 1, 10, tzinfo=timezone.utc),
    )
    assert derive_holdings([c, a, b]) == derive_holdings([a, b, c])


def test_derive_holdings_breaks_same_day_same_time_tie_by_id() -> None:
    """Same date and created_at: id makes the order total and reproducible."""
    ts = datetime(2026, 1, 1, 9, tzinfo=timezone.utc)
    first = _txn("opening", date(2025, 1, 1), "100", created_at=ts, id=UUID(int=1))
    second = _txn("buy", date(2025, 1, 1), "40", created_at=ts, id=UUID(int=2))
    # Regardless of input order the cumulative end-of-day point is 140.
    assert derive_holdings([second, first]) == [
        HoldingPoint(date(2025, 1, 1), Decimal("140")),
    ]


def test_derive_holdings_transfer_signs_both_move_holdings() -> None:
    txns = [
        _txn("opening", date(2025, 1, 1), "100"),
        _txn("transfer", date(2025, 2, 1), "20"),  # transfer in
        _txn("transfer", date(2025, 3, 1), "-15"),  # transfer out
    ]
    assert derive_holdings(txns)[-1] == HoldingPoint(date(2025, 3, 1), Decimal("105"))


# ---------------------------------------------------------------------------
# holdings_as_of — carry-forward
# ---------------------------------------------------------------------------


def test_holdings_as_of_before_first_transaction_is_zero() -> None:
    txns = [_txn("opening", date(2025, 1, 1), "100")]
    assert holdings_as_of(txns, date(2024, 12, 31)) == Decimal("0")


def test_holdings_as_of_carries_forward_between_transactions() -> None:
    txns = [
        _txn("opening", date(2025, 1, 1), "100"),
        _txn("buy", date(2025, 6, 1), "50"),
    ]
    # On the opening day and any day up to (not incl.) the next txn: 100.
    assert holdings_as_of(txns, date(2025, 1, 1)) == Decimal("100")
    assert holdings_as_of(txns, date(2025, 5, 31)) == Decimal("100")
    # On and after the buy: 150.
    assert holdings_as_of(txns, date(2025, 6, 1)) == Decimal("150")
    assert holdings_as_of(txns, date(2030, 1, 1)) == Decimal("150")


# ---------------------------------------------------------------------------
# first_negative_holding_date — the non-negativity check helper
# ---------------------------------------------------------------------------


def test_first_negative_none_when_non_negative_throughout() -> None:
    txns = [
        _txn("opening", date(2025, 1, 1), "100"),
        _txn("sell", date(2025, 6, 1), "-100"),  # exactly zero, still valid
    ]
    assert first_negative_holding_date(txns) is None


def test_first_negative_returns_offending_date() -> None:
    txns = [
        _txn("opening", date(2025, 1, 1), "100"),
        _txn("sell", date(2025, 6, 1), "-150"),  # overdraw
    ]
    assert first_negative_holding_date(txns) == date(2025, 6, 1)


def test_first_negative_catches_intraday_overdraw() -> None:
    """A same-day sell that overdraws is caught even if a later buy restores.

    You cannot sell units you do not hold; the per-transaction scan (not a
    per-day net) flags the sell's date.
    """
    ts_open = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)
    ts_sell = datetime(2026, 1, 1, 9, tzinfo=timezone.utc)
    ts_buy = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
    txns = [
        _txn("opening", date(2025, 1, 1), "100", created_at=ts_open),
        _txn("sell", date(2025, 3, 1), "-150", created_at=ts_sell),
        _txn("buy", date(2025, 3, 1), "200", created_at=ts_buy),
    ]
    # End-of-day net on 2025-03-01 is +150, but the sell mid-day overdraws.
    assert first_negative_holding_date(txns) == date(2025, 3, 1)


def test_first_negative_empty_ledger_is_none() -> None:
    assert first_negative_holding_date([]) is None
