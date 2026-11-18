import { useCallback, useEffect, useRef, useState } from "react";
import { researchApi } from "../api/research";
import WatcherHealth from "../components/events/WatcherHealth";
import DecisionFeed from "../components/events/DecisionFeed";
import JobQueue from "../components/events/JobQueue";

const POLL_MS = 15000;

function Section({ title, state, emptyText, children }) {
  return (
    <div className="card">
      <div className="card-header"><h2>{title}</h2></div>
      {state.loading && <p className="muted">Loading…</p>}
      {state.error && <div className="banner banner-warn">Error: {state.error}</div>}
      {!state.loading && !state.error && state.empty && <p className="muted">{emptyText}</p>}
      {state.data != null && !state.empty && children /* stale data stays visible under an error */}
    </div>
  );
}

export default function EventsPage() {
  const [status, setStatus] = useState({ data: null, error: null, loading: true });
  const [decisions, setDecisions] = useState({ data: null, error: null, loading: true });
  const [jobs, setJobs] = useState({ data: null, error: null, loading: true });
  const [updated, setUpdated] = useState(null);
  const [paused, setPaused] = useState(false);
  const mounted = useRef(true);

  const load = useCallback(async () => {
    const run = (fn, set) =>
      fn().then(
        (data) => mounted.current && set({ data, error: null, loading: false }),
        // keep stale data visible if a refresh fails
        (e) => mounted.current && set((prev) => ({ ...prev, error: e.message, loading: false })),
      );
    await Promise.all([
      run(researchApi.getWatcherStatus, setStatus),
      run(() => researchApi.getWatcherDecisions(50), setDecisions),
      run(() => researchApi.getWatcherJobs(50), setJobs),
    ]);
    if (mounted.current) setUpdated(new Date());
  }, []);

  useEffect(() => {
    mounted.current = true;
    let timer = null;
    const start = () => {
      if (timer == null) timer = setInterval(load, POLL_MS);
    };
    const stop = () => {
      if (timer != null) {
        clearInterval(timer);
        timer = null;
      }
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        setPaused(false);
        load();
        start();
      } else {
        setPaused(true);
        stop();
      }
    };
    load();
    if (document.visibilityState === "visible") start();
    else setPaused(true);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      mounted.current = false;
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [load]);

  return (
    <div className="page-stack">
      <div className="card">
        <div className="card-header">
          <h2>Market watcher</h2>
          <div className="pe-row">
            <span className="muted">
              {paused ? "Auto-refresh paused (tab hidden)" : "Auto-refresh every 15s"}
              {updated ? ` · updated ${updated.toLocaleTimeString()}` : ""}
            </span>
            <button className="btn btn-small" onClick={load}>Refresh now</button>
          </div>
        </div>
        {status.loading && <p className="muted">Loading watcher status…</p>}
        {status.error && <div className="banner banner-warn">Error: {status.error}</div>}
        {status.data && <WatcherHealth status={status.data} />}
      </div>

      <Section
        title="Event feed"
        state={{ ...decisions, empty: decisions.data?.length === 0 }}
        emptyText="No watcher decisions yet."
      >
        {decisions.data && <DecisionFeed decisions={decisions.data} />}
      </Section>

      <Section
        title="Job queue"
        state={{ ...jobs, empty: jobs.data?.length === 0 }}
        emptyText="The research job queue is empty."
      >
        {jobs.data && <JobQueue jobs={jobs.data} />}
      </Section>
    </div>
  );
}
