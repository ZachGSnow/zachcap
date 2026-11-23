import { useCallback, useEffect, useRef, useState } from "react";

/** Loads data now and every `intervalMs` (0 = once). `refresh()` reloads on demand. */
export function usePoll<T>(load: () => Promise<T>, intervalMs: number, key: unknown = null) {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<Error>();
  const loadRef = useRef(load);
  loadRef.current = load;

  const refresh = useCallback(async () => {
    try {
      setData(await loadRef.current());
      setError(undefined);
    } catch (e) {
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
