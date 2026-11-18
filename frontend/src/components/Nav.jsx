const TABS = [
  { key: "dashboard", label: "Dashboard" },
  { key: "recommendations", label: "Recommendations" },
  { key: "monitor", label: "Monitor" },
  { key: "history", label: "Rejected / History" },
  { key: "research", label: "Research" },
  { key: "proposals", label: "Proposals" },
  { key: "events", label: "Events" },
  { key: "journal", label: "Journal" },
  { key: "usage", label: "Usage" },
  { key: "orders", label: "Orders" },
  { key: "settings", label: "Settings" },
];

export default function Nav({ active, onChange, badges = {} }) {
  return (
    <nav className="nav">
      {TABS.map((t) => (
        <button
          key={t.key}
          className={`nav-tab ${active === t.key ? "nav-tab-active" : ""}`}
          onClick={() => onChange(t.key)}
        >
          {t.label}
          {badges[t.key] > 0 && <span className="nav-badge">{badges[t.key]}</span>}
        </button>
      ))}
    </nav>
  );
}
