import type { LiveLine, LiveState } from "@/lib/engine";
import { minutes } from "@/lib/format";

/** The lines on the live panel, and how far it has read: `after`, a line id of the live
 * transcript `run` (each recording has its own, and numbers its lines from 1). */
export type LiveView = { run: string | null; after: number; lines: LiveLine[] };

export const EMPTY_VIEW: LiveView = { run: null, after: 0, lines: [] };

/** The panel keeps the latest lines only; the transcript made after the call has them all. */
export const LINES_SHOWN = 500;

/** The address that asks for the lines after the panel's cursor. */
export function livePath(view: LiveView): string {
  return view.run ? `/api/live?run=${encodeURIComponent(view.run)}&after=${view.after}` : "/api/live";
}

/** The panel's lines after an answer: the new ones added in the order they were said (a
 * line comes when its speaker stops, so one that started earlier can come later), any left
 * out as echo since taken away, and all of them again for another live transcript (a new
 * recording's). The same view when nothing changed, so nothing on screen moves. */
export function mergeLines(view: LiveView, answer: Pick<LiveState, "run" | "lines" | "dropped">): LiveView {
  if (!answer.run) return view.run === null && !view.lines.length ? view : EMPTY_VIEW;
  const same = answer.run === view.run;
  const kept = same ? view.lines : [];
  const seen = new Set(kept.map((line) => line.id));
  const added = answer.lines.filter((line) => !seen.has(line.id));
  const gone = new Set(answer.dropped.filter((id) => seen.has(id)));
  if (same && !added.length && !gone.size) return view;
  const lines = [...kept, ...added]
    .filter((line) => !gone.has(line.id))
    .sort((a, b) => a.start - b.start || a.id - b.id)
    .slice(-LINES_SHOWN);
  const after = Math.max(same ? view.after : 0, ...answer.lines.map((line) => line.id));
  return { run: answer.run, after, lines };
}

/** Who said a line: you, by the name you gave ZachCap, or someone on the call. */
export function speaker(line: Pick<LiveLine, "track">, me: string | null | undefined): string {
  return line.track === "mic" ? me?.trim() || "You" : "Call";
}

/** The recording's clock now: what the engine last said, moved on by the time since (the
 * panel asks once a second, and ticks in between). It stands still while the engine can't be
 * reached: nobody knows then whether it's still recording. */
export function elapsedNow(elapsed: number | null, saidAt: number, now: number, unreachable: string | null = null): number | null {
  if (elapsed === null || unreachable !== null) return elapsed;
  return elapsed + Math.max(0, now - saidAt) / 1000;
}

/** The panel's dot: red while it's recording, amber while that's paused; off (grey) when it
 * isn't, or when the engine can't be reached to say. */
export function dot(state: Pick<LiveState, "recording" | "paused"> | null, unreachable: string | null): "recording" | "paused" | "off" {
  if (!state?.recording || unreachable !== null) return "off";
  return state.paused ? "paused" : "recording";
}

/** Whether a scrolled list is at its end (within `slack` pixels): new lines keep it there. */
export function atEnd(box: { scrollTop: number; scrollHeight: number; clientHeight: number }, slack = 24): boolean {
  return box.scrollHeight - box.scrollTop - box.clientHeight <= slack;
}

/** What Catch me up said of the call so far (the engine's /api/live/catch-up). */
export type CatchUp = { text: string; by: "claude" | "local"; model: string; cost_usd: number };

/** Who wrote a catch-up, as the panel says under it. */
export function catchUpBy(said: Pick<CatchUp, "by" | "model">): string {
  return said.by === "claude" ? "By Claude Haiku" : `By ${said.model} on this PC`;
}

export type LiveNotice = { key: string; tone: "error" | "warning" | "info"; text: string };

/** What the panel says above the lines, most important first: the engine out of reach, what
 * went wrong, why lines are late or missing. `unreachable`: why the last ask failed. */
export function notices(state: LiveState | null, unreachable: string | null): LiveNotice[] {
  const said: LiveNotice[] = [];
  if (unreachable) said.push({ key: "engine", tone: "error", text: `Can't reach the ZachCap engine: ${unreachable}` });
  if (!state) return said;
  if (state.error) {
    said.push({ key: "error", tone: "error", text: `The live transcript ran into a problem: ${state.error}` });
  }
  if (state.on && state.waiting) {
    said.push({
      key: "waiting",
      tone: "info",
      text: "Waiting while an earlier meeting is processed. Lines carry on once that's done.",
    });
  }
  if (state.on && state.paused) {
    said.push({ key: "paused", tone: "info", text: "Paused: nothing is recorded or transcribed until you resume." });
  }
  if (state.skipped >= 1) {
    said.push({
      key: "skipped",
      tone: "warning",
      text: `Skipped ${minutes(state.skipped)} to keep up. The transcript made after the call has every word.`,
    });
  }
  if (!state.recording) said.push({ key: "ended", tone: "info", text: "The recording ended." });
  else if (!state.on) said.push({ key: "off", tone: "info", text: "The live transcript is off for this recording." });
  return said;
}
