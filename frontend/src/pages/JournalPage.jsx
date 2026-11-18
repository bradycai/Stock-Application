import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api/client";

const LOW_SAMPLE = 30;
const HORIZONS = [1, 5, 20];
const TRADED = ["approved", "submitted", "filled"];
const REJECTED = ["rejected_by_risk", "rejected_by_user"];
const DIRECTIONAL = ["buy", "sell"];
const GROUPS = [
  { key: "executed", label: "Executed", test: (r) => TRADED.includes(r.status) && DIRECTIONAL.includes(r.action) },
  { key: "rejected", label: "Rejected", test: (r) => REJECTED.includes(r.status) && DIRECTIONAL.includes(r.action) },
  { key: "watch", label: "Watch", test: (r) => r.action === "watch" },
  { key: "avoid", label: "Avoid", test: (r) => r.action === "avoid" },
];

const num = (v) => (v === null || v === undefined ? null : Number(v));
const pct = (v) => (num(v) === null ? "—" : `${num(v) >= 0 ? "+" : ""}${num(v).toFixed(2)}%`);
const money = (v) =>
  num(v) === null
    ? "—"
    : `${num(v) < 0 ? "-" : ""}$${Math.abs(num(v)).toLocaleString(undefined, {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      })}`;
const px = (v) => (num(v) === null ? "—" : `$${num(v).toFixed(2)}`);
const when = (t) => (t ? new Date(t).toLocaleString() : "—");
const tone = (v) => (num(v) === null ? "" : num(v) > 0 ? "pos" : num(v) < 0 ? "neg" : "");

// Count-weighted merge of the API's aggregate rows (null averages are ignored).
function merge(rows) {
  const n = rows.reduce((a, r) => a + r.count, 0);
  const w = (k) => {
    const use = rows.filter((r) => r[k] !== null && r[k] !== undefined);
    const d = use.reduce((a, r) => a + r.count, 0);
    return d ? use.reduce((a, r) => a + r[k] * r.count, 0) / d : null;
  };
  return {
    n,
    ret: w("avg_return_pct"),
    bench: w("avg_benchmark_return_pct"),
    excess: w("avg_excess_return_pct"),
    hit: w("hit_rate"),
  };
}

function Metric({ label, value, sub, cls = "" }) {
  return (
    <div className="card">
      <div className="metric-label">{label}</div>
      <div className={`metric-value ${cls}`}>{value}</div>
      {sub && <div className="muted">{sub}</div>}
    </div>
  );
}

