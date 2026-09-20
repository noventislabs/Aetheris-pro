import { describe, expect, it } from "vitest";
import {
  changeDirection,
  formatAge,
  formatCompact,
  formatPercent,
  formatPrice,
  NO_VALUE,
  toNumber,
} from "./format";

describe("toNumber", () => {
  it("parses decimal strings", () => {
    expect(toNumber("81242.40")).toBe(81242.4);
  });

  it("returns null for absent or unusable values", () => {
    expect(toNumber(null)).toBeNull();
    expect(toNumber(undefined)).toBeNull();
    expect(toNumber("")).toBeNull();
    expect(toNumber("not a number")).toBeNull();
  });
});

describe("formatPrice", () => {
  it("scales precision to magnitude", () => {
    // A fixed decimal count truncates a micro-cap to zero or pads BTC with noise.
    expect(formatPrice("81242.4")).toBe("81,242.40");
    expect(formatPrice("2.5")).toBe("2.5000");
    expect(formatPrice("0.05705")).toBe("0.05705");
    expect(formatPrice("0.00000123")).toBe("0.00000123");
  });

  it("shows absent values as a dash, never zero", () => {
    // A zero is a claim about the market; a dash is visibly not a number.
    expect(formatPrice(null)).toBe(NO_VALUE);
    expect(formatPrice(undefined)).toBe(NO_VALUE);
  });
});

describe("formatCompact", () => {
  it("abbreviates large volumes", () => {
    expect(formatCompact("7749548640")).toBe("7.75B");
  });

  it("shows absent volume as a dash", () => {
    expect(formatCompact(null)).toBe(NO_VALUE);
  });
});

describe("formatPercent", () => {
  it("signs positive values explicitly", () => {
    expect(formatPercent("1.25")).toBe("+1.25%");
    expect(formatPercent("-11.387")).toBe("-11.39%");
    expect(formatPercent("0")).toBe("0.00%");
  });

  it("shows absent change as a dash", () => {
    expect(formatPercent(null)).toBe(NO_VALUE);
  });
});

describe("changeDirection", () => {
  it("treats unknown as neutral, not negative", () => {
    expect(changeDirection(null)).toBe("flat");
    expect(changeDirection("0")).toBe("flat");
    expect(changeDirection("2")).toBe("up");
    expect(changeDirection("-2")).toBe("down");
  });
});

describe("formatAge", () => {
  it("says so when age could not be verified", () => {
    // OK status with a null age means the venue gave no timestamp. It must not
    // read as fresh.
    expect(formatAge(null)).toBe("age unverified");
  });

  it("reads a negative age as a forming candle", () => {
    // A kline series reports the newest bar's close time, which is in the
    // future while that bar is still open.
    expect(formatAge(-3383)).toBe("forming");
  });

  it("scales units", () => {
    expect(formatAge(0.4)).toBe("<1s ago");
    expect(formatAge(42)).toBe("42s ago");
    expect(formatAge(300)).toBe("5m ago");
    expect(formatAge(7200)).toBe("2h ago");
  });
});
