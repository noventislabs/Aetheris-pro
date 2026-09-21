"""Scanner domain models.

A scanner row aggregates several independently-sourced facts about one
instrument: venue metadata, a ticker snapshot, and metrics derived from
candles. Each of those can be present, absent or unusable *independently*, so
each carries its own status rather than the row having one overall "ok" flag.

The Phase 0 rule still governs every field: an unavailable number is ``None``
with a status explaining why. Nothing is ever defaulted to zero to make a row
look complete.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from aetheris.core.freshness import DataStatus
from aetheris.domain.enums import ContractType, SymbolStatus, Timeframe
from aetheris.domain.setup import TradeSetup


class MetricStatus(StrEnum):
    """Why a candle-derived metric block is or is not present."""

    CALCULATED = "CALCULATED"
    #: Fewer usable closed candles than the calculation requires.
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    #: Candles could not be retrieved, or arrived stale/unusable.
    UNAVAILABLE = "UNAVAILABLE"
    #: The caller did not ask for metrics, so none were computed.
    NOT_REQUESTED = "NOT_REQUESTED"

    @property
    def has_values(self) -> bool:
        return self is MetricStatus.CALCULATED


class TrendDirection(StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    SIDEWAYS = "SIDEWAYS"
    #: Not enough data to judge. Distinct from SIDEWAYS, which is a finding.
    UNKNOWN = "UNKNOWN"


class ScannerMetrics(BaseModel):
    """Deterministic statistics computed from closed candles.

    Every value here is arithmetic over real OHLCV bars. Nothing is estimated,
    smoothed against an assumption, or predicted.
    """

    model_config = ConfigDict(frozen=True)

    timeframe: Timeframe
    candles_used: int = Field(ge=0, description="Closed candles the maths ran over")
    window_return_percent: Decimal = Field(description="Close-to-close change across the window")
    momentum_percent: Decimal = Field(description="Close-to-close change over the recent lookback")
    volatility_percent: Decimal = Field(
        description="Population standard deviation of per-candle percent returns"
    )
    atr: Decimal = Field(ge=0, description="Mean true range over the window")
    atr_percent: Decimal = Field(ge=0, description="ATR as a percentage of the last close")
    average_volume: Decimal = Field(ge=0)
    last_volume: Decimal = Field(ge=0)
    relative_volume: Decimal | None = Field(
        default=None,
        ge=0,
        description="Last closed candle's volume over the mean of the preceding ones",
    )
    range_percent: Decimal = Field(ge=0, description="Window high-to-low range over the low")
    body_percent: Decimal = Field(
        ge=0, description="Mean candle body as a percentage of mean candle range"
    )
    trend: TrendDirection
    trend_consistency: Decimal = Field(
        ge=0, le=1, description="Fraction of candles closing with the net direction"
    )


class ScoreComponent(BaseModel):
    """One measurable factor's contribution to the opportunity score.

    Carried on every score so the number is explainable rather than oracular:
    a reader can see which factor produced it and what the raw measurement was.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    raw_value: Decimal = Field(description="The measurement, in its own units")
    normalized: Decimal = Field(ge=0, le=1, description="Clamped to 0-1 against a reference")
    weight: Decimal = Field(ge=0, le=1)
    contribution: Decimal = Field(ge=0, description="normalized x weight x 100")
    detail: str


class OpportunityScore(BaseModel):
    """Deterministic ranking metric over currently measurable market activity.

    **This is not a probability of profit, an expected return, a win rate, a
    prediction of future price, or any kind of confidence.** It is a weighted
    sum of four present-tense measurements -- relative volume, volatility,
    momentum and trend consistency -- used to order a table. A high score means
    "this instrument is unusually active right now", nothing more.
    """

    model_config = ConfigDict(frozen=True)

    score: Decimal = Field(ge=0, le=100)
    components: tuple[ScoreComponent, ...]
    method: str = Field(description="Identifier of the scoring formula version")


