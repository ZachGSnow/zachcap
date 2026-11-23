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

/** How a shortcut is, as the shell found it: "on" (held now), "shared" (dictate: another app
 * has registered the same keys, and dictation gets them first while it's on), "free" (not
 * needed now, and no other app has registered them), "off" (not needed now, not checked),
 * "taken" (another app has registered them, so they never reach ZachCap), "failed" (Windows
 * refused the keyboard hook) or "invalid". */
export type ShortcutStatus = {
  action: ShortcutAction;
  keys: string;
  state: "on" | "shared" | "free" | "off" | "taken" | "failed" | "invalid";
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
 * while only modifiers are down; or why they can't be a shortcut. The key is read the way
 * Windows matches it, by its virtual-key code, which follows the keyboard layout: on a French
 * keyboard the key marked A is "A". */
export function keysFromEvent(event: KeyboardEvent): { keys: string } | { problem: string } | null {
  const code = event.keyCode;
  if (MODIFIER_CODES.has(event.code) || [16, 17, 18, 91, 92].includes(code)) return null;
  const key =
    (code >= 65 && code <= 90) || (code >= 48 && code <= 57)
      ? String.fromCharCode(code)
      : code >= 112 && code <= 135
        ? `F${code - 111}`
        : null;
  if (!key) return { problem: "Use a letter, a digit, or F1 to F24, with two of Ctrl, Alt and Win." };
  const alone = /^F(1[3-9]|2[0-4])$/.test(key); // keys no app uses (macro keys send them)
  const strong = [event.ctrlKey, event.altKey, event.metaKey].filter(Boolean).length;
  if (!strong && !alone) {
    return { problem: "Hold Ctrl, Alt or Win too: on its own it would be taken from every app." };
  }
  if (strong === 1 && !event.metaKey && !alone) {
    return { problem: "Hold two of Ctrl, Alt and Win: with one it would take a shortcut apps use (Ctrl+R reloads)." };
  }
  // AltGr: on many keyboards Ctrl+Alt and a key types a character (the euro sign, @).
  // Taking those keys would stop that character being typed anywhere.
  if (event.ctrlKey && event.altKey && !event.shiftKey && event.key.length === 1 && event.key.toUpperCase() !== key) {
    return { problem: `On this keyboard Ctrl+Alt+${key} types ${event.key}: pick another key.` };
  }
  const modifiers = [event.ctrlKey && "Ctrl", event.altKey && "Alt", event.shiftKey && "Shift", event.metaKey && "Win"];
  return { keys: [...modifiers.filter(Boolean), key].join("+") };
}
