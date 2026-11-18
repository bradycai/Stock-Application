const BASE = import.meta.env.VITE_API_URL || "http://localhost:8000";
const TOKEN = import.meta.env.VITE_API_TOKEN || "";

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${TOKEN}`,
      ...(options.headers || {}),
    },
  });
  if (!res.ok) {
    let detail;
    try {
      detail = (await res.json()).detail;
    } catch {
      detail = res.statusText;
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  if (res.status === 204) return null;
  return res.json();
}

export const api = {
  getPortfolio: () => request("/portfolio"),
  getWatchlist: () => request("/watchlist"),
  addTicker: (ticker) => request("/watchlist", { method: "POST", body: JSON.stringify({ ticker }) }),
  removeTicker: (ticker) => request(`/watchlist/${ticker}`, { method: "DELETE" }),
  runAnalysis: (opts = {}) =>
    request("/analysis/run", {
      method: "POST",
      body: JSON.stringify({ tickers: opts.tickers || null, universe: opts.universe || null }),
    }),
  getUniverses: () => request("/analysis/universes"),
  getIntents: (status) => request(`/intents${status ? `?status=${status}` : ""}`),
  approveIntent: (id) => request(`/intents/${id}/approve`, { method: "POST" }),
  rejectIntent: (id, reason) =>
    request(`/intents/${id}/reject`, { method: "POST", body: JSON.stringify({ reason }) }),
  deleteIntent: (id) => request(`/intents/${id}`, { method: "DELETE" }),
  clearRejectedIntents: () => request("/intents/clear-rejected", { method: "POST" }),
  clearWatchIntents: () => request("/intents/clear-watch", { method: "POST" }),
  getOrders: () => request("/orders"),
  getRiskState: () => request("/risk/state"),
  haltTrading: () => request("/risk/halt", { method: "POST" }),
  resumeTrading: () => request("/risk/resume", { method: "POST" }),
  getAutomationStatus: () => request("/automation/status"),
  setAutomationMode: (mode) =>
    request("/automation/mode", { method: "POST", body: JSON.stringify({ mode }) }),
  setKillSwitch: (engaged, reason) =>
    request("/automation/kill-switch", {
      method: "POST",
      body: JSON.stringify({ engaged, reason: reason || null }),
    }),
  getAutomationEvents: () => request("/automation/events"),
  clearAutomationEvents: () => request("/automation/events/clear", { method: "POST" }),
  getMonitorStatus: () => request("/monitor/status"),
  getMonitorEvents: () => request("/monitor/events"),
  getManagedPositions: () => request("/monitor/positions"),
  getResearchRuns: (limit = 20) => request(`/research/runs?limit=${limit}`),
  getRunQuality: (id) => request(`/research/runs/${id}/quality`),
  getSnapshot: (id) => request(`/analysis/snapshots/${id}`),
  getLlmUsage: (days = 7) => request(`/llm/usage?days=${days}`),
  getJournalTrades: (limit = 200) => request(`/journal/trades?limit=${limit}`),
  getJournalCandidates: () => request("/journal/candidates"),
  syncJournal: () => request("/journal/sync", { method: "POST" }),
  getLlmCalls: (limit = 50) => request(`/llm/calls?limit=${limit}`),
};
