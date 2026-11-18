import { fmtTime, textOf } from "../proposals/format";

export default function JobQueue({ jobs }) {
  return (
    <div className="table-scroll">
      <table className="table">
        <thead>
          <tr><th>#</th><th>Kind</th><th>Symbol</th><th>Status</th><th>Attempts</th><th>Updated</th><th>Last error</th></tr>
        </thead>
        <tbody>
          {jobs.map((j) => (
            <tr key={j.id}>
              <td>{j.id}</td>
              <td>{j.kind}</td>
              <td><strong>{j.symbol}</strong></td>
              <td>{j.status}</td>
              <td>{j.attempts}/{j.max_attempts}</td>
              <td>{fmtTime(j.updated_at)}</td>
              <td>{j.last_error ? textOf(j.last_error) : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
