import { Check, EyeOff, MoreHorizontal, Pencil, Play, Search, UserRoundSearch } from "lucide-react";
import { memo, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { api, mediaUrl, type Person, type Segment, type Speaker } from "@/lib/engine";
import { attempt } from "@/lib/feedback";
import { clock, minutes, type SpeakerColor } from "@/lib/format";
import { cn } from "@/lib/utils";

export function WhoIsThis({
  meetingId,
  speaker,
  people,
  onNamed,
}: {
  meetingId: number;
  speaker: Speaker;
  people: Person[];
  onNamed: () => void;
}) {
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const sample = useRef<HTMLAudioElement>(null);

  const save = async (value: string) => {
    if (!value.trim()) return;
    setSaving(true);
    try {
      await api(`/api/meetings/${meetingId}/speakers/${speaker.id}/name`, {
        method: "POST",
        json: { name: value.trim() },
      });
      toast.success(`Saved ${value.trim()}`, { description: "Their voice will be recognized in future meetings." });
      onNamed();
    } catch (error) {
      toast.error("Couldn't save the name", { description: String(error) });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex items-center gap-3 rounded-xl border bg-card px-3 py-2.5">
      <Button
        size="icon"
        variant="outline"
        className="size-8 shrink-0 rounded-full"
        disabled={speaker.sample_start == null}
        title={speaker.sample_start == null ? "No sample left to play (it was redacted)" : undefined}
        onClick={() => {
          if (!sample.current) return;
          sample.current.currentTime = 0;
          void sample.current.play();
        }}
        aria-label={`Play a sample of ${speaker.label}`}
      >
        <Play className="size-3.5" />
      </Button>
      {speaker.sample_start != null && (
        // The sample's time is part of the address, so a sample moved by a redaction is
        // never played from the browser's cache.
        <audio
          ref={sample}
          src={mediaUrl(`/api/meetings/${meetingId}/speakers/${speaker.id}/sample?at=${speaker.sample_start}`)}
          preload="none"
        />
      )}
      <div className="w-32 shrink-0">
        <div className="text-sm font-medium">{speaker.label}</div>
        <div className="text-xs text-muted-foreground">
          {speaker.track === "mic" ? "On your mic" : "On the call"} {"\u00b7"} {minutes(speaker.speech_seconds)}
        </div>
      </div>
      {speaker.status === "suggested" && speaker.person_name && (
        <Button size="sm" variant="secondary" disabled={saving} onClick={() => void save(speaker.person_name!)}>
          <Check className="size-3.5" />
          {speaker.person_name}?{" "}
          <span className="text-muted-foreground">{Math.round((speaker.match_score ?? 0) * 100)}%</span>
        </Button>
      )}
      <form
        className="ml-auto flex items-center gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          void save(name);
        }}
      >
        <Input
          list={`people-${speaker.id}`}
          placeholder="Who's this?"
          value={name}
          onChange={(event) => setName(event.target.value)}
          className="h-8 w-44"
        />
        <datalist id={`people-${speaker.id}`}>
          {people.map((person) => (
            <option key={person.id} value={person.name} />
          ))}
        </datalist>
        <Button size="sm" type="submit" disabled={saving || !name.trim()}>
          Save
        </Button>
      </form>
    </div>
  );
}

export function NamePrompt({
  open,
  title,
  description,
  initial = "",
  people,
  onCancel,
  onSubmit,
}: {
  open: boolean;
  title: string;
  description?: string;
  initial?: string;
  people?: Person[];
  onCancel: () => void;
  onSubmit: (value: string) => void;
}) {
  const [value, setValue] = useState(initial);
  useEffect(() => setValue(initial), [initial, open]);
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onCancel()}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          {description && <DialogDescription>{description}</DialogDescription>}
        </DialogHeader>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (value.trim()) onSubmit(value.trim());
          }}
        >
          <Input autoFocus list="name-prompt-people" value={value} onChange={(event) => setValue(event.target.value)} />
          <datalist id="name-prompt-people">
            {people?.map((person) => (
              <option key={person.id} value={person.name} />
            ))}
          </datalist>
          <DialogFooter className="mt-4">
            <Button type="button" variant="outline" onClick={onCancel}>
              Cancel
            </Button>
            <Button type="submit" disabled={!value.trim()}>
              Save
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** Where a line can move: a voice of this meeting (named or not), or a known person. */
type LineTarget = { label: string; speakerId?: number; name?: string };

