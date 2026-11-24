import type { TicketFound } from "@/lib/engine";
import { shownValue } from "@/lib/inplace";

/** What the ticket control shows: a ticket being saved until the engine has answered, else
 * the meeting's; with none, up to three tickets found in the meeting to pick from. The box
 * opens on the ticket shown. */
export function ticketShown(ticket: string | null, pending: string | null, suggestions: TicketFound[]) {
  const shown = shownValue(ticket, pending);
  return { ticket: shown, start: shown ?? "", offered: shown ? [] : suggestions.slice(0, 3) };
}
