# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Extend investment_identifiers.scheme with provider-native fund schemes.

Revision ID: b023_extend_identifier_schemes
Revises: b022_add_market_data_schedule
Create Date: 2026-07-07 13:00:00 UTC

Per ADR-0096 (Identifier Scheme-Set Extension). ADR-0090 fixed
``investment_identifiers.scheme`` as a closed, CHECK-enforced set
``('isin','ticker','figi','cusip','internal')`` — the listed world plus
a self-assigned namespace. The next provider class breaks that
assumption: private-markets data providers key on **proprietary
identifiers** (a Preqin fund ID, a PitchBook profile ID) that a
private-equity fund carries in place of an ISIN/ticker/FIGI it will never
have. This migration swaps the CHECK to the ADR-0096 set

    ('isin','ticker','figi','cusip','internal','preqin','pitchbook')

and does **nothing else**. All three ADR-0090 uniqueness rules carry over
untouched and are exactly right for provider IDs (ADR-0096 §1):

- ``UNIQUE (investment_id, scheme, value)`` — one mapping row per fact;
- partial ``UNIQUE (tenant_id, scheme, value) WHERE scheme <> 'internal'``
  — a given Preqin fund ID maps to one investment per tenant, because a
  provider ID is an external identity like an ISIN;
- partial one-primary-per-investment — a fund whose only identifier is
  its Preqin ID can carry it as primary.

No column, index, or RLS change is needed: only the enumerated value set
grows. The two application-side sources of truth (the ``IDENTIFIER_SCHEMES``
frozensets in ``core/models/investment_identifier.py`` and
``services/market_data/dto.py``) are updated in the same commit — the
"two sources of truth" consequence of ADR-0096, the same discipline as
every closed set in the codebase.

Downgrade narrows the CHECK back to the five-scheme set. This is a
**narrowing** constraint swap: if any ``preqin`` / ``pitchbook`` row
exists at downgrade time the new CHECK is rejected by Postgres (it
validates the constraint against existing rows) and the downgrade fails
loudly. That is acceptable and standard for a value-set-narrowing
migration — production never rolls this back, and a dev rollback is
expected to first delete or re-scheme any provider-native rows (the same
lossy-in-reverse stance documented for the b017 natural-key narrowing).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b023_extend_identifier_schemes"
down_revision: str | None = "b022_add_market_data_schedule"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CONSTRAINT = "ck_investment_identifiers_scheme"
_TABLE = "investment_identifiers"

# The ADR-0096 seven-scheme set (upgrade target).
_SCHEMES_NEW = "'isin', 'ticker', 'figi', 'cusip', 'internal', 'preqin', 'pitchbook'"
# The ADR-0090 five-scheme set (downgrade target).
_SCHEMES_OLD = "'isin', 'ticker', 'figi', 'cusip', 'internal'"


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # Swap the CHECK: drop the five-scheme constraint, recreate it with the
    # ADR-0096 seven-scheme set. Widening a value set never conflicts with
    # existing rows, so no data migration is required.
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        _TABLE,
        f"scheme IN ({_SCHEMES_NEW})",
    )


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # Narrowing swap back to the ADR-0090 five-scheme set. Postgres
    # validates the new CHECK against existing rows, so this fails loudly
    # if any 'preqin'/'pitchbook' row exists — acceptable and expected for
    # a value-set-narrowing downgrade (b017 precedent; production never
    # rolls this back).
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        _TABLE,
        f"scheme IN ({_SCHEMES_OLD})",
    )
