import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import PaperPage from "./page";
import * as api from "@/lib/api";
import type {
  PaperAccount,
  PaperMethod,
  PaperOrder,
  PaperOrderResult,
  PaperPosition,
  PaperTrade,
} from "@/lib/types";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));

function order(overrides: Partial<PaperOrder> = {}): PaperOrder {
  return {
    order_id: "paper-order-1",
    client_order_id: "paper-1",
    symbol: "BTCUSDT",
    side: "BUY",
    order_type: "MARKET",
    state: "FILLED",
    reduce_only: false,
    requested_quantity: "0.00025000",
    filled_quantity: "0.00025000",
    average_fill_price: "80010.00000000",
    fills: [
      {
        fill_id: "paper-fill-1",
        price: "80010.00000000",
        quantity: "0.00025000",
        fee: "0.01000000",
        filled_at: "2026-09-21T12:00:00Z",
        price_source: "binance-futures-usdm:rest:best_ask",
        price_age_seconds: 1.2,
      },
    ],
    leverage: {
      requested_leverage: "1",
      exchange_max_leverage: null,
      risk_max_leverage: null,
      approved_leverage: "1",
      outcome: "APPROVED",
      reason: "LEVERAGE_APPROVED_AT_DOMAIN_MINIMUM",
      detail: "1x approved at the domain minimum. 1x is unlevered exposure.",
      binding_constraint: "domain_minimum",
      basis: "Caller-supplied leverage request",
      inputs: { requested_leverage: "1" },
      note: "Risk Engine has final authority",
    },
    created_at: "2026-09-21T12:00:00Z",
    updated_at: "2026-09-21T12:00:00Z",
    rejection_code: null,
    rejection_detail: null,
    idempotent_replay: false,
    ...overrides,
  };
}

function position(overrides: Partial<PaperPosition> = {}): PaperPosition {
  return {
    position_id: "paper-position-1",
    symbol: "BTCUSDT",
    side: "LONG",
    quantity: "0.00025000",
    entry_price: "80010.00000000",
    notional: "20.00250000",
    margin: "20.00250000",
    approved_leverage: "1",
    entry_fee: "0.01000000",
    stop_price: "78409.80000000",
    target_price: "83210.40000000",
    trailing_stop_percent: null,
    trail_extreme: null,
    liquidation_price: null,
    opened_at: "2026-09-21T12:00:00Z",
    updated_at: "2026-09-21T12:00:05Z",
    opening_order_id: "paper-order-1",
    mark_price: "80100.00000000",
    mark_source: "binance-futures-usdm:rest",
    mark_status: "OK",
    unrealized_pnl: "0.02250000",
    ...overrides,
  };
}

function trade(overrides: Partial<PaperTrade> = {}): PaperTrade {
  return {
    trade_id: "paper-trade-1",
    symbol: "BTCUSDT",
    side: "LONG",
    quantity: "0.00025000",
    entry_price: "80010.00000000",
    exit_price: "83210.00000000",
    notional: "20.00250000",
    margin: "20.00250000",
    approved_leverage: "1",
    opened_at: "2026-09-21T12:00:00Z",
    closed_at: "2026-09-21T12:30:00Z",
    exit_reason: "TAKE_PROFIT",
    gross_pnl: "0.80000000",
    fees: "0.02040000",
    net_pnl: "0.77960000",
    return_percent: "3.89750000",
    balance_after: "100.77960000",
    ...overrides,
  };
}

function account(overrides: Partial<PaperAccount> = {}): PaperAccount {
  return {
    account_id: "paper-default",
    mode: "PAPER",
    created_at: "2026-09-21T11:00:00Z",
    updated_at: "2026-09-21T12:00:05Z",
    starting_balance: "100",
    balance: "99.99000000",
    equity: "100.01250000",
    available_balance: "79.98750000",
    margin_used: "20.00250000",
    realized_pnl: "-0.01000000",
    unrealized_pnl: "0.02250000",
    total_fees: "0.01000000",
    positions: [position()],
    open_orders: [],
    recent_orders: [order()],
    recent_trades: [],
    session: {
      session_date: "2026-09-21",
      realized_pnl: "-0.01000000",
      fees: "0.01000000",
      trades_closed: 0,
      orders_submitted: 1,
      orders_rejected: 0,
      profit_target: "20",
      loss_limit: "-10",
      lock_state: "NONE",
      lock_reason: null,
      locked_at: null,
    },
    autonomous_enabled: false,
    durability: "IN_MEMORY",
    durability_notice:
      "PAPER STATE: IN-MEMORY - RESETS ON RESTART. Balances, positions, orders and history exist only in this process.",
    mark_source: "binance-futures-usdm:rest",
    mark_status: "OK",
    mark_age_seconds: 1.2,
    label: "PAPER / SIMULATION ONLY / NO REAL ORDER",
    disclaimer:
      "Paper trading is a simulation running against real public market data. No order is sent to any exchange.",
    ...overrides,
  };
}

