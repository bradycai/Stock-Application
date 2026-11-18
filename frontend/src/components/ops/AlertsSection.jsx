import { useEffect, useRef, useState } from "react";
import { ops } from "../../api/ops";
import { usePolling } from "./usePolling";
import ViewState, { fmtTime } from "./ViewState";

const SEV = { critical: "neg", warning: "sample-low" };

function detailText(d) {
  if (d === null || d === undefined || d === "") return "";
  return typeof d === "object" ? JSON.stringify(d) : String(d);
}

export default function AlertsSection() {
  const [filter, setFilter] = useState("open");
  const { data, error, loading, unavailable, reload } = usePolling(() => ops.getAlerts(filter), 30000);
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    reload();
  }, [filter, reload]);
  const [acking, setAcking] = useState({});
  const [ackError, setAckError] = useState(null);
  const rows = data || [];

  async function ack(id) {
    setAcking((a) => ({ ...a, [id]: true }));
    setAckError(null);
    try {
      await ops.ackAlert(id);
      await reload();
    } catch (e) {
      setAckError(e.message);
    } finally {
      setAcking((a) => ({ ...a, [id]: false }));
    }
  }

  return (
    <div className="card">
      <div className="card-header">
        <h2>Alerts</h2>
        <div className="perf-actions">
          <select className="filter-input" value={filter}
            onChange={(e) => setFilter(e.target.value)}>
            <option value="open">Open</option>
            <option value="acked">Acknowledged</option>
            <option value="resolved">Resolved</option>
            <option value="all">All</option>
          </select>
          <button className="btn btn-small" onClick={reload}>Refresh</button>
        </div>
      </div>
      <p className="muted section-note">Acknowledging an alert only marks it as seen. It does not change any trading control.</p>
      {ackError && <div className="banner banner-warn">Could not acknowledge: {ackError}</div>}
      <ViewState loading={loading} unavailable={unavailable} error={error} empty={rows.length === 0}
        emptyText={`No ${filter === "all" ? "" : filter + " "}alerts.`}>
        <div className="table-scroll">
          <table className="table">
            <thead><tr><th>Severity</th><th>Alert</th><th>Seen</th><th>Count</th><th>Status</th><th></th></tr></thead>
            <tbody>
              {rows.map((a) => (
                <tr key={a.id}>
                  <td className={SEV[a.severity] || ""}>{String(a.severity)}</td>
                  <td>
                    <div>{String(a.message)}</div>
                    <div className="muted">{String(a.kind)}{a.key ? ` · ${a.key}` : ""} {detailText(a.details)}</div>
                  </td>
                  <td className="muted">
                    first {fmtTime(a.first_seen)}<br />last {fmtTime(a.last_seen)}
                  </td>
                  <td>{a.count}</td>
                  <td>{String(a.status)}{a.acked_at ? <div className="muted">{fmtTime(a.acked_at)}</div> : null}</td>
                  <td>
                    {a.status === "open" && (
                      <button className="btn btn-small" disabled={!!acking[a.id]} onClick={() => ack(a.id)}>
                        {acking[a.id] ? "Acknowledging…" : "Acknowledge"}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </ViewState>
    </div>
  );
}
