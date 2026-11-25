import { AtSign, Bookmark, Camera, Headphones } from "lucide-react";
import { memo, useMemo, type ReactNode } from "react";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { Insights, Mention, Note, Segment, TalkTime } from "@/lib/engine";
import { clock, initials, minutes, type SpeakerColor } from "@/lib/format";
import { cn } from "@/lib/utils";

const NO_SEGMENTS: Segment[] = [];
const NO_NOTES: Note[] = [];
const NO_MENTIONS: Mention[] = [];

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
  mentions = NO_MENTIONS,
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
  /** Where someone said your name: markers on their own row. */
  mentions?: Mention[];
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

  const moments = useMemo(() => notes.filter((note) => note.kind !== "file"), [notes]);

  return (
    <div className="select-none rounded-xl border bg-card p-4">
      <div className="space-y-2">
        {moments.length > 0 && (
          <div className="flex items-center gap-3">
            <div className="flex w-40 shrink-0 items-center gap-2 px-1.5 text-xs text-muted-foreground">
              <Bookmark className="size-3" />
              Your notes
            </div>
            <div className="relative h-4 flex-1">
              {moments.map((note) => (
                <button
                  key={note.id}
                  onClick={() => onSeek(note.at)}
                  title={`${clock(note.at)} ${
                    note.kind === "snapshot"
                      ? `Snapshot${note.text ? `: ${note.text.split("\n")[0]}` : ""}`
                      : note.text || "Bookmarked moment"
                  }`}
                  aria-label={`${note.kind === "snapshot" ? "Snapshot" : "Note"} at ${clock(note.at)}`}
                  className="absolute top-0 -translate-x-1/2"
                  style={{ left: `${(note.at / length) * 100}%` }}
                >
                  {note.kind === "snapshot" ? (
                    <Camera className="size-3.5 text-sky-600 dark:text-sky-400" />
                  ) : (
                    <Bookmark
                      className={cn(
                        "size-3.5",
                        note.kind === "bookmark" ? "fill-amber-400 text-amber-500" : "fill-muted text-muted-foreground",
                      )}
                    />
                  )}
                </button>
              ))}
            </div>
          </div>
        )}
        {mentions.length > 0 && (
          <div className="flex items-center gap-3">
            <div className="flex w-40 shrink-0 items-center gap-2 px-1.5 text-xs text-muted-foreground">
              <AtSign className="size-3" />
              Said your name
            </div>
            <div className="relative h-4 flex-1">
              {mentions.map((mention) => (
                <button
                  key={mention.segment_id}
                  onClick={() => onSeek(mention.at)}
                  title={`${clock(mention.at)} ${mention.speaker} said your name: ${mention.snippet}`}
                  aria-label={`${mention.speaker} said your name at ${clock(mention.at)}`}
                  className="absolute top-0 -translate-x-1/2 rounded-full text-sky-600 hover:text-sky-500 dark:text-sky-400"
                  style={{ left: `${(mention.at / length) * 100}%` }}
                >
                  <AtSign className="size-3.5" strokeWidth={2.5} />
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

/** A word that says what it means when it's pointed at, or reached with Tab. */
function Explained({ label, children }: { label: string; children: ReactNode }) {
  return (
    <Tooltip>
      <TooltipTrigger className="cursor-help rounded-sm underline decoration-muted-foreground/50 decoration-dotted underline-offset-4 outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/50">
        {label}
      </TooltipTrigger>
      <TooltipContent sideOffset={6} className="max-w-64 text-left font-normal text-pretty">
        {children}
      </TooltipContent>
    </Tooltip>
  );
}

/** What the conversation's numbers mean (insights.py works them out). */
const MEANING = {
  pace: "How fast they talk: words per minute, counting only the time they were talking. A dash means too little talk to tell, under 20 seconds.",
  longest: "The longest they talked in one go: nobody else in between, and no pause over two seconds.",
  questions: "How many of their lines end with a question mark.",
  talkedOver:
    "How often they started while someone on the other side of the call was still talking. It can be told between your mic and the call, not between two people on the call.",
  longestStretch: "The longest anyone talked in one go. Click to play it from its start.",
  questionsAsked: "Lines that end with a question mark, by everyone listed here.",
};

/** How the conversation went: who held the floor longest, pace, questions, interruptions. */
export function ConversationStats({
  insights,
  colorOf,
  onSeek,
}: {
  insights: Insights;
  colorOf: (name: string) => SpeakerColor;
  onSeek: (seconds: number) => void;
}) {
  if (!insights.speakers.length) return null;
  const { longest } = insights;
  return (
    <div>
      <div className="mb-3 flex flex-wrap gap-2 text-xs">
        {longest && (
          <Tooltip>
            <TooltipTrigger
              onClick={() => onSeek(longest.at)}
              className="rounded-lg border px-2.5 py-1.5 text-left outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring/50"
            >
              <span className="text-muted-foreground">Longest stretch </span>
              <span className="font-medium">
                {longest.speaker}, {minutes(longest.seconds)}
              </span>
              <span className="ml-1 font-mono text-muted-foreground">at {clock(longest.at)}</span>
            </TooltipTrigger>
            <TooltipContent sideOffset={6} className="max-w-64 text-pretty">
              {MEANING.longestStretch}
            </TooltipContent>
          </Tooltip>
        )}
        <span className="rounded-lg border px-2.5 py-1.5 text-muted-foreground">
          <Explained label="Questions asked">{MEANING.questionsAsked}</Explained>{" "}
          <span className="font-medium text-foreground tabular-nums">{insights.questions}</span>
        </span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs whitespace-nowrap text-muted-foreground">
              <th className="w-full pb-2 text-left font-normal">Speaker</th>
              {(
                [
                  ["Pace", MEANING.pace],
                  ["Longest", MEANING.longest],
                  ["Questions", MEANING.questions],
                  ["Talked over", MEANING.talkedOver],
                ] as const
              ).map(([label, meaning]) => (
                <th key={label} className="pb-2 pl-10 text-right font-normal">
                  <Explained label={label}>{meaning}</Explained>
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="tabular-nums">
            {insights.speakers.map((s) => (
              <tr key={s.speaker} className="border-t">
                <td className="max-w-0 py-2">
                  <span className="flex items-center gap-2">
                    <span className={cn("size-2 shrink-0 rounded-full", colorOf(s.speaker).dot)} />
                    <span className="truncate">{s.speaker}</span>
                  </span>
                </td>
                <td className="py-2 pl-10 text-right whitespace-nowrap">
                  {s.words_per_minute ? `${s.words_per_minute} wpm` : "-"}
                </td>
                <td className="py-2 pl-10 text-right whitespace-nowrap">{minutes(s.longest)}</td>
                <td className="py-2 pl-10 text-right">{s.questions}</td>
                <td className="py-2 pl-10 text-right">{s.interruptions}</td>
              </tr>
            ))}
          </tbody>
        </table>
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
