import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import BacktestPage from "./page";
import * as api from "@/lib/api";
import type { BacktestResult, BacktestTrade } from "@/lib/types";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));

function trade(overrides: Partial<BacktestTrade> = {}): BacktestTrade {
  return {
    side: "LONG",
    entry_time: "2026-09-01T10:00:00Z",
    exit_time: "2026-09-01T14:00:00Z",
    entry_price: "81000.00000000",
    exit_price: "81500.00000000",
    quantity: "0.00012345",
    notional: "10.00000000",
    margin: "10.00000000",
    leverage: "1",
    exit_reason: "TAKE_PROFIT",
    bars_held: 4,
    gross_pnl: "0.06172500",
    fees: "0.01000000",
    net_pnl: "0.05172500",
    return_percent: "0.5173",
    equity_after: "100.05172500",
    max_adverse_excursion_percent: "0.1000",
    max_favourable_excursion_percent: "0.7000",
    ...overrides,
  };
}

function result(overrides: Partial<BacktestResult> = {}): BacktestResult {
  return {
    status: "COMPLETED",
    detail: null,
    symbol: "BTCUSDT",
    timeframe: "1h",
    strategy: "trend_momentum",
    strategy_version: "1.0.0",
    config: {
      starting_balance: "100",
      position_size_percent: "10",
      leverage: "1",
      fee_bps: "5",
      slippage_bps: "2",
      stop_loss_percent: "2",
      take_profit_percent: "4",
      trailing_stop_percent: null,
      allow_long: true,
      allow_short: true,
    },
    metrics: {
      total_trades: 41,
      winning_trades: 19,
      losing_trades: 22,
      breakeven_trades: 0,
      win_rate_percent: "46.3415",
      net_pnl: "-0.24763509",
      gross_profit: "0.95151916",
      gross_loss: "1.19915425",
      total_fees: "0.40969579",
      return_percent: "-0.2406",
      profit_factor: "0.7935",
      average_trade: "-0.00603988",
      average_win: "0.05007996",
      average_loss: "-0.05450701",
      largest_win: "0.20000000",
      largest_loss: "-0.18000000",
      max_drawdown_percent: "0.7306",
      max_drawdown_absolute: "0.73244626",
      sharpe_like_ratio: "-1.4244",
      exposure_percent: "19.9199",
      starting_balance: "100.00000000",
      ending_balance: "99.75938453",
      bars_tested: 999,
      trades_open_at_end: 1,
    },
    trades: [trade()],
    equity_curve: [
      { time: "2026-09-01T10:00:00Z", equity: "100.00000000", drawdown_percent: "0.0000", in_position: false },
      { time: "2026-09-01T11:00:00Z", equity: "99.80000000", drawdown_percent: "0.2000", in_position: true },
      { time: "2026-09-01T12:00:00Z", equity: "99.75938453", drawdown_percent: "0.2406", in_position: true },
    ],
    first_bar_time: "2026-08-10T14:00:00Z",
    last_bar_time: "2026-09-21T04:00:00Z",
    source: "binance-futures-usdm:rest",
    data_status: "OK",
    ran_at: "2026-09-21T05:00:00Z",
    warnings: ["A position was still open when the data ended; it was closed at the last bar's close."],
    assumptions: [
      "Signals computed on a closed bar fill at the NEXT bar's open, never at the close that produced them.",
      "NOT modelled: funding payments, partial fills, order-book depth.",
    ],
    label: "HISTORICAL SIMULATION",
    disclaimer:
      "Historical simulation over past candles under the stated assumptions. It is NOT a prediction, an expected return, a probability of profit, or evidence that these rules will work in future.",
    ...overrides,
  };
}

function stubSymbols() {
  vi.spyOn(api, "getSymbols").mockResolvedValue({
    count: 1,
    eligible_only: true,
    symbols: [],
  });
}

