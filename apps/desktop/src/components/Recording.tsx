import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { Bookmark, Camera, Circle, Megaphone, Mic, Pause, Play, Square, Timer, TriangleAlert } from "lucide-react";
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { toast } from "sonner";

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
import { Label } from "@/components/ui/label";
import { usePoll } from "@/hooks/usePoll";
import { api, isTauri, type RecordState, type Settings } from "@/lib/engine";
import { announce, copyText, notify, windowInFront } from "@/lib/feedback";
import { clock, levelPercent } from "@/lib/format";
import { cn } from "@/lib/utils";

/** Quiet this long after the call app lets go of the mic: the call ended. */
const CALL_END_SETTLE = 10;
/** Nobody heard this long, with no call app: an in-room meeting that ended. */
const QUIET_CHECK_IN = 600;

/** A countdown to stop: the call ended, or it has been quiet for ten minutes. */
type AutoStop = { reason: "call" | "quiet"; deadline: number };

/** To paste in the meeting chat: recording laws differ, and people should know. */
const HEADS_UP =
  "Heads-up: I'm recording this call to take notes. The recording stays on my computer and isn't shared.";

type RecordingContext = {
  state: RecordState | undefined;
  start: (title: string) => Promise<void>;
  stop: () => Promise<number | undefined>;
  toggle: () => Promise<void>;
  /** A note, or with no text a bookmark, at this moment of the recording. */
  note: (text?: string) => Promise<boolean>;
  pause: (paused: boolean) => Promise<void>;
  /** A picture of the call window and the text in it, as a note at this moment. */
  snapshot: () => Promise<void>;
  /** The note being typed in the recording bar. Stopping saves it into this meeting. */
  draft: string;
  setDraft: (next: string | ((current: string) => string)) => void;
  /** Counting down to stop on its own; Keep recording cancels it. */
  autoStop: AutoStop | null;
  keepRecording: () => void;
};

const Context = createContext<RecordingContext | null>(null);

