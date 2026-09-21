import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import TestnetPage from "./page";
import * as api from "@/lib/api";
import type { TestnetStatus } from "@/lib/types";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));

/**
 * The testnet desk shows a real venue's state, so the thing worth testing is
 * what it does when the venue says nothing. Every assertion below is about a
 * missing value being reported as missing rather than rendered as a number --
 * because a fabricated zero on this page is indistinguishable from a real one.
 */

function status(overrides: Partial<TestnetStatus> = {}): TestnetStatus {
  return {
    enabled: true,
    connection: "CONNECTED",
    venue: "binance-futures-usdm-testnet",
    detail: null,
    account: {
      account_id: "testnet",
      position_mode: "ONE_WAY",
      can_trade: true,
      available_balance: "7397.50939323",
    },
    positions: [],
    open_orders: [],
    margin_mode: "ISOLATED",
    observed_at: "2026-09-21T20:11:41.403237+00:00",
    ...overrides,
  };
}

afterEach(() => vi.restoreAllMocks());

describe("Testnet desk", () => {
  it("is permanently labelled as the demo venue, not as live trading", async () => {
    vi.spyOn(api, "getTestnetStatus").mockResolvedValue(status());
    render(<TestnetPage />);

    expect(await screen.findByText(/TESTNET . BINANCE DEMO/i)).toBeInTheDocument();
    expect(screen.getByText(/SIMULATION ENVIRONMENT/i)).toBeInTheDocument();
    expect(screen.getByText(/NO REAL FUNDS/i)).toBeInTheDocument();
  });

  it("shows the venue's balance and never a paper balance", async () => {
    vi.spyOn(api, "getTestnetStatus").mockResolvedValue(status());
    render(<TestnetPage />);

    expect(await screen.findByText(/7,?397/)).toBeInTheDocument();
    // 100 USDT is the paper starting balance. It must never appear here.
    expect(screen.queryByText(/^100\.00 USDT$/)).not.toBeInTheDocument();
  });

  it("reports an unknown trading permission as unknown, not as permitted", async () => {
    vi.spyOn(api, "getTestnetStatus").mockResolvedValue(
      status({ account: { ...status().account!, can_trade: null } }),
    );
    render(<TestnetPage />);

    expect(await screen.findByText("Unknown")).toBeInTheDocument();
    expect(screen.queryByText("Permitted")).not.toBeInTheDocument();
  });

  it("disables order entry until the backend confirms a tradable account", async () => {
    vi.spyOn(api, "getTestnetStatus").mockResolvedValue(
      status({ account: { ...status().account!, can_trade: false } }),
    );
    render(<TestnetPage />);

    const button = await screen.findByRole("button", { name: /submit demo order/i });
    expect(button).toBeDisabled();
  });

  it("renders the backend's reason when testnet is not enabled", async () => {
    vi.spyOn(api, "getTestnetStatus").mockResolvedValue(
      status({
        enabled: false,
        connection: "DISABLED",
        account: null,
        margin_mode: null,
        detail: "Testnet execution is not configured for this deployment.",
      }),
    );
    render(<TestnetPage />);

    expect(await screen.findByText(/Testnet — Unavailable/i)).toBeInTheDocument();
    expect(screen.getByText(/not configured for this deployment/i)).toBeInTheDocument();
    // No order entry surface at all when the venue is off.
    expect(screen.queryByRole("button", { name: /submit demo order/i })).not.toBeInTheDocument();
  });

  it("does not claim a connection the backend did not confirm", async () => {
    vi.spyOn(api, "getTestnetStatus").mockResolvedValue(
      status({ connection: "UNAVAILABLE", account: null, detail: "The venue could not be reached." }),
    );
    const { container } = render(<TestnetPage />);

    // Targeted at the connection badge specifically. "Unavailable" also appears
    // wherever the venue supplied no value, and matching those too would let
    // this pass even if the badge said Connected.
    await waitFor(() => {
      const badge = container.querySelector(".tn-badge");
      expect(badge?.textContent).toBe("Unavailable");
    });
    expect(screen.queryByText("Connected")).not.toBeInTheDocument();
  });

  it("shows a missing mark price and PnL as unavailable rather than zero", async () => {
    vi.spyOn(api, "getTestnetStatus").mockResolvedValue(
      status({
        positions: [
          {
            symbol: "AVAUSDT",
            quantity: "-41.8",
            entry_price: "0.2396",
            mark_price: null,
            unrealized_pnl: null,
            leverage: "20",
            margin_mode: "CROSSED",
          },
        ],
      }),
    );
    render(<TestnetPage />);

    expect(await screen.findByText("AVAUSDT")).toBeInTheDocument();
    expect(screen.getByText("SHORT")).toBeInTheDocument();
    // An em dash, not "0.00": the venue did not report these.
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText("0.00")).not.toBeInTheDocument();
  });

  it("says the account holds nothing rather than inventing a position", async () => {
    vi.spyOn(api, "getTestnetStatus").mockResolvedValue(status());
    render(<TestnetPage />);
    expect(await screen.findByText(/No open positions/i)).toBeInTheDocument();
  });

  it("surfaces a partial read instead of presenting it as complete", async () => {
    vi.spyOn(api, "getTestnetStatus").mockResolvedValue(
      status({ detail: "Connected, but positions could not be read." }),
    );
    render(<TestnetPage />);
    expect(await screen.findByText(/positions could not be read/i)).toBeInTheDocument();
  });

  it("shows a loading state before the backend has answered", () => {
    vi.spyOn(api, "getTestnetStatus").mockReturnValue(new Promise(() => {}));
    render(<TestnetPage />);
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("renders a refusal as prominently as an acceptance", async () => {
    vi.spyOn(api, "getTestnetStatus").mockResolvedValue(status());
    vi.spyOn(api, "submitTestnetOrder").mockResolvedValue({
      accepted: false,
      detail: "The venue applied 10x where the risk engine approved 3x.",
      replayed: false,
      order_id: null,
      client_order_id: null,
      venue_order_id: null,
      state: null,
      rejection_code: "RISK_REJECTED_MAX_LEVERAGE",
      rejection_detail: null,
      venue_leverage: null,
      venue_margin_mode: null,
      checks_performed: [],
    });

    render(<TestnetPage />);
    const button = await screen.findByRole("button", { name: /submit demo order/i });
    await waitFor(() => expect(button).toBeEnabled());
    button.click();

    expect(await screen.findByText("Refused")).toBeInTheDocument();
    expect(screen.getByText("RISK_REJECTED_MAX_LEVERAGE")).toBeInTheDocument();
  });
});
