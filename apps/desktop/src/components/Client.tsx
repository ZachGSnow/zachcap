import { Building2 } from "lucide-react";
import { useId, useState } from "react";

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
 * clients named so far. Saving "" takes it off. */
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
  const listId = useId();

  const save = () => {
    setEditing(false);
    if (text.trim() !== (inferred ? "" : (value ?? ""))) onSave(text.trim());
  };

  if (editing) {
    return (
      <>
        <Input
          autoFocus
          list={listId}
          value={text}
          placeholder="Client"
          aria-label="Client"
          onChange={(event) => setText(event.target.value)}
          onBlur={save}
          onKeyDown={(event) => {
            if (event.key === "Enter") save();
            if (event.key === "Escape") setEditing(false);
          }}
          className="h-6 w-40 text-xs"
        />
        <ClientList id={listId} known={known} />
      </>
    );
  }
  return (
    <button
      title={hint}
      onClick={() => {
        setText(inferred ? "" : (value ?? ""));
        setEditing(true);
      }}
      className={cn(
        "inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs transition-colors hover:bg-muted hover:text-foreground",
        value && !inferred && "border text-foreground",
        value && inferred && "border border-dashed",
      )}
    >
      <Building2 className="size-3" />
      {value ?? empty}
    </button>
  );
}
