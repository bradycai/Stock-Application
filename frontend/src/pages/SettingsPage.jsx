import AutomationPanel from "../components/AutomationPanel";

export default function SettingsPage({ autoStatus, portfolio }) {
  const rows = autoStatus
    ? [
        ["Trading mode", autoStatus.is_paper ? "PAPER" : "LIVE"],
        ["Automation capability", autoStatus.capability_allowed ? "Allowed" : "Blocked"],
        ["Capability reason", autoStatus.capability_reason],
        ["Auto-trading active", autoStatus.active ? "Yes" : "No"],
        ["Min confidence to auto-execute", `${autoStatus.min_confidence}%`],
        ["Monitor interval", `${Math.round(autoStatus.monitor_interval_sec / 60)} min`],
      ]
    : [];

  return (
    <div className="page">
      <AutomationPanel onChange={() => {}} />

      <div className="card">
        <div className="card-header">
          <h2>Configuration</h2>
          {portfolio && (
            <span className={`pill ${portfolio.is_paper ? "pill-paper" : "pill-live"}`}>
              {portfolio.is_paper ? "PAPER" : "LIVE"}
            </span>
          )}
        </div>
        <table className="table">
          <tbody>
            {rows.map(([k, v]) => (
              <tr key={k}>
                <td className="muted">{k}</td>
                <td>{v}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="muted section-note">
          Risk limits (max position %, max open positions, daily-loss limit, confidence
          threshold) and the live-trading confirmation are configured server-side in
          <code> backend/.env</code> and are not editable from the UI by design.
        </p>
      </div>
    </div>
  );
}
