import type { CallEvent } from "@/lib/engine";

/** Where the page is in what the engine did on its own: which run of the engine (one started
 * again numbers its events from 1) and the last event said. */
export type EventCursor = { boot: string; id: number };

/** The address of the recording's state, asking only for the events after the cursor. */
export function recordPath(cursor: EventCursor | null): string {
  return cursor ? `/api/record?boot=${encodeURIComponent(cursor.boot)}&after=${cursor.id}` : "/api/record";
}

/** The events to say now, oldest first, and the cursor after them. The first answer only
 * sets the cursor (what happened before the page loaded is old news); everything from a run
 * of the engine the cursor isn't from is news, whatever its ids. */
export function newEvents(
  cursor: EventCursor | null,
  boot: string | undefined,
  events: CallEvent[] | undefined,
): { cursor: EventCursor | null; fresh: CallEvent[] } {
  if (!boot) return { cursor, fresh: [] };
  const list = [...(events ?? [])].sort((a, b) => a.id - b.id);
  const last = list.length ? list[list.length - 1].id : 0;
  if (cursor === null) return { cursor: { boot, id: last }, fresh: [] };
  const from = cursor.boot === boot ? cursor.id : 0;
  const fresh = list.filter((event) => event.id > from);
  return { cursor: { boot, id: Math.max(from, last) }, fresh };
}
