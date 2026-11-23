import { Bookmark, Headphones } from "lucide-react";
import { memo, useMemo, type ReactNode } from "react";

import type { Note, Segment, TalkTime } from "@/lib/engine";
import { clock, initials, minutes, type SpeakerColor } from "@/lib/format";
import { cn } from "@/lib/utils";

const NO_SEGMENTS: Segment[] = [];
const NO_NOTES: Note[] = [];

export function PersonAvatar({
  name,
  color,
  size = "md",
  className,
}: {
  name: string;
  color: SpeakerColor;
  size?: "sm" | "md" | "lg";
  className?: string;
}) {
  const sizes = { sm: "size-6 text-[10px]", md: "size-8 text-xs", lg: "size-11 text-sm" };
  return (
    <div
      title={name}
      className={cn(
        "flex shrink-0 select-none items-center justify-center rounded-full font-semibold ring-2 ring-background",
        color.soft,
        color.text,
        sizes[size],
        className,
      )}
    >
      {initials(name) || "?"}
    </div>
  );
}

export function AvatarStack({ names, colorOf, max = 4 }: { names: string[]; colorOf: (n: string) => SpeakerColor; max?: number }) {
  const shown = names.slice(0, max);
  return (
    <div className="flex -space-x-2">
      {shown.map((name) => (
        <PersonAvatar key={name} name={name} color={colorOf(name)} size="sm" />
      ))}
      {names.length > max && (
        <div className="flex size-6 items-center justify-center rounded-full bg-muted text-[10px] font-medium text-muted-foreground ring-2 ring-background">
          +{names.length - max}
        </div>
      )}
    </div>
  );
}

export function StatCard({ label, value, hint, icon }: { label: string; value: ReactNode; hint?: string; icon?: ReactNode }) {
  return (
    <div className="rounded-xl border bg-card px-4 py-3.5">
      <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
        {icon}
        {label}
      </div>
      <div className="mt-1 font-heading text-2xl font-semibold tracking-tight tabular-nums">{value}</div>
      {hint && <div className="mt-0.5 text-xs text-muted-foreground">{hint}</div>}
    </div>
  );
}

/** The bars of one lane. Memoized, so playback only moves the playhead. */
const LaneBars = memo(function LaneBars({ segments, dot, length }: { segments: Segment[]; dot: string; length: number }) {
  return (
    <>
      {segments.map((s) => (
        <div
          key={s.id}
          className={cn("absolute top-0.5 bottom-0.5 rounded-sm", dot)}
          style={{
            left: `${(s.start / length) * 100}%`,
            width: `max(2px, ${((s.end - s.start) / length) * 100}%)`,
          }}
        />
      ))}
    </>
  );
});

