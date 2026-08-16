# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Cross-field validation helpers for SAA workflows.

These helpers perform per-row and pair-level checks that the database
schema cannot express directly: duplicate asset classes within an
input list, references from correlations to asset classes that are
not part of the configuration, weight ordering invariants. The
checks run before the repository layer so a violation surfaces as a
typed :class:`SAAValidationError` rather than an opaque Postgres
``IntegrityError``.

Range invariants (volatility ≥ 0, 0 ≤ min/max_weight ≤ 1, |ρ| ≤ 1)
are duplicated here for early failure even though the b005 CHECKs
would catch them at flush time. Two layers cost a few microseconds
and turn "constraint violation in saa_asset_class_inputs" into a
field-targeted error message the SAA UI can attach to the offending
row.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from core.exceptions import ValidationError


class SAAValidationError(ValidationError):
    """Cross-field validation failure for SAA inputs.

    Subclasses :class:`core.exceptions.ValidationError` so the web
    layer's existing ``ValidationError`` handler catches it. The
    extra ``row_index`` attribute lets the UI highlight the offending
    table row when one is available.
    """

    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        row_index: int | None = None,
    ) -> None:
        super().__init__(message=message, field=field)
        self.row_index = row_index


@dataclass(frozen=True)
class SAAAssetClassInputSpec:
    """In-memory spec for one per-asset-class input row.

    Used as the input shape for service-level workflows that have not
    yet persisted (or are about to replace) the corresponding rows.
    """

    asset_class_id: UUID
    expected_return: float
    volatility: float
    min_weight: float
    max_weight: float


@dataclass(frozen=True)
class SAACorrelationSpec:
    """In-memory spec for one correlation triplet.

    Order between ``asset_class_a_id`` and ``asset_class_b_id`` is
    not significant — the repository normalises before persisting.
    """

    asset_class_a_id: UUID
    asset_class_b_id: UUID
    correlation: float


def validate_inputs(inputs: list[SAAAssetClassInputSpec]) -> None:
    """Validate per-row constraints across a list of input specs.

    Checks:

    - ``volatility >= 0`` for every row.
    - ``0 <= min_weight <= 1`` and ``0 <= max_weight <= 1`` for every row.
    - ``min_weight <= max_weight`` for every row.
    - Asset-class ids are unique across the list (no duplicates).

    Args:
        inputs: The candidate input specs.

    Raises:
        SAAValidationError: On any violation. The error carries the
            offending field name and the zero-based ``row_index``.
    """
    seen_asset_class_ids: set[UUID] = set()
    for idx, spec in enumerate(inputs):
        if spec.volatility < 0:
            raise SAAValidationError(
                f"Volatility must be >= 0 (row {idx + 1}, got {spec.volatility}).",
                field="volatility",
                row_index=idx,
            )
        if not 0.0 <= spec.min_weight <= 1.0:
            raise SAAValidationError(
                f"min_weight must be in [0, 1] (row {idx + 1}, got {spec.min_weight}).",
                field="min_weight",
                row_index=idx,
            )
        if not 0.0 <= spec.max_weight <= 1.0:
            raise SAAValidationError(
                f"max_weight must be in [0, 1] (row {idx + 1}, got {spec.max_weight}).",
                field="max_weight",
                row_index=idx,
            )
        if spec.min_weight > spec.max_weight:
            raise SAAValidationError(
                f"min_weight ({spec.min_weight}) must be <= max_weight "
                f"({spec.max_weight}) (row {idx + 1}).",
                field="min_weight",
                row_index=idx,
            )
        if spec.asset_class_id in seen_asset_class_ids:
            raise SAAValidationError(
                f"Duplicate asset class on row {idx + 1} ({spec.asset_class_id}).",
                field="asset_class_id",
                row_index=idx,
            )
        seen_asset_class_ids.add(spec.asset_class_id)


def validate_correlations(
    correlations: list[SAACorrelationSpec],
    inputs: list[SAAAssetClassInputSpec],
) -> None:
    """Validate correlations against the configuration's input set.

    Checks:

    - Every correlation references asset classes present in ``inputs``.
    - Every correlation value is in ``[-1, 1]``.
    - No self-correlations (a == b).
    - No duplicate ``(a, b)`` pairs (after normalising to upper-triangle
      order). Duplicates would conflict with the b005 unique
      constraint.

    Phase 3 deliberately does *not* check positive semi-definiteness
    of the implied correlation matrix. The optimiser nudges a near-
    PSD covariance matrix and rejects clearly-indefinite ones with a
    descriptive error; that is the right place for the check until
    the SAA UI can surface a per-pair complaint.

    Args:
        correlations: The candidate correlation specs.
        inputs: The corresponding inputs (used to validate references).

    Raises:
        SAAValidationError: On any violation.
    """
    valid_asset_class_ids = {spec.asset_class_id for spec in inputs}
    seen_pairs: set[tuple[UUID, UUID]] = set()

    for idx, corr in enumerate(correlations):
        if corr.asset_class_a_id == corr.asset_class_b_id:
            raise SAAValidationError(
                f"Correlation row {idx + 1}: self-correlation is "
                f"implicit (1.0) and must not be stored.",
                field="correlation",
                row_index=idx,
            )
        if corr.asset_class_a_id not in valid_asset_class_ids:
            raise SAAValidationError(
                f"Correlation row {idx + 1}: asset class "
                f"{corr.asset_class_a_id} is not part of this "
                f"configuration's inputs.",
                field="asset_class_a_id",
                row_index=idx,
            )
        if corr.asset_class_b_id not in valid_asset_class_ids:
            raise SAAValidationError(
                f"Correlation row {idx + 1}: asset class "
                f"{corr.asset_class_b_id} is not part of this "
                f"configuration's inputs.",
                field="asset_class_b_id",
                row_index=idx,
            )
        if not -1.0 <= corr.correlation <= 1.0:
            raise SAAValidationError(
                f"Correlation row {idx + 1}: correlation must be in "
                f"[-1, 1] (got {corr.correlation}).",
                field="correlation",
                row_index=idx,
            )

        a, b = corr.asset_class_a_id, corr.asset_class_b_id
        ordered = (a, b) if a < b else (b, a)
        if ordered in seen_pairs:
            raise SAAValidationError(
                f"Correlation row {idx + 1}: duplicate pair ({a}, {b}).",
                field="asset_class_a_id",
                row_index=idx,
            )
        seen_pairs.add(ordered)
