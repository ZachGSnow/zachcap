/** A value changed in place, like a meeting's client or ticket: a button that becomes a box,
 * saved when the box closes. The rules both boxes follow, kept apart so they can be tested. */

/** How the box closes: Enter, Escape, or a click somewhere else. */
export type Closing = "Enter" | "Escape" | "blur";

/** The value shown: one being saved until the engine has answered, else the page's ("" being
 * saved shows as none). */
export function shownValue(value: string | null, pending: string | null): string | null {
  return pending !== null ? pending || null : value;
}

/** What closing the box does: Enter, or a click elsewhere, saves what was typed if it changed
 * (``save`` is null when there's nothing to save); Escape saves nothing. Focus goes back to
 * the button after a key only: after a click it stays where the click put it. */
export function closing(how: Closing, start: string, typed: string): { save: string | null; refocus: boolean } {
  const wanted = typed.trim();
  return { save: how !== "Escape" && wanted !== start ? wanted : null, refocus: how !== "blur" };
}

/** Saves a value, showing it until the save is over and the page has caught up (``show(null)``
 * then shows the page's again), so a value the engine refused doesn't stay on screen. */
export async function saveShowing(
  value: string,
  save: (value: string) => Promise<unknown>,
  show: (pending: string | null) => void,
): Promise<void> {
  show(value);
  try {
    await save(value);
  } finally {
    show(null);
  }
}
