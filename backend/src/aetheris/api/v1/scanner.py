"""Read-only market scanner endpoints.

Every parameter is bounded. An unbounded scanner is not a slow endpoint, it is
a way to have the deployment's IP banned by the venue and to exhaust an 8 GB
machine, so page size, page number, candle count and the metric pool all have
hard ceilings enforced here rather than trusted from the caller.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from aetheris.analysis import scoring
from aetheris.analysis.metrics import (
    DEFAULT_MOMENTUM_LOOKBACK,
    MIN_CANDLES_FOR_METRICS,
    SIDEWAYS_THRESHOLD_PERCENT,
)
from aetheris.api.deps import ScannerDep
from aetheris.domain.enums import Timeframe
from aetheris.domain.scanner import (
    ScannerPage,
    ScannerSortField,
    SortDirection,
    TrendDirection,
)
from aetheris.services.scanner import METRIC_SORT_FIELDS, ScanFilters, ScanQuery

router = APIRouter(prefix="/scanner", tags=["scanner"])

#: Bounds the offset arithmetic. Deep paging past this is not a use case the
#: scanner serves; the filters are the way to narrow a result.
MAX_PAGE = 200


class ScoringWeight(BaseModel):
    component: str
    weight: Decimal


class ScoringMethodResponse(BaseModel):
    """Full disclosure of how the opportunity score is produced."""

    method: str
    disclaimer: str
    weights: list[ScoringWeight]
    min_candles: int
    momentum_lookback: int
    sideways_threshold_percent: Decimal
    metric_sort_fields: list[str]
    notes: list[str]


@router.get(
    "",
    response_model=ScannerPage,
    summary="Scan the dynamically discovered perpetual universe",
)
async def scan(
    service: ScannerDep,
    search: Annotated[
        str | None,
        Query(max_length=32, description="Match against symbol or base asset"),
    ] = None,
    sort: Annotated[
        ScannerSortField, Query(description="Field to order by")
    ] = ScannerSortField.QUOTE_VOLUME_24H,
    direction: Annotated[SortDirection, Query()] = SortDirection.DESC,
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
    timeframe: Annotated[
        Timeframe, Query(description="Timeframe for candle-derived metrics")
    ] = Timeframe.H1,
    include_metrics: Annotated[
        bool,
        Query(
            description=(
                "Compute candle metrics for the returned rows. Costs one upstream "
                "request per row, so it is off by default."
            )
        ),
    ] = False,
    include_setup: Annotated[
        bool,
        Query(
            description=(
                "Score each row against the strategy rule set. Needs a longer "
                "candle window than metrics do, so it reaches a smaller pool and "
                "is off by default. The score is strategy alignment, NOT a "
                "probability of profit."
            )
        ),
    ] = False,
    quote_asset: Annotated[str | None, Query(max_length=16, pattern=r"^[A-Za-z0-9]+$")] = None,
    min_quote_volume: Annotated[Decimal | None, Query(ge=0)] = None,
    min_price_change_percent: Annotated[Decimal | None, Query(ge=-100, le=10_000)] = None,
    max_price_change_percent: Annotated[Decimal | None, Query(ge=-100, le=10_000)] = None,
    min_volatility_percent: Annotated[Decimal | None, Query(ge=0, le=1_000)] = None,
    min_relative_volume: Annotated[Decimal | None, Query(ge=0, le=1_000)] = None,
    trend: Annotated[TrendDirection | None, Query()] = None,
) -> ScannerPage:
    """Return one page of scanner rows.

    **Ranking scope.** Ordering by a ticker field ranks the entire eligible
    universe from a single whole-market request (`FULL_UNIVERSE`). Ordering by
    or filtering on a candle-derived field cannot do that without a request per
    instrument, so the scanner computes metrics for the most liquid N and ranks
    within that pool (`LIQUIDITY_POOL`). The response reports which happened
    and how large the pool was — a subset is never presented as the whole
    market.

    Asking for setup scores narrows the scope further (`STRATEGY_POOL`), because
    every indicator has to warm up before a rule set can be evaluated at all.
    The page reports that pool size separately.

    **The two scores are different things and are never merged.** The Market
    Opportunity Score ranks how unusually active an instrument is right now.
    The Strategy Setup Score measures how completely one strategy's published
    rules are currently satisfied and whether the trade is worth its own risk.
    Neither is a probability of profit, a win rate or an expected return, and
    no ordering here implies one instrument will outperform another.

    **Unavailable values never become zero.** A row whose sort value is unknown
    sorts last in both directions and is ordered among its peers by symbol.
    """
    query = ScanQuery(
        sort_by=sort.value,
        descending=direction is SortDirection.DESC,
        page=page,
        page_size=page_size,
        timeframe=timeframe,
        include_metrics=include_metrics,
        include_setup=include_setup,
        filters=ScanFilters(
            search=search,
            quote_asset=quote_asset,
            min_quote_volume=min_quote_volume,
            min_price_change_percent=min_price_change_percent,
            max_price_change_percent=max_price_change_percent,
            min_volatility_percent=min_volatility_percent,
            min_relative_volume=min_relative_volume,
            trend=trend,
        ),
    )
    return await service.scan(query)


@router.get(
    "/scoring-method",
    response_model=ScoringMethodResponse,
    summary="How the Market Opportunity Score is calculated",
)
async def scoring_method() -> ScoringMethodResponse:
    """Publish the scoring formula rather than hiding it.

    A ranking number a user cannot interrogate invites them to read meaning
    into it that is not there. This endpoint exists so the UI can always link
    "what does this score mean?" to the actual arithmetic.
    """
    return ScoringMethodResponse(
        method=scoring.SCORING_METHOD,
        disclaimer=(
            "The Market Opportunity Score is a deterministic market-analysis and "
            "ranking metric. It is NOT a probability of profit, an expected return, "
            "a win rate, a trading signal, a confidence value, or a prediction of "
            "future price. It is a weighted sum of four present-tense measurements "
            "used to order a table."
        ),
        weights=[
            ScoringWeight(component=name, weight=weight) for name, weight in scoring.WEIGHTS.items()
        ],
        min_candles=MIN_CANDLES_FOR_METRICS,
        momentum_lookback=DEFAULT_MOMENTUM_LOOKBACK,
        sideways_threshold_percent=SIDEWAYS_THRESHOLD_PERCENT,
        metric_sort_fields=sorted(METRIC_SORT_FIELDS),
        notes=[
            "Only closed candles are used; a still-forming bar is discarded before "
            "any calculation, because its volume and range are partial.",
            f"At least {MIN_CANDLES_FOR_METRICS} closed candles are required. Below "
            "that the metrics are reported INSUFFICIENT_DATA rather than estimated.",
            "Momentum contributes as a magnitude: a sharp fall counts as much as a "
            "sharp rise. Direction is reported separately as the trend field.",
            "A score is withheld entirely when relative volume is unavailable, "
            "because a score missing its largest component is not comparable with "
            "the others.",
        ],
    )
