// Helpers for the performance and operations views. Built on the shared `request`
// helper; the backend is only reached through api/client.js.
import { request } from "./client";

// FastAPI answers an unknown route with exactly "Not Found". A 404 from these
// endpoints means "this server does not have the feature yet", not a failure.
export const isNotAvailable = (e) => e?.message === "Not Found";

const enc = encodeURIComponent;

export const ops = {
  getStatus: () => request("/ops/status"),
  getAlerts: (status = "open") => request(`/ops/alerts?status=${enc(status)}`),
  ackAlert: (id) => request(`/ops/alerts/${enc(id)}/ack`, { method: "POST" }),
  getReviews: (period, limit = 30) =>
    request(`/reviews?limit=${limit}${period ? `&period=${enc(period)}` : ""}`),
  getReview: (id) => request(`/reviews/${enc(id)}`),
  decideReview: (id, decision) =>
    request(`/reviews/${enc(id)}/decision`, { method: "POST", body: JSON.stringify({ decision }) }),
  getStrategyMetrics: (limit = 500) => request(`/strategy-metrics?limit=${limit}`),
  getBacktestReports: () => request("/backtests/reports"),
  getBacktestReport: (name) => request(`/backtests/reports/${enc(name)}`),
  getLlmUsage: (days = 7) => request(`/llm/usage?days=${days}`),
};
