import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { Bookmark, Circle, Megaphone, Mic, Pause, Play, Square, TriangleAlert } from "lucide-react";
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
import { api, isTauri, type RecordState } from "@/lib/engine";
import { announce, copyText, notify, windowShowing } from "@/lib/feedback";
import { clock, levelPercent } from "@/lib/format";
import { cn } from "@/lib/utils";

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

  const stop = useCallback(async () => {
    const { meeting_id } = await api<{ meeting_id: number }>("/api/record/stop", { method: "POST" });
    await refresh();
    void announce("Recording saved", "Transcribing and finding speakers...", "success");
    onSaved(meeting_id);
    return meeting_id;
  }, [refresh, onSaved]);

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

  // The tray icon's tooltip says when a recording is running, even with the window closed.
  const recording = state?.recording ?? false;
  useEffect(() => {
    if (isTauri()) void invoke("set_recording_indicator", { recording }).catch(() => undefined);
  }, [recording]);

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
      if (recording) void note();
      else void announce("Not recording", "Bookmarks mark a moment of a recording. Press Ctrl+Alt+R to start one.");
    });
    return () => {
      cancelled = true;
      stops.forEach((stop) => stop());
    };
  }, [toggle, note, recording]);

  return <Context.Provider value={{ state, start, stop, toggle, note, pause }}>{children}</Context.Provider>;
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
    if (!(await windowShowing())) {
      // Toasts wait in a hidden window; a Windows notification is seen right away.
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
      <div className="flex h-2 w-32 gap-px">
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

export function RecordingBar() {
  const { state, stop, note, pause } = useRecording();
  const [stopping, setStopping] = useState(false);
  const [text, setText] = useState("");
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

  return (
    <div className={cn("flex select-none items-center gap-4 border-b px-5 py-2.5", paused ? "bg-amber-500/5" : "bg-red-500/5")}>
      <div className="flex shrink-0 items-center gap-2">
        <span className="relative flex size-2.5">
          {!paused && <span className="absolute inline-flex size-full animate-ping rounded-full bg-red-500 opacity-60" />}
          <span className={cn("relative inline-flex size-2.5 rounded-full", paused ? "bg-amber-500" : "bg-red-500")} />
        </span>
        <span className="max-w-48 truncate text-sm font-medium">{paused ? "Paused" : state.title || "Recording"}</span>
        <span className="font-mono text-sm tabular-nums text-muted-foreground">{clock(state.elapsed)}</span>
      </div>
      <div className={cn("flex shrink-0 flex-col gap-1", paused && "opacity-40")}>
        <Meter label="You" dbfs={state.levels.mic} />
        <Meter label="Meeting" dbfs={state.levels.system} />
      </div>
      <form
        className="flex min-w-40 flex-1 items-center gap-1.5"
        onSubmit={async (event) => {
          event.preventDefault();
          if (text.trim() && (await note(text.trim()))) setText("");
        }}
      >
        <Input
          aria-label="Note at this moment"
          placeholder="Type a note, press Enter"
          value={text}
          onChange={(event) => setText(event.target.value)}
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
        {(state.notes ?? 0) > 0 && (
          <span className="shrink-0 text-xs tabular-nums text-muted-foreground" title="Notes and bookmarks so far">
            {state.notes}
          </span>
        )}
      </form>
      {state.errors && state.errors.length > 0 && (
        <span className="flex min-w-0 items-center gap-1.5 text-xs text-destructive" title={state.errors.join("\n")}>
          <TriangleAlert className="size-3.5 shrink-0" />
          <span className="truncate">{state.errors[0]}</span>
        </span>
      )}
      <Button
        size="icon"
        variant="ghost"
        className="size-8 shrink-0"
        title="Copy a heads-up to paste in the meeting chat"
        aria-label="Copy a heads-up that you're recording"
        onClick={() => void copyText(HEADS_UP, "Heads-up copied: paste it in the meeting chat")}
      >
        <Megaphone className="size-4" />
      </Button>
      <Button
        size="sm"
        variant="outline"
        className="shrink-0"
        title={paused ? "Resume recording" : "Pause privately: nothing is recorded until you resume"}
        onClick={() => void pause(!paused)}
      >
        {paused ? <Play className="size-3.5" /> : <Pause className="size-3.5" />}
        {paused ? "Resume" : "Pause"}
      </Button>
      <Button size="sm" variant="destructive" className="shrink-0" disabled={stopping} onClick={() => void finish()}>
        <Square className="size-3.5 fill-current" />
        {stopping ? "Saving..." : "Stop"}
      </Button>
    </div>
  );
}
