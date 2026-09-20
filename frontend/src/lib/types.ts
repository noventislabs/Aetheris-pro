/**
 * Types mirroring the Aetheris backend API.
 *
 * Monetary values arrive as strings, not numbers, because the backend accounts
 * in Decimal and JSON numbers are IEEE-754 doubles. Parsing them to `number`
 * for display is fine; doing arithmetic on them is not, which is why they stay
 * typed as `string` here rather than being silently widened.
 */

export type DataStatus = "OK" | "STALE" | "UNAVAILABLE" | "ERROR";

export type MetricStatus =
  | "CALCULATED"
  | "INSUFFICIENT_DATA"
  | "UNAVAILABLE"
  | "NOT_REQUESTED";

export type TrendDirection = "UP" | "DOWN" | "SIDEWAYS" | "UNKNOWN";

export type ConnectionStatus = "CONNECTED" | "DEGRADED" | "UNAVAILABLE" | "UNKNOWN";

export type Timeframe = "1m" | "5m" | "15m" | "1h" | "4h" | "1d";

export const TIMEFRAMES: readonly Timeframe[] = ["1m", "5m", "15m", "1h", "4h", "1d"];

/**
 * The backend's provenance envelope.
 *
 * `value` is populated only when `status === "OK"`. The invariant is enforced
 * server-side; the UI must still respect it rather than reading `value`
 * regardless of status.
 */
export interface ObservationEnvelope<T> {
  status: DataStatus;
  source: string;
  event_ts: string | null;
  received_ts: string;
  age_seconds: number | null;
  detail: string | null;
  value: T | null;
}

export interface Ticker {
  symbol: string;
  last_price: string;
  bid_price: string | null;
  ask_price: string | null;
  high_24h: string | null;
  low_24h: string | null;
  open_24h: string | null;
  volume_24h: string | null;
  quote_volume_24h: string | null;
  price_change_24h: string | null;
  price_change_percent_24h: string | null;
  event_time: string | null;
}

export interface Candle {
  open_time: string;
  close_time: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
  quote_volume: string | null;
  trade_count: number | null;
}

export interface CandleSeries {
  symbol: string;
  timeframe: Timeframe;
  candles: Candle[];
}

export interface SymbolFilters {
  tick_size: string;
  step_size: string;
  min_quantity: string;
  min_notional: string | null;
}

export interface SymbolInfo {
  symbol: string;
  base_asset: string;
  quote_asset: string;
  status: string;
  contract_type: string;
  price_precision: number;
  quantity_precision: number;
  filters: SymbolFilters;
  /** Always null for public market data: only an authenticated endpoint serves it. */
  max_leverage: number | null;
}

export interface SymbolListResponse {
  count: number;
  eligible_only: boolean;
  symbols: SymbolInfo[];
}

export interface MarketDataStatus {
  exchange: string;
  source: string;
  connection_status: ConnectionStatus;
  last_success: string | null;
  last_failure: string | null;
  last_error_code: string | null;
  last_success_age_seconds: number | null;
  symbols_discovered: number | null;
  eligible_symbols: number | null;
}

export interface ScannerMetrics {
  timeframe: Timeframe;
  candles_used: number;
  window_return_percent: string;
  momentum_percent: string;
  volatility_percent: string;
  atr: string;
  atr_percent: string;
  average_volume: string;
  last_volume: string;
  relative_volume: string | null;
  range_percent: string;
  body_percent: string;
  trend: TrendDirection;
  trend_consistency: string;
}

export interface ScoreComponent {
  name: string;
  raw_value: string;
  normalized: string;
  weight: string;
  contribution: string;
  detail: string;
}

export interface OpportunityScore {
  score: string;
  components: ScoreComponent[];
  method: string;
}

export interface ScannerRow {
  symbol: string;
  base_asset: string;
  quote_asset: string;
  contract_type: string;
  status: string;
  ticker_status: DataStatus;
  ticker_detail: string | null;
  last_price: string | null;
  bid_price: string | null;
  ask_price: string | null;
  price_change_24h: string | null;
  price_change_percent_24h: string | null;
  high_24h: string | null;
  low_24h: string | null;
  volume_24h: string | null;
  quote_volume_24h: string | null;
  metrics_status: MetricStatus;
  metrics_detail: string | null;
  metrics: ScannerMetrics | null;
  opportunity: OpportunityScore | null;
}

export type RankingScope = "FULL_UNIVERSE" | "LIQUIDITY_POOL";

export interface ScannerPage {
  rows: ScannerRow[];
  page: number;
  page_size: number;
  total_rows: number;
  universe_size: number;
  ranking_scope: RankingScope;
  candidate_pool_size: number | null;
  sort_by: string;
  direction: string;
  ticker_source: string;
  ticker_status: DataStatus;
  ticker_event_ts: string | null;
  ticker_age_seconds: number | null;
  scanned_at: string;
}

export type ScannerSortField =
  | "symbol"
  | "last_price"
  | "price_change_percent_24h"
  | "volume_24h"
  | "quote_volume_24h"
  | "volatility_percent"
  | "momentum_percent"
  | "atr_percent"
  | "relative_volume"
  | "trend_consistency"
  | "opportunity_score";

/** Sorts the backend answers only from a bounded liquidity pool. */
export const METRIC_SORT_FIELDS: readonly ScannerSortField[] = [
  "volatility_percent",
  "momentum_percent",
  "atr_percent",
  "relative_volume",
  "trend_consistency",
  "opportunity_score",
];

export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    details: Record<string, unknown> | null;
    request_id: string | null;
  };
}
