import { useCallback, useEffect, useRef, useState } from "react";
import { isNotAvailable } from "../../api/ops";

// Load `fn` now and every `intervalMs`; paused while the tab is hidden; cleaned up on unmount.
// Returns { data, error, loading, unavailable, reload }. Previous data is kept on a failed refresh.
export function usePolling(fn, intervalMs = 30000) {
  const [state, setState] = useState({ data: null, error: null, loading: true, unavailable: false });
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const alive = useRef(true);

  const reload = useCallback(async () => {
    try {
      const data = await fnRef.current();
      if (alive.current) setState({ data, error: null, loading: false, unavailable: false });
    } catch (e) {
      if (!alive.current) return;
      if (isNotAvailable(e)) setState({ data: null, error: null, loading: false, unavailable: true });
      else setState((s) => ({ ...s, error: e.message, loading: false, unavailable: false }));
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    let timer = null;
    const start = () => {
      if (timer === null && intervalMs) timer = setInterval(reload, intervalMs);
    };
    const stop = () => {
      if (timer !== null) {
        clearInterval(timer);
        timer = null;
      }
    };
    const onVis = () => {
      if (document.hidden) stop();
      else {
        reload();
        start();
      }
    };
    reload();
    if (!document.hidden) start();
    document.addEventListener("visibilitychange", onVis);
    return () => {
      alive.current = false;
      stop();
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [reload, intervalMs]);

  return { ...state, reload };
}
