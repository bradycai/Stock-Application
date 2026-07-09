import { useEffect, useState } from "react";
import { api } from "../api/client";

export default function AutomationPanel({ onChange }) {
  const [status, setStatus] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function refresh() {
    try {
      const s = await api.getAutomationStatus();
      setStatus(s);
      setError(null);
      onChange?.(s);
    } catch (e) {
      setError(e.message);
    }
  }

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 15000);
    return () => clearInterval(t);
  }, []);

  async function toggle() {
    if (!status) return;
    setBusy(true);
    setError(null);
    try {
      if (status.active) {
        await api.disableAutomation();
      } else {
        if (!status.is_paper) {
          const ok = confirm(
            "⚠️ Trading mode is LIVE. Enabling autonomous trading will place REAL orders " +
              "without manual approval. Continue?"
          );
          if (!ok) {
            setBusy(false);
            return;
          }
        }
        await api.enableAutomation();
      }
      await refresh();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  if (!status) {
    return <div className="card">Loading automation status…</div>;
  }

  const canToggle = status.capability_allowed || status.active;

  return (
    <div className="card">
      <div className="card-header">
        <h2>Autonomous Trading</h2>
        <span className={`pill ${status.is_paper ? "pill-paper" : "pill-live"}`}>
          {status.is_paper ? "PAPER" : "LIVE"}
        </span>
      </div>

      <div className="automation-row">
        <div>
          <div className="auto-state">
            {status.active ? (
              <span className="auto-on">● AUTO-TRADING ON</span>
            ) : (
              <span className="auto-off">○ Auto-trading off</span>
            )}
          </div>
          <div className="muted auto-reason">{status.active_reason}</div>
        </div>
        <button
          className={`btn ${status.active ? "btn-stop" : "btn-approve"}`}
          onClick={toggle}
          disabled={busy || !canToggle}
        >
          {status.active ? "Turn OFF" : "Turn ON"}
        </button>
      </div>

      {status.is_paper ? (
        <div className="auto-note auto-note-ok">
          Paper mode — autonomous orders are simulated. Min confidence to auto-execute:{" "}
          <strong>{status.min_confidence}%</strong>. Monitor every{" "}
          {Math.round(status.monitor_interval_sec / 60)} min.
        </div>
      ) : (
        <div className="auto-note auto-note-warn">
          ⚠️ LIVE mode — autonomous orders use real money. Requires
          AUTO_LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_THE_RISK on the server.
        </div>
      )}

      {!status.capability_allowed && !status.active && (
        <div className="muted auto-reason">
          Enable requires server config: {status.capability_reason}
        </div>
      )}

      {error && <div className="inline-error">{error}</div>}
    </div>
  );
}
