import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CandleChart } from "./CandleChart";
import type { Candle } from "@/lib/types";

function candle(overrides: Partial<Candle> = {}): Candle {
  return {
    open_time: "2026-09-20T22:00:00Z",
    close_time: "2026-09-20T22:59:59Z",
    open: "80897.40",
    high: "81256.10",
    low: "80734.10",
    close: "81181.20",
    volume: "3669.947",
    quote_volume: null,
    trade_count: null,
    ...overrides,
  };
}

function series(count: number): Candle[] {
  return Array.from({ length: count }, (_, index) =>
    candle({
      open_time: new Date(Date.UTC(2026, 8, 20, index)).toISOString(),
      close_time: new Date(Date.UTC(2026, 8, 20, index, 59, 59)).toISOString(),
    }),
  );
}

describe("CandleChart", () => {
  it("draws one body and one wick per candle", () => {
    const { container } = render(
      <CandleChart candles={series(5)} timeframe="1h" symbol="BTCUSDT" />,
    );
    // 5 wicks + 5 bodies + 5 volume bars, plus the last-price marker rect.
    expect(container.querySelectorAll("rect").length).toBe(11);
    expect(container.querySelectorAll("line").length).toBeGreaterThanOrEqual(5);
  });

  it("describes itself for assistive technology", () => {
    render(<CandleChart candles={series(3)} timeframe="4h" symbol="ETHUSDT" />);
    expect(
      screen.getByRole("img", { name: /ETHUSDT 4h candlestick chart, 3 candles/ }),
    ).toBeInTheDocument();
  });

  it("says so when the series is empty rather than drawing nothing", () => {
    render(<CandleChart candles={[]} timeframe="1h" symbol="BTCUSDT" />);
    expect(screen.getByText("NO CANDLES")).toBeInTheDocument();
    expect(screen.getByText(/empty series for BTCUSDT/)).toBeInTheDocument();
  });

  it("drops an unparseable candle instead of guessing its values", () => {
    const { container } = render(
      <CandleChart
        candles={[candle(), candle({ close: "not-a-price" }), candle()]}
        timeframe="1h"
        symbol="BTCUSDT"
      />,
    );
    // Two usable candles: 2 bodies + 2 volume bars + 1 price marker.
    expect(container.querySelectorAll("rect").length).toBe(5);
  });

  it("survives a completely flat window without dividing by zero", () => {
    const flat = Array.from({ length: 4 }, (_, index) =>
      candle({
        open_time: new Date(Date.UTC(2026, 8, 20, index)).toISOString(),
        open: "100",
        high: "100",
        low: "100",
        close: "100",
      }),
    );
    const { container } = render(
      <CandleChart candles={flat} timeframe="1h" symbol="FLATUSDT" />,
    );
    const rects = container.querySelectorAll("rect");
    rects.forEach((rect) => {
      const y = Number(rect.getAttribute("y"));
      expect(Number.isFinite(y)).toBe(true);
    });
  });

  it("renders the last close as the current-price marker", () => {
    render(
      <CandleChart
        candles={[candle({ close: "81181.20" })]}
        timeframe="1h"
        symbol="BTCUSDT"
      />,
    );
    expect(screen.getByText("81,181.20")).toBeInTheDocument();
  });
});