export default function JournalPage() {
  const [trades, setTrades] = useState(null);
  const [cands, setCands] = useState(null);
  const [error, setError] = useState(null);
  const [syncing, setSyncing] = useState(false);
  const [syncResult, setSyncResult] = useState(null);
  const [syncError, setSyncError] = useState(null);

  const load = useCallback(async () => {
    setError(null);
    const [t, c] = await Promise.allSettled([api.getJournalTrades(200), api.getJournalCandidates()]);
    const errs = [];
    if (t.status === "fulfilled") setTrades(t.value);
    else {
      errs.push(`Trades: ${t.reason.message}`);
      setTrades((x) => x || []);
    }
    if (c.status === "fulfilled") setCands(c.value);
    else {
      errs.push(`Candidates: ${c.reason.message}`);
      setCands((x) => x || []);
    }
    if (errs.length) setError(errs.join(" | "));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function sync() {
    setSyncing(true);
    setSyncError(null);
    setSyncResult(null);
    try {
      setSyncResult(await api.syncJournal());
      await load();
    } catch (e) {
      setSyncError(e.message);
    } finally {
      setSyncing(false);
    }
  }

  const summary = useMemo(() => {
    const closed = (trades || []).filter((t) => t.status === "closed");
    const pnl = closed.reduce((a, t) => a + (num(t.realized_pnl) || 0), 0);
    const wins = closed.filter((t) => num(t.realized_pnl) > 0).length;
    const rel = closed.filter((t) => num(t.realized_pnl_pct) !== null && num(t.benchmark_return_pct) !== null);
    const vs = rel.length
      ? rel.reduce((a, t) => a + (num(t.realized_pnl_pct) - num(t.benchmark_return_pct)), 0) / rel.length
      : null;
    return { closed: closed.length, pnl, winRate: closed.length ? wins / closed.length : null, vs, relN: rel.length };
  }, [trades]);

  const grouped = useMemo(() => {
    const out = {};
    for (const g of GROUPS) {
      out[g.key] = {};
      for (const h of HORIZONS) {
        out[g.key][h] = merge((cands || []).filter((r) => r.horizon_days === h && g.test(r)));
      }
    }
    return out;
  }, [cands]);

  const openCount = (trades || []).filter((t) => t.status !== "closed").length;
  const ruleVersions = [...new Set((cands || []).map((r) => r.rule_version))];
  const o = syncResult?.orders;
  const c = syncResult?.candidates;

  return (
    <div className="page-stack">
      <div className="card-header">
        <h2>Trade journal</h2>
        <div className="journal-actions">
          <button className="btn btn-small" onClick={load} disabled={syncing}>Refresh</button>
          <button
            className="btn btn-primary btn-small"
            onClick={sync}
            disabled={syncing}
            title="Reads order status from the broker and computes missing outcomes. Places no orders."
          >
            {syncing ? "Syncing…" : "Sync now"}
          </button>
        </div>
      </div>
      <p className="muted section-note">
        Historical record for review only. Money figures are in USD; P&amp;L is realized (closed trades
        only). Past results do not predict future results.
      </p>
      {error && <div className="banner banner-warn">Error: {error}</div>}
      {syncError && <div className="banner banner-danger">Sync failed: {syncError}</div>}
      {syncResult && (
        <div className={`banner ${o?.errors ? "banner-warn" : "banner-ok"}`}>
          Sync complete. Orders checked {o?.checked ?? 0}, updated {o?.updated ?? 0}, fills recorded{" "}
          {o?.fills_recorded ?? 0}, errors {o?.errors ?? 0}. Candidate outcomes written {c?.written ?? 0},
          skipped {c?.skipped ?? 0}.
        </div>
      )}
      {(trades === null || cands === null) && <p className="muted">Loading…</p>}

      {trades !== null && (
        <>
          <div className="journal-cards">
            <Metric label="Closed trades" value={summary.closed} sub={`${openCount} open`} />
            <Metric
              label="Win rate"
              value={summary.winRate === null ? "—" : `${(summary.winRate * 100).toFixed(0)}%`}
              sub={`of ${summary.closed} closed`}
            />
            <Metric label="Total realized P&L (USD)" value={money(summary.pnl)} cls={tone(summary.pnl)} />
            <Metric
              label="Avg return vs SPY"
              value={summary.vs === null ? "—" : `${pct(summary.vs)} pts`}
              cls={tone(summary.vs)}
              sub={`trade return minus SPY, n=${summary.relN}`}
            />
          </div>

          <div className="card">
            <div className="card-header"><h2>Trade outcomes</h2></div>
            {trades.length === 0 ? (
              <p className="muted">No trades recorded yet. Use Sync now after orders have filled.</p>
            ) : (
              <div className="table-scroll table-wide">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Status</th><th>Symbol</th><th>Side</th><th>Entry time</th><th>Entry px</th>
                      <th>Exit time</th><th>Exit px</th><th>Qty</th><th>P&amp;L (USD)</th><th>P&amp;L %</th>
                      <th>Days</th><th>Exit reason</th><th>SPY %</th><th>Intent / run / snapshot</th>
                    </tr>
                  </thead>
                  <tbody>
                    {trades.map((t) => {
                      const open = t.status !== "closed";
                      return (
                        <tr key={t.id} className={open ? "row-open" : ""}>
                          <td>
                            <span className={`tag ${open ? "tag-open" : "tag-closed"}`}>
                              {open ? "OPEN" : "closed"}
                            </span>
                          </td>
                          <td><strong>{t.symbol}</strong></td>
                          <td>{t.side || "—"}</td>
                          <td>{when(t.entry_ts)}</td>
                          <td>{px(t.entry_price)}</td>
                          <td>{open ? "—" : when(t.exit_ts)}</td>
                          <td>{open ? "—" : px(t.exit_price)}</td>
                          <td>{t.qty ?? "—"}</td>
                          <td className={tone(t.realized_pnl)}>{open ? "—" : money(t.realized_pnl)}</td>
                          <td className={tone(t.realized_pnl_pct)}>{open ? "—" : pct(t.realized_pnl_pct)}</td>
                          <td>{t.holding_days ?? "—"}</td>
                          <td>{open ? "—" : t.exit_reason || "—"}</td>
                          <td>{pct(t.benchmark_return_pct)}</td>
                          <td className="journal-ids">
                            #{t.intent_id} / {t.run_id ? `#${t.run_id}` : "—"} /{" "}
                            {t.market_snapshot_id ? `#${t.market_snapshot_id}` : "—"}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </>
      )}

      {cands !== null && (
        <div className="card">
          <div className="card-header"><h2>Candidate outcomes (hypothetical)</h2></div>
          <p className="muted section-note">
            Hypothetical forward close-to-close returns (rule {ruleVersions.length ? ruleVersions.join(", ") : "fwd-close-v1"})
            for ideas whether or not they were traded. No fees, slippage or position sizing; not actual
            results. Groups with n &lt; {LOW_SAMPLE} are marked as small samples.
          </p>
          {cands.length === 0 ? (
            <p className="muted">No candidate outcomes yet. They appear once enough price history exists; try Sync now.</p>
          ) : (
            <div className="table-scroll">
              <table className="table">
                <thead>
                  <tr>
                    <th>Group</th><th>Horizon</th><th>n</th><th>Avg return</th><th>Avg SPY</th>
                    <th>Avg excess</th><th>Positive rate</th>
                  </tr>
                </thead>
                <tbody>
                  {GROUPS.flatMap((g) =>
                    HORIZONS.map((h) => {
                      const m = grouped[g.key][h];
                      return (
                        <tr key={`${g.key}-${h}`}>
                          <td>{h === HORIZONS[0] ? <strong>{g.label}</strong> : ""}</td>
                          <td>{h}-day</td>
                          <td className={m.n < LOW_SAMPLE ? "sample-low" : ""}>
                            {m.n}{m.n > 0 && m.n < LOW_SAMPLE ? " (small sample)" : ""}
                          </td>
                          <td className={tone(m.ret)}>{pct(m.ret)}</td>
                          <td>{pct(m.bench)}</td>
                          <td className={tone(m.excess)}>{pct(m.excess)}</td>
                          <td>{m.hit === null ? "—" : `${(m.hit * 100).toFixed(0)}%`}</td>
                        </tr>
                      );
                    })
                  )}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
