import AutomationPanel from "../components/AutomationPanel";
import { ops } from "../api/ops";
import { usePolling } from "../components/ops/usePolling";
import HealthSection from "../components/ops/HealthSection";
import AlertsSection from "../components/ops/AlertsSection";
import ErrorsSection from "../components/ops/ErrorsSection";
import SpendHistorySection from "../components/ops/SpendHistorySection";

export default function SettingsPage({ autoStatus, portfolio }) {
  // One poll (30s, paused while hidden) feeds both the health and recent-errors sections.
  const status = usePolling(() => ops.getStatus(), 30000);

  const rows = autoStatus
    ? [
        ["Trading mode", autoStatus.is_paper ? "PAPER" : "LIVE"],
        ["Automation mode", autoStatus.mode],
        ["Kill switch", autoStatus.kill_switch ? `ENGAGED — ${autoStatus.kill_switch_reason}` : "Off"],
        ["Automation capability (server)", autoStatus.capability_allowed ? "Allowed" : "Blocked"],
        ["Capability reason", autoStatus.capability_reason],
        ["Automated orders enabled", autoStatus.active ? "Yes" : "No"],
        ["Min confidence to auto-execute", `${autoStatus.min_confidence}%`],
        ["Monitor interval", `${Math.round(autoStatus.monitor_interval_sec / 60)} min`],
      ]
    : [];

  return (
    <div className="page">
      <HealthSection status={status} />
      <AlertsSection />

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

      <ErrorsSection status={status} />
      <SpendHistorySection />
    </div>
  );
}
