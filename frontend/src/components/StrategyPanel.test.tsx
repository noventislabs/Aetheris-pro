import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { IndicatorSelector } from "./IndicatorSelector";
import { StrategyPanel } from "./StrategyPanel";
import type {
  IndicatorDescriptor,
  LeverageDecision,
  StrategyResult,
} from "@/lib/types";

function descriptor(
  key: string,
  kind: "OVERLAY" | "OSCILLATOR",
  name = key.toUpperCase(),
): IndicatorDescriptor {
  return {
    key,
    name,
    kind,
    value_keys: [key],
    description: `${name} description`,
    parameters: [`${key}_period`],
    defaults: { [`${key}_period`]: "14" },
    convention: `${name} follows the documented convention`,
  };
}

const CATALOGUE = [
  descriptor("sma", "OVERLAY", "Simple Moving Average"),
  descriptor("bollinger", "OVERLAY", "Bollinger Bands"),
  descriptor("rsi", "OSCILLATOR", "Relative Strength Index"),
  descriptor("macd", "OSCILLATOR", "MACD"),
];

function leverage(overrides: Partial<LeverageDecision> = {}): LeverageDecision {
  return {
    requested_leverage: "20",
    exchange_max_leverage: null,
    risk_max_leverage: null,
    approved_leverage: null,
    outcome: "REJECTED",
    reason: "RISK_REJECTED_EXCHANGE_MAX_LEVERAGE_UNKNOWN",
    detail: "The venue's maximum leverage for this symbol is unknown.",
    binding_constraint: null,
    basis: "ATR volatility + deterministic strategy-condition agreement -> leverage candidate",
    inputs: {},
    note: "Leverage architecture only. The Risk Engine has final authority and no order is placed.",
    ...overrides,
  };
}

function result(overrides: Partial<StrategyResult> = {}): StrategyResult {
  return {
    strategy: "trend_momentum",
    name: "Trend-Momentum Confluence",
    version: "1.0.0",
    status: "READY",
    bias: "LONG_BIAS",
    detail: "All four long conditions are satisfied.",
    symbol: "BTCUSDT",
    timeframe: "1h",
    parameters: {},
    long_conditions: [
      { name: "trend", satisfied: true, detail: "EMA(21) is above EMA(55)", values: {} },
      { name: "trend_strength", satisfied: true, detail: "ADX 23.36 meets 20", values: {} },
      { name: "momentum", satisfied: true, detail: "RSI 57.28 within (50, 70)", values: {} },
      { name: "macd", satisfied: true, detail: "MACD histogram is positive", values: {} },
    ],
    short_conditions: [
      { name: "trend", satisfied: false, detail: "EMA(21) is not below EMA(55)", values: {} },
      { name: "trend_strength", satisfied: true, detail: "ADX 23.36 meets 20", values: {} },
      { name: "momentum", satisfied: false, detail: "RSI outside bearish band", values: {} },
      { name: "macd", satisfied: false, detail: "MACD histogram is not negative", values: {} },
    ],
    long_conditions_met: 4,
    short_conditions_met: 1,
    conditions_total: 4,
    leverage: leverage(),
    indicators_used: ["ema", "adx", "rsi", "macd"],
    candles_used: 299,
    last_candle_time: "2026-09-20T22:00:00Z",
    source: "binance-futures-usdm:rest",
    data_status: "OK",
    data_age_seconds: 12,
    evaluated_at: "2026-09-20T23:00:00Z",
    disclaimer:
      "Analysis only. This is a deterministic reading of indicator values, not a trade recommendation, a prediction of future price, a probability of profit, or financial advice. No order is placed by this system.",
    ...overrides,
  };
}

describe("StrategyPanel", () => {
  it("labels itself analysis only with no order execution", () => {
    render(<StrategyPanel result={result()} />);
    expect(screen.getByText("ANALYSIS ONLY")).toBeInTheDocument();
    expect(screen.getByText("NO ORDER EXECUTION")).toBeInTheDocument();
  });

  it("shows the bias with its version and reasons", () => {
    render(<StrategyPanel result={result()} />);
    expect(screen.getByText("LONG BIAS")).toBeInTheDocument();
    expect(screen.getByText("v1.0.0")).toBeInTheDocument();
    expect(screen.getByText("EMA(21) is above EMA(55)")).toBeInTheDocument();
  });

  it("shows both directions so a verdict can be judged", () => {
    render(<StrategyPanel result={result()} />);
    expect(screen.getByText("Long conditions")).toBeInTheDocument();
    expect(screen.getByText("Short conditions")).toBeInTheDocument();
    expect(screen.getByText("4/4")).toBeInTheDocument();
    expect(screen.getByText("1/4")).toBeInTheDocument();
  });

  it("renders the full disclaimer verbatim", () => {
    render(<StrategyPanel result={result()} />);
    expect(screen.getByText(/not a trade recommendation/)).toBeInTheDocument();
    expect(screen.getByText(/No order is placed by this system/)).toBeInTheDocument();
  });

  it("shows no bias at all when the analysis could not run", () => {
    render(
      <StrategyPanel
        result={result({
          status: "STALE",
          bias: null,
          detail: "Candle freshness could not be verified.",
        })}
      />,
    );
    expect(screen.getByText("STALE")).toBeInTheDocument();
    expect(screen.queryByText("LONG BIAS")).not.toBeInTheDocument();
    expect(screen.getByText(/freshness could not be verified/)).toBeInTheDocument();
    // Conditions are hidden too: there is no analysis to explain.
    expect(screen.queryByText("Long conditions")).not.toBeInTheDocument();
  });

  it("renders a NEUTRAL verdict as a finding, not an absence", () => {
    render(
      <StrategyPanel
        result={result({ bias: "NEUTRAL", detail: "Conditions conflict: 3/4 long." })}
      />,
    );
    expect(screen.getByText("NEUTRAL")).toBeInTheDocument();
    expect(screen.getByText(/Conditions conflict/)).toBeInTheDocument();
  });
});

