import "@testing-library/jest-dom/vitest";
import { beforeEach, vi } from "vitest";

// Every test must state what the API returned. There is no default stub, so a
// component can never accidentally render fabricated market data in a test.
beforeEach(() => {
  vi.restoreAllMocks();
});
