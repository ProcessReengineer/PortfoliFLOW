# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the pure Takahashi–Alexander profile generator (ADR-0105 §1/§2).

Golden worked examples (one per capital-account type), determinism, the
remaining-profile semantics of a mid-life fund, the investor-flow exemption,
the un-modellable fallback, the loud rejection of a non-capital-account type,
and the periodisation mapping.

The golden lists are the **definition of done** (ADR-0105 §1): each is a fixed
input and the exact expected flow list, pinned as ``(iso_date, flow_type,
amount)`` literals and reconstructed into
:class:`~services.overlay.pipeline.PlanFlow` values for a Decimal-exact
comparison. They were produced by the generator itself and hand-checked at
their anchors (the first call is ``RC_1 × remaining-unfunded``; the year-``t``
distribution is ``(t / L) ** B`` of the grown prior NAV; the final model year
liquidates the residual), so a change to the model breaks them loudly.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest

from services.investments.cash_flow_timeline import Periodisation
from services.investments.flow_type_invariants import OVERLAY_EXEMPT_FLOW_TYPES
from services.investments.ta_profile import (
    GeneratedPlanFlow,
    generate_remaining_profile,
)
from services.investments.ta_profile_constants import (
    TAProfileUnsupportedTypeError,
    parameters_for,
)
from services.overlay import PlanFlow

_INV: UUID = UUID("11111111-1111-1111-1111-111111111111")

# ---------------------------------------------------------------------------
# Golden worked examples — one per capital-account type (ADR-0105 §1)
# ---------------------------------------------------------------------------

# (iso_date, flow_type, amount) rows, Decimal-exact.
_Row = tuple[str, str, str]

_PE_QUARTERLY_GOLDEN: list[_Row] = [
    ("2026-09-30", "capital_call", "-200000.0000"),
    ("2027-06-30", "distribution", "566.3245"),
    ("2027-09-30", "capital_call", "-180000.0000"),
    ("2028-06-30", "distribution", "6175.7199"),
    ("2028-09-30", "capital_call", "-126000.0000"),
    ("2029-06-30", "distribution", "25368.8296"),
    ("2029-09-30", "capital_call", "-73500.0000"),
    ("2030-06-30", "distribution", "66141.8948"),
    ("2030-09-30", "capital_call", "-55125.0000"),
    ("2031-06-30", "distribution", "131497.7890"),
    ("2031-09-30", "capital_call", "-41343.7500"),
    ("2032-06-30", "distribution", "219139.8490"),
    ("2032-09-30", "capital_call", "-31007.8125"),
    ("2033-06-30", "distribution", "311840.1276"),
    ("2033-09-30", "capital_call", "-23255.8594"),
    ("2034-06-30", "distribution", "376869.6928"),
    ("2034-09-30", "capital_call", "-17441.8945"),
    ("2035-06-30", "distribution", "377024.4339"),
    ("2035-09-30", "capital_call", "-13081.4209"),
    ("2036-06-30", "distribution", "296836.5568"),
    ("2036-09-30", "capital_call", "-9811.0657"),
    ("2037-06-30", "distribution", "167715.3570"),
    ("2037-09-30", "capital_call", "-7358.2993"),
    ("2038-06-30", "distribution", "64497.9117"),
]

_PD_QUARTERLY_GOLDEN: list[_Row] = [
    ("2027-03-31", "capital_call", "-250000.0000"),
    ("2028-03-31", "capital_call", "-100000.0000"),
    ("2028-12-31", "distribution", "33750.0000"),
    ("2029-03-31", "capital_call", "-37500.0000"),
    ("2029-12-31", "distribution", "83393.6438"),
    ("2030-03-31", "capital_call", "-28125.0000"),
    ("2030-12-31", "distribution", "121140.4253"),
    ("2031-03-31", "capital_call", "-21093.7500"),
    ("2031-12-31", "distribution", "133206.5759"),
    ("2032-03-31", "capital_call", "-15820.3125"),
    ("2032-12-31", "distribution", "110467.9889"),
    ("2033-03-31", "capital_call", "-11865.2344"),
    ("2033-12-31", "distribution", "66676.6415"),
    ("2034-03-31", "capital_call", "-8898.9258"),
    ("2034-12-31", "distribution", "37682.8967"),
]

