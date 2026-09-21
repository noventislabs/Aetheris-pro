import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SetupPanel } from "./SetupPanel";
import type { TradeSetup } from "@/lib/types";

/**
 * A 0-100 score is the most misreadable thing this terminal renders, so most
 * of what is asserted here is about what the panel must *not* let it become.
 *
 * The rest is refusal handling: a setup the backend declined to derive must
 * show the reason, not a greyed-out number that reads as a weak signal.
 */

function setup(overrides: Partial<TradeSetup> = {}): TradeSetup {
  return {
    symbol: "BTCUSDT",
    timeframe: "1h",
    strategy: "trend_momentum",
    strategy_version: "1.0.0",
    status: "ACTIONABLE",
    direction: "LONG",
    score: {
      value: "82.50",
      method: "setup-score/v1",
      meaning:
        "Strategy alignment on a 0-100 scale. NOT a probability of profit, a win rate, " +
        "an expected return or a forecast.",
      components: [
        {
          name: "trend_alignment",
          raw_value: "2",
          normalized: "1",
          weight: "0.25",
          contribution: "25.00",
          detail: "2 of 2 trend rules satisfied.",
        },
        {
          name: "risk_reward",
          raw_value: "2",
          normalized: "0.6667",
          weight: "0.20",
          contribution: "13.33",
          detail: "Risk/reward 2.000 against a 3R reference.",
        },
      ],
    },
    risk_reward: {
      entry_price: "341.4",
      stop_price: "333.81",
      take_profit_price: "356.56",
      risk_per_unit: "7.59",
      reward_per_unit: "15.16",
      risk_reward_ratio: "2",
      stop_model: "ATR",
      stop_distance_percent: "2.22",
      take_profit_r_multiple: "2",
      entry_basis:
        "Entry is the last closed candle's close. A live order fills at the NEXT bar's open at best.",
    },
    regime: {
      regime: "TREND_UP",
      volatility_band: "NORMAL",
      reason: "ADX 31.20 meets the 20 minimum and the fast EMA is above the slow one.",
      method: "market-regime/v1",
      candles_used: 220,
      measurements: [
        {
          name: "trend_strength",
          value: "31.20",
          threshold: "20",
          detail: "ADX 31.20 against a trend minimum of 20",
        },
      ],
    },
    conditions: [
      { name: "trend", satisfied: true, detail: "EMA(21) is above EMA(55)", values: {} },
      { name: "macd", satisfied: true, detail: "MACD histogram is positive", values: {} },
    ],
    opposing_conditions: [],
    detail: "All four LONG rules hold.",
    indicators_used: ["ema", "adx", "rsi", "macd"],
    candles_used: 220,
    last_candle_time: "2026-09-21T20:00:00+00:00",
    data_source: "binance-futures-usdm:rest",
    data_status: "OK",
    data_age_seconds: 12.5,
    evaluated_at: "2026-09-21T20:11:41+00:00",
    disclaimer:
      "Analysis only. A setup score measures agreement with a published rule set; it is " +
      "not a probability of profit, a prediction, or financial advice. Every order remains " +
      "subject to the risk engine, which has final authority.",
    ...overrides,
  };
}

