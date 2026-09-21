/**
 * Mapping backend indicator series onto the chart's candle axis.
 *
 * The backend returns each indicator point stamped with the candle's open
 * time, not an array index. Aligning by timestamp rather than position is what
 * keeps an overlay correct when the two requests see a slightly different
 * number of bars — which happens routinely, because a bar closes between the
 * candle fetch and the indicator fetch.
 *
 * A bar with no indicator value (warm-up, or undefined at that bar) maps to
 * `null` and the line is simply not drawn there. Nothing is interpolated
 * across a gap: a drawn line implies a computed value.
 */

import type { Candle, IndicatorResult } from "./types";

/** Distinct, colour-blind-safe hues for indicator lines on the dark theme. */
export const LINE_COLOURS: Record<string, string> = {
  "ema:ema": "#4b8bf4",
  "sma:sma": "#d9a441",
  "bollinger:upper": "#8d7bd4",
  "bollinger:middle": "#6f7d93",
  "bollinger:lower": "#8d7bd4",
  "vwap:vwap": "#3aa8a0",
  "rsi:rsi": "#4b8bf4",
  "macd:macd": "#4b8bf4",
  "macd:signal": "#d9a441",
  "macd:histogram": "#6f7d93",
  "stochastic:k": "#4b8bf4",
  "stochastic:d": "#d9a441",
  "adx:adx": "#4b8bf4",
  "adx:plus_di": "#26a96a",
  "adx:minus_di": "#e0554e",
  "atr:atr": "#d9a441",
  "roc:roc": "#4b8bf4",
  "cci:cci": "#4b8bf4",
};

export function lineColour(indicator: string, valueKey: string): string {
  return LINE_COLOURS[`${indicator}:${valueKey}`] ?? "#93a0b4";
}

export interface AlignedLine {
  indicator: string;
  valueKey: string;
  label: string;
  colour: string;
  /** One entry per candle; null where the indicator has no value. */
  values: (number | null)[];
}

function toNumberOrNull(raw: string | null | undefined): number | null {
  if (raw === null || raw === undefined) return null;
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? parsed : null;
}

/**
 * Align every line of one indicator result to the candle array.
 *
 * Returns an empty list when the indicator carries no series, so a caller that
 * forgot to request `series_points` draws nothing rather than a flat line at
 * zero.
 */
export function alignIndicator(
  candles: readonly Candle[],
  result: IndicatorResult,
): AlignedLine[] {
  if (result.series.length === 0) return [];

  const byTime = new Map<string, Record<string, string | null>>();
  for (const point of result.series) byTime.set(point.time, point.values);

  return result.value_keys.map((valueKey) => ({
    indicator: result.indicator,
    valueKey,
    label: result.value_keys.length > 1 ? `${result.name} ${valueKey}` : result.name,
    colour: lineColour(result.indicator, valueKey),
    values: candles.map((candle) => toNumberOrNull(byTime.get(candle.open_time)?.[valueKey])),
  }));
}

/** Format an indicator's latest values for a compact readout. */
export function formatLatest(result: IndicatorResult): string {
  if (result.latest === null) return "";
  return result.value_keys
    .map((key) => {
      const value = toNumberOrNull(result.latest?.[key]);
      if (value === null) return `${key} —`;
      const decimals = Math.abs(value) >= 100 ? 2 : 4;
      return result.value_keys.length > 1
        ? `${key} ${value.toFixed(decimals)}`
        : value.toFixed(decimals);
    })
    .join("  ");
}
