import { useState } from "react";
import CollapsibleSection from "../components/CollapsibleSection";
import IntentCard from "../components/IntentCard";
import AutoEventsLog from "../components/AutoEventsLog";
import Filters from "../components/Filters";
import { EMPTY_FILTERS, filterIntents } from "../lib/filters";
import { api } from "../api/client";

export default function HistoryPage({ intents, minConfidence, reload }) {
  const [filters, setFilters] = useState({ ...EMPTY_FILTERS });
  const [busy, setBusy] = useState(false);
  const visible = filterIntents(intents, filters);

  const conf = (i) => (i.confidence || 0) * 100;
  const latestRun = intents.reduce((m, i) => Math.max(m, i.run_id || 0), 0);

  const rejectedRisk = visible.filter((i) => i.status === "rejected_by_risk");
  const rejectedConfidence = visible.filter(
    (i) => i.status === "pending_approval" && !i.auto_executed && conf(i) < minConfidence
  );
  const historical = visible.filter(
    (i) =>
      i.run_id !== latestRun &&
      ["recommended", "pending_approval", "rejected_by_user"].includes(i.status)
  );

  async function clearRejected() {
    if (!confirm("Archive all rejected items? They're hidden from view but kept in the audit log.")) return;
    setBusy(true);
    try {
      await api.clearRejectedIntents();
      reload();
    } finally {
      setBusy(false);
    }
  }

  async function clearWatch() {
    if (!confirm("Archive all watch/avoid recommendations?")) return;
    setBusy(true);
    try {
      await api.clearWatchIntents();
      reload();
    } finally {
      setBusy(false);
    }
  }

  const render = (i) => (
    <IntentCard key={i.id} intent={i} onResolved={reload} archivable />
  );

  const clearBtn = (label, fn) => (
    <button className="btn btn-small" disabled={busy} onClick={fn}>
      {label}
    </button>
  );

  return (
    <div className="page">
      <div className="card">
        <div className="card-header">
          <h2>Rejected / History</h2>
        </div>
        <Filters value={filters} onChange={setFilters} />
        <p className="muted section-note">
          Clearing archives items (hidden from the UI) — the server-side audit log is
          always preserved.
        </p>
      </div>

      <CollapsibleSection
        title="Rejected by risk engine"
        count={rejectedRisk.length}
        badgeClass="badge-reject"
        actions={clearBtn("Clear all rejected", clearRejected)}
      >
        {rejectedRisk.length ? rejectedRisk.map(render) : <p className="muted">None.</p>}
      </CollapsibleSection>

      <CollapsibleSection
        title="Rejected by confidence"
        count={rejectedConfidence.length}
        badgeClass="badge-conf"
        note={`Below the ${minConfidence}% auto-trade threshold — not auto-executed, but still manually approvable.`}
      >
        {rejectedConfidence.length ? rejectedConfidence.map(render) : <p className="muted">None.</p>}
      </CollapsibleSection>

      <CollapsibleSection
        title="Historical recommendations (previous runs)"
        count={historical.length}
        defaultOpen={false}
        actions={clearBtn("Clear old watch", clearWatch)}
        note="From earlier analysis runs — kept for reference."
      >
        {historical.length ? historical.map(render) : <p className="muted">None.</p>}
      </CollapsibleSection>

      <AutoEventsLog clearable />
    </div>
  );
}
