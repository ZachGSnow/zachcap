import type { Settings, Writing } from "@/lib/engine";

/** Who writes summaries and documents: Claude, or the model on this PC. */
export type Writer = "claude" | "local";

const LOCAL = "ollama/"; // before the model's name in what was written on this PC
const CLAUDE: Record<string, string> = {
  "claude-opus-5-5": "Claude Opus 5.5",
  "claude-sonnet-5-5": "Claude Sonnet 5.5",
  "claude-fable-5-1": "Claude Fable 5.1",
};

/** The model that wrote something, for people. */
export function modelLabel(id: string): string {
  return id.startsWith(LOCAL) ? `${id.slice(LOCAL.length)} on this PC` : (CLAUDE[id] ?? id);
}

/** What that took: nothing but time on this PC. */
export function costLabel(model: string, usd: number, digits = 3): string {
  return model.startsWith(LOCAL) ? "nothing sent" : `$${usd.toFixed(digits)}`;
}

/** Who writes for a meeting: as picked on its page, else the usual one; for a private meeting
 * only the model on this PC, so nobody (null) when there is none. */
export function writerFor(
  picked: Writer | null,
  isPrivate: boolean,
  settings: Pick<Settings, "writer" | "local_model"> | undefined,
): Writer | null {
  const here = !!settings?.local_model;
  if (isPrivate) return here ? "local" : null;
  const wanted = picked ?? settings?.writer ?? "claude";
  return wanted === "local" && !here ? "claude" : wanted;
}

/** How far the model on this PC is. */
export function writingLabel({ part, of }: Pick<Writing, "part" | "of">): string {
  if (!part) return "Waiting for its turn on this PC...";
  if (part > of) return "Putting it together on this PC...";
  return of > 1 ? `Writing on this PC: part ${part} of ${of}...` : "Writing on this PC...";
}

/** A size on disk: "3.3 GB". */
export function gigabytes(bytes: number): string {
  return `${(bytes / 1e9).toFixed(1)} GB`;
}
