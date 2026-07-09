// Pure client-side filtering for intent lists. Used by Recommendations + History pages.
export const EMPTY_FILTERS = { ticker: "", action: "", minConfidence: 0, since: "" };

export function filterIntents(intents, f = EMPTY_FILTERS) {
  return intents.filter((i) => {
    if (f.ticker && !i.ticker.toUpperCase().includes(f.ticker.toUpperCase())) return false;
    if (f.action && i.action !== f.action) return false;
    if (f.minConfidence && (i.confidence || 0) * 100 < Number(f.minConfidence)) return false;
    if (f.since) {
      const cutoff = new Date(f.since).getTime();
      if (new Date(i.created_at).getTime() < cutoff) return false;
    }
    return true;
  });
}