export function RecordingProvider({ children, onSaved }: { children: ReactNode; onSaved: (meetingId: number) => void }) {
  const [fast, setFast] = useState(false);
  const { data: state, refresh } = usePoll(
    async () => {
      const next = await api<RecordState>("/api/record");
      setFast(next.recording);
      return next;
    },
    fast ? 250 : 1500,
  );

  const start = useCallback(
    async (title: string) => {
      await api("/api/record/start", { method: "POST", json: { title: title || null } });
      setFast(true);
      await refresh();
    },
    [refresh],
  );

  const [draft, setDraftState] = useState("");
  const draftNow = useRef("");
  const setDraft = useCallback((next: string | ((current: string) => string)) => {
    const text = typeof next === "function" ? next(draftNow.current) : next;
    draftNow.current = text;
    setDraftState(text);
  }, []);

  const stop = useCallback(async () => {
    // A note typed but not sent yet belongs to this meeting, not the next one.
    const typed = draftNow.current.trim();
    if (typed) {
      try {
        await api("/api/record/note", { method: "POST", json: { text: typed, kind: "note" } });
        setDraft("");
      } catch (error) {
        void announce("Couldn't save the note you were typing", String(error), "error");
      }
    }
    const { meeting_id } = await api<{ meeting_id: number }>("/api/record/stop", { method: "POST" });
    await refresh();
    void announce("Recording saved", "Transcribing and finding speakers...", "success");
    onSaved(meeting_id);
    return meeting_id;
  }, [refresh, onSaved, setDraft]);

  const busy = useRef(false);
  const toggle = useCallback(async () => {
    if (busy.current) return;
    busy.current = true;
    try {
      const current = await api<RecordState>("/api/record");
      if (current.recording) await stop();
      else {
        await start("");
        void announce("Recording started", "Press Ctrl+Alt+R again to stop.");
      }
    } catch (error) {
      // From the tray or the hotkey the window may be hidden: say it where it will be seen.
      void announce("Recording didn't change", String(error), "error");
    } finally {
      busy.current = false;
    }
  }, [start, stop]);

  const note = useCallback(
    async (text = "") => {
      try {
        const saved = await api<{ at: number }>("/api/record/note", {
          method: "POST",
          json: { text, kind: text.trim() ? "note" : "bookmark" },
        });
        void announce(text.trim() ? "Note saved" : "Bookmarked", `At ${clock(saved.at)} in the recording.`, "success");
        await refresh();
        return true;
      } catch (error) {
        void announce(text.trim() ? "Couldn't save the note" : "Couldn't bookmark", String(error), "error");
        return false;
      }
    },
    [refresh],
  );

  const pause = useCallback(
    async (paused: boolean) => {
      try {
        await api("/api/record/pause", { method: "POST", json: { paused } });
        await refresh();
      } catch (error) {
        void announce(paused ? "Couldn't pause" : "Couldn't resume", String(error), "error");
      }
    },
    [refresh],
  );

  // A picture of the call window and the text in it, as a note at this moment.
  const snapshot = useCallback(async () => {
    try {
      const shot = await api<{ at: number; lines: number; window: string }>("/api/record/snapshot", { method: "POST" });
      const read = shot.lines ? `${shot.lines} line${shot.lines === 1 ? "" : "s"} of text` : "no text";
      void announce("Snapshot saved", `${shot.window}, at ${clock(shot.at)}: ${read}.`, "success");
      await refresh();
    } catch (error) {
      void announce("Couldn't take a snapshot", String(error), "error");
    }
  }, [refresh]);

  // The tray icon's tooltip says when a recording is running, even with the window closed.
  const recording = state?.recording ?? false;
  const recordingNow = useRef(recording);
  useEffect(() => {
    recordingNow.current = recording;
    if (isTauri()) void invoke("set_recording_indicator", { recording }).catch(() => undefined);
    // A half-typed note never carries over into the next recording.
    if (!recording) setDraft("");
  }, [recording, setDraft]);

  // The engine's warnings (a muted mic, no call audio, a nearly full disk), each said once
  // while it lasts: as a Windows notification when the window is behind the call.
  const warned = useRef(new Set<string>());
  useEffect(() => {
    const present = new Set(state?.recording ? (state.warnings ?? []).map((w) => w.key) : []);
    for (const key of warned.current) if (!present.has(key)) warned.current.delete(key);
    for (const warning of state?.recording ? (state.warnings ?? []) : []) {
      if (warned.current.has(warning.key)) continue;
      warned.current.add(warning.key);
      void announce("Check the recording", warning.text, "warning");
    }
  }, [state]);

  // Stop when the call ends (the call app let go of the mic and it went quiet), or after ten
  // quiet minutes with no call app (an in-room meeting). Always a countdown that Keep
  // recording cancels, so a lull in the conversation never ends a meeting.
  const [stopWhenCallEnds, setStopWhenCallEnds] = useState(true);
  useEffect(() => {
    if (!recording) return;
    void api<Settings>("/api/settings")
      .then((settings) => setStopWhenCallEnds(settings.stop_when_call_ends))
      .catch(() => undefined);
  }, [recording]);
  const [autoStop, setAutoStop] = useState<AutoStop | null>(null);
  // "Keep recording" holds until things change: the call app is back, or someone spoke.
  const kept = useRef({ call: false, quiet: false });
  useEffect(() => {
    if (!state?.recording) {
      kept.current = { call: false, quiet: false };
      setAutoStop(null);
      return;
    }
    const ended = state.call?.ended_for ?? null;
    const quiet = state.quiet_for ?? 0;
    if (ended === null) kept.current.call = false;
    if (quiet < QUIET_CHECK_IN) kept.current.quiet = false;
    let reason: AutoStop["reason"] | null = null;
    if (stopWhenCallEnds && !state.paused) {
      if (ended !== null && ended >= CALL_END_SETTLE && quiet >= CALL_END_SETTLE && !kept.current.call) reason = "call";
      else if (!state.call?.apps.length && quiet >= QUIET_CHECK_IN && !kept.current.quiet) reason = "quiet";
    }
    setAutoStop((current) =>
      reason === null ? null : current?.reason === reason ? current : { reason, deadline: Date.now() + (reason === "call" ? 15_000 : 60_000) },
    );
  }, [state, stopWhenCallEnds]);
  useEffect(() => {
    if (!autoStop) return;
    const seconds = Math.round((autoStop.deadline - Date.now()) / 1000);
    void announce(
      autoStop.reason === "call" ? "The call ended" : "Nothing heard for ten minutes",
      `The recording stops in ${seconds} seconds unless you keep it going in ZachCap.`,
    );
    const timer = window.setTimeout(() => {
      setAutoStop(null);
      void stop().catch((error) => void announce("Couldn't stop recording", String(error), "error"));
    }, Math.max(0, autoStop.deadline - Date.now()));
    return () => window.clearTimeout(timer);
  }, [autoStop, stop]);
  const keepRecording = useCallback(() => {
    if (autoStop) kept.current[autoStop.reason] = true;
    setAutoStop(null);
  }, [autoStop]);

  // The tray menu, Ctrl+Alt+R and Ctrl+Alt+B (registered by the Rust shell) work even when
  // the window is hidden in the tray.
  useEffect(() => {
    if (!isTauri()) return;
    const stops: (() => void)[] = [];
    let cancelled = false;
    const on = (event: string, handler: () => void) =>
      void listen(event, handler).then((stop) => {
        if (cancelled) stop();
        else stops.push(stop);
      });
    on("toggle-recording", () => void toggle());
    on("bookmark", () => {
      if (recordingNow.current) void note();
      else void announce("Not recording", "Bookmarks mark a moment of a recording. Press Ctrl+Alt+R to start one.");
    });
    on("snapshot", () => {
      if (recordingNow.current) void snapshot();
      else void announce("Not recording", "Snapshots go with a recording. Press Ctrl+Alt+R to start one.");
    });
    return () => {
      cancelled = true;
      stops.forEach((stop) => stop());
    };
    // Read from a ref, so starting or stopping doesn't re-subscribe (and drop a key press).
  }, [toggle, note, snapshot]);

  return (
    <Context.Provider value={{ state, start, stop, toggle, note, pause, snapshot, draft, setDraft, autoStop, keepRecording }}>
      {children}
    </Context.Provider>
  );
}

