import { ArrowUp, Loader2, MessageCircleQuestion } from "lucide-react";
import { memo, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/engine";
import { toSeconds } from "@/lib/format";

type Exchange = { question: string; answer?: string; cost?: number; failed?: boolean };

const STARTERS = ["What did I commit to?", "What was decided?", "Were there any risks or blockers?", "Who owns the next steps?"];

/** One line of an answer: **bold** and [12:34] timestamps that jump the player. */
function Inline({ text, onSeek }: { text: string; onSeek: (seconds: number) => void }) {
  const parts = text.split(/(\*\*[^*]+\*\*|\[\d{1,2}:\d{2}(?::\d{2})?\])/g);
  return (
    <>
      {parts.map((part, index) => {
        if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
          return <strong key={index}>{part.slice(2, -2)}</strong>;
        }
        const at = /^\[\d/.test(part) ? toSeconds(part) : null;
        return at != null ? (
          <button
            key={index}
            onClick={() => onSeek(at)}
            className="mx-0.5 rounded bg-muted px-1 font-mono text-xs text-foreground hover:bg-foreground hover:text-background"
          >
            {part.slice(1, -1)}
          </button>
        ) : (
          <span key={index}>{part}</span>
        );
      })}
    </>
  );
}

/** Light Markdown: paragraphs and "-" bullet lists, with inline bold and timestamps. */
function Answer({ text, onSeek }: { text: string; onSeek: (seconds: number) => void }) {
  const blocks: { list: boolean; lines: string[] }[] = [];
  for (const raw of text.split("\n")) {
    const line = raw.trimEnd();
    const bullet = /^\s*[-*] /.test(line);
    if (!line.trim()) {
      blocks.push({ list: false, lines: [] });
      continue;
    }
    const last = blocks.at(-1);
    const content = bullet ? line.replace(/^\s*[-*] /, "") : line;
    if (last && last.list === bullet && (bullet || last.lines.length)) last.lines.push(content);
    else blocks.push({ list: bullet, lines: [content] });
  }
  return (
    <div className="space-y-2 text-sm leading-relaxed">
      {blocks
        .filter((block) => block.lines.length)
        .map((block, index) =>
          block.list ? (
            <ul key={index} className="list-disc space-y-1 pl-5">
              {block.lines.map((line, i) => (
                <li key={i}>
                  <Inline text={line} onSeek={onSeek} />
                </li>
              ))}
            </ul>
          ) : (
            <p key={index}>
              {block.lines.map((line, i) => (
                <span key={i}>
                  {i > 0 && <br />}
                  <Inline text={line} onSeek={onSeek} />
                </span>
              ))}
            </p>
          ),
        )}
    </div>
  );
}

// Memoized: the panel stays mounted on the meeting page, which re-renders as audio plays.
export const AskPanel = memo(function AskPanel({ meetingId, onSeek }: { meetingId: number; onSeek: (seconds: number) => void }) {
  const [question, setQuestion] = useState("");
  const [history, setHistory] = useState<Exchange[]>([]);
  const [busy, setBusy] = useState(false);

  const ask = async (text: string) => {
    if (!text.trim() || busy) return;
    setQuestion("");
    setBusy(true);
    setHistory((h) => [...h, { question: text.trim() }]);
    try {
      const result = await api<{ answer: string; cost_usd: number }>(`/api/meetings/${meetingId}/ask`, {
        method: "POST",
        json: { question: text.trim() },
      });
      setHistory((h) => h.map((e, i) => (i === h.length - 1 ? { ...e, answer: result.answer, cost: result.cost_usd } : e)));
    } catch (error) {
      toast.error("Couldn't get an answer", { description: String(error) });
      setHistory((h) => h.map((e, i) => (i === h.length - 1 ? { ...e, failed: true } : e)));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      {history.length === 0 && (
        <div className="flex flex-col items-center py-8 text-center">
          <div className="flex size-12 items-center justify-center rounded-full bg-muted">
            <MessageCircleQuestion className="size-5" />
          </div>
          <p className="mt-4 font-medium">Ask about this meeting</p>
          <p className="mt-1 max-w-sm text-sm text-muted-foreground">
            Claude answers from the transcript and cites the moments, so you can jump straight to them.
          </p>
          <div className="mt-4 flex flex-wrap justify-center gap-2">
            {STARTERS.map((starter) => (
              <Button key={starter} size="sm" variant="outline" onClick={() => void ask(starter)}>
                {starter}
              </Button>
            ))}
          </div>
        </div>
      )}
      <div className="space-y-4">
        {history.map((exchange, index) => (
          <div key={index} className="space-y-2">
            <div className="ml-auto w-fit max-w-[80%] rounded-2xl rounded-br-sm bg-foreground px-3.5 py-2 text-sm text-background">
              {exchange.question}
            </div>
            <div className="max-w-[90%] rounded-2xl rounded-bl-sm border bg-card px-3.5 py-2.5">
              {exchange.answer ? (
                <>
                  <Answer text={exchange.answer} onSeek={onSeek} />
                  {exchange.cost != null && (
                    <div className="mt-1.5 text-[10px] text-muted-foreground">${exchange.cost.toFixed(4)}</div>
                  )}
                </>
              ) : exchange.failed ? (
                <span className="text-sm text-muted-foreground">No answer.</span>
              ) : (
                <span className="flex items-center gap-2 text-sm text-muted-foreground">
                  <Loader2 className="size-3.5 animate-spin" />
                  Reading the transcript...
                </span>
              )}
            </div>
          </div>
        ))}
      </div>
      <form
        className="sticky bottom-0 mt-5 flex gap-2 bg-background pb-1"
        onSubmit={(event) => {
          event.preventDefault();
          void ask(question);
        }}
      >
        <Input
          placeholder="Ask anything about this meeting"
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          className="h-10"
        />
        <Button type="submit" size="icon" className="size-10 shrink-0" disabled={busy || !question.trim()}>
          {busy ? <Loader2 className="size-4 animate-spin" /> : <ArrowUp className="size-4" />}
        </Button>
      </form>
    </div>
  );
});
