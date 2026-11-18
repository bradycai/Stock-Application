import { useState } from "react";
import { researchApi } from "../../api/research";

// Only rendered when the linked intent is pending_approval. Two-step: click, then confirm.
export default function ApprovalPanel({ intent, onDone }) {
  const [confirm, setConfirm] = useState(null); // "approve" | "reject" | null
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function submit() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      if (confirm === "approve") await researchApi.approveIntent(intent.id);
      else await researchApi.rejectIntent(intent.id, reason.trim() || "Rejected from proposals page");
      setConfirm(null);
      await onDone();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="pe-approval">
      <div>
        Intent #{intent.id} is <strong>pending approval</strong>: {intent.action} {intent.ticker}, limit $
        {Number(intent.limit_price).toFixed(2)}, {Number(intent.position_size_percent).toFixed(2)}% of equity.
      </div>
      {error && <div className="banner banner-warn">Error: {error}</div>}
      {!confirm && (
        <div className="pe-row">
          <button className="btn btn-approve" disabled={busy} onClick={() => setConfirm("approve")}>
            Approve…
          </button>
          <button className="btn btn-reject" disabled={busy} onClick={() => setConfirm("reject")}>
            Reject…
          </button>
        </div>
      )}
      {confirm === "approve" && (
        <div className="banner banner-danger">
          <p>
            Approving re-checks the risk engine against fresh account state and then{" "}
            <strong>submits a paper order</strong> to the broker. This cannot be undone from here.
          </p>
          <div className="pe-row">
            <button className="btn btn-approve" disabled={busy} onClick={submit}>
              {busy ? "Submitting…" : "Confirm: submit paper order"}
            </button>
            <button className="btn" disabled={busy} onClick={() => setConfirm(null)}>Cancel</button>
          </div>
        </div>
      )}
      {confirm === "reject" && (
        <div className="banner banner-warn">
          <p>Reject this intent? No order will be placed.</p>
          <input
            className="filter-input"
            placeholder="Reason (optional)"
            value={reason}
            maxLength={200}
            onChange={(e) => setReason(e.target.value)}
          />
          <div className="pe-row">
            <button className="btn btn-reject" disabled={busy} onClick={submit}>
              {busy ? "Rejecting…" : "Confirm reject"}
            </button>
            <button className="btn" disabled={busy} onClick={() => setConfirm(null)}>Cancel</button>
          </div>
        </div>
      )}
    </div>
  );
}
