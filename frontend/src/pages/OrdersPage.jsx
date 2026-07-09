import OrdersTable from "../components/OrdersTable";

export default function OrdersPage({ orders }) {
  return (
    <div className="page">
      <OrdersTable orders={orders} />
    </div>
  );
}
