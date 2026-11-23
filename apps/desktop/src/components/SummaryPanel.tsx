import { Check, Copy, Loader2, Mail, Megaphone, MessageSquareQuote, RotateCw, Sparkles, Ticket } from "lucide-react";
import { useRef, useState, type ReactNode } from "react";

import { PersonAvatar } from "@/components/Visuals";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { api, type ActionItem, type RecipeResult, type Summary } from "@/lib/engine";
import { attempt, copyText } from "@/lib/feedback";
import { clock, toSeconds, type SpeakerColor } from "@/lib/format";
import { cn } from "@/lib/utils";

export function modelLabel(id: string): string {
  const known: Record<string, string> = {
    "claude-opus-5-5": "Claude Opus 5.5",
    "claude-sonnet-5-5": "Claude Sonnet 5.5",
    "claude-fable-5-1": "Claude Fable 5.1",
  };
  return known[id] ?? id;
}

function toMarkdown(title: string, summary: Summary, items: ActionItem[]): string {
  const c = summary.content;
  const list = (values: string[]) => values.map((value) => `- ${value}`).join("\n");
  const sections = [
    `# ${title}`,
    c.overview,
    items.length
      ? `## Action items\n${items
          .map((a) => `- [${a.status === "done" ? "x" : " "}] **${a.owner}**: ${a.task}${a.due ? ` (${a.due})` : ""}`)
          .join("\n")}`
      : "",
    c.decisions.length ? `## Decisions\n${list(c.decisions)}` : "",
    c.who_said_what?.length
      ? `## Who said what\n${c.who_said_what.map((s) => `**${s.speaker}**\n${list(s.points)}`).join("\n\n")}`
      : "",
    c.open_questions.length ? `## Open questions\n${list(c.open_questions)}` : "",
  ];
  return sections.filter(Boolean).join("\n\n");
}

function Section({ title, count, children }: { title: string; count?: number; children: ReactNode }) {
  return (
    <section className="mt-7">
      <h3 className="mb-2.5 flex items-center gap-2 text-xs font-medium tracking-wide text-muted-foreground uppercase">
        {title}
        {count !== undefined && <span className="rounded-full bg-muted px-1.5 py-px text-[10px] tabular-nums">{count}</span>}
      </h3>
      {children}
    </section>
  );
}

export function ActionRow({
  item,
  me,
  color,
  onToggle,
  onSeek,
  meetingLabel,
}: {
  item: ActionItem;
  me: string;
  color: SpeakerColor;
  onToggle: (item: ActionItem) => void;
  onSeek?: (seconds: number) => void;
  meetingLabel?: ReactNode;
}) {
  const done = item.status === "done";
  const mine = item.owner === me;
  return (
    <div className={cn("flex items-start gap-3 px-3 py-2.5", mine && !done && "bg-amber-500/5")}>
      <button
        role="checkbox"
        aria-checked={done}
        aria-label={item.task}
        onClick={() => onToggle(item)}
        className={cn(
          "mt-0.5 flex size-4.5 shrink-0 items-center justify-center rounded-[5px] border transition-colors",
          done ? "border-foreground bg-foreground text-background" : "border-muted-foreground/40 hover:border-foreground",
        )}
        title={done ? "Mark as open" : "Mark as done"}
      >
        {done && <Check className="size-3" strokeWidth={3} />}
      </button>
      <div className="min-w-0 flex-1">
        <div className={cn("text-sm", done && "text-muted-foreground line-through")}>{item.task}</div>
        <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
          <span className={cn("inline-flex items-center gap-1 rounded-full px-2 py-px font-medium", color.soft, color.text)}>
            {item.owner}
          </span>
          <Badge variant="outline" className="h-5 px-1.5 text-[10px]">
            {item.kind === "commitment" ? "Commitment" : "Assigned"}
          </Badge>
          {item.due && <span>Due {item.due}</span>}
          {item.said_at != null && onSeek && (
            <button onClick={() => onSeek(item.said_at!)} className="font-mono hover:text-foreground hover:underline">
              said at {clock(item.said_at)}
            </button>
          )}
          {meetingLabel}
        </div>
      </div>
    </div>
  );
}

/** Toggles an action item and says so when it fails. */
export async function toggleAction(item: ActionItem) {
  return attempt("Couldn't update the action item", () =>
    api(`/api/actions/${item.id}`, { method: "PATCH", json: { status: item.status === "done" ? "open" : "done" } }),
  );
}

type Choice = { id: string; label: string };
const RECIPE_ICON: Record<string, typeof Mail> = { email: Mail, status: Megaphone, ticket: Ticket };

