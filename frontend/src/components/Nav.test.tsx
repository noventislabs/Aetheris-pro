import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Nav } from "./Nav";
import * as api from "@/lib/api";
import type { ModeState, SystemStatus, TradingModeName } from "@/lib/types";

vi.mock("next/navigation", () => ({ usePathname: () => "/markets" }));

/**
 * Navigation is where a user first learns what this build can do, so the rule
 * it has to keep is that nothing appears clickable unless the backend has said
 * it works. Testnet availability is a property of the server -- its
 * credentials, its database -- not of the bundle the browser downloaded.
 */

function mode(name: TradingModeName, enabled: boolean): ModeState {
  return {
    mode: name,
    enabled,
    places_real_orders: name === "TESTNET" || name === "LIVE",
    risks_real_funds: name === "LIVE",
  };
}

function systemStatus(testnetEnabled: boolean): SystemStatus {
  return {
    name: "Aetheris Pro",
    version: "0.1.0",
    environment: "test",
    server_time: "2026-09-21T20:00:00Z",
    default_mode: "ANALYSIS",
    autonomous_trading_enabled: false,
    modes: [
      mode("ANALYSIS", true),
      mode("PAPER", true),
      mode("TESTNET", testnetEnabled),
      mode("LIVE", false),
    ],
  };
}

afterEach(() => vi.restoreAllMocks());

describe("Nav", () => {
  it("keeps the existing sections working", async () => {
    vi.spyOn(api, "getSystemStatus").mockResolvedValue(systemStatus(false));
    render(<Nav />);

    for (const label of ["Markets", "Scanner", "Backtest", "Paper Trading"]) {
      expect(screen.getByRole("link", { name: new RegExp(label, "i") })).toBeInTheDocument();
    }
  });

  it("links Testnet only when the backend reports it enabled", async () => {
    vi.spyOn(api, "getSystemStatus").mockResolvedValue(systemStatus(true));
    render(<Nav />);

    const link = await screen.findByRole("link", { name: /testnet \/ demo/i });
    expect(link).toHaveAttribute("href", "/testnet");
    expect(screen.getByText("DEMO")).toBeInTheDocument();
  });

  it("does not offer Testnet when the backend reports it disabled", async () => {
    vi.spyOn(api, "getSystemStatus").mockResolvedValue(systemStatus(false));
    render(<Nav />);

    expect(await screen.findByText("UNAVAILABLE")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /testnet \/ demo/i })).not.toBeInTheDocument();
  });

  it("does not offer Testnet before the backend has answered", () => {
    vi.spyOn(api, "getSystemStatus").mockReturnValue(new Promise(() => {}));
    render(<Nav />);

    // Unknown is not permission. The item exists, and it is not a link.
    expect(screen.getByText("CHECKING")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /testnet \/ demo/i })).not.toBeInTheDocument();
  });

  it("does not offer Testnet when the backend cannot be reached at all", async () => {
    vi.spyOn(api, "getSystemStatus").mockRejectedValue(new Error("network down"));
    render(<Nav />);

    expect(screen.queryByRole("link", { name: /testnet \/ demo/i })).not.toBeInTheDocument();
  });

  it("shows LIVE as locked and never as a destination", async () => {
    vi.spyOn(api, "getSystemStatus").mockResolvedValue(systemStatus(true));
    render(<Nav />);

    expect(await screen.findByText("LOCKED")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /^live$/i })).not.toBeInTheDocument();
  });

  it("keeps unbuilt sections visible but disabled", async () => {
    vi.spyOn(api, "getSystemStatus").mockResolvedValue(systemStatus(true));
    render(<Nav />);

    expect(screen.getAllByText("PLANNED")).toHaveLength(3);
    for (const label of ["Falcon", "Watchlist", "Account"]) {
      expect(screen.queryByRole("link", { name: new RegExp(`^${label}$`, "i") })).toBeNull();
    }
  });
});
