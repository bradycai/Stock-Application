import { useEffect, useState } from "react";
import { api } from "../api/client";

const LABELS = {
  auto_buy_executed: { text: "Auto BUY", cls: "ev-buy" },
  auto_sell_executed: { text: "Auto SELL", cls: "ev-sell" },
  auto_trade_rejected: { text: "Auto rejected", cls: "ev-reject" },
  stop_loss_triggered: { text: "Stop loss", cls: "ev-stop" },
  take_profit_triggered: { text: "Take profit", cls: "ev-take" },
  daily_loss_halt: { text: "Daily-loss halt", cls: "ev-halt" },
  automation_enabled: { text: "Automation ON", cls: "ev-info" },
  automation_disabled: { text: "Automation OFF", cls: "ev-info" },
};

function summarize(ev) {
  const p = ev.payload || {};
  if (p.reason && typeof p.reason === "string") return p.reason;
  if (p.symbol && p.reason) return `${p.symbol}: ${p.reason}`;
  if (p.symbol) return p.symbol;
  if (p.closed) return `closed: ${(p.closed || []).join(", ") || "none"}`;
  if (p.computed_qty != null) return `qty ${p.computed_qty}`;
  return "";
}

export default function AutoEventsLog({ clearable = false }) {
  const [events, setEvents] = useState([]);

  async function refresh() {
    try {
      setEvents(await api.getAutomationEvents());
    } catch {
      /* ignore */
    }
  }

  async function clearAll() {
    if (!confirm("Clear the automation activity feed? (Audit log is preserved server-side.)")) return;
    await api.clearAutomationEvents();
    refresh();
  }

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 15000);
    return () => clearInterval(t);
  }, []);

  return (
    <div className="card">
      <div className="card-header">
        <h2>Automation Activity</h2>
        {clearable && events.length > 0 && (
          <button className="btn btn-small" onClick={clearAll}>Clear</button>
        )}
      </div>
      {events.length === 0 ? (
        <p className="muted">No autonomous events yet.</p>
      ) : (
        <ul className="event-list">
          {events.map((ev) => {
            const meta = LABELS[ev.event_type] || { text: ev.event_type, cls: "ev-info" };
            return (
              <li key={ev.id} className="event-item">
                <span className={`event-tag ${meta.cls}`}>{meta.text}</span>
                <span className="event-summary">{summarize(ev)}</span>
                <span className="event-time muted">
                  {new Date(ev.ts).toLocaleTimeString()}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
