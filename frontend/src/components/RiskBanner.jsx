import { useEffect, useState } from "react";
import { api } from "../api/client";

export default function RiskBanner({ onChange }) {
  const [state, setState] = useState(null);
  const [error, setError] = useState(null);

  async function refresh() {
    try {
      const s = await api.getRiskState();
      setState(s);
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

  async function toggleHalt() {
    if (state?.trading_halted) {
      if (!confirm("Resume trading? New approvals will be allowed again.")) return;
      await api.resumeTrading();
    } else {
      if (!confirm("EMERGENCY STOP: halt all trading approvals for the day?")) return;
      await api.haltTrading();
    }
    refresh();
  }

  if (error) return <div className="banner banner-warn">Risk state error: {error}</div>;
  if (!state) return <div className="banner">Loading risk state…</div>;

  const lossRatio = Math.min(
    1,
    Math.max(0, -state.day_pnl_pct / state.daily_loss_limit_pct)
  );
  const danger = state.trading_halted || lossRatio > 0.8;

  return (
    <div className={`banner ${danger ? "banner-danger" : "banner-ok"}`}>
      <div className="risk-info">
        <strong>{state.trading_halted ? "⛔ TRADING HALTED" : "✅ Trading active"}</strong>
        {state.halted_reason && <span className="muted"> — {state.halted_reason}</span>}
        <div className="risk-meter">
          <div className="risk-meter-fill" style={{ width: `${lossRatio * 100}%` }} />
        </div>
        <span className="muted">
          Day P&amp;L: {state.day_pnl_pct.toFixed(2)}% (limit −{state.daily_loss_limit_pct}%)
        </span>
      </div>
      <button className={`btn ${state.trading_halted ? "btn-resume" : "btn-stop"}`} onClick={toggleHalt}>
        {state.trading_halted ? "Resume Trading" : "🛑 STOP TRADING"}
      </button>
    </div>
  );
}
