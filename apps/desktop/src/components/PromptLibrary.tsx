import { Loader2, Pencil, Plus, RotateCcw, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { api, type Prompt, type PromptKind } from "@/lib/engine";
import { useReturnFocus } from "@/hooks/useReturnFocus";
import { attempt } from "@/lib/feedback";

type Limits = { label: number; instructions: number };

const COPY: Record<
  PromptKind,
  { title: string; help: string; noun: string; hint: string; namePlaceholder: string; placeholder: string; empty: string }
> = {
  template: {
    title: "Kinds of meeting",
    help: "What a summary focuses on. Pick one per meeting, or set the usual one above.",
    noun: "kind of meeting",
    hint: "Say what matters in this kind of meeting: what to pull out, what to leave out, the words your team uses. Summaries keep their sections (overview, decisions, action items, who said what) whatever you write.",
    namePlaceholder: "Vendor call",
    placeholder:
      "This is a call with a vendor. Focus on quotes and prices, lead times and delivery dates, what we asked them for, and what they promised and by when.",
    empty: "No extra focus: the usual sections only.",
  },
  recipe: {
    title: "One-click documents",
    help: "What Claude can write from a meeting, in the Summary tab. Claude gets the summary and the transcript.",
    noun: "document",
    hint: "Say what to write and how: who it's for, the sections, the format and the tone. Write {me} where your name goes.",
    namePlaceholder: "Client recap",
    placeholder:
      "Write a short recap {me} can paste into the client's ticket: what was reported, what we did, what's next with dates, and anything the client must do.",
    empty: "",
  },
};

/** Add or edit one kind of meeting or document. */
function PromptDialog({
  kind,
  editing,
  open,
  limits,
  onClose,
  onSaved,
  onCloseAutoFocus,
}: {
  kind: PromptKind;
  /** null: a new one. */
  editing: Prompt | null;
  open: boolean;
  limits: Limits;
  onClose: () => void;
  onSaved: () => void;
  /** Where focus goes when it closes (it opens from state, not from a trigger). */
  onCloseAutoFocus: (event: Event) => void;
}) {
  const copy = COPY[kind];
  const [label, setLabel] = useState("");
  const [instructions, setInstructions] = useState("");
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    if (!open) return;
    setLabel(editing?.label ?? "");
    setInstructions(editing?.instructions ?? "");
  }, [open, editing]);

  // The general kind of meeting is the one that may have no instructions.
  const mayBeEmpty = kind === "template" && (editing?.builtin ?? false);
  const ready = label.trim() !== "" && (mayBeEmpty || instructions.trim() !== "");

  const save = async () => {
    if (!ready || saving) return;
    setSaving(true);
    const saved = await attempt(`Couldn't save the ${copy.noun}`, () =>
      editing
        ? api(`/api/prompts/${editing.id}`, { method: "PATCH", json: { label, instructions } })
        : api("/api/prompts", { method: "POST", json: { kind, label, instructions } }),
    );
    setSaving(false);
    if (!saved) return;
    toast.success(editing ? `Saved ${label.trim()}` : `Added ${label.trim()}`);
    onSaved();
    onClose();
  };

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="sm:max-w-xl" onCloseAutoFocus={onCloseAutoFocus}>
        <DialogHeader>
          <DialogTitle>{editing ? `Edit ${editing.label}` : `Add a ${copy.noun}`}</DialogTitle>
          <DialogDescription>{copy.hint}</DialogDescription>
        </DialogHeader>
        <form
          className="grid gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            void save();
          }}
        >
          <div className="grid gap-1.5">
            <Label htmlFor="prompt-label">Name</Label>
            <Input
              id="prompt-label"
              autoFocus
              maxLength={limits.label}
              placeholder={copy.namePlaceholder}
              value={label}
              onChange={(event) => setLabel(event.target.value)}
            />
          </div>
          <div className="grid gap-1.5">
            <div className="flex items-baseline justify-between">
              <Label htmlFor="prompt-instructions">Instructions</Label>
              <span className="text-[10px] tabular-nums text-muted-foreground">
                {instructions.length.toLocaleString("en-US")} / {limits.instructions.toLocaleString("en-US")}
              </span>
            </div>
            <Textarea
              id="prompt-instructions"
              maxLength={limits.instructions}
              placeholder={copy.placeholder}
              value={instructions}
              onChange={(event) => setInstructions(event.target.value)}
              className="max-h-[45vh] min-h-36 overflow-y-auto text-sm leading-relaxed"
            />
            {editing?.builtin && (
              <p className="text-xs text-muted-foreground">
                Built in: Reset in the list brings back ZachCap's own wording.
              </p>
            )}
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={!ready || saving}>
              {saving && <Loader2 className="size-4 animate-spin" />}
              Save
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

type Ask = { title: string; body: string; action: string; run: () => Promise<void> };

/** Asks before something that throws away the user's own text. */
function Confirm({
  open,
  ask,
  onCancel,
  onCloseAutoFocus,
}: {
  open: boolean;
  ask: Ask | null;
  onCancel: () => void;
  onCloseAutoFocus: (event: Event) => void;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onCancel()}>
      <DialogContent className="sm:max-w-sm" onCloseAutoFocus={onCloseAutoFocus}>
        <DialogHeader>
          <DialogTitle>{ask?.title}</DialogTitle>
          <DialogDescription>{ask?.body}</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={onCancel}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            disabled={busy}
            onClick={async () => {
              if (!ask) return;
              setBusy(true);
              await ask.run();
              setBusy(false);
              onCancel();
            }}
          >
            {busy && <Loader2 className="size-4 animate-spin" />}
            {ask?.action}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** One list of the prompt library: the built-ins (edit, reset) and the user's own (edit,
 * delete), with Add. */
