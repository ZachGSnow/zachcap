import { Check, Copy, FileText, Loader2, Mail, Megaphone, MessageSquareQuote, RotateCw, Sparkles, Ticket } from "lucide-react";
import { useEffect, useId, useRef, useState, type ReactNode } from "react";

import { PrivateNote } from "@/components/PrivateNote";
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
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { useReturnFocus } from "@/hooks/useReturnFocus";
import { api, type ActionItem, type RecipeResult, type Summary, type Writing } from "@/lib/engine";
import { attempt, copyText, reason } from "@/lib/feedback";
import { clock, toSeconds, type SpeakerColor } from "@/lib/format";
import { summaryMarkdown } from "@/lib/markdown";
import { cn } from "@/lib/utils";
import { costLabel, modelLabel, writingLabel, type Writer } from "@/lib/writer";
import { earlier, isHere, outcome, tell, type Asked } from "@/lib/writing";

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
export async function toggleAction(item: Pick<ActionItem, "id" | "status">) {
  return attempt("Couldn't update the action item", () =>
    api(`/api/actions/${item.id}`, { method: "PATCH", json: { status: item.status === "done" ? "open" : "done" } }),
  );
}

type Choice = { id: string; label: string };
const RECIPE_ICON: Record<string, typeof Mail> = { email: Mail, status: Megaphone, ticket: Ticket };

/** The kind of meeting a summary is written for; it decides what the summary focuses on. One
 * ZachCap picked on its own (Automatic) says so next to it, and so does one it couldn't pick. */
function KindPicker({
  templates,
  value,
  automatic,
  unpicked,
  onChange,
}: {
  templates: Choice[];
  value: string;
  automatic: boolean;
  unpicked: boolean;
  onChange: (id: string) => void;
}) {
  const note = useId();
  if (!templates.length) return null;
  const said = automatic ? "Picked automatically" : unpicked ? "No kind could be picked: written as General" : null;
  return (
    <>
      {said && (
        <span id={note} className="self-center text-xs text-muted-foreground">
          {said}
        </span>
      )}
      <select
        aria-label="Kind of meeting"
        aria-describedby={said ? note : undefined}
        title={
          automatic
            ? "Kind of meeting: ZachCap picked this one. Choose another to change it, or Automatic to pick again."
            : "Kind of meeting: decides what the summary focuses on"
        }
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
    </>
  );
}

/** Who writes, when there is a choice: Claude, or the model on this PC. */
function WriterPicker({
  value,
  names,
  onChange,
}: {
  value: Writer;
  names: Record<Writer, string>;
  onChange: (writer: Writer) => void;
}) {
  return (
    <select
      aria-label="Written by"
      title="Who writes: Claude gets the transcript's text; a model on this PC sends nothing"
      value={value}
      onChange={(event) => onChange(event.target.value as Writer)}
      className="h-8 rounded-md border bg-background px-2 text-xs outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
    >
      <option value="claude">{names.claude}</option>
      <option value="local">{names.local}</option>
    </select>
  );
}

/** How far the model on this PC is, with a way to stop it. */
function WritingNote({ writing, onStop, className }: { writing: Writing; onStop: () => void; className?: string }) {
  return (
    <p role="status" className={cn("flex items-center gap-2 text-sm text-muted-foreground", className)}>
      <Loader2 className="size-4 animate-spin" />
      {writingLabel(writing)}
      <Button size="sm" variant="outline" className="h-7" onClick={onStop}>
        Stop
      </Button>
    </p>
  );
}

/** One "write from this meeting" request; results from an older run are ignored. ``text``
 * is the document as edited by hand; ``refining`` while a change is being written. */
type RecipeRun = {
  id: number;
  recipe: Choice;
  result?: RecipeResult;
  error?: string;
  text?: string;
  refining?: boolean;
};

/** The documents the model on this PC was asked for before the page was opened. */
function before(written: Writing[] | undefined, writing: Writing | null): Record<string, RecipeRun> {
  const runs: Record<string, RecipeRun> = {};
  for (const one of earlier("document", written, writing)) {
    const recipe = { id: one.about ?? "", label: "Document" };
    const result = (one.result as RecipeResult | null) ?? runs[recipe.id]?.result;
    runs[recipe.id] = { id: 0, recipe, result, text: result?.text, refining: one.state === "writing" && !!result };
  }
  return runs;
}

