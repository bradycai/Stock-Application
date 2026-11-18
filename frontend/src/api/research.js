import { request } from "./client";

export const researchApi = {
  listProposals: (status) =>
    request(`/proposals?limit=100${status ? `&status=${encodeURIComponent(status)}` : ""}`),
  getProposal: (id) => request(`/proposals/${id}`),
  getBundle: (id) => request(`/research/bundles/${id}`),
  approveIntent: (id) => request(`/intents/${id}/approve`, { method: "POST" }),
  rejectIntent: (id, reason) =>
    request(`/intents/${id}/reject`, { method: "POST", body: JSON.stringify({ reason }) }),
  getWatcherStatus: () => request("/watcher/status"),
  getWatcherDecisions: (limit = 50) => request(`/watcher/decisions?limit=${limit}`),
  getWatcherJobs: (limit = 50) => request(`/watcher/jobs?limit=${limit}`),
};
