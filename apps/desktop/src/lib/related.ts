import type { RelatedMeeting } from "@/lib/engine";

/** How many of a meeting's related ones show before "more". */
export const SHOWN_AT_FIRST = 3;

/** What the page asks a meeting's related ones for: the meeting as it was last loaded, so a
 * change made on the page (its client, a name, a line) asks again, or null while there's nothing
 * to ask about yet (it isn't loaded, or is being processed, when its lines are still coming). */
export function relatedKey<T>(loaded: T | undefined, processing: boolean): T | null {
  return loaded === undefined || processing ? null : loaded;
}

/** The day a related meeting was on, with its year when that's another one than ``now``'s. */
export function relatedDay(iso: string, now: Date = new Date()): string {
  const date = new Date(iso);
  const year = date.getFullYear() === now.getFullYear() ? undefined : "numeric";
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric", year });
}

/** A line of the list: its title, when, what it shares with the meeting shown, and all of it on
 * hover (a row cuts long ones short). */
export type RelatedRow = { id: number; title: string; when: string; why: string; hint: string };

export function relatedRows(meetings: RelatedMeeting[], now: Date = new Date()): RelatedRow[] {
  return meetings.map(({ id, title, started_at, reasons }) => {
    const when = relatedDay(started_at, now);
    const why = reasons.join(" \u00b7 ");
    return { id, title, when, why, hint: `${title} (${when}): ${why}` };
  });
}

/** The rows to show: the first few, or all of them, with how many are left for "more" (none once
 * they all show). */
export function shownRows(rows: RelatedRow[], all: boolean): { rows: RelatedRow[]; more: number } {
  const shown = all ? rows : rows.slice(0, SHOWN_AT_FIRST);
  return { rows: shown, more: rows.length - shown.length };
}
