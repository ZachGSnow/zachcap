import { api, type Writing } from "@/lib/engine";
import { announce, windowInFront } from "@/lib/feedback";

const EVERY = 1000; // ms between two looks
const TRIES = 5; // looks that fail before it's given up on (the engine being started again, say)

/** What the engine answers when asked to write: what Claude wrote, or, when the model on this PC
 * writes it (which takes minutes), what to ask for it by. */
export type Asked<T> = T | { writing: string };

/** Whether it's being written on this PC. */
export function isHere<T extends object>(asked: Asked<T>): asked is { writing: string } {
  return "writing" in asked;
}

/** What the model on this PC wrote, once it has. Looked for every second rather than waited for in
 * one request: the page has six at a time, and the recording needs some of them. */
export async function outcome<T>(id: string): Promise<T> {
  for (let failed = 0; ; ) {
    await new Promise((next) => setTimeout(next, EVERY));
    let now: Writing<T>;
    try {
      now = await api<Writing<T>>(`/api/writing/${id}`);
      failed = 0;
    } catch (error) {
      failed += 1;
      if (failed === TRIES) throw error;
      continue;
    }
    if (now.state === "done") return now.result as T;
    if (now.state !== "writing") throw new Error(now.error ?? "It wasn't written.");
  }
}

const told = new Set<string>();

/** Says that something was written, unless the user is ``looking`` at it. Once: the page it was
 * asked for on may have been left since, and opened again. */
export async function tell(id: string, what: string, title: string, looking: boolean) {
  if (looking && (await windowInFront())) return;
  if (told.has(id)) return;
  told.add(id);
  void announce(what, title, "success");
}

/** What was asked of the model on this PC before a meeting's page was opened, oldest first: what
 * it wrote, and what it's still on. */
export function earlier(
  what: Writing["what"],
  written: Writing[] | undefined,
  writing: Writing | null,
): Writing[] {
  return [...(written ?? []), ...(writing ? [writing] : [])].filter(
    (one) => one.what === what && (one.state === "done" || one.state === "writing"),
  );
}
