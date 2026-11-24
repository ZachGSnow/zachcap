import { Building2 } from "lucide-react";
import { useId, useRef, useState } from "react";

import { Input } from "@/components/ui/input";
import { clientShown } from "@/lib/clients";
import { closing, saveShowing, type Closing } from "@/lib/inplace";
import { cn } from "@/lib/utils";

/** The clients named so far, offered while typing a client's name. */
export function ClientList({ id, known }: { id: string; known: string[] }) {
  return (
    <datalist id={id}>
      {known.map((name) => (
        <option key={name} value={name} />
      ))}
    </datalist>
  );
}

/** A client's name, shown and changed in place: a button that becomes a box offering the
 * clients named so far. Saving "" takes it off (for a meeting: "no client"). */
export function ClientField({
  value,
  known,
  inferred = false,
  empty = "Add a client",
  hint,
  onSave,
}: {
  value: string | null;
  known: string[];
  /** Shown dashed: it comes from the people in the meeting, not set on it. */
  inferred?: boolean;
  empty?: string;
  hint: string;
  /** Saves it; done once the engine has answered and the page has caught up. */
  onSave: (client: string) => Promise<unknown>;
}) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState("");
  // What the box opened with (left as it was, nothing is saved), and a name being saved,
  // shown until the save is over.
  const [start, setStart] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  // Closed once only: Enter, then the box's blur as it goes away, is one close.
  const open = useRef(false);
  const button = useRef<HTMLButtonElement>(null);
  const listId = useId();
  const shown = clientShown(value, inferred, pending);

  const finish = (how: Closing) => {
    if (!open.current) return;
    open.current = false;
    setEditing(false);
    const { save, refocus } = closing(how, start, text);
    if (refocus) requestAnimationFrame(() => button.current?.focus());
    if (save !== null) void saveShowing(save, onSave, setPending);
  };

  if (editing) {
    return (
      <>
        <Input
          autoFocus
          list={listId}
          value={text}
          maxLength={80}
          placeholder="Client"
          aria-label="Client"
          onFocus={(event) => event.currentTarget.select()}
          onChange={(event) => setText(event.target.value)}
          onBlur={() => finish("blur")}
          onKeyDown={(event) => {
            if (event.key === "Enter") finish("Enter");
            if (event.key === "Escape") finish("Escape");
          }}
          className="h-6 w-40 text-xs"
        />
        <ClientList id={listId} known={known} />
      </>
    );
  }
  return (
    <button
      ref={button}
      title={hint}
      onClick={() => {
        setText(shown.start);
        setStart(shown.start);
        open.current = true;
        setEditing(true);
      }}
      className={cn(
        "inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs transition-colors hover:bg-muted hover:text-foreground",
        shown.name && !shown.dashed && "border text-foreground",
        shown.dashed && "border border-dashed",
      )}
    >
      <Building2 className="size-3" />
      {shown.name ?? empty}
    </button>
  );
}
