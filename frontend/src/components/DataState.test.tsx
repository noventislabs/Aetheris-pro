import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { EmptyState, ErrorState, LoadingState, UnavailableState } from "./DataState";
import { FreshnessBadge, StatusPill } from "./Freshness";
import { ApiError } from "@/lib/api";

describe("LoadingState", () => {
  it("announces itself to assistive technology", () => {
    render(<LoadingState label="Loading candles" />);
    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(screen.getByText("LOADING CANDLES")).toBeInTheDocument();
  });
});

describe("ErrorState", () => {
  it("shows the backend error code and message", () => {
    render(
      <ErrorState error={new ApiError("Exchange is unavailable", "EXCHANGE_UNAVAILABLE", 503, "req-1")} />,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByText("EXCHANGE_UNAVAILABLE")).toBeInTheDocument();
    expect(screen.getByText("Exchange is unavailable")).toBeInTheDocument();
    expect(screen.getByText(/req-1/)).toBeInTheDocument();
  });

  it("offers a retry when one is possible", async () => {
    const user = userEvent.setup();
    const onRetry = vi.fn();
    render(<ErrorState error={new ApiError("boom", "X", 500)} onRetry={onRetry} />);
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalled();
  });
});

describe("UnavailableState", () => {
  it("renders the backend's own reason verbatim", () => {
    // The difference between "no data" and "this instrument last traded 40
    // minutes ago" is the whole point.
    render(
      <UnavailableState
        status="STALE"
        detail="Most recent 1h candle closed 25000s ago (limit 9000s)"
      />,
    );
    expect(screen.getByText("STALE")).toBeInTheDocument();
    expect(screen.getByText(/closed 25000s ago/)).toBeInTheDocument();
  });
});

describe("EmptyState", () => {
  it("distinguishes an empty result from a broken feed", () => {
    render(
      <EmptyState
        message='No instrument matches "ZZZ".'
        detail="528 instruments were discovered, so the feed is working."
      />,
    );
    expect(screen.getByText("NO RESULTS")).toBeInTheDocument();
    expect(screen.getByText(/528 instruments were discovered/)).toBeInTheDocument();
  });
});

describe("FreshnessBadge", () => {
  it("shows status, source and age", () => {
    render(
      <FreshnessBadge status="OK" source="binance-futures-usdm:rest" ageSeconds={3.1} />,
    );
    expect(screen.getByText("OK")).toBeInTheDocument();
    expect(screen.getByText("binance-futures-usdm:rest")).toBeInTheDocument();
    expect(screen.getByText("3s ago")).toBeInTheDocument();
  });

  it("does not present an unverifiable age as fresh", () => {
    // OK + age null means the venue supplied no timestamp. Phase 2 rule: the
    // UI must not read that as guaranteed fresh.
    const { container } = render(
      <FreshnessBadge status="OK" source="venue:rest" ageSeconds={null} />,
    );
    expect(screen.getByText("age unverified")).toBeInTheDocument();
    expect(container.querySelector(".dot-ok")).toBeNull();
    expect(container.querySelector(".dot-unknown")).not.toBeNull();
  });

  it("marks a forming candle rather than a negative age", () => {
    render(<FreshnessBadge status="OK" source="venue:rest" ageSeconds={-3383} />);
    expect(screen.getByText("forming")).toBeInTheDocument();
  });

  it("flags stale data visually", () => {
    const { container } = render(
      <FreshnessBadge status="STALE" source="venue:rest" ageSeconds={900} />,
    );
    expect(container.querySelector(".dot-stale")).not.toBeNull();
  });
});

describe("StatusPill", () => {
  it("stays out of the way when data is fine", () => {
    const { container } = render(<StatusPill status="OK" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("marks anything else", () => {
    render(<StatusPill status="UNAVAILABLE" />);
    expect(screen.getByText("UNAVAILABLE")).toBeInTheDocument();
  });
});