/** A document written from the meeting, ready to copy. */
function RecipeDialog({
  open,
  run,
  by,
  writing,
  onStop,
  onClose,
  onRewrite,
  onEdit,
  onRefine,
  onCloseAutoFocus,
}: {
  open: boolean;
  run: RecipeRun | null;
  /** Who is asked to write it. */
  by: string;
  /** The document the model on this PC is on, if it is. */
  writing: Writing | null;
  onStop: () => void;
  onClose: () => void;
  onRewrite: (recipe: Choice) => void;
  /** The document edited by hand: kept with its run, so it survives closing and reopening. */
  onEdit: (recipe: Choice, text: string) => void;
  /** Rewrite the document as it stands (edits included) the way asked. */
  onRefine: (recipe: Choice, previous: string, change: string) => void;
  onCloseAutoFocus: (event: Event) => void;
}) {
  const unwritten = run !== null && !run.result && !run.error;
  const text = run?.text ?? run?.result?.text ?? "";
  const [change, setChange] = useState("");
  const refine = () => {
    if (!run || run.refining || !change.trim() || !text.trim()) return;
    onRefine(run.recipe, text, change.trim());
    setChange("");
  };
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="sm:max-w-2xl" onCloseAutoFocus={onCloseAutoFocus}>
        <DialogHeader>
          <DialogTitle>{run?.recipe.label}</DialogTitle>
          <DialogDescription>
            Written by {run?.result ? modelLabel(run.result.model) : by} from this meeting. Read it over before you send
            it.
          </DialogDescription>
        </DialogHeader>
        {run?.error && (
          <p className="text-sm text-destructive">
            {run.result ? `Couldn't rewrite it: ${run.error}` : run.error}
          </p>
        )}
        {run?.result ? (
          <>
            <Textarea
              aria-label="The document"
              value={text}
              readOnly={run.refining}
              onChange={(event) => onEdit(run.recipe, event.target.value)}
              className="min-h-72 text-sm leading-relaxed"
            />
            <div className="flex items-center gap-2">
              <Input
                aria-label="Change it"
                placeholder="Change it: shorter, for the client, add next steps..."
                value={change}
                maxLength={500}
                disabled={run.refining}
                onChange={(event) => setChange(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") refine();
                }}
                className="h-8 text-sm"
              />
              <Button size="sm" variant="outline" disabled={run.refining || !change.trim()} onClick={refine}>
                {run.refining ? <Loader2 className="size-3.5 animate-spin" /> : <Sparkles className="size-3.5" />}
                {run.refining ? "Rewriting..." : "Rewrite"}
              </Button>
            </div>
          </>
        ) : (
          !run?.error &&
          (writing ? (
            <WritingNote writing={writing} onStop={onStop} className="py-10" />
          ) : (
            <p className="flex items-center gap-2 py-10 text-sm text-muted-foreground">
              <Loader2 className="size-4 animate-spin" />
              Writing...
            </p>
          ))
        )}
        {run?.refining && writing && <WritingNote writing={writing} onStop={onStop} />}
        <DialogFooter className="items-center">
          {run?.result && (
            <span className="mr-auto text-xs text-muted-foreground">
              {modelLabel(run.result.model)} {"\u00b7"} {costLabel(run.result.model, run.result.cost_usd)}
            </span>
          )}
          <Button variant="outline" onClick={onClose}>
            Close
          </Button>
          <Button variant="outline" disabled={unwritten || run?.refining} onClick={() => run && onRewrite(run.recipe)}>
            <RotateCw className="size-4" />
            Write again
          </Button>
          <Button disabled={!run?.result} onClick={() => run?.result && void copyText(text, "Copied")}>
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
  pickedAutomatically,
  unpicked,
  isPrivate,
  onAllow,
  onKindChange,
  writer,
  writerNames,
  onWriterChange,
  picked,
  writing,
  written,
  onStarted,
  onStop,
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
  /** That kind is the meeting's own, and ZachCap picked it (Automatic). */
  pickedAutomatically: boolean;
  /** The meeting is Automatic, and no kind could be picked for its summary. */
  unpicked: boolean;
  /** Kept from Claude: only the model on this PC writes from it, until it's allowed
   * (``onAllow``). */
  isPrivate: boolean;
  onAllow: () => void;
  onKindChange: (kind: string) => void;
  /** Who writes (null: nobody may), and what each is called; the picker shows when there
   * is a choice. */
  writer: Writer | null;
  writerNames: Record<Writer, string>;
  onWriterChange: ((writer: Writer) => void) | null;
  /** Who writes, when picked on this page (null: the engine's to settle). */
  picked: Writer | null;
  /** What the model on this PC is writing for this meeting now, and the documents it wrote
   * before the page was opened. */
  writing: Writing | null;
  written: Writing[] | undefined;
  /** The model on this PC is on a document now (so the page follows how far it is). */
  onStarted: () => void;
  onStop: () => void;
  /** Owned by the meeting page, so its header button and this panel can't both start one. */
  summarizing: boolean;
  onSummarize: () => void;
  onSeek: (seconds: number) => void;
  onChanged: () => void;
}) {
  // Each document kept per recipe while the page is open: reopening shows the one written (or
  // still being written) instead of paying for another.
  const [runs, setRuns] = useState<Record<string, RecipeRun>>(() => before(written, writing));
  const [shown, setShown] = useState<string | null>(null);
  // Separate from which one is shown, so the dialog keeps its content while it closes.
  const [dialogOpen, setDialogOpen] = useState(false);
  const nextRun = useRef(1);
  const onPage = useRef(true);
  useEffect(() => {
    onPage.current = true;
    return () => {
      onPage.current = false;
    };
  }, []);

  const toggle = async (item: ActionItem) => {
    await toggleAction(item);
    onChanged();
  };

  // Started from a click (not an effect), so it runs exactly once. With ``refine``, the
  // document as it stands is rewritten the way asked.
  const write = async (recipe: Choice, refine?: { previous: string; change: string }) => {
    const id = nextRun.current++;
    const settle = (patch: Partial<RecipeRun>) =>
      setRuns((all) => (all[recipe.id]?.id === id ? { ...all, [recipe.id]: { ...all[recipe.id], ...patch } } : all));
    // A change keeps the document as it stands until the new one arrives (and if it fails).
    setRuns((all) => ({
      ...all,
      [recipe.id]: refine ? { ...all[recipe.id], id, recipe, refining: true, error: undefined } : { id, recipe },
    }));
    setShown(recipe.id);
    setDialogOpen(true);
    await finish(settle, () =>
      api<Asked<RecipeResult>>(`/api/meetings/${meetingId}/recipe`, {
        method: "POST",
        json: { recipe: recipe.id, by: picked, ...refine },
      }),
    );
  };

  /** A run's document, once it's written. */
  const finish = async (
    settle: (patch: Partial<RecipeRun>) => void,
    get: () => Promise<Asked<RecipeResult>>,
  ) => {
    try {
      const asked = await get();
      let result;
      if (isHere(asked)) {
        onStarted();
        result = await outcome<RecipeResult>(asked.writing);
        void tell(asked.writing, "Document ready", title, onPage.current);
      } else result = asked;
      settle({ result, text: result.text, refining: false, error: undefined });
    } catch (error) {
      settle({ error: reason(error), refining: false });
    }
  };

  // One asked for before the page was left, and still being written.
  useEffect(() => {
    if (writing?.what !== "document" || !writing.about) return;
    const { id, about } = writing;
    const settle = (patch: Partial<RecipeRun>) =>
      setRuns((all) => (all[about]?.id === 0 ? { ...all, [about]: { ...all[about], ...patch } } : all));
    void finish(settle, () => Promise.resolve({ writing: id }));
    // Only as the page opens: later ones are followed as they're asked for.
  }, []);

  const edit = (recipe: Choice, text: string) =>
    setRuns((all) => (all[recipe.id] ? { ...all, [recipe.id]: { ...all[recipe.id], text } } : all));

  const returnFocus = useReturnFocus();

  const openRecipe = (recipe: Choice) => {
    const run = runs[recipe.id];
    if (run && (!run.error || run.result)) {
      setRuns((all) => ({ ...all, [recipe.id]: { ...all[recipe.id], recipe } }));
      setShown(recipe.id);
      setDialogOpen(true);
    } else void write(recipe);
  };

  const summaryHere = writing?.what === "summary" ? writing : null;
  const documentHere = writing?.what === "document" ? writing : null;
  const pickers = (asked: boolean) => (
    <>
      {writer !== null && onWriterChange && (
        <WriterPicker value={writer} names={writerNames} onChange={onWriterChange} />
      )}
      <KindPicker
        templates={templates}
        value={kind}
        automatic={pickedAutomatically}
        unpicked={asked && unpicked}
        onChange={onKindChange}
      />
    </>
  );

  if (!summary && writer === null) {
    return (
      <PrivateNote
        what="so there's no summary of it. A model on this PC could write one: see Summaries in Settings."
        onAllow={onAllow}
      />
    );
  }

  if (!summary) {
    return (
      <div className="flex flex-col items-center py-14 text-center">
        <div className="flex size-12 items-center justify-center rounded-full bg-muted">
          <Sparkles className="size-5" />
        </div>
        <p className="mt-4 font-medium">Summarize this meeting</p>
        <p className="mt-1 max-w-sm text-sm text-muted-foreground">
          {writer === "local"
            ? `${writerNames.local} reads the named transcript and pulls out decisions, action items, commitments and who said what. Writing it sends nothing; it takes a few minutes.`
            : "Claude reads the named transcript (text only, never audio) and pulls out decisions, action items, commitments and who said what."}{" "}
          Pick the kind of meeting to set the focus.
        </p>
        <div className="mt-5 flex flex-wrap items-center justify-center gap-2">
          {pickers(false)}
          <Button disabled={summarizing} onClick={onSummarize}>
            {summarizing ? <Loader2 className="size-4 animate-spin" /> : <Sparkles className="size-4" />}
            {summarizing ? "Summarizing..." : writer === "local" ? "Summarize on this PC" : "Summarize with Claude"}
          </Button>
        </div>
        {summaryHere && <WritingNote writing={summaryHere} onStop={onStop} className="mt-4" />}
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
          {((summary.input_tokens + summary.output_tokens) / 1000).toFixed(1)}k tokens {"\u00b7"}{" "}
          {costLabel(summary.model, summary.cost_usd)}
        </span>
        <div className="ml-auto flex flex-wrap justify-end gap-2">
          <Button
            size="sm"
            variant="outline"
            onClick={() => void copyText(summaryMarkdown(title, summary, items), "Summary copied as Markdown")}
          >
            <Copy className="size-3.5" />
            Copy
          </Button>
          {writer !== null && (
            <>
              {pickers(true)}
              <Button size="sm" variant="outline" disabled={summarizing} onClick={onSummarize}>
                {summarizing ? <Loader2 className="size-3.5 animate-spin" /> : <RotateCw className="size-3.5" />}
                Regenerate
              </Button>
            </>
          )}
        </div>
      </div>

      {summaryHere && <WritingNote writing={summaryHere} onStop={onStop} className="mt-3" />}
      {documentHere && !dialogOpen && <WritingNote writing={documentHere} onStop={onStop} className="mt-3" />}
      {isPrivate && (
        <p className="mt-3 text-xs text-muted-foreground">
          {writer === null
            ? "This meeting is private now: Claude can't regenerate the summary or write from it."
            : `This meeting is private: ${writerNames.local} writes from it, and nothing is sent.`}
        </p>
      )}
      {recipes.length > 0 && writer !== null && (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <span className="text-xs text-muted-foreground">Write from this meeting:</span>
          {recipes.map((recipe) => {
            // Your own documents get a plain document icon.
            const Icon = RECIPE_ICON[recipe.id] ?? FileText;
            return (
              <Button
                key={recipe.id}
                size="sm"
                variant="ghost"
                className="h-7 border"
                onClick={(event) => {
                  returnFocus.remember(event.currentTarget);
                  openRecipe(recipe);
                }}
              >
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
        by={writerNames[writer ?? "claude"]}
        writing={documentHere}
        onStop={onStop}
        onClose={() => setDialogOpen(false)}
        onRewrite={(recipe) => void write(recipe)}
        onEdit={edit}
        onRefine={(recipe, previous, change) => void write(recipe, { previous, change })}
        onCloseAutoFocus={returnFocus.onCloseAutoFocus}
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
