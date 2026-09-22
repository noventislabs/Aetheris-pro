"""Measured facts about an open position.

Pure. A position, a mark and a fee rate in; a ``PositionMetrics`` out. No
I/O, no clock of its own, no candle series -- which is what makes it
impossible for anything here to reach forward in time.

## Excursions are observed, never reconstructed

MFE and MAE come from ``best_price`` and ``worst_price``, which the engine
widens from real marks while the position is open. They are deliberately
*not* recomputed from a candle series: a high that printed while the position
was open is an excursion the position actually lived through, and the same
high pulled from history afterwards is hindsight. The two are numerically
identical often enough that confusing them is easy, and the difference is the
whole point.

A consequence worth stating: excursion is only as complete as the tick
cadence. A spike between two polls was never observed and is therefore not in
these numbers. That is an understatement of the true extreme, which is the
safe direction, and it is why the values are labelled observed rather than
maximum.

## Absent is absent

Every field is ``None`` when it could not be computed, and the name is listed
in ``unavailable``. A remaining-R of zero means the mark is sitting on the
stop; it never means nobody knew.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Final

from aetheris.core.money import ZERO, quantize_usdt
from aetheris.domain.enums import PositionSide
from aetheris.domain.intelligence import PositionMetrics
from aetheris.domain.paper import PaperPosition

__all__ = ["compute_position_metrics"]

_HUNDRED: Final = Decimal(100)
_BPS: Final = Decimal(10_000)
_RATIO: Final = Decimal("0.0001")
_PCT: Final = Decimal("0.0001")


def _pct(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator <= ZERO:
        return None
    return (numerator / denominator * _HUNDRED).quantize(_PCT)


def _break_even(position: PaperPosition, *, taker_fee_bps: Decimal) -> Decimal | None:
    """The price at which closing now nets exactly zero, after both fees.

    Solved rather than approximated as "entry plus two fees", because the exit
    fee is charged on the *exit* notional, which depends on the very price
    being solved for. For a long:

        (P - entry) x qty - entry_fee - P x qty x f = 0
        P = (entry x qty + entry_fee) / (qty x (1 - f))

    and the short case mirrors it with the signs reversed. The entry fee is
    taken from the position rather than recomputed, so this matches what was
    actually charged.
    """
    quantity = position.quantity
    if quantity <= ZERO:
        return None
    fee_rate = taker_fee_bps / _BPS
    entry_cost = position.entry_price * quantity

    if position.side is PositionSide.LONG:
        denominator = quantity * (Decimal(1) - fee_rate)
        if denominator <= ZERO:
            return None
        price = (entry_cost + position.entry_fee) / denominator
    else:
        denominator = quantity * (Decimal(1) + fee_rate)
        if denominator <= ZERO:
            return None
        price = (entry_cost - position.entry_fee) / denominator

    return price if price > ZERO else None


def compute_position_metrics(
    position: PaperPosition,
    *,
    now: datetime,
    taker_fee_bps: Decimal,
) -> PositionMetrics:
    """Derive every measurable fact about one open position.

    The mark is taken from the position itself, which the engine sets to
    ``None`` whenever market data was unusable at the last poll. That is why
    an unmarked position yields distances of ``None`` rather than distances
    measured against a stale price.
    """
    unavailable: list[str] = []
    is_long = position.side is PositionSide.LONG
    entry = position.entry_price
    mark = position.mark_price

    elapsed = Decimal(str(max((now - position.opened_at).total_seconds(), 0.0)))

    # --- excursions, from observed extremes only ----------------------
    mfe_unit: Decimal | None = None
    mae_unit: Decimal | None = None
    if position.best_price is not None:
        favourable = position.best_price - entry if is_long else entry - position.best_price
        mfe_unit = favourable if favourable > ZERO else ZERO
    else:
        unavailable.append("mfe")

    if position.worst_price is not None:
        adverse = entry - position.worst_price if is_long else position.worst_price - entry
        mae_unit = adverse if adverse > ZERO else ZERO
    else:
        unavailable.append("mae")

    # --- break-even ---------------------------------------------------
    break_even = _break_even(position, taker_fee_bps=taker_fee_bps)
    if break_even is None:
        unavailable.append("break_even_price")

    # --- distances, which all need a usable mark ----------------------
    stop_distance: Decimal | None = None
    target_distance: Decimal | None = None
    liquidation_distance: Decimal | None = None
    r_now: Decimal | None = None
    remaining_r: Decimal | None = None

    if mark is None:
        unavailable.extend(
            [
                "stop_distance_percent",
                "target_distance_percent",
                "liquidation_distance_percent",
                "r_multiple_now",
                "remaining_r",
            ]
        )
    else:
        if position.stop_price is not None:
            gap = mark - position.stop_price if is_long else position.stop_price - mark
            stop_distance = _pct(gap, mark)
        else:
            unavailable.append("stop_distance_percent")

        if position.target_price is not None:
            gap = position.target_price - mark if is_long else mark - position.target_price
            target_distance = _pct(gap, mark)
        else:
            unavailable.append("target_distance_percent")

        if position.liquidation_price is not None:
            gap = (
                mark - position.liquidation_price if is_long else position.liquidation_price - mark
            )
            liquidation_distance = _pct(gap, mark)
        else:
            # Paper computes one; a venue position carries none, and inventing
            # a maintenance-margin model for it would be a fabrication.
            unavailable.append("liquidation_distance_percent")

        # --- R, which needs the risk planned at entry -----------------
        planned = position.thesis.planned_risk_per_unit if position.thesis else None
        if planned is None or planned <= ZERO:
            unavailable.extend(["r_multiple_now", "remaining_r"])
        else:
            move = mark - entry if is_long else entry - mark
            r_now = (move / planned).quantize(_RATIO)
            if position.stop_price is not None:
                gap = mark - position.stop_price if is_long else position.stop_price - mark
                remaining_r = (gap / planned).quantize(_RATIO)
            else:
                unavailable.append("remaining_r")

    if position.unrealized_pnl is None:
        unavailable.append("unrealized_pnl")

    return PositionMetrics(
        time_in_trade_seconds=elapsed,
        best_price=position.best_price,
        worst_price=position.worst_price,
        mfe_per_unit=mfe_unit,
        mae_per_unit=mae_unit,
        mfe_percent=_pct(mfe_unit, entry) if mfe_unit is not None else None,
        mae_percent=_pct(mae_unit, entry) if mae_unit is not None else None,
        break_even_price=break_even,
        stop_distance_percent=stop_distance,
        target_distance_percent=target_distance,
        liquidation_distance_percent=liquidation_distance,
        r_multiple_now=r_now,
        remaining_r=remaining_r,
        unrealized_pnl=(
            quantize_usdt(position.unrealized_pnl) if position.unrealized_pnl is not None else None
        ),
        unavailable=tuple(dict.fromkeys(unavailable)),
    )
