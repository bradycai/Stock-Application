import { useState } from "react";
import { api } from "../api/client";

export default function IntentCard({ intent, onResolved, archivable = false }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const isActionable = intent.status === "pending_approval";
  const isWatch = intent.action === "watch" || intent.action === "avoid";

  async function archive() {
    setBusy(true);
    setError(null);
    try {
      await api.deleteIntent(intent.id);
      onResolved();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function approve() {
    const live = intent.is_live;
    const msg = live
      ? `⚠️ LIVE ORDER: ${intent.action.toUpperCase()} ${intent.computed_qty} ${intent.ticker} @ $${intent.limit_price}. This uses REAL money. Continue?`
      : `Approve ${intent.action.toUpperCase()} ${intent.computed_qty || "?"} ${intent.ticker} @ $${intent.limit_price} (paper)?`;
    if (!confirm(msg)) return;
    setBusy(true);
    setError(null);
    try {
      await api.approveIntent(intent.id);
      onResolved();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function reject() {
    const reason = prompt("Reason for rejection?", "Not convinced by the thesis");
    if (reason === null) return;
    setBusy(true);
    setError(null);
    try {
      await api.rejectIntent(intent.id, reason);
      onResolved();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const actionClass =
    intent.action === "buy"
      ? "tag-buy"
      : intent.action === "sell"
      ? "tag-sell"
      : intent.action === "avoid"
      ? "tag-avoid"
      : "tag-watch";

  return (
    <div className={`intent-card ${isWatch ? "intent-watch" : ""}`}>
      <div className="intent-top">
        <div>
          <span className={`tag ${actionClass}`}>{intent.action.toUpperCase()}</span>
          <strong className="intent-ticker">{intent.ticker}</strong>
          {intent.auto_executed && <span className="tag tag-auto">AUTO</span>}
          <span className="status-pill">{intent.status.replace(/_/g, " ")}</span>
        </div>
        <div className="intent-top-right">
          <div className="confidence">
            <span className="muted">confidence</span>
            <div className="conf-bar">
              <div className="conf-fill" style={{ width: `${(intent.confidence || 0) * 100}%` }} />
            </div>
            <span>{((intent.confidence || 0) * 100).toFixed(0)}%</span>
          </div>
          {archivable && (
            <button className="chip-x" title="Archive (hide from view)" disabled={busy} onClick={archive}>
              ✕
            </button>
          )}
        </div>
      </div>

      {!isWatch && (
        <div className="intent-grid">
          <div><span className="muted">Limit</span> ${intent.limit_price}</div>
          <div><span className="muted">Size</span> {intent.position_size_percent}%</div>
          <div><span className="muted">Stop</span> {intent.stop_loss_percent}%</div>
          <div><span className="muted">Target</span> {intent.take_profit_percent}%</div>
          {intent.computed_qty != null && (
            <div><span className="muted">Qty</span> {intent.computed_qty} sh</div>
          )}
          {intent.computed_notional != null && (
            <div><span className="muted">Notional</span> ${intent.computed_notional.toFixed(2)}</div>
          )}
        </div>
      )}

      <p className="intent-reason"><strong>Thesis:</strong> {intent.reason}</p>
      <p className="intent-risks"><strong>Risks:</strong> {intent.risks}</p>

      {intent.risk_violations && intent.risk_violations.length > 0 && (
        <div className="violations">
          {intent.risk_violations.map((v) => (
            <span className="violation-chip" key={v.code} title={v.message}>{v.code}</span>
          ))}
        </div>
      )}

      {error && <div className="inline-error">{error}</div>}

      {isActionable && (
        <div className="intent-actions">
          <button className="btn btn-approve" disabled={busy} onClick={approve}>Approve</button>
          <button className="btn btn-reject" disabled={busy} onClick={reject}>Reject</button>
        </div>
      )}
    </div>
  );
}