_RE_MONTHLY_GOLDEN: list[_Row] = [
    ("2026-10-31", "capital_call", "-175000.0000"),
    ("2027-09-30", "distribution", "2180.0000"),
    ("2027-10-31", "capital_call", "-113750.0000"),
    ("2028-09-30", "distribution", "17039.7520"),
    ("2028-10-31", "capital_call", "-42250.0000"),
    ("2029-09-30", "distribution", "51277.2671"),
    ("2029-10-31", "capital_call", "-33800.0000"),
    ("2030-09-30", "distribution", "97789.5933"),
    ("2030-10-31", "capital_call", "-27040.0000"),
    ("2031-09-30", "distribution", "149110.7370"),
    ("2031-10-31", "capital_call", "-21632.0000"),
    ("2032-09-30", "distribution", "186143.6555"),
    ("2032-10-31", "capital_call", "-17305.6000"),
    ("2033-09-30", "distribution", "188299.1204"),
    ("2033-10-31", "capital_call", "-13844.4800"),
    ("2034-09-30", "distribution", "148791.3822"),
    ("2034-10-31", "capital_call", "-11075.5840"),
    ("2035-09-30", "distribution", "86117.7415"),
    ("2035-10-31", "capital_call", "-8860.4672"),
    ("2036-09-30", "distribution", "42951.3529"),
]

_INFRA_QUARTERLY_GOLDEN: list[_Row] = [
    ("2026-09-30", "capital_call", "-150000.0000"),
    ("2027-06-30", "distribution", "10083.4620"),
    ("2027-09-30", "capital_call", "-105000.0000"),
    ("2028-06-30", "distribution", "42717.9536"),
    ("2028-09-30", "capital_call", "-61250.0000"),
    ("2029-06-30", "distribution", "101272.6683"),
    ("2029-09-30", "capital_call", "-36750.0000"),
    ("2030-06-30", "distribution", "182893.2227"),
    ("2030-09-30", "capital_call", "-29400.0000"),
    ("2031-06-30", "distribution", "278376.0510"),
    ("2031-09-30", "capital_call", "-23520.0000"),
    ("2032-06-30", "distribution", "372525.4186"),
    ("2032-09-30", "capital_call", "-18816.0000"),
    ("2033-06-30", "distribution", "443447.4263"),
    ("2033-09-30", "capital_call", "-15052.8000"),
    ("2034-06-30", "distribution", "469664.9079"),
    ("2034-09-30", "capital_call", "-12042.2400"),
    ("2035-06-30", "distribution", "439247.6793"),
    ("2035-09-30", "capital_call", "-9633.7920"),
    ("2036-06-30", "distribution", "357572.5724"),
    ("2036-09-30", "capital_call", "-7707.0336"),
    ("2037-06-30", "distribution", "247945.8752"),
    ("2037-09-30", "capital_call", "-6165.6269"),
    ("2038-06-30", "distribution", "142136.9835"),
    ("2038-09-30", "capital_call", "-4932.5015"),
    ("2039-06-30", "distribution", "64976.5271"),
    ("2039-09-30", "capital_call", "-3946.0012"),
    ("2040-06-30", "distribution", "23338.6529"),
    ("2040-09-30", "capital_call", "-3156.8010"),
    ("2041-06-30", "distribution", "10892.0221"),
]


def _expected(rows: list[_Row], *, currency: str) -> list[PlanFlow]:
    """Reconstruct the exact expected flow list from pinned literal rows."""
    return [
        PlanFlow(
            investment_id=_INV,
            as_of_date=date.fromisoformat(iso_date),
            amount=Decimal(amount),
            currency=currency,
            flow_type=flow_type,
        )
        for iso_date, flow_type, amount in rows
    ]


def test_golden_private_equity_quarterly() -> None:
    """PE, mid-life, quarterly — the exact remaining profile."""
    flows = generate_remaining_profile(
        investment_id=_INV,
        commitment=Decimal("1000000"),
        called_to_date=Decimal("200000"),
        current_nav=Decimal("250000"),
        t0=date(2026, 6, 30),
        investment_type="private_equity",
        currency="EUR",
        periodisation=Periodisation.QUARTERLY,
    )
    assert flows == _expected(_PE_QUARTERLY_GOLDEN, currency="EUR")


