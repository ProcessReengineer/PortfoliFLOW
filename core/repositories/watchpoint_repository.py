# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""WatchpointRepository — the Watch Desk's historised subject registry.

Backs the ``watchpoints`` table introduced in migration b033 (per
ADR-0116 §1). The table stores **immutable version rows**: a stable
``watchpoint_id`` identity plus one row per revision, keyed by
``effective_from``. Nothing is ever updated in place —
:meth:`WatchpointRepository.revise` inserts a new version and
:meth:`WatchpointRepository.retire` inserts one with ``retired = True``,
so the identity and its whole history stay queryable and a finding fired
last month remains explainable against the thresholds that were in force
when it fired.

:meth:`WatchpointRepository.effective_watchpoints` is *the* read — the
one the beat and the web surface share (ADR-0116 §1), so "what was
effective when this finding fired" is the same query in both places.

Division of labour with the schema
----------------------------------
The **schema** owns the asymmetry: per-family CHECK constraints decide
which columns may be non-NULL, which is what makes it impossible for a
bug here or in a route to turn an ``saa`` overlay into a second edit
point for limits. The **repository** owns the values the CHECKs
deliberately do not express (ADR-0116 §3):

* ``50 < warn_threshold_pct < 100``;
* positive deltas, windows, drops, moves, ages, horizons and ratios;
* a well-formed ``BASE/QUOTE`` currency pair with two distinct codes;
* the **singleton rule**: at most one ``freshness`` and one
  ``liquidity`` identity per tenant, because their parameters apply to
  every investment / to the book as a whole, so a second identity would
  be two answers to one question. Further *versions* of the one identity
  are of course the normal case.

Both layers run: the repository raises a typed
:class:`core.exceptions.WatchpointInvalid` naming the field before any
SQL is issued, and the CHECK underneath stays the last line of defence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select, text

from core.exceptions import WatchpointInvalid, WatchpointNotFound
from core.models.watchpoint import (
    SINGLETON_FAMILIES,
    WATCHPOINT_FAMILIES,
    Watchpoint,
)
from core.repositories.base import BaseRepository

#: ``BASE/QUOTE`` with two ISO-4217-shaped alphabetic codes. Format only —
#: whether the pair has rates is the FX service's question, not this one's.
_CURRENCY_PAIR_PATTERN = re.compile(r"^[A-Z]{3}/[A-Z]{3}$")

#: Exclusive bounds on a per-subject WARN override (ADR-0116 §3). Below 50
#: the "warning" fires at less than half the ceiling, which is noise; at or
#: above 100 it fires only once the ceiling is already breached, which is
#: not a warning at all.
_WARN_MIN_EXCLUSIVE: Decimal = Decimal("50")
_WARN_MAX_EXCLUSIVE: Decimal = Decimal("100")

#: Required and forbidden defining columns per family — the repository-side
#: mirror of the b033 CHECKs, used to fail with a named field instead of an
#: opaque IntegrityError. The empty tuples for the overlay families are the
#: point, not an omission: they define nothing.
_REQUIRED_BY_FAMILY: dict[str, tuple[str, ...]] = {
    "saa": (),
    "anlv": (),
    "rss": (),
    "price": ("instrument_id", "drop_pct", "window_days"),
    "fx": ("currency_pair", "move_pct", "window_days"),
    "freshness": ("max_age_days",),
    "liquidity": ("horizon_months", "min_coverage_ratio"),
}

_DEFINING_COLUMNS: tuple[str, ...] = (
    "instrument_id",
    "currency_pair",
    "drop_pct",
    "move_pct",
    "window_days",
    "max_age_days",
    "horizon_months",
    "min_coverage_ratio",
)


