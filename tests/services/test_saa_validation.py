# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Pure validation tests for ``services.saa.validation``.

No database, no fixtures — these are micro-tests of the in-process
cross-field validators that ``SAAService.save_inputs_and_correlations``
runs before any repository call.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from services.saa.validation import (
    SAAAssetClassInputSpec,
    SAACorrelationSpec,
    SAAValidationError,
    validate_correlations,
    validate_inputs,
)


# ---------------------------------------------------------------------------
# validate_inputs
# ---------------------------------------------------------------------------


def _input_spec(
    asset_class_id=None,
    *,
    expected_return: float = 0.05,
    volatility: float = 0.10,
    min_weight: float = 0.0,
    max_weight: float = 1.0,
) -> SAAAssetClassInputSpec:
    return SAAAssetClassInputSpec(
        asset_class_id=asset_class_id or uuid4(),
        expected_return=expected_return,
        volatility=volatility,
        min_weight=min_weight,
        max_weight=max_weight,
    )


def test_validate_inputs_accepts_valid_set() -> None:
    inputs = [_input_spec() for _ in range(3)]
    validate_inputs(inputs)  # no exception


def test_validate_inputs_rejects_negative_volatility() -> None:
    with pytest.raises(SAAValidationError) as info:
        validate_inputs([_input_spec(volatility=-0.01)])
    assert info.value.field == "volatility"
    assert info.value.row_index == 0


def test_validate_inputs_rejects_min_greater_than_max() -> None:
    with pytest.raises(SAAValidationError) as info:
        validate_inputs([_input_spec(min_weight=0.7, max_weight=0.3)])
    assert info.value.field == "min_weight"


def test_validate_inputs_rejects_min_below_zero() -> None:
    with pytest.raises(SAAValidationError) as info:
        validate_inputs([_input_spec(min_weight=-0.1)])
    assert info.value.field == "min_weight"


def test_validate_inputs_rejects_max_above_one() -> None:
    with pytest.raises(SAAValidationError) as info:
        validate_inputs([_input_spec(max_weight=1.5)])
    assert info.value.field == "max_weight"


def test_validate_inputs_rejects_duplicate_asset_class() -> None:
    shared = uuid4()
    with pytest.raises(SAAValidationError) as info:
        validate_inputs([_input_spec(asset_class_id=shared), _input_spec(asset_class_id=shared)])
    assert info.value.field == "asset_class_id"
    assert info.value.row_index == 1


# ---------------------------------------------------------------------------
# validate_correlations
# ---------------------------------------------------------------------------


def test_validate_correlations_accepts_valid_triplets() -> None:
    a, b, c = uuid4(), uuid4(), uuid4()
    inputs = [
        _input_spec(asset_class_id=a),
        _input_spec(asset_class_id=b),
        _input_spec(asset_class_id=c),
    ]
    correlations = [
        SAACorrelationSpec(asset_class_a_id=a, asset_class_b_id=b, correlation=0.3),
        SAACorrelationSpec(asset_class_a_id=a, asset_class_b_id=c, correlation=0.4),
        SAACorrelationSpec(asset_class_a_id=b, asset_class_b_id=c, correlation=0.5),
    ]
    validate_correlations(correlations, inputs)  # no exception


def test_validate_correlations_rejects_self_correlation() -> None:
    a = uuid4()
    inputs = [_input_spec(asset_class_id=a), _input_spec()]
    with pytest.raises(SAAValidationError) as info:
        validate_correlations(
            [SAACorrelationSpec(asset_class_a_id=a, asset_class_b_id=a, correlation=1.0)],
            inputs,
        )
    assert info.value.field == "correlation"


def test_validate_correlations_rejects_unknown_asset_class() -> None:
    a, b = uuid4(), uuid4()
    inputs = [_input_spec(asset_class_id=a), _input_spec(asset_class_id=b)]
    rogue = uuid4()
    with pytest.raises(SAAValidationError) as info:
        validate_correlations(
            [SAACorrelationSpec(asset_class_a_id=a, asset_class_b_id=rogue, correlation=0.3)],
            inputs,
        )
    assert info.value.field == "asset_class_b_id"


def test_validate_correlations_rejects_out_of_range() -> None:
    a, b = uuid4(), uuid4()
    inputs = [_input_spec(asset_class_id=a), _input_spec(asset_class_id=b)]
    with pytest.raises(SAAValidationError) as info:
        validate_correlations(
            [SAACorrelationSpec(asset_class_a_id=a, asset_class_b_id=b, correlation=1.5)],
            inputs,
        )
    assert info.value.field == "correlation"


def test_validate_correlations_rejects_duplicate_pair_either_order() -> None:
    a, b = uuid4(), uuid4()
    inputs = [_input_spec(asset_class_id=a), _input_spec(asset_class_id=b)]
    with pytest.raises(SAAValidationError) as info:
        validate_correlations(
            [
                SAACorrelationSpec(asset_class_a_id=a, asset_class_b_id=b, correlation=0.3),
                SAACorrelationSpec(asset_class_a_id=b, asset_class_b_id=a, correlation=0.4),
            ],
            inputs,
        )
    assert info.value.row_index == 1
