# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for the market-linked predicate (ADR-0090, #036 slice 5).

Pure — no DB, no network. Exercises the four quadrants of the eligibility
predicate (``investment_type`` market-linked or not × a primary market-usable
identifier present or not) plus the scheme boundary (``cusip`` / ``internal``
are valid identifier schemes but not market-usable, so they do not confer
eligibility), the ADR-0097 §9 ``valuation_mode`` clause (only ``'unitised'``
is eligible), and ``primary_market_identifier``'s selection. The type/scheme
quadrants pass ``valuation_mode='unitised'`` so a ``False`` there is
attributable to the type/scheme gate, not the mode.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from core.repositories.investment_identifier_repository import (
    InvestmentIdentifierDTO,
)
from services.investments.market_linked import (
    MARKET_LINKED_TYPES,
    MARKET_USABLE_SCHEMES,
    is_market_linked,
    primary_market_identifier,
)


def _ident(scheme: str, value: str, *, is_primary: bool) -> InvestmentIdentifierDTO:
    now = datetime.now(timezone.utc)
    return InvestmentIdentifierDTO(
        id=uuid4(),
        tenant_id=uuid4(),
        investment_id=uuid4(),
        scheme=scheme,
        value=value,
        is_primary=is_primary,
        source="manual",
        created_by=uuid4(),
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# The four quadrants: type (market-linked or not) × primary market id present
# ---------------------------------------------------------------------------


def test_q1_market_type_with_primary_ticker_is_eligible() -> None:
    ids = [_ident("ticker", "ACME", is_primary=True)]
    assert is_market_linked("listed_equity", ids, "unitised") is True


def test_q2_market_type_without_primary_market_id_is_ineligible() -> None:
    # A non-primary ticker is present, but nothing primary+market-usable.
    ids = [_ident("ticker", "ACME", is_primary=False)]
    assert is_market_linked("listed_equity", ids, "unitised") is False


def test_q3_nonmarket_type_with_primary_ticker_is_ineligible() -> None:
    ids = [_ident("ticker", "ACME", is_primary=True)]
    assert is_market_linked("private_equity", ids, "unitised") is False


def test_q4_nonmarket_type_without_identifiers_is_ineligible() -> None:
    assert is_market_linked("private_equity", [], "unitised") is False


# ---------------------------------------------------------------------------
# Type + scheme boundaries
# ---------------------------------------------------------------------------


def test_listed_bonds_with_primary_isin_is_eligible() -> None:
    ids = [_ident("isin", "DE0001234567", is_primary=True)]
    assert is_market_linked("listed_bonds", ids, "unitised") is True


def test_primary_cusip_alone_is_not_eligible() -> None:
    # cusip is a valid scheme but not in the market-usable set (ADR-0090).
    ids = [_ident("cusip", "037833100", is_primary=True)]
    assert is_market_linked("listed_equity", ids, "unitised") is False


def test_primary_internal_alone_is_not_eligible() -> None:
    ids = [_ident("internal", "OPS-42", is_primary=True)]
    assert is_market_linked("listed_equity", ids, "unitised") is False


def test_all_market_linked_types_covered() -> None:
    # Guard against silent type-set drift; the eight canonical types minus the
    # two market-linked ones must be ineligible even with a primary ticker.
    for t in (
        "private_equity",
        "private_debt",
        "real_estate",
        "infra_equity",
        "cash",
        "other",
    ):
        assert t not in MARKET_LINKED_TYPES
        assert is_market_linked(t, [_ident("ticker", "X", is_primary=True)], "unitised") is False
    for t in MARKET_LINKED_TYPES:
        assert is_market_linked(t, [_ident("ticker", "X", is_primary=True)], "unitised") is True


# ---------------------------------------------------------------------------
# Cash is denied on type alone, permanently (ADR-0103 §1)
# ---------------------------------------------------------------------------


def test_cash_is_never_market_linked() -> None:
    """Membership pin: widening this set would enable live ingest for cash.

    ADR-0103 §1: *the live-ingest eligibility of cash remains denied — cash is
    excluded from live ingest regardless of mode*. The pin exists so a future
    widening of MARKET_LINKED_TYPES cannot quietly grant a currency a market
    price; it must trip this named test first.
    """
    assert "cash" not in MARKET_LINKED_TYPES


def test_cash_dressed_as_a_listed_instrument_is_still_denied() -> None:
    """The strongest form: cash clears every other clause and is still refused.

    Since ADR-0103 §1 a cash position *is* ``valuation_mode='unitised'``, so it
    satisfies clause (3); give it a primary ISIN and it satisfies clause (2)
    too. Clause (1) is what still refuses it — on type alone. A currency has no
    market price to fetch, and its prices are the stored unity rows that only
    the import path writes.
    """
    ids = [_ident("isin", "EU0000000EUR", is_primary=True)]
    assert is_market_linked("cash", ids, "unitised") is False


# ---------------------------------------------------------------------------
# The valuation_mode clause (ADR-0097 §9): only 'unitised' is eligible
# ---------------------------------------------------------------------------


def test_reported_mode_is_ineligible_even_when_type_and_id_qualify() -> None:
    # A listed_equity with a primary ticker but valuation_mode='reported' is
    # NOT live-series-eligible: a per-share series has no correct landing spot
    # in its NAV-driven book (findings F1/F6). The refresh skips it pre-fetch.
    ids = [_ident("ticker", "ACME", is_primary=True)]
    assert is_market_linked("listed_equity", ids, "reported") is False


def test_unitised_mode_flips_an_otherwise_eligible_investment_on() -> None:
    ids = [_ident("ticker", "ACME", is_primary=True)]
    assert is_market_linked("listed_equity", ids, "unitised") is True


def test_unitised_mode_alone_does_not_rescue_a_private_type() -> None:
    # The three gates are conjunctive: mode='unitised' cannot make a
    # private-markets type or a missing primary identifier eligible.
    ids = [_ident("ticker", "ACME", is_primary=True)]
    assert is_market_linked("private_equity", ids, "unitised") is False
    assert is_market_linked("listed_equity", [], "unitised") is False


# ---------------------------------------------------------------------------
# primary_market_identifier selection
# ---------------------------------------------------------------------------


def test_primary_market_identifier_returns_the_primary_market_scheme() -> None:
    figi = _ident("figi", "BBG000B9XRY4", is_primary=True)
    ids = [
        _ident("ticker", "ACME", is_primary=False),
        figi,
        _ident("isin", "US0378331005", is_primary=False),
    ]
    picked = primary_market_identifier(ids)
    assert picked is not None
    assert picked.scheme == "figi"
    assert picked.value == "BBG000B9XRY4"


def test_primary_market_identifier_none_when_primary_is_non_market_scheme() -> None:
    ids = [_ident("cusip", "037833100", is_primary=True)]
    assert primary_market_identifier(ids) is None


def test_market_usable_schemes_are_a_subset_of_the_closed_scheme_set() -> None:
    from core.models.investment_identifier import IDENTIFIER_SCHEMES

    assert MARKET_USABLE_SCHEMES <= IDENTIFIER_SCHEMES
    assert "cusip" not in MARKET_USABLE_SCHEMES
    assert "internal" not in MARKET_USABLE_SCHEMES
