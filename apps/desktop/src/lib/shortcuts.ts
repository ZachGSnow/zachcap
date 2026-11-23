import { useSyncExternalStore } from "react";

/** The global shortcuts. Their keys are picked in Settings and registered by the shell. */
export type ShortcutAction = "record" | "bookmark" | "snapshot" | "dictate";

export const SHORTCUT_ACTIONS: ShortcutAction[] = ["record", "bookmark", "snapshot", "dictate"];

export const DEFAULT_KEYS: Record<ShortcutAction, string> = {
  record: "Ctrl+Alt+R",
  bookmark: "Ctrl+Alt+B",
  snapshot: "Ctrl+Alt+S",
  dictate: "Ctrl+Alt+D",
};

/** How a shortcut is, as the shell found it: "on" (held now), "shared" (held now, and another
 * app has the same keys: dictation gets them first), "free" (not needed now, and no other app
 * has the keys), "off" (not needed now, not checked), "taken" (another app has the keys, so
 * they never reach ZachCap) or "invalid". */
export type ShortcutStatus = {
  action: ShortcutAction;
  keys: string;
  state: "on" | "shared" | "free" | "off" | "taken" | "invalid";
};

let current: Record<ShortcutAction, string> = DEFAULT_KEYS;
const listeners = new Set<() => void>();

/** The keys in use now (Settings changed them, or the settings were loaded). */
export function setKeys(keys: Record<ShortcutAction, string> | undefined) {
  if (!keys || SHORTCUT_ACTIONS.every((action) => keys[action] === current[action])) return;
  current = { ...DEFAULT_KEYS, ...keys };
  listeners.forEach((listener) => listener());
}

/** The keys for a shortcut now, for text outside React (a notification). */
export function keysFor(action: ShortcutAction): string {
  return current[action];
}

/** The keys for every shortcut, kept up to date. */
export function useKeys(): Record<ShortcutAction, string> {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => current,
  );
}

/** The keys as shown on a key cap: "Ctrl + Alt + R". */
export function showKeys(keys: string): string {
  return keys.split("+").join(" + ");
}

const MODIFIER_CODES = new Set([
  "ControlLeft",
  "ControlRight",
  "AltLeft",
  "AltRight",
  "ShiftLeft",
  "ShiftRight",
  "MetaLeft",
  "MetaRight",
  "OSLeft",
  "OSRight",
]);

/** The keys a key press makes, written the way Settings saves them ("Ctrl+Alt+P"); null
 * while only modifiers are down; or why they can't be a shortcut. The key is read by its
 * place on the keyboard, so the layout doesn't matter. */
export function keysFromEvent(event: KeyboardEvent): { keys: string } | { problem: string } | null {
  if (MODIFIER_CODES.has(event.code)) return null;
  const key = /^Key([A-Z])$/.exec(event.code)?.[1] ?? /^Digit([0-9])$/.exec(event.code)?.[1] ?? /^(F(?:[1-9]|1[0-9]|2[0-4]))$/.exec(event.code)?.[1];
  if (!key) return { problem: "Use a letter, a digit, or F1 to F24 with Ctrl, Alt or Win." };
  const modifiers = [event.ctrlKey && "Ctrl", event.altKey && "Alt", event.shiftKey && "Shift", event.metaKey && "Win"].filter(Boolean);
  const alone = /^F(1[3-9]|2[0-4])$/.test(key);
  if (!event.ctrlKey && !event.altKey && !event.metaKey && !alone) {
    return { problem: "Hold Ctrl, Alt or Win too: on its own it would be taken from every app." };
  }
  return { keys: [...modifiers, key].join("+") };
}
