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

// ---------------------------------------------------------------------------
// Phase 4: indicators and strategy analysis
// ---------------------------------------------------------------------------

export type IndicatorStatus =
  | "READY"
  | "WARMING_UP"
  | "INSUFFICIENT_DATA"
  | "UNAVAILABLE"
  | "ERROR";

/** Where an indicator belongs: over price, or in its own pane. */
export type IndicatorKind = "OVERLAY" | "OSCILLATOR";

export interface IndicatorDescriptor {
  key: string;
  name: string;
  kind: IndicatorKind;
  value_keys: string[];
  description: string;
  parameters: string[];
  defaults: Record<string, string>;
  /** The published formula convention, shown so a number is never unlabelled. */
  convention: string;
}

export interface IndicatorCatalogue {
  indicators: IndicatorDescriptor[];
  max_per_request: number;
  note: string;
}

export interface IndicatorPoint {
  time: string;
  values: Record<string, string | null>;
}

export interface IndicatorResult {
  indicator: string;
  name: string;
  kind: IndicatorKind;
  status: IndicatorStatus;
  detail: string | null;
  parameters: Record<string, string>;
  value_keys: string[];
  latest: Record<string, string | null> | null;
  latest_time: string | null;
  warmup_bars: number;
  candles_used: number;
  series: IndicatorPoint[];
}

export interface IndicatorSet {
  symbol: string;
  timeframe: Timeframe;
  source: string;
  data_status: string;
  data_detail: string | null;
  data_age_seconds: number | null;
  candle_count: number;
  last_candle_time: string | null;
  indicators: IndicatorResult[];
}

export type StrategyBias = "LONG_BIAS" | "SHORT_BIAS" | "NEUTRAL";

export type StrategyStatus =
  | "READY"
  | "INSUFFICIENT_DATA"
  | "STALE"
  | "UNAVAILABLE"
  | "ERROR";

export interface ConditionOutcome {
  name: string;
  satisfied: boolean;
  detail: string;
  values: Record<string, string>;
}

export interface StrategyResult {
  strategy: string;
  name: string;
  version: string;
  status: StrategyStatus;
  /** Null unless status is READY: an unrunnable analysis has no finding. */
  bias: StrategyBias | null;
  detail: string | null;
  symbol: string;
  timeframe: Timeframe;
  parameters: Record<string, string>;
  long_conditions: ConditionOutcome[];
  short_conditions: ConditionOutcome[];
  long_conditions_met: number;
  short_conditions_met: number;
  conditions_total: number;
  /** Always present when the analysis ran; always a rejection in this build. */
  leverage: LeverageDecision | null;
  indicators_used: string[];
  candles_used: number;
  last_candle_time: string | null;
  source: string | null;
  data_status: string | null;
  data_age_seconds: number | null;
  evaluated_at: string | null;
  disclaimer: string;
}

/** Overlays draw on the price axis; everything else needs its own pane. */
export function isOverlay(descriptor: IndicatorDescriptor): boolean {
  return descriptor.kind === "OVERLAY";
}

/** Leverage architecture: 1–500x is the *candidate* range, never a permission. */
export type LeverageOutcome = "APPROVED" | "REDUCED" | "REJECTED";

export interface LeverageDecision {
  requested_leverage: string | null;
  /** Null when unknown. Never guessed, and never assumed to be 500. */
  exchange_max_leverage: string | null;
  risk_max_leverage: string | null;
  /** Null unless every constraint is known. */
  approved_leverage: string | null;
  outcome: LeverageOutcome;
  reason: string;
  detail: string;
  binding_constraint: string | null;
  basis: string | null;
  inputs: Record<string, string>;
  note: string;
}

// ---------------------------------------------------------------------------
// Phase 5: historical simulation
// ---------------------------------------------------------------------------

export type BacktestStatus =
  | "COMPLETED"
  | "INSUFFICIENT_DATA"
  | "UNAVAILABLE"
  | "ERROR";

export type ExitReason =
  | "SIGNAL_FLIP"
  | "STOP_LOSS"
  | "TAKE_PROFIT"
  | "TRAILING_STOP"
  | "LIQUIDATION"
  | "END_OF_DATA";

export interface BacktestConfig {
  starting_balance: string;
  position_size_percent: string;
  /** A simulation input only. It authorises nothing and sets no leverage. */
  leverage: string;
  fee_bps: string;
  slippage_bps: string;
  stop_loss_percent: string | null;
  take_profit_percent: string | null;
  trailing_stop_percent: string | null;
  allow_long: boolean;
  allow_short: boolean;
}

