import { useState } from "react";
import CollapsibleSection from "../components/CollapsibleSection";
import IntentCard from "../components/IntentCard";
import Filters from "../components/Filters";
import { EMPTY_FILTERS, filterIntents } from "../lib/filters";

export default function RecommendationsPage({ intents, minConfidence, reload }) {
  const [filters, setFilters] = useState({ ...EMPTY_FILTERS });
  const visible = filterIntents(intents, filters);

  const conf = (i) => (i.confidence || 0) * 100;
  const awaiting = visible.filter(
    (i) => i.status === "pending_approval" && !i.auto_executed && conf(i) >= minConfidence
  );
  const autoExecuted = visible.filter((i) => i.auto_executed);
  const watch = visible.filter((i) => i.action === "watch" && i.status === "recommended");
  const avoid = visible.filter((i) => i.action === "avoid" && i.status === "recommended");

  const render = (i) => <IntentCard key={i.id} intent={i} onResolved={reload} />;

  return (
    <div className="page">
      <div className="card">
        <div className="card-header">
          <h2>Recommendations</h2>
        </div>
        <Filters value={filters} onChange={setFilters} />
      </div>

      <CollapsibleSection title="Awaiting approval" count={awaiting.length} badgeClass="badge-pending">
        {awaiting.length ? awaiting.map(render) : <p className="muted">None.</p>}
      </CollapsibleSection>

      <CollapsibleSection title="Auto-executed" count={autoExecuted.length} badgeClass="badge-auto">
        {autoExecuted.length ? autoExecuted.map(render) : <p className="muted">None.</p>}
      </CollapsibleSection>

      <CollapsibleSection title="Watch" count={watch.length} defaultOpen={false}>
        {watch.length ? watch.map(render) : <p className="muted">None.</p>}
      </CollapsibleSection>

      <CollapsibleSection
        title="Avoid"
        count={avoid.length}
        defaultOpen={false}
        badgeClass="badge-avoid"
        note="Untradeable or distressed — no position is taken."
      >
        {avoid.length ? avoid.map(render) : <p className="muted">None.</p>}
      </CollapsibleSection>
    </div>
  );
}
