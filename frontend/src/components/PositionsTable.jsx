export default function PositionsTable({ positions }) {
  return (
    <div className="card">
      <div className="card-header">
        <h2>Open Positions</h2>
      </div>
      {!positions || positions.length === 0 ? (
        <p className="muted">No open positions.</p>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Qty</th>
              <th>Avg Entry</th>
              <th>Current</th>
              <th>Mkt Value</th>
              <th>Unrealized P&amp;L</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((p) => (
              <tr key={p.symbol}>
                <td><strong>{p.symbol}</strong></td>
                <td>{p.qty}</td>
                <td>${p.avg_entry_price.toFixed(2)}</td>
                <td>${p.current_price.toFixed(2)}</td>
                <td>${p.market_value.toFixed(2)}</td>
                <td className={p.unrealized_pl >= 0 ? "pos" : "neg"}>
                  ${p.unrealized_pl.toFixed(2)} ({p.unrealized_plpc.toFixed(2)}%)
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
