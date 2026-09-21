"use client";

import { useMemo } from "react";
import { alignIndicator, formatLatest } from "@/lib/indicators";
import type { Candle, IndicatorResult } from "@/lib/types";

/**
 * One oscillator in its own pane, drawn as inline SVG.
 *
 * Each line is a single `<polyline>`, so a pane costs a handful of DOM nodes
 * regardless of how many bars it covers. That matters: three oscillators over
 * 200 bars as individual marks would be 600 elements, and this is a machine
 * with ~1 GB of free RAM.
 *
 * A gap in a line is a real gap. Points with no value are dropped from the
 * polyline rather than bridged, because a continuous line across a warm-up
 * region would claim values that were never computed.
 */

const VIEW_W = 1000;
const VIEW_H = 110;
const PAD_TOP = 10;
const PAD_BOTTOM = 16;
const PAD_LEFT = 8;
const PAD_RIGHT = 56;

/** Reference levels worth drawing, per indicator. */
const REFERENCE_LEVELS: Record<string, number[]> = {
  rsi: [30, 70],
  stochastic: [20, 80],
  adx: [20],
  cci: [-100, 100],
  macd: [0],
  roc: [0],
};

interface Segment {
  colour: string;
  points: string;
}

export function OscillatorPane({
  candles,
  result,
}: {
  candles: readonly Candle[];
  result: IndicatorResult;
}) {
  const geometry = useMemo(() => {
    const lines = alignIndicator(candles, result);
    const finite = lines.flatMap((line) =>
      line.values.filter((value): value is number => value !== null),
    );
    if (finite.length === 0) return null;

    let max = Math.max(...finite);
    let min = Math.min(...finite);
    for (const level of REFERENCE_LEVELS[result.indicator] ?? []) {
      max = Math.max(max, level);
      min = Math.min(min, level);
    }
    if (max === min) {
      max += Math.abs(max) * 0.01 || 1;
      min -= Math.abs(min) * 0.01 || 1;
    }
    const pad = (max - min) * 0.08;
    max += pad;
    min -= pad;

    const plotWidth = VIEW_W - PAD_LEFT - PAD_RIGHT;
    const step = candles.length > 1 ? plotWidth / (candles.length - 1) : plotWidth;
    const toY = (value: number) =>
      VIEW_H - PAD_BOTTOM - ((value - min) / (max - min)) * (VIEW_H - PAD_TOP - PAD_BOTTOM);
    const toX = (index: number) => PAD_LEFT + step * index;

    // Split each line into runs of consecutive defined points, so an
    // undefined bar breaks the stroke instead of being bridged.
    const segments: Segment[] = [];
    for (const line of lines) {
      let run: string[] = [];
      line.values.forEach((value, index) => {
        if (value === null) {
          if (run.length > 1) segments.push({ colour: line.colour, points: run.join(" ") });
          run = [];
          return;
        }
        run.push(`${toX(index).toFixed(1)},${toY(value).toFixed(1)}`);
      });
      if (run.length > 1) segments.push({ colour: line.colour, points: run.join(" ") });
    }

    return { min, max, toY, segments };
  }, [candles, result]);

  const latest = formatLatest(result);

  return (
    <div className="osc-pane">
      <div className="osc-head">
        <span className="osc-title">
          {result.name}
          {Object.keys(result.parameters).length > 0 ? (
            <span className="osc-params">
              {Object.entries(result.parameters)
                .map(([key, value]) => `${key.split("_").pop()} ${Number(value)}`)
                .join(" · ")}
            </span>
          ) : null}
        </span>
        {result.status === "READY" ? (
          <span className="num osc-latest">{latest}</span>
        ) : (
          <span className="badge badge-warn" title={result.detail ?? undefined}>
            {result.status}
          </span>
        )}
      </div>

      {geometry === null ? (
        <div className="osc-empty">
          {result.detail ?? "No values to plot for this indicator yet."}
        </div>
      ) : (
        <svg
          viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
          preserveAspectRatio="none"
          style={{ height: 110, width: "100%", display: "block" }}
          role="img"
          aria-label={`${result.name} oscillator over ${candles.length} candles`}
        >
          {(REFERENCE_LEVELS[result.indicator] ?? []).map((level) => (
            <g key={level}>
              <line
                x1={PAD_LEFT}
                x2={VIEW_W - PAD_RIGHT}
                y1={geometry.toY(level)}
                y2={geometry.toY(level)}
                stroke="#2b3342"
                strokeWidth={1}
                strokeDasharray="3 4"
              />
              <text
                x={VIEW_W - PAD_RIGHT + 5}
                y={geometry.toY(level) + 4}
                fill="#5d6a7d"
                fontSize={10}
                fontFamily="monospace"
              >
                {level}
              </text>
            </g>
          ))}

          {geometry.segments.map((segment, index) => (
            <polyline
              key={index}
              points={segment.points}
              fill="none"
              stroke={segment.colour}
              strokeWidth={1.3}
            />
          ))}

          <text
            x={VIEW_W - PAD_RIGHT + 5}
            y={PAD_TOP + 4}
            fill="#5d6a7d"
            fontSize={10}
            fontFamily="monospace"
          >
            {geometry.max.toFixed(1)}
          </text>
          <text
            x={VIEW_W - PAD_RIGHT + 5}
            y={VIEW_H - PAD_BOTTOM}
            fill="#5d6a7d"
            fontSize={10}
            fontFamily="monospace"
          >
            {geometry.min.toFixed(1)}
          </text>
        </svg>
      )}
    </div>
  );
}