@dataclass(frozen=True)
class WatchpointDTO:
    """Plain data-only view of one ``watchpoints`` version row.

    Attributes:
        id: Surrogate primary key of *this version row*.
        watchpoint_id: The stable identity shared by every version.
        tenant_id: Owning tenant.
        effective_from: The instant this version took effect.
        retired: ``True`` when this version retires the identity.
        family: One of :data:`core.models.watchpoint.WATCHPOINT_FAMILIES`.
        subject_key: The subject this watchpoint overlays (derived
            families) or defines (signal families).
        display_name: Operator-readable label.
        muted: Suppresses *finding creation* only — watch-state upserts and
            the monitor row continue (ADR-0116 §3).
        warn_threshold_pct: Per-subject WARN override, or ``None`` for the
            tenant default.
        re_trigger_delta: Per-subject magnitude re-trigger override.
        instrument_id: The watched instrument (``price`` only).
        currency_pair: ``BASE/QUOTE`` (``fx`` only).
        drop_pct: Adverse-move trigger in pp (``price`` only).
        move_pct: Absolute-move trigger in pp (``fx`` only).
        window_days: Observation window, shared by ``price`` and ``fx``.
        max_age_days: NAV age limit (``freshness`` only).
        horizon_months: Coverage horizon (``liquidity`` only).
        min_coverage_ratio: Coverage-ratio floor (``liquidity`` only).
        notes: Optional free-text annotation.
        created_at: Row insertion timestamp.
        updated_at: Row update timestamp (rows are never updated; present
            for house-column parity).
    """

    id: UUID
    watchpoint_id: UUID
    tenant_id: UUID
    effective_from: datetime
    retired: bool
    family: str
    subject_key: str
    display_name: str
    muted: bool
    warn_threshold_pct: Decimal | None
    re_trigger_delta: Decimal | None
    instrument_id: UUID | None
    currency_pair: str | None
    drop_pct: Decimal | None
    move_pct: Decimal | None
    window_days: int | None
    max_age_days: int | None
    horizon_months: int | None
    min_coverage_ratio: Decimal | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


