import { useCallback, useEffect, useState } from "react";
import { api } from "./api/client";
import Nav from "./components/Nav";
import DashboardPage from "./pages/DashboardPage";
import RecommendationsPage from "./pages/RecommendationsPage";
import MonitorPage from "./pages/MonitorPage";
import HistoryPage from "./pages/HistoryPage";
import OrdersPage from "./pages/OrdersPage";
import SettingsPage from "./pages/SettingsPage";

export default function App() {
  const [tab, setTab] = useState("dashboard");
  const [portfolio, setPortfolio] = useState(null);
  const [watchlist, setWatchlist] = useState([]);
  const [intents, setIntents] = useState([]);
  const [orders, setOrders] = useState([]);
  const [autoStatus, setAutoStatus] = useState(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [error, setError] = useState(null);
  const [universes, setUniverses] = useState([]);
  const [selectedUniverse, setSelectedUniverse] = useState("");
  const [lastRun, setLastRun] = useState(null);

  const loadAll = useCallback(async () => {
    const [p, w, i, o, a] = await Promise.all([
      api.getPortfolio().catch(() => null),
      api.getWatchlist().catch(() => []),
      api.getIntents().catch(() => []),
      api.getOrders().catch(() => []),
      api.getAutomationStatus().catch(() => null),
    ]);
    setPortfolio(p);
    setWatchlist(w);
    setIntents(i);
    setOrders(o);
    setAutoStatus(a);
  }, []);

  useEffect(() => {
    loadAll();
    api.getUniverses().then(setUniverses).catch(() => setUniverses([]));
    const t = setInterval(loadAll, 20000);
    return () => clearInterval(t);
  }, [loadAll]);

  async function runAnalysis() {
    setAnalyzing(true);
    setError(null);
    try {
      const summary = await api.runAnalysis({ universe: selectedUniverse || null });
      setLastRun(summary);
      await loadAll();
    } catch (e) {
      setError(e.message);
    } finally {
      setAnalyzing(false);
    }
  }

  const minConfidence = autoStatus?.active ? autoStatus.min_confidence : 0;

  const badges = {
    recommendations: intents.filter((i) => i.status === "pending_approval" && !i.auto_executed).length,
    history: intents.filter((i) => i.status === "rejected_by_risk").length,
  };

  const shared = {
    portfolio, watchlist, setWatchlist, intents, orders, autoStatus, minConfidence,
    lastRun, reload: loadAll,
  };

  return (
    <div className="app">
      <header className="app-header">
        <h1>Trading Assistant</h1>
        <div className="header-actions">
          <select
            className="filter-input"
            value={selectedUniverse}
            onChange={(e) => setSelectedUniverse(e.target.value)}
            title="Universe Mode — analyze a predefined theme instead of the watchlist"
          >
            <option value="">Watchlist</option>
            {universes.map((u) => (
              <option key={u.name} value={u.name}>
                {u.name} ({u.count})
              </option>
            ))}
          </select>
          <button className="btn btn-primary" onClick={runAnalysis} disabled={analyzing}>
            {analyzing ? "Analyzing…" : selectedUniverse ? `Analyze: ${selectedUniverse}` : "Run Claude Analysis"}
          </button>
        </div>
      </header>

      <Nav active={tab} onChange={setTab} badges={badges} />

      {error && <div className="banner banner-warn">Error: {error}</div>}

      {tab === "dashboard" && <DashboardPage {...shared} />}
      {tab === "recommendations" && <RecommendationsPage {...shared} />}
      {tab === "monitor" && <MonitorPage {...shared} />}
      {tab === "history" && <HistoryPage {...shared} />}
      {tab === "orders" && <OrdersPage {...shared} />}
      {tab === "settings" && <SettingsPage {...shared} />}

      <footer className="app-footer muted">
        Claude analyzes &amp; recommends. The risk engine validates. You approve (or, when
        autonomous trading is ON, eligible high-confidence trades execute automatically in
        paper mode). Claude never places trades directly.
      </footer>
    </div>
  );
}
