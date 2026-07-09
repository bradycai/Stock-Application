export default function OrdersTable({ orders }) {
  return (
    <div className="card">
      <div className="card-header">
        <h2>Orders</h2>
      </div>
      {!orders || orders.length === 0 ? (
        <p className="muted">No orders submitted yet.</p>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Side</th>
              <th>Qty</th>
              <th>Limit</th>
              <th>Stop</th>
              <th>Target</th>
              <th>Status</th>
              <th>Exit</th>
              <th>Mode</th>
            </tr>
          </thead>
          <tbody>
            {orders.map((o) => (
              <tr key={o.id}>
                <td><strong>{o.symbol}</strong></td>
                <td>{o.side}</td>
                <td>{o.qty}</td>
                <td>${o.limit_price?.toFixed(2)}</td>
                <td>{o.stop_loss_price ? `$${o.stop_loss_price.toFixed(2)}` : "—"}</td>
                <td>{o.take_profit_price ? `$${o.take_profit_price.toFixed(2)}` : "—"}</td>
                <td><span className="status-pill">{o.status}</span></td>
                <td>{o.exit_reason ? o.exit_reason.replace(/_/g, " ") : (o.monitoring_status || "—")}</td>
                <td>{o.is_live ? <span className="pill pill-live">LIVE</span> : <span className="pill pill-paper">PAPER</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
