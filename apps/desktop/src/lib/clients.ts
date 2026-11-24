import type { Meeting } from "@/lib/engine";

/** How the client box closes: Enter, Escape, or a click somewhere else. */
export type Closing = "Enter" | "Escape" | "blur";

/** What the client button shows: a name being saved until the engine has answered, else the
 * page's; dashed when it comes from the meeting's people rather than being set on it. The box
 * opens on the name shown, one from its people too, so emptying it is a change ("no client")
 * and leaving it as it was saves nothing. */
export function clientShown(value: string | null, fromPeople: boolean, pending: string | null) {
  const name = pending !== null ? pending || null : value;
  return { name, dashed: fromPeople && pending === null && name !== null, start: name ?? "" };
}

/** What closing the box does: Enter, or a click elsewhere, saves what was typed if it changed
 * (``save`` is null when there's nothing to save); Escape saves nothing. Focus goes back to
 * the button after a key only: after a click it stays where the click put it. */
export function closing(how: Closing, start: string, typed: string): { save: string | null; refocus: boolean } {
  const wanted = typed.trim();
  return { save: how !== "Escape" && wanted !== start ? wanted : null, refocus: how !== "blur" };
}

/** Saves a client, showing it until the save is over and the page has caught up (``show(null)``
 * then shows the page's again), so a name the engine refused doesn't stay on screen. */
export async function saveShowing(
  name: string,
  save: (client: string) => Promise<unknown>,
  show: (pending: string | null) => void,
): Promise<void> {
  show(name);
  try {
    await save(name);
  } finally {
    show(null);
  }
}

type MeetingClient = Pick<Meeting, "client" | "client_inferred" | "client_kept" | "client_set">;

/** The client button's hint in a meeting's header. */
export function clientHint(meeting: MeetingClient): string {
  if (meeting.client_inferred) {
    return "The client the people in this meeting work for: click to set another (empty: no client)";
  }
  if (meeting.client_kept) {
    return "The client its people worked for then, kept when one of them moved on: click to set another";
  }
  if (meeting.client_set && !meeting.client) return "Set as not for a client: click to set one";
  return "The client this meeting was for: click to change it (empty: no client)";
}

/** Whether to offer a meeting's client from its people again: one was set on it, or kept. */
export function followsOffered(meeting: MeetingClient): boolean {
  return Boolean(meeting.client_set || meeting.client_kept);
}