class ScannerRow(BaseModel):
    """One instrument as the scanner sees it."""

    model_config = ConfigDict(frozen=True)

    # --- venue metadata (always present: it comes from the universe) -------
    symbol: str
    base_asset: str
    quote_asset: str
    contract_type: ContractType
    status: SymbolStatus

    # --- ticker snapshot --------------------------------------------------
    ticker_status: DataStatus
    ticker_detail: str | None = None
    last_price: Decimal | None = None
    bid_price: Decimal | None = None
    ask_price: Decimal | None = None
    price_change_24h: Decimal | None = None
    price_change_percent_24h: Decimal | None = None
    high_24h: Decimal | None = None
    low_24h: Decimal | None = None
    volume_24h: Decimal | None = None
    quote_volume_24h: Decimal | None = None

    # --- candle-derived ---------------------------------------------------
    metrics_status: MetricStatus = MetricStatus.NOT_REQUESTED
    metrics_detail: str | None = None
    metrics: ScannerMetrics | None = None
    opportunity: OpportunityScore | None = None

    #: Strategy setup scoring, only when the scan asked for it. Separate from
    #: ``opportunity`` on purpose: the two answer different questions and must
    #: not be conflated. The opportunity score ranks how unusually active an
    #: instrument is; a setup score measures how completely one strategy's
    #: published rules are currently satisfied. Neither is a probability.
    setup_status: MetricStatus = MetricStatus.NOT_REQUESTED
    setup_detail: str | None = None
    setup: TradeSetup | None = None


class RankingScope(StrEnum):
    """How far the ordering actually reaches.

    Sorting by a ticker field can rank the whole universe from one bulk
    request. Sorting by a candle-derived field cannot: it would need candles
    for every instrument. Rather than silently ranking a subset and presenting
    it as the whole market, the scanner says which it did.
    """

    FULL_UNIVERSE = "FULL_UNIVERSE"
    #: Candle metrics were computed for the most liquid N instruments only,
    #: and the ordering covers that pool.
    LIQUIDITY_POOL = "LIQUIDITY_POOL"
    #: Setup scoring reaches fewer instruments still -- it needs enough bars
    #: for every indicator to warm up, which is several times the metric
    #: window. The ordering covers only the instruments actually scored.
    STRATEGY_POOL = "STRATEGY_POOL"


class ScannerPage(BaseModel):
    """A page of scanner rows plus the provenance of the scan itself."""

    model_config = ConfigDict(frozen=True)

    rows: tuple[ScannerRow, ...]
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    total_rows: int = Field(ge=0, description="Rows matching the filters, before paging")
    universe_size: int = Field(ge=0, description="Eligible instruments discovered")
    ranking_scope: RankingScope
    candidate_pool_size: int | None = Field(
        default=None, description="Instruments given candle metrics, when scope is LIQUIDITY_POOL"
    )
    setup_pool_size: int | None = Field(
        default=None,
        description="Instruments given a strategy setup score, when one was requested",
    )
    sort_by: str
    direction: str
    ticker_source: str
    ticker_status: DataStatus
    ticker_event_ts: str | None = None
    ticker_age_seconds: float | None = None
    scanned_at: str


class ScannerSortField(StrEnum):
    """Fields a scanner page may be ordered by.

    An enum rather than a free string so an unsupported field is a 422 before
    any work happens, and so the set of orderings is discoverable from the
    OpenAPI document. Note what is absent: there is no sort by confidence,
    prediction or signal strength, because no such quantity exists here.
    """

    SYMBOL = "symbol"
    LAST_PRICE = "last_price"
    PRICE_CHANGE_PERCENT_24H = "price_change_percent_24h"
    VOLUME_24H = "volume_24h"
    QUOTE_VOLUME_24H = "quote_volume_24h"
    VOLATILITY_PERCENT = "volatility_percent"
    MOMENTUM_PERCENT = "momentum_percent"
    ATR_PERCENT = "atr_percent"
    RELATIVE_VOLUME = "relative_volume"
    TREND_CONSISTENCY = "trend_consistency"
    OPPORTUNITY_SCORE = "opportunity_score"
    #: Strategy alignment, not a probability of profit. Ordering by it ranks
    #: only the instruments that were actually scored, and the page says how
    #: many that was.
    SETUP_SCORE = "setup_score"


class SortDirection(StrEnum):
    ASC = "asc"
    DESC = "desc"
