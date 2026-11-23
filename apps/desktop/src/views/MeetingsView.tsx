import { AudioLines, CalendarDays, ChevronRight, Circle, CircleCheck, Clock3, ListChecks, Loader2, Users } from "lucide-react";

import { AvatarStack, StatCard } from "@/components/Visuals";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { usePoll } from "@/hooks/usePoll";
import { api, type KeyStatus, type Meeting, type Settings, type Stats } from "@/lib/engine";
import { STATUS_LABEL, dayGroup, hours, minutes, personColor, when } from "@/lib/format";

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

export function MeetingsView({ onOpen, onSettings, onImport }: { onOpen: (id: number) => void; onSettings: () => void; onImport: () => void }) {
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