/** What a redacted line says instead (as the engine writes it). */
export const REDACTED = "[redacted]";

type Suggestion = { term: string; misheard: string | null };

/** Offers, once, to add what a fixed line taught to Words to know. */
function offerWord({ term, misheard }: Suggestion) {
  const add = async () => {
    const added = await attempt("Couldn't add it to Words to know", () =>
      api("/api/vocabulary/correction", { method: "POST", json: { term, misheard } }),
    );
    if (added) toast.success(`"${term}" is in Words to know`, { description: "Used from the next meeting processed." });
  };
  toast("Line fixed", {
    description: misheard
      ? `Write "${term}" whenever it sounds like "${misheard}", in later meetings too?`
      : `Teach ZachCap to spell "${term}" like that in later meetings?`,
    duration: 15_000,
    action: { label: "Add to Words to know", onClick: () => void add() },
  });
}

/** A line's words, being fixed in place. */
function LineEditor({
  segment,
  onSave,
  onCancel,
}: {
  segment: Segment;
  onSave: (segment: Segment, text: string) => Promise<boolean>;
  onCancel: () => void;
}) {
  const [text, setText] = useState(segment.text === REDACTED ? "" : segment.text);
  const [saving, setSaving] = useState(false);
  const save = async () => {
    if (saving || !text.trim()) return;
    setSaving(true);
    if (!(await onSave(segment, text))) setSaving(false);
  };
  return (
    <div className="min-w-0">
      <Textarea
        autoFocus
        aria-label="The line's words"
        value={text}
        rows={Math.min(6, Math.max(2, Math.ceil(text.length / 70)))}
        onChange={(event) => setText(event.target.value)}
        onFocus={(event) => event.currentTarget.setSelectionRange(text.length, text.length)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            void save();
          } else if (event.key === "Escape") {
            event.preventDefault();
            onCancel();
          }
        }}
        className="min-h-0 text-sm leading-relaxed"
      />
      <div className="mt-1 flex items-center gap-2 text-[11px] text-muted-foreground">
        <span className="min-w-0 flex-1">Enter saves, Esc cancels. Reprocessing the audio replaces fixed lines.</span>
        <Button size="sm" variant="ghost" className="h-6 px-2 text-xs" onClick={onCancel}>
          Cancel
        </Button>
        <Button size="sm" className="h-6 px-2 text-xs" disabled={saving || !text.trim()} onClick={() => void save()}>
          Save
        </Button>
      </div>
    </div>
  );
}

/** One transcript line. Memoized: during playback only the line that becomes current
 * re-renders, which keeps two-hour meetings (thousands of lines) smooth. */
