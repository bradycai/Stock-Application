// Shared helpers. All model/news text is rendered as plain React text nodes (auto-escaped).

export const fmtTime = (t) => (t ? new Date(t).toLocaleString() : "—");
export const fmtPct = (c) => (c == null ? "—" : `${Math.round(Number(c) * 100)}%`);
export const fmtPrice = (v) => (v == null || Number.isNaN(Number(v)) ? "—" : `$${Number(v).toFixed(2)}`);

/** Returns a normalized http(s) URL string, or null for anything else (javascript:, data:, ...). */
export function safeUrl(s) {
  if (typeof s !== "string") return null;
  try {
    const u = new URL(s.trim());
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
  } catch {
    return null;
  }
}

export function SafeLink({ href, children }) {
  const url = safeUrl(href);
  if (!url) return <>{children ?? href}</>;
  return (
    <a href={url} target="_blank" rel="noopener noreferrer">
      {children ?? url}
    </a>
  );
}

export function textOf(v) {
  if (v == null) return "—";
  if (typeof v === "string") return v;
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

/** Violations may be {code,message} objects or bare strings. */
export function violationParts(v) {
  if (v && typeof v === "object") return { code: v.code || "UNKNOWN", message: v.message || "" };
  return { code: String(v), message: "" };
}

export function StrList({ items, empty = "none" }) {
  if (!Array.isArray(items) || items.length === 0) return <span className="muted">{empty}</span>;
  return (
    <ul className="pe-list">
      {items.map((x, i) => (
        <li key={i}>{textOf(x)}</li>
      ))}
    </ul>
  );
}