describe("StrategyPanel leverage chain", () => {
  it("shows every link including the unknown ones", () => {
    // Showing "unknown" is the point: an omitted constraint would read as
    // one that had been checked and passed.
    render(<StrategyPanel result={result()} />);
    expect(screen.getByText("Requested")).toBeInTheDocument();
    expect(screen.getByText("Exchange max")).toBeInTheDocument();
    expect(screen.getByText("Risk max")).toBeInTheDocument();
    expect(screen.getByText("Approved")).toBeInTheDocument();
    expect(screen.getAllByText("unknown")).toHaveLength(2);
  });

  it("reports the rejection reason and never an approved value", () => {
    render(<StrategyPanel result={result()} />);
    expect(screen.getByText("REJECTED")).toBeInTheDocument();
    expect(
      screen.getByText("RISK_REJECTED_EXCHANGE_MAX_LEVERAGE_UNKNOWN"),
    ).toBeInTheDocument();
    expect(screen.getByText("none")).toBeInTheDocument();
  });

  it("never displays 500x as an available leverage", () => {
    const { container } = render(<StrategyPanel result={result()} />);
    expect(container.textContent).not.toContain("500x");
  });

  it("states the Risk Engine has final authority", () => {
    render(<StrategyPanel result={result()} />);
    expect(screen.getByText(/Risk Engine has final authority/)).toBeInTheDocument();
  });

  it("describes the candidate as volatility plus condition agreement", () => {
    render(<StrategyPanel result={result()} />);
    expect(screen.getByText(/ATR volatility \+ deterministic strategy-condition/)).toBeInTheDocument();
  });

  it("shows a reduced approval with the binding constraint named", () => {
    render(
      <StrategyPanel
        result={result({
          leverage: leverage({
            outcome: "REDUCED",
            approved_leverage: "3",
            exchange_max_leverage: "125",
            risk_max_leverage: "3",
            binding_constraint: "risk_max_leverage",
            reason: "LEVERAGE_REDUCED_TO_RISK_MAX",
            detail: "Requested 20x reduced to 3x by the risk engine ceiling.",
          }),
        })}
      />,
    );
    expect(screen.getByText("REDUCED")).toBeInTheDocument();
    expect(screen.getByText("125x")).toBeInTheDocument();
    // 3x appears twice, and that is the finding: the risk ceiling and the
    // approved value are the same number because that ceiling is what bound it.
    expect(screen.getAllByText("3x")).toHaveLength(2);
    expect(screen.getByText(/reduced to 3x by the risk engine ceiling/)).toBeInTheDocument();
  });
});

describe("IndicatorSelector", () => {
  it("renders only indicators the backend published", () => {
    render(
      <IndicatorSelector
        catalogue={CATALOGUE}
        selected={[]}
        maxSelected={8}
        onToggle={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Relative Strength Index" })).toBeInTheDocument();
    // Nothing is invented locally: an unpublished indicator simply is not there.
    expect(screen.queryByRole("button", { name: /Ichimoku/ })).not.toBeInTheDocument();
  });

  it("separates price overlays from oscillators", () => {
    render(
      <IndicatorSelector
        catalogue={CATALOGUE}
        selected={[]}
        maxSelected={8}
        onToggle={vi.fn()}
      />,
    );
    expect(screen.getByText("Price overlays")).toBeInTheDocument();
    expect(screen.getByText("Oscillators")).toBeInTheDocument();
  });

  it("shows SMC overlays as unavailable rather than hiding or drawing them", () => {
    render(
      <IndicatorSelector
        catalogue={CATALOGUE}
        selected={[]}
        maxSelected={8}
        onToggle={vi.fn()}
      />,
    );
    expect(screen.getByText("Smart Money Concepts")).toBeInTheDocument();
    expect(screen.getByText("BOS")).toBeInTheDocument();
    expect(screen.getAllByText("N/A").length).toBeGreaterThan(0);
    expect(screen.getByText("FVG").closest("span")).toHaveAttribute("aria-disabled", "true");
  });

  it("exposes each indicator's published convention", () => {
    render(
      <IndicatorSelector
        catalogue={CATALOGUE}
        selected={[]}
        maxSelected={8}
        onToggle={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "MACD" })).toHaveAttribute(
      "title",
      expect.stringContaining("Convention:"),
    );
  });

  it("toggles a selection", async () => {
    const user = userEvent.setup();
    const onToggle = vi.fn();
    render(
      <IndicatorSelector
        catalogue={CATALOGUE}
        selected={["rsi"]}
        maxSelected={8}
        onToggle={onToggle}
      />,
    );
    expect(screen.getByRole("button", { name: "Relative Strength Index" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await user.click(screen.getByRole("button", { name: "MACD" }));
    expect(onToggle).toHaveBeenCalledWith("macd");
  });

  it("enforces the backend's own per-request cap", () => {
    render(
      <IndicatorSelector
        catalogue={CATALOGUE}
        selected={["sma", "rsi"]}
        maxSelected={2}
        onToggle={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "MACD" })).toBeDisabled();
    // The selected ones stay clickable so a swap is possible.
    expect(screen.getByRole("button", { name: "Simple Moving Average" })).not.toBeDisabled();
    expect(screen.getByText(/Maximum of 2 indicators/)).toBeInTheDocument();
  });
});
