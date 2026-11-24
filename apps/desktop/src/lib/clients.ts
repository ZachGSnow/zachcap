import type { Meeting } from "@/lib/engine";
import { shownValue } from "@/lib/inplace";

/** What the client button shows: a name being saved until the engine has answered, else the
 * page's; dashed when it comes from the meeting's people rather than being set on it. The box
 * opens on the name shown, one from its people too, so emptying it is a change ("no client")
 * and leaving it as it was saves nothing. */
export function clientShown(value: string | null, fromPeople: boolean, pending: string | null) {
  const name = shownValue(value, pending);
  return { name, dashed: fromPeople && pending === null && name !== null, start: name ?? "" };
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