def test_golden_private_debt_quarterly_new_fund() -> None:
    """Private debt, brand-new (no calls, no NAV), quarterly.

    Year one contributes but distributes nothing — the NAV is still zero — so
    its distribution flow is absent, exercising the zero-at-scale skip.
    """
    flows = generate_remaining_profile(
        investment_id=_INV,
        commitment=Decimal("500000"),
        called_to_date=Decimal("0"),
        current_nav=Decimal("0"),
        t0=date(2026, 12, 31),
        investment_type="private_debt",
        currency="USD",
        periodisation=Periodisation.QUARTERLY,
    )
    assert flows == _expected(_PD_QUARTERLY_GOLDEN, currency="USD")


def test_golden_real_estate_monthly() -> None:
    """Real estate, mid-life, monthly — the exact profile on the monthly grid."""
    flows = generate_remaining_profile(
        investment_id=_INV,
        commitment=Decimal("800000"),
        called_to_date=Decimal("300000"),
        current_nav=Decimal("200000"),
        t0=date(2026, 9, 30),
        investment_type="real_estate",
        currency="GBP",
        periodisation=Periodisation.MONTHLY,
    )
    assert flows == _expected(_RE_MONTHLY_GOLDEN, currency="GBP")


def test_golden_infra_equity_quarterly() -> None:
    """Infrastructure equity, heavily called, long life, quarterly."""
    flows = generate_remaining_profile(
        investment_id=_INV,
        commitment=Decimal("2000000"),
        called_to_date=Decimal("1500000"),
        current_nav=Decimal("1200000"),
        t0=date(2026, 6, 30),
        investment_type="infra_equity",
        currency="CHF",
        periodisation=Periodisation.QUARTERLY,
    )
    assert flows == _expected(_INFRA_QUARTERLY_GOLDEN, currency="CHF")


# ---------------------------------------------------------------------------
# Determinism and the remaining-profile semantics (ADR-0105 §2)
# ---------------------------------------------------------------------------


def test_identical_inputs_yield_identical_output() -> None:
    """The same input twice yields bit-identical output (ADR-0105 §2)."""
    kwargs = dict(
        investment_id=_INV,
        commitment=Decimal("1000000"),
        called_to_date=Decimal("200000"),
        current_nav=Decimal("250000"),
        t0=date(2026, 6, 30),
        investment_type="private_equity",
        currency="EUR",
        periodisation=Periodisation.QUARTERLY,
    )
    assert generate_remaining_profile(**kwargs) == generate_remaining_profile(**kwargs)


def test_midlife_fund_contributes_only_the_remaining_unfunded() -> None:
    """A mid-life fund draws only the still-unfunded remainder, never a re-call.

    The schedule is applied to ``commitment − called_to_date``, so seeding a
    non-zero ``called_to_date`` shrinks every contribution — it never re-calls
    capital already drawn (ADR-0105 §2, the analogue of ``repace_flows``'
    remaining-profile semantics).
    """
    common = dict(
        investment_id=_INV,
        commitment=Decimal("1000000"),
        current_nav=Decimal("250000"),
        t0=date(2026, 6, 30),
        investment_type="private_equity",
        currency="EUR",
        periodisation=Periodisation.QUARTERLY,
    )
    fresh = generate_remaining_profile(called_to_date=Decimal("0"), **common)
    midlife = generate_remaining_profile(called_to_date=Decimal("300000"), **common)

    rc_1 = parameters_for("private_equity").rate_of_contribution[0]
    fresh_calls = [f for f in fresh if f.flow_type == "capital_call"]
    midlife_calls = [f for f in midlife if f.flow_type == "capital_call"]

    # The first call draws the first-year rate against the *remaining* unfunded.
    assert fresh_calls[0].amount == -(rc_1 * Decimal("1000000")).quantize(Decimal("0.0001"))
    assert midlife_calls[0].amount == -(rc_1 * (Decimal("1000000") - Decimal("300000"))).quantize(
        Decimal("0.0001")
    )

    # And the mid-life fund calls strictly less capital in total: it is picked
    # up where it stands, not restarted.
    def total(calls: list[PlanFlow]) -> Decimal:
        return sum((abs(c.amount) for c in calls), Decimal(0))

    assert total(midlife_calls) < total(fresh_calls)
    assert total(midlife_calls) <= Decimal("1000000") - Decimal("300000")


