export default function RunSummary({ summary }) {
  if (!summary) return null;
  const items = [
    ["Tickers screened", summary.tickers_screened],
    ["Sent to Claude (top)", summary.prescreen_top_n],
    ["Batches", summary.batches],
    ["Failed batches", summary.failed_batches],
    ["Final recommendations", summary.final_recommendations],
  ];
  return (
    <div className="card run-summary">
      <div className="card-header">
        <h2>Last Analysis Run</h2>
        {summary.universe && <span className="pill pill-paper">{summary.universe}</span>}
      </div>
      <div className="run-summary-grid">
        {items.map(([label, value]) => (
          <div className="run-stat" key={label}>
            <span className={`run-stat-value ${label === "Failed batches" && value > 0 ? "neg" : ""}`}>
              {value}
            </span>
            <span className="run-stat-label">{label}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
