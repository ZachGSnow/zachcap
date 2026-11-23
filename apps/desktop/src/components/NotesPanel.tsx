import { Bookmark, Camera, Copy, NotebookPen, Plus, Trash2 } from "lucide-react";
import { useRef, useState } from "react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useReturnFocus } from "@/hooks/useReturnFocus";
import { api, mediaUrl, type Note } from "@/lib/engine";
import { attempt, copyText } from "@/lib/feedback";
import { clock } from "@/lib/format";
import { useKeys } from "@/lib/shortcuts";

/** A snapshot's picture: the small one for the list, or the full one. The note's time is in
 * the address, so a picture is never mistaken for another one's. */
function pictureUrl(note: Note, size: "thumb" | "full") {
  return mediaUrl(`/api/notes/${note.id}/image?size=${size}&v=${encodeURIComponent(note.created_at)}`);
}

/** A snapshot of the call: its picture, and the text Windows read in it. */
function SnapshotNote({ note, onOpen }: { note: Note; onOpen: (from: HTMLElement) => void }) {
  const lines = note.text.split("\n").filter((line) => line.trim());
  // Pictures go with the recording ("Keep recordings"); the text stays.
  const [missing, setMissing] = useState(false);
  return (
    <div className="flex min-w-0 flex-1 gap-3">
      <button
        onClick={(event) => onOpen(event.currentTarget)}
        className="shrink-0 overflow-hidden rounded-md border"
        title="Show the snapshot"
      >
        {missing ? (
          <span className="flex h-16 w-28 items-center justify-center bg-muted px-2 text-center text-[11px] text-muted-foreground">
            No picture
          </span>
        ) : (
          <img
            src={pictureUrl(note, "thumb")}
            alt="Snapshot of the call"
            className="h-16 w-28 object-cover object-top"
            onError={() => setMissing(true)}
          />
        )}
      </button>
      <div className="min-w-0 flex-1 text-sm">
        {lines.length ? (
          <p className="line-clamp-3 whitespace-pre-line">{lines.slice(0, 3).join("\n")}</p>
        ) : (
          <p className="text-muted-foreground">No text in this snapshot.</p>
        )}
        {lines.length > 3 && (
          <button
            onClick={(event) => onOpen(event.currentTarget)}
            className="mt-0.5 text-xs text-muted-foreground hover:text-foreground hover:underline"
          >
            {lines.length - 3} more line{lines.length === 4 ? "" : "s"}
          </button>
        )}
      </div>
    </div>
  );
}

/** The full picture, for the snapshot's own view. */
function SnapshotPicture({ note }: { note: Note }) {
  const [missing, setMissing] = useState(false);
  if (missing) {
    return (
      <p className="rounded-md border bg-muted px-3 py-6 text-center text-sm text-muted-foreground">
        The picture is no longer kept: pictures are deleted with the recording's audio.
        {note.text ? " What Windows read in it is below." : ""}
      </p>
    );
  }
  return (
    <img
      src={pictureUrl(note, "full")}
      alt="Snapshot of the call"
      className="w-full rounded-md border"
      onError={() => setMissing(true)}
    />
  );
}

/** Notes and bookmarks taken during the meeting (or added while listening back), and
 * snapshots of the call. Summaries and answers give them priority. */
