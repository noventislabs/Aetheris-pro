"use client";

import { useMemo, useRef, useState } from "react";
import { formatCompact, formatPrice, toNumber } from "@/lib/format";
import type { Candle, Timeframe } from "@/lib/types";

/**
 * OHLCV chart drawn as inline SVG.
 *
 * No charting library. The candidates (lightweight-charts, Recharts, D3) add
 * 40-400 kB of JavaScript and, more importantly, a canvas/virtual-DOM layer
 * that has to be re-instantiated on every symbol change — on a machine with
 * ~1 GB free RAM that cost is real and the benefit is not, because this chart
 * draws a few hundred rectangles and two axes.
 *
 * The chart renders exactly the candles the backend supplied. It never
 * interpolates a gap, extends a series, or synthesises a bar: a missing
 * candle is a missing candle.
 */

const VIEW_W = 1000;
const VIEW_H = 420;
const PRICE_TOP = 12;
const PRICE_BOTTOM = 300;
const VOLUME_TOP = 330;
const VOLUME_BOTTOM = 400;
const PAD_LEFT = 8;
const PAD_RIGHT = 74;

interface Bar {
  index: number;
  openTime: string;
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
  rising: boolean;
}

function toBars(candles: readonly Candle[]): Bar[] {
  const bars: Bar[] = [];
  candles.forEach((candle, index) => {
    const o = toNumber(candle.open);
    const h = toNumber(candle.high);
    const l = toNumber(candle.low);
    const c = toNumber(candle.close);
    const v = toNumber(candle.volume);
    // A bar we cannot parse is dropped, not guessed at.
    if (o === null || h === null || l === null || c === null) return;
    bars.push({ index, openTime: candle.open_time, o, h, l, c, v: v ?? 0, rising: c >= o });
  });
  return bars;
}

function timeLabel(iso: string, timeframe: Timeframe): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  if (timeframe === "1d") {
    return date.toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
  }
  if (timeframe === "4h" || timeframe === "1h") {
    return date.toLocaleString("en-GB", {
      day: "2-digit",
      month: "short",
      hour: "2-digit",
      hour12: false,
    });
  }
  return date.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false });
}

