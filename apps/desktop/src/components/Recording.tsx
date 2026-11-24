import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { Bookmark, Camera, Captions, Circle, Megaphone, Mic, Pause, Play, Square, Timer, TriangleAlert } from "lucide-react";
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
import { newEvents, recordPath, type EventCursor } from "@/lib/callEvents";
import { api, isTauri, type CallEvent, type RecordState } from "@/lib/engine";
import { announce, copyText, notify, windowInFront } from "@/lib/feedback";
import { clock, levelPercent } from "@/lib/format";
import { keysFor, useKeys } from "@/lib/shortcuts";
import { cn } from "@/lib/utils";

/** The engine's countdown to stop on its own: the call ended, or nobody was heard for ten
 * minutes. The engine stops the recording itself (a hidden window's timers run late). */
type AutoStop = { reason: "call" | "quiet"; in: number };

/** An automatic recording can be undone this long: past it, Stop keeps what was recorded. */
const DECLINE_WITHIN = 120;

/** Runs ``show`` (a toast) only when the window is in front. */
async function inFront(show: () => unknown) {
  if (await windowInFront()) show();
}

/** Settings tells the rest of the page when something it reads changed (the shortcuts). */
export const SETTINGS_CHANGED = "zachcap:settings-changed";

/** Whether the user is in the middle of something on screen: a dialog, or typing (a line
 * being fixed, a note). A recording that stops on its own doesn't take them away from it. */
function busyOnScreen(): boolean {
  if (document.querySelector("[role=dialog], [role=alertdialog]")) return true;
  return document.activeElement?.matches("input, textarea, select, [contenteditable=true]") ?? false;
}

/** To paste in the meeting chat: recording laws differ, and people should know. */
const HEADS_UP =
  "Heads-up: I'm recording this call to take notes. The recording stays on my computer and isn't shared.";

type RecordingContext = {
  state: RecordState | undefined;
  start: (title: string) => Promise<void>;
  /** Stops and saves, then opens the meeting. */
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
  /** Don't record: deletes a recording ZachCap started on its own for a call. */
  decline: () => Promise<void>;
};

const Context = createContext<RecordingContext | null>(null);

