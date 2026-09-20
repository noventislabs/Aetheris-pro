"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./api";

/**
 * The three states every remote resource can be in.
 *
 * There is no fourth "stale data we're keeping around" state: when a refresh
 * fails, the component shows the error rather than silently continuing to
 * present the previous numbers as current.
 */
export type AsyncState<T> =
  | { kind: "loading" }
  | { kind: "success"; data: T }
  | { kind: "error"; error: ApiError };

interface Options {
  /** Poll interval in ms. Omit for a one-shot fetch. */
  pollMs?: number;
  /** Skip fetching entirely (e.g. no symbol selected yet). */
  enabled?: boolean;
}

/**
 * Fetch a resource, with optional polling that pauses when the tab is hidden.
 *
 * Polling a background tab burns the user's rate-limit budget and the laptop's
 * battery for numbers nobody is looking at, so the interval stops on
 * `visibilitychange` and refreshes immediately when the tab comes back.
 */
export function useApiResource<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[],
  options: Options = {},
): { state: AsyncState<T>; refresh: () => void } {
  const { pollMs, enabled = true } = options;
  const [state, setState] = useState<AsyncState<T>>({ kind: "loading" });
  const [nonce, setNonce] = useState(0);

  // Keeps the effect from re-subscribing every render when the caller passes
  // an inline arrow function.
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const refresh = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    if (!enabled) return;

    const controller = new AbortController();
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const run = async () => {
      try {
        const data = await fetcherRef.current(controller.signal);
        if (!cancelled) setState({ kind: "success", data });
      } catch (cause) {
        if (cancelled || (cause instanceof DOMException && cause.name === "AbortError")) return;
        setState({
          kind: "error",
          error:
            cause instanceof ApiError
              ? cause
              : new ApiError("Unexpected client error", "CLIENT_ERROR", 0),
        });
      } finally {
        if (!cancelled && pollMs && !document.hidden) {
          timer = setTimeout(run, pollMs);
        }
      }
    };

    const onVisibility = () => {
      if (document.hidden) {
        clearTimeout(timer);
      } else if (pollMs) {
        clearTimeout(timer);
        void run();
      }
    };

    setState({ kind: "loading" });
    void run();
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      cancelled = true;
      clearTimeout(timer);
      controller.abort();
      document.removeEventListener("visibilitychange", onVisibility);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, pollMs, enabled, nonce]);

  return { state, refresh };
}
