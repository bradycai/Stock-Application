import ViewState, { fmtAge, fmtTime, fmtUsd } from "./ViewState";

const FRESH_WARN_SEC = 900;

function Fresh({ label, sec, marketOpen }) {
  // Staleness only matters while the market is open.
  const stale = marketOpen && sec !== null && sec !== undefined && sec > FRESH_WARN_SEC;
  return (
    <div className="metric">
      <div className="metric-label">{label}</div>
      <div className={`metric-value ${stale ? "neg" : ""}`}>{fmtAge(sec)}</div>
    </div>
  );
}

function detailText(d) {
  if (d === null || d === undefined || d === "") return "";
  return typeof d === "object" ? JSON.stringify(d) : String(d);
}

export default function HealthSection({ status }) {
  const { data: s, error, loading, unavailable, reload } = status;
  const workers = s?.workers || [];
  const jobs = Object.entries(s?.queue?.research_jobs || {});
  const roles = Object.entries(s?.llm?.roles || {});
  const f = s?.data_freshness || {};
  return (
    <div className="card">
      <div className="card-header">
        <h2>System health</h2>
        <div className="perf-actions">
          {s && <span className="muted">updated {fmtTime(s.generated_at)}</span>}
          <button className="btn btn-small" onClick={reload}>Refresh</button>
        </div>
      </div>
      <ViewState loading={loading} unavailable={unavailable} error={error} empty={!s}
        emptyText="No status reported yet.">
        {s && (
          <>
            <h3 className="section-title">Workers</h3>
            {workers.length === 0 ? (
              <p className="muted">No workers have reported a heartbeat.</p>
            ) : (
              <div className="table-scroll">
                <table className="table">
                  <thead><tr><th>Worker</th><th>Status</th><th>Last heartbeat</th><th>Age</th><th>Details</th></tr></thead>
                  <tbody>
                    {workers.map((w) => (
                      <tr key={w.worker} className={w.stale ? "row-stale" : ""}>
                        <td>{w.worker}</td>
                        <td>
                          {String(w.status)}
                          {w.stale && <span className="flag flag-stale"> STALE</span>}
                        </td>
                        <td>{fmtTime(w.last_beat)}</td>
                        <td className={w.stale ? "neg" : ""}>{fmtAge(w.age_sec)}</td>
                        <td className="muted">{detailText(w.details)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            <h3 className="section-title">Research queue</h3>
            {jobs.length === 0 ? (
              <p className="muted">Queue is empty.</p>
            ) : (
              <div className="journal-cards">
                {jobs.map(([k, v]) => (
                  <div className="metric" key={k}>
                    <div className="metric-label">{k}</div>
                    <div className="metric-value">{v}</div>
                  </div>
                ))}
              </div>
            )}

            <h3 className="section-title">
              Data freshness{" "}
              <span className="muted">(market {f.market_open ? "open" : "closed"})</span>
            </h3>
            <div className="journal-cards">
              <Fresh label="Latest quote" sec={f.latest_quote_age_sec} marketOpen={f.market_open} />
              <Fresh label="Latest bar" sec={f.latest_bar_age_sec} marketOpen={f.market_open} />
              <Fresh label="Latest news" sec={f.latest_news_age_sec} marketOpen={f.market_open} />
            </div>

            <h3 className="section-title">
              Claude spend today <span className="muted">(estimate)</span>
            </h3>
            <p>Total <strong>{fmtUsd(s.llm?.spend_today_usd)}</strong></p>
            {roles.length === 0 ? (
              <p className="muted">No role budgets reported.</p>
            ) : (
              <div className="table-scroll">
                <table className="table">
                  <thead><tr><th>Role</th><th>Model</th><th>Effort</th><th>Spend today</th><th>Daily budget</th><th>Used</th></tr></thead>
                  <tbody>
                    {roles.map(([role, r]) => {
                      const pct = r.daily_budget_usd > 0 ? (r.spend_today_usd / r.daily_budget_usd) * 100 : null;
                      return (
                        <tr key={role}>
                          <td>{role}</td>
                          <td>{String(r.model ?? "—")}</td>
                          <td>{String(r.effort ?? "—")}</td>
                          <td>{fmtUsd(r.spend_today_usd)}</td>
                          <td>{r.daily_budget_usd > 0 ? fmtUsd(r.daily_budget_usd) : "none"}</td>
                          <td className={pct !== null && pct >= 100 ? "neg" : pct !== null && pct >= 80 ? "sample-low" : ""}>
                            {pct === null ? "—" : `${pct.toFixed(0)}%`}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </ViewState>
    </div>
  );
}
