# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The Cases services (ADR-0138).

Where we stand (ADR-0138 §6) — the live, read-only block of a case,
composed at render time — lives in :mod:`services.cases.stand`. The
system-line templates and writers follow (P-CS-2b, P-CS-2c).
"""

from services.cases.stand import (
    CounterpartyReading,
    FxReading,
    InvestmentReading,
    StandContext,
    SubjectReading,
    SubjectStand,
    SystemLineReading,
    TicketReading,
    WhereWeStand,
    book_values_for,
    compose,
    headline_for,
    to_snapshot,
)

__all__ = [
    "CounterpartyReading",
    "FxReading",
    "InvestmentReading",
    "StandContext",
    "SubjectReading",
    "SubjectStand",
    "SystemLineReading",
    "TicketReading",
    "WhereWeStand",
    "book_values_for",
    "compose",
    "headline_for",
    "to_snapshot",
]
