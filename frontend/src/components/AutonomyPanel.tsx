"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ErrorState, LoadingState } from "@/components/DataState";
import {
  ApiError,
  getAutonomousDecisions,
  getAutonomousStatus,
  setAutonomous,
} from "@/lib/api";
import { changeDirection, formatNumber, NO_VALUE } from "@/lib/format";
import type { AutonomousDecision, AutonomousStatus } from "@/lib/types";

/**
 * Autonomous paper trading: state, control, and the audit trail.
 *
 * The panel is built around one fact that must never be ambiguous: **the loop
 * is off unless it says ARMED**. The state is a badge rather than a subtle
 * toggle position, because "is this thing trading right now?" is the only
 * question that matters here and it should be answerable from across a room.
 *
 * The decision log shows every entry, including `NO_SIGNAL` and `SKIPPED`. A
 * log of only the interesting rows cannot distinguish an idle loop from a dead
 * one, and that distinction is the reason it exists.
 */

const POLL_MS = 5000;

function StateBadge({ status }: { status: AutonomousStatus }) {
  const tone =
    status.state === "ARMED" ? "up" : status.state === "FAILED" ? "down" : "";
  return (
    <span className={`badge ${tone}`} title={status.disclaimer}>
      {status.state.replace(/_/g, " ")}
    </span>
  );
}

function ActionCell({ decision }: { decision: AutonomousDecision }) {
  const tone =
    decision.action === "ENTERED" || decision.action === "CLOSED"
      ? "up"
      : decision.action === "REFUSED"
        ? "down"
        : "";
  return <span className={tone}>{decision.action}</span>;
}

function conditions(decision: AutonomousDecision): string {
  if (decision.conditions_total === null || decision.conditions_met === null) return NO_VALUE;
  // A count, never a ratio: 3/4 says exactly what it says.
  return `${decision.conditions_met}/${decision.conditions_total}`;
}

