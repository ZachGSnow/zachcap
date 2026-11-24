import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { AudioLines, FileUp, Loader2, RotateCw, TriangleAlert } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { ErrorBoundary } from "@/components/ErrorBoundary";
import { DictationKeys } from "@/components/Dictation";
import { CallWatcher, RecordingBar, RecordingProvider } from "@/components/Recording";
import { GlobalKeys } from "@/components/Shortcuts";
import { Sidebar, type View } from "@/components/Sidebar";
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
import { useLibraryAsk } from "@/hooks/useLibraryAsk";
import { usePoll } from "@/hooks/usePoll";
import { api, connect, isTauri, waitForEngine, type Meeting, type Stats } from "@/lib/engine";
import { notify, reason } from "@/lib/feedback";
import { ActionsView } from "@/views/ActionsView";
import { AskView } from "@/views/AskView";
import { MeetingView } from "@/views/MeetingView";
import { MeetingsView } from "@/views/MeetingsView";
import { PeopleView } from "@/views/PeopleView";
import { SearchView } from "@/views/SearchView";
import { TopicsView } from "@/views/TopicsView";
import { SettingsView } from "@/views/SettingsView";
import { WorkLogView } from "@/views/WorkLogView";

const MEDIA = ["mp4", "m4a", "mp3", "wav", "flac", "ogg", "webm", "mkv", "aac", "wma", "mov"];

const briefQuestion = (name: string) =>
  `Brief me on ${name} before my next meeting with them: what we talked about recently, what's still open between us, and anything either of us promised.`;

function Startup({ error, setup, onRetry }: { error?: string; setup?: string; onRetry?: () => void }) {
  return (
    <div className="flex h-screen select-none flex-col items-center justify-center gap-3 text-center">
      <div className="flex size-12 items-center justify-center rounded-xl bg-foreground text-background">
        <AudioLines className="size-6" />
      </div>
      {error ? (
        <>
          <p className="font-medium">The ZachCap engine didn't start</p>
          <p className="max-w-md text-sm text-muted-foreground">{error}</p>
          {onRetry && (
            <Button size="sm" className="mt-2" onClick={onRetry}>
              <RotateCw className="size-3.5" />
              Try again
            </Button>
          )}
        </>
      ) : setup !== undefined ? (
        <>
          <p className="flex items-center gap-2 font-medium">
            <Loader2 className="size-4 animate-spin" />
            Setting up the speech engine
          </p>
          <p className="max-w-md text-sm text-muted-foreground">
            The first start after installing downloads Python and the speech libraries (a few GB), so it
            takes a few minutes. ZachCap opens when it's done.
          </p>
          <p className="max-w-md truncate font-mono text-xs text-muted-foreground">{setup}</p>
        </>
      ) : (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />
          Starting the local engine...
        </p>
      )}
    </div>
  );
}

/** Shown when the engine stops after startup (a crash): one click starts it again. */
function EngineStopped({ message, onRestarted }: { message: string; onRestarted: () => void }) {
  const [restarting, setRestarting] = useState(false);
  const restart = async () => {
    setRestarting(true);
    try {
      await invoke("restart_engine");
      await waitForEngine();
      toast.success("The engine is running again");
      onRestarted();
    } catch (error) {
      toast.error("Couldn't restart the engine", { description: String(error) });
    } finally {
      setRestarting(false);
    }
  };
  return (
    <div className="flex items-center gap-3 border-b bg-destructive/10 px-5 py-2.5 text-sm">
      <TriangleAlert className="size-4 shrink-0 text-destructive" />
      <span className="min-w-0 flex-1">
        <span className="font-medium">The ZachCap engine stopped.</span>{" "}
        <span className="text-muted-foreground">{message}</span>
      </span>
      <Button size="sm" disabled={restarting} onClick={() => void restart()}>
        {restarting ? <Loader2 className="size-3.5 animate-spin" /> : <RotateCw className="size-3.5" />}
        Restart engine
      </Button>
    </div>
  );
}

