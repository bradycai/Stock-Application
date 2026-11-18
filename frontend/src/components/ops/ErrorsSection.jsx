import ViewState, { fmtTime } from "./ViewState";

export default function ErrorsSection({ status }) {
  const { data: s, error, loading, unavailable } = status;
  const errs = s?.recent_errors || [];
  return (
    <div className="card">
      <div className="card-header"><h2>Recent errors</h2></div>
      <ViewState loading={loading} unavailable={unavailable} error={error} empty={errs.length === 0}
        emptyText="No recent errors.">
        <div className="table-scroll">
          <table className="table">
            <thead><tr><th>Time</th><th>Event</th><th>Detail</th></tr></thead>
            <tbody>
              {errs.map((e, i) => (
                <tr key={`${e.ts}-${i}`}>
                  <td>{fmtTime(e.ts)}</td>
                  <td>{String(e.event)}</td>
                  <td className="muted">
                    {typeof e.detail === "object" && e.detail !== null ? JSON.stringify(e.detail) : String(e.detail ?? "")}
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
