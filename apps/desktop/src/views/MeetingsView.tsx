import {
  AtSign,
  AudioLines,
  CalendarDays,
  ChevronRight,
  Circle,
  CircleCheck,
  Clock3,
  ListChecks,
  Loader2,
  Users,
} from "lucide-react";

import { toggleAction } from "@/components/SummaryPanel";
import { AvatarStack, StatCard } from "@/components/Visuals";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { usePoll } from "@/hooks/usePoll";
import { api, type ActionItem, type KeyStatus, type Meeting, type Settings, type Stats } from "@/lib/engine";
import { STATUS_LABEL, day, dayGroup, hours, minutes, personColor, when } from "@/lib/format";
import { cn } from "@/lib/utils";

const GROUPS = ["Today", "Yesterday", "This week", "This month", "Earlier"];

export function StatusBadge({ meeting }: { meeting: Meeting }) {
  if (meeting.status === "processing" || meeting.progress === "queued") {
    return (
      <Badge variant="secondary" className="gap-1">
        <Loader2 className="size-3 animate-spin" />
        Processing
      </Badge>
    );
  }
  if (meeting.status === "needs_names") {
    return <Badge className="bg-amber-500/15 text-amber-700 dark:text-amber-300">Needs names</Badge>;
  }
  if (meeting.status === "failed") return <Badge variant="destructive">Failed</Badge>;
  return <Badge variant="outline">{STATUS_LABEL[meeting.status]}</Badge>;
}

