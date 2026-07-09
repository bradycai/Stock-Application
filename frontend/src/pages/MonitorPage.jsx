import { useEffect, useState } from "react";
import { api } from "../api/client";
import CollapsibleSection from "../components/CollapsibleSection";
import PositionManager from "../components/PositionManager";

const EVENT_LABELS = {
  monitor_trigger: { text: "Trigger", cls: "ev-info" },
  monitor_decision: { text: "Claude", cls: "badge-pending" },
  monitor_auto_sell: { text: "Auto-sell", cls: "ev-sell" },
  monitor_stop_adjust: { text: "Stop/Take", cls: "ev-take" },
  monitor_ignored: { text: "Ignored", cls: "ev-reject" },
  monitor_error: { text: "Error", cls: "ev-halt" },
};

function summarize(ev) {
  const p = ev.payload || {};
  if (ev.event_type === "monitor_trigger")
    return `${p.symbol}: ${(p.triggers || []).map((t) => t.type).join(", ")}`;
  if (ev.event_type === "monitor_decision")
    return `${p.symbol}: ${p.action} @ ${p.confidence}% — ${p.reason || ""}`;
  if (ev.event_type === "monitor_auto_sell")
    return `${p.symbol}: ${p.kind} (${p.confidence}%)`;
  if (ev.event_type === "monitor_stop_adjust")
    return `${p.symbol}: ${p.kind} ${p.new_stop ?? p.new_take ?? ""}`;
  if (ev.event_type === "monitor_ignored")
    return `${p.symbol || ""}: ${p.action || ""} — ${p.reason || ""}`;
  if (ev.event_type === "monitor_error") return `${p.symbol || ""}: ${p.error || ""}`;
  return JSON.stringify(p);
}

export default function MonitorPage() {
  const [status, setStatus] = useState(null);
  const [events, setEvents] = useState([]);

  async function refresh() {
    setStatus(await api.getMonitorStatus().catch(() => null));
    setEvents(await api.getMonitorEvents().catch(() => []));
  }

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 15000);
    return () => clearInterval(t);
  }, []);

  const filt = (types) => events.filter((e) => types.includes(e.event_type));

  const row = (ev) => {
    const meta = EVENT_LABELS[ev.event_type] || { text: ev.event_type, cls: "ev-info" };
    return (
      <li key={ev.id} className="event-item">
        <span className={`event-tag ${meta.cls}`}>{meta.text}</span>
        <span className="event-summary">{summarize(ev)}</span>
        <span className="event-time muted">{new Date(ev.ts).toLocaleTimeString()}</span>
      </li>
    );
  };

  const list = (types, empty) => {
    const items = filt(types);
    return items.length ? (
      <ul className="event-list">{items.map(row)}</ul>
    ) : (
      <p className="muted">{empty}</p>
    );
  };

  const stat = (label, value, cls = "") => (
    <div className="run-stat">
      <span className={`run-stat-value ${cls}`}>{value}</span>
      <span className="run-stat-label">{label}</span>
    </div>
  );

  return (
    <div className="page">
      <PositionManager />
      <div className="card">
        <div className="card-header">
          <h2>Continuous Monitor</h2>
          <span className={`pill ${status?.is_paper ? "pill-paper" : "pill-live"}`}>
            {status?.is_paper ? "PAPER" : "LIVE"}
          </span>
        </div>
        {!status ? (
          <p className="muted">Loading…</p>
        ) : (
          <>
            <div className="automation-row">
              <div>
                <div className="auto-state">
                  {status.enabled ? (
                    <span className="auto-on">● MONITOR ON</span>
                  ) : (
                    <span className="auto-off">○ Monitor off</span>
                  )}
                </div>
                <div className="muted auto-reason">{status.capability_reason}</div>
              </div>
              <div className="muted">
                Last check:{" "}
                {status.last_check_at ? new Date(status.last_check_at).toLocaleTimeString() : "—"}
                <br />
                every {status.interval_sec}s · auto-sell ≥ {status.auto_sell_confidence}% · auto-buy ≥{" "}
                {status.auto_buy_confidence}%
              </div>
            </div>
            <div className="run-summary-grid" style={{ marginTop: 12 }}>
              {stat("Monitored", status.monitored)}
              {stat("Triggers", status.triggers)}
              {stat("Claude calls", status.claude_calls)}
              {stat("Auto-sells", status.auto_sells)}
              {stat("Buys", status.buys)}
              {stat("Errors", status.errors, status.errors > 0 ? "neg" : "")}
            </div>
            {!status.enabled && (
              <p className="muted section-note">
                Enable with <code>CONTINUOUS_MONITOR_ENABLED=true</code> in backend/.env and
                restart. Paper only unless live is explicitly confirmed.
              </p>
            )}
          </>
        )}
      </div>

      <CollapsibleSection title="Triggers detected" count={filt(["monitor_trigger"]).length}>
        {list(["monitor_trigger"], "No triggers yet.")}
      </CollapsibleSection>
      <CollapsibleSection title="Claude monitor decisions" count={filt(["monitor_decision"]).length}>
        {list(["monitor_decision"], "No decisions yet.")}
      </CollapsibleSection>
      <CollapsibleSection title="Auto-sells / adjustments" count={filt(["monitor_auto_sell", "monitor_stop_adjust"]).length}>
        {list(["monitor_auto_sell", "monitor_stop_adjust"], "None yet.")}
      </CollapsibleSection>
      <CollapsibleSection title="Ignored triggers" count={filt(["monitor_ignored", "monitor_error"]).length} defaultOpen={false}>
        {list(["monitor_ignored", "monitor_error"], "None.")}
      </CollapsibleSection>
    </div>
  );
}