/** Offers to record when a meeting app starts using the microphone (no Teams integration needed). */
export function CallWatcher() {
  const { state, start } = useRecording();
  const prompted = useRef<string>("");
  const recording = state?.recording ?? false;
  usePoll(async () => {
    const { apps } = await api<{ apps: string[] }>("/api/call");
    const key = apps.join(",");
    if (!apps.length) {
      prompted.current = "";
      return;
    }
    if (prompted.current === key) return;
    // Recording already (or stopped while still on the call): don't offer this call again.
    prompted.current = key;
    if (recording) return;
    if (!(await windowInFront())) {
      // On a call the window is usually hidden or behind it; a notification is seen there.
      await notify(`${apps[0]} is using your microphone`, "Press Ctrl+Alt+R to record this call.");
      return;
    }
    toast(`${apps[0]} is using your microphone`, {
      description: "Looks like a call started. Record it?",
      duration: 20_000,
      action: {
        label: "Record",
        onClick: () => void start(`${apps[0]} call`).catch((error) => toast.error("Couldn't start", { description: String(error) })),
      },
    });
  }, 5000);
  return null;
}

export function useRecording() {
  const value = useContext(Context);
  if (!value) throw new Error("useRecording outside RecordingProvider");
  return value;
}

export function RecordButton() {
  const { state, start } = useRecording();
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const recording = state?.recording ?? false;

  const begin = async () => {
    try {
      await start(title.trim());
      setOpen(false);
      setTitle("");
    } catch (error) {
      toast.error("Couldn't start recording", { description: String(error) });
    }
  };

  return (
    <>
      <Button className="w-full justify-start gap-2" disabled={recording} onClick={() => setOpen(true)}>
        {recording ? <Circle className="size-3 animate-pulse fill-red-500 text-red-500" /> : <Mic className="size-4" />}
        {recording ? "Recording..." : "Record"}
        {!recording && <span className="ml-auto text-[10px] opacity-60">Ctrl+Alt+R</span>}
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Start recording</DialogTitle>
            <DialogDescription>
              Records your microphone and your computer's audio. Keep Teams on your normal mic; nothing to change there.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-2">
            <Label htmlFor="title">Meeting title (optional)</Label>
            <Input
              id="title"
              autoFocus
              placeholder="Weekly sync"
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              onKeyDown={(event) => event.key === "Enter" && void begin()}
            />
          </div>
          <p className="text-xs text-muted-foreground">
            Let people know you're recording. Some places require everyone's consent.
          </p>
          <DialogFooter>
            <Button variant="outline" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button onClick={() => void begin()}>
              <Mic className="size-4" />
              Start
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

function Meter({ label, dbfs }: { label: string; dbfs: number }) {
  const percent = levelPercent(dbfs);
  return (
    <div className="flex items-center gap-2 text-xs text-muted-foreground">
      <span className="w-14 text-right">{label}</span>
      <div className="flex h-2 w-20 gap-px">
        {Array.from({ length: 16 }, (_, i) => (
          <div
            key={i}
            className={`h-full flex-1 rounded-[1px] transition-colors duration-100 ${
              (i / 16) * 100 < percent ? (i > 12 ? "bg-red-500" : i > 9 ? "bg-amber-500" : "bg-emerald-500") : "bg-muted"
            }`}
          />
        ))}
      </div>
    </div>
  );
}

/** One row when there's room. In a narrower window the note field gets a row of its own and
 * the level meters go, so Pause and Stop always stay on screen. */
export function RecordingBar() {
  const { state, stop, note, pause, snapshot, draft, setDraft, autoStop, keepRecording } = useRecording();
  const [stopping, setStopping] = useState(false);
  const saving = useRef(false);
  if (!state?.recording) return null;
  const paused = state.paused ?? false;

  const finish = async () => {
    setStopping(true);
    try {
      await stop();
    } catch (error) {
      toast.error("Couldn't stop recording", { description: String(error) });
    } finally {
      setStopping(false);
    }
  };

  const saveNote = async () => {
    const text = draft.trim();
    if (!text || saving.current) return;
    saving.current = true;
    // Cleared right away, so a second Enter can't save it twice and the next note can be typed.
    setDraft("");
    try {
      if (!(await note(text))) setDraft((typed) => (typed ? `${text} ${typed}` : text));
    } finally {
      saving.current = false;
    }
  };

  return (
    <div className={cn("@container select-none border-b", paused ? "bg-amber-500/5" : "bg-red-500/5")}>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-5 py-2.5">
        <div className="flex min-w-0 shrink-0 items-center gap-2">
          <span className="relative flex size-2.5">
            {!paused && <span className="absolute inline-flex size-full animate-ping rounded-full bg-red-500 opacity-60" />}
            <span className={cn("relative inline-flex size-2.5 rounded-full", paused ? "bg-amber-500" : "bg-red-500")} />
          </span>
          <span className="max-w-40 truncate text-sm font-medium">{paused ? "Paused" : state.title || "Recording"}</span>
          <span className="font-mono text-sm tabular-nums text-muted-foreground">{clock(state.elapsed)}</span>
        </div>
        <div className={cn("hidden shrink-0 flex-col gap-1 @3xl:flex", paused && "opacity-40")}>
          <Meter label="You" dbfs={state.levels.mic} />
          <Meter label="Meeting" dbfs={state.levels.system} />
        </div>
        <form
          className="order-last flex basis-full items-center gap-1.5 @4xl:order-none @4xl:min-w-36 @4xl:flex-1 @4xl:basis-0"
          onSubmit={(event) => {
            event.preventDefault();
            void saveNote();
          }}
        >
          <Input
            aria-label="Note at this moment"
            placeholder="Type a note, press Enter"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            className="h-8 text-xs"
          />
          <Button
            type="button"
            size="icon"
            variant="ghost"
            className="size-8 shrink-0"
            title="Bookmark this moment (Ctrl+Alt+B)"
            aria-label="Bookmark this moment"
            onClick={() => void note()}
          >
            <Bookmark className="size-4" />
          </Button>
          <Button
            type="button"
            size="icon"
            variant="ghost"
            className="size-8 shrink-0"
            title="Snapshot the call: a picture and the text in it (Ctrl+Alt+S)"
            aria-label="Snapshot the call"
            onClick={() => void snapshot()}
          >
            <Camera className="size-4" />
          </Button>
          {(state.notes ?? 0) > 0 && (
            <span className="shrink-0 text-xs tabular-nums text-muted-foreground" title="Notes and bookmarks so far">
              {state.notes}
            </span>
          )}
        </form>
        {autoStop && (
          <div className="order-last flex basis-full items-center gap-2 rounded-md bg-amber-500/10 px-2.5 py-1.5 text-xs">
            <Timer className="size-3.5 shrink-0 text-amber-600" />
            <span className="min-w-0 flex-1">
              {autoStop.reason === "call" ? "The call ended." : "Nothing heard for ten minutes."} Stopping in{" "}
              <span className="font-mono tabular-nums">{Math.max(0, Math.ceil((autoStop.deadline - Date.now()) / 1000))}</span> s.
            </span>
            <Button size="sm" variant="outline" className="h-7" onClick={keepRecording}>
              Keep recording
            </Button>
            <Button size="sm" variant="destructive" className="h-7" disabled={stopping} onClick={() => void finish()}>
              Stop now
            </Button>
          </div>
        )}
        {(state.warnings ?? []).map((warning) => (
          <span
            key={warning.key}
            className="order-last flex min-w-0 basis-full items-center gap-1.5 text-xs text-amber-700 dark:text-amber-300"
            title={warning.text}
          >
            <TriangleAlert className="size-3.5 shrink-0" />
            <span className="truncate">{warning.text}</span>
          </span>
        ))}
        {state.errors && state.errors.length > 0 && (
          <span
            className="order-last flex min-w-0 basis-full items-center gap-1.5 text-xs text-destructive"
            title={state.errors.join("\n")}
          >
            <TriangleAlert className="size-3.5 shrink-0" />
            <span className="truncate">{state.errors[0]}</span>
          </span>
        )}
        <div className="ml-auto flex shrink-0 items-center gap-2">
          <Button
            size="icon"
            variant="ghost"
            className="size-8"
            title="Copy a heads-up to paste in the meeting chat"
            aria-label="Copy a heads-up that you're recording"
            onClick={() => void copyText(HEADS_UP, "Heads-up copied: paste it in the meeting chat")}
          >
            <Megaphone className="size-4" />
          </Button>
          <Button
            size="sm"
            variant="outline"
            title={paused ? "Resume recording" : "Pause privately: nothing is recorded until you resume"}
            onClick={() => void pause(!paused)}
          >
            {paused ? <Play className="size-3.5" /> : <Pause className="size-3.5" />}
            {paused ? "Resume" : "Pause"}
          </Button>
          <Button size="sm" variant="destructive" disabled={stopping} onClick={() => void finish()}>
            <Square className="size-3.5 fill-current" />
            {stopping ? "Saving..." : "Stop"}
          </Button>
        </div>
      </div>
    </div>
  );
}
