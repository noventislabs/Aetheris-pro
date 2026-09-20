import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SymbolSearch } from "./SymbolSearch";
import * as api from "@/lib/api";
import type { SymbolInfo } from "@/lib/types";

function symbol(name: string, base: string): SymbolInfo {
  return {
    symbol: name,
    base_asset: base,
    quote_asset: "USDT",
    status: "TRADING",
    contract_type: "PERPETUAL",
    price_precision: 2,
    quantity_precision: 3,
    filters: { tick_size: "0.10", step_size: "0.001", min_quantity: "0.001", min_notional: "100" },
    max_leverage: null,
  };
}

const UNIVERSE = [symbol("BTCUSDT", "BTC"), symbol("ETHUSDT", "ETH"), symbol("0GUSDT", "0G")];

function stubSymbols(symbols: SymbolInfo[] = UNIVERSE) {
  return vi
    .spyOn(api, "getSymbols")
    .mockResolvedValue({ count: symbols.length, eligible_only: true, symbols });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SymbolSearch", () => {
  it("lists instruments from the backend, not a bundled list", async () => {
    const user = userEvent.setup();
    const spy = stubSymbols();
    render(<SymbolSearch value="BTCUSDT" onSelect={vi.fn()} />);

    await user.click(screen.getByRole("combobox"));
    await waitFor(() => expect(screen.getByText("0GUSDT")).toBeInTheDocument());
    expect(spy).toHaveBeenCalled();
  });

  it('focuses on "/" from anywhere on the page', async () => {
    const user = userEvent.setup();
    stubSymbols();
    render(<SymbolSearch value="BTCUSDT" onSelect={vi.fn()} />);

    expect(screen.getByRole("combobox")).not.toHaveFocus();
    await user.keyboard("/");
    expect(screen.getByRole("combobox")).toHaveFocus();
  });

  it('does not hijack "/" while typing in a field', async () => {
    const user = userEvent.setup();
    stubSymbols();
    render(
      <>
        <input aria-label="other field" />
        <SymbolSearch value="BTCUSDT" onSelect={vi.fn()} />
      </>,
    );

    const other = screen.getByLabelText("other field");
    await user.click(other);
    await user.keyboard("a/b");
    expect(other).toHaveValue("a/b");
    expect(screen.getByRole("combobox")).not.toHaveFocus();
  });

  it("selects the active option on Enter", async () => {
    const user = userEvent.setup();
    stubSymbols();
    const onSelect = vi.fn();
    render(<SymbolSearch value="BTCUSDT" onSelect={onSelect} />);

    await user.click(screen.getByRole("combobox"));
    await waitFor(() => expect(screen.getByText("BTCUSDT")).toBeInTheDocument());
    await user.keyboard("{Enter}");
    expect(onSelect).toHaveBeenCalledWith("BTCUSDT");
  });

  it("moves through options with the arrow keys", async () => {
    const user = userEvent.setup();
    stubSymbols();
    const onSelect = vi.fn();
    render(<SymbolSearch value="BTCUSDT" onSelect={onSelect} />);

    await user.click(screen.getByRole("combobox"));
    await waitFor(() => expect(screen.getByText("ETHUSDT")).toBeInTheDocument());
    await user.keyboard("{ArrowDown}{Enter}");
    expect(onSelect).toHaveBeenCalledWith("ETHUSDT");
  });

  it("closes on Escape without selecting", async () => {
    const user = userEvent.setup();
    stubSymbols();
    const onSelect = vi.fn();
    render(<SymbolSearch value="BTCUSDT" onSelect={onSelect} />);

    await user.click(screen.getByRole("combobox"));
    await waitFor(() => expect(screen.getByRole("listbox")).toBeInTheDocument());
    await user.keyboard("{Escape}");

    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("selects with the pointer", async () => {
    const user = userEvent.setup();
    stubSymbols();
    const onSelect = vi.fn();
    render(<SymbolSearch value="BTCUSDT" onSelect={onSelect} />);

    await user.click(screen.getByRole("combobox"));
    await waitFor(() => expect(screen.getByText("0GUSDT")).toBeInTheDocument());
    await user.click(screen.getByText("0GUSDT"));
    expect(onSelect).toHaveBeenCalledWith("0GUSDT");
  });

  it("reports a failed lookup instead of showing an empty list", async () => {
    // An empty dropdown reads as "no such symbol", which is a different and
    // misleading claim from "the lookup failed".
    const user = userEvent.setup();
    vi.spyOn(api, "getSymbols").mockRejectedValue(
      new api.ApiError("down", "EXCHANGE_UNAVAILABLE", 503),
    );
    render(<SymbolSearch value="BTCUSDT" onSelect={vi.fn()} />);

    await user.click(screen.getByRole("combobox"));
    await waitFor(() =>
      expect(screen.getByText("Could not load instruments")).toBeInTheDocument(),
    );
  });

  it("says when nothing matches a query", async () => {
    const user = userEvent.setup();
    stubSymbols([]);
    render(<SymbolSearch value="BTCUSDT" onSelect={vi.fn()} />);

    await user.click(screen.getByRole("combobox"));
    await user.keyboard("ZZZ");
    await waitFor(() =>
      expect(screen.getByText('No instrument matches "ZZZ"')).toBeInTheDocument(),
    );
  });
});
