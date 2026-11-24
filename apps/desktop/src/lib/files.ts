import { isTauri } from "@/lib/engine";

/** The text files ZachCap reads (the engine refuses others with the reason). */
export const TEXT_FILES = ["txt", "log", "md", "csv", "tsv", "json", "xml", "ini", "cfg", "conf", "config", "yaml", "yml", "toml", "ps1", "psm1", "bat", "cmd", "sh", "py", "reg", "html", "htm", "eml", "err", "out"];

/** A text file the user picks, as its path; null when they cancel. */
export async function pickTextFile(title: string): Promise<string | null> {
  if (!isTauri()) return window.prompt(`${title} (full path)`); // browser development
  const { open } = await import("@tauri-apps/plugin-dialog");
  const picked = await open({ title, filters: [{ name: "Text files", extensions: TEXT_FILES }] });
  return typeof picked === "string" ? picked : null;
}