describe("Setup panel", () => {
  it("labels the score for what it measures, not as a confidence", () => {
    render(<SetupPanel setup={setup()} />);
    expect(screen.getByText("Strategy Setup Score")).toBeInTheDocument();
    expect(screen.getByText(/rule\s+alignment, not a probability of profit/)).toBeInTheDocument();
    expect(screen.getByText("ANALYSIS ONLY")).toBeInTheDocument();
  });

  it("uses probability language only to deny it, never to assert it", () => {
    const sample = setup();
    const { container } = render(<SetupPanel setup={sample} />);
    const text = (container.textContent ?? "").toLowerCase();

    // The words DO appear -- inside the meaning and disclaimer strings, which
    // exist precisely to deny them. Asserting their absence outright would
    // fail on the very sentences that make the panel honest. So they are
    // removed and the remainder is what gets checked: everything the panel
    // says in its own voice.
    const denials = [
      sample.score?.meaning ?? "",
      sample.disclaimer,
      "rule alignment, not a probability of profit",
    ].map((s) => s.toLowerCase().replace(/\s+/g, " "));

    let remainder = text.replace(/\s+/g, " ");
    for (const denial of denials) {
      remainder = remainder.split(denial).join(" ");
    }

    for (const banned of ["confidence", "win rate", "probability", "chance of", "guaranteed"]) {
      expect(remainder).not.toContain(banned);
    }
    // And the denials really were present to begin with.
    expect(text).toContain("not a probability of profit");
  });

  it("renders the backend's own meaning string rather than a paraphrase", () => {
    render(<SetupPanel setup={setup()} />);
    expect(screen.getByText(/NOT a probability of profit, a win rate/)).toBeInTheDocument();
  });

  it("shows each component's arithmetic so the total can be checked", () => {
    render(<SetupPanel setup={setup()} />);
    expect(screen.getByText("trend alignment")).toBeInTheDocument();
    expect(screen.getByText("25.00")).toBeInTheDocument();
    expect(screen.getByText("×0.25")).toBeInTheDocument();
    expect(screen.getByText("risk reward")).toBeInTheDocument();
    expect(screen.getByText("13.33")).toBeInTheDocument();
  });

  it("shows real levels with the stop and target distinguished", () => {
    render(<SetupPanel setup={setup()} />);
    expect(screen.getByText("341.4")).toBeInTheDocument();
    expect(screen.getByText("333.81")).toBeInTheDocument();
    expect(screen.getByText("356.56")).toBeInTheDocument();
    expect(screen.getByText("2R")).toBeInTheDocument();
  });

  it("states that the entry is an estimate, not a quote", () => {
    render(<SetupPanel setup={setup()} />);
    expect(screen.getByText(/fills at the NEXT bar's open/)).toBeInTheDocument();
  });

  it("names the regime as rule-based rather than a model", () => {
    render(<SetupPanel setup={setup()} />);
    expect(screen.getByText("TREND UP")).toBeInTheDocument();
    expect(screen.getByText(/no model is involved/)).toBeInTheDocument();
  });

  it("shows a stale refusal as a reason, with no score and no levels", () => {
    render(
      <SetupPanel
        setup={setup({
          status: "STALE",
          direction: "NO_SIGNAL",
          score: null,
          risk_reward: null,
          regime: null,
          conditions: [],
          detail: "Candle data is STALE; no setup is reported from data that is not current.",
          data_status: "STALE",
        })}
      />,
    );
    expect(screen.getByText("STALE DATA")).toBeInTheDocument();
    expect(screen.getByText("NO SIGNAL")).toBeInTheDocument();
    expect(screen.getByText(/no setup is reported from data that is not current/)).toBeInTheDocument();
    expect(screen.queryByText("/100")).not.toBeInTheDocument();
    expect(screen.queryByText("Risk / reward")).not.toBeInTheDocument();
  });

  it("shows insufficient data as its own answer, not as a weak signal", () => {
    render(
      <SetupPanel
        setup={setup({
          status: "INSUFFICIENT_DATA",
          direction: "NO_SIGNAL",
          score: null,
          risk_reward: null,
          regime: null,
          conditions: [],
          detail: "At least one required indicator has not warmed up over 40 candles.",
        })}
      />,
    );
    expect(screen.getByText("INSUFFICIENT DATA")).toBeInTheDocument();
    expect(screen.getByText(/has not warmed up/)).toBeInTheDocument();
    expect(screen.queryByText("Score components")).not.toBeInTheDocument();
  });

  it("draws no levels when the backend declined to derive a stop", () => {
    render(
      <SetupPanel
        setup={setup({
          status: "NO_ACTIONABLE_SETUP",
          score: null,
          risk_reward: null,
          detail: "No safe stop could be derived under the STRUCTURE model.",
        })}
      />,
    );
    expect(screen.getByText("NO ACTIONABLE SETUP")).toBeInTheDocument();
    expect(screen.queryByText("Entry")).not.toBeInTheDocument();
    expect(screen.queryByText("Stop")).not.toBeInTheDocument();
  });

  it("carries the disclaimer and the provenance", () => {
    render(<SetupPanel setup={setup()} />);
    expect(screen.getByText(/final authority/)).toBeInTheDocument();
    expect(screen.getByText(/220 candles/)).toBeInTheDocument();
  });
});