export function CandleChart({
  candles,
  timeframe,
  symbol,
}: {
  candles: readonly Candle[];
  timeframe: Timeframe;
  symbol: string;
}) {
  const svgRef = useRef<SVGSVGElement>(null);
  const [hover, setHover] = useState<number | null>(null);

  const bars = useMemo(() => toBars(candles), [candles]);

  const geometry = useMemo(() => {
    if (bars.length === 0) return null;
    const highs = bars.map((b) => b.h);
    const lows = bars.map((b) => b.l);
    let max = Math.max(...highs);
    let min = Math.min(...lows);
    if (max === min) {
      // A perfectly flat window would divide by zero; give it a nominal band
      // so the line renders in the middle rather than at an edge.
      max += Math.abs(max) * 0.001 || 1;
      min -= Math.abs(min) * 0.001 || 1;
    }
    const pad = (max - min) * 0.06;
    max += pad;
    min -= pad;

    const maxVolume = Math.max(...bars.map((b) => b.v), 0);
    const plotWidth = VIEW_W - PAD_LEFT - PAD_RIGHT;
    const slot = plotWidth / bars.length;
    const bodyWidth = Math.max(1, Math.min(slot * 0.68, 14));

    const priceToY = (price: number) =>
      PRICE_BOTTOM - ((price - min) / (max - min)) * (PRICE_BOTTOM - PRICE_TOP);
    const volumeToY = (volume: number) =>
      maxVolume === 0
        ? VOLUME_BOTTOM
        : VOLUME_BOTTOM - (volume / maxVolume) * (VOLUME_BOTTOM - VOLUME_TOP);
    const centreX = (index: number) => PAD_LEFT + slot * index + slot / 2;

    return { min, max, slot, bodyWidth, priceToY, volumeToY, centreX };
  }, [bars]);

  if (!geometry || bars.length === 0) {
    return (
      <div className="state">
        <span className="state-code">NO CANDLES</span>
        <div>The backend returned an empty series for {symbol}.</div>
      </div>
    );
  }

  const { min, max, priceToY, volumeToY, centreX, bodyWidth } = geometry;
  const lastBar = bars[bars.length - 1];
  const gridPrices = [0, 0.25, 0.5, 0.75, 1].map((f) => min + (max - min) * f);
  const hovered = hover !== null ? bars[hover] : undefined;

  const onMove = (event: React.MouseEvent<SVGSVGElement>) => {
    const svg = svgRef.current;
    if (!svg) return;
    const rect = svg.getBoundingClientRect();
    if (rect.width === 0) return;
    const x = ((event.clientX - rect.left) / rect.width) * VIEW_W;
    const index = Math.round((x - PAD_LEFT - geometry.slot / 2) / geometry.slot);
    setHover(index >= 0 && index < bars.length ? index : null);
  };

  return (
    <div className="chart-wrap">
      <svg
        ref={svgRef}
        viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
        preserveAspectRatio="none"
        style={{ height: 420 }}
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
        role="img"
        aria-label={`${symbol} ${timeframe} candlestick chart, ${bars.length} candles`}
      >
        {/* price grid */}
        {gridPrices.map((price) => {
          const y = priceToY(price);
          return (
            <g key={price}>
              <line x1={PAD_LEFT} x2={VIEW_W - PAD_RIGHT} y1={y} y2={y} stroke="#232a36" strokeWidth={1} />
              <text x={VIEW_W - PAD_RIGHT + 6} y={y + 4} fill="#5d6a7d" fontSize={11} fontFamily="monospace">
                {formatPrice(String(price))}
              </text>
            </g>
          );
        })}

        {/* candles */}
        {bars.map((bar) => {
          const x = centreX(bar.index);
          const colour = bar.rising ? "#26a96a" : "#e0554e";
          const yOpen = priceToY(bar.o);
          const yClose = priceToY(bar.c);
          const top = Math.min(yOpen, yClose);
          const height = Math.max(Math.abs(yClose - yOpen), 1);
          return (
            <g key={bar.index}>
              <line x1={x} x2={x} y1={priceToY(bar.h)} y2={priceToY(bar.l)} stroke={colour} strokeWidth={1} />
              <rect x={x - bodyWidth / 2} y={top} width={bodyWidth} height={height} fill={colour} />
              <rect
                x={x - bodyWidth / 2}
                y={volumeToY(bar.v)}
                width={bodyWidth}
                height={Math.max(VOLUME_BOTTOM - volumeToY(bar.v), 0.5)}
                fill={colour}
                opacity={0.45}
              />
            </g>
          );
        })}

        {/* last price line */}
        {lastBar ? (
          <g>
            <line
              x1={PAD_LEFT}
              x2={VIEW_W - PAD_RIGHT}
              y1={priceToY(lastBar.c)}
              y2={priceToY(lastBar.c)}
              stroke="#4b8bf4"
              strokeWidth={1}
              strokeDasharray="3 3"
            />
            <rect x={VIEW_W - PAD_RIGHT + 2} y={priceToY(lastBar.c) - 9} width={70} height={18} fill="#4b8bf4" rx={3} />
            <text
              x={VIEW_W - PAD_RIGHT + 6}
              y={priceToY(lastBar.c) + 4}
              fill="#0a0d12"
              fontSize={11}
              fontFamily="monospace"
              fontWeight={600}
            >
              {formatPrice(String(lastBar.c))}
            </text>
          </g>
        ) : null}

        {/* crosshair */}
        {hovered ? (
          <g pointerEvents="none">
            <line
              x1={centreX(hovered.index)}
              x2={centreX(hovered.index)}
              y1={PRICE_TOP}
              y2={VOLUME_BOTTOM}
              stroke="#5d6a7d"
              strokeWidth={1}
              strokeDasharray="2 3"
            />
            <line
              x1={PAD_LEFT}
              x2={VIEW_W - PAD_RIGHT}
              y1={priceToY(hovered.c)}
              y2={priceToY(hovered.c)}
              stroke="#5d6a7d"
              strokeWidth={1}
              strokeDasharray="2 3"
            />
          </g>
        ) : null}

        {/* time axis: first, middle and last only, to keep the DOM small */}
        {[bars[0], bars[Math.floor(bars.length / 2)], lastBar].map((bar, position) =>
          bar ? (
            <text
              key={`t-${position}-${bar.index}`}
              x={centreX(bar.index)}
              y={VIEW_H - 4}
              fill="#5d6a7d"
              fontSize={11}
              fontFamily="monospace"
              textAnchor={position === 0 ? "start" : position === 2 ? "end" : "middle"}
            >
              {timeLabel(bar.openTime, timeframe)}
            </text>
          ) : null,
        )}
      </svg>

      {hovered ? (
        <div
          className="chart-tooltip"
          style={{
            left: `clamp(0px, ${(centreX(hovered.index) / VIEW_W) * 100}%, calc(100% - 150px))`,
            top: 8,
          }}
        >
          <dl>
            <dt>Time</dt>
            <dd>{timeLabel(hovered.openTime, timeframe)}</dd>
            <dt>O</dt>
            <dd>{formatPrice(String(hovered.o))}</dd>
            <dt>H</dt>
            <dd>{formatPrice(String(hovered.h))}</dd>
            <dt>L</dt>
            <dd>{formatPrice(String(hovered.l))}</dd>
            <dt>C</dt>
            <dd className={hovered.rising ? "up" : "down"}>{formatPrice(String(hovered.c))}</dd>
            <dt>Vol</dt>
            <dd>{formatCompact(String(hovered.v))}</dd>
          </dl>
        </div>
      ) : null}
    </div>
  );
}
