"use client";

import { formatNumber, NO_VALUE } from "@/lib/format";
import type { MetricStatus, OpportunityScore } from "@/lib/types";

/**
 * Market Opportunity Score display.
 *
 * The title text spells out every component's contribution, so the number is
 * always one hover away from the arithmetic that produced it. It also repeats
 * what the score is not — a ranking figure with no stated meaning is an
 * invitation to read prediction into it.
 *
 * When the score is absent the cell says *why* (insufficient candles, metrics
 * not requested, upstream failure) rather than showing a dash that could be
 * mistaken for zero.
 */

const ABSENT_REASON: Record<MetricStatus, string> = {
  CALCULATED: "",
  INSUFFICIENT_DATA: "Not enough closed candles",
  UNAVAILABLE: "Candles unavailable",
  NOT_REQUESTED: "Not requested",
};

export function ScoreCell({
  score,
  metricsStatus,
  detail,
}: {
  score: OpportunityScore | null;
  metricsStatus: MetricStatus;
  detail: string | null;
}) {
  if (!score) {
    const reason = ABSENT_REASON[metricsStatus] || "Unavailable";
    return (
      <span className="badge" title={detail ?? reason}>
        {metricsStatus === "NOT_REQUESTED" ? NO_VALUE : reason}
      </span>
    );
  }

  const value = Number(score.score);
  const percent = Number.isFinite(value) ? Math.max(0, Math.min(value, 100)) : 0;
  const breakdown = score.components
    .map((component) => `${component.name}: ${component.contribution} (${component.detail})`)
    .join("\n");

  return (
    <span
      className="score"
      title={
        `Market Opportunity Score ${score.score} / 100 (${score.method})\n\n${breakdown}\n\n` +
        "This is a deterministic ranking metric over present-tense measurements. " +
        "It is NOT a probability of profit, expected return, win rate or prediction."
      }
    >
      <span className="score-bar" aria-hidden="true">
        <span className="score-fill" style={{ width: `${percent}%` }} />
      </span>
      <span>{formatNumber(score.score, 1)}</span>
    </span>
  );
}