/** One lane per speaker: when each person talked, with a playhead and solo buttons. */
export function SpeakerLanes({
  segments,
  talk,
  duration,
  colorOf,
  time,
  solo,
  notes = NO_NOTES,
  onSeek,
  onSolo,
}: {
  segments: Segment[];
  talk: TalkTime[];
  duration: number;
  colorOf: (name: string) => SpeakerColor;
  time: number;
  solo: string | null;
  notes?: Note[];
  onSeek: (seconds: number) => void;
  onSolo: (name: string | null) => void;
}) {
  const total = talk.reduce((sum, t) => sum + t.seconds, 0) || 1;
  const lastEnd = useMemo(() => segments.reduce((end, s) => Math.max(end, s.end), 0), [segments]);
  const length = Math.max(duration, lastEnd, 1);
  const ticks = Array.from({ length: 5 }, (_, i) => (length * i) / 4);
  const bySpeaker = useMemo(() => {
    const lanes = new Map<string, Segment[]>();
    for (const s of segments) {
      const lane = lanes.get(s.speaker);
      if (lane) lane.push(s);
      else lanes.set(s.speaker, [s]);
    }
    return lanes;
  }, [segments]);

  return (
    <div className="select-none rounded-xl border bg-card p-4">
      <div className="space-y-2">
        {notes.length > 0 && (
          <div className="flex items-center gap-3">
            <div className="flex w-40 shrink-0 items-center gap-2 px-1.5 text-xs text-muted-foreground">
              <Bookmark className="size-3" />
              Your notes
            </div>
            <div className="relative h-4 flex-1">
              {notes.map((note) => (
                <button
                  key={note.id}
                  onClick={() => onSeek(note.at)}
                  title={`${clock(note.at)} ${note.text || "Bookmarked moment"}`}
                  aria-label={`Note at ${clock(note.at)}`}
                  className="absolute top-0 -translate-x-1/2"
                  style={{ left: `${(note.at / length) * 100}%` }}
                >
                  <Bookmark
                    className={cn(
                      "size-3.5",
                      note.kind === "bookmark" ? "fill-amber-400 text-amber-500" : "fill-muted text-muted-foreground",
                    )}
                  />
                </button>
              ))}
            </div>
          </div>
        )}
        {talk.map((t) => {
          const color = colorOf(t.speaker);
          const active = solo === t.speaker;
          return (
            <div key={t.speaker} className="flex items-center gap-3">
              <button
                onClick={() => onSolo(active ? null : t.speaker)}
                title={active ? "Play everyone" : `Play only ${t.speaker}`}
                className={cn(
                  "flex w-40 shrink-0 items-center gap-2 rounded-md px-1.5 py-1 text-left text-xs transition-colors",
                  active ? "bg-foreground text-background" : "hover:bg-muted",
                  solo && !active && "opacity-50",
                )}
              >
                <span className={cn("size-2 shrink-0 rounded-full", color.dot)} />
                <span className="truncate font-medium">{t.speaker}</span>
                <span className={cn("ml-auto tabular-nums", active ? "text-background/70" : "text-muted-foreground")}>
                  {Math.round((t.seconds / total) * 100)}%
                </span>
                <Headphones className={cn("size-3 shrink-0", active ? "" : "opacity-0")} />
              </button>
              <div
                className={cn("relative h-5 flex-1 cursor-pointer rounded-md bg-muted/60", solo && !active && "opacity-40")}
                onClick={(event) => {
                  const box = event.currentTarget.getBoundingClientRect();
                  onSeek(((event.clientX - box.left) / box.width) * length);
                }}
              >
                <LaneBars segments={bySpeaker.get(t.speaker) ?? NO_SEGMENTS} dot={color.dot} length={length} />
                <div
                  className="pointer-events-none absolute top-[-3px] bottom-[-3px] w-px bg-foreground"
                  style={{ left: `${(time / length) * 100}%` }}
                />
              </div>
            </div>
          );
        })}
      </div>
      <div className="mt-2 flex justify-between pl-43 font-mono text-[10px] text-muted-foreground tabular-nums">
        {ticks.map((tick) => (
          <span key={tick}>{clock(tick)}</span>
        ))}
      </div>
    </div>
  );
}

export function TalkTimeBars({ talk, colorOf }: { talk: TalkTime[]; colorOf: (name: string) => SpeakerColor }) {
  const total = talk.reduce((sum, t) => sum + t.seconds, 0) || 1;
  const top = Math.max(...talk.map((t) => t.seconds), 1);
  return (
    <div className="space-y-3">
      {talk.map((t) => {
        const color = colorOf(t.speaker);
        return (
          <div key={t.speaker}>
            <div className="mb-1 flex items-baseline justify-between text-sm">
              <span className="flex items-center gap-2 font-medium">
                <span className={cn("size-2 rounded-full", color.dot)} />
                {t.speaker}
              </span>
              <span className="text-xs text-muted-foreground tabular-nums">
                {minutes(t.seconds)} {"\u00b7"} {Math.round((t.seconds / total) * 100)}% {"\u00b7"} {t.turns} turn
                {t.turns === 1 ? "" : "s"}
              </span>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-muted">
              <div className={cn("h-full rounded-full", color.dot)} style={{ width: `${(t.seconds / top) * 100}%` }} />
            </div>
          </div>
        );
      })}
    </div>
  );
}
