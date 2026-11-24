import { Ticket } from "lucide-react";
import { useRef, useState } from "react";

import { Input } from "@/components/ui/input";
import type { TicketFound } from "@/lib/engine";
import { closing, saveShowing, type Closing } from "@/lib/inplace";
import { ticketShown } from "@/lib/tickets";

function found(suggestion: TicketFound): string {
  const where = suggestion.where.map(
    (place) =>
      ({ title: "in the title", notes: "in your notes", snapshots: "in a snapshot", files: "in an attached file", transcript: "said" })[
        place
      ],
  );
  const times = suggestion.count > 1 ? ` (${suggestion.count} times)` : "";
  return `${where.join(", ")}${times}`;
}

/** The service ticket a meeting was about: shown and changed in its header. Ticket numbers
 * found in the meeting (Settings > Tickets says how they look) are offered to pick. */
export function TicketControl({
  ticket,
  suggestions,
  onSave,
}: {
  ticket: string | null;
  suggestions: TicketFound[];
  /** Saves it ("" takes it off); done once the engine has answered and the page has caught up. */
  onSave: (ticket: string) => Promise<unknown>;
}) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");
  // What the box opened with (left as it was, nothing is saved), and a ticket being saved,
  // shown until the save is over.
  const [start, setStart] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  // Closed once only: Enter, then the box's blur as it goes away, is one close.
  const open = useRef(false);
  const button = useRef<HTMLButtonElement>(null);
  const shown = ticketShown(ticket, pending, suggestions);
  const refocus = () => requestAnimationFrame(() => button.current?.focus());

  const finish = (how: Closing) => {
    if (!open.current) return;
    open.current = false;
    setEditing(false);
    const { save, refocus: back } = closing(how, start, value);
    if (back) refocus();
    if (save !== null) void saveShowing(save, onSave, setPending);
  };
  const pick = (suggested: string) => {
    refocus(); // onto the ticket it becomes
    void saveShowing(suggested, onSave, setPending);
  };
  const edit = () => {
    setValue(shown.start);
    setStart(shown.start);
    open.current = true;
    setEditing(true);
  };

  if (editing) {
    return (
      <Input
        autoFocus
        value={value}
        placeholder="Ticket number"
        aria-label="Ticket number"
        maxLength={40}
        onChange={(event) => setValue(event.target.value)}
        onBlur={() => finish("blur")}
        onKeyDown={(event) => {
          if (event.key === "Enter") finish("Enter");
          if (event.key === "Escape") finish("Escape");
        }}
        className="h-6 w-36 font-mono text-xs"
      />
    );
  }
  if (shown.ticket) {
    return (
      <button
        ref={button}
        onClick={edit}
        title="The ticket this meeting was about: click to change it (empty takes it off)"
        className="inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 font-mono text-xs text-foreground transition-colors hover:bg-muted"
      >
        <Ticket className="size-3" />
        {shown.ticket}
      </button>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5">
      {shown.offered.map((suggestion) => (
        <button
          key={suggestion.ticket}
          onClick={() => pick(suggestion.ticket)}
          title={`Use ticket ${suggestion.ticket}: ${found(suggestion)}`}
          className="inline-flex items-center gap-1 rounded-md border border-dashed px-1.5 py-0.5 font-mono text-xs transition-colors hover:bg-muted hover:text-foreground"
        >
          <Ticket className="size-3" />
          {suggestion.ticket}?
        </button>
      ))}
      <button ref={button} onClick={edit} className="text-xs transition-colors hover:text-foreground">
        {suggestions.length ? "Another ticket" : "Add a ticket"}
      </button>
    </span>
  );
}
