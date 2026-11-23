/** Light, dark, or whatever Windows uses. Kept in this window's storage: it's a per-computer
 * preference, not part of the meeting library. */
export type Theme = "system" | "light" | "dark";

const KEY = "zachcap-theme";
const windowsDark = window.matchMedia("(prefers-color-scheme: dark)");

export function getTheme(): Theme {
  const saved = localStorage.getItem(KEY);
  return saved === "light" || saved === "dark" ? saved : "system";
}

export function applyTheme() {
  const theme = getTheme();
  document.documentElement.classList.toggle("dark", theme === "dark" || (theme === "system" && windowsDark.matches));
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
