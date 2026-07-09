import { useState } from "react";

export default function CollapsibleSection({
  title,
  count,
  defaultOpen = true,
  badgeClass = "",
  note,
  actions,
  children,
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="collapsible">
      <div className="collapsible-head">
        <button className="collapsible-toggle" onClick={() => setOpen((o) => !o)}>
          <span className="chevron">{open ? "▾" : "▸"}</span>
          <span className="collapsible-title">{title}</span>
          {count != null && <span className={`count-badge ${badgeClass}`}>{count}</span>}
        </button>
        {actions && <div className="collapsible-actions">{actions}</div>}
      </div>
      {open && (
        <div className="collapsible-body">
          {note && <p className="muted section-note">{note}</p>}
          {children}
        </div>
      )}
    </div>
  );
}
