import { getCurrentWindow } from "@tauri-apps/api/window";
import { ArrowDown, Info, Loader2, Sparkles, TriangleAlert, X } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState, type MouseEvent } from "react";

import { Button } from "@/components/ui/button";
import { api, connect, type LiveState } from "@/lib/engine";
import { reason } from "@/lib/feedback";
import { clock } from "@/lib/format";
import {
  EMPTY_VIEW,
  atEnd,
  catchUpBy,
  dot,
  elapsedNow,
  livePath,
  mergeLines,
  notices,
  speaker,
  type CatchUp,
  type LiveView,
} from "@/lib/live";
import { cn } from "@/lib/utils";

/** How often the panel asks for new lines. */
const ASK_EVERY = 1000;

/** Moves the window by its title bar (it has no frame of its own), but not from its buttons.
 * Dragged by hand rather than with data-tauri-drag-region, whose double click would
 * maximize the panel over the call. */
function drag(event: MouseEvent) {
  if (event.button !== 0 || event.detail !== 1 || (event.target as HTMLElement).closest("button")) return;
  void getCurrentWindow()
    .startDragging()
    .catch(() => undefined);
}

/** The recording's clock, ticking between the engine's answers. */
function Clock({ elapsed, saidAt, unreachable }: { elapsed: number | null; saidAt: number; unreachable: string | null }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(id);
  }, []);
  const seconds = elapsedNow(elapsed, saidAt, now, unreachable);
  if (seconds === null) return null;
  return <span className="font-mono text-xs tabular-nums text-muted-foreground">{clock(seconds)}</span>;
}

/** The live panel: a small window of its own over the call while a recording runs, left out
 * of screen shares and screenshots (the shell opens and closes it). It shows what's said a
 * moment after each person stops talking. Its X closes it for this recording; the recording
 * bar's Captions button brings it back. */