export interface BacktestTrade {
  side: "LONG" | "SHORT";
  entry_time: string;
  exit_time: string;
  entry_price: string;
  exit_price: string;
  quantity: string;
  notional: string;
  margin: string;
  leverage: string;
  exit_reason: ExitReason;
  bars_held: number;
  gross_pnl: string;
  fees: string;
  net_pnl: string;
  return_percent: string;
  equity_after: string;
  max_adverse_excursion_percent: string | null;
  max_favourable_excursion_percent: string | null;
}

export interface EquityPoint {
  time: string;
  equity: string;
  drawdown_percent: string;
  in_position: boolean;
}

export interface BacktestMetrics {
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  breakeven_trades: number;
  win_rate_percent: string | null;
  net_pnl: string;
  gross_profit: string;
  gross_loss: string;
  total_fees: string;
  return_percent: string;
  /** Null when there are no losing trades — undefined, not infinity. */
  profit_factor: string | null;
  average_trade: string | null;
  average_win: string | null;
  average_loss: string | null;
  largest_win: string | null;
  largest_loss: string | null;
  max_drawdown_percent: string;
  max_drawdown_absolute: string;
  /** Null on a flat curve — no dispersion means no defined ratio. */
  sharpe_like_ratio: string | null;
  exposure_percent: string;
  starting_balance: string;
  ending_balance: string;
  bars_tested: number;
  trades_open_at_end: number;
}

export interface BacktestResult {
  status: BacktestStatus;
  detail: string | null;
  symbol: string;
  timeframe: Timeframe;
  strategy: string;
  strategy_version: string;
  config: BacktestConfig;
  metrics: BacktestMetrics | null;
  trades: BacktestTrade[];
  equity_curve: EquityPoint[];
  first_bar_time: string | null;
  last_bar_time: string | null;
  source: string | null;
  data_status: string | null;
  ran_at: string | null;
  warnings: string[];
  assumptions: string[];
  label: string;
  disclaimer: string;
}

// ----------------------------------------------------------------------
// Paper trading
// ----------------------------------------------------------------------

export type OrderSide = "BUY" | "SELL";
export type PositionSide = "LONG" | "SHORT";

export type PaperExitReason =
  | "MANUAL_CLOSE"
  | "STOP_LOSS"
  | "TAKE_PROFIT"
  | "TRAILING_STOP"
  | "LIQUIDATION"
  | "SIGNAL_FLIP"
  | "ACCOUNT_RESET";

export type RiskLockState =
  | "NONE"
  | "DAILY_PROFIT_TARGET"
  | "DAILY_LOSS_LIMIT"
  | "EMERGENCY_STOP";

export interface PaperFill {
  fill_id: string;
  price: string;
  quantity: string;
  fee: string;
  filled_at: string;
  /** Which observation the fill used — best ask, best bid, or last + slippage. */
  price_source: string;
  price_age_seconds: number | null;
}

export interface PaperOrder {
  order_id: string;
  client_order_id: string;
  symbol: string;
  side: OrderSide;
  order_type: string;
  state: string;
  reduce_only: boolean;
  requested_quantity: string;
  filled_quantity: string;
  average_fill_price: string | null;
  fills: PaperFill[];
  leverage: LeverageDecision | null;
  created_at: string;
  updated_at: string;
  rejection_code: string | null;
  rejection_detail: string | null;
  idempotent_replay: boolean;
}

export interface PaperPosition {
  position_id: string;
  symbol: string;
  side: PositionSide;
  quantity: string;
  entry_price: string;
  notional: string;
  margin: string;
  approved_leverage: string;
  entry_fee: string;
  stop_price: string | null;
  target_price: string | null;
  trailing_stop_percent: string | null;
  trail_extreme: string | null;
  /** Null at 1x long: a level of zero is not a liquidation price. */
  liquidation_price: string | null;
  opened_at: string;
  updated_at: string;
  opening_order_id: string;
  /** Null when the last poll found no usable price. */
  mark_price: string | null;
  mark_source: string | null;
  mark_status: string | null;
  unrealized_pnl: string | null;
}

