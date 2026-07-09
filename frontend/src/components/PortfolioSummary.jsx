export default function PortfolioSummary({ portfolio }) {
  if (!portfolio) return <div className="card">Loading portfolio…</div>;
  const pnlClass = portfolio.day_pnl >= 0 ? "pos" : "neg";
  const fmt = (n) => `$${Number(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

  return (
    <div className="card">
      <div className="card-header">
        <h2>Portfolio</h2>
        <span className={`pill ${portfolio.is_paper ? "pill-paper" : "pill-live"}`}>
          {portfolio.is_paper ? "PAPER" : "LIVE"}
        </span>
      </div>
      <div className="metrics">
        <div className="metric">
          <span className="metric-label">Equity</span>
          <span className="metric-value">{fmt(portfolio.equity)}</span>
        </div>
        <div className="metric">
          <span className="metric-label">Cash</span>
          <span className="metric-value">{fmt(portfolio.cash)}</span>
        </div>
        <div className="metric">
          <span className="metric-label">Buying Power</span>
          <span className="metric-value">{fmt(portfolio.buying_power)}</span>
        </div>
        <div className="metric">
          <span className="metric-label">Day P&amp;L</span>
          <span className={`metric-value ${pnlClass}`}>
            {fmt(portfolio.day_pnl)} ({portfolio.day_pnl_pct.toFixed(2)}%)
          </span>
        </div>
        <div className="metric">
          <span className="metric-label">Open Positions</span>
          <span className="metric-value">
            {portfolio.open_positions} / {portfolio.max_open_positions ?? "—"}
          </span>
        </div>
      </div>
    </div>
  );
}
