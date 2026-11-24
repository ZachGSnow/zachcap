import { Check, FileSearch, Loader2, Plus } from "lucide-react";
import { useRef, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useReturnFocus } from "@/hooks/useReturnFocus";
import { api, type WordFound, type WordsFound } from "@/lib/engine";
import { attempt } from "@/lib/feedback";
import { pickTextFile } from "@/lib/files";
import { cn } from "@/lib/utils";
import { addLabel, chosenWords, noRoom, timesFound, toggled } from "@/lib/words";

/** A word found, to tick: the whole row is the checkbox. */
function WordRow({ word, ticked, onToggle }: { word: WordFound; ticked: boolean; onToggle: () => void }) {
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={ticked}
      onClick={onToggle}
      className="flex w-full items-center gap-3 px-3 py-2 text-left text-sm outline-none hover:bg-muted/50 focus-visible:bg-muted focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:ring-inset"
    >
      <span
        className={cn(
          "flex size-4 shrink-0 items-center justify-center rounded-[4px] border transition-colors",
          ticked ? "border-foreground bg-foreground text-background" : "border-muted-foreground/40",
        )}
      >
        {ticked && <Check className="size-3" strokeWidth={3} />}
      </span>
      <span className="min-w-0 flex-1 truncate">{word.term}</span>{" "}
      <span className="shrink-0 text-xs text-muted-foreground tabular-nums">{timesFound(word.count)}</span>
    </button>
  );
}

/** Words to know from a file the user has (an asset list, an exported email, a glossary): the
 * names and terms found in it, to tick and add. */
export function WordsFromFile({ onAdded }: { onAdded: () => Promise<void> }) {
  const [reading, setReading] = useState(false);
  const [adding, setAdding] = useState(false);
  const [open, setOpen] = useState(false);
  const [found, setFound] = useState<WordsFound | null>(null); // kept while the dialog closes
  // None to start with: each one is listened for in every meeting, and goes to Claude with every
  // summary, so it's the user who says which.
  const [ticked, setTicked] = useState<ReadonlySet<string>>(new Set());
  const busy = useRef(false);
  const returnFocus = useReturnFocus();

  const words = found?.terms ?? [];
  const chosen = chosenWords(words, ticked);
  const problem = noRoom(chosen, found?.room ?? 0);

  const look = async () => {
    if (busy.current) return; // the file dialog is open already
    busy.current = true;
    await attempt("Couldn't look in the file", async () => {
      const path = await pickTextFile("Look for words to know in a text file: an asset list, an exported email, a glossary");
      if (!path) return;
      setReading(true);
      setFound(await api<WordsFound>("/api/vocabulary/found", { method: "POST", json: { path } }));
      setTicked(new Set());
      setOpen(true);
    });
    setReading(false);
    busy.current = false;
  };

  const add = async () => {
    setAdding(true);
    const added = await attempt("Couldn't add them", () =>
      api("/api/vocabulary/add", { method: "POST", json: { terms: chosen } }),
    );
    setAdding(false);
    if (!added) return;
    toast.success(chosen.length === 1 ? `"${chosen[0]}" is in Words to know` : `${chosen.length} words are in Words to know`, {
      description: "Used from the next meeting processed.",
    });
    setOpen(false);
    await onAdded();
  };

  return (
    <>
      <Button
        size="sm"
        variant="outline"
        onClick={(event) => {
          returnFocus.remember(event.currentTarget);
          void look();
        }}
      >
        {reading ? <Loader2 className="size-3.5 animate-spin" /> : <FileSearch className="size-3.5" />}
        Add from a file...
      </Button>
      <Dialog open={open} onOpenChange={(next) => !next && !adding && setOpen(false)}>
        <DialogContent className="sm:max-w-md" onCloseAutoFocus={returnFocus.onCloseAutoFocus}>
          <DialogHeader>
            <DialogTitle className="pr-8 leading-snug [overflow-wrap:anywhere]">
              {words.length ? "Found in" : "Nothing new found in"} {found?.name}
            </DialogTitle>
            <DialogDescription>
              {words.length
                ? "Tick the ones ZachCap should spell right. What you add goes to Claude with your summaries, like the rest of Words to know; nothing else of the file is kept or sent."
                : "ZachCap looks for names, products and jargon. The ones already in Words to know, and people you've named, are left out, and so is anything near a password."}
            </DialogDescription>
          </DialogHeader>

          {words.length > 0 && (
            <div
              role="group"
              aria-label="Names and terms found"
              className="max-h-[45vh] divide-y overflow-y-auto rounded-lg border"
            >
              {words.map((word) => (
                <WordRow
                  key={word.term}
                  word={word}
                  ticked={ticked.has(word.term)}
                  onToggle={() => setTicked(toggled(ticked, word.term))}
                />
              ))}
            </div>
          )}
          {found?.cut && (
            <p className="text-xs text-muted-foreground">Only the first 20,000 characters of the file were looked at.</p>
          )}
          {problem ? (
            <p role="alert" className="text-xs text-amber-700 dark:text-amber-300">
              {problem}
            </p>
          ) : (
            words.length > 0 && (
              <p className="text-xs text-muted-foreground">
                Pick the ones that come up in calls: Whisper takes in about the first 500 characters of the list.
              </p>
            )
          )}

          <DialogFooter>
            {words.length ? (
              <>
                <Button variant="outline" disabled={adding} onClick={() => setOpen(false)}>
                  Cancel
                </Button>
                <Button disabled={adding || !chosen.length || problem !== null} onClick={() => void add()}>
                  {adding ? <Loader2 className="size-4 animate-spin" /> : <Plus className="size-4" />}
                  {addLabel(chosen.length)}
                </Button>
              </>
            ) : (
              <Button onClick={() => setOpen(false)}>Close</Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
