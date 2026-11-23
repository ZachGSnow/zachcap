import { listen } from "@tauri-apps/api/event";
import { Circle, Mic, Square } from "lucide-react";
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
import { clock, levelPercent } from "@/lib/format";


type RecordingContext = {
  state: RecordState | undefined;
  start: (title: string) => Promise<void>;
  stop: () => Promise<number | undefined>;
  toggle: () => Promise<void>;
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
    toast.success("Recording saved", { description: "Transcribing and finding speakers..." });
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
        toast("Recording started", { description: "Press Ctrl+Alt+R again to stop." });
      }
    } catch (error) {
      toast.error("Recording didn't change", { description: String(error) });
    } finally {
      busy.current = false;
    }
  }, [start, stop]);

  // The tray menu and Ctrl+Alt+R (both registered by the Rust shell) toggle recording,
  // even when the window is hidden in the tray.
  useEffect(() => {
    if (!isTauri()) return;
    let unlisten: (() => void) | undefined;
    let cancelled = false;
    void listen("toggle-recording", () => void toggle()).then((stop) => {
      if (cancelled) stop();
      else unlisten = stop;
    });
    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, [toggle]);

  return <Context.Provider value={{ state, start, stop, toggle }}>{children}</Context.Provider>;
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
    if (recording || prompted.current === key) return;
    prompted.current = key;
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
  const { state, stop } = useRecording();
  const [stopping, setStopping] = useState(false);
  if (!state?.recording) return null;

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
    <div className="flex select-none items-center gap-5 border-b bg-red-500/5 px-5 py-2.5">
      <div className="flex items-center gap-2">
        <span className="relative flex size-2.5">
          <span className="absolute inline-flex size-full animate-ping rounded-full bg-red-500 opacity-60" />
          <span className="relative inline-flex size-2.5 rounded-full bg-red-500" />
        </span>
        <span className="text-sm font-medium">{state.title || "Recording"}</span>
        <span className="font-mono text-sm tabular-nums text-muted-foreground">{clock(state.elapsed)}</span>
      </div>
      <div className="flex flex-col gap-1">
        <Meter label="You" dbfs={state.levels.mic} />
        <Meter label="Meeting" dbfs={state.levels.system} />
      </div>
      <Button size="sm" variant="destructive" className="ml-auto" disabled={stopping} onClick={() => void finish()}>
        <Square className="size-3.5 fill-current" />
        {stopping ? "Saving..." : "Stop"}
      </Button>
    </div>
  );
}