function SetupChecklist({ keys, hasMeetings, onSettings, onImport }: { keys: KeyStatus; hasMeetings: boolean; onSettings: () => void; onImport: () => void }) {
  const steps = [
    { done: keys.huggingface !== "not set", label: "Add a Hugging Face token", hint: "Tells speakers apart. Free; accept the model's terms first.", action: onSettings },
    { done: keys.anthropic !== "not set", label: "Add your Anthropic API key", hint: "Summaries, action items and Ask. A few cents per meeting.", action: onSettings },
    { done: hasMeetings, label: "Record or import your first meeting", hint: "Press Record (Ctrl+Alt+R) on a call, or import a Teams recording.", action: onImport },
  ];
  if (steps.every((s) => s.done)) return null;
  return (
    <div className="mt-6 rounded-xl border bg-card p-4">
      <div className="text-sm font-medium">Get set up</div>
      <div className="mt-3 space-y-2">
        {steps.map((step) => (
          <button key={step.label} onClick={step.action} className="flex w-full items-start gap-3 rounded-lg p-1.5 text-left hover:bg-muted/50">
            {step.done ? (
              <CircleCheck className="mt-0.5 size-4 shrink-0 text-emerald-600" />
            ) : (
              <Circle className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
            )}
            <div>
              <div className={step.done ? "text-sm text-muted-foreground line-through" : "text-sm font-medium"}>{step.label}</div>
              {!step.done && <div className="text-xs text-muted-foreground">{step.hint}</div>}
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}

/** Recorded meeting time per day, last two weeks, as bars. */
function MeetingTime({ days }: { days: { day: string; seconds: number }[] }) {
  const top = Math.max(...days.map((d) => d.seconds), 1);
  const total = days.reduce((sum, d) => sum + d.seconds, 0);
  return (
    <div className="rounded-xl border bg-card p-4">
      <div className="flex items-baseline justify-between">
        <div className="text-sm font-medium">Meeting time</div>
        <div className="text-xs text-muted-foreground">{hours(total)} in the last two weeks</div>
      </div>
      <div className="mt-4 flex h-24 items-end gap-1.5">
        {days.map((d) => {
          const date = new Date(`${d.day}T12:00:00`);
          const weekend = date.getDay() === 0 || date.getDay() === 6;
          return (
            <div key={d.day} className="flex h-full flex-1 flex-col justify-end" title={`${day(`${d.day}T12:00:00`)}: ${minutes(d.seconds) || "no meetings"}`}>
              <div
                className={cn("w-full rounded-sm", d.seconds ? "bg-foreground/80" : weekend ? "bg-muted/50" : "bg-muted")}
                style={{ height: d.seconds ? `${Math.max(6, (d.seconds / top) * 100)}%` : "4px" }}
              />
            </div>
          );
        })}
      </div>
      <div className="mt-1.5 flex justify-between text-[10px] text-muted-foreground">
        <span>{day(`${days[0].day}T12:00:00`)}</span>
        <span>Today</span>
      </div>
    </div>
  );
}

/** My open action items and commitments, oldest meeting first, with a link to the moment. */
function MyOpenItems({ me, onOpen }: { me: string; onOpen: (id: number, at?: number) => void }) {
  const { data, refresh } = usePoll(() => api<{ me: string; items: ActionItem[] }>("/api/actions?status=open"), 10_000);
  const mine = (data?.items ?? []).filter((item) => item.owner === me).slice(0, 5);
  return (
    <div className="rounded-xl border bg-card p-4">
      <div className="flex items-baseline justify-between">
        <div className="text-sm font-medium">Your open items</div>
        <div className="text-xs text-muted-foreground">{mine.length ? `${mine.length} shown` : ""}</div>
      </div>
      {mine.length === 0 ? (
        <p className="mt-6 text-center text-xs text-muted-foreground">Nothing open with your name on it.</p>
      ) : (
        <div className="mt-2 space-y-1">
          {mine.map((item) => (
            <div key={item.id} className="flex items-start gap-2 rounded-md p-1 hover:bg-muted/50">
              <button
                role="checkbox"
                aria-checked={false}
                aria-label={item.task}
                title="Mark as done"
                onClick={async () => {
                  await toggleAction(item);
                  await refresh();
                }}
                className="mt-0.5 size-3.5 shrink-0 rounded-[4px] border border-muted-foreground/40 hover:border-foreground"
              />
              <button onClick={() => onOpen(item.meeting_id, item.said_at ?? undefined)} className="min-w-0 flex-1 text-left">
                <div className="truncate text-xs">{item.task}</div>
                <div className="truncate text-[10px] text-muted-foreground">
                  {item.meeting_title}
                  {item.due ? ` \u00b7 due ${item.due}` : ""}
                </div>
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function MeetingsView({
  onOpen,
  onSettings,
  onImport,
}: {
  onOpen: (id: number, at?: number) => void;
  onSettings: () => void;
  onImport: () => void;
}) {
  const { data: meetings } = usePoll(() => api<Meeting[]>("/api/meetings"), 2000);
  const { data: stats } = usePoll(() => api<Stats>("/api/stats"), 5000);
  const { data: keys } = usePoll(() => api<KeyStatus>("/api/keys"), 10_000);
  const { data: settings } = usePoll(() => api<Settings>("/api/settings"), 0);
  const me = settings?.me.name ?? "You";

  return (
    <ScrollArea className="min-h-0 flex-1">
      <div className="mx-auto max-w-4xl px-8 py-8">
        <h1 className="font-heading text-2xl font-semibold tracking-tight">Meetings</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Recorded on this computer. Audio never leaves it; only transcript text is sent for summaries.
        </p>

        <div className="mt-6 grid grid-cols-2 gap-3 md:grid-cols-4">
          <StatCard
            icon={<CalendarDays className="size-3.5" />}
            label="This week"
            value={stats ? stats.week : "-"}
            hint={stats ? `${hours(stats.week_seconds)} recorded` : undefined}
          />
          <StatCard
            icon={<Clock3 className="size-3.5" />}
            label="All time"
            value={stats ? hours(stats.seconds) : "-"}
            hint={stats ? `${stats.meetings} meeting${stats.meetings === 1 ? "" : "s"}` : undefined}
          />
          <StatCard
            icon={<ListChecks className="size-3.5" />}
            label="Open action items"
            value={stats ? stats.open_actions : "-"}
            hint="across all meetings"
          />
          <StatCard
            icon={<Users className="size-3.5" />}
            label="People recognized"
            value={stats ? stats.people : "-"}
            hint="by voice"
          />
        </div>

        {stats && meetings && meetings.length > 0 && (
          <div className="mt-3 grid gap-3 md:grid-cols-5">
            <div className="md:col-span-3">
              <MeetingTime days={stats.by_day} />
            </div>
            <div className="md:col-span-2">
              <MyOpenItems me={me} onOpen={onOpen} />
            </div>
          </div>
        )}

        {keys && meetings && (
          <SetupChecklist keys={keys} hasMeetings={meetings.length > 0} onSettings={onSettings} onImport={onImport} />
        )}

        {!meetings && (
          <div className="mt-8 space-y-2">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-16 w-full" />
            ))}
          </div>
        )}

        {meetings?.length === 0 && (
          <div className="mt-16 flex flex-col items-center text-center">
            <div className="flex size-14 items-center justify-center rounded-2xl bg-foreground text-background">
              <AudioLines className="size-6" />
            </div>
            <p className="mt-5 font-medium">No meetings yet</p>
            <p className="mt-1 max-w-sm text-sm text-muted-foreground">
              Press Record when your call starts (or Ctrl+Alt+R from anywhere), or import a recording you already
              have. No bots, no calendar.
            </p>
          </div>
        )}

        {GROUPS.map((group) => {
          const items = meetings?.filter((m) => dayGroup(m.started_at) === group) ?? [];
          if (!items.length) return null;
          return (
            <section key={group} className="mt-8">
              <h2 className="mb-2 text-xs font-medium text-muted-foreground">{group}</h2>
              <div className="divide-y overflow-hidden rounded-xl border bg-card">
                {items.map((meeting) => (
                  <button
                    key={meeting.id}
                    onClick={() => onOpen(meeting.id)}
                    className="flex w-full items-center gap-4 px-4 py-3 text-left transition-colors hover:bg-muted/50"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-medium">{meeting.title}</div>
                      <div className="mt-0.5 truncate text-xs text-muted-foreground">
                        {when(meeting.started_at)}
                        {meeting.duration ? ` \u00b7 ${minutes(meeting.duration)}` : ""}
                        {meeting.participants.length
                          ? ` \u00b7 ${meeting.participants.slice(0, 3).join(", ")}${meeting.participants.length > 3 ? "..." : ""}`
                          : ""}
                        {meeting.status === "processing" && meeting.progress ? ` \u00b7 ${meeting.progress}` : ""}
                      </div>
                    </div>
                    {(meeting.mentions ?? 0) > 0 && (
                      <span
                        className="flex items-center gap-0.5 text-xs tabular-nums text-sky-600 dark:text-sky-400"
                        title={`Someone said your name ${meeting.mentions === 1 ? "once" : `${meeting.mentions} times`}`}
                      >
                        <AtSign className="size-3.5" />
                        {meeting.mentions}
                      </span>
                    )}
                    {meeting.participants.length > 0 && (
                      <AvatarStack names={meeting.participants} colorOf={(name) => personColor(name, name === me)} />
                    )}
                    <StatusBadge meeting={meeting} />
                    <ChevronRight className="size-4 text-muted-foreground" />
                  </button>
                ))}
              </div>
            </section>
          );
        })}
      </div>
    </ScrollArea>
  );
}
