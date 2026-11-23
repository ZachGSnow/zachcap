import { Check, ChevronLeft, ChevronRight, ClipboardCopy, Copy, FileDown, NotebookText } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { toggleAction } from "@/components/SummaryPanel";
import { StatCard } from "@/components/Visuals";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { usePoll } from "@/hooks/usePoll";
import { api, type Settings, type WorkLog, type WorkLogItem } from "@/lib/engine";
import { copyText } from "@/lib/feedback";
import { cn } from "@/lib/utils";

type Period = "day" | "week";
const PERIOD_KEY = "zachcap-worklog-period";

/** A local date as YYYY-MM-DD (not UTC: late meetings belong to the day they happened). */
function isoDay(when: Date): string {
  return `${when.getFullYear()}-${String(when.getMonth() + 1).padStart(2, "0")}-${String(when.getDate()).padStart(2, "0")}`;
}

function shiftDay(day: string, days: number): string {
  const when = new Date(`${day}T12:00:00`); // noon: never pushed over a day by a clock change
  when.setDate(when.getDate() + days);
  return isoDay(when);
}

function duration(seconds: number): string {
  const minutes = Math.round(seconds / 60);
  return minutes >= 60 ? `${Math.floor(minutes / 60)} h ${String(minutes % 60).padStart(2, "0")} min` : `${minutes} min`;
}

/** One line per meeting, ready to paste into a time-entry or ticket system. */
function timeEntries(log: WorkLog): string {
  return log.days
    .flatMap((day) =>
      day.meetings.map((m) =>
        [log.period === "week" ? day.day : null, m.time, `${m.hours} h`, m.client && !log.client ? m.client : null,
          m.ticket ? `Ticket ${m.ticket}` : null, m.note ? `${m.title}: ${m.note}` : m.title]
          .filter(Boolean)
          .join("  "),
      ),
    )
    .join("\n");
}

function ItemRow({ item, meeting, onToggle }: { item: WorkLogItem; meeting?: string; onToggle: (item: WorkLogItem) => void }) {
  const done = item.status === "done";
  return (
    <li className="flex items-start gap-2.5 text-sm">
      <button
        role="checkbox"
        aria-checked={done}
        aria-label={item.task}
        onClick={() => onToggle(item)}
        title={done ? "Mark as open" : "Mark as done"}
        className={cn(
          "mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-[4px] border transition-colors",
          done ? "border-foreground bg-foreground text-background" : "border-muted-foreground/40 hover:border-foreground",
        )}
      >
        {done && <Check className="size-3" strokeWidth={3} />}
      </button>
      <span className={cn("min-w-0 flex-1", done && "text-muted-foreground line-through")}>
        {meeting === undefined && <span className="font-medium">{item.owner}: </span>}
        {item.task}
        {item.due && <span className="text-muted-foreground"> {"\u00b7"} due {item.due}</span>}
        {meeting && <span className="text-muted-foreground"> {"\u00b7"} {meeting}</span>}
      </span>
    </li>
  );
}

/** A day's or a week's meetings with what came of them and draft time entries, built from
 * the summaries already here: free, instant, nothing sent anywhere. */
