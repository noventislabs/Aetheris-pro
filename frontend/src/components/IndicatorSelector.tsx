"use client";

import { useMemo } from "react";
import type { IndicatorDescriptor } from "@/lib/types";

/**
 * Indicator toggles, driven entirely by the backend catalogue.
 *
 * The list is never hardcoded here. If an indicator is not registered and
 * tested on the backend it cannot appear in this UI at all — which is the
 * mechanism that stops a chart offering a line the system cannot compute.
 *
 * Smart Money Concepts overlays are listed separately and explicitly disabled:
 * hiding them entirely would misrepresent the roadmap, and enabling them would
 * mean drawing structures no backend calculation produces.
 */

/** Overlays the phase 4 backend does not implement. Shown, never selectable. */
const PLANNED_OVERLAYS = [
  "HH / HL / LH / LL",
  "BOS",
  "CHoCH",
  "FVG",
  "Liquidity zones",
  "Order blocks",
  "Premium / discount",
] as const;

export function IndicatorSelector({
  catalogue,
  selected,
  maxSelected,
  onToggle,
}: {
  catalogue: IndicatorDescriptor[];
  selected: readonly string[];
  maxSelected: number;
  onToggle: (key: string) => void;
}) {
  const { overlays, oscillators } = useMemo(
    () => ({
      overlays: catalogue.filter((entry) => entry.kind === "OVERLAY"),
      oscillators: catalogue.filter((entry) => entry.kind === "OSCILLATOR"),
    }),
    [catalogue],
  );

  const atLimit = selected.length >= maxSelected;

  const renderGroup = (label: string, entries: IndicatorDescriptor[]) => (
    <div className="ind-group">
      <span className="ind-group-label">{label}</span>
      <div className="ind-chips">
        {entries.map((entry) => {
          const active = selected.includes(entry.key);
          return (
            <button
              key={entry.key}
              type="button"
              className="chip"
              aria-pressed={active}
              disabled={!active && atLimit}
              onClick={() => onToggle(entry.key)}
              title={`${entry.name}\n\n${entry.description}\n\nConvention: ${entry.convention}`}
            >
              {entry.name}
            </button>
          );
        })}
      </div>
    </div>
  );

  return (
    <div className="ind-selector">
      {renderGroup("Price overlays", overlays)}
      {renderGroup("Oscillators", oscillators)}

      <div className="ind-group">
        <span className="ind-group-label">Smart Money Concepts</span>
        <div className="ind-chips">
          {PLANNED_OVERLAYS.map((label) => (
            <span
              key={label}
              className="chip chip-planned"
              aria-disabled="true"
              title="Not implemented. No backend calculation exists for this yet (phase 4+ roadmap)."
            >
              {label}
              <span className="tag">N/A</span>
            </span>
          ))}
        </div>
      </div>

      {atLimit ? (
        <p className="ind-note">
          Maximum of {maxSelected} indicators per request. Deselect one to add another.
        </p>
      ) : null}
    </div>
  );
}
