/**
 * Typed client for the Aetheris backend.
 *
 * Two rules shape this module:
 *
 * **Responses are validated at the boundary.** A malformed or unexpected
 * payload becomes a typed error the calling component renders as an error
 * state — it never reaches a component as a half-populated object that throws
 * mid-render and blanks the whole dashboard.
 *
 * **A failed request is never substituted with data.** There are no fallbacks,
 * no cached-last-known-value, no zeros. If the backend cannot answer, the UI
 * says so.
 */

import type {
  ApiErrorBody,
  BacktestResult,
  PaperAccount,
  PaperMethod,
  PaperOrderResult,
  PaperTickResult,
  IndicatorCatalogue,
  IndicatorSet,
  StrategyResult,
  CandleSeries,
  MarketDataStatus,
  ObservationEnvelope,
  ScannerPage,
  SymbolListResponse,
  Ticker,
  Timeframe,
} from "./types";

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ?? "http://127.0.0.1:8000";

/** A request that failed, carrying the backend's machine-readable code. */
export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly requestId: string | null;

  constructor(message: string, code: string, status: number, requestId: string | null = null) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.requestId = requestId;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasString(value: Record<string, unknown>, key: string): boolean {
  return typeof value[key] === "string";
}

/** Shape check for the provenance envelope every market value arrives in. */
function isObservationEnvelope(value: unknown): value is ObservationEnvelope<unknown> {
  if (!isRecord(value)) return false;
  if (!hasString(value, "status") || !hasString(value, "source")) return false;
  return "value" in value;
}

function isTicker(value: unknown): value is Ticker {
  return isRecord(value) && hasString(value, "symbol") && hasString(value, "last_price");
}

function isCandleSeries(value: unknown): value is CandleSeries {
  return (
    isRecord(value) &&
    hasString(value, "symbol") &&
    hasString(value, "timeframe") &&
    Array.isArray(value["candles"]) &&
    value["candles"].every(
      (candle) =>
        isRecord(candle) &&
        hasString(candle, "open") &&
        hasString(candle, "high") &&
        hasString(candle, "low") &&
        hasString(candle, "close") &&
        hasString(candle, "open_time"),
    )
  );
}

function isScannerPage(value: unknown): value is ScannerPage {
  return (
    isRecord(value) &&
    Array.isArray(value["rows"]) &&
    typeof value["total_rows"] === "number" &&
    typeof value["universe_size"] === "number" &&
    hasString(value, "ranking_scope") &&
    hasString(value, "ticker_status") &&
    value["rows"].every((row) => isRecord(row) && hasString(row, "symbol"))
  );
}

function isSymbolList(value: unknown): value is SymbolListResponse {
  return (
    isRecord(value) &&
    typeof value["count"] === "number" &&
    Array.isArray(value["symbols"]) &&
    value["symbols"].every((s) => isRecord(s) && hasString(s, "symbol"))
  );
}

function isMarketDataStatus(value: unknown): value is MarketDataStatus {
  return isRecord(value) && hasString(value, "exchange") && hasString(value, "connection_status");
}

async function readErrorBody(response: Response): Promise<{ code: string; message: string; requestId: string | null }> {
  try {
    const body: unknown = await response.json();
    if (isRecord(body) && isRecord(body["error"])) {
      const error = body["error"] as ApiErrorBody["error"];
      return {
        code: typeof error.code === "string" ? error.code : "UNKNOWN",
        message: typeof error.message === "string" ? error.message : response.statusText,
        requestId: typeof error.request_id === "string" ? error.request_id : null,
      };
    }
  } catch {
    // Body was not JSON. Fall through to the status line.
  }
  return { code: "UNKNOWN", message: response.statusText || "Request failed", requestId: null };
}

