import { useMemo } from "react";
import { ops } from "../../api/ops";
import { usePolling } from "../ops/usePolling";
import ViewState, { fmtNum, fmtPct, fmtUsd } from "../ops/ViewState";

const ratio = (v) => (v === null || v === undefined ? "—" : `${(Number(v) * 100).toFixed(0)}%`);

export default function MetricsSection() {
  const { data, error, loading, unavailable, reload } = usePolling(() => ops.getStrategyMetrics(), 0);

  // Keep only the newest window per strategy version, then group by dimension.
  const byDim = useMemo(() => {
    const rows = data || [];
    const newest = {};
    rows.forEach((r) => {
      const k = `${r.strategy_id}|${r.strategy_version}`;
      if (!newest[k] || String(r.window_end) > String(newest[k])) newest[k] = r.window_end;
    });
    const out = {};
    rows
      .filter((r) => r.window_end === newest[`${r.strategy_id}|${r.strategy_version}`])
      .forEach((r) => {
        const dim = r.metrics?.group?.dimension || "all";
        (out[dim] ||= []).push(r);
      });
    return out;
  }, [data]);

  const dims = Object.keys(byDim);
  const calibrations = (byDim.all || []).filter((r) => (r.metrics?.calibration || []).length);

  return (
    <div className="card">
      <div className="card-header">
        <h2>Strategy metrics</h2>
        <button className="btn btn-small" onClick={reload}>Refresh</button>
      </div>
      <ViewState loading={loading} unavailable={unavailable} error={error} empty={dims.length === 0}
        emptyText="No strategy metrics computed yet.">
        <p className="muted section-note">
          Groups marked "low sample" have too few closed trades to draw conclusions from.
        </p>
        {dims.map((dim) => (
          <div key={dim}>
            <h3 className="section-title">By {dim.replace(/_/g, " ")}</h3>
            <div className="table-scroll">
              <table className="table">
                <thead>
                  <tr>
                    <th>Strategy</th><th>Group</th><th>Trades</th><th>Win rate</th><th>Expectancy</th>
                    <th>Net P&L</th><th>Net return</th><th>Max drawdown</th>
                  </tr>
                </thead>
                <tbody>
                  {byDim[dim].map((r) => {
                    const m = r.metrics || {};
                    return (
                      <tr key={r.id}>
                        <td>{r.strategy_id} <span className="muted">v{r.strategy_version}</span></td>
                        <td>{String(m.group?.value ?? "all")}</td>
                        <td>
                          {m.sample_size ?? 0}
                          {m.sufficient === false && (
                            <span className="sample-low" title={`Minimum sample ${m.min_sample_size ?? "?"}`}> low sample</span>
                          )}
                        </td>
                        <td>{ratio(m.win_rate)}</td>
                        <td>{fmtUsd(m.expectancy)}</td>
                        <td className={m.net_pnl > 0 ? "pos" : m.net_pnl < 0 ? "neg" : ""}>{fmtUsd(m.net_pnl)}</td>
                        <td>{fmtPct(m.net_return_pct, 2)}</td>
                        <td>{fmtPct(m.max_drawdown_pct, 2)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        ))}
        {calibrations.length > 0 && (
          <div>
            <h3 className="section-title">Confidence calibration</h3>
            <p className="muted section-note">
              Predicted confidence vs how often the trade actually made money. A positive gap means the
              model was overconfident.
            </p>
            {calibrations.map((r) => (
              <div className="table-scroll" key={r.id}>
                <table className="table">
                  <caption className="muted">{r.strategy_id} v{r.strategy_version}</caption>
                  <thead><tr><th>Confidence bucket</th><th>Trades</th><th>Predicted</th><th>Realized hit rate</th><th>Gap</th></tr></thead>
                  <tbody>
                    {r.metrics.calibration.map((c) => (
                      <tr key={c.bucket}>
                        <td>{c.bucket}</td>
                        <td>
                          {c.sample_size}
                          {c.sufficient === false && <span className="sample-low"> low sample</span>}
                        </td>
                        <td>{ratio(c.predicted_confidence)}</td>
                        <td>{ratio(c.realized_hit_rate)}</td>
                        <td>{fmtNum(c.gap, 2)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ))}
          </div>
        )}
      </ViewState>
    </div>
  );
}
