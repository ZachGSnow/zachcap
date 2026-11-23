import {
  ArrowLeft,
  Captions,
  Check,
  ClipboardCopy,
  FileDown,
  FileText,
  Loader2,
  MoreHorizontal,
  Pencil,
  RotateCw,
  Sparkles,
  Trash2,
  UserRoundSearch,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { AskPanel } from "@/components/AskPanel";
import { NotesPanel } from "@/components/NotesPanel";
import { PlayerBar, type PlayerState } from "@/components/PlayerBar";
import { SummaryPanel } from "@/components/SummaryPanel";
import { NamePrompt, Transcript, WhoIsThis } from "@/components/Transcript";
import { AvatarStack, SpeakerLanes, TalkTimeBars } from "@/components/Visuals";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { usePoll } from "@/hooks/usePoll";
import { api, isTauri, mediaUrl, type MeetingDetail, type Person, type Settings, type Speaker } from "@/lib/engine";
import { attempt, copyText } from "@/lib/feedback";
import { clock, colorMap, minutes, when } from "@/lib/format";
import { StatusBadge } from "@/views/MeetingsView";

type Track = "mix" | "mic" | "system";
const TRACK_LABEL: Record<Track, string> = { mix: "Everyone", mic: "Your mic only", system: "Call audio only" };

export function MeetingView({ id, at, onBack }: { id: number; at?: number; onBack: () => void }) {
  const [busy, setBusy] = useState(false);
  const { data, error, refresh } = usePoll(() => api<MeetingDetail>(`/api/meetings/${id}`), busy ? 1500 : 0, id);
  const { data: people = [], refresh: refreshPeople } = usePoll(() => api<Person[]>("/api/people"), 0);
  const { data: settings } = usePoll(() => api<Settings>("/api/settings"), 0);
  const audio = useRef<HTMLAudioElement>(null);
  const [player, setPlayer] = useState<PlayerState>({ playing: false, time: 0, duration: 0, rate: 1 });
  const [solo, setSolo] = useState<string | null>(null);
  const [tab, setTab] = useState<string | null>(null);
  const [editingTitle, setEditingTitle] = useState(false);
  const [title, setTitle] = useState("");
  const [renaming, setRenaming] = useState<Speaker | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [summarizing, setSummarizing] = useState(false);
  const pendingSeek = useRef<{ time: number; play: boolean } | null>(at != null ? { time: at, play: false } : null);

  const meeting = data?.meeting;
  const progress = meeting?.progress ?? null;
  const failedNow = progress?.startsWith("failed:") ?? false;
  // A job is running only while the engine reports on it. "processing" or "recorded" with
  // nothing reported means it was cut off (ZachCap closed mid-job) and can be run again.
  const processing = progress === "queued" || (meeting?.status === "processing" && progress != null && !failedNow);
  const stalled = !processing && (meeting?.status === "processing" || meeting?.status === "recorded");
  useEffect(() => setBusy(processing), [processing]);
  useEffect(() => {
    if (data && tab === null) setTab(data.summary ? "summary" : "transcript");
  }, [data, tab]);

  const refreshAll = useCallback(() => void Promise.all([refresh(), refreshPeople()]), [refresh, refreshPeople]);

  const colorOf = useMemo(
    () => colorMap((data?.talk_time ?? []).map((t) => t.speaker), data?.me ?? "You"),
    [data?.talk_time, data?.me],
  );

  // Audio isolation: play the soloed person's own track and skip everyone else's lines.
  const soloSegments = useMemo(
    () => (solo ? (data?.segments ?? []).filter((s) => s.speaker === solo) : []),
    [solo, data?.segments],
  );
  const track: Track = useMemo(() => {
    if (!solo || soloSegments.length === 0) return "mix";
    const tracks = new Set(soloSegments.map((s) => s.track));
    return tracks.size === 1 ? [...tracks][0] : "mix";
  }, [solo, soloSegments]);
  const src = data ? mediaUrl(`/api/meetings/${id}/audio${track === "mix" ? "" : `?track=${track}`}`) : undefined;

  // The last position and play state, kept outside React state: when the source switches
  // between tracks the element resets to 0 before any effect runs.
  const last = useRef({ time: 0, playing: false });
  useEffect(() => {
    if (!src) return;
    pendingSeek.current = pendingSeek.current ?? { time: last.current.time, play: last.current.playing };
  }, [src]);

  const sync = (element: HTMLAudioElement) => {
    if (element.readyState > 0) last.current = { time: element.currentTime, playing: !element.paused };
    setPlayer({
      playing: !element.paused,
      time: element.currentTime,
      duration: Number.isFinite(element.duration) ? element.duration : (meeting?.duration ?? 0),
      rate: element.playbackRate,
    });
  };

  const onTime = (element: HTMLAudioElement) => {
    if (solo && !element.paused && soloSegments.length) {
      const t = element.currentTime;
      const inside = soloSegments.some((s) => s.start - 0.05 <= t && t < s.end);
      if (!inside) {
        const next = soloSegments.find((s) => s.start > t);
        if (next) element.currentTime = next.start;
        else {
          // Past their last line: back to their first, so Play starts over instead of stalling.
          element.pause();
          element.currentTime = soloSegments[0].start;
          toast(`That was ${solo}'s last line`, { description: "Play starts again from their first." });
        }
      }
    }
    sync(element);
  };

  const seek = useCallback((seconds: number) => {
    const element = audio.current;
    if (!element) return;
    if (element.readyState === 0) {
      // Still loading (the first open of a long meeting builds its audio): seek once it can.
      pendingSeek.current = { time: seconds, play: true };
      return;
    }
    element.currentTime = seconds;
    void element.play();
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.code !== "Space" || event.defaultPrevented || event.repeat) return;
      const target = event.target as HTMLElement;
      if (target.closest("input, textarea, select, [contenteditable], [role=menu], [role=menuitem], [role=dialog], [role=listbox]")) {
        return;
      }
      // Also stops Space from clicking whichever button was clicked last.
      event.preventDefault();
      const element = audio.current;
      if (!element) return;
      if (element.paused) void element.play();
      else element.pause();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const reprocess = async () => {
    if (await attempt("Couldn't start processing", () => api(`/api/meetings/${id}/process`, { method: "POST" }))) {
      setBusy(true);
      await refresh();
    }
  };

  const summarize = async (template?: string) => {
    if (summarizing) return;
    setTab("summary");
    setSummarizing(true);
    const toastId = toast.loading("Summarizing with Claude...");
    try {
      await api(`/api/meetings/${id}/summary`, { method: "POST", json: { template: template ?? null } });
      toast.success("Summary ready", { id: toastId });
      await refresh();
    } catch (error) {
      toast.error("Couldn't create the summary", { id: toastId, description: String(error) });
    } finally {
      setSummarizing(false);
    }
  };

  const saveTitle = async () => {
    setEditingTitle(false);
    if (!title.trim() || title.trim() === meeting?.title) return;
    await attempt("Couldn't rename the meeting", () =>
      api(`/api/meetings/${id}`, { method: "PATCH", json: { title: title.trim() } }),
    );
    await refresh();
  };

  // A file the user picks in the save dialog (or a download, outside the desktop app).
  const exportAs = (format: "md" | "txt" | "srt") =>
    attempt("Couldn't export the meeting", async () => {
      const { name } = await api<{ name: string }>(`/api/meetings/${id}/export-name?format=${format}`);
      if (!isTauri()) {
        const blob = await (await fetch(mediaUrl(`/api/meetings/${id}/export?format=${format}`))).blob();
        const link = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: name });
        link.click();
        URL.revokeObjectURL(link.href);
        return;
      }
      const { save } = await import("@tauri-apps/plugin-dialog");
      const path = await save({ defaultPath: name, filters: [{ name: format.toUpperCase(), extensions: [format] }] });
      if (!path) return;
      const saved = await api<{ path: string }>(`/api/meetings/${id}/export`, { method: "POST", json: { format, path } });
      toast.success("Exported", { description: saved.path });
    });

  const copyTranscript = () => {
    const text = (data?.segments ?? []).map((s) => `[${clock(s.start)}] ${s.speaker}: ${s.text}`).join("\n");
    void copyText(`${meeting?.title}\n\n${text}`, "Transcript copied");
  };

  const remove = async () => {
    setDeleting(true);
    const removed = await attempt("Couldn't delete the meeting", () => api(`/api/meetings/${id}`, { method: "DELETE" }));
    setDeleting(false);
    if (!removed) return;
    toast.success("Meeting deleted", { description: "Its audio, transcript and the voice samples it taught were removed." });
    onBack();
  };

  const nameSpeaker = async (speaker: Speaker, name: string) => {
    const named = await attempt(`Couldn't name ${speaker.display_name}`, () =>
      api(`/api/meetings/${id}/speakers/${speaker.id}/name`, { method: "POST", json: { name } }),
    );
    if (named) toast.success(`${speaker.display_name} is now ${name}`);
    refreshAll();
  };

  const confirmAll = async () => {
    await attempt("Couldn't confirm the matches", async () => {
      const { confirmed } = await api<{ confirmed: number }>(`/api/meetings/${id}/confirm`, { method: "POST" });
      toast.success(`Confirmed ${confirmed} speaker${confirmed === 1 ? "" : "s"}`);
    });
    refreshAll();
  };

  if (!data && error) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8 text-center">
        <p className="font-medium">This meeting didn't load</p>
        <p className="max-w-md text-sm text-muted-foreground">{error.message}</p>
        <div className="flex gap-2">
          <Button variant="outline" onClick={onBack}>
            Back
          </Button>
          <Button onClick={() => void refresh()}>Try again</Button>
        </div>
      </div>
    );
  }

  if (!data || !meeting) {
    return (
      <div className="space-y-3 p-8">
        <Skeleton className="h-7 w-72" />
        <Skeleton className="h-4 w-48" />
        <Skeleton className="mt-6 h-32 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  const unnamed = data.speakers.filter((s) => s.status === "unknown" || s.status === "suggested");
  const current = data.segments.findLast((s) => s.start <= player.time + 0.05);
  const named = data.talk_time.map((t) => t.speaker);
  const participants = named.filter((name) => !/^Speaker \d+|Call audio|Unknown/.test(name));
  const duration = meeting.duration ?? data.segments.at(-1)?.end ?? 0;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <header className="flex select-none items-start gap-3 border-b px-6 py-4">
        <Button size="icon" variant="ghost" className="mt-0.5 size-8" onClick={onBack} aria-label="Back">
          <ArrowLeft className="size-4" />
        </Button>
        <div className="min-w-0 flex-1">
          {editingTitle ? (
            <Input
              autoFocus
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              onBlur={() => void saveTitle()}
              onKeyDown={(event) => {
                if (event.key === "Enter") void saveTitle();
                if (event.key === "Escape") setEditingTitle(false);
              }}
              className="h-8 max-w-lg font-heading text-lg font-semibold"
            />
          ) : (
            <button
              onClick={() => {
                setTitle(meeting.title);
                setEditingTitle(true);
              }}
              className="group flex max-w-full items-center gap-2 text-left"
              title="Rename"
            >
              <h1 className="truncate font-heading text-lg font-semibold tracking-tight">{meeting.title}</h1>
              <Pencil className="size-3.5 shrink-0 text-muted-foreground opacity-0 group-hover:opacity-100" />
            </button>
          )}
          <div className="mt-1 flex items-center gap-3 text-sm text-muted-foreground">
            <span>
              {when(meeting.started_at)}
              {duration ? ` \u00b7 ${minutes(duration)}` : ""}
              {named.length ? ` \u00b7 ${named.length} speaker${named.length === 1 ? "" : "s"}` : ""}
            </span>
            {participants.length > 0 && <AvatarStack names={participants} colorOf={colorOf} max={5} />}
          </div>
        </div>
        <StatusBadge meeting={meeting} />
        {!data.summary && data.segments.length > 0 && !processing && (
          <Button size="sm" disabled={summarizing} onClick={() => void summarize()}>
            {summarizing ? <Loader2 className="size-3.5 animate-spin" /> : <Sparkles className="size-3.5" />}
            Summarize
          </Button>
        )}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button size="icon" variant="outline" className="size-8" aria-label="More">
              <MoreHorizontal className="size-4" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-52">
            <DropdownMenuItem disabled={processing} onSelect={() => void reprocess()}>
              <RotateCw className="size-4" />
              Reprocess audio
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={copyTranscript}>
              <ClipboardCopy className="size-4" />
              Copy transcript
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={() => void exportAs("md")}>
              <FileDown className="size-4" />
              Export as Markdown
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => void exportAs("txt")}>
              <FileText className="size-4" />
              Export transcript (.txt)
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => void exportAs("srt")}>
              <Captions className="size-4" />
              Export subtitles (.srt)
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem variant="destructive" onSelect={() => setConfirmDelete(true)}>
              <Trash2 className="size-4" />
              Delete meeting
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </header>

      <ScrollArea className="min-h-0 flex-1">
        <div className="mx-auto max-w-4xl px-6 py-5">
          {processing && (
            <div className="mb-4 flex items-center gap-2 rounded-xl border bg-muted/40 px-4 py-3 text-sm text-muted-foreground">
              <Loader2 className="size-4 animate-spin" />
              {progress === "queued" ? "Waiting for the GPU..." : (progress ?? "Working...")}
            </div>
          )}
          {stalled && (
            <div className="mb-4 flex items-center gap-3 rounded-xl border bg-muted/40 px-4 py-3 text-sm">
              <span className="flex-1">
                {meeting.status === "processing"
                  ? "Processing stopped when ZachCap closed."
                  : "This recording hasn't been processed yet."}
              </span>
              <Button size="sm" onClick={() => void reprocess()}>
                <RotateCw className="size-3.5" />
                Process now
              </Button>
            </div>
          )}
          {meeting.status === "failed" && (
            <div className="mb-4 rounded-xl border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm">
              Processing failed{progress ? `: ${progress.replace(/^failed: /, "")}` : "."}
            </div>
          )}
          {failedNow && meeting.status !== "failed" && (
            <div className="mb-4 rounded-xl border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm">
              Reprocessing failed: {progress?.replace(/^failed: /, "")} The previous transcript is kept.
            </div>
          )}

          {data.segments.length > 0 && (
            <SpeakerLanes
              segments={data.segments}
              talk={data.talk_time}
              duration={player.duration || duration}
              colorOf={colorOf}
              time={player.time}
              solo={solo}
              notes={data.notes}
              onSeek={seek}
              onSolo={setSolo}
            />
          )}

          <Tabs value={tab ?? "transcript"} onValueChange={setTab} className="mt-5 gap-4">
            <TabsList>
              <TabsTrigger value="summary">Summary</TabsTrigger>
              <TabsTrigger value="transcript">
                Transcript
                {unnamed.length > 0 && (
                  <span className="ml-1.5 rounded-full bg-amber-500/20 px-1.5 text-[10px] text-amber-700 dark:text-amber-300">
                    {unnamed.length}
                  </span>
                )}
              </TabsTrigger>
              <TabsTrigger value="speakers">Speakers</TabsTrigger>
              <TabsTrigger value="notes">
                Notes
                {data.notes.length > 0 && (
                  <span className="ml-1.5 rounded-full bg-muted-foreground/15 px-1.5 text-[10px] tabular-nums">
                    {data.notes.length}
                  </span>
                )}
              </TabsTrigger>
              <TabsTrigger value="ask">Ask</TabsTrigger>
            </TabsList>

            <TabsContent value="notes">
              <NotesPanel meetingId={id} notes={data.notes} time={player.time} onSeek={seek} onChanged={() => void refresh()} />
            </TabsContent>

            {/* Kept mounted, so questions and an answer still on its way survive a tab switch. */}
            <TabsContent value="ask" forceMount className="data-[state=inactive]:hidden">
              <AskPanel meetingId={id} onSeek={seek} />
            </TabsContent>

            <TabsContent value="summary">
              <SummaryPanel
                meetingId={id}
                title={meeting.title}
                summary={data.summary}
                items={data.action_items}
                me={data.me}
                colorOf={colorOf}
                templates={settings?.templates ?? []}
                recipes={settings?.recipes ?? []}
                template={meeting.template ?? settings?.summary_template ?? "general"}
                summarizing={summarizing}
                onSummarize={(template) => void summarize(template)}
                onSeek={seek}
                onChanged={() => void refresh()}
              />
            </TabsContent>

            <TabsContent value="transcript">
              {unnamed.length > 0 && !processing && (
                <section className="mb-6">
                  <div className="mb-2 flex items-center gap-2 text-sm font-medium">
                    <UserRoundSearch className="size-4" />
                    {unnamed.length} speaker{unnamed.length === 1 ? "" : "s"} need{unnamed.length === 1 ? "s" : ""} a name
                    <span className="font-normal text-muted-foreground">
                      {"\u00b7"} play the sample, then name them once
                    </span>
                  </div>
                  <div className="space-y-2">
                    {unnamed.map((speaker) => (
                      <WhoIsThis key={speaker.id} meetingId={id} speaker={speaker} people={people} onNamed={refreshAll} />
                    ))}
                  </div>
                </section>
              )}
              <Transcript
                segments={data.segments}
                speakers={data.speakers}
                people={people}
                colorOf={colorOf}
                currentId={current?.id}
                playing={player.playing}
                solo={solo}
                onSeek={seek}
                onChanged={refreshAll}
              />
            </TabsContent>

            <TabsContent value="speakers">
              <div className="grid gap-5 lg:grid-cols-[1fr_1.2fr]">
                <div className="rounded-xl border bg-card p-4">
                  <h3 className="mb-3 text-xs font-medium tracking-wide text-muted-foreground uppercase">Talk time</h3>
                  <TalkTimeBars talk={data.talk_time} colorOf={colorOf} />
                </div>
                <div>
                  <div className="mb-2 flex items-center justify-between">
                    <h3 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">Voices</h3>
                    <Button size="sm" variant="outline" onClick={() => void confirmAll()}>
                      <Check className="size-3.5" />
                      Confirm matches
                    </Button>
                  </div>
                  <div className="divide-y overflow-hidden rounded-xl border">
                    {data.speakers
                      .filter((s) => s.status !== "minor" || (s.speech_seconds ?? 0) > 0)
                      .map((speaker, index, list) => {
                        // Diarization can split one person into two voices; say so instead of repeating the name.
                        const nth = list.slice(0, index).filter((s) => s.display_name === speaker.display_name).length;
                        return { speaker, label: nth ? `${speaker.display_name} (voice ${nth + 1})` : speaker.display_name };
                      })
                      .map(({ speaker, label }) => (
                        <div key={speaker.id} className="flex items-center gap-3 px-3.5 py-2.5 text-sm">
                          <span className={`size-2 rounded-full ${colorOf(speaker.display_name).dot}`} />
                          <span className="min-w-0 flex-1 truncate font-medium">{label}</span>
                          <span className="text-xs text-muted-foreground">
                            {speaker.track === "mic" ? "Mic" : "Call"} {"\u00b7"} {minutes(speaker.speech_seconds)}
                          </span>
                          <Badge variant="outline" className="capitalize">
                            {speaker.status === "auto" ? "recognized" : speaker.status}
                            {speaker.match_score && speaker.status !== "confirmed"
                              ? ` ${Math.round(speaker.match_score * 100)}%`
                              : ""}
                          </Badge>
                          <Button size="icon" variant="ghost" className="size-7" title="Name this voice" onClick={() => setRenaming(speaker)}>
                            <Pencil className="size-3.5" />
                          </Button>
                        </div>
                      ))}
                  </div>
                  <p className="mt-2 text-xs text-muted-foreground">
                    Naming a voice teaches ZachCap what they sound like; it is recognized in later meetings.
                  </p>
                </div>
              </div>
            </TabsContent>
          </Tabs>
        </div>
      </ScrollArea>

      <audio
        ref={audio}
        src={src}
        preload="metadata"
        onTimeUpdate={(event) => onTime(event.currentTarget)}
        onPlay={(event) => sync(event.currentTarget)}
        onPause={(event) => sync(event.currentTarget)}
        onRateChange={(event) => sync(event.currentTarget)}
        onLoadedMetadata={(event) => {
          const element = event.currentTarget;
          const pending = pendingSeek.current;
          pendingSeek.current = null;
          if (pending) {
            element.currentTime = pending.time;
            if (pending.play) void element.play();
          }
          sync(element);
        }}
      />
      {!processing && data.segments.length > 0 && (
        <PlayerBar audio={audio} state={player} solo={solo} trackLabel={TRACK_LABEL[track]} onClearSolo={() => setSolo(null)} />
      )}

      <NamePrompt
        open={renaming !== null}
        title="Name this voice"
        description="Everything this voice said gets the name, and ZachCap learns how they sound."
        initial={renaming?.person_name ?? ""}
        people={people}
        onCancel={() => setRenaming(null)}
        onSubmit={(name) => {
          if (renaming) void nameSpeaker(renaming, name);
          setRenaming(null);
        }}
      />
      <Dialog open={confirmDelete} onOpenChange={setConfirmDelete}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>Delete this meeting?</DialogTitle>
            <DialogDescription>
              The recording, transcript, summary, action items and any voice samples learned from it are removed.
              This can't be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmDelete(false)}>
              Cancel
            </Button>
            <Button variant="destructive" disabled={deleting} onClick={() => void remove()}>
              {deleting && <Loader2 className="size-4 animate-spin" />}
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