export function LivePanel() {
  const [view, setView] = useState<LiveView>(EMPTY_VIEW);
  const [state, setState] = useState<LiveState | null>(null);
  const [saidAt, setSaidAt] = useState(() => Date.now());
  const [unreachable, setUnreachable] = useState<string | null>(null);
  const viewNow = useRef(view);
  // What Catch me up said, or why it couldn't; kept until closed or asked again.
  const [caughtUp, setCaughtUp] = useState<CatchUp | null>(null);
  const [catchError, setCatchError] = useState<string | null>(null);
  const [catching, setCatching] = useState(false);
  const catchMeUp = async () => {
    setCatching(true);
    setCatchError(null);
    try {
      setCaughtUp(await api<CatchUp>("/api/live/catch-up", { method: "POST" }));
    } catch (error) {
      setCaughtUp(null);
      setCatchError(reason(error));
    } finally {
      setCatching(false);
    }
  };

  // Asked once a second, one request at a time, from where the panel has read to. Where the
  // engine is is asked each time too: a restarted engine has a port and a token of its own.
  useEffect(() => {
    let stopped = false;
    let timer = 0;
    const ask = async () => {
      try {
        if (!(await connect())) throw new Error("The engine is starting");
        const answer = await api<LiveState>(livePath(viewNow.current));
        if (stopped) return;
        viewNow.current = mergeLines(viewNow.current, answer);
        setView(viewNow.current);
        setState(answer);
        setSaidAt(Date.now());
        setUnreachable(null);
      } catch (error) {
        if (!stopped) setUnreachable(reason(error));
      } finally {
        if (!stopped) timer = window.setTimeout(() => void ask(), ASK_EVERY);
      }
    };
    void ask();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, []);

  // New lines keep the list at its end, unless it was scrolled up to read something; then a
  // button offers the latest.
  const list = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const [newer, setNewer] = useState(false);
  useLayoutEffect(() => {
    const box = list.current;
    if (!box) return;
    if (following.current) box.scrollTop = box.scrollHeight;
    else if (view.lines.length) setNewer(true);
  }, [view.lines]);
  const scrolled = () => {
    const box = list.current;
    if (!box) return;
    following.current = atEnd(box);
    if (following.current) setNewer(false);
  };
  const latest = () => {
    const box = list.current;
    if (!box) return;
    following.current = true;
    box.scrollTop = box.scrollHeight;
    setNewer(false);
  };

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-background text-foreground">
      <div className="flex h-9 shrink-0 cursor-default select-none items-center gap-2 border-b pr-1.5 pl-3" onMouseDown={drag}>
        <span
          className={cn(
            "size-2 shrink-0 rounded-full",
            { recording: "bg-red-500", paused: "bg-amber-500", off: "bg-muted-foreground/40" }[dot(state, unreachable)],
          )}
        />
        <span className="text-sm font-medium">Live transcript</span>
        <Clock elapsed={state?.elapsed ?? null} saidAt={saidAt} unreachable={unreachable} />
        <Button
          size="xs"
          variant="ghost"
          className="ml-auto"
          disabled={catching || view.lines.length === 0}
          title="What the call has said so far, in a few lines"
          onClick={() => void catchMeUp()}
        >
          {catching ? <Loader2 className="animate-spin" /> : <Sparkles />}
          Catch me up
        </Button>
        <Button
          size="icon-xs"
          variant="ghost"
          title="Close for this recording (the Captions button in the recording bar brings it back)"
          aria-label="Close the live panel"
          onClick={() =>
            void getCurrentWindow()
              .close()
              .catch(() => undefined)
          }
        >
          <X />
        </Button>
      </div>
      {notices(state, unreachable).map((notice) => (
        <p
          key={notice.key}
          className={cn(
            "flex shrink-0 select-none items-start gap-1.5 border-b px-3 py-1.5 text-xs",
            notice.tone === "error" && "bg-destructive/10 text-destructive",
            notice.tone === "warning" && "bg-amber-500/10 text-amber-700 dark:text-amber-300",
            notice.tone === "info" && "text-muted-foreground",
          )}
        >
          {notice.tone === "info" ? (
            <Info className="mt-px size-3.5 shrink-0" />
          ) : (
            <TriangleAlert className="mt-px size-3.5 shrink-0" />
          )}
          <span className="min-w-0">{notice.text}</span>
        </p>
      ))}
      {(caughtUp || catchError) && (
        <div className="max-h-[45%] shrink-0 overflow-y-auto border-b bg-muted/40 px-3 py-2">
          <div className="flex items-start gap-2">
            <p className={cn("min-w-0 flex-1 text-sm leading-snug whitespace-pre-wrap", catchError && "text-destructive")}>
              {catchError ?? caughtUp?.text}
            </p>
            <Button
              size="icon-xs"
              variant="ghost"
              aria-label="Close what Catch me up said"
              onClick={() => {
                setCaughtUp(null);
                setCatchError(null);
              }}
            >
              <X />
            </Button>
          </div>
          {caughtUp && !catchError && <p className="mt-1 text-xs text-muted-foreground">{catchUpBy(caughtUp)}</p>}
        </div>
      )}
      <div className="relative min-h-0 flex-1">
        <div ref={list} onScroll={scrolled} className="h-full overflow-y-auto px-3 py-2" aria-live="polite">
          {view.lines.length === 0 ? (
            <p className="px-4 py-8 text-center text-sm text-muted-foreground">
              Lines appear a moment after each person stops talking.
            </p>
          ) : (
            view.lines.map((line) => (
              <div key={line.id} className="py-1">
                <div className="flex items-baseline gap-2 text-xs">
                  <span className={cn("font-medium", line.track === "mic" ? "text-foreground" : "text-sky-700 dark:text-sky-300")}>
                    {speaker(line, state?.me)}
                  </span>
                  <span className="font-mono tabular-nums text-muted-foreground">{clock(line.start)}</span>
                </div>
                <p className="text-sm leading-snug break-words whitespace-pre-wrap">{line.text}</p>
              </div>
            ))
          )}
        </div>
        {newer && (
          <Button size="xs" variant="secondary" className="absolute bottom-2 left-1/2 -translate-x-1/2 shadow-sm" onClick={latest}>
            <ArrowDown />
            Latest
          </Button>
        )}
      </div>
    </div>
  );
}
