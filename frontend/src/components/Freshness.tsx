"use client";

import { formatAge } from "@/lib/format";
import type { DataStatus } from "@/lib/types";

/**
 * Provenance strip: where a number came from, and how old it is.
 *
 * The important case is `status === "OK"` with `age_seconds === null`. That
 * means the venue gave us no event timestamp, so freshness could not be
 * *verified* — it does not mean the data is current. Rendering it as "fresh"
 * would be exactly the kind of unearned confidence the data model exists to
 * prevent, so it reads "age unverified" and is styled as unknown, not good.
 */

function dotClass(status: DataStatus, ageKnown: boolean): string {
  if (status === "OK") return ageKnown ? "dot dot-ok" : "dot dot-unknown";
  if (status === "STALE") return "dot dot-stale";
  return "dot dot-bad";
}

export function FreshnessBadge({
  status,
  source,
  ageSeconds,
  label,
}: {
  status: DataStatus;
  source: string;
  ageSeconds: number | null;
  label?: string;
}) {
  const ageKnown = ageSeconds !== null;
  const ageText = formatAge(ageSeconds);
  const title =
    status === "OK" && !ageKnown
      ? "The venue supplied no event timestamp, so this value's age could not be verified."
      : `Status ${status} from ${source}, ${ageText}`;

  return (
    <span className="freshness" title={title}>
      <span className={dotClass(status, ageKnown)} aria-hidden="true" />
      {label ? <span>{label}</span> : null}
      <span>{status}</span>
      <span aria-hidden="true">·</span>
      <span>{source}</span>
      <span aria-hidden="true">·</span>
      <span>{ageText}</span>
    </span>
  );
}

/** Compact status pill for table rows. */
export function StatusPill({ status }: { status: DataStatus }) {
  if (status === "OK") return null;
  const className = status === "STALE" ? "badge badge-warn" : "badge badge-bad";
  return <span className={className}>{status}</span>;
}
