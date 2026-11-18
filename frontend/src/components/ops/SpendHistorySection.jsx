import { useMemo } from "react";
import { ops } from "../../api/ops";
import { usePolling } from "./usePolling";
import ViewState from "./ViewState";

export default function SpendHistorySection() {
  const { data, error, loading, unavailable } = usePolling(() => ops.getLlmUsage(7), 30000);
  const days = useMemo(() => {
    const m = {};
    (data?.groups || []).forEach((g) => {
      m[g.day] ||= { day: g.day, calls: 0, failures: 0, cost: 0 };
      m[g.day].calls += g.calls;
      m[g.day].failures += g.failures;
      m[g.day].cost += g.estimated_cost_usd;
    });
    return Object.values(m).sort((a, b) => b.day.localeCompare(a.day));
  }, [data]);
  return (
    <div className="card">
      <div className="card-header"><h2>Claude spend history (7 days)</h2></div>
      <p className="muted section-note">Estimates from list prices, not invoices. Days are UTC.</p>
      <ViewState loading={loading} unavailable={unavailable} error={error} empty={days.length === 0}
        emptyText="No Claude calls in this period.">
        <div className="table-scroll">
          <table className="table">
            <thead><tr><th>Day (UTC)</th><th>Calls</th><th>Failures</th><th>Est. cost</th></tr></thead>
            <tbody>
              {days.map((d) => (
                <tr key={d.day}>
                  <td>{d.day}</td><td>{d.calls}</td>
                  <td className={d.failures ? "neg" : ""}>{d.failures}</td>
                  <td>~${d.cost.toFixed(4)}</td>
                </tr>
              ))}
            </tbody>
            {data?.totals && (
              <tfoot>
                <tr><td>Total</td><td>{data.totals.calls}</td><td>{data.totals.failures}</td>
                  <td>~${Number(data.totals.estimated_cost_usd || 0).toFixed(4)}</td></tr>
              </tfoot>
            )}
          </table>
        </div>
      </ViewState>
    </div>
  );
}
