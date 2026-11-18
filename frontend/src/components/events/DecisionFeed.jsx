import { fmtTime, textOf } from "../proposals/format";

function PriceCell({ d }) {
  const pct = d.price_change_pct;
  const hasPct = typeof pct === "number";
  const hasPrice = typeof d.last_price === "number";
  if (!hasPct && !hasPrice) return "—";
  return (
    <>
      {hasPct ? (
        <span className={pct > 0 ? "pos" : pct < 0 ? "neg" : ""}>
          {pct > 0 ? "+" : ""}{pct.toFixed(2)}%
        </span>
      ) : "—"}
      {hasPrice && <span className="muted"> · ${d.last_price.toFixed(2)}</span>}
    </>
  );
}

export default function DecisionFeed({ decisions }) {
  return (
    <div className="table-scroll table-wide">
      <table className="table">
        <thead>
          <tr>
            <th>Time</th><th>Symbol</th><th>Priority</th><th>Price</th><th>Event</th><th>Decision</th>
            <th>Evidence / signals</th><th>Explanation</th>
          </tr>
        </thead>
        <tbody>
          {decisions.map((d) => (
            <tr key={d.id}>
              <td>{fmtTime(d.created_at)}</td>
              <td><strong>{d.symbol}</strong></td>
              <td><span className={`pe-urgency pe-urgency-${d.urgency}`}>{d.urgency}</span></td>
              <td><PriceCell d={d} /></td>
              <td>{textOf(d.event_type)}</td>
              <td>
                {d.action}
                {d.downgraded && <span className="flag flag-stale">DOWNGRADED</span>}
              </td>
              <td className="journal-ids">
                {(d.evidence_ids || []).join(", ") || "—"}
                {d.signal_event_ids?.length ? ` · signals: ${d.signal_event_ids.join(", ")}` : ""}
              </td>
              <td>{textOf(d.explanation)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
