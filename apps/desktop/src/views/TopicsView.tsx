import { Hash } from "lucide-react";

import { ScrollArea } from "@/components/ui/scroll-area";
import { usePoll } from "@/hooks/usePoll";
import { api, type TopicOverview } from "@/lib/engine";
import { day } from "@/lib/format";

/** The topics you track (Settings > Topics), with how often each came up and where: to see
 * which clients keep raising renewals, or which meetings were about an outage. */
export function TopicsView({
  onOpen,
  onSettings,
}: {
  onOpen: (meetingId: number) => void;
  onSettings: () => void;
}) {
  const { data: topics, error } = usePoll(() => api<TopicOverview[]>("/api/topics"), 0);

  return (
    <ScrollArea className="min-h-0 flex-1">
      <div className="mx-auto max-w-4xl px-8 py-8">
        <h1 className="font-heading text-2xl font-semibold tracking-tight">Topics</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Words and phrases you track across your meetings, and where they came up. Worked out on this computer; nothing is
          sent anywhere.
        </p>

        {error && !topics && <p className="mt-8 text-sm text-destructive">The topics didn't load: {error.message}</p>}

        {topics && topics.length === 0 && (
          <div className="mt-14 flex flex-col items-center text-center">
            <div className="flex size-12 items-center justify-center rounded-full bg-muted">
              <Hash className="size-5" />
            </div>
            <p className="mt-4 font-medium">No topics yet</p>
            <p className="mt-1 max-w-sm text-sm text-muted-foreground">
              Add the ones you care about in Settings, like{" "}
              <code className="font-mono text-xs">Renewals: renewal, contract, quote</code>, and every meeting that mentions
              them shows up here.
            </p>
            <button onClick={onSettings} className="mt-4 text-sm font-medium underline-offset-4 hover:underline">
              Open Settings
            </button>
          </div>
        )}

        <div className="mt-6 space-y-6">
          {topics?.map((topic) => (
            <section key={topic.topic}>
              <div className="mb-2 flex items-baseline gap-2">
                <h2 className="font-medium">{topic.topic}</h2>
                <span className="text-xs text-muted-foreground">
                  {topic.count === 0
                    ? "not said yet"
                    : `said ${topic.count} time${topic.count === 1 ? "" : "s"} in ${topic.meetings.length} meeting${topic.meetings.length === 1 ? "" : "s"}`}
                </span>
                <span className="ml-auto truncate text-xs text-muted-foreground" title={topic.words.join(", ")}>
                  {topic.words.join(", ")}
                </span>
              </div>
              {topic.meetings.length > 0 && (
                <div className="divide-y overflow-hidden rounded-xl border bg-card">
                  {topic.meetings.slice(0, 12).map((meeting) => (
                    <button
                      key={meeting.id}
                      onClick={() => onOpen(meeting.id)}
                      className="flex w-full items-center gap-3 px-4 py-2.5 text-left text-sm transition-colors hover:bg-muted/50"
                    >
                      <span className="min-w-0 flex-1 truncate font-medium">{meeting.title}</span>
                      {meeting.client && <span className="shrink-0 text-xs text-muted-foreground">{meeting.client}</span>}
                      <span className="w-16 shrink-0 text-right text-xs text-muted-foreground">{day(meeting.started_at)}</span>
                      <span className="w-10 shrink-0 text-right font-mono text-xs tabular-nums">{meeting.count}x</span>
                    </button>
                  ))}
                  {topic.meetings.length > 12 && (
                    <p className="px-4 py-2 text-xs text-muted-foreground">and {topic.meetings.length - 12} older</p>
                  )}
                </div>
              )}
            </section>
          ))}
        </div>
      </div>
    </ScrollArea>
  );
}
