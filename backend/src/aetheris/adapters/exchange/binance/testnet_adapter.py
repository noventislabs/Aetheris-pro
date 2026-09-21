"""Binance USDT-M Futures **testnet** execution.

The only implementation of :class:`TradingPort` in this build, and the only
module that can move a position anywhere.

Three guarantees are structural rather than procedural:

**It cannot reach production.** The host is checked against an allowlist of one
at construction. Not a denylist -- a denylist lets a hostname we have not
thought of through, and the direction of that mistake is unrecoverable.

**It cannot leak a credential.** The secret is read only inside
``signing.build_signed_query``; this module holds a ``SecretStr`` and passes it
on. Errors raised here name the venue's error code and never the query string,
because a signed query contains the signature and the header contains the key.

**It does not decide anything.** It translates, and refuses what it cannot
translate. Every judgement -- whether an order may be placed, at what leverage,
whether an absent order means it never existed -- belongs to the risk engine
and the reconciliation protocol above it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Any, Final
from urllib.parse import urlsplit

import httpx

from aetheris.adapters.exchange.binance import testnet_endpoints as tn
from aetheris.adapters.exchange.binance.signing import build_signed_query
from aetheris.adapters.exchange.binance.venue_status import parse_order_view
from aetheris.adapters.exchange.errors import (
    ExchangeError,
    ExchangeInvalidResponseError,
    ExchangeRateLimitedError,
    ExchangeTimeoutError,
    ExchangeUnavailableError,
)
from aetheris.core.config import TESTNET_ALLOWED_HOST, TestnetSettings
from aetheris.domain.enums import OrderType, TradingMode
from aetheris.domain.order import OrderRecord, VenueOrderView
from aetheris.domain.venue import LeverageBracket, MarginMode, PositionMode, VenueAccount

__all__ = ["BinanceTestnetTradingAdapter", "HedgeModeNotSupportedError", "MarginModeRefusedError"]

_METHOD_GET: Final = "GET"
_METHOD_POST: Final = "POST"
_METHOD_DELETE: Final = "DELETE"


class HedgeModeNotSupportedError(ExchangeError):
    """The venue account is in hedge mode, which this build refuses.

    Adapting half-way is the danger. In hedge mode every order must name the
    side it belongs to and a close must target the right one; an adapter that
    kept sending ``positionSide=BOTH`` would have its orders rejected at best,
    and at worst would open a position against the one it meant to close.
    """


class MarginModeRefusedError(ExchangeError):
    """The venue would not put this symbol into the required margin mode.

    Raised rather than continued from. Proceeding under CROSSED when ISOLATED
    was required would place an order whose loss is not bounded the way the
    operator asked for it to be bounded.
    """


class BinanceTestnetTradingAdapter:
    """Execution against the Binance USDT-M futures testnet."""

    def __init__(
        self,
        settings: TestnetSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        key, secret = settings.api_key, settings.api_secret
        if key is None or secret is None:
            raise ExchangeError(
                "testnet credentials are not configured; the execution adapter "
                "refuses to start without them"
            )
        host = urlsplit(settings.rest_base_url).hostname or ""
        if host != TESTNET_ALLOWED_HOST:
            # Belt and braces: the settings validator already refuses this.
            # Repeated here because this is the object that would do the
            # damage, and a guard at the point of damage survives refactoring
            # of the thing that was supposed to prevent it.
            raise ExchangeError(
                f"the testnet adapter refuses host {host!r}; only "
                f"{TESTNET_ALLOWED_HOST} is allowlisted"
            )

        self._settings = settings
        self._api_key = key
        self._api_secret = secret
        self._clock_offset_ms = 0
        self._client = httpx.AsyncClient(
            base_url=settings.rest_base_url,
            timeout=httpx.Timeout(
                settings.request_timeout_seconds,
                connect=settings.connect_timeout_seconds,
            ),
            transport=transport,
            headers={"User-Agent": "Aetheris-Pro/0.1 (+testnet-execution)"},
            follow_redirects=False,
        )

    @property
    def venue_mode(self) -> TradingMode:
        return TradingMode.TESTNET

    @property
    def venue_name(self) -> str:
        return tn.VENUE_NAME

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    async def sync_clock(self) -> int:
        """Measure our clock's offset from the venue's, once, before signing.

        Binance rejects a request whose timestamp runs ahead of server time.
        On a machine that is slightly fast every signed call fails with a
        signature-adjacent error, which reads exactly like bad credentials and
        is not. Measuring the offset turns a mystifying failure into arithmetic.
        """
        payload = await self._request(_METHOD_GET, tn.SERVER_TIME, signed=False)
        if not isinstance(payload, dict) or "serverTime" not in payload:
            raise ExchangeInvalidResponseError("venue did not return a server time")
        from aetheris.adapters.exchange.binance.signing import utc_timestamp_ms

        self._clock_offset_ms = int(payload["serverTime"]) - utc_timestamp_ms()
        return self._clock_offset_ms

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        signed: bool = True,
    ) -> Any:
        """One signed request. Never retried when it can place an order.

        A POST to the order endpoint is not idempotent from our side: a timeout
        before and after the venue received it are indistinguishable here, so
        retrying risks a second position. Read calls may be retried; writes may
        not, and that asymmetry is enforced by the caller passing the method.
        """
        attempts = self._settings.max_read_retries + 1 if method == _METHOD_GET else 1

        last: Exception | None = None
        for attempt in range(attempts):
            try:
                if signed:
                    request = build_signed_query(
                        params or {},
                        api_key=self._api_key,
                        api_secret=self._api_secret,
                        recv_window_ms=self._settings.recv_window_ms,
                        offset_ms=self._clock_offset_ms,
                    )
                    response = await self._client.request(
                        method, f"{path}?{request.query}", headers=request.headers
                    )
                else:
                    response = await self._client.request(method, path, params=params)
                return self._decode(response, path)
            except ExchangeRateLimitedError:
                raise
            except (ExchangeTimeoutError, ExchangeUnavailableError) as exc:
                last = exc
                if attempt + 1 >= attempts:
                    raise
                await asyncio.sleep(
                    min(
                        self._settings.backoff_seconds * (2**attempt),
                        self._settings.max_backoff_seconds,
                    )
                )
            except httpx.TimeoutException as exc:
                last = ExchangeTimeoutError(f"{path} timed out")
                if attempt + 1 >= attempts:
                    raise last from exc
            except httpx.HTTPError as exc:
                last = ExchangeUnavailableError(f"{path} could not be reached")
                if attempt + 1 >= attempts:
                    raise last from exc
        raise last or ExchangeUnavailableError(f"{path} failed")

    def _decode(self, response: httpx.Response, path: str) -> Any:
        """Turn a venue response into data or a typed failure.

        The request is never echoed into an error. It carries a signature, and
        the header that accompanied it carries the key.
        """
        if response.status_code == 429:
            raise ExchangeRateLimitedError(f"{path} was rate limited")
        if response.status_code == 418:
            raise ExchangeRateLimitedError(
                f"{path} returned 418: this IP is temporarily banned by the venue"
            )
        try:
            payload = response.json()
        except ValueError:
            raise ExchangeInvalidResponseError(f"{path} returned a non-JSON body") from None

        if isinstance(payload, dict) and "code" in payload and "msg" in payload:
            code = int(payload["code"])
            # Binance answers a *successful* margin-type change with
            # {"code": 200, "msg": "success"}. A shape that usually means
            # failure sometimes means the opposite, so the code decides, not
            # the shape. Treating 200 as a rejection refused every order whose
            # margin mode had actually just been set correctly.
            if code != 200:
                raise VenueRejection(code, str(payload["msg"]), path)
            return payload
        if response.status_code >= 500:
            raise ExchangeUnavailableError(f"{path} returned {response.status_code}")
        if response.status_code >= 400:
            raise ExchangeError(f"{path} returned {response.status_code}")
        return payload

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    async def account_identity(self) -> VenueAccount:
        """Balances from v3, the trading permission from v2.

        Two calls, for a reason worth stating. The current account endpoint
        carries the balances but does not publish ``canTrade`` at all; only the
        v2 one does. Reading the missing key with a ``False`` default turned
        "the venue did not say" into "the venue said no", and refused an
        account that was perfectly able to trade.

        When v2 cannot be reached the permission is ``None`` -- unknown, not
        granted. Callers refuse on anything that is not ``True``, so an
        unreadable permission still fails closed; what it must not do is
        fabricate a denial and report it as the venue's answer.
        """
        payload = await self._request(_METHOD_GET, tn.ACCOUNT)
        if not isinstance(payload, dict):
            raise ExchangeInvalidResponseError("account response was not an object")
        mode = await self.position_mode()
        balance = payload.get("availableBalance")

        can_trade: bool | None = None
        try:
            legacy = await self._request(_METHOD_GET, tn.ACCOUNT_V2)
        except ExchangeError:
            legacy = None
        if isinstance(legacy, dict) and "canTrade" in legacy:
            can_trade = bool(legacy["canTrade"])

        return VenueAccount(
            # The account response carries no stable numeric id on this venue,
            # so the identity recorded is the configuration we act under.
            account_id=str(
                payload.get("accountAlias") or (legacy or {}).get("accountAlias") or "testnet"
            ),
            position_mode=mode,
            can_trade=can_trade,
            available_balance=Decimal(str(balance)) if balance is not None else None,
        )

    async def position_mode(self) -> PositionMode:
        payload = await self._request(_METHOD_GET, tn.POSITION_SIDE_DUAL)
        if not isinstance(payload, dict) or "dualSidePosition" not in payload:
            raise ExchangeInvalidResponseError("venue did not report a position mode")
        dual = payload["dualSidePosition"]
        dual = dual if isinstance(dual, bool) else str(dual).lower() == "true"
        return PositionMode.HEDGE if dual else PositionMode.ONE_WAY

    async def require_one_way_mode(self) -> None:
        """Refuse to operate in hedge mode. Called before anything is placed."""
        mode = await self.position_mode()
        if mode is not PositionMode.ONE_WAY:
            raise HedgeModeNotSupportedError(
                "the venue account is in hedge mode. This build sends "
                "positionSide=BOTH and supports one-way mode only, so it refuses "
                "to trade rather than send orders whose side it cannot guarantee."
            )

    # ------------------------------------------------------------------
    # Leverage
    # ------------------------------------------------------------------

    async def leverage_bracket(self, symbol: str, *, notional: Decimal) -> LeverageBracket:
        """The ceiling for the tier this notional actually falls in.

        Selecting the first bracket would report the headline leverage for a
        tiny position and apply it to a large one. The tier is chosen by the
        order's own notional, which is the only reading of the venue's table
        that is not an over-permission.
        """
        payload = await self._request(_METHOD_GET, tn.LEVERAGE_BRACKET, {"symbol": symbol.upper()})
        entries = payload if isinstance(payload, list) else [payload]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("symbol", "")).upper() != symbol.upper():
                continue
            for bracket in entry.get("brackets", []) or []:
                floor = Decimal(str(bracket.get("notionalFloor", 0)))
                cap_raw = bracket.get("notionalCap")
                cap = Decimal(str(cap_raw)) if cap_raw is not None else None
                candidate = LeverageBracket(
                    symbol=symbol.upper(),
                    max_leverage=Decimal(str(bracket["initialLeverage"])),
                    notional_floor=floor,
                    notional_cap=cap,
                    maint_margin_ratio=(
                        Decimal(str(bracket["maintMarginRatio"]))
                        if bracket.get("maintMarginRatio") is not None
                        else None
                    ),
                )
                if candidate.covers(notional):
                    return candidate
        raise ExchangeInvalidResponseError(
            f"the venue published no leverage bracket covering a notional of {notional} "
            f"for {symbol.upper()}"
        )

    async def set_leverage(self, symbol: str, leverage: Decimal) -> Decimal:
        payload = await self._request(
            _METHOD_POST,
            tn.LEVERAGE,
            {"symbol": symbol.upper(), "leverage": int(leverage)},
        )
        if not isinstance(payload, dict) or "leverage" not in payload:
            raise ExchangeInvalidResponseError("venue did not echo the applied leverage")
        return Decimal(str(payload["leverage"]))

    async def session_realized_pnl(self, *, since: datetime) -> Decimal:
        """Realised PnL booked at the venue since a point in time.

        Read, never assumed. The risk engine judges the daily loss limit
        against this number; supplying a zero because it was inconvenient to
        fetch would disable that limit while leaving it looking enforced --
        exactly the failure phase 8a found in the hardcoded unreconciled count.
        A failure here propagates and the caller refuses the order.
        """
        payload = await self._request(
            _METHOD_GET,
            tn.INCOME,
            {
                "incomeType": tn.INCOME_TYPE_REALIZED_PNL,
                "startTime": int(since.timestamp() * 1000),
                "limit": 1000,
            },
        )
        if not isinstance(payload, list):
            raise ExchangeInvalidResponseError("income response was not a list")
        total = Decimal(0)
        for entry in payload:
            if isinstance(entry, dict) and entry.get("income") is not None:
                total += Decimal(str(entry["income"]))
        return total

    # ------------------------------------------------------------------
    # Margin mode
    # ------------------------------------------------------------------

    async def margin_mode(self, symbol: str) -> MarginMode:
        """Read a symbol's margin mode, whether or not a position is open.

        The current position-risk endpoint returns only symbols the account
        actually has a position in, so on a flat account it returns an empty
        list -- and an empty list is not a margin mode. That silence arrives
        moment: the pre-trade check runs before the *first* order on a symbol,
        which is precisely when there is nothing to report. v2 answers for any
        symbol, so it is the fallback.

        Still raises when neither version gives an authoritative value. A
        margin mode that was not read is never assumed, because the caller uses
        this to decide whether an order may be placed under ISOLATED.
        """
        for path in (tn.POSITION_RISK, tn.POSITION_RISK_V2):
            try:
                payload = await self._request(_METHOD_GET, path, {"symbol": symbol.upper()})
            except ExchangeError:
                continue
            entries = payload if isinstance(payload, list) else [payload]
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("symbol", "")).upper() != symbol.upper():
                    continue
                raw = str(entry.get("marginType", "")).upper()
                if raw in ("ISOLATED", "CROSSED", "CROSS"):
                    return MarginMode.ISOLATED if raw == "ISOLATED" else MarginMode.CROSSED
        raise ExchangeInvalidResponseError(
            f"neither positionRisk version reported a margin type for {symbol}; it is not assumed"
        )

    async def set_margin_mode(self, symbol: str, mode: MarginMode) -> None:
        """Request a margin mode, treating "already set" as success.

        The venue answers ``-4046 No need to change margin type`` when the
        symbol is already in the requested mode. That is the goal state, not a
        failure, and treating it as one would refuse every order after the
        first. Every other refusal stays a refusal: a position or open order
        can make the change impossible, and this build will not close anything
        to get its way.
        """
        try:
            await self._request(
                _METHOD_POST,
                tn.MARGIN_TYPE,
                {"symbol": symbol.upper(), "marginType": mode.value},
            )
        except VenueRejection as exc:
            if exc.venue_code == tn.ERROR_NO_NEED_TO_CHANGE_MARGIN_TYPE:
                return
            raise MarginModeRefusedError(
                f"the venue refused to set {symbol.upper()} to {mode.value} "
                f"(code {exc.venue_code}). No position is closed to force it."
            ) from None

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    async def submit(self, record: OrderRecord) -> VenueOrderView:
        """Place the order this record already describes.

        Takes the record rather than loose arguments so there is no way to
        submit something that was never written down. The identity sent to the
        venue is the record's own ``client_order_id``, which makes the venue's
        uniqueness rule and ours the same rule.
        """
        intent = record.intent
        params: dict[str, Any] = {
            "symbol": intent.symbol.upper(),
            "side": intent.side.value,
            "type": intent.order_type.value,
            "quantity": format(intent.quantity.normalize(), "f"),
            "positionSide": tn.POSITION_SIDE_ONE_WAY,
            "newClientOrderId": record.client_order_id,
        }
        if intent.order_type is OrderType.LIMIT:
            if intent.price is None:
                raise ExchangeError("a limit order requires a price")
            params["price"] = format(intent.price.normalize(), "f")
            params["timeInForce"] = "GTC"
        if intent.reduce_only:
            params["reduceOnly"] = "true"

        payload = await self._request(_METHOD_POST, tn.ORDER, params)
        if not isinstance(payload, dict):
            raise ExchangeInvalidResponseError("order response was not an object")
        return parse_order_view(payload)

    async def query(self, *, symbol: str, client_order_id: str) -> VenueOrderView | None:
        """Ask about one order. ``None`` means the venue has no such order.

        Deliberately not "the order does not exist". Only the caller knows
        whether it was ever sent, and absence is proof of nothing without that.
        """
        try:
            payload = await self._request(
                _METHOD_GET,
                tn.ORDER,
                {"symbol": symbol.upper(), "origClientOrderId": client_order_id},
            )
        except VenueRejection as exc:
            if exc.venue_code == tn.ERROR_ORDER_DOES_NOT_EXIST:
                return None
            raise
        if not isinstance(payload, dict):
            raise ExchangeInvalidResponseError("order query response was not an object")
        return parse_order_view(payload)

    async def cancel(self, *, symbol: str, client_order_id: str) -> VenueOrderView:
        payload = await self._request(
            _METHOD_DELETE,
            tn.ORDER,
            {"symbol": symbol.upper(), "origClientOrderId": client_order_id},
        )
        if not isinstance(payload, dict):
            raise ExchangeInvalidResponseError("cancel response was not an object")
        return parse_order_view(payload)

    async def open_orders(self, *, symbol: str | None = None) -> tuple[VenueOrderView, ...]:
        params = {"symbol": symbol.upper()} if symbol else {}
        payload = await self._request(_METHOD_GET, tn.OPEN_ORDERS, params)
        if not isinstance(payload, list):
            raise ExchangeInvalidResponseError("open orders response was not a list")
        return tuple(parse_order_view(entry) for entry in payload if isinstance(entry, dict))


class VenueRejection(ExchangeError):
    """The venue answered with an error code.

    Carries the code so callers can branch on it by name rather than by
    matching a message, and carries the path so a log line is useful. It never
    carries the request: that would mean logging a signature.
    """

    def __init__(self, venue_code: int, message: str, path: str) -> None:
        super().__init__(f"{path} was refused by the venue (code {venue_code}): {message}")
        self.venue_code = venue_code
        self.path = path