async function request<T>(
  path: string,
  validate: (value: unknown) => value is T,
  signal?: AbortSignal,
  body?: unknown,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      signal,
      // A body means a write, and the only writes this client makes are the
      // paper trading ones. Everything else stays a GET.
      method: body === undefined ? "GET" : "POST",
      headers: body === undefined
        ? { Accept: "application/json" }
        : { Accept: "application/json", "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw new ApiError(
      "Could not reach the Aetheris backend. Is it running?",
      "NETWORK_UNREACHABLE",
      0,
    );
  }

  if (!response.ok) {
    const { code, message, requestId } = await readErrorBody(response);
    throw new ApiError(message, code, response.status, requestId);
  }

  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new ApiError("Backend returned a non-JSON response", "INVALID_RESPONSE", response.status);
  }

  if (!validate(payload)) {
    // Refusing an unrecognised shape is what keeps one changed field from
    // crashing an unrelated panel.
    throw new ApiError(
      "Backend returned an unexpected response shape",
      "INVALID_RESPONSE",
      response.status,
    );
  }
  return payload;
}

/** Narrow an observation envelope to a specific payload type. */
function observationOf<T>(
  isValue: (value: unknown) => value is T,
): (value: unknown) => value is ObservationEnvelope<T> {
  return (value): value is ObservationEnvelope<T> => {
    if (!isObservationEnvelope(value)) return false;
    // The backend guarantees value===null unless status is OK; re-check rather
    // than trust, so a contract regression surfaces as an error state.
    if (value.status === "OK") return isValue(value.value);
    return value.value === null;
  };
}

export function getSymbols(search: string | null, signal?: AbortSignal): Promise<SymbolListResponse> {
  const params = new URLSearchParams({ limit: "50" });
  if (search) params.set("search", search);
  return request(`/api/v1/markets/symbols?${params}`, isSymbolList, signal);
}

export function getTicker(
  symbol: string,
  signal?: AbortSignal,
): Promise<ObservationEnvelope<Ticker>> {
  return request(
    `/api/v1/markets/${encodeURIComponent(symbol)}/ticker`,
    observationOf(isTicker),
    signal,
  );
}

export function getKlines(
  symbol: string,
  timeframe: Timeframe,
  limit: number,
  signal?: AbortSignal,
): Promise<ObservationEnvelope<CandleSeries>> {
  const params = new URLSearchParams({ interval: timeframe, limit: String(limit) });
  return request(
    `/api/v1/markets/${encodeURIComponent(symbol)}/klines?${params}`,
    observationOf(isCandleSeries),
    signal,
  );
}

export function getMarketStatus(signal?: AbortSignal): Promise<MarketDataStatus> {
  return request("/api/v1/markets/status", isMarketDataStatus, signal);
}

export interface ScanParams {
  search?: string;
  sort?: string;
  direction?: "asc" | "desc";
  page?: number;
  pageSize?: number;
  timeframe?: Timeframe;
  includeMetrics?: boolean;
  minQuoteVolume?: string;
}

export function getScan(params: ScanParams, signal?: AbortSignal): Promise<ScannerPage> {
  const query = new URLSearchParams();
  if (params.search) query.set("search", params.search);
  if (params.sort) query.set("sort", params.sort);
  if (params.direction) query.set("direction", params.direction);
  if (params.page) query.set("page", String(params.page));
  if (params.pageSize) query.set("page_size", String(params.pageSize));
  if (params.timeframe) query.set("timeframe", params.timeframe);
  if (params.includeMetrics) query.set("include_metrics", "true");
  if (params.minQuoteVolume) query.set("min_quote_volume", params.minQuoteVolume);
  return request(`/api/v1/scanner?${query}`, isScannerPage, signal);
}

// ---------------------------------------------------------------------------
// Phase 4: indicators and strategy analysis
// ---------------------------------------------------------------------------

function isIndicatorCatalogue(value: unknown): value is IndicatorCatalogue {
  return (
    isRecord(value) &&
    Array.isArray(value["indicators"]) &&
    typeof value["max_per_request"] === "number" &&
    value["indicators"].every(
      (entry) => isRecord(entry) && hasString(entry, "key") && hasString(entry, "kind"),
    )
  );
}