export function PromptList({
  kind,
  items,
  limits,
  onChanged,
}: {
  kind: PromptKind;
  items: Prompt[];
  limits: Limits;
  onChanged: () => void;
}) {
  const copy = COPY[kind];
  // What a dialog shows is kept while it closes, so its text doesn't change as it fades.
  const [dialog, setDialog] = useState<{ open: boolean; editing: Prompt | null }>({ open: false, editing: null });
  const [confirm, setConfirm] = useState<{ open: boolean; ask: Ask | null }>({ open: false, ask: null });
  const ask = (next: Ask) => setConfirm({ open: true, ask: next });
  // Focus goes back to the button that opened a dialog, or to Add when that button is gone
  // (the item was deleted, or reset and no longer has a Reset button).
  const addButton = useRef<HTMLButtonElement>(null);
  const returnFocus = useReturnFocus(() => addButton.current);

  const reset = (item: Prompt) =>
    ask({
      title: `Reset ${item.label}?`,
      body: "Your changes to its name and instructions are replaced with ZachCap's own.",
      action: "Reset",
      run: async () => {
        if (await attempt(`Couldn't reset ${item.label}`, () => api(`/api/prompts/${item.id}/reset`, { method: "POST" }))) {
          toast.success(`${item.label} is back to how ZachCap ships it`);
        }
        onChanged();
      },
    });

  const remove = (item: Prompt) =>
    ask({
      title: `Delete ${item.label}?`,
      body:
        kind === "template"
          ? "Summaries already written keep their text. Meetings summarized as this kind use your usual kind next time."
          : "It disappears from the Summary tab. Nothing already written is affected.",
      action: "Delete",
      run: async () => {
        if (await attempt(`Couldn't delete ${item.label}`, () => api(`/api/prompts/${item.id}`, { method: "DELETE" }))) {
          toast.success(`Deleted ${item.label}`);
        }
        onChanged();
      },
    });

  return (
    <div className="px-4 py-3.5">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium">{copy.title}</div>
          <p className="mt-0.5 text-xs text-muted-foreground">{copy.help}</p>
        </div>
        <Button
          ref={addButton}
          size="sm"
          variant="outline"
          onClick={(event) => {
            returnFocus.remember(event.currentTarget);
            setDialog({ open: true, editing: null });
          }}
        >
          <Plus className="size-3.5" />
          Add
        </Button>
      </div>
      <ul className="mt-2.5 divide-y overflow-hidden rounded-lg border" aria-label={copy.title}>
        {items.map((item) => (
          <li key={item.id} className="flex items-center gap-2 px-3 py-2">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-1.5">
                <span className="truncate text-sm">{item.label}</span>
                {item.builtin && item.edited && (
                  <Badge variant="secondary" className="h-4.5 px-1.5 text-[10px]">
                    Edited
                  </Badge>
                )}
                {!item.builtin && (
                  <Badge variant="outline" className="h-4.5 px-1.5 text-[10px]">
                    Yours
                  </Badge>
                )}
              </div>
              <p className="truncate text-xs text-muted-foreground" title={item.instructions || undefined}>
                {item.instructions || copy.empty}
              </p>
            </div>
            <Button
              size="icon"
              variant="ghost"
              className="size-7 shrink-0"
              aria-label={`Edit ${item.label}`}
              title="Edit"
              onClick={(event) => {
                returnFocus.remember(event.currentTarget);
                setDialog({ open: true, editing: item });
              }}
            >
              <Pencil className="size-3.5" />
            </Button>
            {item.builtin ? (
              item.edited && (
                <Button
                  size="icon"
                  variant="ghost"
                  className="size-7 shrink-0"
                  aria-label={`Reset ${item.label}`}
                  title="Reset to how ZachCap ships it"
                  onClick={(event) => {
                    returnFocus.remember(event.currentTarget);
                    reset(item);
                  }}
                >
                  <RotateCcw className="size-3.5" />
                </Button>
              )
            ) : (
              <Button
                size="icon"
                variant="ghost"
                className="size-7 shrink-0"
                aria-label={`Delete ${item.label}`}
                title="Delete"
                onClick={(event) => {
                  returnFocus.remember(event.currentTarget);
                  remove(item);
                }}
              >
                <Trash2 className="size-3.5" />
              </Button>
            )}
          </li>
        ))}
      </ul>
      <PromptDialog
        kind={kind}
        editing={dialog.editing}
        open={dialog.open}
        limits={limits}
        onClose={() => setDialog((d) => ({ ...d, open: false }))}
        onSaved={onChanged}
        onCloseAutoFocus={returnFocus.onCloseAutoFocus}
      />
      <Confirm
        open={confirm.open}
        ask={confirm.ask}
        onCancel={() => setConfirm((c) => ({ ...c, open: false }))}
        onCloseAutoFocus={returnFocus.onCloseAutoFocus}
      />
    </div>
  );
}