const TranscriptRow = memo(function TranscriptRow({
  segment,
  color,
  current,
  editing,
  onSeek,
  onPickSpeaker,
  onLineMenu,
  onEdit,
  onSaveText,
  onCancelEdit,
}: {
  segment: Segment;
  color: SpeakerColor;
  current: boolean;
  editing: boolean;
  onSeek: (seconds: number) => void;
  onPickSpeaker: (segment: Segment, anchor: HTMLElement) => void;
  onLineMenu: (segment: Segment, anchor: HTMLElement) => void;
  onEdit: (segment: Segment) => void;
  onSaveText: (segment: Segment, text: string) => Promise<boolean>;
  onCancelEdit: () => void;
}) {
  const unsure = segment.speaker_status === "suggested" || segment.speaker_status === "unknown";
  const redacted = segment.text === REDACTED;
  return (
    <div
      data-current={current || undefined}
      data-segment={segment.id}
      className={cn(
        "group grid grid-cols-[3.5rem_10rem_1fr_1.5rem] items-start gap-3 rounded-lg px-2 py-1.5 transition-colors",
        current ? "bg-muted" : "hover:bg-muted/40",
      )}
    >
      <button
        onClick={() => onSeek(segment.start)}
        className="select-none pt-0.5 text-left font-mono text-xs tabular-nums text-muted-foreground hover:text-foreground"
        title="Play from here"
      >
        {clock(segment.start)}
      </button>
      <button
        onClick={(event) => onPickSpeaker(segment, event.currentTarget)}
        className="flex min-w-0 select-none items-start gap-2 rounded-md pt-0.5 text-left hover:underline"
        title="Change who said this"
        aria-haspopup="menu"
      >
        <span className={cn("mt-1.5 size-2 shrink-0 rounded-full", color.dot)} />
        <span className={cn("truncate text-sm font-medium", unsure && "text-muted-foreground")}>{segment.speaker}</span>
      </button>
      {editing ? (
        <LineEditor segment={segment} onSave={onSaveText} onCancel={onCancelEdit} />
      ) : (
        <p
          className={cn("text-sm leading-relaxed", redacted && "italic text-muted-foreground")}
          onDoubleClick={() => onEdit(segment)}
        >
          {redacted ? "Redacted" : segment.text}
          {segment.edited ? (
            <span className="ml-1.5 align-baseline text-[10px] text-muted-foreground" title="You fixed these words">
              edited
            </span>
          ) : null}
        </p>
      )}
      <button
        onClick={(event) => onLineMenu(segment, event.currentTarget)}
        className="mt-0.5 flex size-6 items-center justify-center rounded-md text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100 hover:bg-muted hover:text-foreground focus-visible:opacity-100"
        title="Fix the words or redact this moment"
        aria-label="More for this line"
        aria-haspopup="menu"
      >
        <MoreHorizontal className="size-3.5" />
      </button>
    </div>
  );
});

/** Where the shared menu is open: for a line's speaker, or for the line itself. */
type MenuAt = { segment: Segment; x: number; y: number; kind: "speaker" | "line" };
const LINE_MENU_WIDTH = 224; // w-56

