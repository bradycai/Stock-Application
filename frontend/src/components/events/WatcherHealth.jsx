import { fmtTime, textOf } from "../proposals/format";

const usd = (v) => (v == null ? "—" : `$${Number(v).toFixed(2)}`);

export default function WatcherHealth({ status }) {
  const hb = status.heartbeat;
  const b = status.budget || {};
  const q = status.queue || {};
  const lc = status.last_cycle;
  return (
    <div className="pe-grid">
      <div>
        <span className="muted">Watcher</span><br />
        {!status.enabled ? "disabled" : !hb ? "enabled, no heartbeat yet" : hb.stale ? "STALE" : hb.status}
        {status.enabled && hb?.stale && <span className="flag flag-incomplete">STALE</span>}
      </div>
      <div>
        <span className="muted">Heartbeat age</span><br />
        {hb ? `${hb.age_seconds}s` : "—"} <span className="muted">(every {status.interval_seconds}s)</span>
      </div>
      <div><span className="muted">Last beat</span><br />{fmtTime(hb?.last_beat)}</div>
      <div><span className="muted">Model</span><br />{textOf(status.model)}</div>
      <div>
        <span className="muted">Budget today</span><br />
        {usd(b.daily_spent_usd)} / {usd(b.daily_limit_usd)}
      </div>
      <div>
        <span className="muted">Budget month</span><br />
        {usd(b.monthly_spent_usd)} / {usd(b.monthly_limit_usd)}
      </div>
      <div>
        <span className="muted">Queue</span><br />
        {Object.keys(q).length ? Object.entries(q).map(([k, n]) => `${k}: ${n}`).join(", ") : "—"}
      </div>
      <div>
        <span className="muted">Last cycle</span><br />
        {lc ? textOf(lc) : "—"}
      </div>
    </div>
  );
}