def test_fully_called_fund_generates_distributions_only() -> None:
    """A fully-drawn fund has no uncalled balance, so it only distributes."""
    flows = generate_remaining_profile(
        investment_id=_INV,
        commitment=Decimal("1000000"),
        called_to_date=Decimal("1000000"),
        current_nav=Decimal("900000"),
        t0=date(2026, 6, 30),
        investment_type="private_equity",
        currency="EUR",
        periodisation=Periodisation.QUARTERLY,
    )
    assert flows
    assert {f.flow_type for f in flows} == {"distribution"}


# ---------------------------------------------------------------------------
# Investor-flow exemption (ADR-0103 §5 / ADR-0105 §6)
# ---------------------------------------------------------------------------


def test_generator_never_emits_an_overlay_exempt_flow_type() -> None:
    """Only ``capital_call``/``distribution`` — never an exempt investor flow.

    Asserted against the imported :data:`OVERLAY_EXEMPT_FLOW_TYPES` (the single
    formulation, ADR-0103 §5), never a restated set.
    """
    flows = generate_remaining_profile(
        investment_id=_INV,
        commitment=Decimal("1000000"),
        called_to_date=Decimal("200000"),
        current_nav=Decimal("250000"),
        t0=date(2026, 6, 30),
        investment_type="private_equity",
        currency="EUR",
        periodisation=Periodisation.QUARTERLY,
    )
    emitted = {f.flow_type for f in flows}
    assert emitted == {"capital_call", "distribution"}
    assert emitted.isdisjoint(OVERLAY_EXEMPT_FLOW_TYPES)


def test_sign_convention_matches_the_book() -> None:
    """Calls are negative, distributions positive (ADR-0043 / PlanFlow)."""
    flows = generate_remaining_profile(
        investment_id=_INV,
        commitment=Decimal("1000000"),
        called_to_date=Decimal("200000"),
        current_nav=Decimal("250000"),
        t0=date(2026, 6, 30),
        investment_type="private_equity",
        currency="EUR",
        periodisation=Periodisation.QUARTERLY,
    )
    for flow in flows:
        if flow.flow_type == "capital_call":
            assert flow.amount < 0
        else:
            assert flow.amount > 0


# ---------------------------------------------------------------------------
# Un-modellable fallback and loud rejection (ADR-0105 §3 / §Consequences)
# ---------------------------------------------------------------------------


def test_missing_commitment_returns_empty() -> None:
    """A commitment the book never stated yields no profile — never a fabricated one.

    Mirrors :func:`services.investments.pacing_rows._unfunded`'s ``None``
    posture for the same missing datum; S34.7 reads ``[]`` as "stay disabled".
    """
    assert (
        generate_remaining_profile(
            investment_id=_INV,
            commitment=None,
            called_to_date=Decimal("0"),
            current_nav=Decimal("0"),
            t0=date(2026, 6, 30),
            investment_type="private_equity",
            currency="EUR",
            periodisation=Periodisation.QUARTERLY,
        )
        == []
    )


def test_fully_realised_fund_returns_empty() -> None:
    """A fully-called fund with no NAV has nothing left to project."""
    assert (
        generate_remaining_profile(
            investment_id=_INV,
            commitment=Decimal("1000000"),
            called_to_date=Decimal("1000000"),
            current_nav=Decimal("0"),
            t0=date(2026, 6, 30),
            investment_type="private_equity",
            currency="EUR",
            periodisation=Periodisation.QUARTERLY,
        )
        == []
    )