export function NotesPanel({
  meetingId,
  notes,
  time,
  onSeek,
  onChanged,
}: {
  meetingId: number;
  notes: Note[];
  /** Where the player is: new notes go there. */
  time: number;
  onSeek: (seconds: number) => void;
  onChanged: () => void;
}) {
  const shortcutKeys = useKeys();
  const [text, setText] = useState("");
  const saving = useRef(false);
  const [shown, setShown] = useState<Note | null>(null);
  const returnFocus = useReturnFocus();

  const add = async () => {
    const value = text.trim();
    if (!value || saving.current) return;
    saving.current = true;
    // Cleared right away, so a second Enter can't save it twice.
    setText("");
    const added = await attempt("Couldn't save the note", () =>
      api(`/api/meetings/${meetingId}/notes`, { method: "POST", json: { text: value, kind: "note", at: time } }),
    );
    saving.current = false;
    if (added) onChanged();
    else setText((typed) => (typed ? `${value} ${typed}` : value));
  };

  const remove = async (note: Note) => {
    await attempt("Couldn't delete the note", () => api(`/api/notes/${note.id}`, { method: "DELETE" }));
    onChanged();
  };

  return (
    <div>
      <form
        className="flex gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          void add();
        }}
      >
        <Input
          placeholder={`Add a note at ${clock(time)}`}
          value={text}
          onChange={(event) => setText(event.target.value)}
          className="h-9"
        />
        <Button type="submit" size="sm" className="h-9" disabled={!text.trim()}>
          <Plus className="size-3.5" />
          Add
        </Button>
      </form>

      {notes.length === 0 ? (
        <div className="flex flex-col items-center py-12 text-center">
          <div className="flex size-12 items-center justify-center rounded-full bg-muted">
            <NotebookPen className="size-5" />
          </div>
          <p className="mt-4 font-medium">No notes yet</p>
          <p className="mt-1 max-w-sm text-sm text-muted-foreground">
            While recording, type a note in the recording bar or press {shortcutKeys.bookmark} to bookmark a moment. Summaries make
            sure to cover what you noted.
          </p>
        </div>
      ) : (
        <div className="mt-4 divide-y overflow-hidden rounded-xl border">
          {notes.map((note) => (
            <div key={note.id} className="group flex items-start gap-3 px-3 py-2.5">
              <button
                onClick={() => onSeek(note.at)}
                className="mt-0.5 shrink-0 font-mono text-xs tabular-nums text-muted-foreground hover:text-foreground hover:underline"
                title="Play from here"
              >
                {clock(note.at)}
              </button>
              {note.kind === "bookmark" ? (
                <Bookmark className="mt-0.5 size-3.5 shrink-0 fill-amber-400 text-amber-500" />
              ) : note.kind === "snapshot" ? (
                <Camera className="mt-0.5 size-3.5 shrink-0 text-sky-600 dark:text-sky-400" />
              ) : (
                <NotebookPen className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
              )}
              {note.kind === "snapshot" ? (
                <SnapshotNote
                  note={note}
                  onOpen={(from) => {
                    returnFocus.remember(from);
                    setShown(note);
                  }}
                />
              ) : (
                <p className="min-w-0 flex-1 text-sm">
                  {note.text || <span className="text-muted-foreground">Bookmarked moment</span>}
                </p>
              )}
              <Button
                size="icon"
                variant="ghost"
                className="size-7 opacity-0 group-hover:opacity-100 focus-visible:opacity-100"
                aria-label="Delete note"
                onClick={() => void remove(note)}
              >
                <Trash2 className="size-3.5" />
              </Button>
            </div>
          ))}
        </div>
      )}

      <Dialog open={shown !== null} onOpenChange={(open) => !open && setShown(null)}>
        <DialogContent className="sm:max-w-4xl" onCloseAutoFocus={returnFocus.onCloseAutoFocus}>
          <DialogHeader>
            <DialogTitle>Snapshot at {shown ? clock(shown.at) : ""}</DialogTitle>
            <DialogDescription>
              The picture never leaves this computer. The text below is what Windows read in it; summaries and Ask can
              use it.
            </DialogDescription>
          </DialogHeader>
          {shown && (
            <div className="grid max-h-[65vh] gap-3 overflow-auto">
              <SnapshotPicture key={shown.id} note={shown} />
              {shown.text && <pre className="whitespace-pre-wrap rounded-md bg-muted p-3 font-mono text-xs">{shown.text}</pre>}
            </div>
          )}
          <DialogFooter>
            {shown?.text && (
              <Button variant="outline" onClick={() => void copyText(shown.text, "Snapshot text copied")}>
                <Copy className="size-4" />
                Copy text
              </Button>
            )}
            <Button onClick={() => shown && onSeek(shown.at)}>Play from here</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
