/** Light, dark, or whatever Windows uses. Kept in this window's storage: it's a per-computer
 * preference, not part of the meeting library. */
export type Theme = "system" | "light" | "dark";

const KEY = "zachcap-theme";
const windowsDark = window.matchMedia("(prefers-color-scheme: dark)");

export function getTheme(): Theme {
  const saved = localStorage.getItem(KEY);
  return saved === "light" || saved === "dark" ? saved : "system";
}

const listeners = new Set<() => void>();

/** What the window actually shows now: the chosen theme, or Windows' while that's "system". */
export function resolvedTheme(): "light" | "dark" {
  const theme = getTheme();
  return theme === "dark" || (theme === "system" && windowsDark.matches) ? "dark" : "light";
}

/** Calls back whenever the window's theme changes (a new choice, or Windows switching). */
export function subscribeTheme(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function applyTheme() {
  document.documentElement.classList.toggle("dark", resolvedTheme() === "dark");
  listeners.forEach((listener) => listener());
}

export function setTheme(theme: Theme) {
  if (theme === "system") localStorage.removeItem(KEY);
  else localStorage.setItem(KEY, theme);
  applyTheme();
}

/** Applies the saved theme now and follows Windows while it's set to "system". */
export function followTheme() {
  applyTheme();
  windowsDark.addEventListener("change", applyTheme);
}