const METHOD: PaperMethod = {
  label: "PAPER / SIMULATION ONLY / NO REAL ORDER",
  mode: "PAPER",
  assumptions: [
    "SIMULATION ONLY: no order is sent to any exchange, no API credential exists.",
    "Position management is poll-driven: stops fill at the price observed then.",
  ],
  not_modelled: ["Funding payments on perpetual positions", "Partial fills"],
  rejection_codes: ["RISK_REJECTED_STALE_DATA"],
  durability: "IN_MEMORY",
  durability_notice: "PAPER STATE: IN-MEMORY - RESETS ON RESTART.",
  disclaimer:
    "Paper trading is a simulation. It is NOT testnet trading and NOT live trading.",
};

function result(overrides: Partial<PaperOrderResult> = {}): PaperOrderResult {
  return {
    accepted: true,
    order: order(),
    position: position(),
    trade: null,
    account: account(),
    detail: "PAPER order filled: 0.00025 BTCUSDT at 80010. Simulation only.",
    ...overrides,
  };
}

function stub(initial: PaperAccount = account()) {
  vi.spyOn(api, "getSymbols").mockResolvedValue({
    count: 1,
    eligible_only: true,
    symbols: [],
  });
  vi.spyOn(api, "getPaperMethod").mockResolvedValue(METHOD);
  vi.spyOn(api, "getPaperAccount").mockResolvedValue(initial);
  vi.spyOn(api, "tickPaper").mockResolvedValue({
    account: initial,
    closed_trades: [],
    detail: "No position reached a stop, target or liquidation level.",
  });
}

beforeEach(() => {
  vi.useRealTimers();
});

afterEach(() => {
  vi.restoreAllMocks();
});

async function renderDesk(initial: PaperAccount = account()) {
  stub(initial);
  render(<PaperPage />);
  await screen.findByText("Equity");
}

