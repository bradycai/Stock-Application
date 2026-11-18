import { useCallback, useEffect, useState } from "react";
import { researchApi } from "../api/research";
import ProposalList, { STATUS_FILTERS } from "../components/proposals/ProposalList";
import ProposalDetail from "../components/proposals/ProposalDetail";

export default function ProposalsPage() {
  const [status, setStatus] = useState("");
  const [proposals, setProposals] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [selectedId, setSelectedId] = useState(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setProposals(await researchApi.listProposals(status));
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [status]);

  useEffect(() => {
    setLoading(true);
    load();
  }, [load]);

  return (
    <div className="page-stack">
      <div className="card">
        <div className="card-header">
          <h2>Strategist proposals</h2>
          <div className="pe-row">
            <select className="filter-input" value={status} onChange={(e) => setStatus(e.target.value)}>
              {STATUS_FILTERS.map((s) => (
                <option key={s} value={s}>{s || "all statuses"}</option>
              ))}
            </select>
            <button className="btn btn-small" onClick={() => { setLoading(true); load(); }}>Refresh</button>
          </div>
        </div>
        {loading && <p className="muted">Loading proposals…</p>}
        {error && <div className="banner banner-warn">Error: {error}</div>}
        {!loading && !error && proposals?.length === 0 && (
          <p className="muted">
            No proposals{status ? ` with status "${status}"` : ""} yet. They appear after the strategist
            processes a research bundle.
          </p>
        )}
        {!loading && proposals?.length > 0 && (
          <ProposalList proposals={proposals} selectedId={selectedId} onSelect={setSelectedId} />
        )}
      </div>
      {selectedId != null && <ProposalDetail id={selectedId} onChanged={load} />}
    </div>
  );
}
