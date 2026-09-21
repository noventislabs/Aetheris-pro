"use client";

import { useMemo } from "react";
import { formatNumber, toNumber } from "@/lib/format";
import type { EquityPoint } from "@/lib/types";

/**
 * Equity and drawdown, drawn as inline SVG.
 *
 * Two panes sharing an x-axis: equity above, drawdown below. Each is a single
 * `<path>`, so the whole chart is a handful of DOM nodes regardless of how many
 * bars it covers — the backend already thins the curve to 500 points for
 * transport.
 *
 * The starting balance is drawn as a dashed reference line, because "did this
 * end above where it started" is the first question anyone asks and reading it
 * off an axis is worse than seeing it.
 */

const VIEW_W = 1000;
const EQUITY_TOP = 12;
const EQUITY_BOTTOM = 210;
const DD_TOP = 235;
const DD_BOTTOM = 300;
const PAD_LEFT = 8;
const PAD_RIGHT = 76;

export function EquityCurve({
  points,
  startingBalance,
}: {
  points: readonly EquityPoint[];
  startingBalance: string;
}) {
  const geometry = useMemo(() => {
    const values = points
      .map((point) => toNumber(point.equity))
      .filter((value): value is number => value !== null);
    if (values.length < 2) return null;

    const start = toNumber(startingBalance) ?? values[0]!;
    let max = Math.max(...values, start);
    let min = Math.min(...values, start);
    if (max === min) {
      max += Math.abs(max) * 0.001 || 1;
      min -= Math.abs(min) * 0.001 || 1;
    }
    const pad = (max - min) * 0.08;
    max += pad;
    min -= pad;

    const drawdowns = points.map((point) => toNumber(point.drawdown_percent) ?? 0);
    const maxDrawdown = Math.max(...drawdowns, 0.0001);

    const plotWidth = VIEW_W - PAD_LEFT - PAD_RIGHT;
    const step = plotWidth / (points.length - 1);
    const x = (index: number) => PAD_LEFT + step * index;
    const equityY = (value: number) =>
      EQUITY_BOTTOM - ((value - min) / (max - min)) * (EQUITY_BOTTOM - EQUITY_TOP);
    const drawdownY = (value: number) =>
      DD_TOP + (value / maxDrawdown) * (DD_BOTTOM - DD_TOP);

    const equityPath = values.map((value, index) => `${index === 0 ? "M" : "L"}${x(index).toFixed(1)},${equityY(value).toFixed(1)}`).join(" ");
    const drawdownPath =
      `M${x(0).toFixed(1)},${DD_TOP} ` +
      drawdowns.map((value, index) => `L${x(index).toFixed(1)},${drawdownY(value).toFixed(1)}`).join(" ") +
      ` L${x(points.length - 1).toFixed(1)},${DD_TOP} Z`;

    return { min, max, start, maxDrawdown, equityY, equityPath, drawdownPath, x, values };
  }, [points, startingBalance]);

  if (!geometry) {
    return (
      <div className="state">
        <span className="state-code">NO CURVE</span>
        <div>Not enough equity points to plot.</div>
      </div>
    );
  }

  const ended = geometry.values[geometry.values.length - 1]!;
  const up = ended >= geometry.start;

  return (
    <div className="chart-wrap">
      <svg
        viewBox={`0 0 ${VIEW_W} 310`}
        preserveAspectRatio="none"
        style={{ height: 310, width: "100%", display: "block" }}
        role="img"
        aria-label={`Simulated equity curve over ${points.length} points, ending at ${ended.toFixed(2)}`}
      >
        {[0, 0.5, 1].map((fraction) => {
          const value = geometry.min + (geometry.max - geometry.min) * fraction;
          const y = geometry.equityY(value);
          return (
            <g key={fraction}>
              <line x1={PAD_LEFT} x2={VIEW_W - PAD_RIGHT} y1={y} y2={y} stroke="#232a36" />
              <text x={VIEW_W - PAD_RIGHT + 6} y={y + 4} fill="#5d6a7d" fontSize={11} fontFamily="monospace">
                {value.toFixed(2)}
              </text>
            </g>
          );
        })}

        {/* Starting balance: the line the result is judged against. */}
        <line
          x1={PAD_LEFT}
          x2={VIEW_W - PAD_RIGHT}
          y1={geometry.equityY(geometry.start)}
          y2={geometry.equityY(geometry.start)}
          stroke="#5d6a7d"
          strokeWidth={1}
          strokeDasharray="4 4"
        />
        <text
          x={VIEW_W - PAD_RIGHT + 6}
          y={geometry.equityY(geometry.start) - 4}
          fill="#5d6a7d"
          fontSize={10}
          fontFamily="monospace"
        >
          start
        </text>

        <path d={geometry.equityPath} fill="none" stroke={up ? "#26a96a" : "#e0554e"} strokeWidth={1.6} />

        <text x={PAD_LEFT} y={DD_TOP - 8} fill="#5d6a7d" fontSize={10} fontFamily="monospace">
          DRAWDOWN
        </text>
        <path d={geometry.drawdownPath} fill="#e0554e" opacity={0.28} stroke="#e0554e" strokeWidth={1} />
        <text x={VIEW_W - PAD_RIGHT + 6} y={DD_BOTTOM} fill="#5d6a7d" fontSize={11} fontFamily="monospace">
          {formatNumber(String(geometry.maxDrawdown), 2)}%
        </text>
      </svg>
    </div>
  );
}
