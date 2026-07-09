import { useEffect, useState } from "react";
import { api } from "../api/client";

const fmt = (n) => (n == null ? "—" : `$${Number(n).toFixed(2)}`);

export default function PositionManager() {
  const [rows, setRows] = useState([]);

  async function refresh() {
    setRows(await api.getManagedPositions().catch(() => []));
  }
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 15000);
    return () => clearInterval(t);
  }, []);

  return (
    <div className="card">
      <div className="card-header">
        <h2>Position Manager</h2>
        <span className="muted">{rows.length} open</span>
      </div>
      {rows.length === 0 ? (
        <p className="muted">No open positions.</p>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Entry</th>
              <th>Current</th>
              <th>P/L</th>
              <th>High</th>
              <th>Trailing stop</th>
              <th>AI conf</th>
              <th>Last decision</th>
              <th>Next exit</th>
              <th>Partials</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.symbol}>
                <td><strong>{r.symbol}</strong> <span className="muted">×{r.qty}</span></td>
                <td>{fmt(r.entry_price)}</td>
                <td>{fmt(r.current_price)}</td>
                <td className={r.pnl_pct >= 0 ? "pos" : "neg"}>{r.pnl_pct?.toFixed(2)}%</td>
                <td>{fmt(r.highest_price)}</td>
                <td>{fmt(r.trailing_stop)}</td>
                <td>{r.current_confidence != null ? `${(r.current_confidence * 100).toFixed(0)}%` : "—"}</td>
                <td>{r.last_decision ? r.last_decision.replace(/_/g, " ") : (r.managed ? "—" : "seeding…")}</td>
                <td className="muted" title={r.exit_condition || ""}>
                  {r.exit_condition ? r.exit_condition.slice(0, 40) : (r.trailing_stop ? `stop ${fmt(r.trailing_stop)}` : "—")}
                </td>
                <td>{(r.partials_taken && r.partials_taken.length) ? r.partials_taken.map((l) => `+${l}%`).join(" ") : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="muted section-note">
        Exits are AI-managed: an initial protective stop, a trailing stop that only moves up,
        partial profit-taking, and Claude thesis re-evaluation. No fixed take-profit target.
      </p>
    </div>
  );
}
