"""The boundaries that make testnet execution safe to have at all.

These are the tests that would still matter if every other test passed. Each
one guards a failure whose cost is not a bug report: an order on production, a
key in a log, a position at a leverage nobody approved.

None of them touches a network. They assert properties of construction, which
is the point -- a guarantee that depends on a request having been made is not a
guarantee about what happens when the request is not made.
"""

from __future__ import annotations

import pathlib
from decimal import Decimal

import pytest
from pydantic import SecretStr

import aetheris
from aetheris.adapters.exchange.binance.signing import (
    API_KEY_HEADER,
    build_signed_query,
    utc_timestamp_ms,
)
from aetheris.adapters.exchange.binance.testnet_adapter import BinanceTestnetTradingAdapter
from aetheris.adapters.exchange.binance.venue_status import (
    VENUE_STATUS_TO_STATE,
    parse_order_view,
)
from aetheris.adapters.exchange.errors import ExchangeError, ExchangeInvalidResponseError
from aetheris.core.config import (
    LEGACY_TESTNET_HOST,
    PRODUCTION_HOST,
    TESTNET_ALLOWED_HOST,
    Settings,
    TestnetSettings,
)
from aetheris.domain.enums import OrderSide, OrderState
from aetheris.engines.order.identity import build_intent_key, client_order_id, is_valid_venue_id

PACKAGE_ROOT = pathlib.Path(aetheris.__file__).parent

KEY = SecretStr("test-key-not-a-real-credential")
SECRET = SecretStr("test-secret-not-a-real-credential")


def settings(**overrides: object) -> TestnetSettings:
    return TestnetSettings(
        _env_file=None,  # type: ignore[call-arg]
        api_key=KEY,
        api_secret=SECRET,
        **overrides,
    )


# ----------------------------------------------------------------------
# Host isolation
# ----------------------------------------------------------------------


@pytest.mark.parametrize("host", [PRODUCTION_HOST, LEGACY_TESTNET_HOST, "evil.example.com"])
def test_the_settings_refuse_every_host_but_one(host: str) -> None:
    """An allowlist of one. A denylist would let an unseen hostname through."""
    with pytest.raises(ValueError):
        TestnetSettings(_env_file=None, rest_base_url=f"https://{host}")  # type: ignore[call-arg]


def test_the_allowlisted_host_is_the_documented_testnet() -> None:
    assert TESTNET_ALLOWED_HOST == "demo-fapi.binance.com"
    assert settings().rest_base_url == f"https://{TESTNET_ALLOWED_HOST}"


def test_the_adapter_refuses_a_bad_host_even_if_settings_were_bypassed() -> None:
    """The guard is repeated at the object that would do the damage.

    The settings validator already refuses this. Repeating the check here means
    the guarantee survives someone constructing settings another way -- a guard
    at the point of harm outlives the guard that was supposed to prevent it.
    """
    bypassed = settings().model_copy(update={"rest_base_url": f"https://{PRODUCTION_HOST}"})
    with pytest.raises(ExchangeError, match="refuses host"):
        BinanceTestnetTradingAdapter(bypassed)


def test_the_adapter_refuses_to_start_without_credentials() -> None:
    bare = TestnetSettings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(ExchangeError, match="credentials"):
        BinanceTestnetTradingAdapter(bare)


def test_enabling_testnet_without_credentials_fails_closed_at_startup() -> None:
    """Never a quiet downgrade to paper.

    The alternative -- start anyway and simulate -- would leave the system
    looking like it was trading a venue while it was not, with nothing in any
    response to say so.
    """
    with pytest.raises(ValueError, match="does not fall back to paper"):
        Settings(_env_file=None, testnet_trading_enabled=True)  # type: ignore[call-arg]


# ----------------------------------------------------------------------
# Credential containment
# ----------------------------------------------------------------------


def test_only_the_signing_module_imports_hmac() -> None:
    """One module reads the secret. Everything else passes a SecretStr along."""
    offenders = [
        path.relative_to(PACKAGE_ROOT).as_posix()
        for path in PACKAGE_ROOT.rglob("*.py")
        if "import hmac" in path.read_text(encoding="utf-8")
    ]
    assert offenders == ["adapters/exchange/binance/signing.py"]


