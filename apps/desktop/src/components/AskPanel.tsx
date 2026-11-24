import { ArrowUp, Loader2, MessageCircleQuestion } from "lucide-react";
import { memo, useRef, useState, type ReactNode } from "react";
import { toast } from "sonner";

import { Answer } from "@/components/Answer";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/engine";
import { reason } from "@/lib/feedback";
import { costLabel } from "@/lib/writer";

type Exchange = {
  id: number;
  question: string;
  /** Asked of the model on this PC. */
  here: boolean;
  answer?: string;
  model?: string;
  cost?: number;
  failed?: boolean;
};

const STARTERS = ["What did I commit to?", "What was decided?", "Were there any risks or blockers?", "Who owns the next steps?"];

// Memoized: the panel stays mounted on the meeting page, which re-renders as audio plays.
/** Questions about one meeting. ``here`` is the model on this PC that answers for a private one
 * (Claude answers for the others), and ``answering`` whether it is on a question now, which may
 * be one from before the page was left. ``children`` go in place of the question box, when
 * nobody may answer. */
export const AskPanel = memo(function AskPanel({
  meetingId,
  here,
  answering,
  onSeek,
  children,
}: {
  meetingId: number;
  here: string | null;
  answering: boolean;
  onSeek: (seconds: number) => void;
  children?: ReactNode;
}) {
  const [question, setQuestion] = useState("");
  const [history, setHistory] = useState<Exchange[]>([]);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  const nextId = useRef(1);

  const ask = async (text: string) => {
    if (!text.trim() || inFlight.current) return;
    inFlight.current = true;
    const id = nextId.current++;
    const update = (patch: Partial<Exchange>) => setHistory((h) => h.map((e) => (e.id === id ? { ...e, ...patch } : e)));
    setQuestion("");
    setBusy(true);
    setHistory((h) => [...h, { id, question: text.trim(), here: here !== null }]);
    try {
      const result = await api<{ answer: string; model: string; cost_usd: number }>(`/api/meetings/${meetingId}/ask`, {
        method: "POST",
        json: { question: text.trim() },
      });
      update({ answer: result.answer, model: result.model, cost: result.cost_usd });
    } catch (error) {
      if (reason(error) === "Stopped.") toast("Stopped");
      else toast.error("Couldn't get an answer", { description: reason(error) });
      update({ failed: true });
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  };

  const stop = (
    <Button
      size="sm"
      variant="outline"
      className="h-7"
      onClick={() => void api(`/api/meetings/${meetingId}/writing/stop`, { method: "POST" }).catch(() => undefined)}
    >
      Stop
    </Button>
  );
  // One asked before the page was left: its answer has nowhere to go any more.
  const earlier = answering && !history.some((e) => e.here && !e.answer && !e.failed);

  return (
    <div>
      {history.length === 0 && !children && (
        <div className="flex flex-col items-center py-8 text-center">
          <div className="flex size-12 items-center justify-center rounded-full bg-muted">
            <MessageCircleQuestion className="size-5" />
          </div>
          <p className="mt-4 font-medium">Ask about this meeting</p>
          <p className="mt-1 max-w-sm text-sm text-muted-foreground">
            {here
              ? `This meeting is private, so ${here} on this PC answers: nothing is sent, and it takes a minute or so. It cites the moments, so you can jump straight to them.`
              : "Claude answers from the transcript and cites the moments, so you can jump straight to them."}
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
      <div className="space-y-4" role="log" aria-label="Questions and answers" aria-live="polite">
        {history.map((exchange) => (
          <div key={exchange.id} className="space-y-2">
            <div className="ml-auto w-fit max-w-[80%] rounded-2xl rounded-br-sm bg-foreground px-3.5 py-2 text-sm text-background [overflow-wrap:anywhere]">
              {exchange.question}
            </div>
            <div className="max-w-[90%] rounded-2xl rounded-bl-sm border bg-card px-3.5 py-2.5 [overflow-wrap:anywhere]">
              {exchange.answer ? (
                <>
                  <Answer
                    text={exchange.answer}
                    // A moment of another meeting ([M12 04:31]) isn't one to seek to here.
                    onCite={(other, at) => (other == null || other === meetingId) && at != null && onSeek(at)}
                  />
                  {exchange.cost != null && (
                    <div className="mt-1.5 text-[10px] text-muted-foreground">
                      {exchange.model ? costLabel(exchange.model, exchange.cost, 4) : null}
                    </div>
                  )}
                </>
              ) : exchange.failed ? (
                <span className="text-sm text-muted-foreground">No answer.</span>
              ) : (
                <span className="flex items-center gap-2 text-sm text-muted-foreground">
                  <Loader2 className="size-3.5 animate-spin" />
                  Reading the transcript...
                  {exchange.here && stop}
                </span>
              )}
            </div>
          </div>
        ))}
      </div>
      {earlier && (
        <p role="status" className="mt-4 flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="size-3.5 animate-spin" />
          The model on this PC is still on a question asked before.
          {stop}
        </p>
      )}
      {children || (
        <form
          className="sticky bottom-0 mt-5 flex gap-2 bg-background pb-1"
          onSubmit={(event) => {
            event.preventDefault();
            void ask(question);
          }}
        >
          <Input
            aria-label="Your question about this meeting"
            placeholder="Ask anything about this meeting"
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            className="h-10"
          />
          <Button
            type="submit"
            size="icon"
            className="size-10 shrink-0"
            aria-label="Ask"
            disabled={busy || earlier || !question.trim()}
          >
            {busy ? <Loader2 className="size-4 animate-spin" /> : <ArrowUp className="size-4" />}
          </Button>
        </form>
      )}
    </div>
  );
});