async function runSimulation(payload: BacktestResult = result()) {
  stubSymbols();
  const spy = vi.spyOn(api, "runBacktest").mockResolvedValue(payload);
  const user = userEvent.setup();
  render(<BacktestPage />);
  await user.click(screen.getByRole("button", { name: "Run simulation" }));
  await waitFor(() => expect(spy).toHaveBeenCalled());
  return spy;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("BacktestPage", () => {
  it("runs nothing until asked", () => {
    stubSymbols();
    const spy = vi.spyOn(api, "runBacktest");
    render(<BacktestPage />);
    expect(spy).not.toHaveBeenCalled();
    expect(screen.getByText("READY")).toBeInTheDocument();
    expect(screen.getByText(/most expensive request/)).toBeInTheDocument();
  });

  it("labels itself a historical simulation before any result exists", () => {
    stubSymbols();
    render(<BacktestPage />);
    expect(screen.getByText("HISTORICAL SIMULATION")).toBeInTheDocument();
  });

  it("sends the configured parameters", async () => {
    const spy = await runSimulation();
    const [symbol, params] = spy.mock.calls[0]!;
    expect(symbol).toBe("BTCUSDT");
    expect(params.timeframe).toBe("1h");
    expect(params.leverage).toBe("1");
    expect(params.stopLossPercent).toBe("2");
  });

  it("shows the headline metrics", async () => {
    await runSimulation();
    // Return renders at 2dp; net PnL at 4dp.
    expect(await screen.findByText("-0.24%")).toBeInTheDocument();
    expect(screen.getByText("41")).toBeInTheDocument();
    expect(screen.getByText("46.3%")).toBeInTheDocument();
    expect(screen.getByText("0.79")).toBeInTheDocument();
  });

  it("reports a losing simulation as a loss", async () => {
    // The engine must not flatter itself, and neither must the UI.
    await runSimulation();
    const netPnl = await screen.findByText("-0.2476");
    expect(netPnl).toHaveClass("down");
  });

  it("explains an undefined profit factor rather than showing a number", async () => {
    await runSimulation(
      result({
        metrics: { ...result().metrics!, profit_factor: null, sharpe_like_ratio: null },
      }),
    );
    await screen.findByText("Profit factor");
    const cells = screen.getAllByText("—");
    expect(cells.length).toBeGreaterThanOrEqual(2);
    expect(
      screen.getByTitle(/no losing trades, so the ratio has no denominator/),
    ).toBeInTheDocument();
  });

  it("shows warnings next to the numbers, not hidden", async () => {
    await runSimulation();
    expect(await screen.findByText(/still open when the data ended/)).toBeInTheDocument();
  });

  it("lists the assumptions and the disclaimer with the result", async () => {
    await runSimulation();
    expect(await screen.findByText(/fill at the NEXT bar's open/)).toBeInTheDocument();
    expect(screen.getByText(/NOT modelled: funding payments/)).toBeInTheDocument();
    expect(screen.getByText(/not a prediction/i)).toBeInTheDocument();
  });

  it("renders the trade list with its exit reasons", async () => {
    await runSimulation();
    expect(await screen.findByText("TAKE_PROFIT")).toBeInTheDocument();
    expect(screen.getByText("LONG")).toBeInTheDocument();
    expect(screen.getByText("81,000.00")).toBeInTheDocument();
  });

  it("reports a non-completed status instead of empty panels", async () => {
    await runSimulation(
      result({
        status: "INSUFFICIENT_DATA",
        detail: "Needs more than 55 closed candles; 40 available",
        metrics: null,
        trades: [],
        equity_curve: [],
      }),
    );
    expect(await screen.findByText("INSUFFICIENT_DATA")).toBeInTheDocument();
    expect(screen.getByText(/Needs more than 55 closed candles/)).toBeInTheDocument();
    expect(screen.queryByText("Equity and drawdown")).not.toBeInTheDocument();
  });

  it("surfaces a backend error with its code", async () => {
    stubSymbols();
    vi.spyOn(api, "runBacktest").mockRejectedValue(
      new api.ApiError("Exchange is unavailable", "EXCHANGE_UNAVAILABLE", 503),
    );
    const user = userEvent.setup();
    render(<BacktestPage />);
    await user.click(screen.getByRole("button", { name: "Run simulation" }));
    expect(await screen.findByText("EXCHANGE_UNAVAILABLE")).toBeInTheDocument();
  });

  it("describes leverage as a simulation input in the form", () => {
    stubSymbols();
    render(<BacktestPage />);
    expect(screen.getByTitle(/authorises nothing and sets no leverage/)).toBeInTheDocument();
  });

  it("says plainly when no trades were taken", async () => {
    await runSimulation(
      result({ trades: [], metrics: { ...result().metrics!, total_trades: 0 } }),
    );
    expect(await screen.findByText("NO TRADES")).toBeInTheDocument();
  });
});
