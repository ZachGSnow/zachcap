import { ArrowUp, Loader2, MessageCircleQuestion } from "lucide-react";
import { memo, useState } from "react";
import { toast } from "sonner";

import { Answer } from "@/components/Answer";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/engine";

type Exchange = { question: string; answer?: string; cost?: number; failed?: boolean };

const STARTERS = ["What did I commit to?", "What was decided?", "Were there any risks or blockers?", "Who owns the next steps?"];

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
                  <Answer text={exchange.answer} onCite={(_, at) => at != null && onSeek(at)} />
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
