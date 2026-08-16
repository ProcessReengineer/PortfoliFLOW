# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Synthetic adapter — a fixture-driven, fully deterministic provider (ADR-0091).

Purpose: controlled **test-event injection**. Beyond unit tests, an operator
can point the live-import tick (ADR-0093) at ``synthetic`` to generate deltas
Irene reacts to, without faking anything downstream — the DTO it emits is
byte-for-byte the same shape a real provider emits, carrying only
``provider = "synthetic"`` as honest provenance. It declares full scheme/kind
coverage in the matrix but is marked ``routing: forced_only``, so it is
excluded from unforced priority routing entirely and reached **only** through
the ``--provider synthetic`` forced path — never selected automatically.

Fixture schema (JSON)::

    {
      "<identifier value>": {
        "<series kind>": [ ["<ISO date>", "<decimal string>"], ... ],
        ...
      },
      ...
    }

The top-level keys are identifier *values* (matched case-insensitively, the
same trim + upper-case normalisation the identifier repository applies). The
second-level keys are :class:`SeriesKind` values. Each point is a
``[iso_date, decimal_string]`` pair; decimal *strings* are recommended so the
value is exact and the fetch is deterministic. A top-level key starting with
``__`` (e.g. ``__doc__``) is a documentation entry — JSON has no comments —
and is skipped by the loader; see ``config/market_data_synthetic_example.json``.

Determinism: the same fixture and window always yield an identical DTO. An
identifier absent from the fixture is unresolvable; a *kind* absent for a
present identifier is an empty series (a real "no data" gap, not an error).
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from services.market_data.dto import (
    DateWindow,
    NormalizedIdentifier,
    NormalizedSeries,
    SeriesKind,
    SeriesPoint,
)
from services.market_data.provider import (
    IdentifierNotResolvableError,
    MarketDataConfigurationError,
)

_PROVIDER_NAME = "synthetic"
_DEFAULT_CURRENCY = "EUR"


class SyntheticAdapter:
    """Serve series from a JSON fixture, deterministically."""

    def __init__(self, fixture_path: Path, *, currency: str = _DEFAULT_CURRENCY) -> None:
        """Load and validate the fixture.

        Args:
            fixture_path: Path to the JSON fixture.
            currency: The currency to stamp on every emitted series. A single
                currency for all series keeps the test-event provider simple
                and deterministic; default ``"EUR"`` (the platform base).

        Raises:
            MarketDataConfigurationError: If the file is missing, not JSON, or
                structurally invalid (non-mapping, or an unknown kind key).
        """
        self._fixture_path = Path(fixture_path)
        self._currency = currency
        self._data = self._load(self._fixture_path)

    async def fetch_series(
        self,
        ident: NormalizedIdentifier,
        kind: SeriesKind,
        window: DateWindow,
    ) -> NormalizedSeries:
        """Return the fixture's ``kind`` series for ``ident`` within ``window``.

        See :meth:`MarketDataProvider.fetch_series`.

        Raises:
            IdentifierNotResolvableError: If ``ident.value`` is not in the
                fixture.
            MarketDataConfigurationError: If a fixture point is malformed.
        """
        series_for_id = self._data.get(ident.value)
        if series_for_id is None:
            raise IdentifierNotResolvableError(
                f"Synthetic fixture has no identifier {ident.value!r}."
            )

        raw_points = series_for_id.get(kind.value)
        if raw_points is None:
            # Identifier present, kind absent → a real gap, not an error.
            return NormalizedSeries(
                ident=ident,
                provider=_PROVIDER_NAME,
                kind=kind,
                currency=self._currency,
                points=(),
            )

        points = [
            point
            for point in (self._parse_point(row, kind) for row in raw_points)
            if window.contains(point.as_of_date)
        ]
        points.sort(key=lambda point: point.as_of_date)
        return NormalizedSeries(
            ident=ident,
            provider=_PROVIDER_NAME,
            kind=kind,
            currency=self._currency,
            points=tuple(points),
        )

    def _parse_point(self, row: Any, kind: SeriesKind) -> SeriesPoint:
        """Parse one ``[iso_date, decimal_string]`` fixture row.

        Raises:
            MarketDataConfigurationError: If the row is not a 2-element pair,
                the date is not ISO, or the value is not a valid decimal.
        """
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise MarketDataConfigurationError(
                f"Synthetic fixture {self._fixture_path} has a malformed "
                f"{kind.value!r} point (expected [date, value]): {row!r}."
            )
        try:
            day = date.fromisoformat(str(row[0]))
        except ValueError as exc:
            raise MarketDataConfigurationError(
                f"Synthetic fixture {self._fixture_path} has a non-ISO date "
                f"in a {kind.value!r} point: {row[0]!r}."
            ) from exc
        try:
            value = Decimal(str(row[1]))
        except InvalidOperation as exc:
            raise MarketDataConfigurationError(
                f"Synthetic fixture {self._fixture_path} has a non-decimal "
                f"value in a {kind.value!r} point: {row[1]!r}."
            ) from exc
        return SeriesPoint(as_of_date=day, value=value)

    @staticmethod
    def _load(path: Path) -> dict[str, dict[str, Any]]:
        """Load, validate, and key-normalise the fixture.

        Raises:
            MarketDataConfigurationError: If the file is missing, not JSON, not
                a mapping-of-mappings, or contains an unknown kind key.
        """
        if not path.exists():
            raise MarketDataConfigurationError(f"Synthetic fixture not found at {path}.")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise MarketDataConfigurationError(
                f"Synthetic fixture at {path} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise MarketDataConfigurationError(
                f"Synthetic fixture at {path} must be a JSON object."
            )

        normalised: dict[str, dict[str, Any]] = {}
        for ident_value, kinds in raw.items():
            # Keys starting with "__" are documentation (e.g. "__doc__" in the
            # operator sample) — JSON has no comments, so this is the fixture's
            # comment convention. They are skipped, never treated as an
            # identifier (no real ISIN/ticker starts with "__").
            if str(ident_value).startswith("__"):
                continue
            if not isinstance(kinds, dict):
                raise MarketDataConfigurationError(
                    f"Synthetic fixture at {path} maps {ident_value!r} to a "
                    "non-object; expected a kind→points mapping."
                )
            for kind_key in kinds:
                try:
                    SeriesKind(kind_key)
                except ValueError as exc:
                    raise MarketDataConfigurationError(
                        f"Synthetic fixture at {path} declares unknown kind "
                        f"{kind_key!r} under {ident_value!r}."
                    ) from exc
            normalised[str(ident_value).strip().upper()] = kinds
        return normalised
