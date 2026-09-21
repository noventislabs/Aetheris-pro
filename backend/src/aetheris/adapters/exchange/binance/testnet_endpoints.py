"""Binance USDT-M Futures **testnet** paths.

Separate from ``endpoints.py`` on purpose. That module holds public market-data
paths that need no credentials and can place nothing; these paths can move
positions. Keeping them in different files means the architecture test can say
"an order path may appear only here", which is a far stronger statement than
"an order path may appear somewhere in the Binance package".

Every path below was probed against ``demo-fapi.binance.com`` during the phase
8b audit and answered ``-2014 API-key format invalid`` -- that is, it exists
and demands a signed request.
"""

from __future__ import annotations

from typing import Final

#: Venue identity for provenance. Distinct from the market-data label so a
#: record can never be read as having come from the public feed.
VENUE_NAME: Final = "binance-futures-usdm-testnet"
SOURCE_REST: Final = f"{VENUE_NAME}:rest"

#: Server time. The one unsigned path here, used to measure clock drift before
#: any signed call is attempted.
SERVER_TIME: Final = "/fapi/v1/time"

#: Order lifecycle. POST creates, GET queries, DELETE cancels.
ORDER: Final = "/fapi/v1/order"

#: Open orders for the account or one symbol.
#:
#: **Not** ``/fapi/v1/allOpenOrders``. That path is DELETE-only -- it cancels
#: every open order -- and a GET against it returns ``-5000 Method GET is
#: invalid``, confirmed by probe. The published endpoint table lists it as a
#: readable GET, which is the sort of documentation error that is discovered in
#: production. This project uses the path that actually reads.
OPEN_ORDERS: Final = "/fapi/v1/openOrders"

#: Notional-tiered leverage brackets. Signed: the exchange ceiling is not
#: available to an unauthenticated caller, which is precisely why
#: ``exchange_max_leverage`` has been null until now.
LEVERAGE_BRACKET: Final = "/fapi/v1/leverageBracket"

#: Set initial leverage for a symbol. Echoes the applied value, which is what
#: makes verification possible rather than assumed.
LEVERAGE: Final = "/fapi/v1/leverage"

#: Set margin type, ISOLATED or CROSSED.
MARGIN_TYPE: Final = "/fapi/v1/marginType"

#: Position mode. ``dualSidePosition`` true means hedge mode, which phase 8b
#: refuses rather than adapts to.
POSITION_SIDE_DUAL: Final = "/fapi/v1/positionSide/dual"

#: Account state. v3 is current and carries the balances.
ACCOUNT: Final = "/fapi/v3/account"

#: The **only** version that publishes ``canTrade``. v3 dropped the permission
#: flags, and reading an absent key as False turned "the venue did not say"
#: into "the venue said no" -- which refused a perfectly good account. Kept as
#: a separate constant so the reason for two account calls is visible.
ACCOUNT_V2: Final = "/fapi/v2/account"

#: Realised PnL since a point in time. The risk engine's daily loss limit is
#: evaluated against session realised PnL, and there is no honest way to supply
#: that number without reading it: defaulting it to zero would silently disable
#: the daily limit, which is the same failure as phase 8a's hardcoded
#: unreconciled count.
INCOME: Final = "/fapi/v1/income"
INCOME_TYPE_REALIZED_PNL: Final = "REALIZED_PNL"

#: Per-position risk. v3 returns ONLY symbols with an open position, so on a
#: flat account it reports nothing at all -- and "no row" is not a margin mode.
POSITION_RISK: Final = "/fapi/v3/positionRisk"

#: v2 reports a symbol's margin type whether or not a position exists, which is
#: what a pre-trade check needs: the first order on a symbol is placed when the
#: account is flat, which is exactly when v3 is silent.
POSITION_RISK_V2: Final = "/fapi/v2/positionRisk"

#: One-way mode: every order carries this. Hedge mode would require LONG/SHORT
#: and is out of scope.
POSITION_SIDE_ONE_WAY: Final = "BOTH"

#: Binance error codes this project reacts to by name rather than by message.
ERROR_ORDER_DOES_NOT_EXIST: Final = -2013
ERROR_INVALID_API_KEY: Final = -2014
ERROR_SIGNATURE_INVALID: Final = -1022
ERROR_TIMESTAMP_AHEAD: Final = -1021
ERROR_NO_NEED_TO_CHANGE_MARGIN_TYPE: Final = -4046
ERROR_MARGIN_TYPE_CHANGE_REJECTED: Final = -4048
ERROR_DUPLICATE_CLIENT_ORDER_ID: Final = -4015
