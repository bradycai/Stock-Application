import { useEffect, useState } from "react";
import { ops } from "../../api/ops";
import { usePolling } from "../ops/usePolling";
import ViewState, { fmtNum, fmtPct, fmtTime, fmtUsd } from "../ops/ViewState";

const pnlClass = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");
const ratio = (v) => (v === null || v === undefined ? "—" : `${(Number(v) * 100).toFixed(0)}%`);

function Metric({ label, value, cls = "" }) {
  return (
    <div className="metric">
      <div className="metric-label">{label}</div>
      <div className={`metric-value ${cls}`}>{value}</div>
    </div>
  );
}

function Suggestions({ review, onDecided }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const changes = review.suggested_changes || [];
  const status = review.approval_status || "pending";

  async function decide(decision) {
    const ok = window.confirm(
      `Record "${decision === "approve" ? "accept" : "reject"}" for this review's suggestions?\n\n` +
        "This only records your decision. It changes no setting, strategy, risk limit or order."
    );
    if (!ok) return;
    setBusy(true);
    setError(null);
    try {
      await ops.decideReview(review.id, decision);
      await onDecided();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <h3 className="section-title">Suggested changes</h3>
      {changes.length === 0 ? (
        <p className="muted">No suggestions for this review.</p>
      ) : (
        <>
          <ul className="perf-list">
            {changes.map((c, i) => (
              <li key={i}>
                <strong>{String(c.title)}</strong>
                {c.strategy_id && <span className="muted"> · {String(c.strategy_id)}</span>}
                {c.group && <span className="muted"> · {String(c.group)}</span>}
                <div className="muted">{String(c.rationale || "")}</div>
              </li>
            ))}
          </ul>
          <div className="banner banner-ok">
            Accept or reject only records your decision on these suggestions. It does not change any
            setting, strategy or order.
          </div>
          <div className="perf-decision">
            <span>
              Decision:{" "}
              <span className={`tag ${status === "approved" ? "tag-buy" : status === "rejected" ? "tag-sell" : "tag-watch"}`}>
                {status === "approved" ? "accepted" : status}
              </span>
            </span>
            {status === "pending" && (
              <>
                <button className="btn btn-small btn-approve" disabled={busy} onClick={() => decide("approve")}>
                  {busy ? "Saving…" : "Accept (record only)"}
                </button>
                <button className="btn btn-small btn-reject" disabled={busy} onClick={() => decide("reject")}>
                  Reject (record only)
                </button>
              </>
            )}
          </div>
          {error && <div className="banner banner-warn">Error: {error}</div>}
        </>
      )}
    </div>
  );
}

function ReviewDetail({ id, onChanged }) {
  const [state, setState] = useState({ data: null, error: null, loading: true });

  async function load() {
    try {
      const data = await ops.getReview(id);
      setState({ data, error: null, loading: false });
    } catch (e) {
      setState((s) => ({ ...s, error: e.message, loading: false }));
    }
  }
  useEffect(() => {
    setState({ data: null, error: null, loading: true });
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  const r = state.data;
  const d = r?.data || {};
  const p = d.pnl || {};
  const b = d.benchmark || {};
  const br = d.broker || {};
  const missed = d.missed_ideas || [];
  return (
    <ViewState loading={state.loading} error={state.error} empty={!r && !state.error} emptyText="Review not found.">
      {r && (
        <div className="perf-detail">
          {r.summary && <p>{r.summary}</p>}
          <div className="journal-cards">
            <Metric label="Realized P&L" value={fmtUsd(p.realized_pnl)} cls={pnlClass(p.realized_pnl)} />
            <Metric label="Unrealized P&L" value={fmtUsd(p.unrealized_pnl)} cls={pnlClass(p.unrealized_pnl)} />
            <Metric label="Fees" value={fmtUsd(p.fees)} />
            <Metric label="Slippage (avg bps)" value={fmtNum(p.slippage_bps_avg)} />
            <Metric label="Win rate" value={ratio(p.win_rate)} />
            <Metric label="Expectancy" value={fmtUsd(p.expectancy)} cls={pnlClass(p.expectancy)} />
            <Metric label="Max drawdown" value={fmtPct(p.max_drawdown_pct, 2)} />
            <Metric label="Closed trades" value={p.closed_trades ?? "—"} />
          </div>

          <h3 className="section-title">Versus {b.benchmark || "SPY"}</h3>
          <div className="journal-cards">
            <Metric label={`${b.benchmark || "SPY"} return`} value={fmtPct(b.benchmark_return_pct, 2)} />
            <Metric label="Realized return (of equity)" value={fmtPct(b.realized_return_pct_of_equity, 2)} />
            <Metric label="Excess return" value={fmtPct(b.excess_return_pct, 2)} cls={pnlClass(b.excess_return_pct)} />
          </div>

          <h3 className="section-title">Execution quality</h3>
          <div className="journal-cards">
            <Metric label="Orders submitted" value={br.orders_submitted ?? "—"} />
            <Metric label="Fill rate" value={ratio(br.fill_rate)} />
            <Metric label="Rejected" value={br.rejected ?? "—"} />
            <Metric label="Canceled" value={br.canceled ?? "—"} />
            <Metric label="Expired" value={br.expired ?? "—"} />
            <Metric label="Median submit-to-fill" value={br.submit_to_fill_seconds_median == null ? "—" : `${br.submit_to_fill_seconds_median}s`} />
          </div>

          <h3 className="section-title">Missed ideas ({missed.length})</h3>
          {missed.length === 0 ? (
            <p className="muted">No rejected or abstained ideas in this period.</p>
          ) : (
            <div className="table-scroll">
              <table className="table">
                <thead>
                  <tr><th>Ticker</th><th>Action</th><th>Status</th><th>Strategy</th><th>Forward returns (excess vs benchmark)</th></tr>
                </thead>
                <tbody>
                  {missed.map((m) => (
                    <tr key={m.intent_id}>
                      <td>{m.ticker}</td>
                      <td>{m.action}</td>
                      <td>{m.status}</td>
                      <td>{m.strategy_id || "—"}</td>
                      <td>
                        {(m.outcomes || []).length === 0
                          ? <span className="muted">no outcome yet</span>
                          : m.outcomes.map((o) => (
                              <span key={o.horizon_days} className="perf-chip">
                                {o.horizon_days}d{" "}
                                <span className={pnlClass(o.return_pct)}>{fmtPct(o.return_pct, 2)}</span>
                                {o.excess_return_pct != null && (
                                  <span className="muted"> ({fmtPct(o.excess_return_pct, 2)})</span>
                                )}
                              </span>
                            ))}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {(r.errors || []).length > 0 && (
            <div className="banner banner-warn">
              Data gaps: {r.errors.map((e) => String(e)).join("; ")}
            </div>
          )}

          <Suggestions
            review={r}
            onDecided={async () => {
              await load();
              await onChanged();
            }}
          />
        </div>
      )}
    </ViewState>
  );
}

export default function ReviewsSection() {
  const [period, setPeriod] = useState("");
  const [selected, setSelected] = useState(null);
  const { data, error, loading, unavailable, reload } = usePolling(() => ops.getReviews(period || undefined), 0);
  // refetch when the filter changes
  useEffect(() => {
    reload();
  }, [period, reload]);

  const rows = data || [];
  useEffect(() => {
    if (rows.length && (selected === null || !rows.some((r) => r.id === selected))) setSelected(rows[0].id);
  }, [rows, selected]);

  return (
    <div className="card">
      <div className="card-header">
        <h2>Daily and weekly reviews</h2>
        <div className="perf-actions">
          <select className="filter-input" value={period} onChange={(e) => setPeriod(e.target.value)}>
            <option value="">All periods</option>
            <option value="daily">Daily</option>
            <option value="weekly">Weekly</option>
          </select>
          <button className="btn btn-small" onClick={reload}>Refresh</button>
        </div>
      </div>
      <ViewState loading={loading} unavailable={unavailable} error={error} empty={rows.length === 0}
        emptyText="No reviews yet. Reviews appear after the daily review runs.">
        <div className="table-scroll">
          <table className="table">
            <thead><tr><th>Kind</th><th>Period</th><th>Summary</th><th>Suggestions</th></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id} className={`row-click ${selected === r.id ? "row-selected" : ""}`}
                  onClick={() => setSelected(r.id)}>
                  <td>{r.kind}</td>
                  <td>{fmtTime(r.period_start).split(",")[0]}</td>
                  <td>{String(r.summary || "").slice(0, 140)}</td>
                  <td>{(r.suggested_changes || []).length ? `${r.suggested_changes.length} (${r.approval_status})` : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {selected !== null && <ReviewDetail id={selected} onChanged={reload} />}
      </ViewState>
    </div>
  );
}
