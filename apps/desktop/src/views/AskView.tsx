import { ArrowUp, Loader2, MessagesSquare, RotateCcw } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Answer } from "@/components/Answer";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import type { LibraryAsk } from "@/hooks/useLibraryAsk";
import { usePoll } from "@/hooks/usePoll";
import { api, type Person } from "@/lib/engine";
import { modelLabel } from "@/lib/writer";

/** Questions about every meeting. Claude searches the transcripts, summaries and action
 * items with read-only tools and cites the moments, which open the meeting. */
export function AskView({ library, onOpen }: { library: LibraryAsk; onOpen: (meetingId: number, at?: number) => void }) {
  const { history, busy, ask, reset } = library;
  const [question, setQuestion] = useState("");
  const { data: people } = usePoll(() => api<Person[]>("/api/people"), 0);
  const bottom = useRef<HTMLDivElement>(null);

  const submit = (text: string) => {
    if (!text.trim() || busy) return;
    if (ask(text)) setQuestion("");
  };

  useEffect(() => {
    // A block body: newer WebView2 builds return a Promise from scrollIntoView, and an
    // effect must not return anything but a cleanup function.
    void bottom.current?.scrollIntoView({ block: "end", behavior: "smooth" });
  }, [history.length, busy]);

  const names = (people ?? []).filter((p) => !p.is_me && p.meetings > 0 && !p.private).map((p) => p.name);
  const starters = [
    "What did I commit to this week?",
    names[0] ? `What's still open with ${names[0]}?` : "Which action items are still open?",
    "What did we decide in the last few meetings?",
    names[1] ? `Brief me on ${names[1]} before our next meeting.` : "Summarize this week's meetings.",
  ];

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <ScrollArea className="min-h-0 flex-1">
        <div className="mx-auto max-w-3xl px-8 py-8">
          <div className="flex items-start gap-3">
            <div className="min-w-0 flex-1">
              <h1 className="font-heading text-2xl font-semibold tracking-tight">Ask</h1>
              <p className="mt-1 text-sm text-muted-foreground">
                Questions about every meeting, except private ones. Claude searches your transcripts, summaries and
                action items (text only) and links each answer to the moment it came from.
              </p>
            </div>
            {history.length > 0 && (
              <Button size="sm" variant="outline" disabled={busy} onClick={reset}>
                <RotateCcw className="size-3.5" />
                New conversation
              </Button>
            )}
          </div>

          {history.length === 0 && (
            <div className="mt-14 flex flex-col items-center text-center">
              <div className="flex size-12 items-center justify-center rounded-full bg-muted">
                <MessagesSquare className="size-5" />
              </div>
              <p className="mt-4 font-medium">Ask about anything anyone said</p>
              <p className="mt-1 max-w-sm text-sm text-muted-foreground">
                Commitments, decisions, what someone asked for, what's still open. Follow-up questions keep the context.
              </p>
              <div className="mt-5 flex max-w-xl flex-wrap justify-center gap-2">
                {starters.map((starter) => (
                  <Button key={starter} size="sm" variant="outline" onClick={() => submit(starter)}>
                    {starter}
                  </Button>
                ))}
              </div>
            </div>
          )}

          {/* A log: screen readers announce each question and answer as it arrives. */}
          <div className="mt-6 space-y-5" role="log" aria-label="Questions and answers" aria-live="polite">
            {history.map((exchange) => (
              <div key={exchange.id} className="space-y-2">
                <div className="ml-auto w-fit max-w-[80%] rounded-2xl rounded-br-sm bg-foreground px-3.5 py-2 text-sm text-background [overflow-wrap:anywhere]">
                  {exchange.question}
                </div>
                <div className="max-w-[92%] rounded-2xl rounded-bl-sm border bg-card px-4 py-3 [overflow-wrap:anywhere]">
                  {exchange.answer ? (
                    <>
                      <Answer
                        text={exchange.answer}
                        meetings={exchange.meetings}
                        // A bare [04:31] names no meeting here, so it isn't a link.
                        localTimes={false}
                        onCite={(meetingId, at) => meetingId != null && onOpen(meetingId, at ?? undefined)}
                      />
                      <div className="mt-2 text-[10px] text-muted-foreground">
                        {exchange.model ? modelLabel(exchange.model) : ""}
                        {exchange.cost != null ? ` \u00b7 $${exchange.cost.toFixed(3)}` : ""}
                      </div>
                    </>
                  ) : exchange.failed ? (
                    <span className="text-sm text-muted-foreground">No answer.</span>
                  ) : (
                    <span className="flex items-center gap-2 text-sm text-muted-foreground">
                      <Loader2 className="size-3.5 animate-spin" />
                      Searching your meetings...
                    </span>
                  )}
                </div>
              </div>
            ))}
          </div>
          <div ref={bottom} />
        </div>
      </ScrollArea>
      <form
        className="border-t bg-background px-8 py-3"
        onSubmit={(event) => {
          event.preventDefault();
          submit(question);
        }}
      >
        <div className="mx-auto flex max-w-3xl gap-2">
          <Input
            autoFocus
            aria-label="Your question"
            placeholder="Ask about any meeting: who, what, when, what's still open"
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            className="h-10"
          />
          <Button type="submit" size="icon" className="size-10 shrink-0" aria-label="Ask" disabled={busy || !question.trim()}>
            {busy ? <Loader2 className="size-4 animate-spin" /> : <ArrowUp className="size-4" />}
          </Button>
        </div>
      </form>
    </div>
  );
}