def test_the_signing_module_has_no_logger() -> None:
    """Structural, not procedural.

    "Never log the secret" enforced by remembering is enforced by nobody. The
    module that reads the secret has nothing to log with.
    """
    source = (PACKAGE_ROOT / "adapters" / "exchange" / "binance" / "signing.py").read_text(
        encoding="utf-8"
    )
    assert "logging" not in source
    assert "get_logger" not in source
    assert "print(" not in source


def test_a_signed_request_never_reveals_the_key_in_its_repr() -> None:
    request = build_signed_query(
        {"symbol": "BTCUSDT"},
        api_key=KEY,
        api_secret=SECRET,
        recv_window_ms=5000,
    )
    for rendered in (repr(request), str(request), f"{request}"):
        assert KEY.get_secret_value() not in rendered
        assert SECRET.get_secret_value() not in rendered
        assert "redacted" in rendered


def test_the_secret_never_appears_in_the_signed_query() -> None:
    """The signature is derived from the secret. It must not contain it."""
    request = build_signed_query(
        {"symbol": "BTCUSDT"},
        api_key=KEY,
        api_secret=SECRET,
        recv_window_ms=5000,
    )
    assert SECRET.get_secret_value() not in request.query
    assert KEY.get_secret_value() not in request.query
    assert request.headers[API_KEY_HEADER] == KEY.get_secret_value()


def test_no_module_outside_the_adapter_sends_the_api_key_header() -> None:
    allowed = {"adapters/exchange/binance/signing.py"}
    offenders = [
        path.relative_to(PACKAGE_ROOT).as_posix()
        for path in PACKAGE_ROOT.rglob("*.py")
        if API_KEY_HEADER in path.read_text(encoding="utf-8")
    ]
    assert set(offenders) <= allowed, f"{offenders} name the API key header"


def test_no_withdrawal_or_transfer_path_exists_anywhere() -> None:
    """Withdrawal permission is never required and never reachable.

    Asserted against venue *paths*, not against the word. A docstring that says
    "there is no withdrawal endpoint" is the system being honest about its
    limits, and a test that banned the word would push the code towards saying
    less about itself.
    """
    for path in PACKAGE_ROOT.rglob("*.py"):
        source = path.read_text(encoding="utf-8").lower()
        for token in (
            "/sapi/v1/capital/withdraw",
            "/fapi/v1/transfer",
            "/sapi/v1/futures/transfer",
        ):
            assert token not in source, f"{path.relative_to(PACKAGE_ROOT)} names {token}"


# ----------------------------------------------------------------------
# Signing correctness
# ----------------------------------------------------------------------


def test_the_signature_is_hmac_sha256_over_the_string_that_is_sent() -> None:
    """Computed independently here, so the test fails if the scheme changes."""
    import hashlib
    import hmac as _hmac

    request = build_signed_query(
        {"symbol": "BTCUSDT", "side": "BUY"},
        api_key=KEY,
        api_secret=SECRET,
        recv_window_ms=5000,
    )
    body, _, signature = request.query.rpartition("&signature=")
    expected = _hmac.new(
        SECRET.get_secret_value().encode(), body.encode(), hashlib.sha256
    ).hexdigest()
    assert signature == expected


def test_recv_window_and_timestamp_are_added_for_every_caller() -> None:
    """No call site can forget them, because no call site supplies them."""
    query = build_signed_query(
        {"symbol": "BTCUSDT"}, api_key=KEY, api_secret=SECRET, recv_window_ms=7000
    ).query
    assert "recvWindow=7000" in query
    assert "timestamp=" in query


def test_a_clock_offset_shifts_the_timestamp() -> None:
    """A machine running fast fails every signed call; the offset is the fix."""
    assert utc_timestamp_ms(offset_ms=-5000) < utc_timestamp_ms()


# ----------------------------------------------------------------------
# Venue vocabulary
# ----------------------------------------------------------------------


