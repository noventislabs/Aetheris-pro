"""Leverage architecture: 1-500x candidates, fail-closed approval.

The whole point of these tests is that a wide *candidate* range does not widen
what the system may actually do. Every path that lacks a real constraint must
end in a rejection with a named reason, and no path may invent an exchange
ceiling.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from aetheris.analysis.leverage import (
    TARGET_ADVERSE_MOVE_PERCENT,
    propose_leverage,
    resolve_leverage,
)
from aetheris.domain.leverage import (
    LEVERAGE_MAX,
    LEVERAGE_MIN,
    LeverageOutcome,
    LeverageReason,
    LeverageRequest,
)


def request(leverage: str) -> LeverageRequest:
    return LeverageRequest(requested_leverage=Decimal(leverage), basis="test")


# ----------------------------------------------------------------------
# The architectural range
# ----------------------------------------------------------------------


def test_the_candidate_range_is_one_to_five_hundred() -> None:
    assert Decimal(1) == LEVERAGE_MIN
    assert Decimal(500) == LEVERAGE_MAX


@pytest.mark.parametrize("value", ["1", "3", "25", "125", "500"])
def test_any_candidate_inside_the_range_is_representable(value: str) -> None:
    assert request(value).requested_leverage == Decimal(value)


@pytest.mark.parametrize("value", ["0", "0.5", "501", "1000", "-5"])
def test_a_candidate_outside_the_range_cannot_be_constructed(value: str) -> None:
    with pytest.raises(ValidationError):
        request(value)


# ----------------------------------------------------------------------
# Candidate derivation
# ----------------------------------------------------------------------


def test_candidate_is_inversely_proportional_to_volatility() -> None:
    """Higher ATR means a bigger adverse move per event, so less leverage."""
    calm = propose_leverage(atr_percent=Decimal("0.1"), conditions_met=4, conditions_total=4)
    wild = propose_leverage(atr_percent=Decimal("5"), conditions_met=4, conditions_total=4)
    assert calm is not None and wild is not None
    assert calm.requested_leverage > wild.requested_leverage


def test_candidate_matches_the_published_formula() -> None:
    # 2% reference / 0.5% ATR = 4, times 4/4 agreement = 4x
    result = propose_leverage(atr_percent=Decimal("0.5"), conditions_met=4, conditions_total=4)
    assert result is not None
    assert result.requested_leverage == Decimal(4)
    assert result.inputs["target_adverse_move_percent"] == TARGET_ADVERSE_MOVE_PERCENT


def test_partial_agreement_only_ever_scales_the_candidate_down() -> None:
    """Analysis may lower a request. It can never raise one, and never approve."""
    full = propose_leverage(atr_percent=Decimal("0.1"), conditions_met=4, conditions_total=4)
    partial = propose_leverage(atr_percent=Decimal("0.1"), conditions_met=2, conditions_total=4)
    assert full is not None and partial is not None
    assert partial.requested_leverage < full.requested_leverage


def test_candidate_is_clamped_to_the_maximum() -> None:
    """An extremely quiet instrument still cannot request beyond the range."""
    result = propose_leverage(atr_percent=Decimal("0.0001"), conditions_met=4, conditions_total=4)
    assert result is not None
    assert result.requested_leverage == LEVERAGE_MAX


def test_candidate_is_clamped_to_the_minimum() -> None:
    result = propose_leverage(atr_percent=Decimal("50"), conditions_met=1, conditions_total=4)
    assert result is not None
    assert result.requested_leverage == LEVERAGE_MIN


def test_unmeasurable_volatility_yields_no_candidate() -> None:
    """Not 1x. Absent -- a default would look like a deliberate choice."""
    assert propose_leverage(atr_percent=Decimal(0), conditions_met=4, conditions_total=4) is None


def test_candidate_always_states_its_basis() -> None:
    result = propose_leverage(atr_percent=Decimal("1"), conditions_met=4, conditions_total=4)
    assert result is not None
    assert "risk engine" in result.basis
    assert "not an authorisation" in result.basis


# ----------------------------------------------------------------------
# Fail-closed resolution
# ----------------------------------------------------------------------


def test_unknown_exchange_maximum_rejects_rather_than_assuming_five_hundred() -> None:
    """The central rule: an unknown ceiling is not permission.

    Binance serves per-symbol leverage brackets only to authenticated callers,
    and the ceilings vary widely by symbol. Defaulting to the top of the
    architectural range would invent an exchange constraint.
    """
    decision = resolve_leverage(
        request("125"),
        exchange_max_leverage=None,
        risk_max_leverage=Decimal(10),
        risk_engine_available=True,
    )
    assert decision.outcome is LeverageOutcome.REJECTED
    assert decision.reason is LeverageReason.EXCHANGE_MAX_UNKNOWN
    assert decision.approved_leverage is None
    assert decision.requested_leverage == Decimal(125)
    assert decision.detail


def test_missing_risk_engine_rejects_even_with_a_known_exchange_maximum() -> None:
    """The risk engine has final authority, so its absence blocks approval."""
    decision = resolve_leverage(
        request("10"),
        exchange_max_leverage=Decimal(50),
        risk_max_leverage=None,
        risk_engine_available=False,
    )
    assert decision.outcome is LeverageOutcome.REJECTED
    assert decision.reason is LeverageReason.RISK_ENGINE_UNAVAILABLE
    assert decision.approved_leverage is None
    assert "final authority" in decision.detail


def test_risk_engine_present_but_producing_no_maximum_still_rejects() -> None:
    decision = resolve_leverage(
        request("10"),
        exchange_max_leverage=Decimal(50),
        risk_max_leverage=None,
        risk_engine_available=True,
    )
    assert decision.reason is LeverageReason.RISK_MAX_UNKNOWN
    assert decision.approved_leverage is None


def test_no_candidate_rejects_with_insufficient_data() -> None:
    decision = resolve_leverage(
        None,
        exchange_max_leverage=Decimal(50),
        risk_max_leverage=Decimal(10),
        risk_engine_available=True,
    )
    assert decision.outcome is LeverageOutcome.REJECTED
    assert decision.reason is LeverageReason.INSUFFICIENT_DATA
    assert decision.approved_leverage is None


def test_every_rejection_names_a_reason_and_explains_it() -> None:
    for decision in (
        resolve_leverage(
            request("10"),
            exchange_max_leverage=None,
            risk_max_leverage=None,
            risk_engine_available=False,
        ),
        resolve_leverage(
            None,
            exchange_max_leverage=Decimal(5),
            risk_max_leverage=Decimal(5),
            risk_engine_available=True,
        ),
    ):
        assert decision.outcome is LeverageOutcome.REJECTED
        assert decision.reason
        assert len(decision.detail) > 20


# ----------------------------------------------------------------------
# Approval, once every constraint is known
# ----------------------------------------------------------------------


def test_approval_when_the_request_fits_every_constraint() -> None:
    decision = resolve_leverage(
        request("5"),
        exchange_max_leverage=Decimal(50),
        risk_max_leverage=Decimal(10),
        risk_engine_available=True,
    )
    assert decision.outcome is LeverageOutcome.APPROVED
    assert decision.approved_leverage == Decimal(5)


def test_the_exchange_ceiling_binds_when_it_is_lowest() -> None:
    """A symbol whose real cap is far below the architectural range."""
    decision = resolve_leverage(
        request("500"),
        exchange_max_leverage=Decimal(8),
        risk_max_leverage=Decimal(20),
        risk_engine_available=True,
    )
    assert decision.outcome is LeverageOutcome.REDUCED
    assert decision.approved_leverage == Decimal(8)
    assert decision.binding_constraint == "exchange_max_leverage"
    assert decision.reason is LeverageReason.BOUND_BY_EXCHANGE_MAX


def test_the_risk_ceiling_binds_when_it_is_lowest() -> None:
    decision = resolve_leverage(
        request("125"),
        exchange_max_leverage=Decimal(75),
        risk_max_leverage=Decimal(3),
        risk_engine_available=True,
    )
    assert decision.outcome is LeverageOutcome.REDUCED
    assert decision.approved_leverage == Decimal(3)
    assert decision.binding_constraint == "risk_max_leverage"


@pytest.mark.parametrize(
    ("requested", "exchange_max", "risk_max"),
    [
        ("500", "125", "3"),
        ("10", "500", "500"),
        ("1", "1", "1"),
        ("250", "250", "250"),
        ("500", "500", "500"),
    ],
)
def test_approved_never_exceeds_any_constraint(
    requested: str, exchange_max: str, risk_max: str
) -> None:
    """The invariant, exhaustively: approved <= every input ceiling."""
    decision = resolve_leverage(
        request(requested),
        exchange_max_leverage=Decimal(exchange_max),
        risk_max_leverage=Decimal(risk_max),
        risk_engine_available=True,
    )
    assert decision.approved_leverage is not None
    assert decision.approved_leverage <= Decimal(requested)
    assert decision.approved_leverage <= Decimal(exchange_max)
    assert decision.approved_leverage <= Decimal(risk_max)


def test_requested_and_approved_stay_separate_fields() -> None:
    """Rule 7: the request must never be mistaken for the permission."""
    decision = resolve_leverage(
        request("500"),
        exchange_max_leverage=Decimal(20),
        risk_max_leverage=Decimal(3),
        risk_engine_available=True,
    )
    assert decision.requested_leverage == Decimal(500)
    assert decision.approved_leverage == Decimal(3)
    assert decision.requested_leverage != decision.approved_leverage


def test_decision_always_carries_the_no_execution_note() -> None:
    decision = resolve_leverage(
        request("5"),
        exchange_max_leverage=Decimal(10),
        risk_max_leverage=Decimal(10),
        risk_engine_available=True,
    )
    assert "Risk Engine has final authority" in decision.note
    assert "no order is placed" in decision.note.lower()


# ----------------------------------------------------------------------
# Integration with the strategy
# ----------------------------------------------------------------------


def test_strategy_results_never_approve_leverage_in_this_build() -> None:
    """No venue ceiling and no risk engine, so nothing above 1x is approvable.

    A candidate of exactly 1x would resolve at the domain minimum (see below),
    which is why the assertion is "never *leveraged*" rather than "always
    rejected" -- 1x is not leverage. This fixture produces no directional bias,
    so it never reaches that question.
    """
    from datetime import UTC, datetime, timedelta

    from aetheris.analysis.strategies.registry import evaluate_from_candles
    from aetheris.domain.enums import Timeframe
    from aetheris.domain.market import Candle

    base = datetime(2026, 9, 20, tzinfo=UTC)
    candles = [
        Candle(
            open_time=base + timedelta(hours=i),
            close_time=base + timedelta(hours=i + 1, milliseconds=-1),
            open=Decimal(100 + (i * 7) % 23),
            high=Decimal(103 + (i * 7) % 23),
            low=Decimal(98 + (i * 7) % 23),
            close=Decimal(101 + (i * 7) % 23),
            volume=Decimal(100),
        )
        for i in range(200)
    ]
    result = evaluate_from_candles(candles, symbol="TESTUSDT", timeframe=Timeframe.H1)

    assert result.leverage is not None
    # Never guessed from the architectural range.
    assert result.leverage.exchange_max_leverage is None
    approved = result.leverage.approved_leverage
    assert approved is None or approved == Decimal(1), (
        "nothing above the domain minimum can be approved without a venue ceiling and a risk engine"
    )
    assert result.leverage.reason in {
        LeverageReason.EXCHANGE_MAX_UNKNOWN,
        LeverageReason.NO_DIRECTIONAL_BIAS,
        LeverageReason.INSUFFICIENT_DATA,
        LeverageReason.APPROVED_AT_DOMAIN_MINIMUM,
    }


def test_leverage_is_set_in_exactly_two_modules_and_only_after_approval() -> None:
    """Rule 9, narrowed rather than dropped.

    "No leverage API exists" was right while nothing executed. Phase 8b has to
    set leverage at a venue -- risk approval is binding, not advisory, so the
    venue must be made to agree before an order is placed. What replaces the
    ban is confinement: the path constants live in the testnet endpoint map,
    the call lives in the adapter, and the service is the only caller. Anything
    else naming them would be leverage being set outside the approved chain.
    """
    import pathlib

    import aetheris

    root = pathlib.Path(aetheris.__file__).parent
    may_name_paths = {"adapters/exchange/binance/testnet_endpoints.py"}
    may_set = {
        "adapters/exchange/binance/testnet_adapter.py",
        "adapters/exchange/ports.py",
        "services/testnet.py",
    }
    for path in root.rglob("*.py"):
        relative = path.relative_to(root).as_posix()
        source = path.read_text(encoding="utf-8")
        for forbidden in ("/fapi/v1/leverage", "/fapi/v1/marginType"):
            assert forbidden not in source or relative in may_name_paths, (
                f"{relative} references {forbidden}"
            )
        assert "set_leverage" not in source or relative in may_set, (
            f"{relative} sets leverage outside the approved chain"
        )


# ----------------------------------------------------------------------
# Hardened constraint validation
# ----------------------------------------------------------------------

INVALID_CEILINGS = [
    pytest.param(Decimal(0), "zero", id="zero"),
    pytest.param(Decimal(-1), "negative", id="negative"),
    pytest.param(Decimal("-25"), "negative-large", id="negative-large"),
    pytest.param(Decimal("0.5"), "fractional-below-one", id="fractional-below-one"),
    pytest.param(Decimal("0.999"), "just-below-one", id="just-below-one"),
    pytest.param(Decimal(501), "above-domain", id="above-domain"),
    pytest.param(Decimal(1000), "far-above-domain", id="far-above-domain"),
]


@pytest.mark.parametrize(("ceiling", "_label"), INVALID_CEILINGS)
def test_invalid_exchange_ceiling_fails_closed(ceiling: Decimal, _label: str) -> None:
    """A supplied-but-nonsensical venue ceiling approves nothing.

    Note the >500 case is *refused*, not clamped to 500. Clamping would invent
    a domain rule that nothing declares.
    """
    decision = resolve_leverage(
        request("10"),
        exchange_max_leverage=ceiling,
        risk_max_leverage=Decimal(10),
        risk_engine_available=True,
    )
    assert decision.outcome is LeverageOutcome.REJECTED
    assert decision.reason is LeverageReason.EXCHANGE_MAX_INVALID
    assert decision.approved_leverage is None
    assert decision.detail


@pytest.mark.parametrize(("ceiling", "_label"), INVALID_CEILINGS)
def test_invalid_risk_ceiling_fails_closed(ceiling: Decimal, _label: str) -> None:
    decision = resolve_leverage(
        request("10"),
        exchange_max_leverage=Decimal(50),
        risk_max_leverage=ceiling,
        risk_engine_available=True,
    )
    assert decision.outcome is LeverageOutcome.REJECTED
    assert decision.reason is LeverageReason.RISK_MAX_INVALID
    assert decision.approved_leverage is None


@pytest.mark.parametrize("ceiling", [10, 10.0, "10", None])
def test_non_decimal_ceilings_are_refused_not_coerced(ceiling: object) -> None:
    """A float has already lost precision; an int skipped the money rules."""
    decision = resolve_leverage(
        request("5"),
        exchange_max_leverage=ceiling,  # type: ignore[arg-type]
        risk_max_leverage=Decimal(10),
        risk_engine_available=True,
    )
    assert decision.outcome is LeverageOutcome.REJECTED
    assert decision.approved_leverage is None


@pytest.mark.parametrize("boundary", ["1", "500"])
def test_ceilings_exactly_on_the_domain_boundary_are_valid(boundary: str) -> None:
    decision = resolve_leverage(
        request("1"),
        exchange_max_leverage=Decimal(boundary),
        risk_max_leverage=Decimal(boundary),
        risk_engine_available=True,
    )
    assert decision.outcome is LeverageOutcome.APPROVED
    assert decision.approved_leverage == Decimal(1)


def test_a_valid_five_hundred_x_request_resolves_when_every_ceiling_allows_it() -> None:
    """500x is representable — and still only reachable through the full chain."""
    decision = resolve_leverage(
        request("500"),
        exchange_max_leverage=Decimal(500),
        risk_max_leverage=Decimal(500),
        risk_engine_available=True,
    )
    assert decision.outcome is LeverageOutcome.APPROVED
    assert decision.approved_leverage == Decimal(500)


def test_the_min_rule_still_holds_after_validation() -> None:
    decision = resolve_leverage(
        request("500"),
        exchange_max_leverage=Decimal(125),
        risk_max_leverage=Decimal(3),
        risk_engine_available=True,
    )
    assert decision.approved_leverage == min(Decimal(500), Decimal(125), Decimal(3))


# ----------------------------------------------------------------------
# Strict condition-count validation
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("met", "total"),
    [(4, 0), (4, -1), (-1, 4), (-5, 10), (5, 4), (100, 4)],
)
def test_incoherent_condition_counts_yield_no_candidate(met: int, total: int) -> None:
    """A ratio above 1 would scale a candidate *up* on the strength of a bug."""
    assert (
        propose_leverage(atr_percent=Decimal("1"), conditions_met=met, conditions_total=total)
        is None
    )


def test_zero_of_n_is_valid_input_with_no_candidate() -> None:
    """Coherent counts, nothing agrees: no candidate, and that is the answer."""
    assert propose_leverage(atr_percent=Decimal("1"), conditions_met=0, conditions_total=4) is None


@pytest.mark.parametrize(("met", "total"), [(1, 4), (2, 4), (3, 4), (4, 4), (7, 7), (1, 1)])
def test_valid_condition_counts_produce_a_candidate(met: int, total: int) -> None:
    result = propose_leverage(
        atr_percent=Decimal("0.1"), conditions_met=met, conditions_total=total
    )
    assert result is not None
    assert LEVERAGE_MIN <= result.requested_leverage <= LEVERAGE_MAX


@pytest.mark.parametrize("total", [1, 2, 3, 4, 5, 10, 25])
def test_agreement_never_exceeds_one(total: int) -> None:
    """Swept across every coherent count: agreement is bounded by 1."""
    for met in range(total + 1):
        result = propose_leverage(
            atr_percent=Decimal("0.05"), conditions_met=met, conditions_total=total
        )
        if result is None:
            continue
        assert result.inputs["agreement"] <= Decimal(1)
        full = propose_leverage(
            atr_percent=Decimal("0.05"), conditions_met=total, conditions_total=total
        )
        assert full is not None
        assert result.requested_leverage <= full.requested_leverage


# ----------------------------------------------------------------------
# The candidate is not, and is never described as, a confidence
# ----------------------------------------------------------------------


def test_leverage_modules_never_use_confidence_vocabulary() -> None:
    """Rule 3: the deterministic candidate must not be dressed up as a forecast."""
    import pathlib

    import aetheris

    root = pathlib.Path(aetheris.__file__).parent
    banned = (
        "ai confidence",
        "probability of profit",
        "win probability",
        "win_probability",
        "ai certainty",
        "guaranteed leverage",
        "ai-selected execution leverage",
    )
    for name in ("domain/leverage.py", "analysis/leverage.py"):
        # Emphasis markers are stripped so "**not**" reads as "not".
        source = (root / name).read_text(encoding="utf-8").lower().replace("*", "")
        for phrase in banned:
            # The vocabulary may appear only inside an explicit denial. A list
            # like "not an AI confidence, a probability of profit, or a win
            # probability" denies all three from one "not", so the check looks
            # at the preceding context rather than requiring "not a <phrase>".
            start = 0
            while (index := source.find(phrase, start)) != -1:
                context = source[max(0, index - 160) : index]
                assert "not " in context or "never " in context, (
                    f"{name} uses {phrase!r} outside a denial"
                )
                start = index + len(phrase)


def test_candidate_basis_names_its_real_inputs() -> None:
    result = propose_leverage(atr_percent=Decimal("1"), conditions_met=4, conditions_total=4)
    assert result is not None
    basis = result.basis.lower()
    assert "atr volatility" in basis
    assert "deterministic strategy-condition agreement" in basis
    assert "leverage candidate" in basis
    assert "not an authorisation" in basis


# ----------------------------------------------------------------------
# The domain minimum: the one value an unknown ceiling cannot block
# ----------------------------------------------------------------------


def test_one_times_resolves_without_knowing_either_ceiling() -> None:
    """min(1, any valid ceiling) is 1, so the missing values change nothing.

    This is what makes paper trading possible at all in a build with no venue
    ceiling and no risk engine. It is arithmetic over the declared domain, not
    an assumption about a venue.
    """
    decision = resolve_leverage(
        request("1"),
        exchange_max_leverage=None,
        risk_max_leverage=None,
        risk_engine_available=False,
    )
    assert decision.outcome is LeverageOutcome.APPROVED
    assert decision.reason is LeverageReason.APPROVED_AT_DOMAIN_MINIMUM
    assert decision.approved_leverage == Decimal(1)
    assert decision.binding_constraint == "domain_minimum"
    assert "unlevered" in decision.detail


@pytest.mark.parametrize("requested", ["1.0001", "1.5", "2", "3", "25", "500"])
def test_anything_above_the_minimum_still_fails_closed(requested: str) -> None:
    """The exception is exactly 1x and nothing adjacent to it."""
    decision = resolve_leverage(
        request(requested),
        exchange_max_leverage=None,
        risk_max_leverage=None,
        risk_engine_available=False,
    )
    assert decision.outcome is LeverageOutcome.REJECTED
    assert decision.approved_leverage is None
    assert decision.reason is LeverageReason.EXCHANGE_MAX_UNKNOWN


def test_the_minimum_shortcut_does_not_mask_a_malformed_ceiling() -> None:
    """A supplied-but-broken ceiling is a defect to report, not to route around."""
    decision = resolve_leverage(
        request("1"),
        exchange_max_leverage=Decimal(0),
        risk_max_leverage=None,
        risk_engine_available=False,
    )
    assert decision.outcome is LeverageOutcome.REJECTED
    assert decision.reason is LeverageReason.EXCHANGE_MAX_INVALID


def test_a_fully_known_chain_still_reports_approved_in_full_at_one_times() -> None:
    """The shortcut only applies where the chain could not complete."""
    decision = resolve_leverage(
        request("1"),
        exchange_max_leverage=Decimal(20),
        risk_max_leverage=Decimal(3),
        risk_engine_available=True,
    )
    assert decision.reason is LeverageReason.APPROVED_IN_FULL
    assert decision.approved_leverage == Decimal(1)


def test_the_minimum_approval_grants_no_borrowing() -> None:
    """The safety property behind the shortcut, stated as an assertion.

    At 1x margin equals notional, so the approval cannot produce a leveraged
    position however the caller sizes it.
    """
    decision = resolve_leverage(
        request("1"),
        exchange_max_leverage=None,
        risk_max_leverage=None,
        risk_engine_available=False,
    )
    assert decision.approved_leverage is not None
    notional = Decimal("57.25")
    margin = notional / decision.approved_leverage
    assert margin == notional
