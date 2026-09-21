import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AutonomyPanel } from "./AutonomyPanel";
import * as api from "@/lib/api";
import type { AutonomousDecision, AutonomousStatus } from "@/lib/types";

function status(overrides: Partial<AutonomousStatus> = {}): AutonomousStatus {
  return {
    state: "DISARMED",
    enabled: false,
    permitted_by_config: true,
    paper_mode_enabled: true,
    symbols: [],
    excluded_symbols: {},
    timeframe: "15m",
    interval_seconds: 30,
    iterations: 0,
    decisions_recorded: 0,
    entries: 0,
    refusals: 0,
    closes: 0,
    last_iteration_at: null,
    last_iteration_duration_seconds: null,
    failure_detail: null,
    venue_outage: false,
    disclaimer:
      "Autonomous paper trading is a simulation running against real public market data. No order is sent to any exchange, no API credential exists, and no real funds are involved. The risk engine has final authority over every proposal and nothing here can override it.",
    ...overrides,
  };
}

function decision(overrides: Partial<AutonomousDecision> = {}): AutonomousDecision {
  return {
    decision_id: "auto-decision-1",
    sequence: 1,
    decided_at: "2026-09-21T12:00:00Z",
    symbol: "AAAUSDT",
    timeframe: "15m",
    action: "NO_SIGNAL",
    detail:
      "The rule set reports NEUTRAL: the conditions for neither direction were met, or they conflicted. A finding, not a gap.",
    bar_close_time: "2026-09-21T11:59:59Z",
    strategy: "trend_momentum",
    strategy_status: "READY",
    bias: "NEUTRAL",
    conditions_met: 2,
    conditions_total: 4,
    rejection_code: null,
    rejection_detail: null,
    leverage: null,
    risk_max_leverage: null,
    checks_performed: [],
    side: null,
    position_side: null,
    exit_reason: null,
    client_order_id: null,
    order_id: null,
    position_id: null,
    trade_id: null,
    proposed_margin: null,
    realized_pnl: null,
    source: "binance-futures-usdm:rest",
    data_status: "OK",
    data_age_seconds: 1.2,
    atr_percent: "1.4300",
    label: "PAPER / AUTONOMOUS / SIMULATION ONLY / NO REAL ORDER",
    ...overrides,
  };
}

function stub(state: AutonomousStatus = status(), decisions: AutonomousDecision[] = []) {
  vi.spyOn(api, "getAutonomousStatus").mockResolvedValue(state);
  vi.spyOn(api, "getAutonomousDecisions").mockResolvedValue({
    count: decisions.length,
    decisions,
    detail: "Newest first. Bounded in memory and lost on restart, like all paper state.",
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("AutonomyPanel", () => {
  it("shows the loop as disarmed by default", async () => {
    stub();
    render(<AutonomyPanel />);
    expect(await screen.findByText("DISARMED")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Arm autonomous trading" })).toBeInTheDocument();
  });

  it("states that the loop proposes and never decides", async () => {
    stub();
    render(<AutonomyPanel />);
    expect(await screen.findByText(/proposes; it never decides/)).toBeInTheDocument();
    expect(screen.getByText(/disarmed on every restart/)).toBeInTheDocument();
    expect(screen.getByText(/sends no order to any exchange/)).toBeInTheDocument();
  });

  it("says plainly when no universe is configured", async () => {
    // It never picks instruments on its own, and the UI must not imply it might.
    stub();
    render(<AutonomyPanel />);
    expect(
      await screen.findByText(/never picks instruments on its own/),
    ).toBeInTheDocument();
  });

  it("disables arming when configuration forbids it", async () => {
    stub(status({ state: "DISABLED_BY_CONFIG", permitted_by_config: false }));
    render(<AutonomyPanel />);
    expect(await screen.findByText("DISABLED BY CONFIG")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Arm autonomous trading" })).toBeDisabled();
    expect(screen.getByText(/gates arming and never arms on its own/)).toBeInTheDocument();
  });

  it("arms on request", async () => {
    stub();
    const spy = vi.spyOn(api, "setAutonomous").mockResolvedValue(
      status({ state: "ARMED", enabled: true }),
    );
    const user = userEvent.setup();
    render(<AutonomyPanel />);
    await user.click(await screen.findByRole("button", { name: "Arm autonomous trading" }));
    await waitFor(() => expect(spy).toHaveBeenCalledWith(true));
  });

  it("reports a failed loop rather than letting it look idle", async () => {
    stub(
      status({
        state: "FAILED",
        failure_detail: "RuntimeError: iteration exploded",
      }),
    );
    render(<AutonomyPanel />);
    expect(await screen.findByText(/Loop failed/)).toBeInTheDocument();
    expect(screen.getByText(/RuntimeError: iteration exploded/)).toBeInTheDocument();
    expect(screen.getByText(/not restarted on its own/)).toBeInTheDocument();
  });

  it("reports a venue outage", async () => {
    stub(status({ venue_outage: true }));
    render(<AutonomyPanel />);
    expect(await screen.findByText(/Venue outage/)).toBeInTheDocument();
    expect(screen.getByText(/keeps managing open positions/)).toBeInTheDocument();
  });

  it("distinguishes an empty log from a loop that ran and found nothing", async () => {
    stub();
    render(<AutonomyPanel />);
    expect(await screen.findByText("NO DECISIONS")).toBeInTheDocument();
    expect(screen.getByText(/has not run/)).toBeInTheDocument();
  });

  it("renders idle decisions, not only the interesting ones", async () => {
    stub(status({ iterations: 3 }), [decision()]);
    render(<AutonomyPanel />);
    const table = (await screen.findAllByRole("table"))[0]!;
    expect(within(table).getByText("NO_SIGNAL")).toBeInTheDocument();
    expect(within(table).getByText("NEUTRAL")).toBeInTheDocument();
  });

  it("shows condition counts rather than a score", async () => {
    stub(status(), [decision({ conditions_met: 3, conditions_total: 4 })]);
    render(<AutonomyPanel />);
    const cells = await screen.findAllByText("3/4");
    expect(cells.length).toBeGreaterThan(0);
  });

  it("shows a refusal with its code", async () => {
    stub(status({ refusals: 1 }), [
      decision({
        action: "REFUSED",
        rejection_code: "RISK_REJECTED_MAX_LEVERAGE",
        detail: "Leverage was not approved: the venue's maximum is unknown.",
      }),
    ]);
    render(<AutonomyPanel />);
    const codes = await screen.findAllByText(/RISK_REJECTED_MAX_LEVERAGE/);
    expect(codes.length).toBeGreaterThan(0);
  });

  it("renders a card fallback beside the decision table", async () => {
    // Below 720px the stylesheet hides tables; a table with no card renders
    // nothing at all on a phone.
    stub(status(), [decision()]);
    const { container } = render(<AutonomyPanel />);
    await screen.findAllByRole("table");
    expect(container.querySelectorAll(".table-scroll")).toHaveLength(1);
    expect(container.querySelectorAll(".cards")).toHaveLength(1);
  });

  it("surfaces a backend failure with its code", async () => {
    vi.spyOn(api, "getAutonomousStatus").mockRejectedValue(
      new api.ApiError("Backend is unreachable", "NETWORK_UNREACHABLE", 0),
    );
    vi.spyOn(api, "getAutonomousDecisions").mockRejectedValue(
      new api.ApiError("Backend is unreachable", "NETWORK_UNREACHABLE", 0),
    );
    render(<AutonomyPanel />);
    expect(await screen.findByText("NETWORK_UNREACHABLE")).toBeInTheDocument();
  });
});
