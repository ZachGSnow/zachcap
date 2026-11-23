import { useCallback, useRef } from "react";

/** For a dialog opened from state rather than from a DialogTrigger. Radix gives focus back
 * only to a trigger, so without this it falls to the page when the dialog closes and the
 * next Tab starts over in the sidebar. `remember(button)` when opening; pass
 * `onCloseAutoFocus` to the DialogContent. If that button is gone by then (its row was
 * deleted), `fallback` says where focus goes instead. */
export function useReturnFocus(fallback?: () => HTMLElement | null | undefined) {
  const opener = useRef<HTMLElement | null>(null);
  const fallbackRef = useRef(fallback);
  fallbackRef.current = fallback;

  const remember = useCallback((from?: Element | null) => {
    const element = from ?? document.activeElement;
    opener.current = element instanceof HTMLElement && element !== document.body ? element : null;
  }, []);

  const onCloseAutoFocus = useCallback((event: Event) => {
    const remembered = opener.current;
    opener.current = null;
    const target = remembered?.isConnected ? remembered : fallbackRef.current?.();
    if (target?.isConnected) {
      event.preventDefault();
      target.focus({ preventScroll: true });
    }
  }, []);

  return { remember, onCloseAutoFocus };
}
