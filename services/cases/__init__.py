# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The Cases services (ADR-0138).

Where we stand (ADR-0138 §6) — the live, read-only block of a case,
composed at render time — lives in :mod:`services.cases.stand`. The
system lines (ADR-0138 §7) — the event vocabulary, the fixed templates and
the writers the NAV seams and Irene's beat call — live in
:mod:`services.cases.system_lines` (P-CS-2b, P-CS-2c). The wake-up's
tenant enumeration — the one cross-tenant read of the tick runner's third
step — lives in :mod:`services.cases.wake`.
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
from services.cases.system_lines import (
    EVENT_JOB_LANDED,
    EVENT_JOB_REQUESTED,
    EVENT_NAV_RESTATED,
    EVENT_PRESS_CLUSTER,
    EVENT_SUBJECT_WITHIN_CEILING,
    EVENT_TICKET_BOOKED,
    NavSeries,
    job_landed,
    job_requested,
    linked_investment_ids,
    nav_changes,
    nav_restated,
    nav_restated_after,
    snapshot_actual_series,
    subject_within_ceiling,
    ticket_booked,
)
from services.cases.wake import find_case_tick_tenants

__all__ = [
    "EVENT_JOB_LANDED",
    "EVENT_JOB_REQUESTED",
    "EVENT_NAV_RESTATED",
    "EVENT_PRESS_CLUSTER",
    "EVENT_SUBJECT_WITHIN_CEILING",
    "EVENT_TICKET_BOOKED",
    "CounterpartyReading",
    "FxReading",
    "InvestmentReading",
    "NavSeries",
    "StandContext",
    "SubjectReading",
    "SubjectStand",
    "SystemLineReading",
    "TicketReading",
    "WhereWeStand",
    "book_values_for",
    "compose",
    "find_case_tick_tenants",
    "headline_for",
    "job_landed",
    "job_requested",
    "linked_investment_ids",
    "nav_changes",
    "nav_restated",
    "nav_restated_after",
    "snapshot_actual_series",
    "subject_within_ceiling",
    "ticket_booked",
    "to_snapshot",
]