/** The kind of meeting a summary is written for; it decides what the summary focuses on. */
function KindPicker({ templates, value, onChange }: { templates: Choice[]; value: string; onChange: (id: string) => void }) {
  if (!templates.length) return null;
  return (
    <select
      aria-label="Kind of meeting"
      title="Kind of meeting: decides what the summary focuses on"
      value={value}
      onChange={(event) => onChange(event.target.value)}
      className="h-8 rounded-md border bg-background px-2 text-xs outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
    >
      {templates.map((t) => (
        <option key={t.id} value={t.id}>
          {t.label}
        </option>
      ))}
    </select>
  );
}

/** One "write from this meeting" request; results from an older run are ignored. */
type RecipeRun = { id: number; recipe: Choice; result?: RecipeResult; error?: string };

/** A document Claude wrote from the meeting, ready to copy. */
function RecipeDialog({
  open,
  run,
  onClose,
  onRewrite,
}: {
  open: boolean;
  run: RecipeRun | null;
  onClose: () => void;
  onRewrite: (recipe: Choice) => void;
}) {
  const writing = run !== null && !run.result && !run.error;
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{run?.recipe.label}</DialogTitle>
          <DialogDescription>Written by Claude from this meeting. Read it over before you send it.</DialogDescription>
        </DialogHeader>
        {run?.error ? (
          <p className="text-sm text-destructive">{run.error}</p>
        ) : run?.result ? (
          <Textarea readOnly value={run.result.text} className="min-h-72 text-sm leading-relaxed" />
        ) : (
          <p className="flex items-center gap-2 py-10 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" />
            Writing...
          </p>
        )}
        <DialogFooter className="items-center">
          {run?.result && (
            <span className="mr-auto text-xs text-muted-foreground">
              {modelLabel(run.result.model)} {"\u00b7"} ${run.result.cost_usd.toFixed(3)}
            </span>
          )}
          <Button variant="outline" onClick={onClose}>
            Close
          </Button>
          <Button variant="outline" disabled={writing} onClick={() => run && onRewrite(run.recipe)}>
            <RotateCw className="size-4" />
            Write again
          </Button>
          <Button disabled={!run?.result} onClick={() => run?.result && void copyText(run.result.text, "Copied")}>
            <Copy className="size-4" />
            Copy
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function SummaryPanel({
  meetingId,
  title,
  summary,
  items,
  me,
  colorOf,
  templates,
  recipes,
  kind,
  onKindChange,
  summarizing,
  onSummarize,
  onSeek,
  onChanged,
}: {
  meetingId: number;
  title: string;
  summary: Summary | null;
  items: ActionItem[];
  me: string;
  colorOf: (name: string) => SpeakerColor;
  templates: Choice[];
  recipes: Choice[];
  /** The kind of meeting picked, else the one this meeting was summarized as, else the usual
   * kind. Owned by the meeting page, so its header Summarize button uses it too. */
  kind: string;
  onKindChange: (kind: string) => void;
  /** Owned by the meeting page, so its header button and this panel can't both start one. */
  summarizing: boolean;
  onSummarize: () => void;
  onSeek: (seconds: number) => void;
  onChanged: () => void;
}) {
  // Each document kept per recipe while the page is open: reopening shows the one written (or
  // still being written) instead of paying for another.
  const [runs, setRuns] = useState<Record<string, RecipeRun>>({});
  const [shown, setShown] = useState<string | null>(null);
  // Separate from which one is shown, so the dialog keeps its content while it closes.
  const [dialogOpen, setDialogOpen] = useState(false);
  const nextRun = useRef(1);

  const toggle = async (item: ActionItem) => {
    await toggleAction(item);
    onChanged();
  };

  // Started from a click (not an effect), so it runs exactly once.
  const write = async (recipe: Choice) => {
    const id = nextRun.current++;
    const settle = (patch: Partial<RecipeRun>) =>
      setRuns((all) => (all[recipe.id]?.id === id ? { ...all, [recipe.id]: { ...all[recipe.id], ...patch } } : all));
    setRuns((all) => ({ ...all, [recipe.id]: { id, recipe } }));
    setShown(recipe.id);
    setDialogOpen(true);
    try {
      settle({ result: await api<RecipeResult>(`/api/meetings/${meetingId}/recipe`, { method: "POST", json: { recipe: recipe.id } }) });
    } catch (error) {
      settle({ error: String(error) });
    }
  };

  const openRecipe = (recipe: Choice) => {
    const run = runs[recipe.id];
    if (run && !run.error) {
      setShown(recipe.id);
      setDialogOpen(true);
    } else void write(recipe);
  };

  if (!summary) {
    return (
      <div className="flex flex-col items-center py-14 text-center">
        <div className="flex size-12 items-center justify-center rounded-full bg-muted">
          <Sparkles className="size-5" />
        </div>
        <p className="mt-4 font-medium">Summarize this meeting</p>
        <p className="mt-1 max-w-sm text-sm text-muted-foreground">
          Claude reads the named transcript (text only, never audio) and pulls out decisions, action items,
          commitments and who said what. Pick the kind of meeting to set the focus.
        </p>
        <div className="mt-5 flex items-center gap-2">
          <KindPicker templates={templates} value={kind} onChange={onKindChange} />
          <Button disabled={summarizing} onClick={onSummarize}>
            {summarizing ? <Loader2 className="size-4 animate-spin" /> : <Sparkles className="size-4" />}
            {summarizing ? "Summarizing..." : "Summarize with Claude"}
          </Button>
        </div>
      </div>
    );
  }

  const c = summary.content;
  const open = items.filter((i) => i.status === "open").length;

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="secondary">{modelLabel(summary.model)}</Badge>
        <span className="text-xs text-muted-foreground">
          {((summary.input_tokens + summary.output_tokens) / 1000).toFixed(1)}k tokens {"\u00b7"} $
          {summary.cost_usd.toFixed(3)}
        </span>
        <div className="ml-auto flex gap-2">
          <Button
            size="sm"
            variant="outline"
            onClick={() => void copyText(toMarkdown(title, summary, items), "Summary copied as Markdown")}
          >
            <Copy className="size-3.5" />
            Copy
          </Button>
          <KindPicker templates={templates} value={kind} onChange={onKindChange} />
          <Button size="sm" variant="outline" disabled={summarizing} onClick={onSummarize}>
            {summarizing ? <Loader2 className="size-3.5 animate-spin" /> : <RotateCw className="size-3.5" />}
            Regenerate
          </Button>
        </div>
      </div>

      {recipes.length > 0 && (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <span className="text-xs text-muted-foreground">Write from this meeting:</span>
          {recipes.map((recipe) => {
            const Icon = RECIPE_ICON[recipe.id] ?? Sparkles;
            return (
              <Button key={recipe.id} size="sm" variant="ghost" className="h-7 border" onClick={() => openRecipe(recipe)}>
                <Icon className="size-3.5" />
                {recipe.label}
              </Button>
            );
          })}
        </div>
      )}
      <RecipeDialog
        open={dialogOpen}
        run={shown ? (runs[shown] ?? null) : null}
        onClose={() => setDialogOpen(false)}
        onRewrite={(recipe) => void write(recipe)}
      />

      <p className="mt-5 text-[15px] leading-relaxed">{c.overview}</p>

      {items.length > 0 && (
        <Section title={`Action items ${open ? `(${open} open)` : ""}`}>
          <div className="divide-y overflow-hidden rounded-xl border">
            {items.map((item) => (
              <ActionRow
                key={item.id}
                item={item}
                me={me}
                color={colorOf(item.owner)}
                onToggle={(i) => void toggle(i)}
                onSeek={onSeek}
              />
            ))}
          </div>
        </Section>
      )}

      {c.decisions.length > 0 && (
        <Section title="Decisions" count={c.decisions.length}>
          <ul className="space-y-1.5 text-sm">
            {c.decisions.map((decision, index) => (
              <li key={index} className="flex gap-2">
                <Check className="mt-0.5 size-4 shrink-0 text-emerald-600" />
                {decision}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {c.who_said_what && c.who_said_what.length > 0 && (
        <Section title="Who said what">
          <div className="grid gap-3 sm:grid-cols-2">
            {c.who_said_what.map((entry) => (
              <div key={entry.speaker} className="rounded-xl border bg-card p-3.5">
                <div className="flex items-center gap-2">
                  <PersonAvatar name={entry.speaker} color={colorOf(entry.speaker)} size="sm" />
                  <span className="text-sm font-medium">{entry.speaker}</span>
                </div>
                <ul className="mt-2 space-y-1 text-sm text-muted-foreground">
                  {entry.points.map((point, index) => (
                    <li key={index} className="flex gap-2">
                      <MessageSquareQuote className="mt-0.5 size-3.5 shrink-0" />
                      <span>{point}</span>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </Section>
      )}

      {c.open_questions.length > 0 && (
        <Section title="Open questions" count={c.open_questions.length}>
          <ul className="list-disc space-y-1 pl-5 text-sm">
            {c.open_questions.map((question, index) => (
              <li key={index}>{question}</li>
            ))}
          </ul>
        </Section>
      )}

      {c.topics.length > 0 && (
        <Section title="Topics">
          <div className="flex flex-wrap gap-2">
            {c.topics.map((topic, index) => (
              <button
                key={index}
                onClick={() => {
                  const at = toSeconds(topic.start);
                  if (at != null) onSeek(at);
                }}
                className="rounded-lg border px-2.5 py-1 text-xs transition-colors hover:bg-muted"
              >
                <span className="font-mono text-muted-foreground">{topic.start}</span> {topic.title}
              </button>
            ))}
          </div>
        </Section>
      )}
    </div>
  );
}
