import {
  ArrowLeft,
  Captions,
  Check,
  ClipboardCopy,
  FileDown,
  FileText,
  Loader2,
  Lock,
  LockOpen,
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
import { PrivateNote } from "@/components/PrivateNote";
import { RedactDialog } from "@/components/RedactDialog";
import { Related } from "@/components/Related";
import { SummaryPanel } from "@/components/SummaryPanel";
import { ClientField } from "@/components/Client";
import { TicketControl } from "@/components/Ticket";
import { NamePrompt, REDACTED, Transcript, WhoIsThis } from "@/components/Transcript";
import { AvatarStack, ConversationStats, SpeakerLanes, TalkTimeBars } from "@/components/Visuals";
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
import { clientHint, followsOffered } from "@/lib/clients";
import {
  api,
  isTauri,
  mediaUrl,
  type MeetingDetail,
  type Person,
  type RedactResult,
  type RelatedMeeting,
  type Segment,
  type Settings,
  type Speaker,
} from "@/lib/engine";
import { announce, attempt, copyText, reason } from "@/lib/feedback";
import { clock, colorMap, day, minutes, when } from "@/lib/format";
import { kindShown, pickAfterSummary } from "@/lib/kinds";
import { relatedKey } from "@/lib/related";
import { modelLabel, writerFor, type Writer } from "@/lib/writer";
import { StatusBadge } from "@/views/MeetingsView";

type Track = "mix" | "mic" | "system";
const TRACK_LABEL: Record<Track, string> = { mix: "Everyone", mic: "Your mic only", system: "Call audio only" };

/** Pauses longer than this are skipped when "skip silences" is on. */
const SILENCE_GAP = 1.5;
const SKIP_KEY = "zachcap-skip-silences";

/** Stretches where anyone is talking: lines merged when they overlap or nearly touch. A
 * redacted line is silence now (its audio was silenced), so it isn't speech. */
function speechSpans(segments: { start: number; end: number; text: string }[]): { start: number; end: number }[] {
  const spans: { start: number; end: number }[] = [];
  const spoken = segments.filter((segment) => segment.text !== REDACTED);
  for (const segment of spoken.sort((a, b) => a.start - b.start)) {
    const last = spans.at(-1);
    if (last && segment.start <= last.end + 0.3) last.end = Math.max(last.end, segment.end);
    else spans.push({ start: segment.start, end: segment.end });
  }
  return spans;
}

/** The first stretch of speech that hasn't ended by ``time`` (binary search). */
function nextSpan(spans: { start: number; end: number }[], time: number) {
  let lo = 0;
  let hi = spans.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (spans[mid].end <= time) lo = mid + 1;
    else hi = mid;
  }
  return spans[lo];
}

