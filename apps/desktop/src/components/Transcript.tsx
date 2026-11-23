import { Check, Play, Search, UserRoundSearch } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
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
import { api, mediaUrl, type Person, type Segment, type Speaker } from "@/lib/engine";
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
        onClick={() => {
          if (!sample.current) return;
          sample.current.currentTime = 0;
          void sample.current.play();
        }}
        aria-label={`Play a sample of ${speaker.label}`}
      >
        <Play className="size-3.5" />
      </Button>
      <audio ref={sample} src={mediaUrl(`/api/meetings/${meetingId}/speakers/${speaker.id}/sample`)} preload="none" />
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

export function Transcript({
  segments,
  speakers,
  people,
  colorOf,
  currentId,
  playing,
  solo,
  onSeek,
  onChanged,
}: {
  segments: Segment[];
  speakers: Speaker[];
  people: Person[];
  colorOf: (name: string) => SpeakerColor;
  currentId: number | undefined;
  playing: boolean;
  solo: string | null;
  onSeek: (seconds: number) => void;
  onChanged: () => void;
}) {
  const [filter, setFilter] = useState("");
  const [moving, setMoving] = useState<Segment | null>(null);
  const currentRow = useRef<HTMLDivElement>(null);

  const shown = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    return segments.filter(
      (s) =>
        (!needle || s.text.toLowerCase().includes(needle) || s.speaker.toLowerCase().includes(needle)) &&
        (!solo || s.speaker === solo),
    );
  }, [segments, filter, solo]);

  useEffect(() => {
    if (playing) currentRow.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [currentId, playing]);

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
      <div className="space-y-0.5">
        {shown.map((segment) => {
          const color = colorOf(segment.speaker);
          const current = currentId === segment.id && playing;
          const unsure = segment.speaker_status === "suggested" || segment.speaker_status === "unknown";
          return (
            <div
              key={segment.id}
              ref={current ? currentRow : undefined}
              className={cn(
                "group grid grid-cols-[3.5rem_10rem_1fr] items-start gap-3 rounded-lg px-2 py-1.5 transition-colors",
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
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <button
                    className="flex min-w-0 select-none items-start gap-2 rounded-md pt-0.5 text-left hover:underline"
                    title="Change who said this"
                  >
                    <span className={cn("mt-1.5 size-2 shrink-0 rounded-full", color.dot)} />
                    <span className={cn("truncate text-sm font-medium", unsure && "text-muted-foreground")}>
                      {segment.speaker}
                    </span>
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="start" className="w-56">
                  <DropdownMenuLabel className="text-xs text-muted-foreground">Who said this?</DropdownMenuLabel>
                  {voices
                    .filter((voice) => voice.speakerId !== segment.meeting_speaker_id && voice.label !== segment.speaker)
                    .map((voice) => (
                      <DropdownMenuItem key={`voice-${voice.speakerId}`} onSelect={() => void reassign(segment, voice)}>
                        <span className={cn("size-2 rounded-full", colorOf(voice.label).dot)} />
                        {voice.label}
                      </DropdownMenuItem>
                    ))}
                  {others.length > 0 && (
                    <>
                      <DropdownMenuSeparator />
                      <DropdownMenuLabel className="text-xs text-muted-foreground">Someone you know</DropdownMenuLabel>
                      {others.slice(0, 8).map((person) => (
                        <DropdownMenuItem key={`person-${person.name}`} onSelect={() => void reassign(segment, person)}>
                          <span className={cn("size-2 rounded-full", colorOf(person.label).dot)} />
                          {person.label}
                        </DropdownMenuItem>
                      ))}
                    </>
                  )}
                  <DropdownMenuSeparator />
                  <DropdownMenuItem onSelect={() => setMoving(segment)}>
                    <UserRoundSearch className="size-4" />
                    Someone else...
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
              <p className="text-sm leading-relaxed">{segment.text}</p>
            </div>
          );
        })}
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
}
