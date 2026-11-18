import { useEffect, useState } from "react";
import { ops } from "../../api/ops";
import { usePolling } from "../ops/usePolling";
import ViewState, { fmtTime } from "../ops/ViewState";

const KINDS = { baseline: "Baseline", ai_replay: "AI replay", paper_report: "Paper fills" };

function Summary({ summary }) {
  if (summary === null || summary === undefined) return <span className="muted">—</span>;
  if (typeof summary !== "object") return <span>{String(summary)}</span>;
  return (
    <span>
      {Object.entries(summary).slice(0, 6).map(([k, v]) => (
        <span key={k} className="perf-chip">
          <span className="muted">{k.replace(/_/g, " ")}</span>{" "}
          {typeof v === "object" ? JSON.stringify(v) : String(v)}
        </span>
      ))}
    </span>
  );
}

function ReportDetail({ name }) {
  const [state, setState] = useState({ data: null, error: null, loading: true, unavailable: false });
  useEffect(() => {
    let alive = true;
    setState({ data: null, error: null, loading: true, unavailable: false });
    ops.getBacktestReport(name)
      .then((data) => alive && setState({ data, error: null, loading: false, unavailable: false }))
      .catch((e) => alive && setState({ data: null, error: e.message, loading: false, unavailable: false }));
    return () => { alive = false; };
  }, [name]);
  return (
    <ViewState loading={state.loading} error={state.error} empty={!state.data && !state.error}
      emptyText="Report is empty.">
      {state.data && <pre className="json-view">{JSON.stringify(state.data, null, 2)}</pre>}
    </ViewState>
  );
}

export default function BacktestSection() {
  const { data, error, loading, unavailable, reload } = usePolling(() => ops.getBacktestReports(), 0);
  const [open, setOpen] = useState(null);
  const rows = data || [];
  return (
    <div className="card">
      <div className="card-header">
        <h2>Backtest reports</h2>
        <button className="btn btn-small" onClick={reload}>Refresh</button>
      </div>
      <p className="muted section-note">
        Baseline, AI replay and paper-fill reports. These are simulations and not a promise of live results.
      </p>
      <ViewState loading={loading} unavailable={unavailable} error={error} empty={rows.length === 0}
        emptyText="No backtest reports yet.">
        <div className="table-scroll">
          <table className="table">
            <thead><tr><th>Name</th><th>Kind</th><th>Created</th><th>Summary</th></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.name} className={`row-click ${open === r.name ? "row-selected" : ""}`}
                  onClick={() => setOpen(open === r.name ? null : r.name)}>
                  <td>{r.name}</td>
                  <td>{KINDS[r.kind] || String(r.kind)}</td>
                  <td>{fmtTime(r.created_at)}</td>
                  <td><Summary summary={r.summary} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {open && <ReportDetail name={open} />}
      </ViewState>
    </div>
  );
}