export function RecordingProvider({ children, onSaved }: { children: ReactNode; onSaved: (meetingId: number) => void }) {
  const [fast, setFast] = useState(false);
  // How far the page has said what the engine did on its own (asked for from there on).
  const cursor = useRef<EventCursor | null>(null);
  const { data: state, refresh } = usePoll(
    async () => {
      const next = await api<RecordState>(recordPath(cursor.current));
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

  // A note typed but not sent yet belongs to this meeting, not the next one.
  const saveDraft = useCallback(async () => {
    const typed = draftNow.current.trim();
    if (!typed) return;
    try {
      await api("/api/record/note", { method: "POST", json: { text: typed, kind: "note" } });
      setDraft("");
    } catch (error) {
      void announce("Couldn't save the note you were typing", String(error), "error");
    }
  }, [setDraft]);

  /** A recording saved: open it, unless it stopped on its own while something on screen is
   * being done (a dialog, a line being fixed); then it's offered instead. Stopped on its own,
   * the shell has already said so with a Windows notification if the window was behind. */
  const opened = useCallback(
    (meetingId: number, why: string | null) => {
      if (why && busyOnScreen()) {
        toast.success("Recording saved", {
          description: `${why} Transcribing and finding speakers...`,
          duration: 20_000,
          action: { label: "Open", onClick: () => onSaved(meetingId) },
        });
        return;
      }
      if (why) void inFront(() => toast.success("Recording saved", { description: `${why} Transcribing and finding speakers...` }));
      else void announce("Recording saved", "Transcribing and finding speakers...", "success");
      onSaved(meetingId);
    },
    [onSaved],
  );

  const stop = useCallback(async () => {
    await saveDraft();
    const { meeting_id } = await api<{ meeting_id: number }>("/api/record/stop", { method: "POST" });
    await refresh();
    opened(meeting_id, null);
    return meeting_id;
  }, [refresh, saveDraft, opened]);

  const busy = useRef(false);
  const toggle = useCallback(async () => {
    if (busy.current) return;
    busy.current = true;
    try {
      const current = await api<RecordState>("/api/record");
      if (current.recording) await stop();
      else {
        await start("");
        void announce("Recording started", `Press ${keysFor("record")} again to stop.`);
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

  // The engine stops a recording on its own when the call ends (the call app let go of the
  // mic) or after ten quiet minutes in a room, after a short countdown that Keep recording
  // calls off. As it starts, a note being typed is saved, and the countdown is said: as a
  // Windows notification when the window is behind the call, so there's time to keep it.
  const autoStop = state?.recording ? (state.stopping ?? null) : null;
  const countdown = autoStop?.reason ?? null;
  const autoStopNow = useRef(autoStop);
  autoStopNow.current = autoStop;
  useEffect(() => {
    if (!countdown) return;
    void saveDraft();
    const seconds = Math.max(1, Math.ceil(autoStopNow.current?.in ?? 0));
    void announce(
      countdown === "call" ? "The call ended" : "Nothing heard for ten minutes",
      `The recording stops in ${seconds} seconds unless you press Keep recording in ZachCap.`,
    );
  }, [countdown, saveDraft]);
  // The buttons act on the recording they were shown for, never one started since.
  const meetingId = state?.recording ? state.meeting_id : null;
  const keepRecording = useCallback(() => {
    void api("/api/record/keep", { method: "POST", json: { meeting_id: meetingId } })
      .then(() => refresh())
      .catch((error) => void announce("Couldn't keep recording", String(error), "error"));
  }, [refresh, meetingId]);
  const decline = useCallback(async () => {
    try {
      await api("/api/record/decline", { method: "POST", json: { meeting_id: meetingId } });
      setDraft("");
      await refresh();
    } catch (error) {
      void announce("Couldn't delete the recording", String(error), "error");
    }
  }, [refresh, setDraft, meetingId]);

  // What the engine did on its own: said once each, oldest first, as a Windows notification
  // when the window is behind the call. What happened before this page loaded is old news;
  // all that an engine started again did is news, though it numbers its events from 1 again.
  const boot = state?.boot;
  const events = state?.events;
  useEffect(() => {
    const { cursor: next, fresh } = newEvents(cursor.current, boot, events);
    cursor.current = next;
    for (const event of fresh) said(event);
  }, [boot, events, opened]);
  // Windows notifications about calls come from the shell, which never runs late; the
  // window says it too when it's in front.
  function said(event: CallEvent) {
    const app = event.app ?? "The call app";
    if (event.kind === "started") {
      void inFront(() => toast(`Recording your ${app} call`, { description: "It stops when the call ends. Not one to keep? Press Don't record." }));
    } else if (event.kind === "stopped" && event.meeting_id !== null) {
      opened(event.meeting_id, event.reason === "quiet" ? "It stopped after ten quiet minutes." : "It stopped when the call ended.");
    } else if (event.kind === "failed") {
      void inFront(() => toast.error(`Couldn't record the ${app} call`, { description: event.detail ?? "" }));
    } else if (event.kind === "declined") {
      toast.success("Deleted", { description: `This ${app} call won't be recorded.` });
    } else if (event.kind === "discarded") {
      toast("Not kept", { description: `${app} used the microphone briefly, or nobody spoke: nothing was kept.` });
    }
  }

  // The tray menu and the global shortcuts (registered by the Rust shell) work even when
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
      else void announce("Not recording", `Bookmarks mark a moment of a recording. Press ${keysFor("record")} to start one.`);
    });
    on("snapshot", () => {
      if (recordingNow.current) void snapshot();
      else void announce("Not recording", `Snapshots go with a recording. Press ${keysFor("record")} to start one.`);
    });
    return () => {
      cancelled = true;
      stops.forEach((stop) => stop());
    };
    // Read from a ref, so starting or stopping doesn't re-subscribe (and drop a key press).
  }, [toggle, note, snapshot]);

  return (
    <Context.Provider value={{ state, start, stop, toggle, note, pause, snapshot, draft, setDraft, autoStop, keepRecording, decline }}>
      {children}
    </Context.Provider>
  );
}

/** Offers to record when an app starts using the microphone (no Teams integration needed).
 * The call apps chosen in Settings start a recording on their own (the engine does that), so
 * only the others are offered: a call in a web browser, say. */
export function CallWatcher() {
  const { state, start } = useRecording();
  const prompted = useRef<string>("");
  const recording = state?.recording ?? false;
  usePoll(async () => {
    const using = await api<{ apps: string[]; auto?: string[] }>("/api/call");
    const apps = using.apps.filter((app) => !(using.auto ?? []).includes(app));
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
      await notify(`${apps[0]} is using your microphone`, `Press ${keysFor("record")} to record this call.`);
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
  const shortcutKeys = useKeys();
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
        {!recording && <span className="ml-auto text-[10px] opacity-60">{shortcutKeys.record}</span>}
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
  const { state, stop, note, pause, snapshot, draft, setDraft, autoStop, keepRecording, decline } = useRecording();
  const [stopping, setStopping] = useState(false);
  const saving = useRef(false);
  const shortcutKeys = useKeys();
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
            title={`Bookmark this moment (${shortcutKeys.bookmark})`}
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
            title={`Snapshot the call: a picture and the text in it (${shortcutKeys.snapshot})`}
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
              <span className="font-mono tabular-nums">{Math.max(0, Math.ceil(autoStop.in))}</span> s.
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
          {state.live && isTauri() && (
            <Button
              size="icon"
              variant="ghost"
              className="size-8"
              title="Show the live panel: the call as it happens, over the call"
              aria-label="Show the live panel"
              onClick={() => void invoke("show_live_panel").catch(() => undefined)}
            >
              <Captions className="size-4" />
            </Button>
          )}
          {state.auto?.meeting_id === state.meeting_id && state.elapsed < DECLINE_WITHIN && (
            <Button
              size="sm"
              variant="outline"
              title={`Started on its own when ${state.auto.app} took your mic. Deletes it; this call isn't recorded.`}
              onClick={() => void decline()}
            >
              Don't record
            </Button>
          )}
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
