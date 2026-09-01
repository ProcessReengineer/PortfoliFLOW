# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""AuditLogRepository — read-only access to the audit engine's own table.

``audit_log`` is written by ``audit_trigger_function()`` (initial schema,
b001), an ``AFTER INSERT OR UPDATE OR DELETE ... FOR EACH ROW`` trigger
attached to every domain table. Nothing in the application writes it, and
this repository does not either: it is a **read** seam, and the only one.

Why a repository over an audit table at all
-------------------------------------------
Trade-ticket reversal (ADR-0128 §6) may only undo a booking whose emitted
rows are still exactly as the booking left them. Asking that question of the
rows themselves does not work: ``updated_at`` is maintained by *some* update
paths and not others — ``position_transactions`` in particular is written by
plain ORM attribute assignment with no ``onupdate`` and no trigger behind it,
so its ``updated_at`` still reads as the insert time after an edit. A check
built on it would silently pass for two of the four target tables.

The audit trigger has no such gap. It fires on every UPDATE of every target
table regardless of which code path issued it, records ``TG_OP`` literally,
and stamps ``created_at`` with ``NOW()`` — the *transaction* timestamp. That
last property is what makes :meth:`has_update_since` usable as written: a
booking's own writes and the ``trade_ticket_effects`` rows enumerating them
share one transaction and therefore one timestamp, so a **strictly** later
audit row is, by construction, somebody else's edit.

The table is tenant-scoped and RLS-policed (ADR-0035 §7), so this repository
must be constructed on a session from
:func:`core.repositories._session.tenant_context` like any other: another
tenant's audit rows are invisible rather than filtered.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select

from core.models.audit_log import AuditLog
from core.repositories.base import BaseRepository

#: The ``operation`` value the trigger writes for an UPDATE.
#:
#: ``audit_trigger_function()`` inserts ``TG_OP`` verbatim, so the column
#: holds Postgres' own uppercase spelling rather than an application
#: vocabulary.
OPERATION_UPDATE: str = "UPDATE"


class AuditLogRepository(BaseRepository):
    """Read the audit trail for the active tenant. Never writes."""

    async def has_update_since(
        self,
        table_name: str,
        record_id: UUID,
        *,
        after: datetime,
    ) -> bool:
        """Report whether a row was UPDATEd strictly after ``after``.

        The modification test behind a trade-ticket reversal (ADR-0128 §6).
        Only ``UPDATE`` counts: an INSERT is the row coming into existence —
        for an emitted row, the booking itself — and a DELETE is answered by
        the row's absence, which the caller checks directly rather than
        inferring from a log that a re-created row would make ambiguous.

        ``after`` is compared **strictly**, and that is the whole point: the
        audit row's ``created_at`` and the effect's ``emitted_at`` are both
        ``NOW()``, which in Postgres is the transaction timestamp. A write
        made in the booking's own transaction therefore ties rather than
        exceeds, and only a later transaction's edit is reported.

        Args:
            table_name: The audited table, as ``TG_TABLE_NAME`` spells it —
                unqualified and lowercase (``position_transactions``).
            record_id: The audited row's ``id``.
            after: The instant to compare against; typically an effect's
                ``emitted_at``.

        Returns:
            ``True`` if at least one ``UPDATE`` on that row is recorded after
            ``after`` in this tenant, ``False`` otherwise.
        """
        result = await self._session.execute(
            select(AuditLog.id)
            .where(
                AuditLog.table_name == table_name,
                AuditLog.record_id == record_id,
                AuditLog.operation == OPERATION_UPDATE,
                AuditLog.created_at > after,
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None