export function AutonomyPanel() {
  const [status, setStatus] = useState<AutonomousStatus | null>(null);
  const [decisions, setDecisions] = useState<AutonomousDecision[]>([]);
  const [error, setError] = useState<ApiError | null>(null);
  const [loadError, setLoadError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const refresh = useCallback(async () => {
    try {
      const [next, log] = await Promise.all([
        getAutonomousStatus(),
        getAutonomousDecisions(50),
      ]);
      if (!mounted.current) return;
      setStatus(next);
      setDecisions(log.decisions);
      setLoadError(null);
    } catch (cause) {
      if (mounted.current && cause instanceof ApiError) setLoadError(cause);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), POLL_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  const toggle = async () => {
    if (!status) return;
    setBusy(true);
    setError(null);
    try {
      const next = await setAutonomous(!status.enabled);
      if (mounted.current) setStatus(next);
    } catch (cause) {
      if (mounted.current) {
        setError(
          cause instanceof ApiError ? cause : new ApiError("Request failed", "UNKNOWN", 0),
        );
      }
    } finally {
      if (mounted.current) setBusy(false);
      void refresh();
    }
  };

  if (loadError && status === null) {
    return (
      <section className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Autonomous trading</h2>
        </div>
        <ErrorState error={loadError} onRetry={() => void refresh()} />
      </section>
    );
  }

  if (status === null) {
    return (
      <section className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Autonomous trading</h2>
        </div>
        <LoadingState label="Loading autonomy state" />
      </section>
    );
  }

  const permitted = status.permitted_by_config && status.paper_mode_enabled;

  return (
    <section className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Autonomous trading</h2>
        <StateBadge status={status} />
      </div>

      <p className="notice" role="note">
        <strong>The loop proposes; it never decides.</strong> Every entry passes the risk
        engine and then, independently, the paper gate. It sends no order to any exchange,
        holds no credential, and starts <strong>disarmed on every restart</strong> however
        it was left.
      </p>

      {status.state === "FAILED" ? (
        <p className="notice" role="status">
          <strong>Loop failed</strong> — {status.failure_detail}. Autonomy was disarmed and
          is not restarted on its own; re-arming is you saying you have looked at it.
        </p>
      ) : null}

      {status.venue_outage ? (
        <p className="notice" role="status">
          <strong>Venue outage</strong> — every symbol has failed for several iterations.
          The loop has lengthened its interval and keeps managing open positions.
        </p>
      ) : null}

      <div className="stats">
        <div className="stat">
          <div className="stat-label">Armed</div>
          <div className={`stat-value ${status.enabled ? "up" : ""}`}>
            {status.enabled ? "YES" : "NO"}
          </div>
        </div>
        <div className="stat" title="AETHERIS_AUTONOMOUS_TRADING_ENABLED. Gates arming; never arms.">
          <div className="stat-label">Permitted</div>
          <div className="stat-value">{status.permitted_by_config ? "YES" : "NO"}</div>
        </div>
        <div className="stat">
          <div className="stat-label">Iterations</div>
          <div className="stat-value">{status.iterations}</div>
        </div>
        <div className="stat">
          <div className="stat-label">Entries</div>
          <div className="stat-value">{status.entries}</div>
        </div>
        <div className="stat">
          <div className="stat-label">Refused</div>
          <div className="stat-value down">{status.refusals}</div>
        </div>
        <div className="stat">
          <div className="stat-label">Closed</div>
          <div className="stat-value">{status.closes}</div>
        </div>
        <div className="stat" title="Decision timeframe; closed bars only">
          <div className="stat-label">Timeframe</div>
          <div className="stat-value">{status.timeframe ?? NO_VALUE}</div>
        </div>
        <div className="stat">
          <div className="stat-label">Interval</div>
          <div className="stat-value">
            {status.interval_seconds ? `${status.interval_seconds}s` : NO_VALUE}
          </div>
        </div>
      </div>

      <div className="bt-row">
        <button type="button" className="run-button" disabled={busy || !permitted} onClick={() => void toggle()}>
          {status.enabled ? "Disarm" : "Arm autonomous trading"}
        </button>
        <span className="freshness">
          {status.symbols.length === 0
            ? "No symbols configured — the loop evaluates nothing and never picks instruments on its own."
            : `Watching ${status.symbols.join(", ")}`}
        </span>
      </div>

      {!permitted ? (
        <p className="state-detail">
          Arming is disabled: set <code>AETHERIS_AUTONOMOUS_TRADING_ENABLED=true</code> and
          restart. The flag gates arming and never arms on its own.
        </p>
      ) : null}

      {Object.keys(status.excluded_symbols).length > 0 ? (
        <p className="state-detail">
          <strong>Dropped this session:</strong>{" "}
          {Object.entries(status.excluded_symbols)
            .map(([symbol, reason]) => `${symbol} (${reason})`)
            .join(" · ")}
        </p>
      ) : null}

      {error ? <ErrorState error={error} /> : null}

      <div className="panel-head">
        <h3 className="panel-title">Decisions ({decisions.length})</h3>
        <span className="freshness">Every iteration, including the idle ones</span>
      </div>

      {decisions.length === 0 ? (
        <div className="state">
          <span className="state-code">NO DECISIONS</span>
          <div>
            The loop has not recorded anything yet. An empty log means it has not run —
            not that it ran and found nothing.
          </div>
        </div>
      ) : (
        <>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Action</th>
                  <th>Bias</th>
                  <th className="num">Conditions</th>
                  <th className="num">ATR %</th>
                  <th>Reason</th>
                </tr>
              </thead>
              <tbody>
                {decisions.map((decision) => (
                  <tr key={decision.decision_id}>
                    <td>{decision.symbol}</td>
                    <td>
                      <ActionCell decision={decision} />
                    </td>
                    <td>{decision.bias ?? decision.strategy_status ?? NO_VALUE}</td>
                    <td className="num">{conditions(decision)}</td>
                    <td className="num">
                      {decision.atr_percent ? formatNumber(decision.atr_percent, 2) : NO_VALUE}
                    </td>
                    <td title={decision.detail}>{decision.rejection_code ?? decision.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="cards">
            {decisions.map((decision) => (
              <article key={decision.decision_id} className="card">
                <div className="card-top">
                  <span style={{ fontWeight: 600 }}>
                    {decision.symbol} <ActionCell decision={decision} />
                  </span>
                  {decision.realized_pnl ? (
                    <span className={`num ${changeDirection(decision.realized_pnl)}`}>
                      {formatNumber(decision.realized_pnl, 4)}
                    </span>
                  ) : null}
                </div>
                <div className="card-grid">
                  <div>
                    <span>Bias</span>
                    <span className="num">
                      {decision.bias ?? decision.strategy_status ?? NO_VALUE}
                    </span>
                  </div>
                  <div>
                    <span>Conditions</span>
                    <span className="num">{conditions(decision)}</span>
                  </div>
                </div>
                <p className="state-detail">
                  {decision.rejection_code ? <strong>{decision.rejection_code}</strong> : null}{" "}
                  {decision.detail}
                </p>
              </article>
            ))}
          </div>
        </>
      )}
    </section>
  );
}
