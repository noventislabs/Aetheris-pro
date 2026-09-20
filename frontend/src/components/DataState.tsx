"use client";

import type { ReactNode } from "react";
import type { ApiError } from "@/lib/api";

/**
 * The explicit states every market-data component must handle.
 *
 * There is deliberately no "fall back to something plausible" path. When the
 * backend cannot answer, the UI says which of these happened and why — it does
 * not render a zero, a dash pretending to be a price, or last week's number.
 */

export function LoadingState({ label = "Loading" }: { label?: string }) {
  return (
    <div className="state" role="status" aria-live="polite">
      <span className="state-code">{label.toUpperCase()}</span>
      <div style={{ display: "grid", gap: 6, maxWidth: 320, margin: "10px auto 0" }}>
        <div className="skeleton" style={{ height: 10, width: "70%" }} />
        <div className="skeleton" style={{ height: 10, width: "90%" }} />
        <div className="skeleton" style={{ height: 10, width: "55%" }} />
      </div>
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: ApiError; onRetry?: () => void }) {
  return (
    <div className="state" role="alert">
      <span className="state-code">{error.code}</span>
      <div>{error.message}</div>
      {error.requestId ? (
        <p className="state-detail">Request ID {error.requestId}</p>
      ) : null}
      {onRetry ? (
        <button type="button" onClick={onRetry}>
          Retry
        </button>
      ) : null}
    </div>
  );
}

/**
 * The backend answered, but with no value and a reason.
 *
 * `detail` is the backend's own explanation and is rendered verbatim: it is the
 * difference between "no data" and "this instrument last traded 40 minutes ago".
 */
export function UnavailableState({
  status,
  detail,
  onRetry,
}: {
  status: string;
  detail: string | null;
  onRetry?: () => void;
}) {
  return (
    <div className="state" role="status">
      <span className="state-code">{status}</span>
      <div>No value available for this request.</div>
      {detail ? <p className="state-detail">{detail}</p> : null}
      {onRetry ? (
        <button type="button" onClick={onRetry}>
          Retry
        </button>
      ) : null}
    </div>
  );
}

export function EmptyState({ message, detail }: { message: string; detail?: ReactNode }) {
  return (
    <div className="state">
      <span className="state-code">NO RESULTS</span>
      <div>{message}</div>
      {detail ? <p className="state-detail">{detail}</p> : null}
    </div>
  );
}