export function WorkLogView({ onOpen }: { onOpen: (meetingId: number) => void }) {
  const [period, setPeriod] = useState<Period>(() => (localStorage.getItem(PERIOD_KEY) === "week" ? "week" : "day"));
  const [day, setDay] = useState(() => isoDay(new Date()));
  // One client's meetings only (offered once a client has been named).
  const [client, setClient] = useState("");
  useEffect(() => localStorage.setItem(PERIOD_KEY, period), [period]);
  const { data: log, error, refresh } = usePoll(
    () => api<WorkLog>(`/api/worklog?period=${period}&day=${day}${client ? `&client=${encodeURIComponent(client)}` : ""}`),
    0,
    `${period}-${day}-${client}`,
  );
  const { data: known = [] } = usePoll(() => api<string[]>("/api/clients"), 0);
  const { data: settings } = usePoll(() => api<Settings>("/api/settings"), 0);
  const today = isoDay(new Date());
  const showsToday = log ? log.start <= today && today <= log.end : true;

  const toggle = async (item: WorkLogItem) => {
    await toggleAction(item);
    await refresh();
  };

  const save = async () => {
    try {
      const { path } = await api<{ path: string }>("/api/worklog/save", { method: "POST", json: { period, day } });
      toast.success("Saved to the export folder", { description: path });
    } catch (e) {
      toast.error("Couldn't save the work log", { description: String(e) });
    }
  };

  return (
    <ScrollArea className="min-h-0 flex-1">
      <div className="mx-auto max-w-4xl px-8 py-8">
        <h1 className="font-heading text-2xl font-semibold tracking-tight">Work log</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Your meetings, what came of them, and a draft time entry for each (rounded up to 15 minutes). Built from your
          summaries on this computer; nothing is sent anywhere.
        </p>

        <div className="mt-6 flex flex-wrap items-center gap-2">
          <div
            className="flex rounded-lg border p-0.5"
            role="radiogroup"
            aria-label="Period"
            onKeyDown={(event) => {
              // Arrow keys move the choice, as in any radio group (one tab stop for the group).
              if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
              event.preventDefault();
              const next = period === "day" ? "week" : "day";
              setPeriod(next);
              event.currentTarget.querySelector<HTMLButtonElement>(`[data-period="${next}"]`)?.focus();
            }}
          >
            {(["day", "week"] as const).map((choice) => (
              <button
                key={choice}
                role="radio"
                data-period={choice}
                aria-checked={period === choice}
                tabIndex={period === choice ? 0 : -1}
                onClick={() => setPeriod(choice)}
                className={cn(
                  "rounded-md px-3 py-1 text-sm capitalize transition-colors",
                  period === choice ? "bg-foreground text-background" : "text-muted-foreground hover:text-foreground",
                )}
              >
                {choice}
              </button>
            ))}
          </div>
          <Button size="icon" variant="outline" className="size-8" aria-label="Earlier" onClick={() => setDay(shiftDay(day, period === "week" ? -7 : -1))}>
            <ChevronLeft className="size-4" />
          </Button>
          <Button size="icon" variant="outline" className="size-8" aria-label="Later" onClick={() => setDay(shiftDay(day, period === "week" ? 7 : 1))}>
            <ChevronRight className="size-4" />
          </Button>
          {!showsToday && (
            <Button size="sm" variant="ghost" onClick={() => setDay(today)}>
              {period === "week" ? "This week" : "Today"}
            </Button>
          )}
          {known.length > 0 && (
            <select
              aria-label="Client"
              value={client}
              onChange={(event) => setClient(event.target.value)}
              className="h-8 rounded-md border bg-transparent px-2 text-sm"
            >
              <option value="">Every client</option>
              {known.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          )}
          <span className="ml-1 font-medium">{log?.title.replace(/^Work log: /, "") ?? ""}</span>
          <div className="ml-auto flex gap-2">
            {log && log.totals.meetings > 0 && (
              <Button size="sm" variant="outline" onClick={() => void copyText(log.markdown, "Work log copied as Markdown")}>
                <Copy className="size-3.5" />
                Copy
              </Button>
            )}
            {settings?.export_folder && log && log.totals.meetings > 0 && (
              <Button size="sm" variant="outline" onClick={() => void save()} title={`Save into ${settings.export_folder}`}>
                <FileDown className="size-3.5" />
                Save to export folder
              </Button>
            )}
          </div>
        </div>

        {error && !log && <p className="mt-8 text-sm text-destructive">The work log didn't load: {error.message}</p>}

        {log && log.totals.meetings === 0 && (
          <div className="mt-14 flex flex-col items-center text-center">
            <div className="flex size-12 items-center justify-center rounded-full bg-muted">
              <NotebookText className="size-5" />
            </div>
            <p className="mt-4 font-medium">
              {showsToday ? (period === "week" ? "No meetings this week" : "No meetings today") : `No meetings that ${period}`}
            </p>
            <p className="mt-1 max-w-sm text-sm text-muted-foreground">
              Recorded and imported meetings show up here with their summaries, action items and time entries.
            </p>
          </div>
        )}

        {log && log.totals.meetings > 0 && (
          <>
            <div className="mt-5 grid grid-cols-3 gap-3">
              <StatCard label="Meetings" value={log.totals.meetings} hint={period === "week" ? "in the week" : "on the day"} />
              <StatCard label="Time" value={duration(log.totals.seconds)} hint="recorded" />
              <StatCard label="To bill" value={`${log.totals.hours} h`} hint="15-minute steps" />
            </div>

            <section className="mt-6 rounded-xl border bg-card">
              <div className="flex items-center justify-between border-b px-4 py-2.5">
                <h2 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">Time entries</h2>
                <Button size="sm" variant="ghost" className="h-7" onClick={() => void copyText(timeEntries(log), "Time entries copied")}>
                  <ClipboardCopy className="size-3.5" />
                  Copy
                </Button>
              </div>
              <table className="w-full table-fixed text-sm">
                <tbody className="divide-y">
                  {log.days.flatMap((day) =>
                    day.meetings.map((m) => (
                      <tr key={m.id} className="align-top">
                        {period === "week" && <td className="w-24 py-2 pl-4 text-muted-foreground">{day.label.split(",")[0]}</td>}
                        <td className="w-28 py-2 pl-4 font-mono text-xs tabular-nums text-muted-foreground">{m.time}</td>
                        <td className="w-14 py-2 font-mono text-xs tabular-nums">{m.hours} h</td>
                        <td className="py-2 pr-4 [overflow-wrap:anywhere]">
                          {m.client && !log.client && (
                            <span className="mr-1.5 text-xs text-muted-foreground" title="The client it was for">
                              {m.client}
                            </span>
                          )}
                          {m.ticket && (
                            <span className="mr-1.5 font-mono text-xs text-muted-foreground" title="The ticket it was about">
                              {m.ticket}
                            </span>
                          )}
                          <button onClick={() => onOpen(m.id)} className="text-left font-medium hover:underline">
                            {m.title}
                          </button>
                          {m.note && <span className="text-muted-foreground"> {"\u00b7"} {m.note}</span>}
                        </td>
                      </tr>
                    )),
                  )}
                </tbody>
              </table>
            </section>

            {log.days.map((day) => (
              <section key={day.day} className="mt-7">
                {period === "week" && <h2 className="mb-2.5 text-sm font-semibold">{day.label}</h2>}
                <div className="space-y-3">
                  {day.meetings.map((m) => (
                    <article key={m.id} className="rounded-xl border bg-card p-4">
                      <div className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-1">
                        <span className="font-mono text-xs tabular-nums text-muted-foreground">{m.time}</span>
                        <button onClick={() => onOpen(m.id)} className="min-w-0 text-left font-medium [overflow-wrap:anywhere] hover:underline">
                          {m.title}
                        </button>
                        {m.people.length > 0 && <span className="text-xs text-muted-foreground">with {m.people.join(", ")}</span>}
                      </div>
                      <p className={cn("mt-2 text-sm leading-relaxed [overflow-wrap:anywhere]", !m.overview && "text-muted-foreground")}>
                        {m.overview || "Not summarized yet: summarize it for an overview, decisions and action items."}
                      </p>
                      {m.decisions.length > 0 && (
                        <ul className="mt-2 space-y-1 text-sm">
                          {m.decisions.map((decision, index) => (
                            <li key={index} className="flex gap-2">
                              <Check className="mt-0.5 size-3.5 shrink-0 text-emerald-600" />
                              {decision}
                            </li>
                          ))}
                        </ul>
                      )}
                      {m.items.length > 0 && (
                        <ul className="mt-3 space-y-1.5 border-t pt-3">
                          {m.items.map((item) => (
                            <ItemRow key={item.id} item={item} onToggle={(i) => void toggle(i)} />
                          ))}
                        </ul>
                      )}
                    </article>
                  ))}
                </div>
              </section>
            ))}

            {(log.your_items.length > 0 || log.waiting_on.length > 0) && (
              <div className="mt-7 grid gap-4 sm:grid-cols-2">
                {[
                  { title: `Yours (${log.me})`, items: log.your_items, empty: "Nothing open on your side." },
                  { title: "Waiting on others", items: log.waiting_on, empty: "Nobody owes you anything here." },
                ].map((group) => (
                  <section key={group.title} className="rounded-xl border bg-card p-4">
                    <h2 className="mb-2.5 text-xs font-medium tracking-wide text-muted-foreground uppercase">{group.title}</h2>
                    {group.items.length ? (
                      <ul className="space-y-1.5">
                        {group.items.map((item) => (
                          <ItemRow
                            key={item.id}
                            item={item}
                            meeting={group.items === log.your_items ? (item.meeting_title ?? "") : undefined}
                            onToggle={(i) => void toggle(i)}
                          />
                        ))}
                      </ul>
                    ) : (
                      <p className="text-sm text-muted-foreground">{group.empty}</p>
                    )}
                  </section>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </ScrollArea>
  );
}
