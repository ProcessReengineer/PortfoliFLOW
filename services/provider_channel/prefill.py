# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The pre-fill seam: a provider confirmation becomes proposed ticket fields.

ADR-0129 §3: the instance pre-fills the ADR-0128 booking step for user review
— it never books autonomously. That is the whole contract of this module. An
``executed`` message is the provider's claim about what happened; turning a
claim into a booking is an act the operator performs, with the numbers in
front of them, through the machinery ADR-0128 already built.

Two consequences shape the mapping:

* **It maps, it does not compute** (D-amounts). ``gross_amount`` and
  ``net_amount`` are absent from the result on purpose. Net arithmetic is
  stated exactly once in the codebase, in the ticket layer's validation
  ("``net_amount`` stated outright, else ``gross ± fees/taxes``"), and a
  second implementation here would be a second answer waiting to diverge. The
  composer derives the totals from these fields exactly as it does for a
  hand-typed ticket.
* **It carries no status.** Nothing here can move a ticket anywhere; there is
  no lifecycle verb in this module, which is what
  ``tests/services/provider_channel/test_contract.py`` pins.

The free-text ``message`` on the confirmation is *not* mapped: it is
addressed to the human reviewing the pre-fill, not to a ticket column.

Pure: no repository, no session, no clock.
"""

from __future__ import annotations

from typing import Final

from services.provider_channel.schemas import FillPayload

# ---------------------------------------------------------------------------
# Pre-fill keys
#
# Each mirrors a column on the trade-ticket model, so the composer can consume
# the dict without a translation table; pinned by
# tests/services/provider_channel/test_contract.py.
# ---------------------------------------------------------------------------

PREFILL_FIELD_UNITS: Final[str] = "units"
PREFILL_FIELD_PRICE_PER_UNIT: Final[str] = "price_per_unit"
PREFILL_FIELD_FEES: Final[str] = "fees"
PREFILL_FIELD_TAXES: Final[str] = "taxes"
PREFILL_FIELD_CURRENCY: Final[str] = "currency"
PREFILL_FIELD_TRADE_DATE: Final[str] = "trade_date"
PREFILL_FIELD_SETTLEMENT_DATE: Final[str] = "settlement_date"
PREFILL_FIELD_MASTER_DATA: Final[str] = "master_data"

#: mirrors services.transactions.constants.MD_IDENTIFIER_SCHEME
MD_IDENTIFIER_SCHEME: Final[str] = "identifier_scheme"

#: mirrors services.transactions.constants.MD_IDENTIFIER_VALUE
MD_IDENTIFIER_VALUE: Final[str] = "identifier_value"

#: mirrors web/routes/transactions._RESOLVABLE_SCHEMES[0]
IDENTIFIER_SCHEME_ISIN: Final[str] = "isin"


def fill_to_prefill(fill: FillPayload) -> dict[str, object]:
    """Map a provider confirmation onto proposed booking fields.

    The instance pre-fills the ADR-0128 booking step for user review — it
    never books autonomously (ADR-0129 §3).

    Values pass through untouched: no rounding, no arithmetic, no defaults
    beyond ``None`` for an absent settlement date. Totals are deliberately not
    derived here (D-amounts).

    Args:
        fill: The parsed confirmation.

    Returns:
        A fresh dict keyed by the ``PREFILL_FIELD_*`` names. When the
        confirmation states an ISIN the dict also carries
        :data:`PREFILL_FIELD_MASTER_DATA` with the identifier pair; otherwise
        that key is absent. ``fill.message`` is never mapped.
    """
    prefill: dict[str, object] = {
        PREFILL_FIELD_UNITS: fill.units,
        PREFILL_FIELD_PRICE_PER_UNIT: fill.price_per_unit,
        PREFILL_FIELD_FEES: fill.fees,
        PREFILL_FIELD_TAXES: fill.taxes,
        PREFILL_FIELD_CURRENCY: fill.currency,
        PREFILL_FIELD_TRADE_DATE: fill.trade_date,
        PREFILL_FIELD_SETTLEMENT_DATE: fill.settlement_date,
    }
    if fill.isin is not None:
        prefill[PREFILL_FIELD_MASTER_DATA] = {
            MD_IDENTIFIER_SCHEME: IDENTIFIER_SCHEME_ISIN,
            MD_IDENTIFIER_VALUE: fill.isin,
        }
    return prefill


__all__ = [
    "IDENTIFIER_SCHEME_ISIN",
    "MD_IDENTIFIER_SCHEME",
    "MD_IDENTIFIER_VALUE",
    "PREFILL_FIELD_CURRENCY",
    "PREFILL_FIELD_FEES",
    "PREFILL_FIELD_MASTER_DATA",
    "PREFILL_FIELD_PRICE_PER_UNIT",
    "PREFILL_FIELD_SETTLEMENT_DATE",
    "PREFILL_FIELD_TAXES",
    "PREFILL_FIELD_TRADE_DATE",
    "PREFILL_FIELD_UNITS",
    "fill_to_prefill",
]