export const Transcript = memo(function Transcript({
  segments,
  speakers,
  people,
  colorOf,
  currentId,
  playing,
  focusAt,
  solo,
  onSeek,
  onChanged,
  onRedact,
}: {
  segments: Segment[];
  speakers: Speaker[];
  people: Person[];
  colorOf: (name: string) => SpeakerColor;
  currentId: number | undefined;
  playing: boolean;
  /** A cited moment to show when the page opens: that line is scrolled to and marked. */
  focusAt?: number;
  solo: string | null;
  onSeek: (seconds: number) => void;
  onChanged: () => void;
  /** Opens the redaction dialog for the moment this line was said. */
  onRedact: (segment: Segment) => void;
}) {
  const [filter, setFilter] = useState("");
  const [moving, setMoving] = useState<Segment | null>(null);
  const [editingId, setEditingId] = useState<number | null>(null);
  // One menu for every line, opened next to what was clicked: the name (who said it) or
  // the line's own button (fix the words, redact).
  const [menu, setMenu] = useState<MenuAt | null>(null);
  // What opened the menu gets focus back when it closes (keyboard users), unless the
  // choice opened something that takes focus itself.
  const anchor = useRef<HTMLElement | null>(null);
  const refocus = useRef(true);
  const menuLabel = useId();
  const list = useRef<HTMLDivElement>(null);

  const shown = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    return segments.filter(
      (s) =>
        (!needle || s.text.toLowerCase().includes(needle) || s.speaker.toLowerCase().includes(needle)) &&
        (!solo || s.speaker === solo),
    );
  }, [segments, filter, solo]);

  useEffect(() => {
    if (!playing) return;
    list.current?.querySelector("[data-current]")?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [currentId, playing]);

  // The cited line stays marked until playback takes over the highlight.
  const [focusId, setFocusId] = useState<number | null>(null);
  const focused = useRef(false);
  useEffect(() => {
    if (focusAt == null || focused.current || segments.length === 0) return;
    focused.current = true;
    setFocusId((segments.findLast((s) => s.start <= focusAt + 0.05) ?? segments[0]).id);
  }, [focusAt, segments]);
  useEffect(() => {
    if (focusId == null) return;
    list.current?.querySelector(`[data-segment="${focusId}"]`)?.scrollIntoView({ block: "center" });
  }, [focusId]);
  useEffect(() => {
    if (playing) setFocusId(null);
  }, [playing]);

  const openMenu = useCallback((segment: Segment, from: HTMLElement, kind: MenuAt["kind"]) => {
    anchor.current = from;
    refocus.current = true;
    const box = from.getBoundingClientRect();
    const parent = list.current?.getBoundingClientRect();
    // The line menu opens leftward from its button at the row's end.
    const x = (kind === "line" ? box.right - LINE_MENU_WIDTH : box.left) - (parent?.left ?? 0);
    setMenu({ segment, x, y: box.bottom - (parent?.top ?? 0), kind });
  }, []);
  const pickSpeaker = useCallback((segment: Segment, from: HTMLElement) => openMenu(segment, from, "speaker"), [openMenu]);
  const lineMenu = useCallback((segment: Segment, from: HTMLElement) => openMenu(segment, from, "line"), [openMenu]);

  const editLine = useCallback((segment: Segment) => setEditingId(segment.id), []);
  const cancelEdit = useCallback(() => setEditingId(null), []);
  const saveText = useCallback(
    async (segment: Segment, text: string) => {
      const value = text.split(/\s+/).join(" ").trim();
      if (!value) return false;
      if (value === segment.text) {
        setEditingId(null);
        return true;
      }
      try {
        const { suggestion } = await api<{ text: string; suggestion: Suggestion | null }>(`/api/segments/${segment.id}`, {
          method: "PATCH",
          json: { text: value },
        });
        setEditingId(null);
        onChanged();
        if (suggestion) offerWord(suggestion);
        else toast.success("Line fixed");
        return true;
      } catch (error) {
        toast.error("Couldn't save the line", { description: String(error) });
        return false;
      }
    },
    [onChanged],
  );

  const reassign = async (segment: Segment, target: LineTarget) => {
    try {
      await api(`/api/segments/${segment.id}/speaker`, {
        method: "POST",
        json: target.speakerId != null ? { speaker_id: target.speakerId } : { name: target.name },
      });
      toast.success(`Line moved to ${target.label}`);
      onChanged();
    } catch (error) {
      toast.error("Couldn't move the line", { description: String(error) });
    }
  };

  // Voices of this meeting by id, so picking "Speaker 2" never invents a person called
  // that; then everyone else ZachCap knows, by name.
  const voices = useMemo(() => {
    const seen = new Set<string>();
    return speakers.flatMap((s) => {
      if (seen.has(s.display_name)) return [];
      seen.add(s.display_name);
      return [{ label: s.display_name, speakerId: s.id, personId: s.person_id }];
    });
  }, [speakers]);
  const others = useMemo(() => {
    const present = new Set(speakers.filter((s) => s.status === "auto" || s.status === "confirmed").map((s) => s.person_id));
    return people.filter((p) => !present.has(p.id)).map((p) => ({ label: p.name, name: p.name }));
  }, [speakers, people]);

  return (
    <div>
      <div className="relative mb-3">
        <Search className="pointer-events-none absolute top-2.5 left-2.5 size-4 text-muted-foreground" />
        <Input
          placeholder="Filter the transcript"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
          className="h-9 pl-8"
        />
      </div>
      {shown.length === 0 && (
        <p className="py-6 text-center text-sm text-muted-foreground">
          {segments.length === 0 ? "No speech in this recording yet." : "Nothing matches."}
        </p>
      )}
      <div ref={list} className="relative space-y-0.5">
        {shown.map((segment) => (
          <TranscriptRow
            key={segment.id}
            segment={segment}
            color={colorOf(segment.speaker)}
            current={(currentId === segment.id && playing) || segment.id === focusId}
            editing={segment.id === editingId}
            onSeek={onSeek}
            onPickSpeaker={pickSpeaker}
            onLineMenu={lineMenu}
            onEdit={editLine}
            onSaveText={saveText}
            onCancelEdit={cancelEdit}
          />
        ))}
        <DropdownMenu open={menu !== null} onOpenChange={(open) => !open && setMenu(null)}>
          <DropdownMenuTrigger asChild>
            <span
              aria-hidden
              className="pointer-events-none absolute size-0"
              style={{ left: menu?.x ?? 0, top: menu?.y ?? 0 }}
            />
          </DropdownMenuTrigger>
          {menu?.kind === "line" && (
            <DropdownMenuContent
              align="start"
              className="w-56"
              aria-labelledby={menuLabel}
              onCloseAutoFocus={(event) => {
                event.preventDefault();
                if (refocus.current) anchor.current?.focus({ preventScroll: true });
              }}
            >
              <DropdownMenuLabel id={menuLabel} className="text-xs text-muted-foreground">
                This line
              </DropdownMenuLabel>
              <DropdownMenuItem
                onSelect={() => {
                  refocus.current = false; // the editor takes focus
                  setEditingId(menu.segment.id);
                }}
              >
                <Pencil className="size-4" />
                Fix the words
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem
                variant="destructive"
                onSelect={() => {
                  refocus.current = false; // the dialog takes focus
                  onRedact(menu.segment);
                }}
              >
                <EyeOff className="size-4" />
                Redact this moment...
              </DropdownMenuItem>
            </DropdownMenuContent>
          )}
          {menu?.kind === "speaker" && (
            <DropdownMenuContent
              align="start"
              className="w-56"
              aria-labelledby={menuLabel}
              onCloseAutoFocus={(event) => {
                // Radix would focus the hidden anchor, which can't take focus.
                event.preventDefault();
                if (refocus.current) anchor.current?.focus({ preventScroll: true });
              }}
            >
              <DropdownMenuLabel id={menuLabel} className="text-xs text-muted-foreground">
                Who said this?
              </DropdownMenuLabel>
              {voices
                .filter((voice) => voice.speakerId !== menu.segment.meeting_speaker_id && voice.label !== menu.segment.speaker)
                .map((voice) => (
                  <DropdownMenuItem key={`voice-${voice.speakerId}`} onSelect={() => void reassign(menu.segment, voice)}>
                    <span className={cn("size-2 rounded-full", colorOf(voice.label).dot)} />
                    {voice.label}
                  </DropdownMenuItem>
                ))}
              {others.length > 0 && (
                <>
                  <DropdownMenuSeparator />
                  <DropdownMenuLabel className="text-xs text-muted-foreground">Someone you know</DropdownMenuLabel>
                  {others.slice(0, 8).map((person) => (
                    <DropdownMenuItem key={`person-${person.name}`} onSelect={() => void reassign(menu.segment, person)}>
                      <span className={cn("size-2 rounded-full", colorOf(person.label).dot)} />
                      {person.label}
                    </DropdownMenuItem>
                  ))}
                </>
              )}
              <DropdownMenuSeparator />
              <DropdownMenuItem onSelect={() => setMoving(menu.segment)}>
                <UserRoundSearch className="size-4" />
                Someone else...
              </DropdownMenuItem>
            </DropdownMenuContent>
          )}
        </DropdownMenu>
      </div>
      <NamePrompt
        open={moving !== null}
        title="Who said this?"
        description="Only this line moves. Name the whole speaker from the Speakers tab instead to teach ZachCap their voice."
        people={people}
        onCancel={() => setMoving(null)}
        onSubmit={(name) => {
          if (moving) void reassign(moving, { label: name, name });
          setMoving(null);
        }}
      />
    </div>
  );
});
