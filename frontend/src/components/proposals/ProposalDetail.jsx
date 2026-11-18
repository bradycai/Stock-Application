import { useCallback, useEffect, useState } from "react";
import { researchApi } from "../../api/research";
import ApprovalPanel from "./ApprovalPanel";
import {
  SafeLink, StrList, fmtPct, fmtPrice, fmtTime, safeUrl, textOf, violationParts,
} from "./format";

function Violations({ items, title }) {
  if (!Array.isArray(items) || items.length === 0) return null;
  return (
    <div>
      <h3 className="pe-h3">{title}</h3>
      <ul className="pe-list">
        {items.map((v, i) => {
          const { code, message } = violationParts(v);
          return (
            <li key={i}>
              <code className="pe-code">{code}</code> {message}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function EvidenceList({ ids, evidence, bundleError }) {
  if (!Array.isArray(ids) || ids.length === 0) return <span className="muted">none cited</span>;
  return (
    <ul className="pe-list">
      {ids.map((id) => {
        const item = evidence ? evidence[id] : null;
        const link = item && (safeUrl(item.source) || safeUrl(item.value?.url));
        return (
          <li key={id}>
            <code className="pe-code">{id}</code>{" "}
            {item ? (
              <>
                <span className="muted">[{item.kind}]</span> {textOf(item.value)}{" "}
                <span className="muted">
                  (source: {textOf(item.source)}{item.as_of ? `, as of ${fmtTime(item.as_of)}` : ""})
                </span>
                {link && <> <SafeLink href={link}>open source</SafeLink></>}
              </>
            ) : (
              <span className="muted">{bundleError ? "bundle unavailable" : "not found in bundle"}</span>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function AnalystReport({ row, evidence }) {
  const r = row.report;
  return (
    <div className="pe-report">
      <div className="pe-report-head">
        <strong>{row.analyst}</strong>
        {r && <span className={`pe-stance pe-stance-${r.stance}`}>{r.stance}</span>}
        {r && <span className="muted">confidence {fmtPct(r.confidence)}</span>}
        <span className="muted">status: {row.status}</span>
      </div>
      {row.error && <div className="banner banner-warn">Analyst error: {row.error}</div>}
      {r ? (
        <>
          <p>{textOf(r.summary)}</p>
          <div className="pe-cols">
            <div><h4 className="pe-h4">Key points</h4><StrList items={r.key_points} /></div>
            <div><h4 className="pe-h4">Risks</h4><StrList items={r.risks} /></div>
            <div><h4 className="pe-h4">Data gaps</h4><StrList items={r.data_gaps} /></div>
          </div>
          {r.invalidation && <p className="muted">Invalidation: {textOf(r.invalidation)}</p>}
          {Array.isArray(r.evidence_ids) && r.evidence_ids.length > 0 && (
            <details>
              <summary className="muted">Evidence cited ({r.evidence_ids.length})</summary>
              <EvidenceList ids={r.evidence_ids} evidence={evidence} />
            </details>
          )}
        </>
      ) : (
        !row.error && <p className="muted">No report body.</p>
      )}
    </div>
  );
}

export default function ProposalDetail({ id, onChanged }) {
  const [data, setData] = useState(null);
  const [bundle, setBundle] = useState(null);
  const [bundleError, setBundleError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(async (isCurrent = () => true) => {
    setError(null);
    setBundleError(null);
    try {
      const d = await researchApi.getProposal(id);
      if (!isCurrent()) return;
      setData(d);
      try {
        const b = await researchApi.getBundle(d.bundle_id);
        if (isCurrent()) setBundle(b);
      } catch (e) {
        if (isCurrent()) {
          setBundle(null);
          setBundleError(e.message);
        }
      }
    } catch (e) {
      if (isCurrent()) setError(e.message);
    } finally {
      if (isCurrent()) setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    let live = true;
    setData(null);
    setBundle(null);
    setLoading(true);
    load(() => live);
    return () => {
      live = false;
    };
  }, [load]);

  async function afterDecision() {
    await load();
    if (onChanged) onChanged();
  }

  if (loading) return <div className="card"><p className="muted">Loading proposal #{id}…</p></div>;
  if (error) return <div className="card"><div className="banner banner-warn">Error: {error}</div></div>;
  if (!data) return null;

  const p = data.proposal || {};
  const v = data.validation || {};
  const intent = data.intent;
  const evidence = bundle?.evidence || null;
  const abstain = p.abstain_reason;

  return (
    <div className="card pe-detail">
      <div className="card-header">
        <h2>
          #{data.id} {data.symbol}: {p.action || data.action}{" "}
          <span className={`pe-status pe-status-${data.status}`}>{data.status}</span>
        </h2>
      </div>
      <p className="muted">
        Created {fmtTime(data.created_at)} · model {textOf(data.model)} · strategy {textOf(data.strategy_id)} v
        {textOf(data.strategy_version)} · bundle #{data.bundle_id} · expires {fmtTime(data.expires_at)}
      </p>

      {abstain && (
        <div className="banner banner-warn"><strong>Abstain reason:</strong> {textOf(abstain)}</div>
      )}

      <h3 className="pe-h3">Recommendation</h3>
      <div className="pe-grid">
        <div><span className="muted">Action</span><br />{p.action || "—"}</div>
        <div><span className="muted">Confidence</span><br />{fmtPct(p.confidence)}</div>
        <div><span className="muted">Entry range</span><br />{fmtPrice(p.entry_low)} – {fmtPrice(p.entry_high)}</div>
        <div><span className="muted">Invalidation</span><br />{fmtPrice(p.invalidation_price)}</div>
        <div><span className="muted">Take profit</span><br />{fmtPrice(p.take_profit_price)}</div>
        <div>
          <span className="muted">Size</span><br />
          {p.position_size_percent != null ? `${p.position_size_percent}%` : "—"}
          {v.size_clamped && <span className="muted"> (clamped to {v.position_size_percent}%)</span>}
        </div>
        <div>
          <span className="muted">Horizon</span><br />
          {p.holding_horizon_days != null ? `${p.holding_horizon_days} days` : "—"}
        </div>
      </div>
      <h4 className="pe-h4">Thesis</h4>
      <p>{textOf(p.thesis)}</p>
      <h4 className="pe-h4">Exit conditions</h4>
      <StrList items={p.exit_conditions} />

      <h3 className="pe-h3">Evidence cited</h3>
      {bundleError && (
        <div className="banner banner-warn">Could not load bundle #{data.bundle_id}: {bundleError}</div>
      )}
      <EvidenceList ids={p.evidence_ids} evidence={evidence} bundleError={bundleError} />

      <div className="pe-cols">
        <div><h3 className="pe-h3">Conflicting evidence</h3><StrList items={p.conflicting_evidence} /></div>
        <div><h3 className="pe-h3">Risks</h3><StrList items={p.risks} /></div>
        <div><h3 className="pe-h3">Limitations</h3><StrList items={p.limitations} /></div>
      </div>

      <h3 className="pe-h3">Analyst reports ({data.reports?.length || 0})</h3>
      {!data.reports?.length && <p className="muted">No analyst reports stored for this bundle.</p>}
      {(data.reports || []).map((row) => (
        <AnalystReport key={row.id} row={row} evidence={evidence} />
      ))}

      <h3 className="pe-h3">Validation</h3>
      <p className="muted">
        Validation status: {textOf(v.status)} · reports used: {textOf(v.reports_used)} · quote{" "}
        {fmtPrice(v.quote_price)} at {fmtTime(v.quote_ts)}
      </p>
      {!v.violations?.length && !v.risk_violations?.length && !intent?.risk_violations?.length && (
        <p className="muted">No validation or risk violations recorded.</p>
      )}
      <Violations items={v.violations} title="Validation violations" />
      <Violations items={v.risk_violations || intent?.risk_violations} title="Risk violations" />

      <h3 className="pe-h3">Linked intent</h3>
      {!intent ? (
        <p className="muted">No intent was created from this proposal.</p>
      ) : (
        <>
          <p>
            Intent #{intent.id}: {intent.action} {intent.ticker}, limit {fmtPrice(intent.limit_price)},{" "}
            {intent.position_size_percent}% of equity · status{" "}
            <strong>{intent.status}</strong> · risk {intent.risk_passed ? "passed" : "not passed"} · expires{" "}
            {fmtTime(intent.expires_at)}
          </p>
          {intent.status === "pending_approval" && (
            <ApprovalPanel key={intent.id} intent={intent} onDone={afterDecision} />
          )}
        </>
      )}
    </div>
  );
}
