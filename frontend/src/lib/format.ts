/**
 * Display formatting.
 *
 * Every function here takes the backend's decimal *strings* and produces text.
 * None of them do arithmetic on money: converting to `number` for a chart pixel
 * position is fine, but a rounded float must never travel back anywhere that
 * matters.
 *
 * Absent values format as an em dash, never as "0" or "-". A dash is visibly
 * not a number; a zero is a claim.
 */

export const NO_VALUE = "—";

/** Parse a decimal string for geometry only. Returns null when unusable. */
export function toNumber(value: string | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/**
 * Format a price with precision suited to its magnitude.
 *
 * Crypto perpetuals span roughly 0.00000001 to 100000, so a fixed number of
 * decimals either truncates a micro-cap to zero or pads BTC with noise.
 */
export function formatPrice(value: string | null | undefined): string {
  const parsed = toNumber(value);
  if (parsed === null) return NO_VALUE;
  const magnitude = Math.abs(parsed);
  let decimals: number;
  if (magnitude === 0) decimals = 2;
  else if (magnitude >= 1000) decimals = 2;
  else if (magnitude >= 1) decimals = 4;
  else if (magnitude >= 0.01) decimals = 5;
  else decimals = 8;
  return parsed.toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

/** Compact notation for volumes, which run to billions. */
export function formatCompact(value: string | null | undefined): string {
  const parsed = toNumber(value);
  if (parsed === null) return NO_VALUE;
  return parsed.toLocaleString("en-US", {
    notation: "compact",
    maximumFractionDigits: 2,
  });
}

export function formatPercent(value: string | null | undefined, decimals = 2): string {
  const parsed = toNumber(value);
  if (parsed === null) return NO_VALUE;
  const sign = parsed > 0 ? "+" : "";
  return `${sign}${parsed.toFixed(decimals)}%`;
}

export function formatNumber(value: string | null | undefined, decimals = 2): string {
  const parsed = toNumber(value);
  if (parsed === null) return NO_VALUE;
  return parsed.toFixed(decimals);
}

/** Sign of a change, for colouring. Unknown is neutral, not negative. */
export function changeDirection(value: string | null | undefined): "up" | "down" | "flat" {
  const parsed = toNumber(value);
  if (parsed === null || parsed === 0) return "flat";
  return parsed > 0 ? "up" : "down";
}

/**
 * Human-readable data age.
 *
 * A negative age is not an error: a kline series reports the newest bar's close
 * time, and that bar is normally still forming, so it closes in the future.
 * That reads as "forming" rather than as a nonsensical negative staleness.
 */
export function formatAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "age unverified";
  if (seconds < 0) return "forming";
  if (seconds < 1) return "<1s ago";
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  return `${Math.round(seconds / 3600)}h ago`;
}

export function formatClock(iso: string | null | undefined): string {
  if (!iso) return NO_VALUE;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return NO_VALUE;
  return date.toLocaleTimeString("en-GB", { hour12: false });
}