function isIndicatorSet(value: unknown): value is IndicatorSet {
  return (
    isRecord(value) &&
    hasString(value, "symbol") &&
    hasString(value, "source") &&
    hasString(value, "data_status") &&
    Array.isArray(value["indicators"]) &&
    value["indicators"].every(
      (entry) =>
        isRecord(entry) && hasString(entry, "indicator") && hasString(entry, "status"),
    )
  );
}

function isStrategyResult(value: unknown): value is StrategyResult {
  return (
    isRecord(value) &&
    hasString(value, "strategy") &&
    hasString(value, "status") &&
    hasString(value, "disclaimer") &&
    Array.isArray(value["long_conditions"]) &&
    Array.isArray(value["short_conditions"])
  );
}

export function getIndicatorCatalogue(signal?: AbortSignal): Promise<IndicatorCatalogue> {
  return request("/api/v1/analysis/indicators", isIndicatorCatalogue, signal);
}

export function getIndicators(
  symbol: string,
  timeframe: Timeframe,
  keys: readonly string[],
  options: { limit?: number; seriesPoints?: number } = {},
  signal?: AbortSignal,
): Promise<IndicatorSet> {
  const params = new URLSearchParams({
    indicators: keys.join(","),
    timeframe,
    limit: String(options.limit ?? 300),
    series_points: String(options.seriesPoints ?? 0),
  });
  return request(
    `/api/v1/analysis/${encodeURIComponent(symbol)}/indicators?${params}`,
    isIndicatorSet,
    signal,
  );
}

export function getStrategy(
  symbol: string,
  timeframe: Timeframe,
  signal?: AbortSignal,
): Promise<StrategyResult> {
  const params = new URLSearchParams({ strategy: "trend_momentum", timeframe });
  return request(
    `/api/v1/analysis/${encodeURIComponent(symbol)}/strategy?${params}`,
    isStrategyResult,
    signal,
  );
}

// ---------------------------------------------------------------------------
// Phase 5: historical simulation
// ---------------------------------------------------------------------------

function isBacktestResult(value: unknown): value is BacktestResult {
  return (
    isRecord(value) &&
    hasString(value, "status") &&
    hasString(value, "symbol") &&
    hasString(value, "label") &&
    hasString(value, "disclaimer") &&
    Array.isArray(value["trades"]) &&
    Array.isArray(value["equity_curve"]) &&
    Array.isArray(value["assumptions"])
  );
}

export interface BacktestParams {
  timeframe: Timeframe;
  limit: number;
  startingBalance: string;
  positionSizePercent: string;
  leverage: string;
  feeBps: string;
  slippageBps: string;
  stopLossPercent: string | null;
  takeProfitPercent: string | null;
  trailingStopPercent: string | null;
  allowLong: boolean;
  allowShort: boolean;
}

export function runBacktest(
  symbol: string,
  params: BacktestParams,
  signal?: AbortSignal,
): Promise<BacktestResult> {
  const query = new URLSearchParams({
    timeframe: params.timeframe,
    limit: String(params.limit),
    starting_balance: params.startingBalance,
    position_size_percent: params.positionSizePercent,
    leverage: params.leverage,
    fee_bps: params.feeBps,
    slippage_bps: params.slippageBps,
    allow_long: String(params.allowLong),
    allow_short: String(params.allowShort),
  });
  // Optional exits are omitted entirely rather than sent empty, so the backend
  // sees "not configured" rather than a value it must interpret.
  if (params.stopLossPercent) query.set("stop_loss_percent", params.stopLossPercent);
  if (params.takeProfitPercent) query.set("take_profit_percent", params.takeProfitPercent);
  if (params.trailingStopPercent)
    query.set("trailing_stop_percent", params.trailingStopPercent);

  return request(
    `/api/v1/backtest/${encodeURIComponent(symbol)}?${query}`,
    isBacktestResult,
    signal,
  );
}