def _to_dto(model: Watchpoint) -> WatchpointDTO:
    return WatchpointDTO(
        id=model.id,
        watchpoint_id=model.watchpoint_id,
        tenant_id=model.tenant_id,
        effective_from=model.effective_from,
        retired=model.retired,
        family=model.family,
        subject_key=model.subject_key,
        display_name=model.display_name,
        muted=model.muted,
        warn_threshold_pct=model.warn_threshold_pct,
        re_trigger_delta=model.re_trigger_delta,
        instrument_id=model.instrument_id,
        currency_pair=model.currency_pair,
        drop_pct=model.drop_pct,
        move_pct=model.move_pct,
        window_days=model.window_days,
        max_age_days=model.max_age_days,
        horizon_months=model.horizon_months,
        min_coverage_ratio=model.min_coverage_ratio,
        notes=model.notes,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _family_sort_key(dto: WatchpointDTO) -> tuple[int, str, str]:
    """Order rows by family (declaration order), then subject, then name."""
    try:
        family_rank = WATCHPOINT_FAMILIES.index(dto.family)
    except ValueError:  # pragma: no cover - a CHECK-guarded column
        family_rank = len(WATCHPOINT_FAMILIES)
    return (family_rank, dto.subject_key, dto.display_name)


class WatchpointRepository(BaseRepository):
    """Read and write watchpoints in the active tenant context."""

    # -- reads ---------------------------------------------------------------

    async def effective_watchpoints(
        self,
        as_of: datetime,
        *,
        family: str | None = None,
        include_retired: bool = False,
    ) -> list[WatchpointDTO]:
        """Return the version of every watchpoint in force at ``as_of``.

        The single shared read (ADR-0116 §1): for each identity, the row
        with the largest ``effective_from <= as_of``. Retired identities
        are excluded by default — their history stays readable through
        :meth:`list_versions`, which is what keeps a past finding
        explainable without keeping a dead subject on the monitor.

        This is the read for "what applies right now", and therefore the
        one the beat and the monitor want. For "which subjects does this
        tenant watch at all", including a version dated in the future, use
        :meth:`list_live_identities`.

        Args:
            as_of: The evaluation instant (timezone-aware).
            family: Optional family filter.
            include_retired: When ``True``, identities whose current
                version is a retirement are returned too.

        Returns:
            The effective versions, ordered by family, subject key and
            display name — a stable order the monitor and the beat share.

        Raises:
            WatchpointInvalid: If ``as_of`` is naive or ``family`` is not a
                known family.
        """
        self._require_aware(as_of, field="as_of")
        if family is not None and family not in WATCHPOINT_FAMILIES:
            raise WatchpointInvalid(
                f"Unknown watchpoint family {family!r}; expected one of "
                f"{list(WATCHPOINT_FAMILIES)}.",
                field="family",
            )

        stmt = (
            select(Watchpoint)
            .where(Watchpoint.effective_from <= as_of)
            .distinct(Watchpoint.watchpoint_id)
            .order_by(Watchpoint.watchpoint_id, Watchpoint.effective_from.desc())
        )
        if family is not None:
            stmt = stmt.where(Watchpoint.family == family)

        result = await self._session.execute(stmt)
        rows = [_to_dto(model) for model in result.scalars().all()]
        if not include_retired:
            rows = [row for row in rows if not row.retired]
        return sorted(rows, key=_family_sort_key)

    async def get_current(
        self, watchpoint_id: UUID, *, as_of: datetime | None = None
    ) -> WatchpointDTO | None:
        """Return one identity's version in force at ``as_of``, or ``None``.

        Includes retired versions: a caller asking about a specific
        identity is entitled to see that it was retired. ``as_of`` defaults
        to "the latest version there is", which is what an editor wants.

        Args:
            watchpoint_id: The stable identity.
            as_of: Optional cut-off instant (timezone-aware).

        Returns:
            The applicable :class:`WatchpointDTO`, or ``None`` when the
            identity has no version at or before ``as_of`` — including the
            case of an identity that does not exist or belongs to another
            tenant (RLS hides it, and absence is reported, never raised).

        Raises:
            WatchpointInvalid: If ``as_of`` is naive.
        """
        stmt = select(Watchpoint).where(Watchpoint.watchpoint_id == watchpoint_id)
        if as_of is not None:
            self._require_aware(as_of, field="as_of")
            stmt = stmt.where(Watchpoint.effective_from <= as_of)
        stmt = stmt.order_by(Watchpoint.effective_from.desc()).limit(1)
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    async def list_live_identities(self, *, family: str | None = None) -> list[WatchpointDTO]:
        """Return the latest non-retired version of every identity, any instant.

        Deliberately **unbounded in time**, unlike
        :meth:`effective_watchpoints`: this answers "which subjects does
        this tenant watch at all", which is the question the singleton rule
        and the default-watchpoint seeder both ask. A version dated in the
        future is still a live identity — treating it as absent would let
        the seeder create a duplicate the singleton rule then refuses.

        Args:
            family: Optional family filter.

        Returns:
            One row per live identity, in the same family / subject order
            as :meth:`effective_watchpoints`.

        Raises:
            WatchpointInvalid: If ``family`` is not a known family.
        """
        if family is not None and family not in WATCHPOINT_FAMILIES:
            raise WatchpointInvalid(
                f"Unknown watchpoint family {family!r}; expected one of "
                f"{list(WATCHPOINT_FAMILIES)}.",
                field="family",
            )
        stmt = (
            select(Watchpoint)
            .distinct(Watchpoint.watchpoint_id)
            .order_by(Watchpoint.watchpoint_id, Watchpoint.effective_from.desc())
        )
        if family is not None:
            stmt = stmt.where(Watchpoint.family == family)
        result = await self._session.execute(stmt)
        rows = [_to_dto(model) for model in result.scalars().all() if not model.retired]
        return sorted(rows, key=_family_sort_key)

    async def list_versions(self, watchpoint_id: UUID) -> list[WatchpointDTO]:
        """Return every version of one identity, oldest first.

        The history view behind a monitor row's "versions" affordance
        (ADR-0116 §6).

        Args:
            watchpoint_id: The stable identity.

        Returns:
            All version rows, ordered by ``effective_from`` ascending.
            Empty for an unknown or cross-tenant identity.
        """
        result = await self._session.execute(
            select(Watchpoint)
            .where(Watchpoint.watchpoint_id == watchpoint_id)
            .order_by(Watchpoint.effective_from)
        )
        return [_to_dto(model) for model in result.scalars().all()]

    # -- writes --------------------------------------------------------------

    async def create(
        self,
        *,
        family: str,
        subject_key: str,
        display_name: str,
        effective_from: datetime,
        muted: bool = False,
        warn_threshold_pct: Decimal | None = None,
        re_trigger_delta: Decimal | None = None,
        instrument_id: UUID | None = None,
        currency_pair: str | None = None,
        drop_pct: Decimal | None = None,
        move_pct: Decimal | None = None,
        window_days: int | None = None,
        max_age_days: int | None = None,
        horizon_months: int | None = None,
        min_coverage_ratio: Decimal | None = None,
        notes: str | None = None,
    ) -> WatchpointDTO:
        """Create a new watchpoint identity with its first version.

        Args:
            family: One of :data:`core.models.watchpoint.WATCHPOINT_FAMILIES`.
            subject_key: The subject overlaid (derived families) or defined
                (signal families).
            display_name: Operator-readable label.
            effective_from: When this first version takes effect
                (timezone-aware).
            muted: Suppress finding creation for the subject.
            warn_threshold_pct: Per-subject WARN override in ``(50, 100)``.
            re_trigger_delta: Per-subject magnitude re-trigger override.
            instrument_id: ``price`` only.
            currency_pair: ``fx`` only, ``BASE/QUOTE``.
            drop_pct: ``price`` only.
            move_pct: ``fx`` only.
            window_days: ``price`` and ``fx``.
            max_age_days: ``freshness`` only.
            horizon_months: ``liquidity`` only.
            min_coverage_ratio: ``liquidity`` only.
            notes: Optional annotation.

        Returns:
            The created :class:`WatchpointDTO` — its ``watchpoint_id`` is
            the identity to pass to :meth:`revise` and :meth:`retire`.

        Raises:
            WatchpointInvalid: On any value, shape or singleton violation.
        """
        parameters = {
            "muted": muted,
            "warn_threshold_pct": warn_threshold_pct,
            "re_trigger_delta": re_trigger_delta,
            "instrument_id": instrument_id,
            "currency_pair": currency_pair,
            "drop_pct": drop_pct,
            "move_pct": move_pct,
            "window_days": window_days,
            "max_age_days": max_age_days,
            "horizon_months": horizon_months,
            "min_coverage_ratio": min_coverage_ratio,
        }
        self._validate(
            family=family,
            subject_key=subject_key,
            display_name=display_name,
            effective_from=effective_from,
            parameters=parameters,
        )
        if family in SINGLETON_FAMILIES:
            await self._require_no_live_identity(family)

        return await self._insert_version(
            watchpoint_id=uuid4(),
            family=family,
            subject_key=subject_key,
            display_name=display_name,
            effective_from=effective_from,
            retired=False,
            parameters=parameters,
            notes=notes,
        )

    async def revise(
        self,
        watchpoint_id: UUID,
        *,
        effective_from: datetime,
        display_name: str,
        muted: bool = False,
        warn_threshold_pct: Decimal | None = None,
        re_trigger_delta: Decimal | None = None,
        drop_pct: Decimal | None = None,
        move_pct: Decimal | None = None,
        window_days: int | None = None,
        max_age_days: int | None = None,
        horizon_months: int | None = None,
        min_coverage_ratio: Decimal | None = None,
        notes: str | None = None,
    ) -> WatchpointDTO:
        """Write a new version of an existing identity.

        A revision states the **complete** calibration of the new version;
        nothing is carried forward silently, because an immutable version
        row is meant to be readable on its own without replaying its
        predecessors. Read the current version with :meth:`get_current`
        first — that is what an editor form is for.

        The identity-defining fields — ``family``, ``subject_key``,
        ``instrument_id`` and ``currency_pair`` — are inherited from the
        current version and cannot be revised: changing what a watchpoint
        watches makes it a different watchpoint, which is a
        :meth:`create` plus a :meth:`retire`, not an edit.

        ``effective_from`` must lie strictly after the current version's.
        Back-dating would rewrite what was in force in the past, and the
        whole point of the versioning is that it cannot be.

        Args:
            watchpoint_id: The stable identity to revise.
            effective_from: When the new version takes effect.
            display_name: Operator-readable label for the new version.
            muted: Suppress finding creation for the subject.
            warn_threshold_pct: Per-subject WARN override in ``(50, 100)``.
            re_trigger_delta: Per-subject magnitude re-trigger override.
            drop_pct: ``price`` only.
            move_pct: ``fx`` only.
            window_days: ``price`` and ``fx``.
            max_age_days: ``freshness`` only.
            horizon_months: ``liquidity`` only.
            min_coverage_ratio: ``liquidity`` only.
            notes: Optional annotation.

        Returns:
            The newly written :class:`WatchpointDTO`.

        Raises:
            WatchpointNotFound: If the identity has no current version.
            WatchpointInvalid: On any value or shape violation, or if
                ``effective_from`` does not advance.
        """
        current = await self._require_current(watchpoint_id)
        parameters = {
            "muted": muted,
            "warn_threshold_pct": warn_threshold_pct,
            "re_trigger_delta": re_trigger_delta,
            # Inherited: the subject's identity, not its calibration.
            "instrument_id": current.instrument_id,
            "currency_pair": current.currency_pair,
            "drop_pct": drop_pct,
            "move_pct": move_pct,
            "window_days": window_days,
            "max_age_days": max_age_days,
            "horizon_months": horizon_months,
            "min_coverage_ratio": min_coverage_ratio,
        }
        self._validate(
            family=current.family,
            subject_key=current.subject_key,
            display_name=display_name,
            effective_from=effective_from,
            parameters=parameters,
        )
        self._require_advancing(effective_from, current)

        return await self._insert_version(
            watchpoint_id=watchpoint_id,
            family=current.family,
            subject_key=current.subject_key,
            display_name=display_name,
            effective_from=effective_from,
            retired=False,
            parameters=parameters,
            notes=notes,
        )

    async def retire(
        self,
        watchpoint_id: UUID,
        *,
        effective_from: datetime,
        notes: str | None = None,
    ) -> WatchpointDTO:
        """Retire an identity by writing a ``retired = True`` version.

        The retiring version copies the current version's calibration
        verbatim — partly because the per-family CHECKs demand a
        well-formed row whatever its ``retired`` flag says, and partly
        because "what was it set to when it was retired" is a question the
        history should answer without a join.

        Args:
            watchpoint_id: The stable identity to retire.
            effective_from: When the retirement takes effect.
            notes: Optional annotation, e.g. the reason.

        Returns:
            The retiring :class:`WatchpointDTO`.

        Raises:
            WatchpointNotFound: If the identity has no current version.
            WatchpointInvalid: If ``effective_from`` does not advance, or
                the identity is already retired.
        """
        current = await self._require_current(watchpoint_id)
        if current.retired:
            raise WatchpointInvalid(
                f"Watchpoint {watchpoint_id} was already retired at "
                f"{current.effective_from.isoformat()}.",
                field="watchpoint_id",
            )
        self._require_aware(effective_from, field="effective_from")
        self._require_advancing(effective_from, current)

        return await self._insert_version(
            watchpoint_id=watchpoint_id,
            family=current.family,
            subject_key=current.subject_key,
            display_name=current.display_name,
            effective_from=effective_from,
            retired=True,
            parameters={
                "muted": current.muted,
                "warn_threshold_pct": current.warn_threshold_pct,
                "re_trigger_delta": current.re_trigger_delta,
                "instrument_id": current.instrument_id,
                "currency_pair": current.currency_pair,
                "drop_pct": current.drop_pct,
                "move_pct": current.move_pct,
                "window_days": current.window_days,
                "max_age_days": current.max_age_days,
                "horizon_months": current.horizon_months,
                "min_coverage_ratio": current.min_coverage_ratio,
            },
            notes=notes if notes is not None else current.notes,
        )

    # -- internals -----------------------------------------------------------

    async def _insert_version(
        self,
        *,
        watchpoint_id: UUID,
        family: str,
        subject_key: str,
        display_name: str,
        effective_from: datetime,
        retired: bool,
        parameters: dict[str, object],
        notes: str | None,
    ) -> WatchpointDTO:
        """Insert one version row in the active tenant context."""
        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        model = Watchpoint(
            watchpoint_id=watchpoint_id,
            tenant_id=active_tenant,
            effective_from=effective_from,
            retired=retired,
            family=family,
            subject_key=subject_key,
            display_name=display_name,
            notes=notes,
            **parameters,
        )
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return _to_dto(model)

    async def _require_current(self, watchpoint_id: UUID) -> WatchpointDTO:
        current = await self.get_current(watchpoint_id)
        if current is None:
            raise WatchpointNotFound(
                f"No watchpoint with identity {watchpoint_id} in this tenant; "
                "a revision writes a new version of an existing identity."
            )
        return current

    async def _require_no_live_identity(self, family: str) -> None:
        """Enforce the singleton rule for ``freshness`` / ``liquidity``.

        Unbounded in time on purpose (see
        :meth:`list_live_identities`): a second identity whose first
        version is dated in the future is still a second identity, and the
        rule is about how many there are, not about when they start.
        """
        existing = await self.list_live_identities(family=family)
        if existing:
            raise WatchpointInvalid(
                f"The {family!r} family is a singleton: this tenant already has "
                f"watchpoint {existing[0].watchpoint_id} ({existing[0].subject_key}). "
                "Revise that one rather than creating a second — its parameters "
                "already apply to every subject the family covers.",
                field="family",
            )

    def _require_advancing(self, effective_from: datetime, current: WatchpointDTO) -> None:
        if effective_from <= current.effective_from:
            raise WatchpointInvalid(
                f"effective_from {effective_from.isoformat()} does not advance "
                f"past the current version's {current.effective_from.isoformat()}. "
                "Versions are immutable: a new one takes effect later, it never "
                "rewrites what was in force.",
                field="effective_from",
            )

    @staticmethod
    def _require_aware(value: datetime, *, field: str) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise WatchpointInvalid(
                f"{field} must be timezone-aware; the column is TIMESTAMPTZ and "
                "a naive instant has no defined position on it.",
                field=field,
            )

    def _validate(
        self,
        *,
        family: str,
        subject_key: str,
        display_name: str,
        effective_from: datetime,
        parameters: dict[str, object],
    ) -> None:
        """Run every repository-owned rule before any SQL is issued."""
        if family not in WATCHPOINT_FAMILIES:
            raise WatchpointInvalid(
                f"Unknown watchpoint family {family!r}; expected one of "
                f"{list(WATCHPOINT_FAMILIES)}.",
                field="family",
            )
        if not subject_key.strip():
            raise WatchpointInvalid("subject_key must not be blank.", field="subject_key")
        if not display_name.strip():
            raise WatchpointInvalid("display_name must not be blank.", field="display_name")
        self._require_aware(effective_from, field="effective_from")

        self._validate_shape(family, parameters)
        self._validate_values(family, parameters)

    @staticmethod
    def _validate_shape(family: str, parameters: dict[str, object]) -> None:
        """Mirror the b033 per-family CHECKs with a named-field error."""
        required = _REQUIRED_BY_FAMILY[family]
        for column in required:
            if parameters.get(column) is None:
                raise WatchpointInvalid(
                    f"The {family!r} family requires {column}.",
                    field=column,
                )
        for column in _DEFINING_COLUMNS:
            if column in required:
                continue
            if parameters.get(column) is not None:
                raise WatchpointInvalid(
                    f"The {family!r} family must not set {column} — it is a "
                    "parameter of another family. For the overlay families "
                    "(saa / anlv / rss) the subject and its ceiling belong to "
                    "the limit set alone.",
                    field=column,
                )
        if family == "rss":
            for column in ("warn_threshold_pct", "re_trigger_delta"):
                if parameters.get(column) is not None:
                    raise WatchpointInvalid(
                        "An rss overlay carries mute only: a cluster subject is "
                        f"non-scalar, so {column} has nothing to measure against.",
                        field=column,
                    )

    @staticmethod
    def _validate_values(family: str, parameters: dict[str, object]) -> None:
        """Bounds, positivity and currency-pair format (ADR-0116 §3)."""
        warn = parameters.get("warn_threshold_pct")
        if warn is not None:
            warn_value = Decimal(str(warn))
            if not (_WARN_MIN_EXCLUSIVE < warn_value < _WARN_MAX_EXCLUSIVE):
                raise WatchpointInvalid(
                    f"warn_threshold_pct must lie strictly between "
                    f"{_WARN_MIN_EXCLUSIVE} and {_WARN_MAX_EXCLUSIVE}; got "
                    f"{warn_value}.",
                    field="warn_threshold_pct",
                )

        for column in (
            "re_trigger_delta",
            "drop_pct",
            "move_pct",
            "window_days",
            "max_age_days",
            "horizon_months",
            "min_coverage_ratio",
        ):
            value = parameters.get(column)
            if value is None:
                continue
            if Decimal(str(value)) <= 0:
                raise WatchpointInvalid(
                    f"{column} must be positive; got {value}. A zero or "
                    "negative threshold either never fires or always does.",
                    field=column,
                )

        pair = parameters.get("currency_pair")
        if pair is not None:
            pair_text = str(pair)
            if not _CURRENCY_PAIR_PATTERN.match(pair_text):
                raise WatchpointInvalid(
                    f"currency_pair must be 'BASE/QUOTE' with two upper-case "
                    f"three-letter codes; got {pair_text!r}.",
                    field="currency_pair",
                )
            base, quote = pair_text.split("/")
            if base == quote:
                raise WatchpointInvalid(
                    f"currency_pair {pair_text!r} names one currency twice; a "
                    "pair against itself never moves.",
                    field="currency_pair",
                )