export function MeetingView({
  id,
  at,
  onBack,
  onOpen,
}: {
  id: number;
  at?: number;
  onBack: () => void;
  onOpen: (meetingId: number) => void;
}) {
  const [busy, setBusy] = useState(false);
  const { data, error, refresh } = usePoll(() => api<MeetingDetail>(`/api/meetings/${id}`), busy ? 1500 : 0, id);
  const { data: people = [], refresh: refreshPeople } = usePoll(() => api<Person[]>("/api/people"), 0);
  const { data: settings } = usePoll(() => api<Settings>("/api/settings"), 0);
  const audio = useRef<HTMLAudioElement>(null);
  const [player, setPlayer] = useState<PlayerState>({ playing: false, time: 0, duration: 0, rate: 1 });
  const [solo, setSolo] = useState<string | null>(null);
  const [skipSilences, setSkipSilences] = useState(() => localStorage.getItem(SKIP_KEY) === "1");
  useEffect(() => {
    if (skipSilences) localStorage.setItem(SKIP_KEY, "1");
    else localStorage.removeItem(SKIP_KEY);
  }, [skipSilences]);
  // A cited moment (from Ask, search or an action item) opens on the transcript at that line.
  const [tab, setTab] = useState<string | null>(at != null ? "transcript" : null);
  // The kind of meeting picked for the summary; null leaves it to the engine (this meeting's
  // kind, or the usual kind from Settings).
  const [kind, setKind] = useState<string | null>(null);
  // Who writes, when picked here: Claude or the model on this PC (null: the usual one).
  const [writerPicked, setWriterPicked] = useState<Writer | null>(null);
  // Asked of the model on this PC from here, and not done yet.
  const [writingHere, setWritingHere] = useState(0);
  const [editingTitle, setEditingTitle] = useState(false);
  const [title, setTitle] = useState("");
  const [renaming, setRenaming] = useState<Speaker | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [summarizing, setSummarizing] = useState(false);
  // Also as a ref: a toast's "Summarize again" keeps the closure from when it was shown.
  const summarizingNow = useRef(false);
  const [redacting, setRedacting] = useState<Segment | null>(null);
  // The line button that opened the redaction dialog, and the moment, to give focus back to
  // (or to the line now at that moment, when the line was removed).
  const redactOpener = useRef<{ from: HTMLElement | null; at: number } | null>(null);
  const openRedact = useCallback((segment: Segment, from: HTMLElement | null) => {
    redactOpener.current = { from, at: segment.start };
    setRedacting(segment);
  }, []);
  const moreButton = useRef<HTMLButtonElement>(null);
  // Bumped after a redaction, so the player loads the silenced audio, not a cached copy.
  const [audioVersion, setAudioVersion] = useState(0);
  const pendingSeek = useRef<{ time: number; play: boolean } | null>(at != null ? { time: at, play: false } : null);

  const meeting = data?.meeting;
  const progress = meeting?.progress ?? null;
  const failedNow = progress?.startsWith("failed:") ?? false;
  // A job is running only while the engine reports on it. "processing" or "recorded" with
  // nothing reported means it was cut off (ZachCap closed mid-job) and can be run again.
  const processing = progress === "queued" || (meeting?.status === "processing" && progress != null && !failedNow);
  const stalled = !processing && (meeting?.status === "processing" || meeting?.status === "recorded");
  const writing = data?.writing ?? null;
  const followed = processing || writing !== null || writingHere > 0;
  useEffect(() => setBusy(followed), [followed]);
  // Written on this PC for this meeting before its page was opened again: shown once done.
  const summarizingHere = summarizing || writing?.what === "summary";
  // Other meetings about the same thing: asked for once this one is loaded, and again after each
  // change made on the page (its client, a name, a line).
  const relatedOf = relatedKey(data, processing);
  const { data: related = [] } = usePoll(
    () => (relatedOf ? api<RelatedMeeting[]>(`/api/meetings/${id}/related`) : Promise.resolve([])),
    0,
    relatedOf,
  );
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
  const spans = useMemo(() => speechSpans(data?.segments ?? []), [data?.segments]);
  const track: Track = useMemo(() => {
    if (!solo || soloSegments.length === 0) return "mix";
    const tracks = new Set(soloSegments.map((s) => s.track));
    return tracks.size === 1 ? [...tracks][0] : "mix";
  }, [solo, soloSegments]);
  const src =
    data && !data.meeting.audio_removed_at
      ? mediaUrl(
          `/api/meetings/${id}/audio?track=${track}${audioVersion ? `&v=${audioVersion}` : ""}`,
        )
      : undefined;

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
    } else if (skipSilences && !element.paused) {
      const t = element.currentTime;
      const next = nextSpan(spans, t);
      if (next && next.start - t > SILENCE_GAP) element.currentTime = next.start - 0.25;
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
    // Space or K plays and pauses; Left and Right move 5 s, J and L 10 s (as in video players).
    const moves: Record<string, number> = { ArrowLeft: -5, ArrowRight: 5, KeyJ: -10, KeyL: 10 };
    // Whether the focused control got focus from the keyboard (Tab): it showed a focus ring
    // as it got focus. Asked then, not at the key press, which makes any focus "visible".
    let keyboardFocus = false;
    const onFocus = (event: FocusEvent) => {
      keyboardFocus = event.target instanceof Element && event.target.matches(":focus-visible");
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.ctrlKey || event.altKey || event.metaKey) return;
      const toggle = event.code === "Space" || event.code === "KeyK";
      if (!toggle && !(event.code in moves)) return;
      const target = event.target as HTMLElement;
      if (
        target.closest(
          "input, textarea, select, [contenteditable], [role=menu], [role=menuitem], [role=dialog], [role=listbox], [role=tab], [role=slider]",
        )
      ) {
        return;
      }
      // A keyboard user on a button, checkbox or switch presses it with Space. A control that
      // a click focused isn't pressed again: Space plays and pauses then, as before.
      const control = target.closest("button, a[href], [role=button], [role=checkbox], [role=switch], [role=radio]");
      if (event.code === "Space" && control && keyboardFocus) return;
      event.preventDefault();
      const element = audio.current;
      if (!element) return;
      if (toggle) {
        if (event.repeat) return;
        if (element.paused) void element.play();
        else element.pause();
      } else {
        const end = Number.isFinite(element.duration) ? element.duration : Infinity;
        element.currentTime = Math.max(0, Math.min(end, element.currentTime + moves[event.code]));
      }
    };
    window.addEventListener("focusin", onFocus, true);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("focusin", onFocus, true);
      window.removeEventListener("keydown", onKey);
    };
  }, []);

  const reprocess = async () => {
    if (await attempt("Couldn't start processing", () => api(`/api/meetings/${id}/process`, { method: "POST" }))) {
      setBusy(true);
      await refresh();
    }
  };

  const summarize = async () => {
    if (summarizingNow.current) return;
    summarizingNow.current = true;
    setTab("summary");
    setSummarizing(true);
    const by = writerFor(writerPicked, meeting?.private ?? false, settings);
    const here = by === "local";
    const toastId = here
      ? toast("Writing the summary on this PC", { description: "It takes a few minutes. You can go on using ZachCap." })
      : toast.loading("Summarizing with Claude...");
    if (here) setWritingHere((count) => count + 1);
    const sent = kind;
    try {
      await api(`/api/meetings/${id}/summary`, { method: "POST", json: { template: sent, by } });
      // Minutes later, the window may be behind others: said where it's seen.
      if (here) void announce("Summary ready", meeting?.title, "success");
      else toast.success("Summary ready", { id: toastId });
      await refresh();
      setKind((now) => pickAfterSummary(now, sent));
    } catch (error) {
      const why = reason(error);
      if (here && why === "Stopped.") toast("Stopped", { description: "The summary wasn't changed." });
      else toast.error("Couldn't create the summary", { id: here ? undefined : toastId, description: why });
    } finally {
      summarizingNow.current = false;
      setSummarizing(false);
      if (here) setWritingHere((count) => count - 1);
    }
  };

  const stopWriting = () =>
    attempt("Couldn't stop it", () => api(`/api/meetings/${id}/writing/stop`, { method: "POST" }));

  const saveTitle = async () => {
    setEditingTitle(false);
    if (!title.trim() || title.trim() === meeting?.title) return;
    await attempt("Couldn't rename the meeting", () =>
      api(`/api/meetings/${id}`, { method: "PATCH", json: { title: title.trim() } }),
    );
    await refresh();
  };

  /** The meeting's client ("" for none, null to take its people's again); done once the page
   * shows what the engine stored. */
  const saveClient = async (client: string | null) => {
    const saved = await attempt("Couldn't save the client", () =>
      api(`/api/meetings/${id}`, { method: "PATCH", json: { client } }),
    );
    if (saved) await refresh();
  };

  /** The service ticket it was about ("" takes it off); done once the page shows what the
   * engine stored. */
  const saveTicket = async (ticket: string) => {
    const saved = await attempt("Couldn't save the ticket", () =>
      api(`/api/meetings/${id}`, { method: "PATCH", json: { ticket } }),
    );
    if (saved) await refresh();
  };

  /** Keeps the meeting from Claude, or lets Claude read it. */
  const savePrivate = async (on: boolean) => {
    const saved = await attempt(on ? "Couldn't make the meeting private" : "Couldn't change the meeting", () =>
      api(`/api/meetings/${id}`, { method: "PATCH", json: { private: on } }),
    );
    if (!saved) return;
    await refresh();
    toast.success(on ? "Kept from Claude" : "Claude can read it", {
      description: on
        ? "Nothing in this meeting is sent to Claude from now on. A summary it already has stays."
        : "Summaries, documents, Ask and the MCP server can use this meeting.",
    });
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

  const redact = async (start: number, end: number) => {
    // Windows won't rewrite a file that is being played: let go of it first.
    const element = audio.current;
    if (element) {
      element.pause();
      element.removeAttribute("src");
      element.load();
    }
    try {
      const done = await api<RedactResult>(`/api/meetings/${id}/redact`, { method: "POST", json: { start, end } });
      // The lines, notes and action items as they are now, before the dialog closes, so
      // focus can go to the line that is now at that moment.
      await Promise.all([refresh(), refreshPeople()]);
      setRedacting(null);
      const lines = done.lines_removed + done.lines_redacted;
      const notes = done.notes_removed ?? 0;
      const changed = [
        `${lines} line${lines === 1 ? "" : "s"} changed.`,
        notes ? `${notes} note${notes === 1 ? "" : "s"} or snapshot${notes === 1 ? "" : "s"} from then removed.` : "",
        done.summary_removed
          ? "The summary and action items were removed too, since they may repeat it."
          : done.has_summary
            ? "The summary was written before: regenerate it if it mentions this."
            : "",
      ];
      const again =
        (done.summary_removed || done.has_summary) &&
        writerFor(writerPicked, meeting?.private ?? false, settings) !== null;
      toast.success(`Redacted ${clock(start)} to ${clock(end)}`, {
        description: changed.filter(Boolean).join(" "),
        duration: 12_000,
        action: again
          ? { label: done.summary_removed ? "Summarize again" : "Regenerate summary", onClick: () => void summarize() }
          : undefined,
      });
      if (done.audio_error) {
        toast.warning("The audio wasn't silenced", {
          description: `The words were removed, but the audio couldn't be silenced: ${done.audio_error}. Try again.`,
          duration: 20_000,
        });
      }
      return true;
    } catch (error) {
      toast.error("Couldn't redact", { description: reason(error) });
      return false;
    } finally {
      setAudioVersion((version) => version + 1);
    }
  };

  /** Focus after the redaction dialog: the line's button, or the line now at that moment
   * when the redacted line was removed. */
  const redactClosed = (event: Event) => {
    const opener = redactOpener.current;
    redactOpener.current = null;
    if (!opener) return;
    let target = opener.from?.isConnected ? opener.from : null;
    if (!target) {
      const rows = [...document.querySelectorAll<HTMLElement>("[data-segment][data-start]")];
      const row = rows.find((r) => Number(r.dataset.start) >= opener.at - 0.01) ?? rows.at(-1);
      target = row?.querySelector<HTMLElement>("button[aria-label='More for this line']") ?? null;
    }
    if (target) {
      event.preventDefault();
      target.focus({ preventScroll: true });
    }
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
  const shownKind = kindShown(kind, meeting, settings?.summary_template);
  const writer = writerFor(writerPicked, meeting.private ?? false, settings);

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
            <ClientField
              value={meeting.client ?? null}
              inferred={meeting.client_inferred || meeting.client_kept}
              known={data.clients_known ?? []}
              empty={meeting.client_set ? "No client" : "Add a client"}
              hint={clientHint(meeting)}
              onSave={saveClient}
            />
            {followsOffered(meeting) && (
              <button
                onClick={() => void saveClient(null)}
                title="Take the client the people in this meeting work for now"
                className="text-xs transition-colors hover:text-foreground"
              >
                From its people
              </button>
            )}
            {!meeting.client &&
              !meeting.client_set &&
              (data.clients_heard ?? []).length > 1 &&
              data.clients_heard.map((name) => (
                <button
                  key={name}
                  title={`Its people work for more than one client: is it ${name}'s?`}
                  onClick={() => void saveClient(name)}
                  className="rounded-md border border-dashed px-1.5 py-0.5 text-xs transition-colors hover:bg-muted hover:text-foreground"
                >
                  {name}?
                </button>
              ))}
            <TicketControl
              ticket={meeting.ticket ?? null}
              suggestions={data.ticket_suggestions ?? []}
              onSave={saveTicket}
            />
          </div>
        </div>
        {meeting.private && (
          <Badge variant="outline" className="gap-1" title="Nothing in this meeting is sent to Claude">
            <Lock className="size-3" />
            Private
          </Badge>
        )}
        <StatusBadge meeting={meeting} />
        {!data.summary && data.segments.length > 0 && !processing && writer !== null && (
          <Button size="sm" disabled={summarizingHere} onClick={() => void summarize()}>
            {summarizingHere ? <Loader2 className="size-3.5 animate-spin" /> : <Sparkles className="size-3.5" />}
            Summarize
          </Button>
        )}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button ref={moreButton} size="icon" variant="outline" className="size-8" aria-label="More">
              <MoreHorizontal className="size-4" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-52">
            <DropdownMenuItem disabled={processing || !!meeting.audio_removed_at} onSelect={() => void reprocess()}>
              <RotateCw className="size-4" />
              Reprocess audio
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={copyTranscript}>
              <ClipboardCopy className="size-4" />
              Copy transcript
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => void savePrivate(!meeting.private)}>
              {meeting.private ? <LockOpen className="size-4" /> : <Lock className="size-4" />}
              {meeting.private ? "Let Claude read it" : "Keep from Claude"}
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
              {progress === "queued" ? "Waiting its turn..." : (progress ?? "Working...")}
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
          {meeting.audio_removed_at && (
            <div className="mb-4 rounded-xl border bg-muted/40 px-4 py-3 text-sm text-muted-foreground">
              The recording was deleted on {day(meeting.audio_removed_at)} by your "keep recordings" setting. The
              transcript, summary and notes are kept.
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
              mentions={data.mentions}
              onSeek={seek}
              onSolo={setSolo}
            />
          )}

          <Related meetings={related} onOpen={onOpen} />

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
              <AskPanel
                meetingId={id}
                here={meeting.private && writer === "local" ? (settings?.local_model ?? null) : null}
                answering={writing?.what === "answer"}
                onSeek={seek}
              >
                {writer === null && (
                  <PrivateNote
                    what="so it can't be asked about it. A model on this PC could answer: see Summaries in Settings."
                    onAllow={() => void savePrivate(false)}
                  />
                )}
              </AskPanel>
            </TabsContent>

            <TabsContent value="summary" forceMount className="data-[state=inactive]:hidden">
              <SummaryPanel
                meetingId={id}
                title={meeting.title}
                summary={data.summary}
                items={data.action_items}
                me={data.me}
                colorOf={colorOf}
                templates={settings?.templates ?? []}
                recipes={settings?.recipes ?? []}
                kind={shownKind.value}
                pickedAutomatically={shownKind.automatic}
                unpicked={shownKind.unpicked}
                isPrivate={meeting.private ?? false}
                onAllow={() => void savePrivate(false)}
                onKindChange={setKind}
                writer={writer}
                writerNames={{
                  claude: modelLabel(settings?.summary_model ?? "") || "Claude",
                  local: `${settings?.local_model ?? ""} on this PC`,
                }}
                onWriterChange={settings?.local_model && !meeting.private ? setWriterPicked : null}
                writing={writing}
                onWritingHere={(on) => setWritingHere((count) => count + (on ? 1 : -1))}
                onStop={() => void stopWriting()}
                summarizing={summarizingHere}
                onSummarize={() => void summarize()}
                onSeek={seek}
                onChanged={() => void refresh()}
              />
            </TabsContent>

            {/* Kept mounted, like Ask: a cited line is scrolled to once (not on every return to
                the tab), and a line being fixed survives a look at another tab. */}
            <TabsContent value="transcript" forceMount className="data-[state=inactive]:hidden">
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
                focusAt={at}
                mentions={data.mentions}
                topics={data.topics}
                solo={solo}
                onSeek={seek}
                onChanged={refreshAll}
                onRedact={openRedact}
              />
            </TabsContent>

            <TabsContent value="speakers">
              <div className="grid gap-5 lg:grid-cols-[1fr_1.2fr]">
                <div className="space-y-5">
                  <div className="rounded-xl border bg-card p-4">
                    <h3 className="mb-3 text-xs font-medium tracking-wide text-muted-foreground uppercase">Talk time</h3>
                    <TalkTimeBars talk={data.talk_time} colorOf={colorOf} />
                  </div>
                  {data.insights.speakers.length > 0 && (
                    <div className="rounded-xl border bg-card p-4">
                      <h3 className="mb-3 text-xs font-medium tracking-wide text-muted-foreground uppercase">
                        Conversation
                      </h3>
                      <ConversationStats insights={data.insights} colorOf={colorOf} onSeek={seek} />
                    </div>
                  )}
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
      {!processing && data.segments.length > 0 && !meeting.audio_removed_at && (
        <PlayerBar
          audio={audio}
          state={player}
          solo={solo}
          trackLabel={TRACK_LABEL[track]}
          skipSilences={skipSilences}
          onSkipSilences={setSkipSilences}
          onClearSolo={() => setSolo(null)}
        />
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
      <RedactDialog
        line={redacting}
        segments={data.segments}
        duration={duration}
        hasSummary={data.summary !== null}
        audioRemoved={!!meeting.audio_removed_at}
        onCancel={() => setRedacting(null)}
        onConfirm={redact}
        onCloseAutoFocus={redactClosed}
      />
      <Dialog open={confirmDelete} onOpenChange={setConfirmDelete}>
        <DialogContent
          className="sm:max-w-sm"
          onCloseAutoFocus={(event) => {
            // Opened from the More menu, not a trigger: focus goes back to that button.
            if (moreButton.current?.isConnected) {
              event.preventDefault();
              moreButton.current.focus({ preventScroll: true });
            }
          }}
        >
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