@pytest.mark.parametrize(
    "investment_type", ["listed_equity", "listed_bonds", "cash", "other", "xyz"]
)
def test_non_capital_account_type_raises_loudly(investment_type: str) -> None:
    """A non-capital-account type is rejected, never defaulted (ADR-0105 §3)."""
    with pytest.raises(TAProfileUnsupportedTypeError) as excinfo:
        generate_remaining_profile(
            investment_id=_INV,
            commitment=Decimal("1000000"),
            called_to_date=Decimal("0"),
            current_nav=Decimal("0"),
            t0=date(2026, 6, 30),
            investment_type=investment_type,
            currency="EUR",
            periodisation=Periodisation.QUARTERLY,
        )
    assert excinfo.value.field == "investment_type"
    assert investment_type in str(excinfo.value)


# ---------------------------------------------------------------------------
# Type reuse and periodisation mapping (ADR-0105 §2a / §2)
# ---------------------------------------------------------------------------


def test_generated_flow_is_a_plan_flow() -> None:
    """``GeneratedPlanFlow`` *is* ``PlanFlow`` — S34.7's fold needs no translation."""
    assert GeneratedPlanFlow is PlanFlow
    flows = generate_remaining_profile(
        investment_id=_INV,
        commitment=Decimal("1000000"),
        called_to_date=Decimal("200000"),
        current_nav=Decimal("250000"),
        t0=date(2026, 6, 30),
        investment_type="private_equity",
        currency="EUR",
        periodisation=Periodisation.QUARTERLY,
    )
    assert all(isinstance(f, PlanFlow) for f in flows)
    # Every flow is fully formed — it carries the investment id, so folding the
    # list into PlanFrames.plan_flows is a concatenation.
    assert all(f.investment_id == _INV for f in flows)


def test_periodisation_changes_dates_not_the_annual_model() -> None:
    """Quarterly and monthly differ only in *when* flows land, not their economics.

    A model year's contribution lands one period into the year; under monthly
    that is a month after the year's start, under quarterly a quarter after —
    so the dates differ, while the amounts and the annual totals are identical.
    """
    common = dict(
        investment_id=_INV,
        commitment=Decimal("1000000"),
        called_to_date=Decimal("200000"),
        current_nav=Decimal("250000"),
        t0=date(2026, 6, 30),
        investment_type="private_equity",
        currency="EUR",
    )
    quarterly = generate_remaining_profile(periodisation=Periodisation.QUARTERLY, **common)
    monthly = generate_remaining_profile(periodisation=Periodisation.MONTHLY, **common)

    def by_type(flows: list[PlanFlow], flow_type: str) -> list[PlanFlow]:
        return [f for f in flows if f.flow_type == flow_type]

    q_calls = by_type(quarterly, "capital_call")
    m_calls = by_type(monthly, "capital_call")

    # Contribution dates differ (one quarter vs one month after the year start),
    # first call lands 2026-09-30 quarterly / 2026-07-31 monthly …
    assert q_calls[0].as_of_date == date(2026, 9, 30)
    assert m_calls[0].as_of_date == date(2026, 7, 31)
    # … but the amounts and count are identical — the model is annual.
    assert [f.amount for f in q_calls] == [f.amount for f in m_calls]
    # Distributions land at the annual boundary under both, so they coincide.
    assert [(f.as_of_date, f.amount) for f in by_type(quarterly, "distribution")] == [
        (f.as_of_date, f.amount) for f in by_type(monthly, "distribution")
    ]


def test_flows_are_ascending_by_date() -> None:
    """The emitted profile is date-ordered — a stable, deterministic sequence."""
    flows = generate_remaining_profile(
        investment_id=_INV,
        commitment=Decimal("1000000"),
        called_to_date=Decimal("200000"),
        current_nav=Decimal("250000"),
        t0=date(2026, 6, 30),
        investment_type="private_equity",
        currency="EUR",
        periodisation=Periodisation.QUARTERLY,
    )
    dates = [f.as_of_date for f in flows]
    assert dates == sorted(dates)


def test_currency_is_upper_cased() -> None:
    """Every emitted flow is stated in the fund's position currency, upper-cased."""
    flows = generate_remaining_profile(
        investment_id=_INV,
        commitment=Decimal("1000000"),
        called_to_date=Decimal("200000"),
        current_nav=Decimal("250000"),
        t0=date(2026, 6, 30),
        investment_type="private_equity",
        currency="eur",
        periodisation=Periodisation.QUARTERLY,
    )
    assert {f.currency for f in flows} == {"EUR"}
