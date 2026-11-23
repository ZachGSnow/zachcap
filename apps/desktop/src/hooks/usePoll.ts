import { useCallback, useEffect, useRef, useState } from "react";

/** Loads data now and every `intervalMs` (0 = once). `refresh()` reloads on demand.
 * An answer that arrives after the answer to a newer request is dropped, so a slow
 * response can never overwrite fresher data. */
export function usePoll<T>(load: () => Promise<T>, intervalMs: number, key: unknown = null) {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<Error>();
  const loadRef = useRef(load);
  loadRef.current = load;
  const issued = useRef(0);
  const applied = useRef(0);

  const refresh = useCallback(async () => {
    const request = ++issued.current;
    try {
      const next = await loadRef.current();
      if (request < applied.current) return;
      applied.current = request;
      setData(next);
      setError(undefined);
    } catch (e) {
      if (request < applied.current) return;
      applied.current = request;
      setError(e instanceof Error ? e : new Error(String(e)));
    }
  }, []);

  useEffect(() => {
    void refresh();
    if (!intervalMs) return;
    const id = window.setInterval(() => void refresh(), intervalMs);
    return () => window.clearInterval(id);
  }, [refresh, intervalMs, key]);

  return { data, error, refresh };
}
