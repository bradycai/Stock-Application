import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api/client";

const fmtCost = (c) => `~$${Number(c || 0).toFixed(4)}`;
const rate = (f, c) => (c ? `${((f / c) * 100).toFixed(1)}%` : "0%");

function Card({ title, calls, failures, cost }) {
  return (
    <div className="card">
      <div className="metric-label">{title}</div>
      <div className="metric-value">{fmtCost(cost)} <span className="muted">(est.)</span></div>
      <div className="muted">{calls} calls, failure rate {rate(failures, calls)}</div>
    </div>
  );
}

export default function UsagePage() {
  const [usage, setUsage] = useState(null);
  const [calls, setCalls] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [u, c] = await Promise.all([api.getLlmUsage(7), api.getLlmCalls(50)]);
      setUsage(u);
      setCalls(c);
    } catch (e) {
      setError(e.message);
      setUsage((u) => u || { totals: null, groups: [] });
      setCalls((c) => c || []);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const today = useMemo(() => {
    const day = new Date().toISOString().slice(0, 10);
    const t = { calls: 0, failures: 0, cost: 0 };
    (usage?.groups || []).filter((g) => g.day === day).forEach((g) => {
      t.calls += g.calls;
      t.failures += g.failures;
      t.cost += g.estimated_cost_usd;
    });
    return t;
  }, [usage]);

  const byModelRole = useMemo(() => {
    const m = {};
    (usage?.groups || []).forEach((g) => {
      const k = `${g.requested_model}|${g.role}`;
      m[k] ||= { model: g.requested_model, role: g.role, calls: 0, failures: 0, inp: 0, out: 0, cache: 0, cost: 0 };
      const r = m[k];
      r.calls += g.calls;
      r.failures += g.failures;
      r.inp += g.input_tokens;
      r.out += g.output_tokens;
      r.cache += g.cache_read_tokens + g.cache_write_tokens;
      r.cost += g.estimated_cost_usd;
    });
    return Object.values(m).sort((a, b) => b.cost - a.cost);
  }, [usage]);

  const failed = (calls || []).filter((c) => c.status !== "ok");
  const totals = usage?.totals;

  return (
    <div className="page-stack">
      <div className="card-header">
        <h2>Claude usage</h2>
        <button className="btn btn-small" onClick={load}>Refresh</button>
      </div>
      <p className="muted section-note">All costs are estimates from list prices, not invoices. Days are UTC.</p>
      {error && <div className="banner banner-warn">Error: {error}</div>}
      {usage === null && <p className="muted">Loading…</p>}
      {usage && (
        <>
          <div className="usage-cards">
            <Card title="Last 7 days" calls={totals?.calls || 0} failures={totals?.failures || 0}
              cost={totals?.estimated_cost_usd} />
            <Card title="Today (UTC)" calls={today.calls} failures={today.failures} cost={today.cost} />
          </div>
          <div className="card">
            <div className="card-header"><h2>By model and role (7 days)</h2></div>
            {byModelRole.length === 0 ? (
              <p className="muted">No Claude calls in this period.</p>
            ) : (
              <div className="table-scroll">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Model</th><th>Role</th><th>Calls</th><th>Failures</th>
                      <th>Input tok</th><th>Output tok</th><th>Cache tok</th><th>Est. cost</th>
                    </tr>
                  </thead>
                  <tbody>
                    {byModelRole.map((r) => (
                      <tr key={`${r.model}|${r.role}`}>
                        <td>{r.model}</td><td>{r.role}</td><td>{r.calls}</td>
                        <td className={r.failures ? "neg" : ""}>{r.failures}</td>
                        <td>{r.inp.toLocaleString()}</td><td>{r.out.toLocaleString()}</td>
                        <td>{r.cache.toLocaleString()}</td><td>{fmtCost(r.cost)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
          <div className="card">
            <div className="card-header"><h2>Recent failed calls</h2></div>
            {calls === null ? (
              <p className="muted">Loading…</p>
            ) : failed.length === 0 ? (
              <p className="muted">No failed calls among the last 50.</p>
            ) : (
              <div className="table-scroll">
                <table className="table">
                  <thead><tr><th>Time</th><th>Model / role</th><th>Status</th><th>Error</th></tr></thead>
                  <tbody>
                    {failed.map((c) => (
                      <tr key={c.id}>
                        <td>{new Date(c.created_at).toLocaleString()}</td>
                        <td>{c.requested_model} / {c.role}</td>
                        <td className="neg">{c.status}</td>
                        <td>{c.error || c.stop_reason || "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
