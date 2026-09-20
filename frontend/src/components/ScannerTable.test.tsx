import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ScannerTable } from "./ScannerTable";
import type { ScannerRow } from "@/lib/types";

function row(overrides: Partial<ScannerRow> = {}): ScannerRow {
  return {
    symbol: "BTCUSDT",
    base_asset: "BTC",
    quote_asset: "USDT",
    contract_type: "PERPETUAL",
    status: "TRADING",
    ticker_status: "OK",
    ticker_detail: null,
    last_price: "81242.40",
    bid_price: "81242.00",
    ask_price: "81242.50",
    price_change_24h: "38.20",
    price_change_percent_24h: "0.047",
    high_24h: "81472.60",
    low_24h: "80095.90",
    volume_24h: "95633.147",
    quote_volume_24h: "7749548640.96",
    metrics_status: "NOT_REQUESTED",
    metrics_detail: null,
    metrics: null,
    opportunity: null,
    ...overrides,
  };
}

const METRICS: NonNullable<ScannerRow["metrics"]> = {
  timeframe: "1h",
  candles_used: 59,
  window_return_percent: "5.0000",
  momentum_percent: "19.3649",
  volatility_percent: "1.2000",
  atr: "15.00000000",
  atr_percent: "1.9376",
  average_volume: "1000.00000000",
  last_volume: "16354.00000000",
  relative_volume: "16.3543",
  range_percent: "4.0000",
  body_percent: "55.0000",
  trend: "UP",
  trend_consistency: "0.5085",
};

const SCORE: NonNullable<ScannerRow["opportunity"]> = {
  score: "81.32",
  method: "market-opportunity/v1",
  components: [
    {
      name: "relative_volume",
      raw_value: "16.3543",
      normalized: "1.0000",
      weight: "0.30",
      contribution: "30.00",
      detail: "traded 16x its recent mean volume",
    },
  ],
};

function renderTable(rows: ScannerRow[], onSort = vi.fn()) {
  render(
    <ScannerTable rows={rows} sortBy="quote_volume_24h" direction="desc" onSort={onSort} />,
  );
  return onSort;
}

describe("ScannerTable", () => {
  it("renders the values the backend supplied", () => {
    renderTable([row()]);
    // Symbol appears in both the table and the card layout; both are real.
    expect(screen.getAllByText("BTCUSDT").length).toBeGreaterThan(0);
    expect(screen.getAllByText("81,242.40").length).toBeGreaterThan(0);
    expect(screen.getAllByText("+0.05%").length).toBeGreaterThan(0);
    expect(screen.getAllByText("7.75B").length).toBeGreaterThan(0);
  });

  it("shows a stale row with a status pill and no prices", () => {
    renderTable([
      row({
        ticker_status: "STALE",
        ticker_detail: "Last trade for this instrument was 4000s ago",
        last_price: null,
        price_change_percent_24h: null,
        quote_volume_24h: null,
      }),
    ]);
    expect(screen.getAllByText("STALE").length).toBeGreaterThan(0);
    // No fabricated zero stands in for the withheld price.
    expect(screen.queryByText("0.00")).not.toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("shows metric columns when metrics were calculated", () => {
    renderTable([row({ metrics_status: "CALCULATED", metrics: METRICS, opportunity: SCORE })]);
    expect(screen.getAllByText("1.94").length).toBeGreaterThan(0); // ATR %
    expect(screen.getAllByText("16.35").length).toBeGreaterThan(0); // relative volume
    expect(screen.getAllByText("81.3").length).toBeGreaterThan(0); // score
  });

  it("explains an absent score instead of showing a dash", () => {
    renderTable([
      row({
        metrics_status: "INSUFFICIENT_DATA",
        metrics_detail: "Not enough closed candles to compute statistics",
      }),
    ]);
    expect(screen.getAllByText("Not enough closed candles").length).toBeGreaterThan(0);
  });

  it("labels a score with its components and a disclaimer", () => {
    renderTable([row({ metrics_status: "CALCULATED", metrics: METRICS, opportunity: SCORE })]);
    const scored = screen.getAllByTitle(/Market Opportunity Score/)[0];
    expect(scored).toBeDefined();
    const title = scored?.getAttribute("title") ?? "";
    expect(title).toContain("relative_volume: 30.00");
    expect(title).toContain("NOT a probability of profit");
  });

  it("requests a sort when a column header is activated", async () => {
    const user = userEvent.setup();
    const onSort = renderTable([row()]);
    const header = screen.getByRole("columnheader", { name: /Volatility/ });
    await user.click(within(header).getByRole("button"));
    expect(onSort).toHaveBeenCalledWith("volatility_percent");
  });

  it("marks the active sort column for assistive technology", () => {
    renderTable([row()]);
    expect(screen.getByRole("columnheader", { name: /Quote Vol/ })).toHaveAttribute(
      "aria-sort",
      "descending",
    );
  });

  it("renders every row it is given", () => {
    renderTable([row(), row({ symbol: "ETHUSDT" }), row({ symbol: "0GUSDT" })]);
    expect(screen.getAllByText("0GUSDT").length).toBeGreaterThan(0);
    expect(screen.getAllByText("ETHUSDT").length).toBeGreaterThan(0);
  });

  it("opens a symbol when its name is activated", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(
      <ScannerTable
        rows={[row()]}
        sortBy="quote_volume_24h"
        direction="desc"
        onSort={vi.fn()}
        onSelectSymbol={onSelect}
      />,
    );
    await user.click(screen.getAllByRole("button", { name: "BTCUSDT" })[0]!);
    expect(onSelect).toHaveBeenCalledWith("BTCUSDT");
  });
});