// ----------------------------------------------------------------------
// Paper trading
//
// The only write calls in this client. Every one of them acts on simulated
// state: no order reaches an exchange, and the backend holds no credential
// that could send one.
// ----------------------------------------------------------------------

function isPaperAccount(value: unknown): value is PaperAccount {
  return (
    isRecord(value) &&
    hasString(value, "account_id") &&
    hasString(value, "balance") &&
    hasString(value, "equity") &&
    hasString(value, "durability_notice") &&
    Array.isArray(value["positions"]) &&
    isRecord(value["session"])
  );
}

function isPaperOrderResult(value: unknown): value is PaperOrderResult {
  return (
    isRecord(value) &&
    typeof value["accepted"] === "boolean" &&
    isRecord(value["order"]) &&
    isPaperAccount(value["account"])
  );
}

function isPaperTickResult(value: unknown): value is PaperTickResult {
  return (
    isRecord(value) &&
    isPaperAccount(value["account"]) &&
    Array.isArray(value["closed_trades"]) &&
    hasString(value, "detail")
  );
}

function isPaperMethod(value: unknown): value is PaperMethod {
  return (
    isRecord(value) &&
    hasString(value, "label") &&
    hasString(value, "disclaimer") &&
    Array.isArray(value["assumptions"]) &&
    Array.isArray(value["not_modelled"])
  );
}

export function getPaperMethod(signal?: AbortSignal): Promise<PaperMethod> {
  return request("/api/v1/paper/method", isPaperMethod, signal);
}

export function getPaperAccount(signal?: AbortSignal): Promise<PaperAccount> {
  return request("/api/v1/paper/account", isPaperAccount, signal);
}

export interface PaperOrderParams {
  symbol: string;
  side: "BUY" | "SELL";
  margin?: string;
  quantity?: string;
  leverage: string;
  stopLossPercent?: string;
  takeProfitPercent?: string;
  trailingStopPercent?: string;
  clientOrderId?: string;
}

export function submitPaperOrder(
  params: PaperOrderParams,
  signal?: AbortSignal,
): Promise<PaperOrderResult> {
  const body: Record<string, unknown> = {
    symbol: params.symbol,
    side: params.side,
    leverage: params.leverage,
  };
  // Optional fields are omitted rather than sent empty, so the backend sees
  // "not configured" instead of a value it has to interpret.
  if (params.margin) body["margin"] = params.margin;
  if (params.quantity) body["quantity"] = params.quantity;
  if (params.stopLossPercent) body["stop_loss_percent"] = params.stopLossPercent;
  if (params.takeProfitPercent) body["take_profit_percent"] = params.takeProfitPercent;
  if (params.trailingStopPercent) body["trailing_stop_percent"] = params.trailingStopPercent;
  if (params.clientOrderId) body["client_order_id"] = params.clientOrderId;

  return request("/api/v1/paper/orders", isPaperOrderResult, signal, body);
}

export function tickPaper(signal?: AbortSignal): Promise<PaperTickResult> {
  return request("/api/v1/paper/tick", isPaperTickResult, signal, {});
}

export function closePaperPosition(
  symbol: string,
  signal?: AbortSignal,
): Promise<PaperOrderResult> {
  return request(
    `/api/v1/paper/positions/${encodeURIComponent(symbol)}/close`,
    isPaperOrderResult,
    signal,
    {},
  );
}

export function resetPaperAccount(
  startingBalance?: string,
  signal?: AbortSignal,
): Promise<PaperAccount> {
  return request(
    "/api/v1/paper/reset",
    isPaperAccount,
    signal,
    startingBalance ? { starting_balance: startingBalance } : {},
  );
}

export function setPaperEmergencyStop(
  engaged: boolean,
  reason: string,
  signal?: AbortSignal,
): Promise<PaperAccount> {
  return request("/api/v1/paper/emergency-stop", isPaperAccount, signal, { engaged, reason });
}