export interface PaperTrade {
  trade_id: string;
  symbol: string;
  side: PositionSide;
  quantity: string;
  entry_price: string;
  exit_price: string;
  notional: string;
  margin: string;
  approved_leverage: string;
  opened_at: string;
  closed_at: string;
  exit_reason: PaperExitReason;
  gross_pnl: string;
  fees: string;
  net_pnl: string;
  return_percent: string;
  balance_after: string;
}

export interface DailySession {
  session_date: string;
  realized_pnl: string;
  fees: string;
  trades_closed: number;
  orders_submitted: number;
  orders_rejected: number;
  profit_target: string;
  loss_limit: string;
  lock_state: RiskLockState;
  lock_reason: string | null;
  locked_at: string | null;
}

export interface PaperAccount {
  account_id: string;
  mode: string;
  created_at: string;
  updated_at: string;
  starting_balance: string;
  balance: string;
  equity: string;
  available_balance: string;
  margin_used: string;
  realized_pnl: string;
  /** Null when an open position could not be marked — never a substituted zero. */
  unrealized_pnl: string | null;
  total_fees: string;
  positions: PaperPosition[];
  open_orders: PaperOrder[];
  recent_orders: PaperOrder[];
  recent_trades: PaperTrade[];
  session: DailySession;
  autonomous_enabled: boolean;
  durability: "IN_MEMORY" | "DURABLE";
  durability_notice: string;
  mark_source: string | null;
  mark_status: string | null;
  mark_age_seconds: number | null;
  label: string;
  disclaimer: string;
}

export interface PaperOrderResult {
  accepted: boolean;
  order: PaperOrder;
  position: PaperPosition | null;
  trade: PaperTrade | null;
  account: PaperAccount;
  detail: string | null;
}

export interface PaperTickResult {
  account: PaperAccount;
  closed_trades: PaperTrade[];
  detail: string;
}

export interface PaperMethod {
  label: string;
  mode: string;
  assumptions: string[];
  not_modelled: string[];
  rejection_codes: string[];
  durability: string;
  durability_notice: string;
  disclaimer: string;
}
// ----------------------------------------------------------------------
// Phase 7: the risk engine and autonomous paper trading
// ----------------------------------------------------------------------

export type AutonomousAction =
  | "ENTERED"
  | "REFUSED"
  | "MANAGED"
  | "CLOSED"
  | "NO_SIGNAL"
  | "SKIPPED";

export type AutonomousLoopState = "DISABLED_BY_CONFIG" | "DISARMED" | "ARMED" | "FAILED";

export interface AutonomousDecision {
  decision_id: string;
  sequence: number;
  decided_at: string;
  symbol: string;
  timeframe: Timeframe;
  action: AutonomousAction;
  detail: string;
  bar_close_time: string | null;

  strategy: string | null;
  strategy_status: string | null;
  bias: string | null;
  /** Counts, deliberately not a ratio — 3/4 cannot be read as a likelihood. */
  conditions_met: number | null;
  conditions_total: number | null;

  rejection_code: string | null;
  rejection_detail: string | null;
  leverage: LeverageDecision | null;
  risk_max_leverage: string | null;
  /** Named checks the risk engine ran, in order — including the ones that passed. */
  checks_performed: string[];

  side: OrderSide | null;
  position_side: PositionSide | null;
  exit_reason: PaperExitReason | null;
  client_order_id: string | null;
  order_id: string | null;
  position_id: string | null;
  trade_id: string | null;
  proposed_margin: string | null;
  realized_pnl: string | null;

  source: string | null;
  data_status: string | null;
  data_age_seconds: number | null;
  atr_percent: string | null;
  label: string;
}

export interface AutonomousStatus {
  state: AutonomousLoopState;
  /** False on every process start, whatever it was before a restart. */
  enabled: boolean;
  /** Gates arming; never arms on its own. */
  permitted_by_config: boolean;
  paper_mode_enabled: boolean;
  symbols: string[];
  excluded_symbols: Record<string, string>;
  timeframe: Timeframe | null;
  interval_seconds: number | null;
  iterations: number;
  decisions_recorded: number;
  entries: number;
  refusals: number;
  closes: number;
  last_iteration_at: string | null;
  last_iteration_duration_seconds: number | null;
  /** Set only when the loop failed — a silent death must not look like a quiet market. */
  failure_detail: string | null;
  venue_outage: boolean;
  disclaimer: string;
}

export interface AutonomousDecisions {
  count: number;
  decisions: AutonomousDecision[];
  detail: string;
}
