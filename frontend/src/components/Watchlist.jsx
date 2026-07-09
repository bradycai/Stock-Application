import { useState } from "react";
import { api } from "../api/client";

export default function Watchlist({ tickers, onChange }) {
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);

  async function add(e) {
    e.preventDefault();
    if (!input.trim()) return;
    setBusy(true);
    try {
      const updated = await api.addTicker(input.trim().toUpperCase());
      onChange(updated);
      setInput("");
    } finally {
      setBusy(false);
    }
  }

  async function remove(t) {
    setBusy(true);
    try {
      const updated = await api.removeTicker(t);
      onChange(updated);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <div className="card-header">
        <h2>Watchlist</h2>
      </div>
      <form className="watchlist-form" onSubmit={add}>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Add ticker (e.g. AAPL)"
        />
        <button className="btn" disabled={busy} type="submit">Add</button>
      </form>
      <div className="chips">
        {(tickers || []).map((t) => (
          <span className="chip" key={t}>
            {t}
            <button className="chip-x" onClick={() => remove(t)} disabled={busy}>×</button>
          </span>
        ))}
        {(!tickers || tickers.length === 0) && <span className="muted">No tickers yet.</span>}
      </div>
    </div>
  );
}
