import { AudioLines, FileUp, Loader2 } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { CallWatcher, RecordingBar, RecordingProvider } from "@/components/Recording";
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
import { usePoll } from "@/hooks/usePoll";
import { api, connect, isTauri, waitForEngine, type Meeting, type Stats } from "@/lib/engine";
import { ActionsView } from "@/views/ActionsView";
import { MeetingView } from "@/views/MeetingView";
import { MeetingsView } from "@/views/MeetingsView";
import { PeopleView } from "@/views/PeopleView";
import { SearchView } from "@/views/SearchView";
import { SettingsView } from "@/views/SettingsView";

const MEDIA = ["mp4", "m4a", "mp3", "wav", "flac", "ogg", "webm", "mkv", "aac", "wma", "mov"];

function Startup({ error }: { error?: string }) {
  return (
    <div className="flex h-screen select-none flex-col items-center justify-center gap-3 text-center">
      <div className="flex size-12 items-center justify-center rounded-xl bg-foreground text-background">
        <AudioLines className="size-6" />
      </div>
      {error ? (
        <>
          <p className="font-medium">The ZachCap engine didn't start</p>
          <p className="max-w-md text-sm text-muted-foreground">{error}</p>
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

async function notify(title: string, body: string) {
  if (!isTauri()) return;
  try {
    const { isPermissionGranted, requestPermission, sendNotification } = await import("@tauri-apps/plugin-notification");
    if (!(await isPermissionGranted()) && (await requestPermission()) !== "granted") return;
    sendNotification({ title, body });
  } catch {
    // Notifications are a nicety; the in-app toast already told the user.
  }
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
      toast.error("Couldn't import", { description: String(error) });
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
  const { data: stats } = usePoll(() => api<Stats>("/api/stats"), 5000);

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
    <RecordingProvider onSaved={(id) => setView({ name: "meeting", id })}>
      <div className="flex h-screen overflow-hidden bg-background text-foreground">
        <Sidebar view={view} openActions={stats?.open_actions ?? 0} onNavigate={setView} onImport={() => void chooseFile()} />
        <main className="flex min-w-0 flex-1 flex-col">
          <RecordingBar />
          <CallWatcher />
          {view.name === "meetings" && (
            <MeetingsView onOpen={openMeeting} onSettings={() => setView({ name: "settings" })} onImport={() => void chooseFile()} />
          )}
          {view.name === "meeting" && (
            <MeetingView key={`${view.id}-${view.at ?? ""}`} id={view.id} at={view.at} onBack={() => setView({ name: "meetings" })} />
          )}
          {view.name === "actions" && <ActionsView onOpen={openMeeting} />}
          {view.name === "people" && <PeopleView />}
          {view.name === "search" && <SearchView onOpen={openMeeting} />}
          {view.name === "settings" && <SettingsView />}
        </main>
      </div>
      <ImportDialog path={importPath} onClose={() => setImportPath(null)} onImported={(id) => setView({ name: "meeting", id })} />
    </RecordingProvider>
  );
}

export default function App() {
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string>();

  useEffect(() => {
    connect()
      .then(() => waitForEngine())
      .then(() => setReady(true))
      .catch((e) => setError(String(e)));
  }, []);

  if (!ready) return <Startup error={error} />;
  return <Shell />;
}
