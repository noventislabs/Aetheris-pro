import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, getKlines, getScan, getTicker } from "./api";

/**
 * The API client's job at the boundary: turn anything unexpected into a typed
 * error, and never let a malformed payload through as if it were data.
 */

function mockFetch(response: Partial<Response> & { jsonBody?: unknown }) {
  const stub = vi.fn().mockResolvedValue({
    ok: response.ok ?? true,
    status: response.status ?? 200,
    statusText: response.statusText ?? "OK",
    json: async () => {
      if ("jsonBody" in response) return response.jsonBody;
      throw new SyntaxError("not json");
    },
  } as Response);
  vi.stubGlobal("fetch", stub);
  return stub;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

const VALID_TICKER = {
  status: "OK",
  source: "binance-futures-usdm:rest",
  event_ts: "2026-09-20T23:03:33Z",
  received_ts: "2026-09-20T23:03:36Z",
  age_seconds: 3.1,
  detail: null,
  value: { symbol: "BTCUSDT", last_price: "81242.40" },
};

describe("getTicker", () => {
  it("returns a valid observation envelope", async () => {
    mockFetch({ jsonBody: VALID_TICKER });
    const result = await getTicker("BTCUSDT");
    expect(result.status).toBe("OK");
    expect(result.value?.last_price).toBe("81242.40");
  });

  it("accepts a STALE envelope carrying no value", async () => {
    mockFetch({
      jsonBody: { ...VALID_TICKER, status: "STALE", value: null, detail: "40s old" },
    });
    const result = await getTicker("BTCUSDT");
    expect(result.status).toBe("STALE");
    expect(result.value).toBeNull();
  });

  it("rejects an OK envelope that carries no value", async () => {
    // The backend guarantees this cannot happen; the client re-checks so a
    // contract regression surfaces as an error state, not a crash mid-render.
    mockFetch({ jsonBody: { ...VALID_TICKER, value: null } });
    await expect(getTicker("BTCUSDT")).rejects.toMatchObject({ code: "INVALID_RESPONSE" });
  });

  it("rejects a STALE envelope that smuggles a value", async () => {
    mockFetch({ jsonBody: { ...VALID_TICKER, status: "STALE" } });
    await expect(getTicker("BTCUSDT")).rejects.toMatchObject({ code: "INVALID_RESPONSE" });
  });

  it("rejects a payload missing required fields", async () => {
    mockFetch({ jsonBody: { status: "OK", source: "x", value: { symbol: "BTCUSDT" } } });
    await expect(getTicker("BTCUSDT")).rejects.toMatchObject({ code: "INVALID_RESPONSE" });
  });

  it("surfaces the backend error code", async () => {
    mockFetch({
      ok: false,
      status: 503,
      jsonBody: {
        error: {
          code: "EXCHANGE_UNAVAILABLE",
          message: "Exchange is unavailable",
          details: null,
          request_id: "req-9",
        },
      },
    });
    await expect(getTicker("BTCUSDT")).rejects.toMatchObject({
      code: "EXCHANGE_UNAVAILABLE",
      status: 503,
      requestId: "req-9",
    });
  });

  it("turns non-JSON into a typed error rather than throwing raw", async () => {
    mockFetch({});
    await expect(getTicker("BTCUSDT")).rejects.toBeInstanceOf(ApiError);
  });

  it("reports an unreachable backend distinctly", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    await expect(getTicker("BTCUSDT")).rejects.toMatchObject({ code: "NETWORK_UNREACHABLE" });
  });

  it("encodes the symbol into the path", async () => {
    const stub = mockFetch({ jsonBody: VALID_TICKER });
    await getTicker("BTCUSDT");
    expect(String(stub.mock.calls[0]?.[0])).toContain("/markets/BTCUSDT/ticker");
  });
});

describe("getKlines", () => {
  const series = {
    status: "OK",
    source: "binance-futures-usdm:rest",
    event_ts: null,
    received_ts: "2026-09-20T23:03:36Z",
    age_seconds: -3383,
    detail: null,
    value: {
      symbol: "BTCUSDT",
      timeframe: "1h",
      candles: [
        {
          open_time: "2026-09-20T22:00:00Z",
          close_time: "2026-09-20T22:59:59Z",
          open: "80897.40",
          high: "81256.10",
          low: "80734.10",
          close: "81181.20",
          volume: "3669.947",
          quote_volume: null,
          trade_count: null,
        },
      ],
    },
  };

  it("returns a validated series", async () => {
    mockFetch({ jsonBody: series });
    const result = await getKlines("BTCUSDT", "1h", 200);
    expect(result.value?.candles).toHaveLength(1);
  });

  it("rejects candles missing price fields", async () => {
    mockFetch({
      jsonBody: {
        ...series,
        value: { ...series.value, candles: [{ open_time: "x", open: "1" }] },
      },
    });
    await expect(getKlines("BTCUSDT", "1h", 200)).rejects.toMatchObject({
      code: "INVALID_RESPONSE",
    });
  });

  it("sends the interval and limit as query parameters", async () => {
    const stub = mockFetch({ jsonBody: series });
    await getKlines("BTCUSDT", "4h", 50);
    const url = String(stub.mock.calls[0]?.[0]);
    expect(url).toContain("interval=4h");
    expect(url).toContain("limit=50");
  });
});

describe("getScan", () => {
  const page = {
    rows: [{ symbol: "BTCUSDT" }],
    page: 1,
    page_size: 25,
    total_rows: 1,
    universe_size: 528,
    ranking_scope: "FULL_UNIVERSE",
    candidate_pool_size: null,
    sort_by: "quote_volume_24h",
    direction: "desc",
    ticker_source: "binance-futures-usdm:rest",
    ticker_status: "OK",
    ticker_event_ts: null,
    ticker_age_seconds: 1,
    scanned_at: "2026-09-20T23:03:36Z",
  };

  it("returns a validated page", async () => {
    mockFetch({ jsonBody: page });
    const result = await getScan({ page: 1 });
    expect(result.universe_size).toBe(528);
  });

  it("only sends parameters that were set", async () => {
    const stub = mockFetch({ jsonBody: page });
    await getScan({ sort: "opportunity_score", direction: "asc", includeMetrics: true });
    const url = String(stub.mock.calls[0]?.[0]);
    expect(url).toContain("sort=opportunity_score");
    expect(url).toContain("direction=asc");
    expect(url).toContain("include_metrics=true");
    expect(url).not.toContain("search=");
  });

  it("rejects a page whose rows are not row-shaped", async () => {
    mockFetch({ jsonBody: { ...page, rows: [{ nope: true }] } });
    await expect(getScan({})).rejects.toMatchObject({ code: "INVALID_RESPONSE" });
  });
});