def test_expired_in_match_maps_to_expired_without_a_new_domain_state() -> None:
    """A distinct venue outcome, terminal in the same way.

    Branching the machine on it would put venue vocabulary into the state
    machine permanently. The exact value survives on the record instead.
    """
    assert VENUE_STATUS_TO_STATE["EXPIRED_IN_MATCH"] is OrderState.EXPIRED
    assert VENUE_STATUS_TO_STATE["EXPIRED"] is OrderState.EXPIRED
    assert OrderState.EXPIRED.is_terminal


def test_every_published_venue_status_is_mapped() -> None:
    published = {
        "NEW",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCELED",
        "REJECTED",
        "EXPIRED",
        "EXPIRED_IN_MATCH",
    }
    assert published <= set(VENUE_STATUS_TO_STATE)


def test_an_unknown_venue_status_raises_rather_than_defaulting() -> None:
    """A default here would resolve an order on a string nobody had read."""
    with pytest.raises(ExchangeInvalidResponseError, match="unrecognised"):
        parse_order_view({"status": "SOMETHING_NEW", "clientOrderId": "aeth-1", "executedQty": "0"})


def test_a_zero_average_price_is_reported_as_unknown_not_as_zero() -> None:
    """The venue sends 0 before anything fills. Zero is not a price."""
    view = parse_order_view(
        {
            "status": "NEW",
            "clientOrderId": "aeth-1",
            "orderId": 99,
            "executedQty": "0",
            "avgPrice": "0.00",
        }
    )
    assert view.average_fill_price is None
    assert view.state is OrderState.ACCEPTED


# ----------------------------------------------------------------------
# Identity, against the venue's published rule
# ----------------------------------------------------------------------


def test_the_derived_client_order_id_is_acceptable_to_the_venue() -> None:
    """Binance requires ^[\\.A-Z\\:/a-z0-9_-]{1,36}$ for newClientOrderId.

    The phase 8a identity was designed before this was checked against the
    published rule. It fits, and this test is what keeps it fitting.
    """
    derived = client_order_id(
        account_id="11111111-1111-1111-1111-111111111111",
        intent_key=build_intent_key(
            symbol="ETHUSDT", side=OrderSide.BUY, purpose="testnet", bucket="manual-1"
        ),
    )
    assert len(derived) <= 36
    assert is_valid_venue_id(derived)


def test_the_same_intent_derives_the_same_venue_identity() -> None:
    """What makes a retry after a restart a replay rather than a duplicate."""
    key = build_intent_key(
        symbol="ETHUSDT", side=OrderSide.BUY, purpose="testnet", bucket="manual-1"
    )
    first = client_order_id(account_id="acct", intent_key=key)
    second = client_order_id(account_id="acct", intent_key=key)
    assert first == second


# ----------------------------------------------------------------------
# Leverage: the ceiling is never invented
# ----------------------------------------------------------------------


def test_no_module_hardcodes_a_leverage_ceiling() -> None:
    """500 is the candidate range's top, not a permission.

    The only place it may appear is the domain constant that bounds what may be
    *requested*. Anything else naming it would be a ceiling nobody read from a
    venue.
    """
    allowed = {"domain/leverage.py"}
    for path in PACKAGE_ROOT.rglob("*.py"):
        relative = path.relative_to(PACKAGE_ROOT).as_posix()
        if relative in allowed:
            continue
        source = path.read_text(encoding="utf-8")
        assert "Decimal(500)" not in source and "Decimal('500')" not in source, (
            f"{relative} hardcodes a leverage ceiling"
        )


def test_a_bracket_covers_only_its_own_notional_tier() -> None:
    """Selecting the first bracket would over-permit exactly the large orders."""
    from aetheris.domain.venue import LeverageBracket

    small = LeverageBracket(
        symbol="BTCUSDT",
        max_leverage=Decimal(125),
        notional_floor=Decimal(0),
        notional_cap=Decimal(50_000),
    )
    assert small.covers(Decimal(1_000))
    assert not small.covers(Decimal(500_000))

    open_ended = LeverageBracket(
        symbol="BTCUSDT",
        max_leverage=Decimal(1),
        notional_floor=Decimal(500_000),
        notional_cap=None,
    )
    assert open_ended.covers(Decimal(10_000_000))
    assert not open_ended.covers(Decimal(1_000))
