import { useEffect, useState } from "react";
import { api } from "../api/client";

const MODES = [
  {
    key: "manual",
    label: "Manual",
    help: "Nothing is automated. Every trade waits for your approval.",
  },
  {
    key: "observe",
    label: "Observe",
    help: "Automation runs and records what it WOULD do, but places no orders.",
  },
  {
    key: "auto",
    label: "Auto",
    help: "Eligible high-confidence trades execute automatically, after the risk engine.",
  },
];

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

  async function run(fn) {
    setBusy(true);
    setError(null);
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  function chooseMode(mode) {
    if (!status || mode === status.mode) return;
    if (mode === "auto") {
      const warning = status.is_paper
        ? "Switch to AUTO? Eligible trades will be placed on the PAPER account without your approval."
        : "⚠️ Trading mode is LIVE. AUTO will place REAL orders without your approval. Continue?";
      if (!confirm(warning)) return;
    }
    run(() => api.setAutomationMode(mode));
  }

  function engageKillSwitch() {
    const reason = prompt(
      "ENGAGE KILL SWITCH: blocks all automated orders and all new buys until released, and " +
        "sets the mode to Manual. Reason (optional):"
    );
    if (reason === null) return; // cancelled
    run(() => api.setKillSwitch(true, reason));
  }

  function releaseKillSwitch() {
    if (!confirm("Release the kill switch? Automation stays in Manual until you choose a mode.")) return;
    run(() => api.setKillSwitch(false));
  }

  if (!status) {
    return <div className="card">{error ? `Automation status error: ${error}` : "Loading automation status…"}</div>;
  }

  const autoBlocked = !status.capability_allowed || status.kill_switch;

  return (
    <div className="card">
      <div className="card-header">
        <h2>Automation</h2>
        <span className={`pill ${status.is_paper ? "pill-paper" : "pill-live"}`}>
          {status.is_paper ? "PAPER" : "LIVE"}
        </span>
      </div>

      {status.kill_switch ? (
        <div className="kill-switch kill-switch-on">
          <div>
            <strong>⛔ KILL SWITCH ENGAGED</strong>
            <div className="muted">
              {status.kill_switch_reason}
              {status.kill_switch_by && ` — by ${status.kill_switch_by}`}
              {status.kill_switch_at && `, ${new Date(status.kill_switch_at).toLocaleString()}`}
            </div>
            <div className="muted">
              Automated orders and new buys are blocked. Manual sells and broker-side stops still work.
            </div>
          </div>
          <button className="btn btn-resume" onClick={releaseKillSwitch} disabled={busy}>
            Release
          </button>
        </div>
      ) : (
        <div className="kill-switch">
          <span className="muted">Kill switch off</span>
          <button className="btn btn-stop" onClick={engageKillSwitch} disabled={busy}>
            🛑 Engage kill switch
          </button>
        </div>
      )}

      <div className="mode-select" role="radiogroup" aria-label="Automation mode">
        {MODES.map((m) => {
          const disabled = busy || (m.key === "auto" && autoBlocked && status.mode !== "auto");
          return (
            <button
              key={m.key}
              role="radio"
              aria-checked={status.mode === m.key}
              className={`mode-option ${status.mode === m.key ? "mode-option-active" : ""}`}
              onClick={() => chooseMode(m.key)}
              disabled={disabled}
              title={m.help}
            >
              {m.label}
            </button>
          );
        })}
      </div>
      <div className="muted auto-reason">{MODES.find((m) => m.key === status.mode)?.help}</div>

      <div className="auto-state">
        {status.active ? (
          <span className="auto-on">● Automated orders ENABLED</span>
        ) : (
          <span className="auto-off">○ Automated orders disabled</span>
        )}
        <span className="muted auto-reason"> — {status.active_reason}</span>
      </div>

      {status.is_paper ? (
        <div className="auto-note auto-note-ok">
          Paper mode — orders are simulated. Min confidence to auto-execute:{" "}
          <strong>{status.min_confidence}%</strong>. Monitor every{" "}
          {Math.round(status.monitor_interval_sec / 60)} min.
        </div>
      ) : (
        <div className="auto-note auto-note-warn">
          ⚠️ LIVE mode — automated orders use real money. Requires
          AUTO_LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_THE_RISK on the server.
        </div>
      )}

      {!status.capability_allowed && (
        <div className="muted auto-reason">
          Auto mode requires server config: {status.capability_reason}
        </div>
      )}

      {error && <div className="inline-error">{error}</div>}
    </div>
  );
}
