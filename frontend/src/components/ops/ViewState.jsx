// Shared loading / unavailable / error / empty wrapper. Error text is rendered as plain text.
export default function ViewState({ loading, unavailable, error, empty, emptyText, children }) {
  if (unavailable) return <p className="muted">Not available on this server yet.</p>;
  return (
    <>
      {error && <div className="banner banner-warn">Error: {String(error)}</div>}
      {loading ? (
        <p className="muted">Loading…</p>
      ) : empty && !error ? (
        <p className="muted">{emptyText || "Nothing to show."}</p>
      ) : empty ? null : (
        children
      )}
    </>
  );
}

export const fmtAge = (s) => {
  if (s === null || s === undefined) return "—";
  const n = Number(s);
  if (!Number.isFinite(n)) return "—";
  if (n < 90) return `${Math.round(n)}s`;
  if (n < 5400) return `${Math.round(n / 60)}m`;
  return `${(n / 3600).toFixed(1)}h`;
};
export const fmtUsd = (v) =>
  v === null || v === undefined ? "—" : `${Number(v) < 0 ? "-" : ""}$${Math.abs(Number(v)).toFixed(2)}`;
export const fmtPct = (v, d = 1) => (v === null || v === undefined ? "—" : `${Number(v).toFixed(d)}%`);
export const fmtNum = (v, d = 2) => (v === null || v === undefined ? "—" : Number(v).toFixed(d));
export const fmtTime = (t) => (t ? new Date(t).toLocaleString() : "—");
