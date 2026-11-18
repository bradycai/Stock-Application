import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import CollapsibleSection from "../components/CollapsibleSection";

const fmtTime = (t) => (t ? new Date(t).toLocaleString() : "—");
const fmtCost = (c) => (c == null ? "—" : `~$${Number(c).toFixed(4)}`);

function intentSummary(by) {
  const entries = Object.entries(by || {});
  if (!entries.length) return "none";
  return entries.map(([k, v]) => `${k.replace(/_/g, " ")}: ${v}`).join(", ");
}

function RunDetail({ run }) {
  const [quality, setQuality] = useState(null);
  const [snapshot, setSnapshot] = useState(null);
  const [snapLoading, setSnapLoading] = useState(false);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let live = true;
    setQuality(null);
    setSnapshot(null);
    setError(null);
    setLoading(true);
    api
      .getRunQuality(run.id)
      .then((q) => live && setQuality(q))
      .catch((e) => live && setError(e.message))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [run.id]);

  async function loadSnapshot() {
    setSnapLoading(true);
    try {
      setSnapshot(await api.getSnapshot(run.market_snapshot_id));
    } catch (e) {
      setError(e.message);
    } finally {
      setSnapLoading(false);
    }
  }

  return (
    <div className="card">
      <div className="card-header">
        <h2>Run #{run.id} data quality</h2>
      </div>
      {loading && <p className="muted">Loading…</p>}
      {error && <div className="banner banner-warn">Error: {error}</div>}
      {quality && !quality.market_snapshot_id && (
        <p className="muted">This run has no stored market snapshot (it predates snapshots).</p>
      )}
      {quality && quality.symbols.length > 0 && (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr><th>Symbol</th><th>Flags</th><th>Quote age</th><th>Missing</th></tr>
            </thead>
            <tbody>
              {quality.symbols.map((s) => (
                <tr
                  key={s.symbol}
                  className={s.incomplete ? "row-incomplete" : s.stale ? "row-stale" : ""}
                >
                  <td><strong>{s.symbol}</strong></td>
                  <td>
                    {s.stale && <span className="flag flag-stale">STALE</span>}
                    {s.incomplete && <span className="flag flag-incomplete">INCOMPLETE</span>}
                    {s.no_quality_data && <span className="muted">no data</span>}
                    {!s.stale && !s.incomplete && !s.no_quality_data && <span className="flag-ok">ok</span>}
                  </td>
                  <td>{s.quote_age_sec == null ? "unknown" : `${s.quote_age_sec}s`}</td>
                  <td>{s.missing.length ? s.missing.join(", ") : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {quality && quality.market_snapshot_id && (
        <CollapsibleSection title="Raw snapshot JSON" defaultOpen={false}>
          {!snapshot && (
            <button className="btn btn-small" onClick={loadSnapshot} disabled={snapLoading}>
              {snapLoading ? "Loading…" : `Load snapshot #${quality.market_snapshot_id}`}
            </button>
          )}
          {snapshot && <pre className="json-view">{JSON.stringify(snapshot, null, 2)}</pre>}
        </CollapsibleSection>
      )}
    </div>
  );
}

export default function ResearchPage() {
  const [runs, setRuns] = useState(null);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setRuns(await api.getResearchRuns(20));
    } catch (e) {
      setError(e.message);
      setRuns((r) => r || []);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const run = runs?.find((r) => r.id === selected);

  return (
    <div className="page-stack">
      <div className="card">
        <div className="card-header">
          <h2>Recent analysis runs</h2>
          <button className="btn btn-small" onClick={load}>Refresh</button>
        </div>
        <p className="muted section-note">Costs are estimates from list prices, not invoices. Select a run for details.</p>
        {error && <div className="banner banner-warn">Error: {error}</div>}
        {runs === null && <p className="muted">Loading…</p>}
        {runs && runs.length === 0 && !error && <p className="muted">No analysis runs yet.</p>}
        {runs && runs.length > 0 && (
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  <th>Time</th><th>Model / effort</th><th>Est. cost</th><th>Failed batches</th>
                  <th>Stale</th><th>Incomplete</th><th>Intents</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr
                    key={r.id}
                    className={`row-click ${selected === r.id ? "row-selected" : ""}`}
                    onClick={() => setSelected(r.id)}
                  >
                    <td>{fmtTime(r.created_at)}</td>
                    <td>{r.model}{r.effort ? ` / ${r.effort}` : ""}</td>
                    <td>{fmtCost(r.estimated_cost_usd)}</td>
                    <td className={r.failed_batches > 0 ? "neg" : ""}>
                      {r.failed_batches ?? "—"}{r.batches != null ? ` / ${r.batches}` : ""}
                    </td>
                    <td>{r.quality ? r.quality.stale : "—"}</td>
                    <td>{r.quality ? r.quality.incomplete : "—"}</td>
                    <td>{intentSummary(r.intents_by_status)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      {run && <RunDetail run={run} />}
    </div>
  );
}
