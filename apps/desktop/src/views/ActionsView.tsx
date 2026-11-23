import { ListChecks } from "lucide-react";
import { useMemo, useState } from "react";

import { ActionRow, toggleAction } from "@/components/SummaryPanel";
import { StatCard } from "@/components/Visuals";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { usePoll } from "@/hooks/usePoll";
import { api, type ActionItem } from "@/lib/engine";
import { clock, day, personColor } from "@/lib/format";
import { cn } from "@/lib/utils";

type Filter = "open" | "done" | "all";

export function ActionsView({ onOpen }: { onOpen: (meetingId: number, at?: number) => void }) {
  const { data, refresh } = usePoll(() => api<{ me: string; items: ActionItem[] }>("/api/actions"), 0);
  const [filter, setFilter] = useState<Filter>("open");
  const [mine, setMine] = useState(false);
  const [kind, setKind] = useState<"all" | "commitment" | "action">("all");

  const me = data?.me ?? "You";
  const items = data?.items ?? [];
  const shown = useMemo(
    () =>
      items.filter(
        (i) =>
          (filter === "all" || i.status === filter) &&
          (!mine || i.owner === me) &&
          (kind === "all" || i.kind === kind),
      ),
    [items, filter, mine, me, kind],
  );
  const groups = useMemo(() => {
    const map = new Map<number, ActionItem[]>();
    for (const item of shown) map.set(item.meeting_id, [...(map.get(item.meeting_id) ?? []), item]);
    return [...map.values()];
  }, [shown]);

  const toggle = async (item: ActionItem) => {
    await toggleAction(item);
    await refresh();
  };

  const open = items.filter((i) => i.status === "open");
  return (
    <ScrollArea className="min-h-0 flex-1">
      <div className="mx-auto max-w-4xl px-8 py-8">
        <h1 className="font-heading text-2xl font-semibold tracking-tight">Action items</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Every commitment and assigned task from your summarized meetings, in one list.
        </p>

        <div className="mt-6 grid grid-cols-3 gap-3">
          <StatCard label="Open" value={open.length} hint="still to do" />
          <StatCard label="Yours" value={open.filter((i) => i.owner === me).length} hint={`owned by ${me}`} />
          <StatCard label="Done" value={items.length - open.length} hint="checked off" />
        </div>

        <div className="mt-6 flex flex-wrap items-center gap-2">
          <Tabs value={filter} onValueChange={(value) => setFilter(value as Filter)}>
            <TabsList>
              <TabsTrigger value="open">Open</TabsTrigger>
              <TabsTrigger value="done">Done</TabsTrigger>
              <TabsTrigger value="all">All</TabsTrigger>
            </TabsList>
          </Tabs>
          <Button size="sm" variant={mine ? "default" : "outline"} onClick={() => setMine(!mine)}>
            Mine
          </Button>
          {(["all", "commitment", "action"] as const).map((value) => (
            <Button
              key={value}
              size="sm"
              variant={kind === value ? "secondary" : "ghost"}
              onClick={() => setKind(value)}
              className={cn(kind !== value && "text-muted-foreground")}
            >
              {value === "all" ? "Any kind" : value === "commitment" ? "Commitments" : "Assigned"}
            </Button>
          ))}
        </div>

        {data && shown.length === 0 && (
          <div className="mt-16 flex flex-col items-center text-center">
            <div className="flex size-12 items-center justify-center rounded-full bg-muted">
              <ListChecks className="size-5" />
            </div>
            <p className="mt-4 font-medium">{items.length ? "Nothing here" : "No action items yet"}</p>
            <p className="mt-1 max-w-sm text-sm text-muted-foreground">
              {items.length
                ? "Try another filter."
                : "Summarize a meeting and its action items and commitments show up here."}
            </p>
          </div>
        )}

        {groups.map((group) => (
          <section key={group[0].meeting_id} className="mt-7">
            <button
              onClick={() => onOpen(group[0].meeting_id)}
              className="mb-2 flex items-baseline gap-2 text-left hover:underline"
            >
              <span className="text-sm font-medium">{group[0].meeting_title}</span>
              <span className="text-xs text-muted-foreground">{day(group[0].meeting_started_at)}</span>
            </button>
            <div className="divide-y overflow-hidden rounded-xl border bg-card">
              {group.map((item) => (
                <ActionRow
                  key={item.id}
                  item={item}
                  me={me}
                  color={personColor(item.owner, item.owner === me)}
                  onToggle={(i) => void toggle(i)}
                  meetingLabel={
                    item.said_at != null ? (
                      <button
                        onClick={() => onOpen(item.meeting_id, item.said_at ?? undefined)}
                        className="font-mono hover:text-foreground hover:underline"
                      >
                        said at {clock(item.said_at)}
                      </button>
                    ) : undefined
                  }
                />
              ))}
            </div>
          </section>
        ))}
      </div>
    </ScrollArea>
  );
}