describe("PaperPage", () => {
  it("labels itself a simulation that places no real order", async () => {
    await renderDesk();
    expect(
      screen.getByText("PAPER / SIMULATION ONLY / NO REAL ORDER"),
    ).toBeInTheDocument();
  });

  it("warns that state is in memory and resets on restart", async () => {
    // A balance that silently resets is worse than one the user knows resets.
    await renderDesk();
    expect(screen.getByText(/PAPER STATE: IN-MEMORY/)).toBeInTheDocument();
    expect(screen.getByText(/lost when it\s+stops/)).toBeInTheDocument();
  });

  it("says management only runs while the page is open", async () => {
    await renderDesk();
    expect(
      screen.getByText(/Management runs only while this page is open/),
    ).toBeInTheDocument();
    expect(screen.getByText(/no\s+server-side loop/)).toBeInTheDocument();
  });

  it("shows the account figures", async () => {
    await renderDesk();
    expect(screen.getByText("100.0125")).toBeInTheDocument(); // equity
    expect(screen.getByText("79.9875")).toBeInTheDocument(); // available
  });

  it("reports an unmarkable position as unknown rather than zero", async () => {
    await renderDesk(
      account({
        unrealized_pnl: null,
        positions: [position({ mark_price: null, unrealized_pnl: null, mark_status: "STALE" })],
      }),
    );
    const dashes = screen.getAllByText("—");
    expect(dashes.length).toBeGreaterThanOrEqual(2);
    expect(
      screen.getByTitle(/could not be marked to a usable price. Not zero/),
    ).toBeInTheDocument();
  });

  it("renders an open position with its levels", async () => {
    await renderDesk();
    const positions = screen.getAllByRole("table")[0]!;
    expect(within(positions).getByText("LONG")).toBeInTheDocument();
    expect(within(positions).getByText("78,409.80")).toBeInTheDocument();
    expect(within(positions).getByText("83,210.40")).toBeInTheDocument();
    expect(within(positions).getByText("80,100.00")).toBeInTheDocument();
  });

  it("shows no liquidation price at 1x long rather than zero", async () => {
    await renderDesk();
    expect(
      screen.getByTitle(/a level of zero is not a liquidation price/),
    ).toBeInTheDocument();
  });

  it("submits an order and reports the fill with its price source", async () => {
    stub();
    const spy = vi.spyOn(api, "submitPaperOrder").mockResolvedValue(result());
    const user = userEvent.setup();
    render(<PaperPage />);
    await screen.findByText("Equity");

    await user.click(screen.getByRole("button", { name: "Submit paper order" }));
    await waitFor(() => expect(spy).toHaveBeenCalled());

    expect(spy.mock.calls[0]![0]!.leverage).toBe("1");
    // The outcome panel carries its own FILLED badge alongside the one already
    // in the order history, so both must be present.
    expect(await screen.findByText(/best_ask/)).toBeInTheDocument();
    expect(screen.getAllByText("FILLED").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText(/PAPER order filled/)).toBeInTheDocument();
  });

  it("renders a refusal as prominently as a fill, with its code", async () => {
    // A risk limit that fires silently teaches a user that limits do not exist.
    stub();
    vi.spyOn(api, "submitPaperOrder").mockResolvedValue(
      result({
        accepted: false,
        position: null,
        detail:
          "RISK_REJECTED_DAILY_LOSS_LIMIT: Daily loss limit of -10 reached. No new entries today.",
        order: order({
          state: "REJECTED",
          rejection_code: "RISK_REJECTED_DAILY_LOSS_LIMIT",
          rejection_detail: "Daily loss limit of -10 reached.",
          fills: [],
          average_fill_price: null,
        }),
      }),
    );
    const user = userEvent.setup();
    render(<PaperPage />);
    await screen.findByText("Equity");
    await user.click(screen.getByRole("button", { name: "Submit paper order" }));

    expect(
      await screen.findAllByText("RISK_REJECTED_DAILY_LOSS_LIMIT"),
    ).not.toHaveLength(0);
    expect(screen.getByText(/No new entries today/)).toBeInTheDocument();
  });

  it("shows the whole leverage chain on a refusal, not just the verdict", async () => {
    stub();
    vi.spyOn(api, "submitPaperOrder").mockResolvedValue(
      result({
        accepted: false,
        position: null,
        detail: "RISK_REJECTED_MAX_LEVERAGE: Leverage was not approved.",
        order: order({
          state: "REJECTED",
          rejection_code: "RISK_REJECTED_MAX_LEVERAGE",
          fills: [],
          leverage: {
            requested_leverage: "10",
            exchange_max_leverage: null,
            risk_max_leverage: null,
            approved_leverage: null,
            outcome: "REJECTED",
            reason: "RISK_REJECTED_EXCHANGE_MAX_LEVERAGE_UNKNOWN",
            detail: "The venue's maximum leverage for this symbol is unknown.",
            binding_constraint: null,
            basis: null,
            inputs: {},
            note: "Risk Engine has final authority",
          },
        }),
      }),
    );
    const user = userEvent.setup();
    render(<PaperPage />);
    await screen.findByText("Equity");
    await user.click(screen.getByRole("button", { name: "Submit paper order" }));

    const chain = await screen.findByText(/Leverage requested 10x/);
    expect(chain).toHaveTextContent("venue ceiling UNKNOWN");
    expect(chain).toHaveTextContent("risk ceiling UNKNOWN");
  });

  it("describes leverage as a request, never an authorisation", async () => {
    await renderDesk();
    expect(
      screen.getByTitle(/A request, not an authorisation/),
    ).toBeInTheDocument();
  });

  it("surfaces the daily lock and says positions are still managed", async () => {
    await renderDesk(
      account({
        session: {
          ...account().session,
          lock_state: "DAILY_LOSS_LIMIT",
          lock_reason: "Daily loss limit of -10 USDT reached.",
        },
      }),
    );
    expect(screen.getByText("DAILY LOSS LIMIT")).toBeInTheDocument();
    expect(screen.getByText(/never force-closed/)).toBeInTheDocument();
  });

  it("closes a position on request", async () => {
    stub();
    const spy = vi
      .spyOn(api, "closePaperPosition")
      .mockResolvedValue(
        result({ position: null, trade: trade(), account: account({ positions: [] }) }),
      );
    const user = userEvent.setup();
    render(<PaperPage />);
    await screen.findByText("Equity");

    await user.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(spy).toHaveBeenCalledWith("BTCUSDT"));
    expect(await screen.findByText("FLAT")).toBeInTheDocument();
  });

  it("shows no quantity for a refused order rather than zero", async () => {
    // "0" reads as an order for nothing; the size was never resolved at all.
    await renderDesk(
      account({
        positions: [],
        recent_orders: [
          order({
            state: "REJECTED",
            rejection_code: "RISK_REJECTED_MIN_NOTIONAL",
            rejection_detail: "Notional is below the venue's published minimum.",
            filled_quantity: "0.00000001",
            average_fill_price: null,
            fills: [],
          }),
        ],
      }),
    );
    const history = screen.getAllByRole("table").at(-1)!;
    expect(within(history).getByText("REJECTED")).toBeInTheDocument();
    expect(within(history).queryByText("0.00000001")).not.toBeInTheDocument();
  });

  it("lists closed trades with their exit reason", async () => {
    await renderDesk(account({ positions: [], recent_trades: [trade()] }));
    // Twice: once in the table, once in the card fallback for narrow screens.
    expect(screen.getAllByText("TAKE_PROFIT")).toHaveLength(2);
    expect(screen.getAllByText("0.7796").length).toBeGreaterThanOrEqual(1);
  });

  it("resets the account on request", async () => {
    stub();
    const spy = vi
      .spyOn(api, "resetPaperAccount")
      .mockResolvedValue(account({ positions: [], recent_orders: [], balance: "100" }));
    const user = userEvent.setup();
    render(<PaperPage />);
    await screen.findByText("Equity");

    await user.click(screen.getByRole("button", { name: "Reset account" }));
    await waitFor(() => expect(spy).toHaveBeenCalled());
  });

  it("engages the emergency stop", async () => {
    stub();
    const spy = vi.spyOn(api, "setPaperEmergencyStop").mockResolvedValue(account());
    const user = userEvent.setup();
    render(<PaperPage />);
    await screen.findByText("Equity");

    await user.click(screen.getByRole("button", { name: "Emergency stop" }));
    await waitFor(() => expect(spy).toHaveBeenCalledWith(true, expect.any(String)));
  });

  it("publishes what the simulation does not model", async () => {
    await renderDesk();
    expect(await screen.findByText(/NOT testnet trading/)).toBeInTheDocument();
    expect(screen.getByText(/poll-driven/)).toBeInTheDocument();
    expect(screen.getByText(/Funding payments/)).toBeInTheDocument();
  });

  it("renders card fallbacks beside every table", async () => {
    // Below 720px the stylesheet hides .table-scroll and shows .cards, so a
    // table with no card beside it renders nothing at all on a phone. This is
    // the regression test for exactly that: found at 390px in a browser, where
    // the positions table had collapsed to zero height.
    stub(account({ recent_trades: [trade()] }));
    const { container } = render(<PaperPage />);
    await screen.findByText("Equity");
    await waitFor(() => {
      const tables = container.querySelectorAll(".table-scroll");
      const cards = container.querySelectorAll(".cards");
      expect(tables.length).toBeGreaterThan(0);
      expect(cards.length).toBe(tables.length);
    });
  });

  it("surfaces a backend failure with its code", async () => {
    vi.spyOn(api, "getSymbols").mockResolvedValue({
      count: 0,
      eligible_only: true,
      symbols: [],
    });
    vi.spyOn(api, "getPaperMethod").mockResolvedValue(METHOD);
    vi.spyOn(api, "getPaperAccount").mockRejectedValue(
      new api.ApiError("Backend is unreachable", "NETWORK_UNREACHABLE", 0),
    );
    render(<PaperPage />);
    expect(await screen.findByText("NETWORK_UNREACHABLE")).toBeInTheDocument();
  });
});
