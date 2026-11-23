import { Ticket } from "lucide-react";
import { useState } from "react";

import { Input } from "@/components/ui/input";
import { api, type TicketFound } from "@/lib/engine";
import { attempt } from "@/lib/feedback";

function found(suggestion: TicketFound): string {
  const where = suggestion.where.map(
    (place) => ({ title: "in the title", notes: "in your notes", snapshots: "in a snapshot", transcript: "said" })[place],
  );
  const times = suggestion.count > 1 ? ` (${suggestion.count} times)` : "";
  return `${where.join(", ")}${times}`;
}

/** The service ticket a meeting was about: shown and changed in its header. Ticket numbers
 * found in the meeting (Settings > Tickets says how they look) are offered to pick. */
export function TicketControl({
  meetingId,
  ticket,
  suggestions,
  onChanged,
}: {
  meetingId: number;
  ticket: string | null;
  suggestions: TicketFound[];
  onChanged: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");

  const save = async (next: string) => {
    setEditing(false);
    if (next.trim() === (ticket ?? "")) return;
    const saved = await attempt("Couldn't save the ticket", () =>
      api(`/api/meetings/${meetingId}`, { method: "PATCH", json: { ticket: next.trim() } }),
    );
    if (saved) onChanged();
  };
  const edit = () => {
    setValue(ticket ?? "");
    setEditing(true);
  };

  if (editing) {
    return (
      <Input
        autoFocus
        value={value}
        placeholder="Ticket number"
        aria-label="Ticket number"
        onChange={(event) => setValue(event.target.value)}
        onBlur={() => void save(value)}
        onKeyDown={(event) => {
          if (event.key === "Enter") void save(value);
          if (event.key === "Escape") setEditing(false);
        }}
        className="h-6 w-36 font-mono text-xs"
      />
    );
  }
  if (ticket) {
    return (
      <button
        onClick={edit}
        title="The ticket this meeting was about: click to change it (empty takes it off)"
        className="inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 font-mono text-xs text-foreground transition-colors hover:bg-muted"
      >
        <Ticket className="size-3" />
        {ticket}
      </button>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5">
      {suggestions.slice(0, 3).map((suggestion) => (
        <button
          key={suggestion.ticket}
          onClick={() => void save(suggestion.ticket)}
          title={`Use ticket ${suggestion.ticket}: ${found(suggestion)}`}
          className="inline-flex items-center gap-1 rounded-md border border-dashed px-1.5 py-0.5 font-mono text-xs transition-colors hover:bg-muted hover:text-foreground"
        >
          <Ticket className="size-3" />
          {suggestion.ticket}?
        </button>
      ))}
      <button onClick={edit} className="text-xs transition-colors hover:text-foreground">
        {suggestions.length ? "Another ticket" : "Add a ticket"}
      </button>
    </span>
  );
}
