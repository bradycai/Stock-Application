import RiskBanner from "../components/RiskBanner";
import PortfolioSummary from "../components/PortfolioSummary";
import AutomationPanel from "../components/AutomationPanel";
import Watchlist from "../components/Watchlist";
import PositionsTable from "../components/PositionsTable";
import IntentCard from "../components/IntentCard";
import RunSummary from "../components/RunSummary";

const TOP_N = 4;

export default function DashboardPage({
  portfolio,
  watchlist,
  setWatchlist,
  intents,
  lastRun,
  reload,
}) {
  // Top actionable recommendations only: pending approval, highest confidence first.
  const actionable = intents
    .filter((i) => i.status === "pending_approval" && !i.auto_executed)
    .sort((a, b) => (b.confidence || 0) - (a.confidence || 0))
    .slice(0, TOP_N);

  return (
    <>
      <RiskBanner onChange={() => {}} />
      <div className="grid">
        <div className="col">
          <PortfolioSummary portfolio={portfolio} />
          <PositionsTable positions={portfolio?.positions} />
          <Watchlist tickers={watchlist} onChange={setWatchlist} />
        </div>
        <div className="col">
          {lastRun && <RunSummary summary={lastRun} />}
          <AutomationPanel onChange={() => {}} />
          <div className="card">
            <div className="card-header">
              <h2>Top Actionable</h2>
              <span className="muted">{actionable.length} of top {TOP_N}</span>
            </div>
            {actionable.length === 0 ? (
              <p className="muted">
                No actionable recommendations. Run an analysis, then see all of them on the
                Recommendations tab.
              </p>
            ) : (
              actionable.map((i) => <IntentCard key={i.id} intent={i} onResolved={reload} />)
            )}
          </div>
        </div>
      </div>
    </>
  );
}
