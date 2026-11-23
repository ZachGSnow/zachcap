import { Building2 } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";

import { Input } from "@/components/ui/input";
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
  /** Shown lighter: it comes from the people in the meeting, not set on it. */
  inferred?: boolean;
  empty?: string;
  hint: string;
  onSave: (client: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState("");
  // What it was when the box opened, and what was saved until the page catches up: a quick
  // reopen shows the new name, and leaving the box unchanged saves nothing.
  const [opened, setOpened] = useState("");
  const [saved, setSaved] = useState<string | null>(null);
  useEffect(() => setSaved(null), [value]);
  const button = useRef<HTMLButtonElement>(null);
  const listId = useId();
  const shown = saved !== null ? saved || null : value;

  const close = () => {
    setEditing(false);
    requestAnimationFrame(() => button.current?.focus());
  };
  const save = () => {
    close();
    const wanted = text.trim();
    if (wanted === opened) return;
    setSaved(wanted);
    onSave(wanted);
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
          onChange={(event) => setText(event.target.value)}
          onBlur={save}
          onKeyDown={(event) => {
            if (event.key === "Enter") save();
            if (event.key === "Escape") close();
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
        const start = inferred && saved === null ? "" : (shown ?? "");
        setText(start);
        setOpened(start);
        setEditing(true);
      }}
      className={cn(
        "inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs transition-colors hover:bg-muted hover:text-foreground",
        shown && !(inferred && saved === null) && "border text-foreground",
        shown && inferred && saved === null && "border border-dashed",
      )}
    >
      <Building2 className="size-3" />
      {shown ?? empty}
    </button>
  );
}