function ImportDialog({ path, onClose, onImported }: { path: string | null; onClose: () => void; onImported: (id: number) => void }) {
  const fileName = path?.split(/[\\/]/).pop() ?? "";
  const [title, setTitle] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setTitle(fileName.replace(/\.[^.]+$/, "").replace(/-\d{8}_\d{6}-Meeting Recording$/, ""));
    setStart("");
    setEnd("");
  }, [fileName]);

  const run = async () => {
    if (!path) return;
    setBusy(true);
    try {
      const { meeting_id } = await api<{ meeting_id: number }>("/api/import", {
        method: "POST",
        json: { path, title: title.trim() || null, start: start || null, end: end || null },
      });
      toast.success("Imported", { description: "Transcribing and finding speakers..." });
      onImported(meeting_id);
      onClose();
    } catch (error) {
      toast.error("Couldn't import", { description: reason(error) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={path !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Import a recording</DialogTitle>
          <DialogDescription className="truncate">{fileName}</DialogDescription>
        </DialogHeader>
        <div className="grid gap-3">
          <div className="grid gap-1.5">
            <Label htmlFor="import-title">Title</Label>
            <Input id="import-title" value={title} onChange={(event) => setTitle(event.target.value)} />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="grid gap-1.5">
              <Label htmlFor="import-start">Start at (optional)</Label>
              <Input id="import-start" placeholder="0:00" value={start} onChange={(event) => setStart(event.target.value)} />
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="import-end">End at (optional)</Label>
              <Input id="import-end" placeholder="end" value={end} onChange={(event) => setEnd(event.target.value)} />
            </div>
          </div>
          <p className="text-xs text-muted-foreground">
            A finished recording has everyone on one track; ZachCap tells people apart by voice. Name yourself "You"
            in the speaker cards.
          </p>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button disabled={busy} onClick={() => void run()}>
            {busy ? <Loader2 className="size-4 animate-spin" /> : <FileUp className="size-4" />}
            Import
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function Shell() {
  const [view, setView] = useState<View>({ name: "meetings" });
  const [importPath, setImportPath] = useState<string | null>(null);
  const { data: stats, error: engineError } = usePoll(() => api<Stats>("/api/stats"), 5000);
  const [engineStopped, setEngineStopped] = useState<string | null>(null);
  // Kept here, so the conversation (and an answer still on its way) survives looking at
  // a meeting it cited.
  const library = useLibraryAsk();

  useEffect(() => {
    if (!isTauri()) return;
    let unlisten: (() => void) | undefined;
    let cancelled = false;
    void listen<string>("engine-exited", (event) => setEngineStopped(event.payload)).then((stop) => {
      if (cancelled) stop();
      else unlisten = stop;
    });
    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, []);

  // Tell the user when a meeting finishes processing, even if they're looking elsewhere.
  const statuses = useRef<Map<number, string> | null>(null);
  usePoll(async () => {
    const meetings = await api<Meeting[]>("/api/meetings");
    const previous = statuses.current;
    statuses.current = new Map(meetings.map((m) => [m.id, m.status]));
    if (!previous) return;
    for (const meeting of meetings) {
      const before = previous.get(meeting.id);
      if ((before === "processing" || before === "recorded") && (meeting.status === "ready" || meeting.status === "needs_names")) {
        const body = meeting.status === "needs_names" ? "Some speakers need a name." : "Transcript ready.";
        toast.success(`${meeting.title} is ready`, {
          description: body,
          action: { label: "Open", onClick: () => setView({ name: "meeting", id: meeting.id }) },
        });
        void notify(`${meeting.title} is ready`, body);
      }
    }
  }, 4000);

  const openMeeting = useCallback((id: number, at?: number) => setView({ name: "meeting", id, at }), []);
  // Stable, so the recording provider doesn't re-subscribe to the hotkey on every render.
  const onSaved = useCallback((id: number) => setView({ name: "meeting", id }), []);

  // "Brief me" on a person asks the question right away and shows the Ask page.
  const brief = (name: string) => {
    if (!library.ask(briefQuestion(name))) {
      toast("Still answering your last question", { description: "Ask for the brief again when it's done." });
    }
    setView({ name: "ask" });
  };

  const chooseFile = async () => {
    if (isTauri()) {
      const { open } = await import("@tauri-apps/plugin-dialog");
      const picked = await open({ multiple: false, filters: [{ name: "Recordings", extensions: MEDIA }] });
      if (typeof picked === "string") setImportPath(picked);
    } else {
      const typed = window.prompt("Full path of the recording to import");
      if (typed) setImportPath(typed);
    }
  };

  return (
    <RecordingProvider onSaved={onSaved}>
      <div className="flex h-screen overflow-hidden bg-background text-foreground">
        <Sidebar
          view={view}
          openActions={stats?.open_actions ?? 0}
          engineOk={!engineError}
          onNavigate={setView}
          onImport={() => void chooseFile()}
        />
        <main className="flex min-w-0 flex-1 flex-col">
          {engineStopped && <EngineStopped message={engineStopped} onRestarted={() => setEngineStopped(null)} />}
          <RecordingBar />
          <CallWatcher />
          <GlobalKeys />
          <DictationKeys />
          <ErrorBoundary key={JSON.stringify(view)} onReset={() => setView({ name: "meetings" })}>
            {view.name === "meetings" && (
              <MeetingsView onOpen={openMeeting} onSettings={() => setView({ name: "settings" })} onImport={() => void chooseFile()} />
            )}
            {view.name === "meeting" && (
              <MeetingView key={`${view.id}-${view.at ?? ""}`} id={view.id} at={view.at} onBack={() => setView({ name: "meetings" })} />
            )}
            {view.name === "ask" && <AskView library={library} onOpen={openMeeting} />}
            {view.name === "actions" && <ActionsView onOpen={openMeeting} />}
            {view.name === "worklog" && <WorkLogView onOpen={openMeeting} />}
            {view.name === "people" && (
              <PeopleView onBrief={brief} />
            )}
            {view.name === "search" && <SearchView onOpen={openMeeting} />}
            {view.name === "topics" && (
              <TopicsView onOpen={openMeeting} onSettings={() => setView({ name: "settings" })} />
            )}
            {view.name === "settings" && <SettingsView />}
          </ErrorBoundary>
        </main>
      </div>
      <ImportDialog path={importPath} onClose={() => setImportPath(null)} onImported={onSaved} />
    </RecordingProvider>
  );
}

export default function App() {
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string>();
  const [setup, setSetup] = useState<string>();

  const start = useCallback(() => {
    setError(undefined);
    setSetup(undefined);
    connect()
      .then(() => waitForEngine(undefined, setSetup))
      .then(() => setReady(true))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, []);
  useEffect(start, [start]);

  const retry = async () => {
    try {
      await invoke("restart_engine");
    } catch (e) {
      setError(String(e));
      return;
    }
    start();
  };

  if (!ready) return <Startup error={error} setup={setup} onRetry={isTauri() ? () => void retry() : undefined} />;
  return <Shell />;
}
