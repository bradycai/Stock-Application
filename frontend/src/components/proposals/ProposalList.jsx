import { fmtPct, fmtTime } from "./format";

export const STATUS_FILTERS = ["", "proposed", "converted", "rejected", "abstained", "expired"];

export default function ProposalList({ proposals, selectedId, onSelect }) {
  return (
    <div className="table-scroll">
      <table className="table">
        <thead>
          <tr><th>#</th><th>Symbol</th><th>Action</th><th>Conf.</th><th>Status</th><th>Created</th></tr>
        </thead>
        <tbody>
          {proposals.map((p) => (
            <tr
              key={p.id}
              className={`row-click ${p.id === selectedId ? "row-selected" : ""}`}
              onClick={() => onSelect(p.id)}
            >
              <td>{p.id}</td>
              <td><strong>{p.symbol}</strong></td>
              <td>{p.action}</td>
              <td>{fmtPct(p.confidence)}</td>
              <td><span className={`pe-status pe-status-${p.status}`}>{p.status}</span></td>
              <td>{fmtTime(p.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
